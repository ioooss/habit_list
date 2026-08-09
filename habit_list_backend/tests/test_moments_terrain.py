"""Phase 1 product loop: explicit moments become evidence; terrain waits for formation."""
from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import MemoryClaim, MemoryEvidence, OutboxEvent, UserEvent
from app.db.models import Episodic, RawLedger
from app.memory_v2.service import EXTRACTION_REQUESTED

pytestmark = pytest.mark.anyio


async def _seed_terrain_claim(
    settings: Settings,
    *,
    day_offsets: list[int],
    sessions: list[str],
    source: str = "moment",
    mode: str = "moment",
    terrain_eligible: bool = True,
    formed: bool = False,
    terrain_state: str = "recurring",
) -> str:
    """Seed one claim plus its grounded evidence.

    ``formed`` decides whether the claim was written by the formation layer.
    Only a formed claim is terrain; a claim that merely accumulated evidence is
    a lead, and the projection has to keep those two apart.
    """

    assert len(day_offsets) == len(sessions)
    base = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    async with get_db(read_only=False) as db:
        claim = MemoryClaim(
            user_id=settings.default_user_id,
            claim_type="semantic",
            category="other" if formed else "habit",
            subject="self",
            predicate="formation" if formed else "returns_to",
            object_value="散步后更容易安静下来",
            claim_text="你似乎反复在散步后重新安静下来",
            slot_key="formation:walking-calm" if formed else "habit:walking-calm",
            content_hash=hashlib.sha256(b"walking-calm").hexdigest(),
            source_type="formation" if formed else "system_inferred",
            confidence=0.74,
            user_status="proposed",
            sensitivity="normal",
            observed_at=base.isoformat().replace("+00:00", "Z"),
            evidence_count=len(day_offsets),
            allow_proactive=False,
            terrain_state=terrain_state if formed else "forming",
            created_by_policy_version="terrain-test-v1",
        )
        db.add(claim)
        await db.flush()
        for index, (offset, session_id) in enumerate(zip(day_offsets, sessions, strict=True)):
            text = f"第 {index + 1} 次散步后，我慢慢安静下来了"
            occurred_at = (base + timedelta(days=offset)).isoformat().replace("+00:00", "Z")
            event = UserEvent(
                user_id=settings.default_user_id,
                session_id=session_id,
                request_id=f"terrain-event-{index}",
                source=source,
                mode=mode,
                content=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                occurred_at=occurred_at,
                sensitivity="normal",
                status="active",
                terrain_eligible=terrain_eligible,
            )
            db.add(event)
            await db.flush()
            db.add(
                MemoryEvidence(
                    claim_id=claim.claim_id,
                    event_id=event.event_id,
                    evidence_role="supports",
                    excerpt_start=0,
                    excerpt_end=len(text),
                    excerpt_text=text,
                    source_weight=1.0,
                    extractor_version="terrain-test-v1",
                )
            )
        await db.flush()
        return claim.claim_id


