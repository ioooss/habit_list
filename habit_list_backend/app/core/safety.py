"""Crisis detection and response: the one place safety copy is allowed to live.

There used to be two crisis responses in this codebase — one for 生活页 fragments and
one buried in the chat fallback path — and they had already drifted: the fragment one
never gave an emergency number.  A safety string that exists twice is a safety string
that will be updated once.

声音基线 §6：危机响应允许违反声音基线其余全部规则（它必须提问以评估当下危险、必须给
建议、可以长），因为那些规则保护的是「用户是主角」，而危机时的主角是这个人还活着。
§3.3：它在任何降级档位下都必须送到，因为它不依赖模型——规则命中、内容固定。
"""

from __future__ import annotations

import re

CRISIS_PATTERN = re.compile(
    r"自杀|自残|轻生|寻死|想死|不想(?:继续|再)?活|活不下去|"
    r"结束(?:自己|生命)|伤害自己|割腕|跳楼|服药自杀|"
    r"(?:kill|hurt)\s+myself|self[- ]?harm|end\s+my\s+life|"
    r"(?:do\s+not|don't)\s+want\s+to\s+live|can't\s+(?:go|keep)\s+on",
    re.IGNORECASE,
)

# 三件事必须都在：可立即拨打的渠道、一个能到身边的人、一个评估当下危险的问句。
CRISIS_RESPONSE = (
    "我看见这句话很重。若你此刻有伤害自己的风险，请立即联系当地急救："
    "在中国大陆可拨 120 或 110；同时找一个能马上来到你身边的人，先不要独自承受。"
    "你现在身边有可以伤害自己的东西吗？"
)


def is_crisis_text(text: str | None) -> bool:
    """Rule-based crisis classifier, deliberately independent of any model call."""

    return bool(CRISIS_PATTERN.search(text or ""))


__all__ = ["CRISIS_PATTERN", "CRISIS_RESPONSE", "is_crisis_text"]
