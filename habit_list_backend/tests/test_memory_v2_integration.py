"""Transactional, API, retrieval, and deletion tests for Memory V2."""
from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import func, select

import tests  # noqa: F401
from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import (
    MemoryClaim,
    MemoryDeletionTombstone,
    MemoryEvidence,
    MemoryRetrievalTrace,
    MemoryRevision,
    OutboxEvent,
    UserEvent,
)
from app.memory_v2.retrieval import retrieve_memories
from app.memory_v2.service import (
    EXTRACTION_REQUESTED,
    enqueue_user_event,
    record_retrieval_trace,
)
from app.memory_v2.worker import process_pending_outbox


async def _remember(
    *,
    text: str,
    request_id: str,
    settings: Settings,
    mode: str = "confide",
    terrain_eligible: bool = False,
) -> str:
    async with get_db(read_only=False) as db:
        enqueued = await enqueue_user_event(
            db,
            user_id=settings.default_user_id,
            session_id="memory-v2-test-session",
            request_id=request_id,
            content=text,
            mode=mode,
            terrain_eligible=terrain_eligible,
            settings=settings,
        )
    assert enqueued is not None
    result = await process_pending_outbox(settings)
    assert result == {"claimed": 1, "processed": 1, "retried": 0, "dead": 0}
    return enqueued.event_id


