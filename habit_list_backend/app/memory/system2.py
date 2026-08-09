"""System2 调度器：APScheduler AsyncIOScheduler。

调度的 3 件事：
1) 每周日 03:00  睡眠巩固 consolidate_weekly()
2) 每天 04:30    艾宾浩斯 apply_ebbinghaus()
3) 每 10 分钟    备忘到期扫 → status=overdue_stale（真推通知留给 iOS APNs / 本地通知，这里只打标）
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.models import Memo
from ..media.service import cleanup_unattached_assets

log = logging.getLogger("habit_list.memory.system2")

_scheduler: AsyncIOScheduler | None = None


async def _job_consolidate():
    from .consolidate import consolidate_weekly
    try:
        await consolidate_weekly()
    except Exception:  # noqa: BLE001
        log.exception("consolidate job failed")


async def _job_ebbinghaus():
    from .forgetting import apply_ebbinghaus
    try:
        await apply_ebbinghaus()
    except Exception:  # noqa: BLE001
        log.exception("ebbinghaus job failed")


async def _job_memo_stale_scan():
    """每 10 分钟扫一次：due_offset_days 很小 + created_at 很久 + 还 pending 的 → 标 overdue_stale。

    MVP 这里只打标，真实 iOS 通知由 App 前台/后台 Background Modes 或 APNs token 推送触发；
    我们这里先把 status 改对，iOS 切到备忘页就能看到红逾期。
    """
    now = datetime.now(UTC).replace(microsecond=0)
    async with get_db(read_only=False) as db:
        rows = (
            await db.execute(
                select(Memo).where(
                    Memo.status == "pending",
                    Memo.due_offset_days.in_([0, 1]),
                )
            )
        ).scalars().all()
        now_iso = now.isoformat().replace("+00:00", "Z")
        for memo in rows:
            try:
                created = datetime.fromisoformat(memo.created_at.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            overdue_hours = int(memo.due_offset_days or 0) * 24 + 1
            if (now - created).total_seconds() > overdue_hours * 3600:
                memo.status = "overdue_stale"
                memo.status_changed_at = now_iso
    return True


async def _job_media_unattached_cleanup():
    """Remove media uploads abandoned before a life fragment was saved."""

    settings = get_settings()
    async with get_db(read_only=False) as db:
        removed = await cleanup_unattached_assets(db, settings=settings)
    if removed:
        log.info("removed %s abandoned media upload(s)", removed)
    return removed


async def _job_memory_v2_outbox():
    """Drain a bounded Memory V2 outbox batch without blocking chat requests."""

    from ..memory_v2.worker import process_pending_outbox

    try:
        result = await process_pending_outbox()
        if result["claimed"]:
            log.info("memory_v2 outbox batch: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("memory_v2 outbox job failed")


def _parse_cron(expr: str) -> tuple[str, str, str, str, str]:
    a = expr.split()
    if len(a) != 5:
        raise ValueError(f"cron invalid: {expr}")
    return tuple(a)  # type: ignore[return-value]


def get_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = settings or get_settings()
    s = AsyncIOScheduler(timezone=settings.default_user_timezone)

    # 1) 睡眠巩固
    m, h, dom, mon, dow = _parse_cron(settings.system2_sleep_consolidation_cron)
    s.add_job(_job_consolidate, CronTrigger(minute=m, hour=h, day=dom, month=mon, day_of_week=dow),
              id="sleep_consolidation", replace_existing=True, misfire_grace_time=3600)

    # 2) 艾宾浩斯
    m, h, dom, mon, dow = _parse_cron(settings.system2_ebbinghaus_cron)
    s.add_job(_job_ebbinghaus, CronTrigger(minute=m, hour=h, day=dom, month=mon, day_of_week=dow),
              id="ebbinghaus", replace_existing=True, misfire_grace_time=3600)

    # 3) 过期备忘打标
    s.add_job(_job_memo_stale_scan, IntervalTrigger(minutes=10),
              id="memo_stale_scan", replace_existing=True, misfire_grace_time=180)

    # Composer uploads are stored before the user taps save. Keep abandoned
    # local files bounded without touching assets already attached to a record.
    s.add_job(_job_media_unattached_cleanup, IntervalTrigger(minutes=15),
              id="media_unattached_cleanup", replace_existing=True, misfire_grace_time=300)

    # 4) Fragment responses are product behavior, not Memory V2 behavior.
    # Keep this worker alive when Memory V2 is off so a saved life fragment can
    # still resolve its response queue instead of remaining Pending forever.
    s.add_job(
        _job_memory_v2_outbox,
        IntervalTrigger(seconds=settings.memory_v2_worker_interval_seconds),
        id="memory_v2_outbox",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(30, settings.memory_v2_worker_interval_seconds * 2),
    )

    _scheduler = s
    return s
