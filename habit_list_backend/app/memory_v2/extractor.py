"""Memory atom extraction from user-authored text only."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..core.config import Settings, get_settings
from ..providers import dashscope
from .domain import (
    ClaimType,
    MemoryAtom,
    MemoryCategory,
    MemoryExtraction,
    Sensitivity,
    SourceType,
)

log = logging.getLogger("habit_list.memory_v2.extractor")


_CRISIS_KEYWORDS = re.compile(
    r"自杀|自残|轻生|寻死|想死|不想(?:继续|再)?活|活不下去|"
    r"结束(?:自己|生命)|伤害自己|割腕|跳楼|服药自杀|"
    r"(?:kill|hurt)\s+myself|self[- ]?harm|end\s+my\s+life|"
    r"(?:do\s+not|don't)\s+want\s+to\s+live|can't\s+(?:go|keep)\s+on",
    re.IGNORECASE,
)
_SENSITIVE_KEYWORDS = re.compile(
    r"病|诊断|药|抑郁|焦虑症|工资|收入|负债|欠款|"
    r"住址|地址|身份证|银行卡|宗教|政治|性取向|性生活|" + _CRISIS_KEYWORDS.pattern
)


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    category: MemoryCategory
    predicate: str
    claim_prefix: str
    claim_type: ClaimType = ClaimType.SEMANTIC
    importance: float = 0.55


_RULES: tuple[_Rule, ...] = (
    _Rule(
        re.compile(r"我(?:一直|很|挺|比较|特别)?喜欢(?P<value>[^，。！？；\n]{1,80})"),
        MemoryCategory.PREFERENCE,
        "likes",
        "喜欢",
    ),
    _Rule(
        re.compile(r"我(?:一直|很|挺|比较|特别)?不(?:太)?喜欢(?P<value>[^，。！？；\n]{1,80})"),
        MemoryCategory.PREFERENCE,
        "dislikes",
        "不喜欢",
    ),
    _Rule(
        re.compile(r"我叫(?P<value>[\u4e00-\u9fffA-Za-z·]{1,30})"),
        MemoryCategory.IDENTITY,
        "name",
        "名字是",
        importance=0.8,
    ),
    _Rule(
        re.compile(r"(?:我的目标是|我打算|我准备)(?P<value>[^，。！？；\n]{2,120})"),
        MemoryCategory.GOAL,
        "current_goal",
        "当前目标是",
        importance=0.7,
    ),
    _Rule(
        re.compile(r"(?:以后|接下来)?(?:请你|希望你|你可以)(?P<value>[^，。！？；\n]{2,120})"),
        MemoryCategory.INTERACTION_PREFERENCE,
        "assistant_behavior",
        "希望陪伴者",
        claim_type=ClaimType.PROCEDURAL,
        importance=0.75,
    ),
    _Rule(
        re.compile(r"我(?P<frequency>每天|每晚|每周|经常|通常|习惯)(?P<value>[^，。！？；\n]{1,100})"),
        MemoryCategory.HABIT,
        "routine",
        "习惯",
        importance=0.65,
    ),
)


_EXTRACTION_SYSTEM_PROMPT = """你是“内在地形”的记忆提取器，只处理本条用户原话。

任务：提取未来跨会话确实有帮助的稳定事实、偏好、目标、习惯和互动偏好。

