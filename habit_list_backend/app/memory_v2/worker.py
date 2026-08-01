"""Reliable outbox worker for Memory V2 extraction and indexing."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.memory_models import MemoryClaim, MemoryEmbedding, OutboxEvent, UserEvent
from ..db.models import _utcnow_iso
from ..providers import dashscope
from .extractor import extract_memory_atoms
from .reconcile import EMBEDDING_REQUESTED, reconcile_event
from .service import EXTRACTION_REQUESTED

log = logging.getLogger("habit_list.memory_v2.worker")


def _iso_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _claim_batch(limit: int) -> list[str]:
    now = _utcnow_iso()
    stale_lock = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with get_db(read_only=False) as db:
        events = list(
            (
                await db.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.event_type.in_([EXTRACTION_REQUESTED, EMBEDDING_REQUESTED]),
                        OutboxEvent.available_at <= now,
                        or_(
                            OutboxEvent.status == "pending",
                            (OutboxEvent.status == "processing")
                            & (OutboxEvent.locked_at.is_not(None))
                            & (OutboxEvent.locked_at <= stale_lock),
                        ),
                    )
                    .order_by(OutboxEvent.created_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
        )
        for event in events:
            event.status = "processing"
            event.locked_at = now
            event.attempts = int(event.attempts or 0) + 1
            event.last_error = None
        return [event.outbox_id for event in events]


async def _load_outbox(outbox_id: str) -> OutboxEvent | None:
    async with get_db(read_only=True) as db:
        return (
            await db.execute(select(OutboxEvent).where(OutboxEvent.outbox_id == outbox_id))
        ).scalar_one_or_none()


async def _mark_processed(outbox_id: str) -> None:
    async with get_db(read_only=False) as db:
        event = (
            await db.execute(select(OutboxEvent).where(OutboxEvent.outbox_id == outbox_id))
        ).scalar_one_or_none()
        if event is None:
            return
        event.status = "processed"
        event.processed_at = _utcnow_iso()
        event.locked_at = None
        event.last_error = None


async def _mark_failed(outbox_id: str, exc: Exception, settings: Settings) -> None:
    async with get_db(read_only=False) as db:
        event = (
            await db.execute(select(OutboxEvent).where(OutboxEvent.outbox_id == outbox_id))
        ).scalar_one_or_none()
        if event is None:
            return
        attempts = int(event.attempts or 1)
        event.status = "dead" if attempts >= settings.memory_v2_outbox_max_attempts else "pending"
        event.available_at = _iso_after(min(300, 2**attempts))
        event.locked_at = None
        # Persist only the exception class to avoid leaking model output or user text.
        event.last_error = type(exc).__name__[:120]


async def _process_extraction(outbox: OutboxEvent, settings: Settings) -> None:
    event_id = str((outbox.payload_json or {}).get("event_id") or outbox.aggregate_id)
    async with get_db(read_only=True) as db:
        source = (
            await db.execute(
                select(UserEvent).where(
                    UserEvent.event_id == event_id,
                    UserEvent.user_id == outbox.user_id,
                    UserEvent.status == "active",
                )
            )
        ).scalar_one_or_none()
        source_data = (
            (source.content, source.occurred_at, source.request_id) if source is not None else None
        )

    if source_data is None:
        await _mark_processed(outbox.outbox_id)
        return
    source_text, occurred_at, request_id = source_data

    extraction = await extract_memory_atoms(
        source_text,
        occurred_at=occurred_at,
        request_id=request_id,
        settings=settings,
    )
    async with get_db(read_only=False) as db:
        source = (
            await db.execute(
                select(UserEvent).where(
                    UserEvent.event_id == event_id,
                    UserEvent.user_id == outbox.user_id,
                    UserEvent.status == "active",
                )
            )
        ).scalar_one_or_none()
        current_outbox = (
            await db.execute(
                select(OutboxEvent).where(OutboxEvent.outbox_id == outbox.outbox_id)
            )
        ).scalar_one_or_none()
        if current_outbox is None:
            return
        if source is not None:
            await reconcile_event(
                db,
                event=source,
                extraction=extraction,
                request_id=request_id,
                settings=settings,
            )
        current_outbox.status = "processed"
        current_outbox.processed_at = _utcnow_iso()
        current_outbox.locked_at = None
        current_outbox.last_error = None


async def _process_embedding(outbox: OutboxEvent, settings: Settings) -> None:
    if not settings.memory_v2_embedding_enabled:
        await _mark_processed(outbox.outbox_id)
        return
    claim_id = str((outbox.payload_json or {}).get("claim_id") or outbox.aggregate_id)
    async with get_db(read_only=True) as db:
        claim = (
            await db.execute(
                select(MemoryClaim).where(
                    MemoryClaim.claim_id == claim_id,
                    MemoryClaim.user_id == outbox.user_id,
                    MemoryClaim.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        claim_data = (
            " ".join(
                [claim.category, claim.subject, claim.predicate, claim.object_value, claim.claim_text]
            )
            if claim is not None
            else None
        )

    if claim_data is None:
        await _mark_processed(outbox.outbox_id)
        return
    text_to_embed = claim_data
    content_hash = hashlib.sha256(text_to_embed.encode("utf-8")).hexdigest()

    vectors = await dashscope.embed_texts([text_to_embed], settings=settings)
    if not vectors or not vectors[0]:
        raise ValueError("embedding provider returned no vector")
    vector = vectors[0]
    if len(vector) != settings.dashscope_embedding_dim:
        raise ValueError("embedding provider returned an unexpected vector dimension")
    async with get_db(read_only=False) as db:
        current_outbox = (
            await db.execute(
                select(OutboxEvent).where(OutboxEvent.outbox_id == outbox.outbox_id)
            )
        ).scalar_one_or_none()
        if current_outbox is None:
            return
        existing = (
            await db.execute(
                select(MemoryEmbedding).where(
                    MemoryEmbedding.claim_id == claim_id,
                    MemoryEmbedding.model == settings.dashscope_embedding_model,
                    MemoryEmbedding.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            old_embeddings = (
                await db.execute(
                    select(MemoryEmbedding).where(
                        MemoryEmbedding.claim_id == claim_id,
                        MemoryEmbedding.model == settings.dashscope_embedding_model,
                        MemoryEmbedding.status == "active",
                    )
                )
            ).scalars().all()
            for old in old_embeddings:
                old.status = "stale"
            db.add(
                MemoryEmbedding(
                    claim_id=claim_id,
                    user_id=str(outbox.user_id),
                    provider="dashscope",
                    model=settings.dashscope_embedding_model,
                    dimension=len(vector),
                    vector_json=vector,
                    content_hash=content_hash,
                    status="active",
                )
            )
        current_outbox.status = "processed"
        current_outbox.processed_at = _utcnow_iso()
        current_outbox.locked_at = None
        current_outbox.last_error = None


async def process_pending_outbox(settings: Settings | None = None) -> dict[str, int]:
    """Process one bounded batch and return operational counters."""

    settings = settings or get_settings()
    counters = {"claimed": 0, "processed": 0, "retried": 0, "dead": 0}
    if settings.memory_v2_mode == "off":
        return counters
    outbox_ids = await _claim_batch(settings.memory_v2_outbox_batch_size)
    counters["claimed"] = len(outbox_ids)
    for outbox_id in outbox_ids:
        outbox = await _load_outbox(outbox_id)
        if outbox is None:
            continue
        try:
            if outbox.event_type == EXTRACTION_REQUESTED:
                await _process_extraction(outbox, settings)
            elif outbox.event_type == EMBEDDING_REQUESTED:
                await _process_embedding(outbox, settings)
            else:  # Defensive; selector currently excludes other event types.
                await _mark_processed(outbox_id)
            counters["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - durable retry boundary
            log.exception("Memory V2 outbox processing failed event_type=%s", outbox.event_type)
            await _mark_failed(outbox_id, exc, settings)
            if int(outbox.attempts or 1) >= settings.memory_v2_outbox_max_attempts:
                counters["dead"] += 1
            else:
                counters["retried"] += 1
    return counters


__all__ = ["process_pending_outbox"]
