"""BM25 关键词检索（FTS5 + jieba 切词 + rank_bm25 重排兜底）。

MVP：如果 jieba 没装 / SQLite FTS5 没返回足够结果，直接用 jieba 分词 + rank_bm25 在内存里重排；
这样在 Windows/Linux 没装 sqlite FTS5 jieba 分词器时也能用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from sqlalchemy import text

log = logging.getLogger("habit_list.retrieval.bm25")

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


try:  # pragma: no cover - 可选依赖
    import jieba  # type: ignore

    def _cut(t: str) -> list[str]:
        return list(jieba.cut_for_search(t or ""))
except Exception:  # noqa: BLE001
    def _cut(t: str) -> list[str]:  # fallback: 2-gram
        s = t or ""
        if len(s) <= 2:
            return [s] if s else []
        return [s[i:i + 2] for i in range(len(s) - 1)]


@dataclass
class BM25Hit:
    episodic_id: str
    score: float
    snippet: str = ""


def _clean_term(t: str) -> str:
    return (t or "").strip().strip("'\"`;,")


async def search(
    session: "AsyncSession",
    user_id: str,
    query: str,
    topk: int = 12,
) -> list[BM25Hit]:
    """先用 SQLite FTS5 查；命中不够再回落到 rank_bm25 内存重排。"""
    terms = [t for t in _cut(query) if len(t) > 0]
    if not terms:
        return []

    # 1) FTS5
    fts_hits: list[BM25Hit] = []
    try:
        # 用 NEAR 语法提高命中率；兼容中文用 OR 连接
        q_str = " OR ".join(f'"{_clean_term(t)}"' for t in terms if _clean_term(t))
        if q_str:
            sql = text(
                f"""
                SELECT
                  f.episodic_id,
                  snippet(episodic_fts, 2, '◁', '▷', '…', 16) AS snip,
                  bm25(episodic_fts) AS score
                FROM episodic_fts f
                WHERE f.user_id = :uid
                  AND episodic_fts MATCH :q
                ORDER BY score
                LIMIT :lim
                """
            )
            rows = (await session.execute(sql, {"uid": user_id, "q": q_str, "lim": topk})).all()
            for r in rows:
                # bm25(fts) 越小越好；倒数转成 越大越好
                score = 1.0 / (float(r.score) + 1e-6)
                fts_hits.append(BM25Hit(episodic_id=r.episodic_id, score=score, snippet=(r.snip or "")))
    except Exception as exc:  # noqa: BLE001 - FTS5 异常回落
        log.warning("FTS5 失败，回落 rank_bm25: %s", exc)

    if fts_hits:
        return fts_hits[:topk]

    # 2) 回落 rank_bm25 内存扫 top 1000 条
    try:
        from rank_bm25 import BM25Okapi  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 依赖缺失
        log.warning("rank_bm25 不可用，BM25 检索返回空: %s", exc)
        return []
    rows = (await session.execute(
        text(
            """
            SELECT episodic_id, COALESCE(summary_1line,'') || ' ' || COALESCE(raw_user_text,'') AS txt
            FROM episodic
            WHERE user_id=:uid AND status='active'
            ORDER BY created_at DESC
            LIMIT 1000
            """
        ),
        {"uid": user_id},
    )).all()
    if not rows:
        return []
    corpus: list[list[str]] = [_cut(r.txt) for r in rows]
    bm = BM25Okapi(corpus)
    tokenized_query = _cut(query)
    scores = bm.get_scores(tokenized_query)
    paired = sorted(
        ((scores[i], rows[i].episodic_id, rows[i].txt) for i in range(len(rows))),
        reverse=True,
    )
    out = []
    for score, eid, txt in paired[:topk]:
        if score <= 0:
            break
        out.append(BM25Hit(episodic_id=eid, score=float(score), snippet=txt[:80]))
    return out
