"""Regression coverage for the P0 safety/deletion gates and P1 revisit loop."""

from __future__ import annotations

import hashlib

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import (
    MemoryClaim,
    MemoryEmbedding,
    MemoryEvidence,
    OutboxEvent,
    UserEvent,
)
from app.db.models import MomentInteraction, User
from app.memory_v2.worker import process_pending_outbox
from app.moments import service as moment_service
from app.moments.service import (
    MOMENT_ECHO_REVISIT_REQUESTED,
    MomentAgentDecision,
    is_crisis_text,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "text",
    [
        "我不想继续活下去",
        "我在想轻生",
        "我想伤害自己",
        "我想结束自己",
        "I want to kill myself",
        "self-harm feels unavoidable",
    ],
)
def test_crisis_classifier_covers_common_variants(text: str):
    assert is_crisis_text(text)


async def _set_mode(client: AsyncClient, mode: str) -> None:
    response = await client.patch(
        "/api/v1/me/profile", json={"settings": {"life_reply_mode": mode}}
    )
    assert response.status_code == 200, response.text


async def test_crisis_bypasses_silent_and_never_enters_terrain(
    client: AsyncClient, test_settings: Settings
):
    await _set_mode(client, "silent")
    response = await client.post(
        "/api/v1/moments",
        json={
            "text": "我不想活了",
            "use_for_terrain": True,
            "allow_proactive": True,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["response_pending"] is True
    assert payload["user_event_id"] is None
    result = await process_pending_outbox(test_settings)
    assert result["processed"] == 1

    async with get_db(read_only=True) as db:
        interactions = list((await db.execute(select(MomentInteraction))).scalars().all())
        assert list((await db.execute(select(UserEvent))).scalars().all()) == []
        assert list((await db.execute(select(MemoryClaim))).scalars().all()) == []
    assert len(interactions) == 1
    assert interactions[0].metadata_json["safety_response"] is True
    assert interactions[0].kind == "comment"


async def test_crisis_anchor_does_not_schedule_a_revisit_placeholder(
    client: AsyncClient,
):
    await _set_mode(client, "silent")
    source = await client.post(
        "/api/v1/moments",
        json={"text": "去年春天我在河边看见一只白鹭", "allow_proactive": True},
    )
    crisis = await client.post(
        "/api/v1/moments", json={"text": "我不想继续活下去"}
    )
    assert source.status_code == crisis.status_code == 201
    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.status_code == 200
    assert hint.json() == {"interaction": None, "why_now": "", "pending": False, "visit_id": None}


async def test_save_only_has_no_initial_event_but_user_reply_still_works(
    client: AsyncClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    await _set_mode(client, "always")
    created = await client.post(
        "/api/v1/moments", json={"text": "只想把今天的云保存下来", "save_only": True}
    )
    assert created.status_code == 201, created.text
    assert created.json()["response_pending"] is False
    assert created.json()["save_only"] is True

    async def fake_decision(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="paused",
            kind="comment",
            comment="我在这片里听见了你补充的这一句。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", fake_decision)
    reply = await client.post(
        f"/api/v1/moments/{created.json()['moment_id']}/interactions",
        json={"content": "后来风也停了"},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["response_pending"] is True
    assert (await process_pending_outbox(test_settings))["processed"] == 1


async def test_crisis_user_reply_bypasses_save_only_permission(
    client: AsyncClient, test_settings: Settings
):
    await _set_mode(client, "silent")
    created = await client.post(
        "/api/v1/moments", json={"text": "只保存这一片，不需要回应", "save_only": True}
    )
    assert created.status_code == 201, created.text
    reply = await client.post(
        f"/api/v1/moments/{created.json()['moment_id']}/interactions",
        json={"content": "我想伤害自己"},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["response_pending"] is True
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    thread = await client.get(f"/api/v1/moments/{created.json()['moment_id']}/interactions")
    safety_rows = [
        item for item in thread.json()["items"] if item["actor"] == "assistant"
    ]
    assert len(safety_rows) == 1
    async with get_db(read_only=True) as db:
        interaction = (
            await db.execute(select(MomentInteraction).where(MomentInteraction.actor == "assistant"))
        ).scalar_one()
    assert interaction.metadata_json["safety_response"] is True


async def test_save_only_permission_is_enforced_server_side(
    client: AsyncClient, test_settings: Settings
):
    await _set_mode(client, "always")
    saved = await client.post(
        "/api/v1/moments",
        json={
            "text": "旧客户端把三个开关都打开了，但我只想保存",
            "save_only": True,
            "allow_response": True,
            "use_for_terrain": True,
            "allow_proactive": True,
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["save_only"] is True
    assert saved.json()["allow_response"] is False
    assert saved.json()["use_for_terrain"] is False
    assert saved.json()["allow_proactive"] is False
    assert saved.json()["user_event_id"] is None
    assert saved.json()["response_pending"] is False

    await _set_mode(client, "silent")
    normal = await client.post(
        "/api/v1/moments",
        json={
            "text": "先允许形成，再改成只保存",
            "use_for_terrain": True,
            "allow_proactive": True,
        },
    )
    assert normal.status_code == 201, normal.text
    patched = await client.patch(
        f"/api/v1/moments/{normal.json()['moment_id']}",
        json={"save_only": True},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["save_only"] is True
    assert patched.json()["use_for_terrain"] is False
    assert patched.json()["allow_proactive"] is False
    async with get_db(read_only=True) as db:
        events = list((await db.execute(select(UserEvent))).scalars().all())
    assert events
    assert all(event.status == "deleted" for event in events)


async def test_memory_v2_off_still_drains_moment_responses(
    client: AsyncClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    await _set_mode(client, "always")

    async def fake_decision(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="seen",
            kind="reaction",
            comment="",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", fake_decision)
    created = await client.post("/api/v1/moments", json={"text": "off 模式仍要及时收下"})
    assert created.status_code == 201
    off = test_settings.model_copy(update={"memory_v2_mode": "off"})
    result = await process_pending_outbox(off)
    assert result["processed"] == 1
    async with get_db(read_only=True) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == moment_service.MOMENT_RESPONSE_REQUESTED
                )
            )
        ).scalar_one()
    assert event.status == "processed"


async def test_memory_v2_off_cancels_old_memory_tasks_without_touching_moment_queue(
    client: AsyncClient, test_settings: Settings
):
    """Disabling Memory V2 must not strand extraction work in Pending."""

    await _set_mode(client, "silent")
    created = await client.post(
        "/api/v1/moments",
        json={"text": "先留下这条可形成地形的线索", "use_for_terrain": True},
    )
    assert created.status_code == 201, created.text
    assert created.json()["user_event_id"]

    off = test_settings.model_copy(update={"memory_v2_mode": "off"})
    result = await process_pending_outbox(off)
    assert result["claimed"] == 0

    async with get_db(read_only=True) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "memory.extraction.requested"
                )
            )
        ).scalar_one()
    assert event.status == "cancelled"
    assert event.last_error == "memory_v2_disabled"


async def test_cancelled_worker_task_cannot_be_marked_processed(
    client: AsyncClient, test_settings: Settings
):
    await _set_mode(client, "always")
    created = await client.post("/api/v1/moments", json={"text": "删除竞态测试"})
    assert created.status_code == 201
    async with get_db(read_only=False) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == moment_service.MOMENT_RESPONSE_REQUESTED
                )
            )
        ).scalar_one()
        event.status = "cancelled"
        event.locked_at = None
        outbox_id = event.outbox_id

    from app.memory_v2 import worker

    await worker._mark_processed(outbox_id)
    await worker._mark_failed(outbox_id, RuntimeError("late failure"), test_settings)
    async with get_db(read_only=True) as db:
        event = (
            await db.execute(select(OutboxEvent).where(OutboxEvent.outbox_id == outbox_id))
        ).scalar_one()
    assert event.status == "cancelled"


def test_scheduler_keeps_moment_worker_when_memory_v2_is_off(test_settings: Settings):
    from app.memory import system2

    system2._scheduler = None
    settings = test_settings.model_copy(update={"memory_v2_mode": "off"})
    scheduler = system2.get_scheduler(settings)
    assert scheduler.get_job("memory_v2_outbox") is not None
    assert scheduler.get_job("memo_stale_scan") is not None


async def test_not_like_me_changes_future_response_policy(
    client: AsyncClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    await _set_mode(client, "always")

    async def fake_comment(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="paused",
            kind="comment",
            comment="这里有一个具体的瞬间。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", fake_comment)
    first = await client.post("/api/v1/moments", json={"text": "窗台上的薄荷今天发芽了"})
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    first_id = first.json()["moment_id"]
    listing = (await client.get("/api/v1/moments")).json()["items"]
    interaction_id = next(
        item["latest_agent_interaction"]["interaction_id"]
        for item in listing
        if item["moment_id"] == first_id
    )
    feedback = await client.post(
        f"/api/v1/moments/{first_id}/interactions/{interaction_id}/feedback",
        json={"feedback": "not_like_me"},
    )
    assert feedback.status_code == 200
    assert any(item.startswith("theme:") for item in feedback.json()["suppressions_added"])

    second = await client.post("/api/v1/moments", json={"text": "薄荷又长高了一点"})
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    item = next(
        row for row in (await client.get("/api/v1/moments")).json()["items"]
        if row["moment_id"] == second.json()["moment_id"]
    )
    assert item["latest_agent_interaction"] is None
    async with get_db(read_only=True) as db:
        user = (await db.execute(select(User))).scalar_one()
    assert "comment" in (user.settings_json or {}).get("moment_feedback_rules", {}).get(
        "kinds", []
    )


async def test_not_like_me_also_changes_future_revisit_echoes(
    client: AsyncClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    await _set_mode(client, "silent")
    first_source = await client.post(
        "/api/v1/moments",
        json={"text": "春天我给窗台换了一个更大的花盆", "allow_proactive": True},
    )
    first_anchor = await client.post(
        "/api/v1/moments", json={"text": "今天新叶终于越过盆沿", "allow_response": True}
    )
    assert first_source.status_code == first_anchor.status_code == 201

    async def first_echo(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="echo",
            kind="echo",
            comment="今天的叶子把换盆那天接了回来。",
            source_moment_ids=[first_source.json()["moment_id"]],
            why_now="新叶越过盆沿，和换盆时的期待接上了。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", first_echo)
    first_hint = await client.get("/api/v1/moments/echo/latest")
    assert first_hint.json()["pending"] is True
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    delivered = await client.get("/api/v1/moments/echo/latest")
    interaction_id = delivered.json()["interaction"]["interaction_id"]
    feedback = await client.post(
        f"/api/v1/moments/{first_anchor.json()['moment_id']}/interactions/{interaction_id}/feedback",
        json={"feedback": "not_like_me"},
    )
    assert feedback.status_code == 200, feedback.text
    await client.post(f"/api/v1/moments/echo/{interaction_id}/dismiss")

    async with get_db(read_only=False) as db:
        user = (await db.execute(select(User))).scalar_one()
        settings = dict(user.settings_json or {})
        visit_state = dict(settings.get("echo_visit_state") or {})
        visit_state["last_scheduled_at"] = "2000-01-01T00:00:00Z"
        settings["echo_visit_state"] = visit_state
        user.settings_json = settings

    second_source = await client.post(
        "/api/v1/moments",
        json={"text": "今天在书架上发现一张旧车票", "allow_proactive": True},
    )
    second_anchor = await client.post(
        "/api/v1/moments", json={"text": "回家时又看见那本旧书", "allow_response": True}
    )
    assert second_source.status_code == second_anchor.status_code == 201

    async def second_echo(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="echo",
            kind="echo",
            comment="这次也许可以连到那张车票。",
            source_moment_ids=[second_source.json()["moment_id"]],
            why_now="旧书和车票都把你带回了同一段路。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", second_echo)
    second_hint = await client.get("/api/v1/moments/echo/latest")
    assert second_hint.json()["pending"] is True
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    listing = (await client.get("/api/v1/moments")).json()["items"]
    second_item = next(
        item for item in listing if item["moment_id"] == second_anchor.json()["moment_id"]
    )
    assert second_item["latest_agent_interaction"] is None


async def test_source_deleted_during_generation_cannot_leave_an_echo(
    client: AsyncClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    await _set_mode(client, "silent")
    source = await client.post(
        "/api/v1/moments",
        json={"text": "去年夏天我在河边捡到一块蓝色玻璃", "allow_proactive": True},
    )
    await _set_mode(client, "always")
    current = await client.post(
        "/api/v1/moments", json={"text": "今天又在路边看见蓝色的光", "allow_response": True}
    )
    assert source.status_code == current.status_code == 201

    async def delete_then_echo(**_kwargs):
        deleted = await client.delete(f"/api/v1/moments/{source.json()['moment_id']}")
        assert deleted.status_code == 200
        return MomentAgentDecision(
            should_respond=True,
            reaction="echo",
            kind="echo",
            comment="这和去年那块玻璃很像。",
            source_moment_ids=[source.json()["moment_id"]],
            why_now="今天的蓝色光线让我想起那次捡到玻璃。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", delete_then_echo)
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    listing = (await client.get("/api/v1/moments")).json()["items"]
    current_item = next(
        item for item in listing if item["moment_id"] == current.json()["moment_id"]
    )
    assert current_item["latest_agent_interaction"] is None


async def test_delete_invalidates_derived_claim_and_source_queue(
    client: AsyncClient, test_settings: Settings
):
    await _set_mode(client, "silent")
    created = await client.post(
        "/api/v1/moments",
        json={"text": "我喜欢沿着河边散步", "use_for_terrain": True},
    )
    moment_id = created.json()["moment_id"]
    event_id = created.json()["user_event_id"]
    async with get_db(read_only=False) as db:
        event = (
            await db.execute(select(UserEvent).where(UserEvent.event_id == event_id))
        ).scalar_one()
        claim = MemoryClaim(
            user_id=test_settings.default_user_id,
            category="preference",
            subject="self",
            predicate="likes",
            object_value="沿着河边散步",
            claim_text="喜欢沿着河边散步",
            slot_key="delete-test-slot",
            content_hash=hashlib.sha256(b"delete-test").hexdigest(),
            source_type="user_explicit",
            confidence=0.9,
            user_status="confirmed",
            sensitivity="normal",
            evidence_count=1,
            created_by_policy_version="test",
        )
        db.add(claim)
        await db.flush()
        db.add(
            MemoryEvidence(
                claim_id=claim.claim_id,
                event_id=event.event_id,
                evidence_role="supports",
                excerpt_start=0,
                excerpt_end=len(event.content),
                excerpt_text=event.content,
                source_weight=1.0,
                extractor_version="test",
            )
        )
        db.add(
            MemoryEmbedding(
                claim_id=claim.claim_id,
                user_id=test_settings.default_user_id,
                provider="test",
                model="test",
                dimension=3,
                vector_json=[0.1, 0.2, 0.3],
                content_hash="embedding-delete-test",
                status="active",
            )
        )
        db.add(
            OutboxEvent(
                user_id=test_settings.default_user_id,
                aggregate_type="memory_claim",
                aggregate_id=claim.claim_id,
                event_type="memory.embedding.requested",
                payload_json={"claim_id": claim.claim_id},
            )
        )
        claim_id = claim.claim_id
    deleted = await client.delete(f"/api/v1/moments/{moment_id}")
    assert deleted.status_code == 200
    async with get_db(read_only=True) as db:
        event = (await db.execute(select(UserEvent))).scalar_one()
        claim = (await db.execute(select(MemoryClaim).where(MemoryClaim.claim_id == claim_id))).scalar_one()
        assert (await db.execute(select(MemoryEvidence).where(MemoryEvidence.claim_id == claim_id))).scalars().all() == []
        assert (await db.execute(select(MemoryEmbedding).where(MemoryEmbedding.claim_id == claim_id))).scalars().all() == []
        outbox = list((await db.execute(select(OutboxEvent))).scalars().all())
    assert event.status == "deleted"
    assert claim.deleted_at is not None
    assert claim.allow_proactive is False
    assert all(row.status not in {"pending", "processing"} for row in outbox)


async def test_life_page_visit_schedules_and_delivers_fresh_revisit_echo(
    client: AsyncClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    await _set_mode(client, "silent")
    source = await client.post(
        "/api/v1/moments",
        json={"text": "去年春天我给窗台换了一个更大的花盆", "allow_proactive": True},
    )
    current = await client.post(
        "/api/v1/moments",
        json={"text": "今天新叶终于把花盆边缘遮住了", "allow_response": True},
    )
    assert source.status_code == current.status_code == 201

    async def fake_echo(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="echo",
            kind="echo",
            comment="今天的叶子把去年的换盆接回来了。",
            source_moment_ids=[source.json()["moment_id"]],
            why_now="现在叶子越过盆沿，变化和那次换盆连在了一起。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", fake_echo)
    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.status_code == 200
    assert hint.json()["pending"] is True
    async with get_db(read_only=True) as db:
        pending = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_ECHO_REVISIT_REQUESTED
                )
            )
        ).scalar_one()
    assert "text" not in pending.payload_json
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    delivered = await client.get("/api/v1/moments/echo/latest")
    assert delivered.json()["interaction"]["kind"] == "echo"
    assert delivered.json()["interaction"]["user_feedback"] is None
    async with get_db(read_only=True) as db:
        interaction = (
            await db.execute(
                select(MomentInteraction).where(MomentInteraction.kind == "echo")
            )
        ).scalar_one()
    assert interaction.metadata_json["trigger_type"] == "revisit"
    assert (await client.get("/api/v1/moments/echo/latest")).json()["interaction"] is None
