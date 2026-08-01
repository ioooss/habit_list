"""Production worker runtime with scheduler ownership and health heartbeat."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import Settings, get_settings
from ..core.logging import setup_logging
from ..db.database import init_db
from ..memory.system2 import get_scheduler

log = logging.getLogger("habit_list.worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def write_heartbeat(path: Path, *, status: str = "running") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "pid": os.getpid(),
        "updated_at": _utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def heartbeat_is_fresh(path: Path, *, stale_seconds: int, now: datetime | None = None) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "running":
            return False
        updated = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return 0 <= (current - updated).total_seconds() <= stale_seconds


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.process_role not in {"worker", "all"}:
        raise RuntimeError("worker runtime requires PROCESS_ROLE=worker or all")

    setup_logging(settings.log_level, settings.app_env)
    await init_db(settings)
    scheduler = get_scheduler(settings)
    scheduler.start()
    heartbeat_path = Path(settings.worker_heartbeat_path).resolve()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):  # Windows event loops
            pass

    log.info("worker started pid=%s", os.getpid())
    try:
        while not stop_event.is_set():
            write_heartbeat(heartbeat_path)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.worker_heartbeat_interval_seconds,
                )
            except TimeoutError:
                continue
    finally:
        scheduler.shutdown(wait=False)
        write_heartbeat(heartbeat_path, status="stopped")
        log.info("worker stopped")


def main() -> None:
    asyncio.run(run_worker())


__all__ = ["heartbeat_is_fresh", "main", "run_worker", "write_heartbeat"]
