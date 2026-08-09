"""此刻天气：读，而不是推断；短期，而不是序列。

上游权威：`内在地形-产品基线-v2.md` §4（此刻天气「默认短期」「用户可控」）、
§11（Working 层「限制长期化」）；`内在地形-地形页视觉规范-v1.md` §4。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_db
from app.db.models import User, Working
from app.memory.weather import read_weather

pytestmark = pytest.mark.anyio


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_weather(
    settings: Settings,
    *,
    word: str | None,
    age_hours: float = 0.0,
    role: str = "user",
) -> None:
    async with get_db(read_only=False) as db:
        db.add(
            Working(
                user_id=settings.default_user_id,
                session_id="s-weather",
                role=role,
                content="我今天好累",
                mood=word,
                created_at=_iso(datetime.now(UTC) - timedelta(hours=age_hours)),
            )
        )


# ---- 读，而不是推断 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我今天好累", "累"),
        ("有点烦", "烦"),
        ("我累了", "累"),
        ("今天挺开心的", "开心"),
        ("我觉得很委屈", "委屈"),
        ("越来越迷茫", "迷茫"),
    ],
)
def test_weather_echoes_the_users_own_word(text: str, expected: str):
    """天气永远是用户自己写下的那个词，不是一个更漂亮的替代词。"""
    assert read_weather(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "空调坏了",          # 「空」是子串，不是状态
        "这件事很重要",       # 「重」是子串，不是状态
        "没什么特别的",       # 没有状态词
        "",
        "   ",
    ],
)
def test_weather_stays_silent_when_it_cannot_read_a_word(text: str):
    """读不出来就没有天气。空槽位是正确结果，不是失败（视觉规范 §4）。"""
    assert read_weather(text) is None


@pytest.mark.parametrize("text", ["我很不开心", "有点不舒服", "我不累"])
def test_weather_never_reads_a_negation_as_its_opposite(text: str):
    """宁可读不到，也不能把「很不开心」读成「开心」。"""
    assert read_weather(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "我不想活了，好难过",   # 危机
        "最近在吃药，我很难过",  # 敏感
    ],
)
def test_weather_refuses_crisis_and_sensitive_turns(text: str):
    """把一个正在说不想活的人做成一朵柔光，是把痛苦当装饰。"""
    assert read_weather(text) is None


def test_weather_takes_the_latest_word_because_it_is_about_now():
    """一句话常常从过去讲到现在，落在最后的那个词才是「此刻」。"""
    assert read_weather("早上很烦，现在平静一点") == "平静"


# ---- 短期，而不是序列 -------------------------------------------------------


async def test_terrain_surfaces_one_recent_weather_word(
    client: AsyncClient,
    test_settings: Settings,
):
    await _seed_weather(test_settings, word="累")
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"]["word"] == "累"


async def test_weather_disperses_after_its_ttl(
    client: AsyncClient,
    test_settings: Settings,
):
    """超过窗口就当作已经散掉——这是「默认短期」的落点。"""
    await _seed_weather(
        test_settings,
        word="累",
        age_hours=test_settings.terrain_weather_ttl_hours + 1,
    )
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"] is None


async def test_terrain_returns_at_most_one_word_never_a_series(
    client: AsyncClient,
    test_settings: Settings,
):
    """可累积的天气序列就是情绪统计：接口里连表达它的形状都不存在。"""
    await _seed_weather(test_settings, word="烦", age_hours=3)
    await _seed_weather(test_settings, word="平静", age_hours=1)
    body = (await client.get("/api/v1/terrain")).json()
    # 最近的那个词，且 weather 是对象而不是数组
    assert body["weather"]["word"] == "平静"
    assert not isinstance(body["weather"], list)
    assert "weather_history" not in body and "weather_series" not in body


async def test_weather_is_never_terrain_evidence(
    client: AsyncClient,
    test_settings: Settings,
):
    """天气是推断，证据必须是原话。用推断喂形成层，地形就长在猜测上（P4）。"""
    await _seed_weather(test_settings, word="累")
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"]["word"] == "累"
    # 天气不制造地貌，也不制造线索
    assert body["items"] == []
    assert body["candidates"] == []


async def test_assistant_turns_never_carry_weather(
    client: AsyncClient,
    test_settings: Settings,
):
    """天气读的是用户的状态，不是它自己的语气。"""
    await _seed_weather(test_settings, word="温暖", role="assistant")
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"] is None


# ---- 用户可控 ---------------------------------------------------------------


async def test_user_can_let_the_weather_disperse(
    client: AsyncClient,
    test_settings: Settings,
):
    await _seed_weather(test_settings, word="累")
    dispersed = await client.request("DELETE", "/api/v1/terrain/weather")
    assert dispersed.status_code == 200
    assert dispersed.json()["dispersed"] >= 1
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"] is None


async def test_dispersing_weather_keeps_the_utterance_it_was_read_from(
    client: AsyncClient,
    test_settings: Settings,
):
    """散掉的只是这次读法，不是用户说过的那句话。"""
    await _seed_weather(test_settings, word="累")
    await client.request("DELETE", "/api/v1/terrain/weather")
    async with get_db(read_only=True) as db:
        rows = (
            await db.execute(
                select(Working).where(Working.user_id == test_settings.default_user_id)
            )
        ).scalars().all()
    assert rows, "话轮本身不该被删掉"
    assert all(row.mood is None for row in rows)
    assert any("好累" in row.content for row in rows)


async def test_user_can_mute_weather_for_good(
    client: AsyncClient,
    test_settings: Settings,
):
    await client.request("DELETE", "/api/v1/terrain/weather", params={"mute": "true"})
    await _seed_weather(test_settings, word="平静")
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"] is None


async def test_paused_memory_also_pauses_weather(
    client: AsyncClient,
    test_settings: Settings,
):
    """暂停记忆的人不该在地形页上被读出一个状态词。"""
    async with get_db(read_only=False) as db:
        user = (
            await db.execute(
                select(User).where(User.user_id == test_settings.default_user_id)
            )
        ).scalar_one()
        user.settings_json = {**(user.settings_json or {}), "memory_paused": True}
    await _seed_weather(test_settings, word="累")
    body = (await client.get("/api/v1/terrain")).json()
    assert body["weather"] is None
