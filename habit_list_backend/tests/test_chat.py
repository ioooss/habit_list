"""聊天流式接口（重点：memo 自动识别入备忘 + SSE delta/done 顺序 + Ledger/Working/Episodic 都写）。"""
from __future__ import annotations

import asyncio

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

import tests  # noqa: F401
from tests import mock_dashscope_chat_stream, mock_dashscope_embeddings

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
async def test_chat_memo_detected_and_saved(client: AsyncClient, test_settings):
    mock_dashscope_chat_stream(respx, test_settings, [
        "好的，", " 这事儿给你记着。",
    ], usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})
    mock_dashscope_embeddings(respx, test_settings, n=1)

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={"text": "明天下午三点提醒我交周报，别忘了啊"},
    ) as r:
        assert r.status_code == 200
        events = await _collect_sse(r)

    # SSE 顺序：memo_detected → delta → delta → meta? → done
    kinds = [e.get("event") for e in events]
    assert "memo_detected" in kinds, kinds
    assert "delta" in kinds
    done_idx = [i for i, e in enumerate(events) if e.get("event") == "done"]
    assert done_idx, events
    done = events[done_idx[0]]
    assert done["data"]["memo_hit"] is True
    assert "明天" in done["data"]["memo"]["due_text"] or "下午" in done["data"]["memo"]["due_text"]
    assert done["data"]["assistant_text"] == "好的， 这事儿给你记着。"

    # SSE 发完 + fire-and-forget 写库可能还没跑完 → 给一点时间
    for _ in range(20):
        await asyncio.sleep(0.1)
        r = await client.get("/api/v1/memos", params={"filter": "all", "q": "交周报"})
        if r.status_code == 200 and r.json()["items"]:
            break
    lst = r.json()
    assert lst["items"], "备忘没写入库？"
    m = lst["items"][0]
    assert "交周报" in m["clean_text"] or "交周报" in m["text"]
    assert m["source"] == "companion_auto"
    # 石子列表应出现一条 memo kind 的
    pebbles = (await client.get("/api/v1/pebbles", params={"kind": "memo"})).json()
    assert pebbles["total"] >= 1, pebbles
    # working 最近几条里有 user + assistant
    from app.db.database import get_sessionmaker
    from app.db.memory_models import OutboxEvent, UserEvent
    from app.db.models import Working
    maker = get_sessionmaker()
    async with maker() as s:
        wks = list((await s.execute(select(Working).order_by(Working.created_at.desc()))).scalars().all())
    roles = {w.role for w in wks}
    assert {"user", "assistant"} <= roles
    async with maker() as s:
        source_events = list((await s.execute(select(UserEvent))).scalars().all())
        extraction_events = list((await s.execute(select(OutboxEvent))).scalars().all())
    assert [event.content for event in source_events] == ["明天下午三点提醒我交周报，别忘了啊"]
    assert all("这事儿给你记着" not in event.content for event in source_events)
    assert [event.event_type for event in extraction_events] == [
        "memory.extraction.requested"
    ]


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
async def test_explicit_confide_mode_is_not_overridden_by_memo_detection(
    client: AsyncClient,
    test_settings,
):
    mock_dashscope_chat_stream(respx, test_settings, ["我听着，你继续说。"])
    mock_dashscope_embeddings(respx, test_settings, n=1)

    async with client.stream(
        "POST",
        "/api/v1/chat/completions",
        json={
            "text": "明天下午三点提醒我交周报，但我现在只是想聊聊压力",
            "mode": "confide",
        },
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)

    assert "memo_detected" not in [event.get("event") for event in events]
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["memo_hit"] is False
    assert (await client.get("/api/v1/memos", params={"filter": "all"})).json()["items"] == []

    from app.db.database import get_sessionmaker
    from app.db.memory_models import UserEvent

    async with get_sessionmaker()() as session:
        source = (await session.execute(select(UserEvent))).scalar_one()
    assert source.mode == "confide"
