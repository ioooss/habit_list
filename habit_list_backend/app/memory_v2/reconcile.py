"""Evidence grounding, claim reconciliation, and temporal versioning."""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.memory_models import (
    MemoryClaim,
    MemoryDeletionTombstone,
    MemoryEvidence,
    MemoryRevision,
    OutboxEvent,
    UserEvent,
)
from ..db.models import _utcnow_iso
from .domain import (
    ClaimType,
    EvidenceRole,
    MemoryAtom,
    MemoryCategory,
    MemoryExtraction,
    ReconciliationResult,
    Sensitivity,
    SourceType,
    UserStatus,
)

EMBEDDING_REQUESTED = "memory.embedding.requested"

_NORMALIZE_RE = re.compile(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]{}]+")
_AUTO_CONFIRM_CATEGORIES = {
    MemoryCategory.IDENTITY,
    MemoryCategory.PREFERENCE,
    MemoryCategory.GOAL,
    MemoryCategory.HABIT,
    MemoryCategory.CREATIVITY,
    MemoryCategory.CONSUMPTION,
    MemoryCategory.CYCLE,
}
_OPEN_STATUSES = {
    UserStatus.PROPOSED.value,
    UserStatus.CONFIRMED.value,
    UserStatus.CORRECTED.value,
}
_SINGLE_VALUE_PREDICATES = {"name"}


@dataclass(frozen=True)
class GroundedEvidence:
    start: int
    end: int
    text: str


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return _NORMALIZE_RE.sub("", value)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deletion_evidence_hash(*, user_id: str, event_id: str, evidence_text: str) -> str:
    """Opaque key that prevents one deleted event/claim binding from reappearing."""

    return _hash(f"{user_id}|{event_id}|{_hash(evidence_text)}")


def claim_keys_from_fields(
    *,
    category: str,
    subject: str,
    predicate: str,
    object_value: str,
    claim_text: str,
) -> tuple[str, str]:
    normalized_predicate = _normalize(predicate)
    slot_parts = [category, _normalize(subject), normalized_predicate]
    # Likes, goals, habits, and interaction preferences are sets: liking tea
    # must not silently erase liking coffee. Only explicitly single-valued
    # predicates share a temporal slot and enter conflict confirmation.
    if normalized_predicate not in _SINGLE_VALUE_PREDICATES:
        slot_parts.append(_normalize(object_value))
    slot = "|".join(slot_parts)
    content = "|".join([slot, _normalize(object_value), _normalize(claim_text)])
    return _hash(slot), _hash(content)


def claim_keys(atom: MemoryAtom) -> tuple[str, str]:
    """Return (slot_key, content_hash).

    A slot identifies the thing that may evolve (e.g. self/likes), while the
    content hash identifies one concrete value in that slot.
    """

    return claim_keys_from_fields(
        category=atom.category.value,
        subject=atom.subject,
        predicate=atom.predicate,
        object_value=atom.object_value,
        claim_text=atom.claim_text,
    )


def ground_evidence(atom: MemoryAtom, source_text: str) -> GroundedEvidence | None:
    """Accept only a literal continuous excerpt from the source event."""

    if atom.evidence_start is not None and atom.evidence_end is not None:
        start = atom.evidence_start
        end = atom.evidence_end
        if 0 <= start < end <= len(source_text):
            excerpt = source_text[start:end]
            if excerpt == atom.evidence_text:
                return GroundedEvidence(start=start, end=end, text=excerpt)
    start = source_text.find(atom.evidence_text)
    if start < 0:
        return None
    return GroundedEvidence(start=start, end=start + len(atom.evidence_text), text=atom.evidence_text)


def _auto_status(atom: MemoryAtom, settings: Settings) -> UserStatus:
    if atom.sensitivity != Sensitivity.NORMAL:
        return UserStatus.PROPOSED
    if atom.claim_type == ClaimType.PROCEDURAL:
        return UserStatus.PROPOSED
    if (
        atom.source_type == SourceType.USER_EXPLICIT
        and atom.category in _AUTO_CONFIRM_CATEGORIES
        and atom.confidence >= settings.memory_v2_auto_confirm_conf
    ):
        return UserStatus.CONFIRMED
    return UserStatus.PROPOSED


