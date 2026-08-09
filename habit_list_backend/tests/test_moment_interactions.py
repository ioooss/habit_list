"""Life fragments remain primary records while AI interaction stays isolated."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import OutboxEvent, UserEvent
from app.db.models import Memo, MomentInteraction, Working
from app.memory_v2.worker import process_pending_outbox
from app.moments import service as moment_service
from app.moments.service import MOMENT_RESPONSE_REQUESTED, MomentAgentDecision
from app.providers import dashscope

pytestmark = pytest.mark.anyio


async def _set_reply_mode(client: AsyncClient, mode: str) -> None:
    response = await client.patch(
        "/api/v1/me/profile",
        json={"settings": {"life_reply_mode": mode}},
    )
    assert response.status_code == 200, response.text


async def test_life_fragment_defaults_to_no_terrain_and_silent_mode_has_no_agent_work(
    client: AsyncClient,
):
    await _set_reply_mode(client, "silent")
    response = await client.post(
        "/api/v1/moments",
        json={"text": "窗台上的薄荷今天长出一片新叶"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["use_for_terrain"] is False
    assert payload["user_event_id"] is None
    assert payload["response_pending"] is False

    async with get_db(read_only=True) as db:
        user_events = list((await db.execute(select(UserEvent))).scalars().all())
        outbox = list((await db.execute(select(OutboxEvent))).scalars().all())
    assert user_events == []
    assert outbox == []


async def test_agent_echo_is_source_grounded_and_visible_on_the_fragment(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    await _set_reply_mode(client, "silent")
    source_response = await client.post(
        "/api/v1/moments",
        json={
            "text": "上个月第一次给窗台的薄荷换了盆",
            "allow_proactive": True,
        },
    )
    source_id = source_response.json()["moment_id"]

    await _set_reply_mode(client, "always")
    current_response = await client.post(
        "/api/v1/moments",
        json={"text": "薄荷今天终于冒出了一片很小的新叶"},
    )
    current_id = current_response.json()["moment_id"]
    assert current_response.json()["response_pending"] is True

    async def _fake_decision(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="echo",
            kind="echo",
            comment="它和上次换盆的那一天，接上了一点绿色。",
            source_moment_ids=[source_id],
            why_now="新叶接上了换盆那次，能看见这段时间的延续。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake_decision)
    result = await process_pending_outbox(test_settings)
    assert result == {"claimed": 1, "processed": 1, "retried": 0, "dead": 0}

    listing = await client.get("/api/v1/moments")
    assert listing.status_code == 200, listing.text
    current = next(item for item in listing.json()["items"] if item["moment_id"] == current_id)
    reply = current["latest_agent_interaction"]
    assert reply["kind"] == "echo"
    assert reply["reaction"] == "echo"
    assert reply["why_now"].startswith("新叶接上了换盆")
    assert reply["source_moments"] == [
        {
            "moment_id": source_id,
            "excerpt": "上个月第一次给窗台的薄荷换了盆",
            "created_at": source_response.json()["created_at"],
        }
    ]
    assert current["response_pending"] is False


async def test_image_life_fragment_reaches_agent_with_typed_multimodal_parts(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """The persisted media id must become a provider-compatible image part."""

    await _set_reply_mode(client, "always")
    uploaded = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image"},
        files={"file": ("window.png", io.BytesIO(b"fake-image"), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["asset_id"]

    captured: dict[str, object] = {}

    async def _fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {
            "should_respond": True,
            "reaction": "seen",
            "kind": "comment",
            "comment": "窗边这一刻，我看见了。",
            "source_moment_ids": [],
            "why_now": "",
        }

    monkeypatch.setattr(dashscope, "chat_json", _fake_chat_json)
    created = await client.post(
        "/api/v1/moments",
        json={
            "text": "窗边的光今天落得很慢",
            "media_asset_ids": [asset_id],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["response_pending"] is True

    result = await process_pending_outbox(test_settings)
    assert result["processed"] == 1

    messages = captured["messages"]
    assert isinstance(messages, list)
    user_message = messages[-1]
    assert user_message["role"] == "user"
    parts = user_message["content"]
    assert isinstance(parts, list)
    assert parts[0]["type"] == "text"
    assert "窗边的光今天落得很慢" in parts[0]["text"]
    image_parts = [part for part in parts if part.get("type") == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    listing = await client.get("/api/v1/moments")
    item = next(row for row in listing.json()["items"] if row["moment_id"] == created.json()["moment_id"])
    assert item["latest_agent_interaction"]["content"] == "窗边这一刻，我看见了。"


async def test_fragment_reply_stays_in_thread_and_does_not_create_memory_or_todo(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    await _set_reply_mode(client, "silent")
    created = await client.post(
        "/api/v1/moments",
        json={"text": "下班路上看到一只蹲在路灯下的橘猫"},
    )
    moment_id = created.json()["moment_id"]

    reply = await client.post(
        f"/api/v1/moments/{moment_id}/interactions",
        json={"content": "我当时停下来陪它待了两分钟"},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["response_pending"] is True

    async with get_db(read_only=True) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED
                )
            )
        ).scalar_one()
    assert "text" not in event.payload_json
    assert "content" not in event.payload_json

    async def _fake_decision(**kwargs):
        assert kwargs["trigger_type"] == "user_reply"
        return MomentAgentDecision(
            should_respond=True,
            reaction="paused",
            kind="comment",
            comment="两分钟很短，但那盏路灯和那只橘猫被你认真看见了。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake_decision)
    result = await process_pending_outbox(test_settings)
    assert result["processed"] == 1

    thread = await client.get(f"/api/v1/moments/{moment_id}/interactions")
    assert thread.status_code == 200, thread.text
    assert [item["actor"] for item in thread.json()["items"]] == ["user", "assistant"]
    assert thread.json()["response_pending"] is False

    async with get_db(read_only=True) as db:
        interactions = list(
            (await db.execute(select(MomentInteraction))).scalars().all()
        )
        working = list((await db.execute(select(Working))).scalars().all())
        memos = list((await db.execute(select(Memo))).scalars().all())
        user_events = list((await db.execute(select(UserEvent))).scalars().all())
    assert len(interactions) == 2
    assert working == []
    assert memos == []
    assert user_events == []


async def test_fragment_thread_accepts_original_voice_and_deletes_it_with_fragment(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_asr(_raw: bytes, _filename: str) -> dashscope.Transcription:
        return dashscope.Transcription(text="我在楼下听见雨落在伞上")

    async def fake_decision(**_kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="seen",
            kind="comment",
            comment="这段雨声被你留在这一片里了。",
        )

    monkeypatch.setattr(dashscope, "asr_transcribe", fake_asr)
    monkeypatch.setattr(moment_service, "generate_agent_decision", fake_decision)
    await _set_reply_mode(client, "silent")
    uploaded = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "true"},
        files={"file": ("reply.webm", io.BytesIO(b"original-voice"), "audio/webm")},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    created = await client.post(
        "/api/v1/moments", json={"text": "楼下的雨停了一会儿"}
    )
    assert created.status_code == 201, created.text
    moment_id = created.json()["moment_id"]

    reply = await client.post(
        f"/api/v1/moments/{moment_id}/interactions",
        json={"audio_asset_id": asset["asset_id"]},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["interaction"]["content"] == "我在楼下听见雨落在伞上"
    assert reply.json()["interaction"]["audio_asset_id"] == asset["asset_id"]
    assert (await process_pending_outbox(test_settings))["processed"] == 1

    thread = await client.get(f"/api/v1/moments/{moment_id}/interactions")
    assert thread.status_code == 200, thread.text
    assert thread.json()["items"][0]["audio_asset_id"] == asset["asset_id"]
    assert thread.json()["items"][1]["actor"] == "assistant"

    deleted = await client.delete(f"/api/v1/moments/{moment_id}")
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(asset["url"])).status_code == 404


async def test_fragment_thread_allows_voice_without_transcript(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    seen_media: list[str] = []

    async def fake_decision(**kwargs):
        seen_media.extend(kwargs.get("media_asset_ids") or [])
        return MomentAgentDecision(
            should_respond=True,
            reaction="seen",
            kind="comment",
            comment="我先收下这一段声音。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", fake_decision)
    await _set_reply_mode(client, "silent")
    uploaded = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "false"},
        files={"file": ("reply.webm", io.BytesIO(b"raw-voice"), "audio/webm")},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    created = await client.post(
        "/api/v1/moments", json={"text": "今天的雨停得很突然"}
    )
    moment_id = created.json()["moment_id"]

    reply = await client.post(
        f"/api/v1/moments/{moment_id}/interactions",
        json={"audio_asset_id": asset["asset_id"]},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["interaction"]["content"] == ""
    assert reply.json()["interaction"]["audio_asset_id"] == asset["asset_id"]
    assert (await process_pending_outbox(test_settings))["processed"] == 1
    assert seen_media == [asset["asset_id"]]


async def _create_fragment_with_dead_response(client: AsyncClient) -> str:
    """Create a fragment, force its initial outbox event into the dead state."""
    await _set_reply_mode(client, "always")
    created = await client.post(
        "/api/v1/moments",
        json={"text": "傍晚的风里有桂花的味道，今年第一次闻到"},
    )
    assert created.status_code == 201, created.text
    moment_id = created.json()["moment_id"]

    async with get_db(read_only=False) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                    OutboxEvent.aggregate_id == moment_id,
                )
            )
        ).scalar_one()
        event.status = "dead"
        event.attempts = 5
        event.last_error = "model_timeout"
        event.locked_at = None
    return moment_id


async def test_retry_reanimates_a_dead_fragment_response_and_is_idempotent(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    moment_id = await _create_fragment_with_dead_response(client)

    # Listing reports the fragment as failed before retry.
    listing = await client.get("/api/v1/moments")
    failed = [m for m in listing.json()["items"] if m["moment_id"] == moment_id][0]
    assert failed["response_failed"] is True

    retry = await client.post(f"/api/v1/moments/{moment_id}/retry")
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["retried"] is True
    assert body["response_pending"] is True
    assert body["response_failed"] is False

    async with get_db(read_only=True) as db:
        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                    OutboxEvent.aggregate_id == moment_id,
                )
            )
        ).scalar_one()
    assert event.status == "pending"
    assert event.attempts == 0
    assert event.last_error is None

    # The worker still produces exactly one interaction for the reanimated event.
    async def _fake_decision(**kwargs):
        return MomentAgentDecision(
            should_respond=True,
            reaction="paused",
            kind="comment",
            comment="桂花的第一缕香气，像是秋天悄悄递来的字条。",
        )

    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake_decision)
    assert (await process_pending_outbox(test_settings))["processed"] == 1

    thread = await client.get(f"/api/v1/moments/{moment_id}/interactions")
    assistant = [i for i in thread.json()["items"] if i["actor"] == "assistant"]
    assert len(assistant) == 1

    # A second retry after a response exists is a safe no-op.
    retry_again = await client.post(f"/api/v1/moments/{moment_id}/retry")
    assert retry_again.status_code == 200
    assert retry_again.json()["retried"] is False
    assert retry_again.json()["reason"] == "already_responded"

    async with get_db(read_only=True) as db:
        events = list(
            (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                        OutboxEvent.aggregate_id == moment_id,
                    )
                )
            ).scalars().all()
        )
    # No duplicate outbox event was created by the no-op retry.
    assert len(events) == 1


async def test_retry_is_a_noop_when_a_response_is_already_pending(
    client: AsyncClient,
):
    await _set_reply_mode(client, "always")
    created = await client.post(
        "/api/v1/moments", json={"text": "地铁上读完了最后一章"}
    )
    moment_id = created.json()["moment_id"]

    retry = await client.post(f"/api/v1/moments/{moment_id}/retry")
    assert retry.status_code == 200
    body = retry.json()
    assert body["retried"] is False
    assert body["reason"] == "already_pending"
    assert body["response_pending"] is True


async def test_retry_respects_save_only_fragments(client: AsyncClient):
    await _set_reply_mode(client, "always")
    created = await client.post(
        "/api/v1/moments",
        json={"text": "只是想存一下这句话", "allow_response": False},
    )
    moment_id = created.json()["moment_id"]

    retry = await client.post(f"/api/v1/moments/{moment_id}/retry")
    assert retry.status_code == 200
    body = retry.json()
    assert body["retried"] is False
    assert body["reason"] == "save_only"
    assert body["response_pending"] is False


async def test_retry_unknown_moment_returns_404(client: AsyncClient):
    retry = await client.post("/api/v1/moments/does-not-exist/retry")
    assert retry.status_code == 404
