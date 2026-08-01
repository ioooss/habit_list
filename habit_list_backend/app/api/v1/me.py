"""「它」页：用户资料 + 风格参数（procedural）读取 / 修改。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...db.database import get_db
from ...db.models import Procedural, RawLedger, User
from ..v1.common import ApiError, BaseSchema, current_user

log = logging.getLogger("habit_list.api.me")
router = APIRouter()


class ProcOut(BaseSchema):
    param_key: str
    param_value: dict[str, Any]
    confidence: float
    learned_reason: str
    learned_ev_count: int


class ProfileOut(BaseSchema):
    user_id: str
    created_at: str
    locale: str
    timezone: str
    current_style: str
    settings: dict
    params: list[ProcOut]


class PatchProfileReq(BaseModel):
    locale: Optional[str] = None
    timezone: Optional[str] = None
    current_style: Optional[str] = Field(default=None, max_length=32)
    settings: Optional[dict] = None
    params: Optional[dict[str, Any]] = None  # {reply_speed:{...}, tone_gentle:{value:0.8}} 直接覆盖


@router.get("/profile", response_model=ProfileOut)
async def get_profile(user_id: str = Depends(current_user)):
    async with get_db(read_only=True) as db:
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        if not u:
            raise ApiError("NOT_FOUND", "用户不存在", 404)
        procs = list((await db.execute(
            select(Procedural).where(Procedural.user_id == user_id).order_by(Procedural.param_key.asc())
        )).scalars().all())
    return ProfileOut(
        user_id=u.user_id, created_at=u.created_at, locale=u.locale, timezone=u.timezone,
        current_style=u.current_style, settings=(u.settings_json or {}),
        params=[ProcOut(
            param_key=p.param_key, param_value=(p.param_value_json or {}),
            confidence=float(p.confidence), learned_reason=p.learned_reason or "",
            learned_ev_count=int(p.learned_ev_count or 0),
        ) for p in procs],
    )


@router.patch("/profile", response_model=ProfileOut)
async def patch_profile(req: PatchProfileReq, user_id: str = Depends(current_user)):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with get_db(read_only=False) as db:
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one()
        changes: list[str] = []
        if req.locale is not None:
            u.locale = req.locale
            changes.append(f"locale→{req.locale}")
        if req.timezone is not None:
            u.timezone = req.timezone
            changes.append(f"tz→{req.timezone}")
        if req.current_style is not None:
            u.current_style = req.current_style
            changes.append(f"style→{req.current_style}")
        if req.settings is not None:
            merged = dict(u.settings_json or {})
            merged.update(req.settings)
            u.settings_json = merged
            changes.append("settings_update")
        if req.params:
            for k, v in req.params.items():
                existing = (await db.execute(
                    select(Procedural).where(Procedural.user_id == user_id, Procedural.param_key == k)
                )).scalar_one_or_none()
                if existing is None:
                    db.add(Procedural(
                        user_id=user_id, param_key=k, param_value_json=v or {},
                        confidence=1.0, learned_reason="用户在『它』页显式设置", learned_ev_count=1,
                    ))
                else:
                    existing.param_value_json = v or {}
                    existing.confidence = 1.0
                    existing.updated_at = now
                    existing.learned_reason = "用户在『它』页显式修改"
                    existing.learned_ev_count = int(existing.learned_ev_count or 0) + 1
                changes.append(f"param:{k}")
        db.add(RawLedger(
            user_id=user_id, entry_type="style_param_change",
            payload_json={"changes": changes, "payload": req.model_dump()},
        ))
    return await get_profile(user_id)  # type: ignore[misc]
