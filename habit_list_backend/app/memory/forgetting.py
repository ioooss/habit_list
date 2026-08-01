"""艾宾浩斯衰减：越久没被「捞起」的石子/事实，retrieval_weight 按时间衰减，但永不到 0。

公式（简单但够）：
    w_new = clamp( w_old * FORGET_STRENGTH ** (days_since_last_landed / 3), MIN_WEIGHT, 1.0 )
- 捞起（用户点过这条石子/编辑过/确认过洞察）→ 立刻把 last_landed_at 写到现在 + w 回弹到 1.0
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.models import Episodic, Semantic

log = logging.getLogger("habit_list.memory.forgetting")

MIN_WEIGHT = 0.12
LAND_REBOUND = 1.0


def _days_between(iso_now: str, iso_then: Optional[str]) -> float:
    if not iso_then:
        return 30.0
    try:
        then = datetime.fromisoformat(iso_then.replace("Z", "+00:00"))
        now = datetime.fromisoformat(iso_now.replace("Z", "+00:00"))
        return max(0.0, (now - then).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001
        return 30.0


async def apply_ebbinghaus(settings: Optional[Settings] = None) -> dict[str, int]:
    """System2 每天 04:30 触发一次；返回 {episodic_updated, semantic_updated}。"""
    settings = settings or get_settings()
    strength = float(settings.forget_strength)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ep_u = 0
    sem_u = 0
    async with get_db(read_only=False) as db:
        # 1) Episodic
        rows = (await db.execute(
            select(Episodic.episodic_id, Episodic.last_landed_at, Episodic.retrieval_weight)
            .where(Episodic.status == "active")
        )).all()
        for r in rows:
            d = _days_between(now, r.last_landed_at)
            if d < 2:
                continue
            factor = strength ** (d / 3.0)
            new_w = max(MIN_WEIGHT, min(LAND_REBOUND, float(r.retrieval_weight or 1.0) * factor))
            if abs(new_w - float(r.retrieval_weight or 1.0)) < 0.005:
                continue
            await db.execute(
                text("UPDATE episodic SET retrieval_weight=:w WHERE episodic_id=:id"),
                {"w": new_w, "id": r.episodic_id},
            )
            ep_u += 1
        # 2) Semantic
        rows = (await db.execute(
            select(Semantic.semantic_id, Semantic.last_landed_at, Semantic.retrieval_weight)
            .where(Semantic.status == "active")
        )).all()
        for r in rows:
            d = _days_between(now, r.last_landed_at)
            if d < 7:  # 语义事实衰减慢一点，一周作为一个 step
                continue
            factor = strength ** (d / 10.0)
            new_w = max(MIN_WEIGHT, min(LAND_REBOUND, float(r.retrieval_weight or 1.0) * factor))
            if abs(new_w - float(r.retrieval_weight or 1.0)) < 0.005:
                continue
            await db.execute(
                text("UPDATE semantic SET retrieval_weight=:w WHERE semantic_id=:id"),
                {"w": new_w, "id": r.semantic_id},
            )
            sem_u += 1
        if ep_u or sem_u:
            log.info("ebbinghaus applied: ep=%s sem=%s", ep_u, sem_u)
    return {"episodic_updated": ep_u, "semantic_updated": sem_u}


async def land(user_id: str, *, episodic_id: Optional[str] = None, semantic_id: Optional[str] = None) -> None:
    """用户「捞起」：点过石子 / 编辑过 / 看过洞察详情 → 权重回弹到 1.0。"""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with get_db(read_only=False) as db:
        if episodic_id:
            await db.execute(
                text(
                    "UPDATE episodic SET retrieval_weight=1.0, last_landed_at=:now "
                    "WHERE episodic_id=:id AND user_id=:uid"
                ),
                {"now": now, "id": episodic_id, "uid": user_id},
            )
        if semantic_id:
            await db.execute(
                text(
                    "UPDATE semantic SET retrieval_weight=1.0, last_landed_at=:now "
                    "WHERE semantic_id=:id AND user_id=:uid"
                ),
                {"now": now, "id": semantic_id, "uid": user_id},
            )