硬规则：
1. 只能使用用户原话，不能推断诊断、人格标签或未明确表达的结论。
2. evidence_text 必须是输入中的连续原文；evidence_start/end 必须是 Python 字符索引，end 为开区间。
3. 一次情绪、寒暄、泛泛感受通常不写长期记忆；没有合格内容就返回 {"atoms":[]}。
4. 健康、关系、财务、位置、宗教、政治、性相关内容标为 sensitive，危机内容标为 crisis。
5. 用户明确说出的内容 source_type=user_explicit；跨事件推断不在本任务执行。
6. claim_text 使用中性、简短、可展示的中文，不使用“你总是”“你就是”等绝对化措辞。
"""


def infer_sensitivity(text: str, category: MemoryCategory) -> Sensitivity:
    if _CRISIS_KEYWORDS.search(text):
        return Sensitivity.CRISIS
    if category in {
        MemoryCategory.HEALTH,
        MemoryCategory.RELATIONSHIP,
        MemoryCategory.FINANCE,
        MemoryCategory.LOCATION,
    } or _SENSITIVE_KEYWORDS.search(text):
        return Sensitivity.SENSITIVE
    return Sensitivity.NORMAL


def _clean_value(value: str) -> str:
    return value.strip(" ，。！？；、\t\r\n")[:500]


def extract_rule_based(user_text: str, *, occurred_at: str) -> MemoryExtraction:
    """Deterministic zero-cost extraction for explicit, low-ambiguity statements."""

    atoms: list[MemoryAtom] = []
    seen: set[tuple[str, str, str]] = set()
    for rule in _RULES:
        for match in rule.pattern.finditer(user_text):
            value = _clean_value(match.group("value"))
            if not value:
                continue
            key = (rule.category.value, rule.predicate, value.casefold())
            if key in seen:
                continue
            seen.add(key)
            evidence = match.group(0)
            sensitivity = infer_sensitivity(evidence, rule.category)
            atoms.append(
                MemoryAtom(
                    claim_type=rule.claim_type,
                    category=rule.category,
                    subject="self",
                    predicate=rule.predicate,
                    object_value=value,
                    claim_text=f"{rule.claim_prefix}{value}",
                    source_type=SourceType.USER_EXPLICIT,
                    confidence=0.9,
                    sensitivity=sensitivity,
                    valid_from=occurred_at,
                    importance=rule.importance,
                    evidence_text=evidence,
                    evidence_start=match.start(),
                    evidence_end=match.end(),
                )
            )
    return MemoryExtraction(atoms=atoms)


async def extract_with_llm(
    user_text: str,
    *,
    occurred_at: str,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> MemoryExtraction:
    settings = settings or get_settings()
    payload = await dashscope.chat_json(
        [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"occurred_at={occurred_at}\n用户原话：\n{user_text}",
            },
        ],
        json_schema=MemoryExtraction.model_json_schema(),
        schema_name="memory_extraction_v1",
        temperature=0.0,
        max_tokens=1400,
        request_id=request_id,
        settings=settings,
    )
    return MemoryExtraction.model_validate(payload)


def _atom_key(atom: MemoryAtom) -> tuple[str, str, str, str]:
    return (
        atom.category.value,
        atom.subject.casefold(),
        atom.predicate.casefold(),
        atom.object_value.casefold(),
    )


def _merge_extractions(primary: MemoryExtraction, fallback: MemoryExtraction) -> MemoryExtraction:
    atoms = list(primary.atoms)
    seen = {_atom_key(atom) for atom in atoms}
    for atom in fallback.atoms:
        if _atom_key(atom) not in seen:
            atoms.append(atom)
            seen.add(_atom_key(atom))
    return MemoryExtraction(atoms=atoms[:12])


async def extract_memory_atoms(
    user_text: str,
    *,
    occurred_at: str,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> MemoryExtraction:
    """Dispatch extraction according to the configured cost/reliability mode."""

    settings = settings or get_settings()
    rules = extract_rule_based(user_text, occurred_at=occurred_at)
    mode = settings.memory_v2_extractor_mode
    if mode == "rules" or not settings.dashscope_api_key:
        return rules
    try:
        llm = await extract_with_llm(
            user_text,
            occurred_at=occurred_at,
            request_id=request_id,
            settings=settings,
        )
    except Exception:
        if mode == "llm":
            raise
        log.exception("Memory V2 LLM extraction failed; falling back to grounded rules")
        return rules
    return _merge_extractions(llm, rules)


__all__ = ["extract_memory_atoms", "extract_rule_based", "extract_with_llm", "infer_sensitivity"]
