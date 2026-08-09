"""情境：现在几点、上次说话过了多久。

这两件事注入的是 prompt 的**情境**部分，不是 ``SYSTEM_PROMPT``。人格不随时间变，
情境每一轮都变；混在一起，人格就会被今晚几点污染（声音基线 §4.2）。

两条都是「知道」而不是「做点什么」：

- **时段只改语气与长度，不改可用性。** 深夜不屏蔽任何功能、不推内容、不问候，
  更不劝睡——「早点睡吧」是把陪伴换成管理（§4.2 禁止清单）。
- **间隔在 14 天以内一个字都不提。** 三天不说话是正常生活，不是事件。
  超过 14 天才允许中性地承认**一次**，因为一个消失过的人自己知道他走了，
  装作没发现是一种更刺眼的不在场；但「你好久没来了」是指责，
  「我一直在等你」是绑架，「你还好吗？」三条硬规则全犯（§5.1）。

间隔不从 ``User`` 上的一个 ``last_chat_at`` 字段读：上次说话不是一份需要维护的状态，
它就是最后一条话轮的时间。多存一个字段，就多一个会和账本不一致的地方。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("habit_list.memory.situation")

DayBand = Literal["night", "day", "evening"]
AbsenceBand = Literal["continuous", "short", "long", "distant"]

FALLBACK_TIMEZONE = "Asia/Shanghai"

NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 5
EVENING_START_HOUR = 18

SHORT_ABSENCE_DAYS = 3
LONG_ABSENCE_DAYS = 14
DISTANT_ABSENCE_DAYS = 60


def _parse_iso(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("situation: 无法解析时间戳 %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_hour(now: datetime, timezone_name: str | None) -> int:
    """用户本地时刻的小时数。

    时区来自 ``User.timezone``，用户可以改（``PATCH /me``）。接口那头会校验，
    但历史行里可能已经躺着一个不存在的时区名——那种情况下退回默认时区，
    因为「不知道现在几点」比「因为时区串坏了而整轮报错」代价大得多。
    """

    name = (timezone_name or "").strip() or FALLBACK_TIMEZONE
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("situation: 未知时区 %r，退回 %s", name, FALLBACK_TIMEZONE)
        tz = ZoneInfo(FALLBACK_TIMEZONE)
    return now.astimezone(tz).hour


def is_known_timezone(name: str | None) -> bool:
    """接口层用它把认不出的时区名挡在库外，免得以后不知道现在几点。"""

    try:
        ZoneInfo((name or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def resolve_day_band(hour: int) -> DayBand:
    if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR:
        return "night"
    if hour >= EVENING_START_HOUR:
        return "evening"
    return "day"


def absence_days(now: datetime, last_spoke_at: str | None) -> Optional[float]:
    """距上次说话过了多少天；没有上一次就返回 None（第一次说话不是间隔）。"""

    previous = _parse_iso(last_spoke_at)
    if previous is None:
        return None
    gap = (now - previous).total_seconds() / 86400.0
    return gap if gap > 0 else 0.0


def resolve_absence_band(days: float | None) -> AbsenceBand:
    if days is None or days < SHORT_ABSENCE_DAYS:
        return "continuous"
    if days < LONG_ABSENCE_DAYS:
        return "short"
    if days <= DISTANT_ABSENCE_DAYS:
        return "long"
    return "distant"


# 深夜档只收紧语气与长度。它不含任何「等到白天再说」的建议，因为深夜的危机响应
# 必须整条送到（§4.3）——把「明天再看看」写进人格，危机路径就会带着它一起下沉。
_DAY_BAND_LINES: dict[DayBand, str] = {
    "night": (
        "现在是用户本地的深夜。更短、更低，允许只回一句。"
        "不引出新话题、不提计划、不提「明天」。"
        "不要劝他睡觉——「早点睡吧」是把陪伴换成管理。"
    ),
    "evening": "现在是用户本地的傍晚。默认声线，可以略长一点。",
    "day": "现在是用户本地的白天。默认声线。",
}

# 连续与短别不出现在 prompt 里：与其叮嘱它「别提间隔」（那等于先把间隔说给它听），
# 不如让它根本不知道有间隔。三天不说话本来就不是一件需要被提起的事。
_ABSENCE_BAND_LINES: dict[AbsenceBand, str] = {
    "long": (
        "距上次说话过了一段时间。你可以用中性的说法承认这段间隔，"
        "最多一次（例如「有一段时间了。」），然后立刻回到用户这句话上。"
        "不要问他去了哪里、为什么、还好吗；"
        "不要说「你好久没来了」「我一直在等你」；不要表达等待或期待。"
    ),
    "distant": (
        "距上次说话过了很久。你可以用中性的说法承认这段间隔，最多一次，"
        "然后立刻回到用户这句话上。不要问他去了哪里、为什么、还好吗；"
        "不要说「你好久没来了」「我一直在等你」；不要表达等待或期待。"
        "另外：隔了这么久，你记得的事可能已经不是他现在的样子了——"
        "不要主动引用超过一条旧记录；等他自己重新说起，你再接上。"
    ),
}


def situation_to_prompt(day_band: DayBand, absence_band: AbsenceBand) -> str:
    lines = [_DAY_BAND_LINES[day_band]]
    absence_line = _ABSENCE_BAND_LINES.get(absence_band)
    if absence_line:
        lines.append(absence_line)
    return "【此刻的情境】\n" + "\n".join(f"· {line}" for line in lines)


__all__ = [
    "AbsenceBand",
    "DISTANT_ABSENCE_DAYS",
    "DayBand",
    "EVENING_START_HOUR",
    "FALLBACK_TIMEZONE",
    "LONG_ABSENCE_DAYS",
    "NIGHT_END_HOUR",
    "NIGHT_START_HOUR",
    "SHORT_ABSENCE_DAYS",
    "absence_days",
    "is_known_timezone",
    "local_hour",
    "resolve_absence_band",
    "resolve_day_band",
    "situation_to_prompt",
]
