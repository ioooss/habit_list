"""聊天流式接口：共处不自动建备忘/石子，显式模式仍可可靠持久化。"""
from __future__ import annotations

import asyncio
import json

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

import tests  # noqa: F401
from tests import mock_dashscope_asr, mock_dashscope_chat_stream, mock_dashscope_embeddings

pytestmark = pytest.mark.anyio


async def _collect_sse(r):
    """把 AsyncClient.stream 的 SSE bytes 转成 event 列表。"""
    events = []
    async for line in r.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            import orjson
            obj = orjson.loads(data)
        except Exception:
            import json
            obj = json.loads(data)
        events.append(obj)
    return events


@pytest.mark.anyio
@respx.mock
async def test_chat_default_does_not_create_memo_or_pebble(
    client: AsyncClient,
    test_settings,
):
    mock_dashscope_chat_stream(respx, test_settings, [
        "听起来这件事压在心里，", "我们先把当下说清楚。",
    ], usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})
    mock_dashscope_embeddings(respx, test_settings, n=1)

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"text": "明天下午三点提醒我交周报，别忘了啊"},
    ) as r:
        assert r.status_code == 200
        events = await _collect_sse(r)

    kinds = [e.get("event") for e in events]
    assert "memo_detected" not in kinds, kinds
    assert "delta" in kinds
    done_idx = [i for i, e in enumerate(events) if e.get("event") == "done"]
    assert done_idx, events
    done = events[done_idx[0]]
    assert done["data"]["memo_hit"] is False
    assert done["data"]["memo"] is None
    assert done["data"]["assistant_text"] == "听起来这件事压在心里，我们先把当下说清楚。"

    assert (await client.get("/api/v1/memos", params={"filter": "all"})).json()["items"] == []
    assert (await client.get("/api/v1/pebbles")).json()["total"] == 0

    # Working 保留会话连续性，Memory V2 只保留有来源的影子事件；不生成 Episodic。
    from app.db.database import get_sessionmaker
    from app.db.memory_models import OutboxEvent, UserEvent
    from app.db.models import Episodic, Working
    maker = get_sessionmaker()
    async with maker() as s:
        wks = list(
            (
                await s.execute(select(Working).order_by(Working.created_at.desc()))
            ).scalars().all()
        )
    roles = {w.role for w in wks}
    assert {"user", "assistant"} <= roles
    async with maker() as s:
        source_events = list((await s.execute(select(UserEvent))).scalars().all())
        extraction_events = list((await s.execute(select(OutboxEvent))).scalars().all())
        episodic = list((await s.execute(select(Episodic))).scalars().all())
    assert [event.content for event in source_events] == ["明天下午三点提醒我交周报，别忘了啊"]
    assert [event.mode for event in source_events] == ["confide"]
    assert all("我们先把当下说清楚" not in event.content for event in source_events)
    assert [event.event_type for event in extraction_events] == [
        "memory.extraction.requested"
    ]
    assert episodic == []


@pytest.mark.anyio
@respx.mock
async def test_chat_explicit_memo_mode_remains_available(
    client: AsyncClient,
    test_settings,
):
    mock_dashscope_chat_stream(respx, test_settings, ["已经放进手动备忘。"])
    mock_dashscope_embeddings(respx, test_settings, n=1)

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={
            "text": "明天下午三点提醒我交周报，别忘了啊",
            "mode": "memo",
        },
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)

    assert "memo_detected" in [event.get("event") for event in events]
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["memo_hit"] is True

    for _ in range(20):
        await asyncio.sleep(0.1)
        response = await client.get(
            "/api/v1/memos",
            params={"filter": "all", "q": "交周报"},
        )
        if response.status_code == 200 and response.json()["items"]:
            break
    memo = response.json()["items"][0]
    assert memo["source"] == "companion_explicit"
    pebbles = (await client.get("/api/v1/pebbles", params={"kind": "memo"})).json()
    assert pebbles["total"] == 1


@pytest.mark.anyio
@respx.mock
async def test_chat_explicit_life_mode_remains_a_deliberate_pebble(
    client: AsyncClient,
    test_settings,
):
    mock_dashscope_chat_stream(respx, test_settings, ["这一刻已经留下。"])
    mock_dashscope_embeddings(respx, test_settings, n=1)

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"text": "今晚回家时看见了一轮很亮的月亮", "mode": "life"},
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)

    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["memo_hit"] is False
    pebbles = (await client.get(
        "/api/v1/pebbles",
        params={"kind": "life_fragment"},
    )).json()
    assert pebbles["total"] == 1
    assert pebbles["groups"][0]["items"][0]["source"] == "life_explicit"


