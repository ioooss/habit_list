"""发现 / 洞察接口：GET /insights   POST /{id}/confirm   POST /{id}/deny。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from ...db.database import get_db
from ...db.models import Insight, Procedural, RawLedger, Semantic
from ..v1.common import ApiError, BaseSchema, current_user

log = logging.getLogger("habit_list.api.insights")
router = APIRouter()

InsightType = Literal["关联·pattern", "规律·rhythm", "矛盾·tension", "趋势·drift"]


class InsightOut(BaseSchema):
    insight_id: str
    type: InsightType
    text_html: str
    meta: str
    confidence: float
    status: Literal["pending", "confirmed", "denied", "archived"]
    created_at: str
    evidence_count: int


class ListResp(BaseSchema):
    items: list[InsightOut]


@router.get("", response_model=ListResp)
async def list_insights(
    status: Literal["pending", "confirmed", "denied", "all"] = Query("pending"),
    limit: int = Query(60, ge=1, le=300),
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=True) as db:
        stmt = select(Insight).where(Insight.user_id == user_id)
        if status != "all":
            stmt = stmt.where(Insight.status == status)
        rows = (await db.execute(stmt.order_by(Insight.created_at.desc()).limit(limit))).scalars().all()
        items = []
        for r in rows:
            ev = r.evidence_json or {}
            cnt = 0
            if isinstance(ev, dict):
                for v in ev.values():
                    if isinstance(v, list):
                        cnt += len(v)
            items.append(InsightOut(
                insight_id=r.insight_id, type=r.type, text_html=r.text_html,
                meta=r.meta, confidence=float(r.confidence), status=r.status,
                created_at=r.created_at, evidence_count=cnt,
            ))
    return ListResp(items=items)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@router.post("/{iid}/confirm", response_model=InsightOut)
async def confirm(iid: str, payload: Optional[dict] = None, user_id: str = Depends(current_user)):
    now = _now_iso()
    async with get_db(read_only=False) as db:
        ins = (await db.execute(
            select(Insight).where(Insight.insight_id == iid, Insight.user_id == user_id)
        )).scalar_one_or_none()
        if not ins:
            raise ApiError("NOT_FOUND", "insight 不存在", 404)
        ins.status = "confirmed"
        ins.feedback_at = now
        # 映射 semantic / procedural
        ev = ins.evidence_json or {}
        sup = sum(len(v) for v in ev.values() if isinstance(v, list)) or 1
        sem = None
        if ins.type in {"关联·pattern", "规律·rhythm", "趋势·drift", "矛盾·tension"}:
            # 先把 html 里的纯文本抓出来做 fact
            import re as _re
            txt = _re.sub(r"<[^>]+>", "", ins.text_html).strip()
            if txt:
                sem = Semantic(
                    user_id=user_id,
                    category="周期" if ins.type == "规律·rhythm" else ("偏好" if ins.type == "关联·pattern" else "习惯"),
                    fact_text=txt[:500],
                    source_kind="insight_user_confirmed",
                    confidence=1.0,
                    evidence_count=sup,
                    tags_json=[ins.type, "user_confirmed"],
                    status="active",
                )
                db.add(sem)
                await db.flush()
                ins.ref_semantic_id = sem.semantic_id
        # 如用户 payload 说的是改某条 procedural（连续3次确认同一风格），这里留钩子：后续加
        db.add(RawLedger(
            user_id=user_id, entry_type="insight_confirmed",
            ref_ledger_id=ins.ref_semantic_id,
            payload_json={"insight_id": iid, "semantic_id": ins.ref_semantic_id, "payload": payload or {}},
        ))
    async with get_db(read_only=True) as db2:
        fresh = (await db2.execute(select(Insight).where(Insight.insight_id == iid))).scalar_one()
        return InsightOut(
            insight_id=fresh.insight_id, type=fresh.type, text_html=fresh.text_html,
            meta=fresh.meta, confidence=float(fresh.confidence), status=fresh.status,
            created_at=fresh.created_at, evidence_count=0,
        )


@router.post("/{iid}/deny")
async def deny(iid: str, reason: Optional[str] = None, user_id: str = Depends(current_user)):
    now = _now_iso()
    async with get_db(read_only=False) as db:
        ins = (await db.execute(
            select(Insight).where(Insight.insight_id == iid, Insight.user_id == user_id)
        )).scalar_one_or_none()
        if not ins:
            raise ApiError("NOT_FOUND", "insight 不存在", 404)
        ins.status = "denied"
        ins.feedback_at = now
        db.add(RawLedger(
            user_id=user_id, entry_type="insight_denied",
            payload_json={"insight_id": iid, "reason": reason or "", "text_html": ins.text_html},
        ))
    return {"ok": True, "denied": True}
