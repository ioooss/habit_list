"""冲突检测：同用户同 category 的语义事实互相矛盾 → 标 pending_conflict 并写 insights 让用户确认。

简化规则（MVP 够用）：
1. category='习惯' 里出现 "不X" vs "X 很规律" → 冲突
2. 同一 fact_text 归一化后（去标点/空格/同义词）出现反向词（"不"/"没"/"很少"/"偶尔" vs "经常"/"每天"/"一直"）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.models import Insight, Semantic

log = logging.getLogger("habit_list.memory.conflict")

_NEG_WORDS = re.compile(r"不(?!错|同|要|会|能|妨)|没(?!有特别)|很少|偶尔|不常|不怎么")
_POS_WORDS = re.compile(r"每天|一直|经常|常常|总是|规律|雷打不动|坚持")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").replace("，", "").replace("。", "").replace(",", "")


def _is_contradict(a: str, b: str) -> bool:
    if _norm(a) == _norm(b):
        return False
    na, nb = _norm(a), _norm(b)
    # 简单同一句子有无否定词互斥
    if abs(len(na) - len(nb)) <= 4:
        has_neg_a = bool(_NEG_WORDS.search(na))
        has_neg_b = bool(_NEG_WORDS.search(nb))
        if has_neg_a != has_neg_b:
            # 去掉否定词后看是否高度重合
            stripped_a = _NEG_WORDS.sub("", na)
            stripped_b = _NEG_WORDS.sub("", nb)
            if len(stripped_a) >= 2 and stripped_a in stripped_b or stripped_b in stripped_a:
                return True
    if bool(_POS_WORDS.search(na)) != bool(_POS_WORDS.search(nb)):
        if len(na) >= 3 and na[-3:] == nb[-3:]:
            return True
    return False


@dataclass
class Conflict:
    a_id: str
    b_id: str
    category: str
    a_text: str
    b_text: str


async def detect_and_publish(settings: Settings | None = None) -> list[Conflict]:
    """跑一次全量冲突检测；把新冲突以 insights (type='矛盾·tension') 形式入 pending。"""
    settings = settings or get_settings()
    conflicts: list[Conflict] = []
    async with get_db(read_only=False) as db:
        rows = (await db.execute(
            select(Semantic)
            .where(Semantic.status.in_(["active", "pending_conflict"]))
            .order_by(Semantic.category, Semantic.created_at.desc())
        )).scalars().all()
        # 按 category 分桶对比
        buckets: dict[str, list[Semantic]] = {}
        for r in rows:
            buckets.setdefault(r.category, []).append(r)
        for cat, items in buckets.items():
            if len(items) < 2:
                continue
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a, b = items[i], items[j]
                    if _is_contradict(a.fact_text, b.fact_text):
                        conflicts.append(Conflict(
                            a_id=a.semantic_id, b_id=b.semantic_id,
                            category=cat, a_text=a.fact_text, b_text=b.fact_text,
                        ))
                        a.status = "pending_conflict"
                        b.status = "pending_conflict"
                        # 写 insight
                        exists = (await db.execute(
                            select(Insight).where(
                                Insight.user_id == a.user_id,
                                Insight.type == "矛盾·tension",
                                Insight.text_html.contains(a.fact_text[:12]),
                                Insight.text_html.contains(b.fact_text[:12]),
                            )
                        )).scalar_one_or_none()
                        if not exists:
                            db.add(Insight(
                                user_id=a.user_id,
                                type="矛盾·tension",
                                text_html=(
                                    f"<em>你说过</em>「{a.fact_text}」"
                                    f"，又说过「{b.fact_text}」<br>哪个才是现在的你？"
                                ),
                                meta="基于 " + cat + " 语义事实比对 · 信心度高",
                                confidence=0.92,
                                evidence_json={"a": a.semantic_id, "b": b.semantic_id},
                                status="pending",
                            ))
        if conflicts:
            log.info("conflicts published: %s", len(conflicts))
    return conflicts
