"""SQLite-only local search structures.

Production relational schema changes are owned exclusively by Alembic.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..core.config import Settings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger("habit_list")


async def apply(conn: "AsyncConnection", settings: Settings) -> None:
    if conn.dialect.name != "sqlite":
        raise RuntimeError("SQLite feature bootstrap called for a non-SQLite database")

    # 1) FTS5 虚表：episodic（原文 + 摘要 + 实体，BM25 检索）
    tok = settings.fts5_tokenizer or "unicode61"
    await conn.execute(
        text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5("
            "  episodic_id UNINDEXED, user_id UNINDEXED, "
            "  summary_1line, raw_user_text, raw_assistant_text, emotion, entities, "
            f"  tokenize='{tok}', content='episodic', content_rowid='rowid'"
            ");"
        )
    )

    # 2) sqlite-vss 向量表：维度读 settings.dashscope_embedding_dim（默认 qwen3.7-text-embedding=1024）
    # 扩展没加载时会抛错，捕获降级
    emb_dim = int(getattr(settings, "dashscope_embedding_dim", 1024) or 1024)
    try:
        await conn.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS episodic_vec USING vss0("
                "  episodic_id TEXT, "
                f"  embedding({emb_dim}) FLOAT, "
                "  user_id TEXT"
                ");"
            )
        )
    except Exception as exc:  # pragma: no cover - 环境相关
        log.warning("sqlite-vss episodic_vec 创建失败（可忽略，功能降级）: %s", exc)
