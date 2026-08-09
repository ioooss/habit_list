"""SQLite-only local search structures.

Production relational schema changes are owned exclusively by Alembic.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text

from ..core.config import Settings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger("habit_list")


async def apply(conn: "AsyncConnection", settings: Settings) -> None:
    if conn.dialect.name != "sqlite":
        raise RuntimeError("SQLite feature bootstrap called for a non-SQLite database")

    # ``auto_create`` has been used by the local preview before Alembic was
    # introduced.  Keep those databases usable without rebuilding them: this
    # repair only adds the media table and terrain lifecycle columns introduced
    # after the original preview schema.  Production never calls this path.
    await _repair_preview_schema(conn)

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


async def _repair_preview_schema(conn: "AsyncConnection") -> None:
    from .models import MediaAsset

    await conn.run_sync(lambda sync_conn: MediaAsset.__table__.create(sync_conn, checkfirst=True))
    media_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in sqlalchemy_inspect(sync_conn).get_columns("media_assets")
        }
    )
    media_additions = {
        "media_group_id": "VARCHAR(64)",
        "media_role": "VARCHAR(24)",
        "transcript_confidence": "FLOAT",
    }
    for name, ddl in media_additions.items():
        if name not in media_columns:
            await conn.execute(text(f"ALTER TABLE media_assets ADD COLUMN {name} {ddl}"))
    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in sqlalchemy_inspect(sync_conn).get_columns("memory_claims")
        }
    )
    additions = {
        "terrain_state": "VARCHAR(24) NOT NULL DEFAULT 'forming'",
        "terrain_user_label": "VARCHAR(160)",
        "terrain_first_revealed_at": "VARCHAR(32)",
        "terrain_last_changed_at": "VARCHAR(32)",
        "terrain_history_json": "JSON NOT NULL DEFAULT '[]'",
    }
    for name, ddl in additions.items():
        if name not in columns:
            await conn.execute(text(f"ALTER TABLE memory_claims ADD COLUMN {name} {ddl}"))
    event_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in sqlalchemy_inspect(sync_conn).get_columns("user_events")
        }
    )
    if "terrain_eligible" not in event_columns:
        await conn.execute(
            text("ALTER TABLE user_events ADD COLUMN terrain_eligible BOOLEAN NOT NULL DEFAULT 0")
        )
        # Match the previous projection, which only accepted moment sources.
        await conn.execute(
            text(
                "UPDATE user_events SET terrain_eligible = 1 "
                "WHERE source = 'moment' AND mode = 'moment'"
            )
        )
