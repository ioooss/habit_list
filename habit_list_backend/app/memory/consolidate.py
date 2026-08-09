"""睡眠巩固（System2）：每周日 03:00 跑一次。

职责：
1. 扫过去 7 天所有 RawLedger → user_utterance + ai_response_final，喂给 LLM（qwen-plus）提炼：
   - 画像事实（category：身份/习惯/偏好/关系/健康/创作/消费/周期）→ confidence ≥ AUTO_CONF 直接入 semantic；
     低于这个阈值但 ≥ MIN_CONF → 入 insights 等待用户确认。
2. 把明显的行为节奏（"冥想的那几天阅读也更多"）→ 进 insight (type=规律·rhythm / 关联·pattern / 趋势·drift)。
3. 跑完整个流程写一条 entry_type=system_sleep_consolidation 的 Ledger 作为审计日志。
4. 顺手：写 graph_nodes / graph_edges（person/location/activity/work → ep:XXX 石子），让 System1 的知识图谱能用。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.models import (
    Episodic,
    GraphEdge,
    GraphNode,
    Insight,
    RawLedger,
    Semantic,
    User,
)
from ..providers import dashscope
from ..retrieval import graph as graph_retrieval
from . import conflict as conflict_mod

log = logging.getLogger("habit_list.memory.consolidate")


CATEGORIES = ["身份", "习惯", "偏好", "关系", "健康", "创作", "作品", "消费", "周期"]
INSIGHT_TYPES = ["关联·pattern", "规律·rhythm", "矛盾·tension", "趋势·drift"]

_EXTRACT_PROMPT_ZH = """你是「陪伴记忆分析师」。基于用户近一周的原话：
1. 提取画像事实（category 只能选：{cats}），给 0~1 confidence，fact_text 不要太长，不要带主观推断；
2. 提取关系/模式/节奏/变化，生成 insight（type 只能选：{ins}），给 0~1 confidence；
3. 从每条记录里抽可能的实体（人/地点/活动/作品/消费物，最多 5 项 per record）。
4. 只能把用户原话作为事实证据；不得使用或猜测 AI 回复、系统提示和安全模板。
严格输出 JSON：
{{
  "semantics": [{{"category":"习惯", "fact_text":"…", "confidence": 0.92, "support_episodic_ids": ["…","…"]}}],
  "insights": [{{"type":"关联·pattern", "text_html":"…", "meta":"基于 X 周数据 · 信心度中", "confidence": 0.76, "evidence_ids": ["…"]}}],
  "entities": [{{"episodic_id":"…", "entities":[{{"type":"人物","name":"妈"}}] }}]
}}
只输出 JSON，不要解释、不要 Markdown 代码块。
""".format(cats="/".join(CATEGORIES), ins="/".join(INSIGHT_TYPES))


class _SemanticCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    fact_text: str = Field(min_length=2, max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)
    support_episodic_ids: list[str] = Field(default_factory=list, max_length=30)


class _InsightCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    text_html: str = Field(min_length=4, max_length=1000)
    meta: str = Field(default="", max_length=120)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class _EntityCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    name: str = Field(min_length=1, max_length=128)


class _EpisodeEntities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episodic_id: str
    entities: list[_EntityCandidate] = Field(default_factory=list, max_length=5)


class _ConsolidationExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantics: list[_SemanticCandidate] = Field(default_factory=list, max_length=30)
    insights: list[_InsightCandidate] = Field(default_factory=list, max_length=30)
    entities: list[_EpisodeEntities] = Field(default_factory=list, max_length=100)


def _past_window_iso(days: int = 7) -> tuple[str, str]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


async def _fetch_window_episodic(session, user_id: str, start_iso: str, end_iso: str):
    rows = (await session.execute(
        select(Episodic)
        .where(
            Episodic.user_id == user_id,
            Episodic.created_at >= start_iso,
            Episodic.created_at <= end_iso,
            Episodic.status == "active",
        )
        .order_by(Episodic.created_at.asc())
    )).scalars().all()
    return list(rows)


def _build_extract_user_msg(episodes) -> str:
    parts = []
    for ep in episodes:
        parts.append(
            f"- [{ep.episodic_id}] {ep.created_at} kind={ep.kind}\n"
            f"  用户说：{(ep.raw_user_text or '').strip()}"
        )
    return "\n".join(parts) or "（本周没记录）"


async def _call_llm_extract(msg: str, settings: Settings) -> dict:
    if not settings.dashscope_api_key:
        log.warning("no DashScope key, sleep consolidation empty result")
        return {"semantics": [], "insights": [], "entities": []}
    try:
        payload = await dashscope.chat_json(
            [
                {"role": "system", "content": _EXTRACT_PROMPT_ZH},
                {"role": "user", "content": msg},
            ],
            json_schema=_ConsolidationExtraction.model_json_schema(),
            schema_name="weekly_memory_consolidation_v1",
            temperature=0.2,
            max_tokens=1800,
            settings=settings,
        )
        return _ConsolidationExtraction.model_validate(payload).model_dump()
    except Exception as exc:  # noqa: BLE001
        log.warning("sleep consolidation failed error_type=%s", type(exc).__name__)
        return {"semantics": [], "insights": [], "entities": []}


def _norm_fact(t: str) -> str:
    return re.sub(r"\s+", "", t or "")[:120]


@dataclass
class ConsolidationResult:
    semantic_new: int = 0
    semantic_auto: int = 0
    insight_new: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    conflicts: int = 0


async def consolidate_weekly(settings: Settings | None = None, user_id_override: Optional[str] = None) -> ConsolidationResult:
    """每周日 03:00 APScheduler 触发。"""
    settings = settings or get_settings()
    auto_th = settings.system2_auto_confirm_conf
    min_th = settings.system2_insight_conf_min
    res = ConsolidationResult()
    async with get_db(read_only=False) as db:
        # 单用户 MVP，直接 settings.default_user_id；后面多租户改 user_id_override
        user_id = user_id_override or settings.default_user_id
        # 用户暂停了记忆形成：本周不做任何自动沉淀
        user = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        if user is not None and (user.settings_json or {}).get("memory_paused"):
            log.info("consolidate skipped: memory_paused user=%s", user_id)
            return res
        start_iso, end_iso = _past_window_iso(days=7)
        episodes = await _fetch_window_episodic(db, user_id, start_iso, end_iso)
        if not episodes:
            log.info("sleep consolidate: no episodes this week")
            return res
        msg = _build_extract_user_msg(episodes)
        data = await _call_llm_extract(msg, settings)

        # 1) Semantics
        seen_facts: set[str] = {
            _norm_fact(r.fact_text)
            for r in (await db.execute(select(Semantic.fact_text).where(Semantic.user_id == user_id))).all()
        }
        for s in data.get("semantics") or []:
            try:
                fact = str(s.get("fact_text") or "").strip()
                cat = s.get("category") or "偏好"
                if cat not in CATEGORIES:
                    cat = "偏好"
                conf = float(s.get("confidence") or 0.5)
                if not fact or len(fact) < 2:
                    continue
                nkey = _norm_fact(fact)
                if nkey in seen_facts:
                    continue
                seen_facts.add(nkey)
                support = list(s.get("support_episodic_ids") or [])
                if conf >= auto_th:
                    sem = Semantic(
                        user_id=user_id,
                        category=cat,
                        fact_text=fact,
                        source_kind="consolidation_automatic",
                        confidence=conf,
                        evidence_count=len(support),
                        tags_json=["consolidation_auto"],
                        status="active",
                    )
                    db.add(sem)
                    res.semantic_new += 1
                    res.semantic_auto += 1
                elif conf >= min_th:
                    db.add(Insight(
                        user_id=user_id,
                        type="趋势·drift" if cat == "周期" else "关联·pattern",
                        text_html=f"[新增画像 · {cat}] <em>{fact}</em>",
                        meta=f"基于最近 {len(support)} 条记录 · 信心度 {'高' if conf>0.8 else '中'}",
                        confidence=conf,
                        evidence_json={"episodic_ids": support},
                        status="pending",
                    ))
                    res.insight_new += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("semantic insert skip: %s", exc)

        # 2) Insights
        for ins in data.get("insights") or []:
            try:
                itype = ins.get("type") or "关联·pattern"
                if itype not in INSIGHT_TYPES:
                    itype = "关联·pattern"
                html = str(ins.get("text_html") or "").strip()
                if not html or len(html) < 4:
                    continue
                conf = float(ins.get("confidence") or 0.5)
                if conf < min_th:
                    continue
                ev_ids = list(ins.get("evidence_ids") or [])
                meta = str(ins.get("meta") or "")[:120] or f"基于本周 {len(ev_ids)} 条记录"
                db.add(Insight(
                    user_id=user_id, type=itype, text_html=html, meta=meta,
                    confidence=conf, evidence_json={"episodic_ids": ev_ids}, status="pending",
                ))
                res.insight_new += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("insight insert skip: %s", exc)

        # 3) Entities → graph_nodes / graph_edges（node_id 按类型归一化防重复）
        existing_nodes: set[tuple[str, str]] = {
            (r.user_id, r.node_id)
            for r in (await db.execute(select(GraphNode.user_id, GraphNode.node_id))).all()
        }
        existing_edges: set[tuple[str, str, str]] = {
            (r.user_id, r.src, r.dst)
            for r in (await db.execute(select(GraphEdge.user_id, GraphEdge.src, GraphEdge.dst))).all()
        }
        TYPE_NORM = {"人物": "person", "地点": "loc", "活动": "act", "作品": "work", "消费物": "item"}
        entity_eps_map: dict[tuple[str, str], set[str]] = defaultdict(set)
        for e in data.get("entities") or []:
            eid = e.get("episodic_id")
            for ent in e.get("entities") or []:
                t = TYPE_NORM.get(ent.get("type") or "？") or "misc"
                name = str(ent.get("name") or "").strip()
                if not name or not eid:
                    continue
                norm = re.sub(r"\s+", "", name)
                key = (t, norm)
                entity_eps_map[key].add(eid)
        for (t, norm), ep_ids in entity_eps_map.items():
            # 实体节点
            node_id = f"{t}:{norm}"
            if (user_id, node_id) not in existing_nodes:
                db.add(GraphNode(
                    node_id=node_id, user_id=user_id,
                    node_type=t, node_name=norm, entity_norm_name=norm,
                ))
                existing_nodes.add((user_id, node_id))
                res.graph_nodes += 1
            for ep_id in ep_ids:
                ep_node = f"ep:{ep_id}"
                if (user_id, ep_node) not in existing_nodes:
                    db.add(GraphNode(
                        node_id=ep_node, user_id=user_id,
                        node_type="ep", node_name=ep_id, entity_norm_name=ep_id,
                    ))
                    existing_nodes.add((user_id, ep_node))
                    res.graph_nodes += 1
                for a, b in [(node_id, ep_node), (ep_node, node_id)]:  # 双向
                    if (user_id, a, b) not in existing_edges:
                        db.add(GraphEdge(user_id=user_id, src=a, dst=b, weight=1.0))
                        existing_edges.add((user_id, a, b))
                        res.graph_edges += 1

        # 4) 冲突检测
        conflicts = await conflict_mod.detect_and_publish(settings)
        res.conflicts = len(conflicts)

        # 5) 知识图谱缓存失效：下次 graph.search 重建
        graph_retrieval.bump_graph_version()

        # 6) Ledger 审计
        db.add(RawLedger(
            user_id=user_id,
            entry_type="system_sleep_consolidation",
            payload_json={
                "window_start": start_iso, "window_end": end_iso,
                "episodes": len(episodes),
                "semantic_new": res.semantic_new, "semantic_auto": res.semantic_auto,
                "insight_new": res.insight_new,
                "graph_nodes": res.graph_nodes, "graph_edges": res.graph_edges,
                "conflicts": res.conflicts,
            },
        ))

    log.info("sleep consolidation done: %s", res)
    return res
