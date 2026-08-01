"""Alembic upgrade, drift check, downgrade, and replay verification."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = REPO_DIR / "data" / "pytest-alembic.db"


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    database_url = f"sqlite+aiosqlite:///{DATABASE_PATH.resolve().as_posix()}"
    environment = {
        **os.environ,
        "APP_ENV": "dev",
        "PROCESS_ROLE": "api",
        "DATABASE_SCHEMA_MODE": "alembic",
        "DATABASE_URL": database_url,
        "DASHSCOPE_API_KEY": "sk-test-migration-placeholder",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return result


def _table_names() -> set[str]:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def test_initial_migration_has_no_model_drift_and_can_replay():
    DATABASE_PATH.unlink(missing_ok=True)
    try:
        _run_alembic("upgrade", "head")
        assert {"alembic_version", "users", "memory_claims", "outbox_events"} <= _table_names()
        assert "No new upgrade operations detected" in _run_alembic("check").stdout

        _run_alembic("downgrade", "base")
        assert "memory_claims" not in _table_names()
        _run_alembic("upgrade", "head")
        assert "memory_claims" in _table_names()
    finally:
        DATABASE_PATH.unlink(missing_ok=True)
