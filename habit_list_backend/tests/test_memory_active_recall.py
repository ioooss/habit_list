"""记忆正式启用（memory_v2_mode=active）后的行为契约。

active 不是一个实验开关，而是产品形态：用户必须能看见"它记得我"。
这里同时钉住降级路径——退回 shadow_retrieve 时，召回仍然发生、轨迹仍然
落库，但一个字都不进入回复，也不向前端宣称"我用了记忆"。
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

import tests  # noqa: F401
from app.core.config import Settings
from app.db.database import get_db
from app.db.memory_models import MemoryRetrievalTrace
from app.memory import system1
from app.memory_v2.service import enqueue_user_event
from app.memory_v2.worker import process_pending_outbox
from tests import mock_dashscope_chat_stream

pytestmark = pytest.mark.anyio

_RECALL_QUESTION = "你还记得我喜欢什么吗？"


async def _seed_confirmed_preference(settings: Settings) -> None:
    async with get_db(read_only=False) as db:
        await enqueue_user_event(
            db,
            user_id=settings.default_user_id,
            session_id="active-recall-session",
            request_id="active-recall-seed",
            content="我喜欢手冲咖啡。",
            mode="confide",
            terrain_eligible=False,
            settings=settings,
        )
    assert (await process_pending_outbox(settings))["processed"] == 1


def _pin_mode(monkeypatch, settings: Settings, mode: str) -> Settings:
    """把 system1 读到的运行模式钉死，其余配置保持生产默认。"""

    overridden = settings.model_copy(
        update={"memory_v2_mode": mode, "memory_v2_min_retrieval_score": 0.0}
    )
    monkeypatch.setattr(system1, "get_settings", lambda: overridden)
    return overridden


def _system_prompt() -> str:
    for call in respx.calls:
        if call.request.url.path.endswith("/chat/completions"):
            return json.loads(call.request.content)["messages"][0]["content"]
    raise AssertionError("模型没有被调用")


async def _chat(client: AsyncClient) -> list[dict]:
    events: list[dict] = []
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": _RECALL_QUESTION}
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            events.append(json.loads(payload))
    return events


@respx.mock
async def test_active_mode_lets_a_confirmed_memory_reach_the_reply(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch,
):
    settings = _pin_mode(monkeypatch, test_settings, "active")
    await _seed_confirmed_preference(settings)
    mock_dashscope_chat_stream(respx, settings, ["你说过喜欢手冲咖啡。"])

    events = await _chat(client)

    referenced = [event for event in events if event.get("event") == "memory_reference"]
    assert len(referenced) == 1
    assert [item["claim_text"] for item in referenced[0]["data"]] == ["喜欢手冲咖啡"]
    # 前端拿到的引用必须真的进了系统提示，否则"它记得我"是假的。
    assert "喜欢手冲咖啡" in _system_prompt()

    async with get_db(read_only=True) as db:
        traces = list((await db.execute(select(MemoryRetrievalTrace))).scalars().all())
    assert [trace.used_in_response for trace in traces] == [True]


@respx.mock
async def test_shadow_retrieve_still_traces_but_never_speaks(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch,
):
    settings = _pin_mode(monkeypatch, test_settings, "shadow_retrieve")
    await _seed_confirmed_preference(settings)
    mock_dashscope_chat_stream(respx, settings, ["我在听。"])

    events = await _chat(client)

    assert [event for event in events if event.get("event") == "memory_reference"] == []
    assert "喜欢手冲咖啡" not in _system_prompt()
    # 降级不等于停止观测：轨迹照写，只是标明这一轮没有用于回复。
    async with get_db(read_only=True) as db:
        traces = list((await db.execute(select(MemoryRetrievalTrace))).scalars().all())
    assert [trace.used_in_response for trace in traces] == [False]
    assert traces[0].selected_json
