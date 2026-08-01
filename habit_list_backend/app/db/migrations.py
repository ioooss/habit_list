"""初始化虚表（FTS5 / sqlite-vss vector）+ 默认用户 + 默认 Procedural 参数。幂等。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..core.config import Settings
from ..db.models import _utcnow_iso, uuid7

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger("habit_list")


def _to_json(val) -> str:
    """把 dict/list 等对象转成紧凑 JSON 字符串（给 migrations 里的 SQL 参数绑定用）。"""
    import json as _json
    return _json.dumps(val, ensure_ascii=False, separators=(",", ":"))


async def apply(conn: "AsyncConnection", settings: Settings) -> None:
    # 1) 默认用户
    # 注意：必须手动填 created_at / dashscope_quota，因为 mapped_column(default=...)
    # 只在 SQLAlchemy ORM db.add() 时生效；这里是原生 text() SQL，NOT NULL 列必须显式赋值。
    await conn.execute(
        text(
            """
            INSERT OR IGNORE INTO users(user_id, created_at, locale, timezone, dashscope_quota,
                                        current_style, settings_json)
            VALUES(:uid, :created_at, :locale, :tz, -1, 'default', '{}')
            """
        ),
        {
            "uid": settings.default_user_id,
            "created_at": _utcnow_iso(),
            "locale": settings.default_user_locale,
            "tz": settings.default_user_timezone,
        },
    )

    # 2) FTS5 虚表：episodic（原文 + 摘要 + 实体，BM25 检索）
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

    # 3) sqlite-vss 向量表：维度读 settings.dashscope_embedding_dim（默认 qwen3.7-text-embedding=1024）
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

    # 4) 默认 Procedural 参数（后续 System1 读不到就取这些默认值）
    # 注意：这里的 param_value_json 要先手动 _to_json() → 因为 text() + sqlite3 原生绑定不支持 dict
    defaults = [
        ("reply_speed", {"label": "中等速度", "level": 2, "hint": "快-1/中-2/慢-3"}, 0.5, "默认值"),
        ("reply_length", {"label": "一段", "level": 1, "hint": "一句-1/一段-2/长段落-3"}, 0.5, "默认值"),
        ("silence_tolerance_days", {"label": "3 天不说话视为低谷", "days": 3}, 0.5, "默认值"),
        ("proactivity", {"label": "不主动问候", "level": 0}, 0.5, "默认值"),
        ("tone_gentle", {"value": 0.7}, 0.5, "默认值"),
        ("tone_sarcastic", {"value": 0.05}, 0.5, "默认值"),
        ("default_companion_kind_guess", {"value": "confide"}, 0.5, "默认值"),
    ]
    now = _utcnow_iso()
    for key, val, conf, reason in defaults:
        await conn.execute(
            text(
                """
                INSERT OR IGNORE INTO procedural(proc_id, user_id, param_key, param_value_json, confidence,
                                                 learned_reason, learned_ev_count, created_at, updated_at,
                                                 ref_ledger_ids_json)
                VALUES(:proc_id, :uid, :key, :val_json, :conf, :reason, 0, :now, :now, '[]')
                """
            ),
            {
                "proc_id": str(uuid7()),
                "uid": settings.default_user_id,
                "key": key,
                "val_json": _to_json(val),
                "conf": conf,
                "reason": reason,
                "now": now,
            },
        )
