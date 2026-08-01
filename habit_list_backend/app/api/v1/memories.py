"""User-owned Memory V2 APIs: inspect, correct, control, and delete."""
from __future__ import annotations

import base64
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, or_, select

from ...db.database import get_db
from ...db.memory_models import MemoryClaim, MemoryEvidence, UserEvent
from ...memory_v2.domain import MemoryCategory
from ...memory_v2.service import (
    correct_claim,
    get_claim_for_user,
    permanently_delete_claim,
    transition_claim,
)
from .common import ApiError, BaseSchema, current_user, request_id

router = APIRouter()


class MemoryOut(BaseSchema):
    claim_id: str
    claim_type: str
    category: str
    claim_text: str
    source_type: str
    confidence: float
    user_status: str
    sensitivity: str
    valid_from: str | None
    valid_to: str | None
    evidence_count: int
    supersedes_claim_id: str | None
    pinned: bool
    allow_proactive: bool
    importance: float
    created_at: str
    updated_at: str


class MemoryListOut(BaseSchema):
    items: list[MemoryOut]
    next_cursor: str | None = None


class MemoryEvidenceOut(BaseSchema):
    evidence_id: str
    event_id: str
    evidence_role: str
    excerpt_text: str
    occurred_at: str
    source: str
    mode: str


class MemoryPatchReq(BaseModel):
    claim_text: str | None = Field(default=None, min_length=2, max_length=500)
    category: MemoryCategory | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=128)
    predicate: str | None = Field(default=None, min_length=1, max_length=128)
    object_value: str | None = Field(default=None, min_length=1, max_length=500)
    valid_from: str | None = None
    valid_to: str | None = None
    pinned: bool | None = None
    allow_proactive: bool | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个要修改的字段")
        return self


def _memory_out(claim: MemoryClaim) -> MemoryOut:
    return MemoryOut(
        claim_id=claim.claim_id,
        claim_type=claim.claim_type,
        category=claim.category,
        claim_text=claim.claim_text,
        source_type=claim.source_type,
        confidence=round(float(claim.confidence), 4),
        user_status=claim.user_status,
        sensitivity=claim.sensitivity,
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
        evidence_count=int(claim.evidence_count or 0),
        supersedes_claim_id=claim.supersedes_claim_id,
        pinned=bool(claim.pinned),
        allow_proactive=bool(claim.allow_proactive),
        importance=float(claim.importance or 0.0),
        created_at=claim.created_at,
        updated_at=claim.updated_at,
    )


def _encode_cursor(updated_at: str, claim_id: str) -> str:
    raw = json.dumps({"updated_at": updated_at, "claim_id": claim_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        updated_at = str(obj["updated_at"])
        claim_id = str(obj["claim_id"])
        if not updated_at or not claim_id:
            raise ValueError
        return updated_at, claim_id
    except Exception as exc:  # noqa: BLE001
        raise ApiError("INVALID_CURSOR", "分页游标无效", status.HTTP_400_BAD_REQUEST) from exc


@router.get("", response_model=MemoryListOut)
async def list_memories(
    user_id: str = Depends(current_user),
    status_filter: Annotated[
        Literal["current", "usable", "proposed", "hidden", "rejected", "all"],
        Query(alias="status"),
    ] = "current",
    category: MemoryCategory | None = None,
    q: str | None = Query(default=None, max_length=120),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=30, ge=1, le=100),
):
    async with get_db(read_only=True) as db:
        stmt = select(MemoryClaim).where(
            MemoryClaim.user_id == user_id,
            MemoryClaim.deleted_at.is_(None),
        )
        if status_filter == "current":
            stmt = stmt.where(
                MemoryClaim.user_status.in_(["proposed", "confirmed", "corrected"])
            )
        elif status_filter == "usable":
            stmt = stmt.where(MemoryClaim.user_status.in_(["confirmed", "corrected"]))
        elif status_filter != "all":
            stmt = stmt.where(MemoryClaim.user_status == status_filter)
        if category is not None:
            stmt = stmt.where(MemoryClaim.category == category.value)
        if q and q.strip():
            query = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    MemoryClaim.claim_text.ilike(query),
                    MemoryClaim.object_value.ilike(query),
                )
            )
        if cursor:
            updated_at, claim_id = _decode_cursor(cursor)
            stmt = stmt.where(
                or_(
                    MemoryClaim.updated_at < updated_at,
                    and_(
                        MemoryClaim.updated_at == updated_at,
                        MemoryClaim.claim_id < claim_id,
                    ),
                )
            )
        rows = list(
            (
                await db.execute(
                    stmt.order_by(MemoryClaim.updated_at.desc(), MemoryClaim.claim_id.desc())
                    .limit(limit + 1)
                )
            ).scalars().all()
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1].updated_at, rows[-1].claim_id) if has_more else None
    return MemoryListOut(items=[_memory_out(row) for row in rows], next_cursor=next_cursor)


