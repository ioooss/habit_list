"""Permission-gated administrator audit-event reader."""

from __future__ import annotations

import base64
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select

from ....admin.models import AdminAuditEvent
from ....admin.service import AdminPrincipal
from ....db.database import get_db
from ...v1.common import ApiError
from .common import require_permission

router = APIRouter(prefix="/audit-events")
AuditReader = Annotated[
    AdminPrincipal,
    Depends(require_permission("audit.read")),
]


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditEventOut(StrictSchema):
    audit_id: str
    actor_admin_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str | None
    metadata: dict[str, Any]
    created_at: str


class AuditListOut(StrictSchema):
    items: list[AuditEventOut]
    next_cursor: str | None


def _encode_cursor(created_at: str, audit_id: str) -> str:
    raw = f"{created_at}|{audit_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, audit_id = base64.urlsafe_b64decode(padded).decode("utf-8").split("|", 1)
    except Exception as exc:  # noqa: BLE001 - normalized public error
        raise ApiError("INVALID_CURSOR", "分页游标无效", 422) from exc
    if not created_at or not audit_id:
        raise ApiError("INVALID_CURSOR", "分页游标无效", 422)
    return created_at, audit_id


@router.get("", response_model=AuditListOut)
async def list_audit_events(
    _principal: AuditReader,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
) -> AuditListOut:
    stmt = select(AdminAuditEvent)
    if cursor:
        created_at, audit_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                AdminAuditEvent.created_at < created_at,
                and_(
                    AdminAuditEvent.created_at == created_at,
                    AdminAuditEvent.audit_id < audit_id,
                ),
            )
        )
    async with get_db(read_only=True) as db:
        rows = list(
            (
                await db.execute(
                    stmt.order_by(
                        AdminAuditEvent.created_at.desc(),
                        AdminAuditEvent.audit_id.desc(),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor(rows[-1].created_at, rows[-1].audit_id) if has_more and rows else None
    )
    return AuditListOut(
        items=[
            AuditEventOut(
                audit_id=row.audit_id,
                actor_admin_id=row.actor_admin_id,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                outcome=row.outcome,
                request_id=row.request_id,
                metadata=row.metadata_json or {},
                created_at=row.created_at,
            )
            for row in rows
        ],
        next_cursor=next_cursor,
    )


__all__ = ["router"]
