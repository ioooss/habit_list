"""Hybrid RRF 融合：三路检索 → RRF(k=60) 归一化 → 按 retrieval_weight(艾宾浩斯) 再加权 → TopN。"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..db.models import Episodic
from . import bm25, graph, vector

log = logging.getLogger("habit_list.retrieval.hybrid")

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


RRF_K = 60

# 三路权重（可后面由 Procedural 参数调）
W_BM25 = 1.0
W_VEC = 1.0
W_GRAPH = 0.9

# 艾宾浩斯加成因子：weight=0.3 的石子 → 总分 * (0.7 + 0.3 * retrieval_weight) = 0.79；既不消失也会被顶下去点
FORGET_BIAS = 0.7

# 单路检索超时（秒）。DashScope Embedding 偶尔慢，避免拖慢整个 SSE 首字延迟
_RETRIEVE_ONE_TIMEOUT = 6.0


@dataclass
class RetrievalResult:
    episodic_id: str
    rrf_score: float
    final_score: float
    retrieval_weight: float
    snippet: str
    sources: list[str]  # 哪些检索命中过：bm25/vector/graph


async def hybrid_retrieve(
    session: "AsyncSession",
    user_id: str,
    query: str,
    topk: int | None = None,
    settings: Settings | None = None,
) -> list[RetrievalResult]:
    settings = settings or get_settings()
    topk = topk or settings.system1_n_retrieval_topk

    # 先把「新生成的 30 颗以内石子」向量化一次，避免召回冷启动时 vector 路为空
    new_ids_rows = (await session.execute(
        select(Episodic.episodic_id)
        .where(Episodic.user_id == user_id, Episodic.status == "active")
        .order_by(Episodic.created_at.desc())
        .limit(30)
    )).all()
    if new_ids_rows:
        try:
            await asyncio.wait_for(
                vector.ensure_embeddings_for(
                    session, user_id, [r.episodic_id for r in new_ids_rows], settings=settings
                ),
                timeout=_RETRIEVE_ONE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("ensure_embeddings_for timed out in %.1fs, skipped", _RETRIEVE_ONE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            log.warning("ensure_embeddings_for failed: %s", exc)

    # 三路并行？为了简单稳，按顺序来（SQLite 并发写不好，这里都是读）
    # 但每一路都给超时：DashScope Embedding 偶发慢，超时就空结果，避免 SSE 等太久
    async def _safe(name, coro):
        try:
            return await asyncio.wait_for(coro, timeout=_RETRIEVE_ONE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("hybrid_retrieve %s timed out in %.1fs, skipped", name, _RETRIEVE_ONE_TIMEOUT)
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning("hybrid_retrieve %s failed: %s", name, exc)
            return []

    b_hits = await _safe("bm25", bm25.search(session, user_id, query, topk=topk * 3))
    v_hits = await _safe("vector", vector.search(session, user_id, query, topk=topk * 3, settings=settings))
    g_hits = await _safe("graph", graph.search(session, user_id, query, topk=topk * 3))

    ranks_per_source: dict[str, dict[str, int]] = {
        "bm25": {h.episodic_id: i + 1 for i, h in enumerate(b_hits)},
        "vector": {h.episodic_id: i + 1 for i, h in enumerate(v_hits)},
        "graph": {h.episodic_id: i + 1 for i, h in enumerate(g_hits)},
    }

    snippets = {}
    for h in b_hits:
        if h.snippet:
            snippets[h.episodic_id] = h.snippet

    all_ids: set[str] = set()
    for d in ranks_per_source.values():
        all_ids.update(d.keys())
    if not all_ids:
        return []

    rrf_scores: dict[str, float] = defaultdict(float)
    sources_per_id: dict[str, list[str]] = defaultdict(list)
    for src, ranks in ranks_per_source.items():
        w = {"bm25": W_BM25, "vector": W_VEC, "graph": W_GRAPH}[src]
        for eid, r in ranks.items():
            rrf_scores[eid] += w * (1.0 / (RRF_K + r))
            sources_per_id[eid].append(src)

    # 拉 retrieval_weight（艾宾浩斯）
    weight_map: dict[str, float] = {}
    rows = (await session.execute(
        select(Episodic.episodic_id, Episodic.retrieval_weight, Episodic.summary_1line, Episodic.raw_user_text)
        .where(Episodic.episodic_id.in_(tuple(all_ids)))
    )).all()
    for r in rows:
        weight_map[r.episodic_id] = float(r.retrieval_weight or 1.0)
        if r.episodic_id not in snippets:
            snip = (r.summary_1line or r.raw_user_text or "")[:80]
            if snip:
                snippets[r.episodic_id] = snip

    results = []
    for eid, score in rrf_scores.items():
        rw = weight_map.get(eid, 1.0)
        final = score * (FORGET_BIAS + (1.0 - FORGET_BIAS) * rw)
        results.append(
            RetrievalResult(
                episodic_id=eid,
                rrf_score=score,
                final_score=final,
                retrieval_weight=rw,
                snippet=snippets.get(eid, ""),
                sources=sources_per_id.get(eid, []),
            )
        )
    results.sort(key=lambda r: r.final_score, reverse=True)
    out = results[:topk]
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "hybrid retrieve topN=%s bm25=%s vec=%s graph=%s merged=%s",
            len(out), len(b_hits), len(v_hits), len(g_hits), len(all_ids),
        )
    return out
