"""Formation layer regressions: every case here is a contract violation.

These tests do not check that a formation can be produced.  They check that the
layer refuses to produce one when the evidence, the user's decisions, or the
model's output do not permit it — which is the only property that makes an
inference about someone safe to show them.
"""
from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import tests  # noqa: F401
from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import (
    MemoryClaim,
    MemoryEvidence,
    OutboxEvent,
    UserEvent,
)
from app.memory_v2.formation import (
    FORMATION_SCAN_REQUESTED,
    build_clusters,
    run_formation_scan,
)
from app.memory_v2.service import enqueue_formation_scan, permanently_delete_claim
from app.providers import dashscope

pytestmark = pytest.mark.anyio

_BASE = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


async def _seed_claim(
    settings: Settings,
    *,
    slot_key: str,
    day_offsets: list[int],
    sessions: list[str],
    terrain_eligible: bool = True,
    user_status: str = "proposed",
    sensitivity: str = "normal",
    source_type: str = "system_inferred",
    source: str = "moment",
    mode: str = "moment",
    roles: list[str] | None = None,
) -> str:
    assert len(day_offsets) == len(sessions)
    roles = roles or ["supports"] * len(day_offsets)
    async with get_db(read_only=False) as db:
        claim = MemoryClaim(
            user_id=settings.default_user_id,
            claim_type="semantic",
            category="habit",
            subject="self",
            predicate="returns_to",
            object_value=slot_key,
            claim_text=f"你似乎反复回到{slot_key}",
            slot_key=slot_key,
            content_hash=hashlib.sha256(slot_key.encode("utf-8")).hexdigest(),
            source_type=source_type,
            confidence=0.7,
            user_status=user_status,
            sensitivity=sensitivity,
            observed_at=_BASE.isoformat().replace("+00:00", "Z"),
            evidence_count=len(day_offsets),
            allow_proactive=False,
            created_by_policy_version="formation-test-v1",
        )
        db.add(claim)
        await db.flush()
        for index, (offset, session_id) in enumerate(
            zip(day_offsets, sessions, strict=True)
        ):
            text = f"{slot_key} 第 {index + 1} 次，我慢慢安静下来了"
            event = UserEvent(
                user_id=settings.default_user_id,
                session_id=session_id,
                request_id=f"{slot_key}-event-{index}",
                source=source,
                mode=mode,
                content=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                occurred_at=(_BASE + timedelta(days=offset))
                .isoformat()
                .replace("+00:00", "Z"),
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
                    evidence_role=roles[index],
                    excerpt_start=0,
                    excerpt_end=len(text),
                    excerpt_text=text,
                    source_weight=1.0,
                    extractor_version="formation-test-v1",
                )
            )
        await db.flush()
        return claim.claim_id


