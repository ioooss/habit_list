"""向量语义检索（sqlite-vss，cosine similarity TopK）。"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import text

from ..core.config import Settings, get_settings
from ..providers import dashscope

log = logging.getLogger("habit_list.retrieval.vector")
_log_once = {"warned": False}

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class VecHit:
    episodic_id: str
    score: float  # cosine 相似度，越大越像


_EMB_DIM_FALLBACK = 1024  # 仅当没传 settings 时兜底；真实值读 settings.dashscope_embedding_dim


def _emb_dim(settings: Settings | None = None) -> int:
    if settings is not None:
        return int(settings.dashscope_embedding_dim or _EMB_DIM_FALLBACK)
    try:
        return int(get_settings().dashscope_embedding_dim or _EMB_DIM_FALLBACK)
    except Exception:  # noqa: BLE001
        return _EMB_DIM_FALLBACK


def _vector_to_blob(vec: list[float], dim: int | None = None) -> bytes:
    d = dim if dim is not None else _emb_dim()
    if len(vec) != d:
        if len(vec) > d:
            vec = vec[:d]
        else:
            vec = vec + [0.0] * (d - len(vec))
    return struct.pack(f"{d}f", *np.asarray(vec, dtype=np.float32))


async def ensure_embeddings_for(session: "AsyncSession", user_id: str, episodic_ids: list[str], settings: Settings | None = None) -> None:
    """对没向量的石子批量 dashscope embedding 并落库 episodic_vec。幂等。
    Windows/轻量环境没装 sqlite-vss（缺 vss0 module）时，episodic_vec 虚表没建成功，
    SELECT LEFT JOIN 会直接抛 OperationalError: no such table: episodic_vec。
    这里静默降级（只警告一次），保证 hybrid_retrieve / LLM 主链不受影响。"""
    settings = settings or get_settings()
    if session.get_bind().dialect.name != "sqlite":
        # PostgreSQL long-term vectors are owned by Memory V2/pgvector. Do not
        # spend model calls on the legacy sqlite-vss table path.
        return
    if not episodic_ids:
        return
    id_list = list(dict.fromkeys(episodic_ids))  # 去重保序
    placeholders = ", ".join(f":id_{i}" for i in range(len(id_list)))
    bind_params: dict = {"uid": user_id}
    for i, eid in enumerate(id_list):
        bind_params[f"id_{i}"] = eid
    try:
        rows = (await session.execute(
            text(
                f"""
                SELECT e.episodic_id, COALESCE(e.summary_1line,'') || ' ' || COALESCE(e.raw_user_text,'') AS txt
                FROM episodic e
                LEFT JOIN episodic_vec v ON v.episodic_id = e.episodic_id
                WHERE e.user_id=:uid AND e.status='active' AND e.episodic_id IN ({placeholders})
                  AND v.episodic_id IS NULL
                """
            ),
            bind_params,
        )).all()
    except Exception as e:  # noqa: BLE001 - 最常见是 no such table: episodic_vec
        if not _log_once["warned"]:
            log.warning("episodic_vec 未启用，跳过 embedding（sqlite-vss 未加载？）：%s", e)
            _log_once["warned"] = True
        return
    if not rows:
        return
    # 按 DashScope embedding 单次上限 25 条分批
    dim = _emb_dim(settings)
    for i in range(0, len(rows), 25):
        batch = rows[i:i + 25]
        texts = [r.txt for r in batch]
        embs = await dashscope.embed_texts(texts, dimensions=dim, settings=settings)
        # 写 episodic_vec 虚表（等价 INSERT OR IGNORE，虚表没主键就手动去重）
        for j, r in enumerate(batch):
            try:
                blob = _vector_to_blob(embs[j] if j < len(embs) else [0.0] * dim, dim=dim)
                await session.execute(
                    text(
                        """
                        INSERT INTO episodic_vec(episodic_id, embedding, user_id)
                        VALUES(:eid, :blob, :uid)
                        """
                    ),
                    {"eid": r.episodic_id, "blob": blob, "uid": user_id},
                )
            except Exception as exc:  # noqa: BLE001 - vss0 没加载
                log.debug("episodic_vec 写入跳过: %s", exc)
                return


async def search(
    session: "AsyncSession",
    user_id: str,
    query: str,
    topk: int = 12,
    settings: Settings | None = None,
) -> list[VecHit]:
    """1) dashscope embed query → 2) vss_topk episodic_vec cosine distance → 返回相似度"""
    settings = settings or get_settings()
    if session.get_bind().dialect.name != "sqlite":
        return []
    dim = _emb_dim(settings)
    if not settings.dashscope_api_key:
        log.info("DashScope key 未配置，跳过向量检索")
        return []
    q_emb = (await dashscope.embed_texts([query], dimensions=dim, settings=settings))
    if not q_emb:
        return []
    blob = _vector_to_blob(q_emb[0], dim=dim)
    try:
        rows = (await session.execute(
            text(
                f"""
                SELECT
                  episodic_id,
                  distance
                FROM vss_top_k('episodic_vec', {topk * 3}, :blob, 0.6)
                WHERE user_id=:uid
                ORDER BY distance ASC
                LIMIT :lim
                """
            ),
            {"blob": blob, "uid": user_id, "lim": topk},
        )).all()
    except Exception as exc:  # noqa: BLE001 - sqlite-vss 不可用
        log.warning("vector search failed (sqlite-vss not loaded?): %s", exc)
        return []
    out: list[VecHit] = []
    for r in rows:
        dist = float(r.distance)
        # cosine distance -> similarity: higher better
        sim = max(0.0, 1.0 - dist)
        if sim <= 0.01:
            continue
        out.append(VecHit(episodic_id=r.episodic_id, score=sim))
    return out
