"""Application services for Memory V2 write paths and user control."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.memory_models import (
    MemoryClaim,
    MemoryDeletionTombstone,
    MemoryEvidence,
    MemoryRelation,
    MemoryRetrievalTrace,
    MemoryRevision,
    OutboxEvent,
    UserEvent,
)
from ..db.models import _utcnow_iso
from .domain import MemoryCategory, RetrievalBatch, SourceType, UserStatus
from .extractor import infer_sensitivity
from .reconcile import (
    EMBEDDING_REQUESTED,
    claim_keys_from_fields,
    deletion_evidence_hash,
)

EXTRACTION_REQUESTED = "memory.extraction.requested"


@dataclass(frozen=True)
class EnqueuedUserEvent:
    event_id: str
    outbox_id: str | None
    created: bool


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot(claim: MemoryClaim) -> dict[str, Any]:
    return {
        "claim_text": claim.claim_text,
        "category": claim.category,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object_value": claim.object_value,
        "confidence": claim.confidence,
        "user_status": claim.user_status,
        "sensitivity": claim.sensitivity,
        "valid_from": claim.valid_from,
        "valid_to": claim.valid_to,
        "pinned": claim.pinned,
        "allow_proactive": claim.allow_proactive,
        "version": claim.version,
    }


def _add_user_revision(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    action: str,
    before: dict[str, Any],
    request_id: str | None,
    reason: str,
    settings: Settings,
) -> None:
    session.add(
        MemoryRevision(
            claim_id=claim.claim_id,
            actor_type="user",
            action=action,
            before_json=before,
            after_json=_snapshot(claim),
            reason=reason,
            request_id=request_id,
            policy_version=settings.memory_v2_policy_version,
        )
    )


async def enqueue_user_event(
    session: AsyncSession,
    *,
    user_id: str,
    session_id: str,
    request_id: str,
    content: str,
    mode: str,
    occurred_at: str | None = None,
    client_event_id: str | None = None,
    source_ref_id: str | None = None,
    settings: Settings | None = None,
) -> EnqueuedUserEvent | None:
    """Persist a user source event and its extraction request atomically."""

    settings = settings or get_settings()
    if settings.memory_v2_mode == "off":
        return None
    existing = (
        await session.execute(
            select(UserEvent).where(
                UserEvent.user_id == user_id,
                UserEvent.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return EnqueuedUserEvent(event_id=existing.event_id, outbox_id=None, created=False)

    now = occurred_at or _utcnow_iso()
    sensitivity = infer_sensitivity(content, MemoryCategory.OTHER)
    event = UserEvent(
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        client_event_id=client_event_id,
        source="chat",
        mode=mode,
        content=content,
        content_hash=_sha256(content),
        occurred_at=now,
        recorded_at=_utcnow_iso(),
        sensitivity=sensitivity.value,
        status="active",
        source_ref_id=source_ref_id,
        metadata_json={"policy_version": settings.memory_v2_policy_version},
    )
    session.add(event)
    await session.flush()
    outbox = OutboxEvent(
        user_id=user_id,
        aggregate_type="user_event",
        aggregate_id=event.event_id,
        event_type=EXTRACTION_REQUESTED,
        # Never duplicate raw user text in the outbox.
        payload_json={"event_id": event.event_id},
    )
    session.add(outbox)
    await session.flush()
    return EnqueuedUserEvent(event_id=event.event_id, outbox_id=outbox.outbox_id, created=True)


async def record_retrieval_trace(
    session: AsyncSession,
    *,
    user_id: str,
    request_id: str,
    batch: RetrievalBatch,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    trace = MemoryRetrievalTrace(
        user_id=user_id,
        request_id=request_id,
        query_hash=batch.query_hash,
        route=batch.route.value,
        candidates_json=[
            {
                "claim_id": item.claim_id,
                "score": item.final_score,
                "features": item.features,
                "reasons": item.reasons,
            }
            for item in batch.candidates
        ],
        selected_json=[
            {"claim_id": item.claim_id, "score": item.final_score}
            for item in batch.selected
        ],
        used_in_response=batch.used_in_response,
        policy_version=settings.memory_v2_policy_version,
    )
    session.add(trace)
    if batch.used_in_response and batch.selected:
        landed_at = _utcnow_iso()
        selected_ids = [item.claim_id for item in batch.selected]
        claims = (
            await session.execute(
                select(MemoryClaim).where(
                    MemoryClaim.user_id == user_id,
                    MemoryClaim.claim_id.in_(selected_ids),
                    MemoryClaim.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        for claim in claims:
            claim.last_landed_at = landed_at
            claim.retrieval_count = int(claim.retrieval_count or 0) + 1
    await session.flush()
    return trace.trace_id


async def get_claim_for_user(
    session: AsyncSession,
    *,
    user_id: str,
    claim_id: str,
) -> MemoryClaim | None:
    return (
        await session.execute(
            select(MemoryClaim).where(
                MemoryClaim.claim_id == claim_id,
                MemoryClaim.user_id == user_id,
                MemoryClaim.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def transition_claim(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    action: str,
    request_id: str | None,
    settings: Settings | None = None,
) -> MemoryClaim:
    settings = settings or get_settings()
    target = {
        "confirm": UserStatus.CONFIRMED,
        "reject": UserStatus.REJECTED,
        "hide": UserStatus.HIDDEN,
        "restore": UserStatus.CONFIRMED,
    }.get(action)
    if target is None:
        raise ValueError(f"unsupported memory transition: {action}")
    if claim.user_status == target.value:
        return claim
    allowed_sources = {
        "confirm": {UserStatus.PROPOSED.value},
        "reject": {
            UserStatus.PROPOSED.value,
            UserStatus.CONFIRMED.value,
            UserStatus.CORRECTED.value,
            UserStatus.HIDDEN.value,
        },
        "hide": {
            UserStatus.PROPOSED.value,
            UserStatus.CONFIRMED.value,
            UserStatus.CORRECTED.value,
        },
        "restore": {UserStatus.HIDDEN.value},
    }
    if claim.user_status not in allowed_sources[action]:
        raise ValueError(f"cannot {action} memory in status {claim.user_status}")
    before = _snapshot(claim)
    claim.user_status = target.value
    if action in {"confirm", "restore"}:
        claim.source_type = SourceType.USER_CONFIRMED.value
        claim.confidence = max(float(claim.confidence), 0.99)
    if action in {"reject", "hide"}:
        claim.allow_proactive = False
    claim.updated_at = _utcnow_iso()

    if action in {"confirm", "restore"} and claim.supersedes_claim_id:
        previous = (
            await session.execute(
                select(MemoryClaim).where(
                    MemoryClaim.claim_id == claim.supersedes_claim_id,
                    MemoryClaim.user_id == claim.user_id,
                    MemoryClaim.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if previous is not None and previous.user_status in {
            UserStatus.CONFIRMED.value,
            UserStatus.CORRECTED.value,
        }:
            previous_before = _snapshot(previous)
            previous.user_status = UserStatus.SUPERSEDED.value
            previous.valid_to = claim.valid_from or claim.updated_at
            previous.updated_at = claim.updated_at
            _add_user_revision(
                session,
                claim=previous,
                action="superseded_by_confirmation",
                before=previous_before,
                request_id=request_id,
                reason="user confirmed a newer value for the same temporal slot",
                settings=settings,
            )
    _add_user_revision(
        session,
        claim=claim,
        action=action,
        before=before,
        request_id=request_id,
        reason=f"user requested {action}",
        settings=settings,
    )
    return claim


async def correct_claim(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    changes: dict[str, Any],
    request_id: str | None,
    settings: Settings | None = None,
) -> MemoryClaim:
    settings = settings or get_settings()
    allowed = {
        "claim_text",
        "category",
        "subject",
        "predicate",
        "object_value",
        "valid_from",
        "valid_to",
        "pinned",
        "allow_proactive",
        "importance",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported memory fields: {', '.join(sorted(unknown))}")
    before = _snapshot(claim)
    for key, value in changes.items():
        setattr(claim, key, value)
    claim.slot_key, claim.content_hash = claim_keys_from_fields(
        category=claim.category,
        subject=claim.subject,
        predicate=claim.predicate,
        object_value=claim.object_value,
        claim_text=claim.claim_text,
    )
    claim.user_status = UserStatus.CORRECTED.value
    claim.source_type = SourceType.USER_CONFIRMED.value
    claim.confidence = 1.0
    claim.version = int(claim.version or 1) + 1
    claim.updated_at = _utcnow_iso()
    _add_user_revision(
        session,
        claim=claim,
        action="corrected",
        before=before,
        request_id=request_id,
        reason="user corrected the memory",
        settings=settings,
    )
    if settings.memory_v2_embedding_enabled:
        session.add(
            OutboxEvent(
                user_id=claim.user_id,
                aggregate_type="memory_claim",
                aggregate_id=claim.claim_id,
                event_type=EMBEDDING_REQUESTED,
                payload_json={"claim_id": claim.claim_id},
            )
        )
    return claim


async def permanently_delete_claim(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    request_id: str | None,
) -> MemoryDeletionTombstone:
    """Hard-delete a claim and all derived content, retaining only a hash tombstone."""

    resource_hash = _sha256(f"{claim.user_id}|memory_claim|{claim.claim_id}")
    tombstone = MemoryDeletionTombstone(
        resource_type="memory_claim",
        resource_hash=resource_hash,
        actor_type="user",
        request_id=request_id,
    )
    session.add(tombstone)
    evidence_bindings = list(
        (
            await session.execute(
                select(MemoryEvidence.event_id, MemoryEvidence.excerpt_text).where(
                    MemoryEvidence.claim_id == claim.claim_id
                )
            )
        ).all()
    )
    for event_id, excerpt_text in evidence_bindings:
        session.add(
            MemoryDeletionTombstone(
                resource_type="memory_claim_evidence",
                resource_hash=deletion_evidence_hash(
                    user_id=claim.user_id,
                    event_id=event_id,
                    evidence_text=excerpt_text,
                ),
                actor_type="user",
                request_id=request_id,
            )
        )
    await session.execute(
        delete(MemoryRelation).where(
            MemoryRelation.user_id == claim.user_id,
            or_(
                (MemoryRelation.src_type == "claim") & (MemoryRelation.src_id == claim.claim_id),
                (MemoryRelation.dst_type == "claim") & (MemoryRelation.dst_id == claim.claim_id),
            ),
        )
    )
    await session.execute(
        delete(OutboxEvent).where(
            OutboxEvent.aggregate_type == "memory_claim",
            OutboxEvent.aggregate_id == claim.claim_id,
        )
    )
    await session.delete(claim)
    await session.flush()
    return tombstone


__all__ = [
    "EXTRACTION_REQUESTED",
    "EnqueuedUserEvent",
    "correct_claim",
    "enqueue_user_event",
    "get_claim_for_user",
    "permanently_delete_claim",
    "record_retrieval_trace",
    "transition_claim",
]