def _fake_model(monkeypatch, payloads: list[dict], calls: list[dict]):
    """Replace the provider with a scripted responder and record every call."""

    async def _chat_json(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        if not payloads:
            raise AssertionError("the model was called more times than scripted")
        return payloads.pop(0)

    monkeypatch.setattr(dashscope, "chat_json", _chat_json)


def _labels(refs: list[str], role: str = "supports") -> list[dict]:
    return [{"ref": ref, "role": role} for ref in refs]


async def _formation_claims(settings: Settings) -> list[MemoryClaim]:
    async with get_db(read_only=True) as db:
        return list(
            (
                await db.execute(
                    select(MemoryClaim).where(
                        MemoryClaim.user_id == settings.default_user_id,
                        MemoryClaim.source_type == "formation",
                    )
                )
            ).scalars().all()
        )


async def test_under_evidenced_cluster_never_reaches_the_model(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    # Two moments, five days, one context.  The thresholds live in stage 1 so the
    # model is not merely overruled afterwards — it is never consulted.
    await _seed_claim(
        test_settings,
        slot_key="habit:short-lived",
        day_offsets=[0, 5],
        sessions=["morning", "morning"],
    )
    calls: list[dict] = []
    _fake_model(monkeypatch, [], calls)

    async with get_db(read_only=True) as db:
        clusters, _considered = await build_clusters(
            db, user_id=test_settings.default_user_id, settings=test_settings
        )
    assert clusters == []

    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert result.hypotheses_requested == 0
    assert calls == []
    assert await _formation_claims(test_settings) == []


async def test_hypothesis_with_too_few_supports_is_discarded(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    await _seed_claim(
        test_settings,
        slot_key="habit:walking",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
    )
    calls: list[dict] = []
    _fake_model(
        monkeypatch,
        [
            {
                "terrain_kind": "recurring",
                "claim_text": "你似乎反复回到散步这件事上",
                "why_now": "最近这几次都提到了同一种安静",
                # All three refs are labelled, but only two support the claim.
                "evidence_roles": _labels(["E1", "E2"]) + _labels(["E3"], "contradicts"),
            }
        ],
        calls,
    )

    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert len(calls) == 1
    assert result.hypotheses_discarded == 1
    assert await _formation_claims(test_settings) == []


async def test_banned_language_is_retried_once_then_discarded(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    await _seed_claim(
        test_settings,
        slot_key="habit:closeness",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
    )
    banned = {
        "terrain_kind": "recurring",
        "claim_text": "你总是在回避亲密",
        "why_now": "这几次都出现了同一种退开",
        "evidence_roles": _labels(["E1", "E2", "E3"]),
    }
    calls: list[dict] = []
    _fake_model(monkeypatch, [dict(banned), dict(banned)], calls)

    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    # Exactly one retry: a model that reached for a fixed-personality verdict is
    # not reliably steerable back into the product's voice by asking twice.
    assert len(calls) == 2
    assert result.hypotheses_discarded == 1
    assert await _formation_claims(test_settings) == []


async def test_unknown_evidence_ref_voids_the_whole_hypothesis(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    await _seed_claim(
        test_settings,
        slot_key="habit:reading",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
    )
    calls: list[dict] = []
    _fake_model(
        monkeypatch,
        [
            {
                "terrain_kind": "growing",
                "claim_text": "你似乎正在长出一种慢下来的能力",
                "why_now": "这几次都停在同一个地方",
                # E9 was never shown to the model.  A fabricated citation voids
                # the hypothesis rather than being filtered out of it.
                "evidence_roles": _labels(["E1", "E2", "E3", "E9"]),
            }
        ],
        calls,
    )

    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert result.hypotheses_discarded == 1
    assert await _formation_claims(test_settings) == []


async def test_two_forces_without_a_contradiction_is_discarded(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    await _seed_claim(
        test_settings,
        slot_key="habit:tension",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
    )
    calls: list[dict] = []
    _fake_model(
        monkeypatch,
        [
            {
                "terrain_kind": "two_forces",
                "claim_text": "有两股力量似乎在你身上同时为真",
                "why_now": "这几次都停在同一个地方",
                "evidence_roles": _labels(["E1", "E2", "E3"]),
            }
        ],
        calls,
    )

    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert result.hypotheses_discarded == 1
    assert await _formation_claims(test_settings) == []


async def test_rejected_and_sensitive_claims_never_enter_a_cluster(
    app_no_scheduler,
    test_settings: Settings,
):
    # A rejection is a durable correction: the interpretation must not re-enter
    # through a broader hypothesis.  Sensitive material is out by policy.
    await _seed_claim(
        test_settings,
        slot_key="habit:rejected",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
        user_status="rejected",
    )
    await _seed_claim(
        test_settings,
        slot_key="habit:sensitive",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
        sensitivity="sensitive",
    )
    async with get_db(read_only=True) as db:
        clusters, considered = await build_clusters(
            db, user_id=test_settings.default_user_id, settings=test_settings
        )
    assert considered == 0
    assert clusters == []


async def test_companion_only_evidence_can_form_terrain(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    # The positive side of the baseline 8.2.1 decision: material the user never
    # opted into per-message is exactly what the layer exists to notice.
    await _seed_claim(
        test_settings,
        slot_key="habit:companion",
        day_offsets=[0, 4, 9],
        sessions=["chat-a", "chat-b", "chat-a"],
        source="chat",
        mode="confide",
    )
    calls: list[dict] = []
    _fake_model(
        monkeypatch,
        [
            {
                "terrain_kind": "recurring",
                "claim_text": "你似乎反复回到想把节奏放慢这件事",
                "why_now": "这几次分别在不同场景里出现",
                "evidence_roles": _labels(["E1", "E2", "E3"]),
            }
        ],
        calls,
    )

    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert len(result.created_claim_ids) == 1
    formed = await _formation_claims(test_settings)
    assert len(formed) == 1
    claim = formed[0]
    assert claim.terrain_state == "recurring"
    # A formation is an inference about someone: always a proposal, never
    # allowed to speak up before the user has accepted it.
    assert claim.user_status == "proposed"
    assert claim.allow_proactive is False
    assert claim.evidence_count == 3
    # The model never supplies text or offsets; evidence is inherited verbatim.
    async with get_db(read_only=True) as db:
        inherited = list(
            (
                await db.execute(
                    select(MemoryEvidence).where(MemoryEvidence.claim_id == claim.claim_id)
                )
            ).scalars().all()
        )
    assert len(inherited) == 3
    assert all(row.excerpt_text for row in inherited)


async def test_rescanning_the_same_cluster_does_not_create_a_second_claim(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    await _seed_claim(
        test_settings,
        slot_key="habit:idempotent",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
    )
    hypothesis = {
        "terrain_kind": "recurring",
        "claim_text": "你似乎反复回到同一种安静里",
        "why_now": "这几次分别在不同场景里出现",
        "evidence_roles": _labels(["E1", "E2", "E3"]),
    }
    calls: list[dict] = []
    _fake_model(monkeypatch, [dict(hypothesis), dict(hypothesis)], calls)

    first = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    second = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert len(first.created_claim_ids) == 1
    assert second.created_claim_ids == []
    assert len(await _formation_claims(test_settings)) == 1


async def test_permanently_deleted_formation_does_not_come_back(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    await _seed_claim(
        test_settings,
        slot_key="habit:deleted",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "morning"],
    )
    hypothesis = {
        "terrain_kind": "recurring",
        "claim_text": "你似乎反复回到同一个念头",
        "why_now": "这几次分别在不同场景里出现",
        "evidence_roles": _labels(["E1", "E2", "E3"]),
    }
    calls: list[dict] = []
    _fake_model(monkeypatch, [dict(hypothesis), dict(hypothesis)], calls)

    first = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    claim_id = first.created_claim_ids[0]

    async with get_db(read_only=False) as db:
        claim = (
            await db.execute(select(MemoryClaim).where(MemoryClaim.claim_id == claim_id))
        ).scalar_one()
        await permanently_delete_claim(db, claim=claim, request_id="delete-formation")

    # The source moments survive the deletion, so without a fingerprint tombstone
    # the very next scan would rebuild the same feature from the same evidence.
    again = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert again.created_claim_ids == []
    assert await _formation_claims(test_settings) == []


async def test_evidence_burst_produces_a_single_scan(
    app_no_scheduler,
    test_settings: Settings,
):
    async with get_db(read_only=False) as db:
        outbox_ids = [
            await enqueue_formation_scan(
                db, user_id=test_settings.default_user_id, settings=test_settings
            )
            for _ in range(5)
        ]
    assert len([value for value in outbox_ids if value]) == 1
    async with get_db(read_only=True) as db:
        scans = list(
            (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == FORMATION_SCAN_REQUESTED
                    )
                )
            ).scalars().all()
        )
    assert len(scans) == 1
    # The scan is deliberately future-dated: a forming feature must not arrive in
    # the same breath as the message that completed it.
    assert scans[0].available_at > datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def test_life_fragment_without_optin_is_not_terrain_eligible(
    client: AsyncClient,
):
    saved = await client.post(
        "/api/v1/moments",
        json={
            "text": "只想留一句给自己看",
            "use_for_terrain": False,
            "allow_response": False,
        },
    )
    opted_in = await client.post(
        "/api/v1/moments",
        json={
            "text": "今晚散步后，脑子终于安静了一点",
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert saved.status_code == opted_in.status_code == 201
    async with get_db(read_only=True) as db:
        events = list((await db.execute(select(UserEvent))).scalars().all())
    assert [event.terrain_eligible for event in events] == [True]


async def test_formation_pause_revokes_new_eligibility_only(
    client: AsyncClient,
    test_settings: Settings,
):
    paused = await client.patch("/api/v1/me/privacy", json={"formation_paused": True})
    assert paused.status_code == 200
    assert paused.json()["formation_paused"] is True

    created = await client.post(
        "/api/v1/moments",
        json={
            "text": "暂停期间的这一句不应该进入形成",
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert created.status_code == 201
    assert created.json()["use_for_terrain"] is False

    async with get_db(read_only=True) as db:
        events = list((await db.execute(select(UserEvent))).scalars().all())
    assert events == []

    resumed = await client.patch("/api/v1/me/privacy", json={"formation_paused": False})
    assert resumed.json()["formation_paused"] is False
    after = await client.post(
        "/api/v1/moments",
        json={
            "text": "恢复之后这一句可以进入形成",
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert after.json()["use_for_terrain"] is True


async def _upload_voice(
    client: AsyncClient, monkeypatch, *, text: str, confidence: float | None, name: str
) -> str:
    async def _fake_asr(_raw: bytes, _filename: str) -> dashscope.Transcription:
        return dashscope.Transcription(text=text, confidence=confidence)

    monkeypatch.setattr(dashscope, "asr_transcribe", _fake_asr)
    uploaded = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "true"},
        files={"file": (name, io.BytesIO(b"voice-" + name.encode()), "audio/webm")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["transcript"] == text
    assert uploaded.json()["transcript_confidence"] == confidence
    return uploaded.json()["asset_id"]


async def test_unverified_transcript_is_not_terrain_evidence(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch,
):
    # Baseline 8.3.  The fragment is still saved and still gets a reply; only its
    # use as material for an inference about the person is withheld.
    unknown = await _upload_voice(
        client, monkeypatch, text="我最近老是走同一条路", confidence=None, name="unknown.webm"
    )
    low = await _upload_voice(
        client, monkeypatch, text="我最近老是走同一条路", confidence=0.41, name="low.webm"
    )
    trusted = await _upload_voice(
        client, monkeypatch, text="我最近老是走同一条路", confidence=0.93, name="high.webm"
    )

    for asset_id in (unknown, low):
        withheld = await client.post(
            "/api/v1/moments",
            json={
                "media_asset_ids": [asset_id],
                "use_for_terrain": True,
                "allow_response": False,
            },
        )
        assert withheld.status_code == 201, withheld.text
        # The transcript is still the fragment's text; it just is not evidence.
        assert withheld.json()["text"] == "我最近老是走同一条路"
        assert withheld.json()["use_for_terrain"] is False
        assert withheld.json()["user_event_id"] is None
        # Nor can the permission be granted afterwards.
        regranted = await client.patch(
            f"/api/v1/moments/{withheld.json()['moment_id']}",
            json={"use_for_terrain": True},
        )
        assert regranted.status_code == 200, regranted.text
        assert regranted.json()["use_for_terrain"] is False

    accepted = await client.post(
        "/api/v1/moments",
        json={
            "media_asset_ids": [trusted],
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["use_for_terrain"] is True
    assert accepted.json()["user_event_id"]

    async with get_db(read_only=True) as db:
        events = list((await db.execute(select(UserEvent))).scalars().all())
    assert [event.terrain_eligible for event in events] == [True]


async def test_reviewed_transcript_becomes_the_users_own_words(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch,
):
    # The gate must not be a dead end for voice: once the user confirms or edits
    # the transcript it arrives as text, and text is the user's own words.
    asset_id = await _upload_voice(
        client, monkeypatch, text="我最近老是走同一条路", confidence=None, name="reviewed.webm"
    )
    reviewed = await client.post(
        "/api/v1/moments",
        json={
            "text": "我最近老是走同一条路回家",
            "media_asset_ids": [asset_id],
            "use_for_terrain": True,
            "allow_response": False,
        },
    )
    assert reviewed.status_code == 201, reviewed.text
    assert reviewed.json()["use_for_terrain"] is True
    assert reviewed.json()["user_event_id"]


async def _seed_crisis_event(
    settings: Settings, *, session_id: str, day_offset: int
) -> None:
    text = "我真的撑不下去了，想过要自杀"
    async with get_db(read_only=False) as db:
        db.add(
            UserEvent(
                user_id=settings.default_user_id,
                session_id=session_id,
                request_id=f"crisis-{session_id}-{day_offset}",
                source="chat",
                mode="confide",
                content=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                occurred_at=(_BASE + timedelta(days=day_offset))
                .isoformat()
                .replace("+00:00", "Z"),
                sensitivity="crisis",
                status="active",
                terrain_eligible=False,
            )
        )


async def test_crisis_window_isolates_the_hours_that_follow(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    # Baseline 8.3.  The crisis turn itself was never eligible.  What the layer
    # must also refuse is the material around it: what someone says in the hours
    # after a crisis is the crisis speaking, not a stable feature of who they are.
    await _seed_claim(
        test_settings,
        slot_key="habit:after-crisis",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "night"],
    )
    # The crisis lands in the same session as the third piece of evidence, which
    # was recorded at day 9 exactly.
    await _seed_crisis_event(test_settings, session_id="night", day_offset=9)

    async with get_db(read_only=True) as db:
        clusters, considered = await build_clusters(
            db, user_id=test_settings.default_user_id, settings=test_settings
        )
    # One claim was considered, but only two of its three supports survived, so
    # the cluster no longer meets the evidence bar and never reaches a model.
    assert considered == 1
    assert clusters == []

    calls: list[dict] = []
    _fake_model(monkeypatch, [], calls)
    result = await run_formation_scan(
        user_id=test_settings.default_user_id, settings=test_settings
    )
    assert result.hypotheses_requested == 0
    assert calls == []
    assert await _formation_claims(test_settings) == []


async def test_evidence_before_a_crisis_stays_usable(
    app_no_scheduler,
    test_settings: Settings,
    monkeypatch,
):
    # The window opens at the crisis, not around it.  Turns recorded before
    # anything happened were not said under it and must not be erased.
    await _seed_claim(
        test_settings,
        slot_key="habit:before-crisis",
        day_offsets=[0, 4, 9],
        sessions=["morning", "evening", "night"],
    )
    await _seed_crisis_event(test_settings, session_id="night", day_offset=11)

    async with get_db(read_only=True) as db:
        clusters, considered = await build_clusters(
            db, user_id=test_settings.default_user_id, settings=test_settings
        )
    assert considered == 1
    assert len(clusters) == 1
    assert len(clusters[0].supporting) == 3