async def test_moment_permission_controls_memory_evidence_pipeline(
    client: AsyncClient,
):
    first = await client.post(
        "/api/v1/moments",
        json={
            "text": "今晚散步后，脑子终于安静了一点",
            "use_for_terrain": True,
            "allow_proactive": False,
        },
    )
    second = await client.post(
        "/api/v1/moments",
        json={
            "text": "这一句只想保存，不用于形成判断",
            "use_for_terrain": False,
            "allow_proactive": False,
        },
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["user_event_id"]
    assert second.json()["user_event_id"] is None

    async with get_db(read_only=True) as db:
        moments = list((await db.execute(Episodic.__table__.select())).all())
        ledgers = list((await db.execute(RawLedger.__table__.select())).all())
        events = list((await db.execute(UserEvent.__table__.select())).all())
        outbox = list((await db.execute(OutboxEvent.__table__.select())).all())
    assert len(moments) == 2
    assert len([row for row in ledgers if row.entry_type == "moment_explicit"]) == 2
    assert len(events) == 1
    assert events[0].source == "moment"
    extraction_outbox = [row for row in outbox if row.event_type == EXTRACTION_REQUESTED]
    assert len(extraction_outbox) == 1


async def test_image_only_stays_out_of_terrain_but_text_with_image_can_enter(
    client: AsyncClient,
):
    image_only_upload = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image"},
        files={"file": ("image-only.png", io.BytesIO(b"image-only"), "image/png")},
    )
    text_image_upload = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image"},
        files={"file": ("text-image.png", io.BytesIO(b"text-image"), "image/png")},
    )
    assert image_only_upload.status_code == text_image_upload.status_code == 201

    image_only = await client.post(
        "/api/v1/moments",
        json={
            "media_asset_ids": [image_only_upload.json()["asset_id"]],
            # An old or malicious client must not turn a visual-only record
            # into a text-derived terrain event by sending this flag alone.
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert image_only.status_code == 201, image_only.text
    assert image_only.json()["use_for_terrain"] is False
    assert image_only.json()["user_event_id"] is None

    text_image = await client.post(
        "/api/v1/moments",
        json={
            "text": "这张照片记录了我今天想慢一点走",
            "media_asset_ids": [text_image_upload.json()["asset_id"]],
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert text_image.status_code == 201, text_image.text
    assert text_image.json()["use_for_terrain"] is True
    assert text_image.json()["user_event_id"]

    async with get_db(read_only=True) as db:
        events = list((await db.execute(select(UserEvent))).scalars().all())
        outbox = list((await db.execute(select(OutboxEvent))).scalars().all())
    assert [event.content for event in events] == ["这张照片记录了我今天想慢一点走"]
    assert [row.event_type for row in outbox] == [EXTRACTION_REQUESTED]


async def test_terrain_withholds_one_off_or_short_lived_patterns(
    client: AsyncClient,
    test_settings: Settings,
):
    await _seed_terrain_claim(
        test_settings,
        day_offsets=[0, 2],
        sessions=["morning", "evening"],
    )
    response = await client.get("/api/v1/terrain")
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["withheld_count"] == 1
    assert payload["thresholds"] == {
        "min_evidence": 3,
        "min_span_days": 7,
        "min_contexts": 2,
    }


async def test_terrain_ignores_evidence_without_row_level_permission(
    client: AsyncClient,
    test_settings: Settings,
):
    # The permission lives on the source row, not on its ``source`` string, so a
    # companion turn the user never opted into stays out of the terrain even
    # though its evidence is otherwise mature.
    await _seed_terrain_claim(
        test_settings,
        day_offsets=[0, 4, 9],
        sessions=["chat-a", "chat-b", "chat-a"],
        source="chat",
        mode="confide",
        terrain_eligible=False,
        formed=True,
    )
    response = await client.get("/api/v1/terrain")
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["candidates"] == []
    assert response.json()["withheld_count"] == 1


async def test_companion_evidence_can_form_terrain_once_eligible(
    client: AsyncClient,
    test_settings: Settings,
):
    # Baseline 8.2.1: companion turns are eligible material.  Restricting terrain
    # to explicitly saved fragments would hide exactly the unconscious material
    # the product exists to surface.
    claim_id = await _seed_terrain_claim(
        test_settings,
        day_offsets=[0, 4, 9],
        sessions=["chat-a", "chat-b", "chat-a"],
        source="chat",
        mode="confide",
        terrain_eligible=True,
        formed=True,
    )
    items = (await client.get("/api/v1/terrain")).json()["items"]
    assert [item["claim_id"] for item in items] == [claim_id]


async def test_counted_evidence_is_a_lead_not_terrain(
    client: AsyncClient,
    test_settings: Settings,
):
    # Counting recurrences is not formation.  A claim that only accumulated
    # evidence may be offered as a lead, but the product must not assert it as
    # terrain, because nothing ever named what it shows.
    claim_id = await _seed_terrain_claim(
        test_settings,
        day_offsets=[0, 3, 8],
        sessions=["weekday", "weekend", "weekday"],
    )
    payload = (await client.get("/api/v1/terrain")).json()
    assert payload["items"] == []
    assert [row["claim_id"] for row in payload["candidates"]] == [claim_id]
    assert payload["candidates"][0]["evidence_count"] == 3
    assert payload["candidates"][0]["span_days"] == 8
    assert payload["candidates"][0]["context_count"] == 2


async def test_formed_terrain_is_explainable_and_user_can_defer_then_confirm(
    client: AsyncClient,
    test_settings: Settings,
):
    claim_id = await _seed_terrain_claim(
        test_settings,
        day_offsets=[0, 3, 8],
        sessions=["weekday", "weekend", "weekday"],
        formed=True,
        terrain_state="recurring",
    )
    response = await client.get("/api/v1/terrain")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["claim_id"] == claim_id
    assert item["terrain_type"] == "反复回到"
    assert item["state"] == "recurring"
    assert item["evidence_count"] == 3
    assert item["span_days"] == 8
    assert item["context_count"] == 2
    assert "confidence" not in item

    evidence = await client.get(f"/api/v1/memories/{claim_id}/evidence")
    assert evidence.status_code == 200
    assert len(evidence.json()) == 3
    assert {row["source"] for row in evidence.json()} == {"moment"}

    deferred = await client.post(f"/api/v1/memories/{claim_id}/defer")
    assert deferred.status_code == 200
    assert deferred.json()["user_status"] == "deferred"
    after_defer = (await client.get("/api/v1/terrain")).json()["items"][0]
    assert after_defer["maturity"] == "等你再看看"

    confirmed = await client.post(f"/api/v1/memories/{claim_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["user_status"] == "confirmed"


async def test_terrain_projection_is_read_only_and_reveal_is_explicit(
    client: AsyncClient,
    test_settings: Settings,
):
    claim_id = await _seed_terrain_claim(
        test_settings,
        day_offsets=[0, 3, 8],
        sessions=["weekday", "weekend", "weekday"],
        formed=True,
        terrain_state="growing",
    )

    async with get_db(read_only=True) as db:
        before = (
            await db.execute(select(MemoryClaim).where(MemoryClaim.claim_id == claim_id))
        ).scalar_one()
        before_history = list(before.terrain_history_json or [])
        before_revealed = before.terrain_first_revealed_at

    first = await client.get("/api/v1/terrain")
    second = await client.get("/api/v1/terrain")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["items"][0]["is_first_reveal"] is True

    async with get_db(read_only=True) as db:
        after_reads = (
            await db.execute(select(MemoryClaim).where(MemoryClaim.claim_id == claim_id))
        ).scalar_one()
        assert list(after_reads.terrain_history_json or []) == before_history
        assert after_reads.terrain_first_revealed_at == before_revealed

    revealed = await client.post(f"/api/v1/terrain/{claim_id}/reveal")
    assert revealed.status_code == 200
    assert revealed.json() == {
        "ok": True,
        "claim_id": claim_id,
        "revealed": True,
        "state": "growing",
    }
    after_reveal = (await client.get("/api/v1/terrain")).json()["items"][0]
    assert after_reveal["is_first_reveal"] is False
    assert after_reveal["first_revealed_at"]
    assert [row["kind"] for row in after_reveal["recent_changes"]] == ["reveal"]

    # Repeating the explicit action is idempotent and does not duplicate the
    # lifecycle history.
    repeated = await client.post(f"/api/v1/terrain/{claim_id}/reveal")
    assert repeated.status_code == 200
    async with get_db(read_only=True) as db:
        final = (
            await db.execute(select(MemoryClaim).where(MemoryClaim.claim_id == claim_id))
        ).scalar_one()
        assert len([row for row in (final.terrain_history_json or []) if row.get("kind") == "reveal"]) == 1
