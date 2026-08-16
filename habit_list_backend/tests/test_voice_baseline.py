"""声音基线 §8 验收清单：降级、时段、离开与回归、声线。

这一份守的不是功能，是承诺。这个产品唯一的承诺是「它真的在听」，
下面每一条都对应一个最容易把这句话戳破的地方：模型挂了它编不编、
凌晨三点它知不知道现在几点、消失 40 天回来它说不说话、以及它说话的长度。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

import tests  # noqa: F401
from app.core.safety import CRISIS_RESPONSE
from app.memory.situation import (
    absence_days,
    resolve_absence_band,
    resolve_day_band,
    situation_to_prompt,
)
from app.memory.system1 import DEGRADED_NOTICE, SYSTEM_PROMPT

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_HTML = REPO_ROOT / "app.html"
SYSTEM1_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "memory" / "system1.py"
).read_text(encoding="utf-8")


async def _collect_sse(response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        events.append(json.loads(payload))
    return events


def _break_the_model(respx_mock, settings) -> None:
    """让 chat/completions 整条失败——降级路径的唯一入口。"""

    respx_mock.post(f"{settings.dashscope_base_url}/chat/completions").respond(500)


def _capture_system_prompt(respx_mock, settings, parts=("我在。",)) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request):
        from httpx import Response

        captured["messages"] = json.loads(request.content)["messages"]
        body = b"".join(
            ('data: {"choices":[{"delta":{"content":' + json.dumps(part, ensure_ascii=False) + '}}]}\n\n').encode()
            for part in parts
        ) + b"data: [DONE]\n\n"
        return Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    respx_mock.post(f"{settings.dashscope_base_url}/chat/completions").side_effect = handler
    return captured


def _tz_name_for_local_hour(target_hour: int, now: datetime | None = None) -> str:
    """找一个真实时区，使用户此刻的本地时间正好落在 ``target_hour``。

    比 monkeypatch 掉「现在几点」更值得写：被测的是整条真实链路
    （users.timezone → zoneinfo → 时段档 → prompt），不是我自己的桩。
    ``Etc/GMT+N`` 的符号是反的（``Etc/GMT-8`` 就是 UTC+8），这里按 POSIX 语义拼。
    """

    utc_hour = (now or datetime.now(timezone.utc)).hour
    offset = (target_hour - utc_hour) % 24
    if offset > 12:
        offset -= 24
    return f"Etc/GMT{-offset:+d}"


async def _set_timezone(client: AsyncClient, name: str) -> None:
    response = await client.patch("/api/v1/me/profile", json={"timezone": name})
    assert response.status_code == 200, response.text


async def _backdate_last_turn(days: float) -> None:
    """把已有话轮整体推到过去，模拟用户消失了这么多天。"""

    from app.db.database import get_sessionmaker
    from app.db.models import Working

    then = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    maker = get_sessionmaker()
    async with maker() as session:
        rows = list((await session.execute(select(Working))).scalars().all())
        assert rows, "需要先有一轮对话才能把它推到过去"
        for row in rows:
            row.created_at = then
        await session.commit()


# --- #1 / #3：模型不可用 + 普通话轮 ---------------------------------------


@respx.mock
async def test_degraded_turn_says_nothing_human(client: AsyncClient, test_settings):
    _break_the_model(respx, test_settings)

    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "今天真的好累，什么都不想做"}
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse(response)

    deltas = [event for event in events if event.get("event") == "delta"]
    assert deltas == [], "模型不可用时它一个字都不该说（声音基线 §3.2）"
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["assistant_text"] == ""
    assert done["data"]["fallback"] is True
    assert done["data"]["degraded_notice"] == DEGRADED_NOTICE
    # 三件事，一件不多：这不是你的错、你的话没有丢、下一步能做什么。
    assert "接不上" in DEGRADED_NOTICE
    assert "你的话留在这里了" in DEGRADED_NOTICE


@respx.mock
async def test_degraded_turn_keeps_the_user_words_and_writes_no_empty_reply(
    client: AsyncClient,
    test_settings,
):
    _break_the_model(respx, test_settings)

    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "今天真的好累"}
    ) as response:
        events = await _collect_sse(response)
    assert next(e for e in events if e.get("event") == "done")["data"]["fallback"] is True

    from app.db.database import get_sessionmaker
    from app.db.models import Working

    maker = get_sessionmaker()
    async with maker() as session:
        rows = list((await session.execute(select(Working))).scalars().all())
    assert [row.role for row in rows] == ["user"], "它没说话，就不该留下一条空的 assistant 话轮"
    assert rows[0].content == "今天真的好累", "用户的话必须留着"


def test_no_keyword_selected_fallback_poems_remain_in_system1():
    # 曾经这里按正则命中「累」就发一句「你回来的时候，一定很安静。」——
    # 用户分辨不出那句话背后没有任何理解，于是以为被听见了。那是腹语，不是降级。
    # 只看代码：那几句诗现在只应该活在解释为什么删掉它们的注释里。
    code = "\n".join(
        line for line in SYSTEM1_SOURCE.splitlines() if not line.lstrip().startswith("#")
    )
    for ghost in ("你回来的时候", "不必把这种感觉赶走", "FALLBACK_LINES", "_fallback_line"):
        assert ghost not in code, ghost


def test_frontend_consumes_the_fallback_flag():
    html = APP_HTML.read_text(encoding="utf-8")
    # 后端诚实地标了降级，前端丢掉，等于后端的诚实不存在（§3.2）。
    assert "if(dd.fallback)" in html
    assert "pushDegraded(" in html
    assert "data-degraded" in html
    # 降级形态不能长成陪伴气泡：没有落款、不能朗读。
    degraded = html[html.index("function pushDegraded("):]
    degraded = degraded[: degraded.index("\n}\n")]
    for companion_only in ("bubble", "message-speak", "— 它"):
        assert companion_only not in degraded, companion_only


# --- #2 / #7：危机永不降级 -------------------------------------------------


@respx.mock
async def test_crisis_is_delivered_even_when_the_model_is_gone(
    client: AsyncClient,
    test_settings,
):
    _break_the_model(respx, test_settings)

    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "我不想活了"}
    ) as response:
        events = await _collect_sse(response)

    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["assistant_text"] == CRISIS_RESPONSE
    assert done["data"]["fallback"] is True
    deltas = [event["data"] for event in events if event.get("event") == "delta"]
    assert "".join(deltas) == CRISIS_RESPONSE


def test_crisis_copy_carries_a_dialable_channel_and_needs_no_daylight():
    assert "120" in CRISIS_RESPONSE and "110" in CRISIS_RESPONSE
    assert CRISIS_RESPONSE.rstrip().endswith("？"), "必须问，因为要评估当下的危险（§6）"
    # 深夜档不允许危机响应缩减，也不允许它含任何要等到白天的建议（§4.3）。
    for daylight in ("明天", "白天", "工作时间", "上班", "预约"):
        assert daylight not in CRISIS_RESPONSE, daylight


@respx.mock
async def test_crisis_is_delivered_at_night_unchanged(client: AsyncClient, test_settings):
    await _set_timezone(client, _tz_name_for_local_hour(2))
    _break_the_model(respx, test_settings)

    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "我想死"}
    ) as response:
        events = await _collect_sse(response)

    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["assistant_text"] == CRISIS_RESPONSE


# --- #6：它得知道现在几点 -------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "band"),
    [
        (23, "night"), (0, "night"), (2, "night"), (4, "night"),
        (5, "day"), (9, "day"), (15, "day"), (17, "day"),
        (18, "evening"), (21, "evening"), (22, "evening"),
    ],
)
def test_day_bands_cover_the_clock(hour, band):
    assert resolve_day_band(hour) == band


def test_night_band_never_manages_the_user():
    night = situation_to_prompt("night", "continuous")
    assert "深夜" in night
    assert "不引出新话题" in night
    assert "不提计划" in night and "不提「明天」" in night
    # 「早点睡吧」是把陪伴换成管理，所以这句必须显式禁掉，不能只靠模型自觉。
    assert "不要劝他睡觉" in night
    # 时段只改语气与长度，不改可用性：深夜不许屏蔽功能、不许推内容、不许问候。
    for out_of_scope in ("早上好", "晚安", "不要使用", "暂时不可用"):
        assert out_of_scope not in night, out_of_scope


@respx.mock
async def test_prompt_situation_differs_between_night_and_afternoon(
    client: AsyncClient,
    test_settings,
):
    await _set_timezone(client, _tz_name_for_local_hour(2))
    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "我今天好累"}
    ) as response:
        await _collect_sse(response)
    night_prompt = captured["messages"][0]["content"]

    await _set_timezone(client, _tz_name_for_local_hour(15))
    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "我今天好累"}
    ) as response:
        await _collect_sse(response)
    day_prompt = captured["messages"][0]["content"]

    assert night_prompt != day_prompt
    assert "深夜" in night_prompt and "深夜" not in day_prompt
    assert "白天" in day_prompt


async def test_unknown_timezone_is_refused_at_the_boundary(client: AsyncClient):
    # 存下一个 zoneinfo 认不出的名字，等于让它以后不知道现在几点。
    response = await client.patch("/api/v1/me/profile", json={"timezone": "Mars/Olympus"})
    assert response.status_code == 400
    profile = await client.get("/api/v1/me/profile")
    assert profile.json()["timezone"] != "Mars/Olympus"


# --- #8 / #9 / #10：离开与回归 --------------------------------------------


@pytest.mark.parametrize(
    ("days", "band"),
    [
        (None, "continuous"), (0, "continuous"), (2, "continuous"), (2.9, "continuous"),
        (3, "short"), (10, "short"), (13.9, "short"),
        (14, "long"), (30, "long"), (60, "long"),
        (61, "distant"), (90, "distant"),
    ],
)
def test_absence_bands_cover_the_calendar(days, band):
    assert resolve_absence_band(days) == band


def test_first_ever_turn_is_not_an_absence():
    assert absence_days(datetime.now(timezone.utc), None) is None
    assert resolve_absence_band(None) == "continuous"


@pytest.mark.parametrize("band", ["continuous", "short"])
def test_short_gaps_are_not_events(band):
    prompt = situation_to_prompt("day", band)
    # 与其叮嘱它「别提间隔」（那等于先把间隔说给它听），不如让它根本不知道有间隔。
    assert "间隔" not in prompt
    assert "上次" not in prompt


@pytest.mark.parametrize("band", ["long", "distant"])
def test_long_absence_may_be_acknowledged_once_and_never_as_a_reproach(band):
    prompt = situation_to_prompt("day", band)
    assert "最多一次" in prompt
    # 「你好久没来了」是指责，「我一直在等你」是绑架，「你还好吗？」问句 + 关切压力
    # + 把回合推回去三条全犯。它们只允许以「不要」的形式出现。
    assert "不要问他去了哪里、为什么、还好吗" in prompt
    assert "不要说「你好久没来了」「我一直在等你」" in prompt
    assert "不要表达等待或期待" in prompt


def test_distant_absence_stops_citing_old_terrain():
    distant = situation_to_prompt("day", "distant")
    assert "不要主动引用超过一条旧记录" in distant
    assert "不要主动引用" not in situation_to_prompt("day", "long")


@respx.mock
async def test_recent_turns_put_no_absence_line_in_the_prompt(
    client: AsyncClient,
    test_settings,
):
    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "第一句"}
    ) as response:
        await _collect_sse(response)
    await _backdate_last_turn(10)

    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "第二句"}
    ) as response:
        await _collect_sse(response)
    assert "间隔" not in captured["messages"][0]["content"]


@respx.mock
async def test_returning_after_a_month_injects_the_long_absence_band(
    client: AsyncClient,
    test_settings,
):
    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "第一句"}
    ) as response:
        await _collect_sse(response)
    await _backdate_last_turn(30)

    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "我回来了"}
    ) as response:
        await _collect_sse(response)
    prompt = captured["messages"][0]["content"]
    assert "最多一次" in prompt
    assert "不要主动引用超过一条旧记录" not in prompt


@respx.mock
async def test_returning_after_three_months_also_holds_back_old_memory(
    client: AsyncClient,
    test_settings,
):
    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "第一句"}
    ) as response:
        await _collect_sse(response)
    await _backdate_last_turn(90)

    captured = _capture_system_prompt(respx, test_settings)
    async with client.stream(
        "POST", "/api/v1/chat/completions", json={"text": "我回来了"}
    ) as response:
        await _collect_sse(response)
    assert "不要主动引用超过一条旧记录" in captured["messages"][0]["content"]


# --- #11：声线 -------------------------------------------------------------


def test_voice_rules_are_in_the_persona_not_the_situation():
    assert "默认一到两句" in SYSTEM_PROMPT
    assert "不用问句收尾" in SYSTEM_PROMPT
    assert "不排比" in SYSTEM_PROMPT and "不列点" in SYSTEM_PROMPT
    assert "不总结" in SYSTEM_PROMPT
    # 人格不随时间变：时段与间隔不许写进 SYSTEM_PROMPT（§4.2）。
    for situational in ("深夜", "傍晚", "间隔", "上次说话"):
        assert situational not in SYSTEM_PROMPT, situational


# --- #4 / #5：局部失败与整体失败不同 --------------------------------------


def test_failed_transcription_says_it_did_not_hear_and_keeps_the_recording():
    html = APP_HTML.read_text(encoding="utf-8")
    assert "这段我没听清。录音留着了，可以直接发送" in html


def test_embedding_failure_stays_silent():
    # 召回退化但对话仍然成立——没有承诺被打破，就不需要告诉用户（§3.4）。
    embedding_failure = SYSTEM1_SOURCE[
        SYSTEM1_SOURCE.index("query embedding failed") - 400 :
        SYSTEM1_SOURCE.index("query embedding failed") + 200
    ]
    assert "fallback" not in embedding_failure
    assert DEGRADED_NOTICE not in embedding_failure