def _source_weight(atom: MemoryAtom) -> float:
    if atom.source_type == SourceType.USER_CONFIRMED:
        return 1.25
    if atom.source_type == SourceType.USER_EXPLICIT:
        return 1.0
    return 0.45


def _updated_confidence(current: float, *, supports: bool, weight: float) -> float:
    current = min(max(float(current), 0.02), 0.98)
    logit = math.log(current / (1.0 - current))
    delta = 0.65 * weight if supports else -0.85 * weight
    return round(min(max(1.0 / (1.0 + math.exp(-(logit + delta))), 0.01), 0.99), 4)


def _claim_snapshot(claim: MemoryClaim) -> dict:
    return {
        "claim_text": claim.claim_text,
        "confidence": claim.confidence,
        "user_status": claim.user_status,
        "valid_from": claim.valid_from,
        "valid_to": claim.valid_to,
        "evidence_count": claim.evidence_count,
        "version": claim.version,
    }


async def _evidence_exists(
    session: AsyncSession,
    *,
    claim_id: str,
    event_id: str,
    role: EvidenceRole,
) -> bool:
    row = (
        await session.execute(
            select(MemoryEvidence.evidence_id).where(
                MemoryEvidence.claim_id == claim_id,
                MemoryEvidence.event_id == event_id,
                MemoryEvidence.evidence_role == role.value,
            )
        )
    ).first()
    return row is not None


async def _add_evidence(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    event: UserEvent,
    atom: MemoryAtom,
    grounded: GroundedEvidence,
    role: EvidenceRole,
    settings: Settings,
) -> bool:
    if await _evidence_exists(
        session, claim_id=claim.claim_id, event_id=event.event_id, role=role
    ):
        return False
    session.add(
        MemoryEvidence(
            claim_id=claim.claim_id,
            event_id=event.event_id,
            evidence_role=role.value,
            excerpt_start=grounded.start,
            excerpt_end=grounded.end,
            excerpt_text=grounded.text,
            source_weight=_source_weight(atom),
            extractor_version=settings.memory_v2_extractor_version,
        )
    )
    claim.evidence_count = int(claim.evidence_count or 0) + 1
    return True


def _add_revision(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    action: str,
    before: dict,
    reason: str,
    settings: Settings,
    request_id: str | None,
    actor_type: str = "system",
) -> None:
    session.add(
        MemoryRevision(
            claim_id=claim.claim_id,
            actor_type=actor_type,
            action=action,
            before_json=before,
            after_json=_claim_snapshot(claim),
            reason=reason,
            request_id=request_id,
            policy_version=settings.memory_v2_policy_version,
        )
    )


def _enqueue_embedding(session: AsyncSession, claim: MemoryClaim, settings: Settings) -> None:
    if not settings.memory_v2_embedding_enabled:
        return
    session.add(
        OutboxEvent(
            user_id=claim.user_id,
            aggregate_type="memory_claim",
            aggregate_id=claim.claim_id,
            event_type=EMBEDDING_REQUESTED,
            payload_json={"claim_id": claim.claim_id},
        )
    )


