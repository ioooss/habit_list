"""Container health command for the independent worker."""
from __future__ import annotations

from pathlib import Path

from ..core.config import get_settings
from .runtime import heartbeat_is_fresh


def main() -> None:
    settings = get_settings()
    healthy = heartbeat_is_fresh(
        Path(settings.worker_heartbeat_path).resolve(),
        stale_seconds=settings.worker_heartbeat_stale_seconds,
    )
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
