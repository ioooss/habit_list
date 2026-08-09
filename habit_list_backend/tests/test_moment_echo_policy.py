"""Strategy gate, echo V1, feedback, and deletion closed loop for fragments."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import OutboxEvent
from app.db.models import Episodic, MomentInteraction, User
from app.memory_v2.worker import process_pending_outbox
from app.moments import policy
from app.moments import service as moment_service
from app.moments.service import MOMENT_RESPONSE_REQUESTED, MomentAgentDecision

pytestmark = pytest.mark.anyio


async def _set_reply_mode(client: AsyncClient, mode: str) -> None:
    response = await client.patch(
        "/api/v1/me/profile",
        json={"settings": {"life_reply_mode": mode}},
    )
    assert response.status_code == 200, response.text


async def _create_moment(
    client: AsyncClient,
    text: str,
    *,
    allow_proactive: bool = False,
    use_for_terrain: bool = False,
) -> dict:
    response = await client.post(
        "/api/v1/moments",
        json={
            "text": text,
            "allow_proactive": allow_proactive,
            "use_for_terrain": use_for_terrain,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _echo_decision(source_id: str, *, why_now: str = "它和眼前这一幕确实连在一起。"):
    async def _fake(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="echo",
            kind="echo",
            comment="这条旧记录和新的一刻接上了。",
            source_moment_ids=[source_id],
            why_now=why_now,
        )

    return _fake


def _comment_decision():
    async def _fake(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="paused",
            kind="comment",
            comment="这里有一个很具体的瞬间。",
        )

    return _fake


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------
async def test_gate_blocks_density_and_duplicates(client: AsyncClient, test_settings: Settings, monkeypatch):
    monkeypatch.setattr(moment_service, "generate_agent_decision", _comment_decision())

    await _set_reply_mode(client, "always")
    await _create_moment(client, "傍晚在河边看到有人放纸船")
    assert await process_pending_outbox(test_settings) == {
        "claimed": 1,
        "processed": 1,
        "retried": 0,
        "dead": 0,
    }

    # Second fragment within the density window stays quiet in occasional mode.
    await _set_reply_mode(client, "occasional")
    second = await _create_moment(client, "晚饭煮糊了一锅粥")
    result = await process_pending_outbox(test_settings)
    assert result["processed"] == 1
    listing = await client.get("/api/v1/moments")
    entry = next(
        item for item in listing.json()["items"] if item["moment_id"] == second["moment_id"]
    )
    assert entry["latest_agent_interaction"] is None
    assert entry["response_pending"] is False

    # Near-duplicate content stays quiet even without a density hit.
    await _set_reply_mode(client, "silent")
    await _create_moment(client, "窗台上的薄荷今天长出一片新叶")
    await _set_reply_mode(client, "occasional")
    duplicate = await _create_moment(client, "窗台上的薄荷今天长出一片新叶")
    await process_pending_outbox(test_settings)
    listing = await client.get("/api/v1/moments")
    entry = next(
        item for item in listing.json()["items"] if item["moment_id"] == duplicate["moment_id"]
    )
    assert entry["latest_agent_interaction"] is None


def test_gate_helpers_are_explainable():
    settings = {"life_reply_throttle_level": 1}
    assert policy.occasional_window(settings) == 6
    assert policy.occasional_window({"life_reply_throttle_level": 99}) == 3 + 2 * 3
    merged = policy.append_suppression(
        {}, entry_type="source", value="m-1", created_at="2026-08-03T00:00:00Z"
    )
    merged = policy.append_suppression(
        merged, entry_type="source", value="m-1", created_at="2026-08-03T01:00:00Z"
    )
    assert policy.suppressed_source_ids(merged) == {"m-1"}
    merged = policy.append_suppression(
        merged, entry_type="theme", value="薄荷", created_at="2026-08-03T01:00:00Z"
    )
    assert policy.text_hits_suppression("窗台上的薄荷长出新叶", ["薄荷"]) == "薄荷"
    assert policy.text_hits_suppression("今天的晚霞", ["薄荷"]) is None
    assert policy.extract_theme_keywords("上个月第一次给窗台的薄荷换了盆") != []


# ---------------------------------------------------------------------------
# Echo budget, cooldown, and idempotency
# ---------------------------------------------------------------------------
async def test_echo_budget_and_source_cooldown(test_settings: Settings):
    from app.db.models import _utcnow_iso

    user_id = test_settings.default_user_id
    async with get_db(read_only=False) as db:
        for episodic_id in ("moment-anchor", "source-a"):
            db.add(
                Episodic(
                    episodic_id=episodic_id,
                    user_id=user_id,
                    source="moment_explicit",
                    kind="life_fragment",
                    summary_1line="test",
                    raw_user_text="test",
                )
            )
        db.add(
            MomentInteraction(
                moment_id="moment-anchor",
                user_id=user_id,
                actor="assistant",
                kind="echo",
                content="回声示例",
                reaction="echo",
                metadata_json={
                    "trigger_type": "initial",
                    "source_moment_ids": ["source-a"],
                },
                created_at=_utcnow_iso(),
            )
        )
    async with get_db(read_only=True) as db:
        assert await policy.echo_budget_available(db, user_id=user_id) is False
        used = await policy.recently_used_source_ids(db, user_id=user_id)
    assert used == {"source-a"}


async def test_duplicate_outbox_consumption_creates_one_response(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    monkeypatch.setattr(moment_service, "generate_agent_decision", _comment_decision())
    await _set_reply_mode(client, "always")
    created = await _create_moment(client, "深夜便利店的灯亮着")
    assert await process_pending_outbox(test_settings) == {
        "claimed": 1,
        "processed": 1,
        "retried": 0,
        "dead": 0,
    }
    async with get_db(read_only=False) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED
                )
            )
        ).scalar_one()
        event.status = "pending"
        event.locked_at = None
        event.available_at = "2000-01-01T00:00:00Z"
    await process_pending_outbox(test_settings)
    async with get_db(read_only=True) as db:
        interactions = list(
            (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.moment_id == created["moment_id"],
                        MomentInteraction.actor == "assistant",
                    )
                )
            ).scalars().all()
        )
    assert len(interactions) == 1


async def test_dead_outbox_surfaces_failed_state(
    client: AsyncClient, test_settings: Settings
):
    await _set_reply_mode(client, "always")
    created = await _create_moment(client, "雨下了一整天")
    async with get_db(read_only=False) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED
                )
            )
        ).scalar_one()
        event.status = "dead"
        event.last_error = "TimeoutError"
    listing = await client.get("/api/v1/moments")
    entry = next(
        item for item in listing.json()["items"] if item["moment_id"] == created["moment_id"]
    )
    assert entry["response_failed"] is True
    assert entry["response_pending"] is False


# ---------------------------------------------------------------------------
# Feedback closed loop
# ---------------------------------------------------------------------------
async def _create_echo_pair(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch,
    *,
    source_text: str = "上个月第一次给窗台的薄荷换了盆",
    current_text: str = "薄荷今天终于冒出了一片很小的新叶",
) -> tuple[str, str, str]:
    await _set_reply_mode(client, "silent")
    source = await _create_moment(client, source_text, allow_proactive=True)
    await _set_reply_mode(client, "always")
    current = await _create_moment(client, current_text)
    monkeypatch.setattr(
        moment_service, "generate_agent_decision", _echo_decision(source["moment_id"])
    )
    await process_pending_outbox(test_settings)
    listing = await client.get("/api/v1/moments")
    entry = next(
        item for item in listing.json()["items"] if item["moment_id"] == current["moment_id"]
    )
    assert entry["latest_agent_interaction"]["kind"] == "echo"
    return source["moment_id"], current["moment_id"], entry["latest_agent_interaction"][
        "interaction_id"
    ]


async def test_stop_source_feedback_blocks_future_echoes(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    source_id, current_id, interaction_id = await _create_echo_pair(
        client, test_settings, monkeypatch
    )
    response = await client.post(
        f"/api/v1/moments/{current_id}/interactions/{interaction_id}/feedback",
        json={"feedback": "stop_source"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["suppressions_added"] == [f"source:{source_id}"]

    await _create_moment(client, "又看了一眼窗台")
    monkeypatch.setattr(
        moment_service, "generate_agent_decision", _echo_decision(source_id)
    )
    await process_pending_outbox(test_settings)
    listing = await client.get("/api/v1/moments")
    latest = next(
        item
        for item in listing.json()["items"]
        if item["text"] == "又看了一眼窗台"
    )
    reply = latest["latest_agent_interaction"]
    assert reply is None or reply["kind"] != "echo"


async def test_stop_category_feedback_suppresses_theme(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    source_id, current_id, interaction_id = await _create_echo_pair(
        client, test_settings, monkeypatch
    )
    response = await client.post(
        f"/api/v1/moments/{current_id}/interactions/{interaction_id}/feedback",
        json={"feedback": "stop_category", "keyword": "薄荷"},
    )
    assert response.status_code == 200, response.text
    assert any(item.startswith("theme:") for item in response.json()["suppressions_added"])

    # Existing echo hint must disappear once its source matches the theme.
    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.status_code == 200
    assert hint.json()["interaction"] is None


async def test_less_responses_feedback_raises_throttle(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    source_id, current_id, interaction_id = await _create_echo_pair(
        client, test_settings, monkeypatch
    )
    for expected_level in (1, 2, 2):
        response = await client.post(
            f"/api/v1/moments/{current_id}/interactions/{interaction_id}/feedback",
            json={"feedback": "less_responses"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["throttle_level"] == expected_level
    async with get_db(read_only=True) as db:
        user = (
            await db.execute(
                select(User).where(User.user_id == test_settings.default_user_id)
            )
        ).scalar_one()
    assert policy.occasional_window(user.settings_json) == 9


async def test_feedback_only_targets_assistant_replies(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    await _set_reply_mode(client, "silent")
    created = await _create_moment(client, "把阳台收拾了一遍")
    reply = await client.post(
        f"/api/v1/moments/{created['moment_id']}/interactions",
        json={"content": "顺手浇了花"},
    )
    user_interaction_id = reply.json()["interaction"]["interaction_id"]
    response = await client.post(
        f"/api/v1/moments/{created['moment_id']}/interactions/{user_interaction_id}/feedback",
        json={"feedback": "stop_source"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Echo hint, dismiss, and permission revocation
# ---------------------------------------------------------------------------
async def test_echo_hint_dismiss_and_revocation(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    source_id, _current_id, _interaction_id = await _create_echo_pair(
        client, test_settings, monkeypatch
    )
    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.status_code == 200
    payload = hint.json()
    assert payload["interaction"] is not None
    assert payload["why_now"]
    echo_interaction_id = payload["interaction"]["interaction_id"]

    dismissed = await client.post(f"/api/v1/moments/echo/{echo_interaction_id}/dismiss")
    assert dismissed.status_code == 200
    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.json()["interaction"] is None

    # Revoking proactive permission hides the echo source everywhere.
    patch = await client.patch(f"/api/v1/moments/{source_id}", json={"allow_proactive": False})
    assert patch.status_code == 200
    assert patch.json()["allow_proactive"] is False
    listing = await client.get("/api/v1/moments")
    for item in listing.json()["items"]:
        reply = item["latest_agent_interaction"]
        if reply is not None:
            assert reply["source_moments"] == []


async def test_revoking_echo_source_invalidates_existing_derived_reply(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    source_id, current_id, _interaction_id = await _create_echo_pair(
        client, test_settings, monkeypatch
    )
    patch = await client.patch(
        f"/api/v1/moments/{source_id}", json={"allow_proactive": False}
    )
    assert patch.status_code == 200
    listing = (await client.get("/api/v1/moments")).json()["items"]
    current = next(item for item in listing if item["moment_id"] == current_id)
    assert current["latest_agent_interaction"] is None
    thread = await client.get(f"/api/v1/moments/{current_id}/interactions")
    assert all(item["kind"] != "echo" for item in thread.json()["items"])


async def test_patch_moment_terrain_revocation_cancels_extraction(
    client: AsyncClient, test_settings: Settings
):
    await _set_reply_mode(client, "silent")
    created = await _create_moment(client, "把旧相册翻了一遍", use_for_terrain=True)
    assert created["user_event_id"] is not None
    patch = await client.patch(
        f"/api/v1/moments/{created['moment_id']}", json={"use_for_terrain": False}
    )
    assert patch.status_code == 200
    assert patch.json()["use_for_terrain"] is False
    async with get_db(read_only=True) as db:
        pending = list(
            (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "memory.extraction.requested",
                        OutboxEvent.status.in_(["pending", "processing"]),
                    )
                )
            ).scalars().all()
        )
    assert pending == []


# ---------------------------------------------------------------------------
# Deletion closed loop
# ---------------------------------------------------------------------------
async def test_delete_moment_cancels_outbox_and_thread(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    decisions_called = 0

    async def _fake(**_kwargs):
        nonlocal decisions_called
        decisions_called += 1
        return MomentAgentDecision(should_respond=False)

    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake)
    await _set_reply_mode(client, "always")
    created = await _create_moment(client, "路上听到一首老歌")

    deleted = await client.delete(f"/api/v1/moments/{created['moment_id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "deleted": True}

    result = await process_pending_outbox(test_settings)
    assert result["claimed"] == 0
    assert decisions_called == 0

    async with get_db(read_only=True) as db:
        moment = (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id == created["moment_id"]
                )
            )
        ).scalar_one()
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED
                )
            )
        ).scalar_one()
    assert moment.status == "archived"
    assert event.status == "cancelled"

    listing = await client.get("/api/v1/moments")
    assert all(item["moment_id"] != created["moment_id"] for item in listing.json()["items"])


async def test_deleted_source_never_revives_echo_display(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    source_id, _current_id, _interaction_id = await _create_echo_pair(
        client, test_settings, monkeypatch
    )
    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.json()["interaction"] is not None

    deleted = await client.delete(f"/api/v1/moments/{source_id}")
    assert deleted.status_code == 200

    hint = await client.get("/api/v1/moments/echo/latest")
    assert hint.json()["interaction"] is None
    listing = await client.get("/api/v1/moments")
    for item in listing.json()["items"]:
        reply = item["latest_agent_interaction"]
        if reply is not None:
            assert reply["source_moments"] == []


# ---------------------------------------------------------------------------
# Rewrite preference → generation prompt (生成端消费改写偏好)
# ---------------------------------------------------------------------------
def test_rewrite_preference_storage_is_bounded_and_deduped():
    settings = None
    texts = ["它记得你反复提起的那座城。", "你好像还是放不下这件事。"]

    for i, text in enumerate(texts):
        settings = policy.append_rewrite_preference(
            settings,
            text=text,
            created_at=f"2026-08-0{i + 1}T00:00:00Z",
        )
    # 重复文本不会重复入库
    settings = policy.append_rewrite_preference(
        settings,
        text=texts[0],
        created_at="2026-08-04T00:00:00Z",
    )
    prefs = policy.load_rewrite_preferences(settings, limit=10)
    assert prefs == list(reversed(texts))  # 去重且最新在前
    # 读取按最新在前
    assert policy.load_rewrite_preferences(settings, limit=1) == [texts[-1]]
    # 空 / 非法输入安全
    assert policy.load_rewrite_preferences(None) == []
    assert policy.load_rewrite_preferences({"moment_rewrite_preferences": "bad"}) == []


async def test_rewrite_preference_flows_into_generation_prompt(
    client: AsyncClient, test_settings: Settings, monkeypatch
):
    """用户改写回应后，下次生成必须把改写文本作为偏好传给模型。"""
    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(**kwargs)
        return MomentAgentDecision(should_respond=False)

    # 第一阶段：先产生一条 assistant 回应供改写
    monkeypatch.setattr(moment_service, "generate_agent_decision", _comment_decision())
    await _set_reply_mode(client, "always")
    created = await _create_moment(client, "窗台的薄荷冒了新叶")
    await process_pending_outbox(test_settings)

    # 找到 assistant 回应，并改写它
    listing = await client.get("/api/v1/moments")
    entry = next(
        item for item in listing.json()["items"] if item["moment_id"] == created["moment_id"]
    )
    interaction = entry["latest_agent_interaction"]
    assert interaction is not None

    rewritten_text = "它冒了一片很小的新叶，你好像跟着松了口气。"
    rewrite = await client.patch(
        f"/api/v1/moments/{created['moment_id']}/interactions/{interaction['interaction_id']}",
        json={"content": rewritten_text},
    )
    assert rewrite.status_code == 200, rewrite.text

    # 第二阶段：切换为捕获决策，再记录一条新碎片，验证偏好被带上
    monkeypatch.setattr(moment_service, "generate_agent_decision", _capture)
    captured.clear()
    await _create_moment(client, "今天又看了那盆薄荷")
    await process_pending_outbox(test_settings)

    assert captured, "应触发一次新的 generate_agent_decision"
    assert captured.get("rewrite_preferences") == [rewritten_text]