async def reconcile_event(
    session: AsyncSession,
    *,
    event: UserEvent,
    extraction: MemoryExtraction,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> ReconciliationResult:
    """Merge grounded atoms into a user's temporal claim history."""

    settings = settings or get_settings()
    result = ReconciliationResult()
    now = _utcnow_iso()

    for atom in extraction.atoms:
        # Crisis language belongs to the safety flow and must never become a
        # normal profile claim.
        if atom.sensitivity == Sensitivity.CRISIS:
            result.skipped_atoms += 1
            continue
        grounded = ground_evidence(atom, event.content)
        if grounded is None:
            result.skipped_atoms += 1
            continue

        slot_key, content_hash = claim_keys(atom)
        deleted_binding = (
            await session.execute(
                select(MemoryDeletionTombstone.tombstone_id).where(
                    MemoryDeletionTombstone.resource_type == "memory_claim_evidence",
                    MemoryDeletionTombstone.resource_hash
                    == deletion_evidence_hash(
                        user_id=event.user_id,
                        event_id=event.event_id,
                        evidence_text=grounded.text,
                    ),
                )
            )
        ).first()
        if deleted_binding is not None:
            result.skipped_atoms += 1
            continue
        rows = (
            await session.execute(
                select(MemoryClaim)
                .where(
                    MemoryClaim.user_id == event.user_id,
                    MemoryClaim.slot_key == slot_key,
                    MemoryClaim.deleted_at.is_(None),
                )
                .order_by(MemoryClaim.version.desc(), MemoryClaim.updated_at.desc())
            )
        ).scalars().all()
        claims = list(rows)
        current = next(
            (
                claim
                for claim in claims
                if claim.user_status
                in {UserStatus.CONFIRMED.value, UserStatus.CORRECTED.value}
            ),
            next(
                (claim for claim in claims if claim.user_status == UserStatus.PROPOSED.value),
                None,
            ),
        )
        exact = next(
            (
                claim
                for claim in claims
                if claim.content_hash == content_hash and claim.user_status in _OPEN_STATUSES
            ),
            None,
        )

        if exact is not None:
            before = _claim_snapshot(exact)
            added = await _add_evidence(
                session,
                claim=exact,
                event=event,
                atom=atom,
                grounded=grounded,
                role=EvidenceRole.SUPPORTS,
                settings=settings,
            )
            if not added:
                continue
            exact.confidence = _updated_confidence(
                exact.confidence, supports=True, weight=_source_weight(atom)
            )
            if (
                exact.user_status == UserStatus.PROPOSED.value
                and exact.supersedes_claim_id is None
            ):
                promoted = _auto_status(atom, settings)
                if promoted == UserStatus.CONFIRMED:
                    exact.user_status = promoted.value
            exact.updated_at = now
            _add_revision(
                session,
                claim=exact,
                action="evidence_added",
                before=before,
                reason="new grounded user evidence supports the existing claim",
                settings=settings,
                request_id=request_id,
            )
            _enqueue_embedding(session, exact, settings)
            result.updated_claim_ids.append(exact.claim_id)
            continue

        next_version = max((int(claim.version or 1) for claim in claims), default=0) + 1
        status = _auto_status(atom, settings)
        valid_from = atom.valid_from or event.occurred_at
        supersedes_id: str | None = None

        if current is not None:
            supersedes_id = current.claim_id
            # A different value in a single-valued slot is always a proposal.
            # The old confirmed version remains active until the user confirms
            # this new version explicitly.
            status = UserStatus.PROPOSED
            before = _claim_snapshot(current)
            await _add_evidence(
                session,
                claim=current,
                event=event,
                atom=atom,
                grounded=grounded,
                role=EvidenceRole.CONTRADICTS,
                settings=settings,
            )
            current.confidence = _updated_confidence(
                current.confidence, supports=False, weight=_source_weight(atom)
            )
            current.updated_at = now
            _add_revision(
                session,
                claim=current,
                action="conflict_detected",
                before=before,
                reason="new grounded evidence has a different value for the same temporal slot",
                settings=settings,
                request_id=request_id,
            )

        claim = MemoryClaim(
            user_id=event.user_id,
            claim_type=atom.claim_type.value,
            category=atom.category.value,
            subject=atom.subject,
            predicate=atom.predicate,
            object_value=atom.object_value,
            claim_text=atom.claim_text,
            slot_key=slot_key,
            content_hash=content_hash,
            source_type=atom.source_type.value,
            confidence=round(float(atom.confidence), 4),
            user_status=status.value,
            sensitivity=atom.sensitivity.value,
            valid_from=valid_from,
            valid_to=atom.valid_to,
            observed_at=event.occurred_at,
            evidence_count=0,
            supersedes_claim_id=supersedes_id,
            allow_proactive=atom.sensitivity == Sensitivity.NORMAL,
            importance=atom.importance,
            version=next_version,
            created_by_policy_version=settings.memory_v2_policy_version,
            created_at=now,
            updated_at=now,
        )
        session.add(claim)
        await session.flush()
        await _add_evidence(
            session,
            claim=claim,
            event=event,
            atom=atom,
            grounded=grounded,
            role=EvidenceRole.SUPPORTS,
            settings=settings,
        )
        _add_revision(
            session,
            claim=claim,
            action="created",
            before={},
            reason="created from grounded user evidence",
            settings=settings,
            request_id=request_id,
        )
        _enqueue_embedding(session, claim, settings)
        result.created_claim_ids.append(claim.claim_id)

    return result


__all__ = [
    "EMBEDDING_REQUESTED",
    "claim_keys",
    "claim_keys_from_fields",
    "deletion_evidence_hash",
    "ground_evidence",
    "reconcile_event",
]
