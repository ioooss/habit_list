"""此刻天气：从用户自己的话里读出当下的一个词。

设计上这个模块拒绝三件事，每一件都对应基线里的一条硬约束：

1. **不推断，只回声。** 天气只会是用户**自己写下的那个词**（「好累」→ 累）。
   模型可以造一个更漂亮的词，但那就变成了「它替你定义你此刻是谁」。
   读不出来就没有天气——地形页的天气槽位因此默认是空的，这是正确结果，
   不是失败（视觉规范 §4：不给它编一个词）。

2. **不累积成序列。** 天气写在 ``Working``（第 1 层会话记忆）上，
   随会话过期，永远不进 ``MemoryClaim``、永远不是地形证据。
   基线 §11 把「此刻天气」归到 Working 层并要求「限制长期化」；
   一条可累积的天气序列就是情绪统计，那既违反这一条，
   也违反 P4「证据先于结论」——用推断去喂形成层，
   地形就会长在模型自己的猜测上，而不是长在用户真的说过的话上。
   会累积的东西是天气**底下**的那句原话，它本来就已经在走证据通路了。

3. **危机与敏感话轮不读天气。** 把一个正在说不想活的人标成一朵「难过」的柔光，
   是把痛苦做成装饰。这一层在函数内部拦，不交给调用方记得拦。
"""
from __future__ import annotations

import re

from ..memory_v2.domain import MemoryCategory, Sensitivity
from ..memory_v2.extractor import infer_sensitivity

# 用户真的会打出来的状态词。只收 1–3 字、能独立成「一个词」的，
# 因为 §4 规定天气只有当下一个词。
_STATE_WORDS = (
    "疲惫", "烦躁", "焦虑", "紧张", "难过", "伤心", "委屈", "生气", "孤独",
    "麻木", "沉重", "混乱", "迷茫", "无力", "不安", "害怕", "愧疚", "后悔",
    "平静", "安静", "轻松", "放松", "开心", "高兴", "快乐", "满足", "踏实",
    "期待", "兴奋", "感动", "温暖", "清醒", "舒服", "释然", "自在", "踏实",
    "累", "困", "烦", "慌", "空", "乱", "丧", "糟", "爽",
)
# 长词优先，否则「疲惫」会先被「累」之外的短词切碎。
_WORDS_RE = "|".join(sorted(set(_STATE_WORDS), key=len, reverse=True))

# 必须带程度副词、「现在…」或「我…了」的框，裸词不算。
# 中文里子串匹配太容易误伤（「空」会命中「空调」，「重」会命中「重要」），
# 而「好累」「有点烦」「我累了」恰好就是人写当下状态的方式，精度足够高。
# 程度副词与词之间只允许空白：这样「很不开心」「有点不舒服」都读不出词，
# 宁可读不到，也不能把否定读成肯定。
_FRAMED = re.compile(
    r"(?:好|很|太|挺|超|特别|非常|有点|有些|比较|越来越|真的?|实在)\s*"
    rf"(?P<word>{_WORDS_RE})"
    rf"|(?:现在|此刻|这会儿|眼下)\s*(?:感觉|觉得)?\s*"
    rf"(?:有点|有些|很|好|挺)?\s*(?P<word3>{_WORDS_RE})"
    rf"|我\s*(?:感觉|觉得)?\s*(?P<word2>{_WORDS_RE})\s*了"
)

MAX_WEATHER_LEN = 8


def read_weather(text: str) -> str | None:
    """回声用户此刻的一个状态词；读不出来就返回 None。

    取**最后**一个匹配：一句话往往是从过去讲到现在（「早上很烦，现在好一点」），
    落在最后的那个词才是「此刻」。
    """

    if not text or not text.strip():
        return None
    if infer_sensitivity(text, MemoryCategory.OTHER) is not Sensitivity.NORMAL:
        return None
    word = None
    for match in _FRAMED.finditer(text):
        word = match.group("word") or match.group("word2") or match.group("word3")
    if word is None or len(word) > MAX_WEATHER_LEN:
        return None
    return word


__all__ = ["MAX_WEATHER_LEN", "read_weather"]