@router.get("/{claim_id}", response_model=MemoryOut)
async def get_memory(claim_id: str, user_id: str = Depends(current_user)):
    async with get_db(read_only=True) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("MEMORY_NOT_FOUND", "记忆不存在", status.HTTP_404_NOT_FOUND)
        return _memory_out(claim)


@router.get("/{claim_id}/evidence", response_model=list[MemoryEvidenceOut])
async def get_memory_evidence(claim_id: str, user_id: str = Depends(current_user)):
    async with get_db(read_only=True) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("MEMORY_NOT_FOUND", "记忆不存在", status.HTTP_404_NOT_FOUND)
        rows = (
            await db.execute(
                select(MemoryEvidence, UserEvent)
                .join(UserEvent, UserEvent.event_id == MemoryEvidence.event_id)
                .where(
                    MemoryEvidence.claim_id == claim_id,
                    UserEvent.user_id == user_id,
                    UserEvent.status == "active",
                )
                .order_by(UserEvent.occurred_at.desc(), MemoryEvidence.evidence_id.desc())
            )
        ).all()
        return [
            MemoryEvidenceOut(
                evidence_id=evidence.evidence_id,
                event_id=event.event_id,
                evidence_role=evidence.evidence_role,
                excerpt_text=evidence.excerpt_text,
                occurred_at=event.occurred_at,
                source=event.source,
                mode=event.mode,
            )
            for evidence, event in rows
        ]


@router.patch("/{claim_id}", response_model=MemoryOut)
async def patch_memory(
    claim_id: str,
    body: MemoryPatchReq,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    changes = body.model_dump(exclude_unset=True)
    if isinstance(changes.get("category"), MemoryCategory):
        changes["category"] = changes["category"].value
    async with get_db(read_only=False) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("MEMORY_NOT_FOUND", "记忆不存在", status.HTTP_404_NOT_FOUND)
        try:
            await correct_claim(
                db,
                claim=claim,
                changes=changes,
                request_id=req_id,
            )
        except ValueError as exc:
            raise ApiError("INVALID_MEMORY_UPDATE", str(exc)) from exc
        await db.flush()
        return _memory_out(claim)


async def _transition(
    claim_id: str,
    action: str,
    user_id: str,
    req_id: str,
) -> MemoryOut:
    async with get_db(read_only=False) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("MEMORY_NOT_FOUND", "记忆不存在", status.HTTP_404_NOT_FOUND)
        try:
            await transition_claim(db, claim=claim, action=action, request_id=req_id)
        except ValueError as exc:
            raise ApiError(
                "INVALID_MEMORY_TRANSITION",
                "当前记忆状态不支持这个操作",
                status.HTTP_409_CONFLICT,
            ) from exc
        await db.flush()
        return _memory_out(claim)


@router.post("/{claim_id}/confirm", response_model=MemoryOut)
async def confirm_memory(
    claim_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    return await _transition(claim_id, "confirm", user_id, req_id)


@router.post("/{claim_id}/reject", response_model=MemoryOut)
async def reject_memory(
    claim_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    return await _transition(claim_id, "reject", user_id, req_id)


@router.post("/{claim_id}/hide", response_model=MemoryOut)
async def hide_memory(
    claim_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    return await _transition(claim_id, "hide", user_id, req_id)


@router.post("/{claim_id}/restore", response_model=MemoryOut)
async def restore_memory(
    claim_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    return await _transition(claim_id, "restore", user_id, req_id)


@router.delete("/{claim_id}")
async def delete_memory(
    claim_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    async with get_db(read_only=False) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("MEMORY_NOT_FOUND", "记忆不存在", status.HTTP_404_NOT_FOUND)
        tombstone = await permanently_delete_claim(
            db,
            claim=claim,
            request_id=req_id,
        )
        return {
            "ok": True,
            "deletion_id": tombstone.tombstone_id,
            "completed": True,
        }


__all__ = ["router"]
