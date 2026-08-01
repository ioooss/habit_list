"""Memory V2 persistence models.

The legacy four-layer memory tables stay intact while V2 runs in shadow mode.
V2 separates deletable user content from derived claims and keeps every claim
grounded in evidence.  All timestamps are UTC ISO-8601 strings for compatibility
with the existing SQLite deployment; the schema is also PostgreSQL friendly.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import _utcnow_iso, uuid7

_JSON_DEFAULT_DICT = lambda: {}  # noqa: E731
_JSON_DEFAULT_LIST = lambda: []  # noqa: E731


class UserEvent(Base):
    """A deletable, user-authored source event.

    Assistant output is intentionally excluded.  Derived memory evidence must
    point back to one of these rows so that corrections and deletion propagate.
    """

    __tablename__ = "user_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    client_event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="chat")
    mode: Mapped[str] = mapped_column(String(24), default="confide", index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[str] = mapped_column(String(32), index=True)
    recorded_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    sensitivity: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source_ref_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    deleted_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_user_events_user_request"),
        UniqueConstraint("user_id", "client_event_id", name="uq_user_events_user_client_event"),
        Index("idx_user_events_user_time", "user_id", "occurred_at", "event_id"),
    )


class MemoryClaim(Base):
    """A versioned semantic or procedural claim grounded in user evidence."""

    __tablename__ = "memory_claims"

    claim_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    claim_type: Mapped[str] = mapped_column(String(24), default="semantic", index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(128), default="self")
    predicate: Mapped[str] = mapped_column(String(128))
    object_value: Mapped[str] = mapped_column(Text)
    claim_text: Mapped[str] = mapped_column(Text)
    slot_key: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="system_inferred")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    user_status: Mapped[str] = mapped_column(String(24), default="proposed", index=True)
    sensitivity: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    valid_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    valid_to: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    observed_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    supersedes_claim_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("memory_claims.claim_id", ondelete="SET NULL"), nullable=True
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    allow_proactive: Mapped[bool] = mapped_column(Boolean, default=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    last_landed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    retrieval_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    updated_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    deleted_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_memory_claim_user_slot", "user_id", "slot_key", "user_status"),
        Index("idx_memory_claim_user_updated", "user_id", "updated_at", "claim_id"),
        Index("idx_memory_claim_retrievable", "user_id", "user_status", "valid_to", "pinned"),
    )


class MemoryEvidence(Base):
    """Grounding from a user event to a memory claim."""

    __tablename__ = "memory_evidence"

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    claim_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_claims.claim_id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_events.event_id", ondelete="CASCADE"), index=True
    )
    evidence_role: Mapped[str] = mapped_column(String(16), default="supports", index=True)
    excerpt_start: Mapped[int] = mapped_column(Integer)
    excerpt_end: Mapped[int] = mapped_column(Integer)
    excerpt_text: Mapped[str] = mapped_column(Text)
    source_weight: Mapped[float] = mapped_column(Float, default=1.0)
    extractor_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)

    __table_args__ = (
        UniqueConstraint("claim_id", "event_id", "evidence_role", name="uq_memory_evidence_once"),
        Index("idx_memory_evidence_claim_time", "claim_id", "created_at"),
    )


class MemoryRevision(Base):
    """Auditable claim state transition; deleted with the parent claim."""

    __tablename__ = "memory_revisions"

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    claim_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_claims.claim_id", ondelete="CASCADE"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(24), default="system")
    action: Mapped[str] = mapped_column(String(32), index=True)
    before_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    after_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    reason: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)


class MemoryEmbedding(Base):
    """Versioned vector payload.

    SQLite stores vectors as JSON for local development.  PostgreSQL migration
    will map this field to pgvector without changing the service contract.
    """

    __tablename__ = "memory_embeddings"

    embedding_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    claim_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_claims.claim_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    dimension: Mapped[int] = mapped_column(Integer)
    vector_json: Mapped[list] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)

    __table_args__ = (
        UniqueConstraint("claim_id", "model", "content_hash", name="uq_memory_embedding_version"),
    )


class MemoryRelation(Base):
    """A typed, time-bounded edge between claims, events, and entities."""

    __tablename__ = "memory_relations"

    relation_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    src_type: Mapped[str] = mapped_column(String(24))
    src_id: Mapped[str] = mapped_column(String(128), index=True)
    dst_type: Mapped[str] = mapped_column(String(24))
    dst_id: Mapped[str] = mapped_column(String(128), index=True)
    relation_type: Mapped[str] = mapped_column(String(48), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    valid_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    valid_to: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_event_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("user_events.event_id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)

    __table_args__ = (
        Index("idx_memory_relation_user_src", "user_id", "src_type", "src_id", "status"),
        Index("idx_memory_relation_user_dst", "user_id", "dst_type", "dst_id", "status"),
    )


class MemoryRetrievalTrace(Base):
    """Privacy-minimized explanation of a retrieval decision."""

    __tablename__ = "memory_retrieval_traces"

    trace_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    query_hash: Mapped[str] = mapped_column(String(64))
    route: Mapped[str] = mapped_column(String(32))
    candidates_json: Mapped[list] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)
    selected_json: Mapped[list] = mapped_column(JSON, default=_JSON_DEFAULT_LIST)
    used_in_response: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)

    __table_args__ = (
        Index("idx_memory_trace_user_time", "user_id", "created_at", "trace_id"),
    )


class OutboxEvent(Base):
    """Transactional outbox used by the Memory V2 worker."""

    __tablename__ = "outbox_events"

    outbox_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=_JSON_DEFAULT_DICT)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    locked_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    processed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_outbox_dispatch", "status", "available_at", "created_at"),
    )


class MemoryDeletionTombstone(Base):
    """Irreversible deletion proof without retaining user content or claim IDs."""

    __tablename__ = "memory_deletion_tombstones"

    tombstone_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid7()))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(24), default="user")
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)


__all__ = [
    "MemoryClaim",
    "MemoryDeletionTombstone",
    "MemoryEmbedding",
    "MemoryEvidence",
    "MemoryRelation",
    "MemoryRetrievalTrace",
    "MemoryRevision",
    "OutboxEvent",
    "UserEvent",
]
