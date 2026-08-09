"""「它」页：用户资料 + 风格参数（procedural）读取 / 修改。"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ...db.database import get_db
from ...db.memory_models import (
    MemoryClaim,
    MemoryDeletionTombstone,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryRelation,
    MemoryRevision,
    OutboxEvent,
    UserEvent,
)
from ...db.models import (
    Episodic,
    GraphEdge,
    GraphNode,
    Insight,
    MediaAsset,
    Memo,
    MomentInteraction,
    Procedural,
    RawLedger,
    Semantic,
    User,
    Working,
)
from ...memory.situation import is_known_timezone
from ..v1.common import ApiError, BaseSchema, current_user

log = logging.getLogger("habit_list.api.me")
router = APIRouter()


class ProcOut(BaseSchema):
    param_key: str
    param_value: dict[str, Any]
    confidence: float
    learned_reason: str
    learned_ev_count: int


class FeedbackOut(BaseSchema):
    """User-visible proof that a correction changed future memory behavior."""

    rejected_memory_count: int = 0
    latest_rejection_at: str | None = None


class ProfileOut(BaseSchema):
    user_id: str
    created_at: str
    locale: str
    timezone: str
    current_style: str
    settings: dict
    params: list[ProcOut]
    feedback: FeedbackOut = Field(default_factory=FeedbackOut)


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
        rejection_count, latest_rejection_at = (
            await db.execute(
                select(
                    func.count(MemoryRevision.revision_id),
                    func.max(MemoryRevision.created_at),
                )
                .select_from(MemoryRevision)
                .join(MemoryClaim, MemoryClaim.claim_id == MemoryRevision.claim_id)
                .where(
                    MemoryClaim.user_id == user_id,
                    MemoryClaim.deleted_at.is_(None),
                    MemoryRevision.actor_type == "user",
                    MemoryRevision.action == "reject",
                )
            )
        ).one()
    return ProfileOut(
        user_id=u.user_id, created_at=u.created_at, locale=u.locale, timezone=u.timezone,
        current_style=u.current_style, settings=(u.settings_json or {}),
        params=[ProcOut(
            param_key=p.param_key, param_value=(p.param_value_json or {}),
            confidence=float(p.confidence), learned_reason=p.learned_reason or "",
            learned_ev_count=int(p.learned_ev_count or 0),
        ) for p in procs],
        feedback=FeedbackOut(
            rejected_memory_count=int(rejection_count or 0),
            latest_rejection_at=str(latest_rejection_at) if latest_rejection_at else None,
        ),
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
            # 时区不再只是一份档案：生成路径靠它算出用户本地几点，深夜档因此成立
            # （声音基线 §4）。存下一个 zoneinfo 认不出的名字，等于让它以后不知道几点。
            if not is_known_timezone(req.timezone):
                raise HTTPException(status_code=400, detail="unknown_timezone")
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


# ============ 隐私 / 记忆掌控（P5/P7：可暂停、可携带、可删除） ============


class PrivacyStateOut(BaseSchema):
    memory_paused: bool = False
    paused_at: Optional[str] = None
    formation_paused: bool = False
    formation_paused_at: Optional[str] = None
    onboarded_at: Optional[str] = None


class PrivacyPatchReq(BaseModel):
    memory_paused: Optional[bool] = None
    # Baseline P7: a user may stop new terrain from forming while keeping the
    # companion, life records and existing terrain fully working.  This is
    # narrower than ``memory_paused`` on purpose.
    formation_paused: Optional[bool] = None


def _row_to_dict(row: Any) -> dict[str, Any]:
    """把 ORM 实例转成 {column: value}，跳过 SQLAlchemy 内部字段。"""
    if row is None:
        return {}
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


@router.get("/privacy", response_model=PrivacyStateOut)
async def get_privacy(user_id: str = Depends(current_user)):
    async with get_db(read_only=True) as db:
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        if not u:
            raise ApiError("NOT_FOUND", "用户不存在", 404)
        s = u.settings_json or {}
        return PrivacyStateOut(
            memory_paused=bool(s.get("memory_paused")),
            paused_at=s.get("memory_paused_at"),
            formation_paused=bool(s.get("memory_formation_paused")),
            formation_paused_at=s.get("memory_formation_paused_at"),
            onboarded_at=s.get("onboarded_at"),
        )


@router.patch("/privacy", response_model=PrivacyStateOut)
async def patch_privacy(req: PrivacyPatchReq, user_id: str = Depends(current_user)):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with get_db(read_only=False) as db:
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one()
        merged = dict(u.settings_json or {})
        if req.memory_paused is not None:
            merged["memory_paused"] = bool(req.memory_paused)
            merged["memory_paused_at"] = now if req.memory_paused else None
        if req.formation_paused is not None:
            merged["memory_formation_paused"] = bool(req.formation_paused)
            merged["memory_formation_paused_at"] = now if req.formation_paused else None
        u.settings_json = merged
        db.add(RawLedger(
            user_id=user_id, entry_type="privacy_change",
            payload_json={
                "memory_paused": req.memory_paused,
                "formation_paused": req.formation_paused,
            },
        ))
    return await get_privacy(user_id)  # type: ignore[arg-type]


@router.get("/data")
async def export_my_data(user_id: str = Depends(current_user)):
    """导出属于该用户的全部业务数据（JSON）。

    不包含内部 OutboxEvent / 检索轨迹（这些是技术日志，不属于"我的数据"）。
    不包含其他用户的数据。
    """
    async with get_db(read_only=True) as db:
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        if not u:
            raise ApiError("NOT_FOUND", "用户不存在", 404)

        async def all_of(model):
            rows = (await db.execute(select(model).where(model.user_id == user_id))).scalars().all()
            return [_row_to_dict(r) for r in rows]

        claims = (await db.execute(select(MemoryClaim).where(MemoryClaim.user_id == user_id))).scalars().all()
        claim_ids = [c.claim_id for c in claims]
        evidence_rows = []
        revision_rows = []
        embedding_rows = []
        relation_rows = []
        if claim_ids:
            evidence_rows = (await db.execute(
                select(MemoryEvidence).where(MemoryEvidence.claim_id.in_(claim_ids))
            )).scalars().all()
            revision_rows = (await db.execute(
                select(MemoryRevision).where(MemoryRevision.claim_id.in_(claim_ids))
            )).scalars().all()
            embedding_rows = (await db.execute(
                select(MemoryEmbedding).where(MemoryEmbedding.claim_id.in_(claim_ids))
            )).scalars().all()
            relation_rows = (await db.execute(
                select(MemoryRelation).where(MemoryRelation.claim_id.in_(claim_ids))
            )).scalars().all()

        export = {
            "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "user": {
                "user_id": u.user_id,
                "created_at": u.created_at,
                "locale": u.locale,
                "timezone": u.timezone,
                "current_style": u.current_style,
                "settings": u.settings_json or {},
            },
            "procedural_preferences": await all_of(Procedural),
            "moments": await all_of(MomentInteraction),
            "episodic": await all_of(Episodic),
            "memos": await all_of(Memo),
            "working_session_messages": await all_of(Working),
            "semantic_memories": await all_of(Semantic),
            "insights": await all_of(Insight),
            "media_assets": await all_of(MediaAsset),
            "graph_nodes": await all_of(GraphNode),
            "graph_edges": await all_of(GraphEdge),
            "terrain_claims": [_row_to_dict(c) for c in claims],
            "terrain_evidence": [_row_to_dict(r) for r in evidence_rows],
            "terrain_revisions": [_row_to_dict(r) for r in revision_rows],
            "terrain_embeddings": [_row_to_dict(r) for r in embedding_rows],
            "terrain_relations": [_row_to_dict(r) for r in relation_rows],
        }
        return export


@router.delete("/data")
async def delete_my_data(
    confirmation: str = "",
    user_id: str = Depends(current_user),
):
    """永久删除该用户的全部业务数据。

    安全：必须通过 query string `?confirmation=permanently%20delete` 二次确认，
    防止误触。删除后写 tombstone（只存 user_id 的 SHA-256，不存内容或 ID），
    用于幂等和审计，但已无法从 tombstone 反查出原始用户。
    """
    if confirmation != "permanently delete":
        raise ApiError(
            "CONFIRMATION_REQUIRED",
            "永久删除需要确认参数 confirmation=permanently delete",
            400,
        )

    async with get_db(read_only=False) as db:
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        if not u:
            raise ApiError("NOT_FOUND", "用户不存在", 404)

        # 按外键依赖顺序清掉该用户的业务数据
        claims = (await db.execute(select(MemoryClaim).where(MemoryClaim.user_id == user_id))).scalars().all()
        claim_ids = [c.claim_id for c in claims]
        if claim_ids:
            for model, col in [
                (MemoryEvidence, MemoryEvidence.claim_id),
                (MemoryRevision, MemoryRevision.claim_id),
                (MemoryEmbedding, MemoryEmbedding.claim_id),
                (MemoryRelation, MemoryRelation.claim_id),
            ]:
                await db.execute(delete(model).where(col.in_(claim_ids)))
            await db.execute(delete(MemoryClaim).where(MemoryClaim.user_id == user_id))

        for model in [
            UserEvent,
            OutboxEvent,
            MomentInteraction,
            Episodic,
            Memo,
            Working,
            Semantic,
            Insight,
            MediaAsset,
            GraphNode,
            GraphEdge,
            Procedural,
            RawLedger,
        ]:
            await db.execute(delete(model).where(model.user_id == user_id))

        # 写无内容墓碑（只存 user_id 的单向 hash），用于幂等/审计
        resource_hash = hashlib.sha256(f"user:{user_id}".encode("utf-8")).hexdigest()
        existing_tomb = (await db.execute(
            select(MemoryDeletionTombstone).where(MemoryDeletionTombstone.resource_hash == resource_hash)
        )).scalar_one_or_none()
        if existing_tomb is None:
            db.add(MemoryDeletionTombstone(
                resource_type="user_account",
                resource_hash=resource_hash,
                actor_type="user",
            ))

        # 用户行本身软删除（status=deleted + deleted_at），保留 7 天以便撤销
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        u.status = "deleted"
        u.deleted_at = now
        u.settings_json = {"deleted_at": now}

    return {"ok": True, "deleted_at": now}
