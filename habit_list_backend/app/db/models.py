"""8 张核心 ORM 模型（UUID7 主键 + SQLite TEXT 存）。

只定义结构，不做 migrate；索引/约束直接在 Column / Table args 里写好。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

# Python 3.12+ 有 uuid.uuid7；3.11/3.13 及以下用自定义 polyfill（时间有序，36 位 hex 字符串）
try:  # pragma: no cover - 平台相关
    _uuid7_native = _uuid.uuid7
except AttributeError:  # pragma: no cover
    import secrets as _secrets
    import time as _time

    def _uuid7_native() -> _uuid.UUID:  # type: ignore[misc]
        """简易 polyfill：前 48 bits=毫秒时间戳，后 80 bits=随机；保证时间有序，TEXT 存够用。"""
        ms = int(_time.time_ns() // 1_000_000)
        rand = int.from_bytes(_secrets.token_bytes(10), "big")
        # UUID v7: 0111 (ver=7) 保留位：variant=10xx
        value = ((ms & 0xFFFFFFFFFFFF) << 80) | (0x7 << 76) | ((rand >> 4) & 0x0FFFFFFFFFFFFFFF) | (0x8 << 62)
        return _uuid.UUID(int=value & ((1 << 128) - 1))


def uuid7() -> str:
    """返回标准 UUID7 字符串（36 位）。"""
    return str(_uuid7_native())

_JSON_DEFAULT_DICT = lambda: {}  # noqa: E731
_JSON_DEFAULT_LIST = lambda: []  # noqa: E731


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    locale: Mapped[str] = mapped_column(String(16), default="zh-CN")
    timezone: Mapped[str] = mapped_column(String(32), default="Asia/Shanghai")
    dashscope_quota: Mapped[int] = mapped_column(default=-1)
    current_style: Mapped[str] = mapped_column(String(32), default="default")
    settings_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)


class RawLedger(Base):
    """权威原始账本，只 append，永不 UPDATE/DELETE。"""
    __tablename__ = "raw_ledger"
    ledger_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    entry_type: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    ref_ledger_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    trace_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)

    __table_args__ = (
        Index("idx_ledger_user_time", "user_id", "created_at"),
        Index("idx_ledger_user_session", "user_id", "session_id"),
        Index("idx_ledger_type_time", "entry_type", "created_at"),
    )


class Working(Base):
    """第1层 工作记忆（会话级）。"""
    __tablename__ = "working"
    working_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant / memo_auto_detect
    content: Mapped[str] = mapped_column(Text)
    mood: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    source_kind: Mapped[str] = mapped_column(String(32), default="confide")
    ref_ledger_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        Index("idx_wkg_user_session", "user_id", "session_id", "created_at"),
    )


class Episodic(Base):
    """第2层 情景记忆 = 河里的石子。"""
    __tablename__ = "episodic"
    episodic_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    source: Mapped[str] = mapped_column(String(32), default="companion")
    kind: Mapped[str] = mapped_column(String(32), default="confide", index=True)  # confide/memo/life_fragment
    kind_fixed_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    kind_fixed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    summary_1line: Mapped[str] = mapped_column(String(256), default="")
    emotion: Mapped[str] = mapped_column(String(8), default="-")
    entities_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)
    raw_user_text: Mapped[str] = mapped_column(Text, default="")
    raw_assistant_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    retrieval_weight: Mapped[float] = mapped_column(default=1.0, index=True)
    last_landed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archived/pending_conflict
    ref_ledger_ids_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)

    __table_args__ = (
        Index("idx_ep_user_date", "user_id", "created_at"),
        Index("idx_ep_user_kind", "user_id", "kind", "created_at"),
        Index("idx_ep_user_weight", "user_id", "retrieval_weight"),
    )


class Semantic(Base):
    """第3层 语义事实（画像 + 确认过的洞察）。"""
    __tablename__ = "semantic"
    semantic_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    fact_text: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_count: Mapped[int] = mapped_column(default=0)
    retrieval_weight: Mapped[float] = mapped_column(default=1.0, index=True)
    last_landed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, onupdate=_utcnow_iso)
    tags_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)
    status: Mapped[str] = mapped_column(String(16), default="active")

    __table_args__ = (
        Index("idx_sem_user_cat", "user_id", "category", "status"),
    )


class Procedural(Base):
    """第4层 程序偏好（交互参数/风格/习惯）。"""
    __tablename__ = "procedural"
    proc_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36))
    param_key: Mapped[str] = mapped_column(String(64))
    param_value_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    confidence: Mapped[float] = mapped_column(default=0.5)
    learned_reason: Mapped[str] = mapped_column(Text, default="")
    learned_ev_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, onupdate=_utcnow_iso)
    ref_ledger_ids_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)

    __table_args__ = (
        UniqueConstraint("user_id", "param_key", name="uq_proc_user_key"),
    )


class Memo(Base):
    """备忘（iOS 备忘页直接读，派生对应 Episodic）。"""
    __tablename__ = "memos"
    memo_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    text: Mapped[str] = mapped_column(Text)
    clean_text: Mapped[str] = mapped_column(Text, default="")
    due_text: Mapped[str] = mapped_column(String(128), default="")
    due_iso: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    due_offset_days: Mapped[int] = mapped_column(default=99, index=True)
    importance: Mapped[str] = mapped_column(String(8), default="green", index=True)  # red/yellow/green
    source: Mapped[str] = mapped_column(String(32), default="companion_auto")  # companion_auto / memo_page_manual
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)  # pending/done/overdue_stale/archived
    notified_ios_ids_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status_changed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    linked_episodic_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    linked_ledger_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    detect_meta_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_memo_user_status", "user_id", "status", "due_offset_days", "created_at"),
        Index("idx_memo_user_imp", "user_id", "importance", "status"),
        Index("idx_memo_due_iso", "due_iso"),
    )


class Insight(Base):
    """发现页候选（未确认的 Semantic/Procedural）。"""
    __tablename__ = "insights"
    insight_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    text_html: Mapped[str] = mapped_column(Text)
    meta: Mapped[str] = mapped_column(String(128), default="")
    confidence: Mapped[float] = mapped_column(default=0.6)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/confirmed/denied/archived
    feedback_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    ref_semantic_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)


class GraphNode(Base):
    """图谱节点持久化（MVP：每次启机从这里读，重建内存里的 NetworkX DiGraph）。"""
    __tablename__ = "graph_nodes"
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(32))
    node_name: Mapped[str] = mapped_column(String(128))
    entity_norm_name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    src: Mapped[str] = mapped_column(String(64), primary_key=True)
    dst: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    weight: Mapped[float] = mapped_column(default=1.0)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
