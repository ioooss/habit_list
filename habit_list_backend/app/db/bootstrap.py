"""Idempotent, cross-dialect seed data required by the transitional API."""
from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..core.config import Settings
from .models import Procedural, User, _utcnow_iso, uuid7

_PROCEDURAL_DEFAULTS: tuple[tuple[str, dict[str, Any], float, str], ...] = (
    ("reply_speed", {"label": "中等速度", "level": 2, "hint": "快-1/中-2/慢-3"}, 0.5, "默认值"),
    ("reply_length", {"label": "一段", "level": 1, "hint": "一句-1/一段-2/长段落-3"}, 0.5, "默认值"),
    ("silence_tolerance_days", {"label": "3 天不说话视为低谷", "days": 3}, 0.5, "默认值"),
    ("proactivity", {"label": "不主动问候", "level": 0}, 0.5, "默认值"),
    ("tone_gentle", {"value": 0.7}, 0.5, "默认值"),
    ("tone_sarcastic", {"value": 0.05}, 0.5, "默认值"),
    ("default_companion_kind_guess", {"value": "confide"}, 0.5, "默认值"),
)


def _insert_do_nothing(conn: AsyncConnection, table, values: dict[str, Any], index: list[str]):
    if conn.dialect.name == "postgresql":
        return postgresql_insert(table).values(**values).on_conflict_do_nothing(index_elements=index)
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=index)
    raise RuntimeError(f"unsupported database dialect: {conn.dialect.name}")


async def seed_transitional_defaults(conn: AsyncConnection, settings: Settings) -> None:
    """Seed the fixed-user compatibility layer without startup races."""

    now = _utcnow_iso()
    await conn.execute(
        _insert_do_nothing(
            conn,
            User.__table__,
            {
                "user_id": settings.default_user_id,
                "created_at": now,
                "locale": settings.default_user_locale,
                "timezone": settings.default_user_timezone,
                "dashscope_quota": -1,
                "current_style": "default",
                "settings_json": {},
            },
            ["user_id"],
        )
    )
    for key, value, confidence, reason in _PROCEDURAL_DEFAULTS:
        await conn.execute(
            _insert_do_nothing(
                conn,
                Procedural.__table__,
                {
                    "proc_id": str(uuid7()),
                    "user_id": settings.default_user_id,
                    "param_key": key,
                    "param_value_json": value,
                    "confidence": confidence,
                    "learned_reason": reason,
                    "learned_ev_count": 0,
                    "created_at": now,
                    "updated_at": now,
                    "ref_ledger_ids_json": [],
                },
                ["user_id", "param_key"],
            )
        )


__all__ = ["seed_transitional_defaults"]