@pytest.mark.anyio
@respx.mock
async def test_chat_non_stream_returns_json(client: AsyncClient, test_settings):
    mock_dashscope_chat_stream(respx, test_settings, ["抱抱你，", "一直都在的。"])
    mock_dashscope_embeddings(respx, test_settings, n=1)
    r = await client.post("/api/v1/chat/completions", json={
        "text": "今天没人理我，好难受", "stream": False,
    })
    assert r.status_code == 200
    j = r.json()
    assert "assistant_text" in j
    assert "抱抱" in j["assistant_text"]


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize("mode", ["auto", "confide"])
async def test_non_memo_modes_are_not_overridden_by_memo_detection(
    client: AsyncClient,
    test_settings,
    mode: str,
):
    mock_dashscope_chat_stream(respx, test_settings, ["我听着，你继续说。"])
    mock_dashscope_embeddings(respx, test_settings, n=1)

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={
            "text": "明天下午三点提醒我交周报，但我现在只是想聊聊压力",
            "mode": mode,
        },
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)

    assert "memo_detected" not in [event.get("event") for event in events]
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["memo_hit"] is False
    assert (await client.get("/api/v1/memos", params={"filter": "all"})).json()["items"] == []

    from app.db.memory_models import UserEvent
    from app.db.database import get_sessionmaker

    async with get_sessionmaker()() as session:
        source = (await session.execute(select(UserEvent))).scalar_one()
    assert source.mode == "confide"


@pytest.mark.anyio
async def test_removed_legacy_retention_contract_is_rejected(client: AsyncClient):
    response = await client.post(
        "/api/v1/chat/completions",
        json={"text": "这句话只想说一次", "intent": "stay", "no_trace": True},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["memo", "life"])
async def test_removed_legacy_contract_is_rejected_for_all_modes(
    client: AsyncClient,
    mode: str,
):
    response = await client.post(
        "/api/v1/chat/completions",
        json={"text": "请保存", "mode": mode, "no_trace": True},
    )
    assert response.status_code == 422


@pytest.mark.anyio
@respx.mock
async def test_voice_only_chat_uses_transcript_and_keeps_original_asset(
    client: AsyncClient,
    test_settings,
):
    mock_dashscope_asr(respx, test_settings, text="今天在河边听见风")
    upload = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "true"},
        files={"file": ("voice.webm", b"RIFF-voice", "audio/webm")},
    )
    assert upload.status_code == 201, upload.text
    asset = upload.json()
    mock_dashscope_chat_stream(respx, test_settings, ["我听见了。"])
    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"audio_asset_id": asset["asset_id"]},
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["assistant_text"] == "我听见了。"

    from app.db.database import get_sessionmaker
    from app.db.models import MediaAsset

    async with get_sessionmaker()() as session:
        saved = (await session.execute(select(MediaAsset))).scalar_one()
    assert saved.owner_type == "chat_turn"
    assert saved.transcript == "今天在河边听见风"


@pytest.mark.anyio
@respx.mock
async def test_voice_only_chat_without_transcript_sends_original_audio_without_memory_event(
    client: AsyncClient,
    test_settings,
):
    upload = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "false"},
        files={"file": ("voice.webm", b"RIFF-raw-voice", "audio/webm")},
    )
    assert upload.status_code == 201, upload.text
    asset = upload.json()
    assert asset["transcript"] is None
    mock_dashscope_chat_stream(respx, test_settings, ["我先收下这段声音。"])

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"audio_asset_id": asset["asset_id"]},
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["assistant_text"] == "我先收下这段声音。"

    chat_request = next(
        call.request
        for call in respx.calls
        if call.request.url.path.endswith("/chat/completions")
    )
    payload = json.loads(chat_request.content)
    user_message = payload["messages"][-1]
    assert isinstance(user_message["content"], list)
    audio_parts = [part for part in user_message["content"] if part.get("type") == "input_audio"]
    assert audio_parts
    assert audio_parts[0]["input_audio"]["data"]
    assert audio_parts[0]["input_audio"]["format"] == "webm"

    from app.db.database import get_sessionmaker
    from app.db.memory_models import UserEvent

    async with get_sessionmaker()() as session:
        assert list((await session.execute(select(UserEvent))).scalars().all()) == []
