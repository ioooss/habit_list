"""备忘接口：/memos  CRUD / detect / batch_done。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...db.database import get_db
from ...db.models import Episodic, Memo, RawLedger, uuid7
from ...memory import memo_utils
from ..v1.common import ApiError, BaseSchema, current_user

log = logging.getLogger("habit_list.api.memos")
router = APIRouter()

Importance = Literal["red", "yellow", "green"]
Status = Literal["pending", "done", "overdue_stale", "archived"]
FilterKey = Literal["all", "red", "yellow", "green", "done"]

GROUP_LABEL = {
    "overdue": "已逾期 / 紧急",
    "today": "今 天",
    "week": "本 周",
    "later": "之 后",
    "done": "已 完 成",
}
GROUP_ORDER = ["overdue", "today", "week", "later", "done"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _group_of(m: Memo) -> str:
    if m.status == "done":
        return "done"
    if m.importance == "red" and (m.due_offset_days <= 0 or re.search(r"今天|今晚|现在|马上|立刻", m.due_text or "")):
        return "overdue"
    if m.status == "overdue_stale":
        return "overdue"
    if m.due_offset_days == 0 or re.search(r"今天|今晚|下午|上午|早上|晚上|中午|凌晨|现在|马上|立刻", m.due_text or ""):
        return "today"
    if 1 <= m.due_offset_days <= 7:
        return "week"
    return "later"


# -------- Schemas --------
class MemoDetectReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class MemoDetectResp(BaseSchema):
    hit: bool
    due_text: str
    importance: Importance
    offset: int
    clean_text: str


class MemoOut(BaseSchema):
    memo_id: str
    text: str
    clean_text: str
    due_text: str
    due_iso: Optional[str] = None
    due_offset_days: int
    importance: Importance
    source: str
    status: Status
    group: str
    created_at: str
    linked_episodic_id: Optional[str] = None


class MemoListResp(BaseSchema):
    stats: dict
    groups: list[dict]
    items: list[MemoOut]


class MemoCreateReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    due_text: str = ""
    importance: Importance = "green"
    due_offset_days: Optional[int] = None


class MemoPatchReq(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    due_text: Optional[str] = None
    importance: Optional[Importance] = None
    due_offset_days: Optional[int] = None
    status: Optional[Status] = None


class BatchDoneReq(BaseModel):
    memo_ids: list[str] = Field(..., min_length=1, max_length=200)


# -------- Routes --------
@router.post("/detect", response_model=MemoDetectResp)
def detect(req: MemoDetectReq):
    r = memo_utils.detect_memo(req.text)
    return MemoDetectResp(
        hit=r.hit, due_text=r.due_text, importance=r.importance, offset=r.offset, clean_text=r.clean_text or req.text,
    )


@router.get("", response_model=MemoListResp)
async def list_memos(
    f: FilterKey = Query("all", alias="filter"),
    q: str = Query("", alias="q", max_length=120),
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=True) as db:
        stmt = select(Memo).where(Memo.user_id == user_id)
        if f == "done":
            stmt = stmt.where(Memo.status == "done")
        elif f in {"red", "yellow", "green"}:
            stmt = stmt.where(Memo.importance == f, Memo.status != "archived")
        else:  # all: 只看未完成 + overdue_stale（不默认显示 archived）
            stmt = stmt.where(Memo.status.in_(["pending", "overdue_stale"]))
        if q.strip():
            kw = f"%{q.strip()}%"
            stmt = stmt.where((Memo.text.like(kw)) | (Memo.clean_text.like(kw)) | (Memo.due_text.like(kw)))
        rows = list((await db.execute(stmt.order_by(Memo.created_at.desc()))).scalars().all())

    # group sort + stat
    today_cnt = sum(1 for m in rows if _group_of(m) == "today")
    todo_cnt = sum(1 for m in rows if m.status in {"pending", "overdue_stale"})
    done_cnt = 0
    if f == "done":
        done_cnt = len(rows)
    else:
        # 统计一下总的 done_cnt（不依赖当前 filter）
        async with get_db(read_only=True) as db2:
            total_done = (await db2.execute(
                select(Memo).where(Memo.user_id == user_id, Memo.status == "done")
            )).scalars().all()
            done_cnt = len(list(total_done))

    # sort
    rows.sort(key=lambda m: (GROUP_ORDER.index(_group_of(m)), m.due_offset_days or 999,
                              -1 * (0 if m.status == "done" else 1),
                              _parse_created_ts(m.created_at)))
    items_out = [MemoOut.model_validate({**_memo_dict(m), "group": _group_of(m)}) for m in rows]
    groups = [{"key": g, "label": GROUP_LABEL[g]} for g in GROUP_ORDER if any(_group_of(m) == g for m in rows)]
    return MemoListResp(
        stats={"today": today_cnt, "todo": todo_cnt, "done": done_cnt},
        groups=groups,
        items=items_out,
    )


def _parse_created_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _memo_dict(m: Memo) -> dict:
    return {
        "memo_id": m.memo_id, "text": m.text, "clean_text": m.clean_text or m.text,
        "due_text": m.due_text, "due_iso": m.due_iso, "due_offset_days": m.due_offset_days,
        "importance": m.importance, "source": m.source, "status": m.status,
        "created_at": m.created_at, "linked_episodic_id": m.linked_episodic_id,
    }


@router.post("", response_model=MemoOut)
async def create_memo(req: MemoCreateReq, user_id: str = Depends(current_user)):
    now = _now_iso()
    detect = memo_utils.detect_memo(req.text) if not req.due_text else None
    due_text = req.due_text or (detect.due_text if detect else "") or "（没说时间，你自己定）"
    importance = req.importance
    if not req.due_text and detect and req.importance == "green" and detect.importance != "green":
        importance = detect.importance
    offset = req.due_offset_days if req.due_offset_days is not None else (
        detect.offset if detect else memo_utils.guess_offset(due_text, req.text)
    )
    clean = req.text.strip()
    if detect:
        clean = detect.clean_text or clean
    async with get_db(read_only=False) as db:
        m = Memo(
            memo_id=str(uuid7()),
            user_id=user_id, text=req.text, clean_text=clean,
            due_text=due_text, due_offset_days=offset, importance=importance,
            source="memo_page_manual", status="pending",
        )
        db.add(m)
        await db.flush()
        ep = Episodic(
            user_id=user_id, created_at=now, source="memo_page", kind="memo",
            summary_1line=clean[:120], emotion="-", entities_json=[],
            raw_user_text=req.text, raw_assistant_text=None,
            ref_ledger_ids_json=[],
        )
        db.add(ep)
        await db.flush()
        m.linked_episodic_id = ep.episodic_id
        db.add(RawLedger(
            user_id=user_id, entry_type="memo_state_change",
            payload_json={"op": "create", "memo_id": m.memo_id, "source": "memo_page_manual"},
        ))
    async with get_db(read_only=True) as db2:
        fresh = (await db2.execute(select(Memo).where(Memo.memo_id == m.memo_id))).scalar_one()
        return MemoOut.model_validate({**_memo_dict(fresh), "group": _group_of(fresh)})


@router.patch("/{mid}", response_model=MemoOut)
async def patch_memo(mid: str, req: MemoPatchReq, user_id: str = Depends(current_user)):
    async with get_db(read_only=False) as db:
        m = (await db.execute(select(Memo).where(Memo.memo_id == mid, Memo.user_id == user_id))).scalar_one_or_none()
        if not m:
            raise ApiError("NOT_FOUND", "备忘不存在", 404)
        changed: list[str] = []
        if req.text is not None:
            m.text = req.text
            if not m.clean_text or len(req.text) < len(m.clean_text):
                m.clean_text = req.text
            changed.append("text")
        if req.due_text is not None:
            m.due_text = req.due_text
            m.due_offset_days = memo_utils.guess_offset(req.due_text, m.text)
            changed.append("due_text")
        if req.importance is not None:
            m.importance = req.importance
            changed.append("importance")
        if req.status is not None:
            if m.status != req.status:
                m.status = req.status
                m.status_changed_at = _now_iso()
                changed.append(f"status:{req.status}")
        if req.due_offset_days is not None:
            m.due_offset_days = req.due_offset_days
            changed.append("due_offset_days")
        if changed:
            db.add(RawLedger(
                user_id=user_id, entry_type="memo_state_change", ref_ledger_id=m.linked_ledger_id,
                payload_json={"op": "patch", "memo_id": mid, "changes": changed},
            ))
    async with get_db(read_only=True) as db2:
        fresh = (await db2.execute(select(Memo).where(Memo.memo_id == mid))).scalar_one()
        return MemoOut.model_validate({**_memo_dict(fresh), "group": _group_of(fresh)})


@router.post("/batch_done")
async def batch_done(req: BatchDoneReq, user_id: str = Depends(current_user)):
    now = _now_iso()
    ids = list(dict.fromkeys(req.memo_ids))
    async with get_db(read_only=False) as db:
        rows = (await db.execute(
            select(Memo).where(Memo.memo_id.in_(tuple(ids) if ids else ("-",)), Memo.user_id == user_id)
        )).scalars().all()
        done = 0
        for m in rows:
            if m.status != "done":
                m.status = "done"
                m.status_changed_at = now
                done += 1
        db.add(RawLedger(
            user_id=user_id, entry_type="memo_state_change",
            payload_json={"op": "batch_done", "count": done, "ids": [m.memo_id for m in rows]},
        ))
    return {"ok": True, "done_count": done, "total": len(ids)}
