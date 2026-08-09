"""Legacy Episodic compatibility API for migrated pebble data.

The current product does not expose a memory-river entry point. Keep this
surface only for old clients, data migration and user-controlled access to
already stored records; new features belong under ``/moments`` or ``/terrain``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...db.database import get_db
from ...db.models import Episodic, RawLedger
from ...memory.forgetting import land as land_memory
from ...moments.service import delete_moment_cascade
from ..v1.common import ApiError, BaseSchema, current_user, request_id

log = logging.getLogger("habit_list.api.pebbles")
router = APIRouter()

Kind = Literal["confide", "memo", "life_fragment"]


class PebbleOut(BaseSchema):
    episodic_id: str
    created_at: str
    source: str
    kind: Kind
    kind_fixed_from: str | None = None
    summary_1line: str
    emotion: str
    raw_user_text: str
    raw_assistant_text: str | None = None
    retrieval_weight: float


class DayGroup(BaseSchema):
    day_label: str
    date_key: str
    count: int
    items: list[PebbleOut]


class PebbleListResp(BaseSchema):
    total: int
    groups: list[DayGroup]
    filter_kind: str | None = None


class PebblePatchReq(BaseModel):
    summary_1line: str | None = Field(default=None, max_length=256)
    emotion: str | None = Field(default=None, max_length=8)
    kind: Kind | None = None
    raw_user_text: str | None = None
    raw_assistant_text: str | None = None
    land_it: bool = False  # 顺便捞起（权重回弹）


@router.get("", response_model=PebbleListResp)
async def list_pebbles(
    kind: Kind | None = Query(default=None),
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=120, ge=1, le=500),
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=True) as db:
        stmt = (
            select(Episodic)
            .where(Episodic.user_id == user_id, Episodic.status == "active")
            .order_by(Episodic.created_at.desc())
        )
        if kind:
            stmt = stmt.where(Episodic.kind == kind)
        if q.strip():
            kw = f"%{q.strip()}%"
            stmt = stmt.where(
                (Episodic.summary_1line.like(kw))
                | (Episodic.raw_user_text.like(kw))
                | (Episodic.raw_assistant_text.like(kw) if False else Episodic.raw_assistant_text.like(kw))
            )
        rows = list((await db.execute(stmt.limit(limit))).scalars().all())

    buckets: dict[str, list[PebbleOut]] = {}
    order: list[str] = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r.created_at.replace("Z", "+00:00"))
            key = dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            key = r.created_at[:10]
        p = PebbleOut.model_validate({c.name: getattr(r, c.name) for c in Episodic.__table__.columns})
        buckets.setdefault(key, [])
        buckets[key].append(p)
        if key not in order:
            order.append(key)
    groups = [
        DayGroup(day_label=label_of(order[i], buckets), date_key=order[i], count=len(buckets[order[i]]), items=buckets[order[i]])
        for i in range(len(order))
    ]
    # 上面的 label_of 是占位，再重算一遍 label 更干净：
    for g, key in zip(groups, order):
        g.day_label = _day_label(key)
    return PebbleListResp(total=len(rows), groups=groups, filter_kind=kind)


def _day_label(key: str) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    today = datetime.now(UTC).date()
    try:
        d = datetime.strptime(key, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return key
    m_d = f"{d.month}/{d.day}"
    wd = weekdays[d.weekday()]
    diff = (today - d).days
    if diff == 0:
        return f"今天 · {m_d}  {wd}"
    if diff == 1:
        return f"昨天 · {m_d}  {wd}"
    return f"{m_d}  {wd}"


def label_of(key, buckets):  # pragma: no cover - 备用
    return _day_label(key)


@router.patch("/{pid}", response_model=PebbleOut)
async def patch_pebble(pid: str, req: PebblePatchReq, user_id: str = Depends(current_user)):
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with get_db(read_only=False) as db:
        p = (await db.execute(
            select(Episodic).where(Episodic.episodic_id == pid, Episodic.user_id == user_id)
        )).scalar_one_or_none()
        if not p:
            raise ApiError("NOT_FOUND", "石子不存在", 404)
        changes = []
        if req.summary_1line is not None:
            p.summary_1line = req.summary_1line
            changes.append("summary_1line")
        if req.emotion is not None:
            p.emotion = req.emotion
            changes.append("emotion")
        if req.kind is not None and req.kind != p.kind:
            p.kind_fixed_from = p.kind_fixed_from or p.kind
            p.kind_fixed_at = now
            p.kind = req.kind
            changes.append(f"kind:{p.kind_fixed_from}→{req.kind}")
        if req.raw_user_text is not None:
            p.raw_user_text = req.raw_user_text
            changes.append("raw_user_text")
        if req.raw_assistant_text is not None:
            p.raw_assistant_text = req.raw_assistant_text
            changes.append("raw_assistant_text")
        if changes:
            db.add(RawLedger(
                user_id=user_id, entry_type="pebble_category_fix" if any(c.startswith("kind:") for c in changes) else "pebble_edit",
                ref_ledger_id=(p.ref_ledger_ids_json or ["-"])[0] if p.ref_ledger_ids_json else None,
                payload_json={"episodic_id": pid, "changes": changes},
            ))
    if req.land_it:
        await land_memory(user_id, episodic_id=pid)
    async with get_db(read_only=True) as db2:
        fresh = (await db2.execute(select(Episodic).where(Episodic.episodic_id == pid))).scalar_one()
        return PebbleOut.model_validate({c.name: getattr(fresh, c.name) for c in Episodic.__table__.columns})


@router.delete("/{pid}")
async def archive_pebble(
    pid: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    async with get_db(read_only=False) as db:
        p = (await db.execute(
            select(Episodic).where(Episodic.episodic_id == pid, Episodic.user_id == user_id)
        )).scalar_one_or_none()
        if not p:
            raise ApiError("NOT_FOUND", "石子不存在", 404)
        if p.kind == "life_fragment":
            # Fragments need the full closed loop: thread interactions,
            # queued responses, and terrain-derived events must follow deletion.
            await delete_moment_cascade(
                db, user_id=user_id, moment_id=pid, request_id=req_id
            )
            return {"ok": True, "archived": True}
        p.status = "archived"
        db.add(RawLedger(
            user_id=user_id, entry_type="pebble_archive",
            payload_json={"episodic_id": pid, "summary": p.summary_1line},
        ))
    return {"ok": True, "archived": True}