async def test_outbox_reconciliation_keeps_sets_and_confirms_conflicts(
    client: AsyncClient,
    test_settings: Settings,
):
    await _remember(text="我喜欢咖啡。", request_id="pref-coffee", settings=test_settings)
    await _remember(text="我喜欢茶。", request_id="pref-tea", settings=test_settings)
    await _remember(text="我叫小岚。", request_id="name-first", settings=test_settings)
    await _remember(text="我叫小雨。", request_id="name-second", settings=test_settings)

    async with get_db(read_only=True) as db:
        preferences = list(
            (
                await db.execute(
                    select(MemoryClaim).where(MemoryClaim.category == "preference")
                )
            ).scalars().all()
        )
        names = list(
            (
                await db.execute(
                    select(MemoryClaim)
                    .where(MemoryClaim.predicate == "name")
                    .order_by(MemoryClaim.version.asc())
                )
            ).scalars().all()
        )

    assert {claim.object_value for claim in preferences} == {"咖啡", "茶"}
    assert {claim.user_status for claim in preferences} == {"confirmed"}
    assert len(names) == 2
    old_name, proposed_name = names
    assert old_name.user_status == "confirmed"
    assert proposed_name.user_status == "proposed"
    assert proposed_name.supersedes_claim_id == old_name.claim_id
    assert old_name.valid_to is None

    response = await client.post(f"/api/v1/memories/{proposed_name.claim_id}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["user_status"] == "confirmed"

    async with get_db(read_only=True) as db:
        refreshed = list(
            (
                await db.execute(
                    select(MemoryClaim)
                    .where(MemoryClaim.predicate == "name")
                    .order_by(MemoryClaim.version.asc())
                )
            ).scalars().all()
        )
    assert refreshed[0].user_status == "superseded"
    assert refreshed[0].valid_to == refreshed[1].valid_from
    assert refreshed[1].user_status == "confirmed"


async def test_sensitive_procedural_memory_is_proposed_and_not_retrieved(
    client: AsyncClient,
    test_settings: Settings,
):
    await _remember(
        text="我喜欢手冲咖啡。",
        request_id="normal-preference",
        settings=test_settings,
    )
    await _remember(
        text="以后请你不要主动提我的工资。",
        request_id="sensitive-procedural",
        settings=test_settings,
    )

    listed = await client.get("/api/v1/memories", params={"status": "all"})
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    sensitive = next(item for item in items if item["sensitivity"] == "sensitive")
    assert sensitive["claim_type"] == "procedural"
    assert sensitive["user_status"] == "proposed"
    assert sensitive["allow_proactive"] is False

    active_settings = test_settings.model_copy(
        update={"memory_v2_mode": "active", "memory_v2_min_retrieval_score": 0.0}
    )
    async with get_db(read_only=True) as db:
        batch = await retrieve_memories(
            db,
            user_id=test_settings.default_user_id,
            query="你还记得我喜欢什么吗？",
            settings=active_settings,
        )
    assert batch.used_in_response is True
    assert [item.claim_text for item in batch.selected] == ["喜欢手冲咖啡"]

    async with get_db(read_only=False) as db:
        trace_id = await record_retrieval_trace(
            db,
            user_id=test_settings.default_user_id,
            request_id="retrieval-trace-test",
            batch=batch,
            settings=active_settings,
        )
    async with get_db(read_only=True) as db:
        trace = (
            await db.execute(
                select(MemoryRetrievalTrace).where(
                    MemoryRetrievalTrace.trace_id == trace_id
                )
            )
        ).scalar_one()
        landed = (
            await db.execute(
                select(MemoryClaim).where(
                    MemoryClaim.claim_id == batch.selected[0].claim_id
                )
            )
        ).scalar_one()
    assert trace.used_in_response is True
    assert trace.selected_json[0]["claim_id"] == landed.claim_id
    assert landed.last_landed_at is not None
    assert landed.retrieval_count == 1


async def test_memory_api_explains_corrects_and_permanently_deletes(
    client: AsyncClient,
    test_settings: Settings,
):
    event_id = await _remember(
        text="我喜欢爵士乐。",
        request_id="delete-source",
        settings=test_settings,
    )
    listed = await client.get("/api/v1/memories", params={"q": "爵士"})
    claim_id = listed.json()["items"][0]["claim_id"]

    evidence = await client.get(f"/api/v1/memories/{claim_id}/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()[0]["excerpt_text"] == "我喜欢爵士乐"

    corrected = await client.patch(
        f"/api/v1/memories/{claim_id}",
        json={"claim_text": "喜欢在夜晚听爵士乐", "pinned": True},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["user_status"] == "corrected"
    assert corrected.json()["pinned"] is True

    deleted = await client.delete(f"/api/v1/memories/{claim_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["completed"] is True
    assert deleted.json()["deletion_id"]
    assert (await client.get(f"/api/v1/memories/{claim_id}")).status_code == 404

    async with get_db(read_only=True) as db:
        claim_count = await db.scalar(
            select(func.count()).select_from(MemoryClaim).where(
                MemoryClaim.claim_id == claim_id
            )
        )
        evidence_count = await db.scalar(
            select(func.count()).select_from(MemoryEvidence).where(
                MemoryEvidence.claim_id == claim_id
            )
        )
        revision_count = await db.scalar(
            select(func.count()).select_from(MemoryRevision).where(
                MemoryRevision.claim_id == claim_id
            )
        )
        tombstones = await db.scalar(
            select(func.count()).select_from(MemoryDeletionTombstone)
        )
        source = (
            await db.execute(select(UserEvent).where(UserEvent.event_id == event_id))
        ).scalar_one()
    assert (claim_count, evidence_count, revision_count) == (0, 0, 0)
    assert tombstones == 2
    # Deleting a derived memory does not delete the user's original chat event.
    assert source.status == "active"

    # A delayed/replayed outbox event for the same evidence must not resurrect
    # the memory after hard deletion.
    async with get_db(read_only=False) as db:
        db.add(
            OutboxEvent(
                user_id=test_settings.default_user_id,
                aggregate_type="user_event",
                aggregate_id=event_id,
                event_type=EXTRACTION_REQUESTED,
                payload_json={"event_id": event_id},
            )
        )
    result = await process_pending_outbox(test_settings)
    assert result["processed"] == 1
    async with get_db(read_only=True) as db:
        resurrected = await db.scalar(
            select(func.count()).select_from(MemoryClaim).where(
                MemoryClaim.user_id == test_settings.default_user_id
            )
        )
    assert resurrected == 0


async def test_rejected_memory_is_not_recreated_and_profile_shows_correction(
    client: AsyncClient,
    test_settings: Settings,
):
    await _remember(
        text="我喜欢蓝莓。",
        request_id="reject-blueberry-first",
        settings=test_settings,
    )
    listed = await client.get("/api/v1/memories", params={"q": "蓝莓"})
    assert listed.status_code == 200, listed.text
    claim_id = listed.json()["items"][0]["claim_id"]

    rejected = await client.post(f"/api/v1/memories/{claim_id}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["user_status"] == "rejected"

    profile = await client.get("/api/v1/me/profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["feedback"]["rejected_memory_count"] == 1

    # A later event with the same grounded interpretation must not create a
    # fresh Claim after the user has explicitly rejected it.
    await _remember(
        text="我喜欢蓝莓。",
        request_id="reject-blueberry-replay",
        settings=test_settings,
    )
    async with get_db(read_only=True) as db:
        claims = list(
            (
                await db.execute(
                    select(MemoryClaim).where(
                        MemoryClaim.user_id == test_settings.default_user_id,
                        MemoryClaim.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
        )
    assert len(claims) == 1
    assert claims[0].claim_id == claim_id
    assert claims[0].user_status == "rejected"
