"""Production configuration and worker health invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.db.memory_models import MemoryEmbedding
from app.worker.runtime import heartbeat_is_fresh, write_heartbeat

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
HEARTBEAT_PATH = DATA_DIR / "pytest-worker-heartbeat.json"
PRODUCTION_SECRETS = {
    "auth_mode": "sessions",
    "auth_token_pepper": "production-test-pepper-000000000000000000001",
    "pii_encryption_key": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "admin_mfa_encryption_key": "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE=",
    "apple_client_ids": "com.example.innerterrain.tests",
    "dashscope_api_key": "sk-test-only-not-a-real-provider-key",
    "cors_allowed_origins": "https://admin.example.test",
    "deploy_domain": "api.innerterrain.cn",
    "deploy_email": "operator@innerterrain.cn",
}


def test_production_rejects_sqlite_and_embedded_process_role():
    with pytest.raises(ValueError, match="生产环境禁止使用 SQLite"):
        Settings(
            _env_file=None,
            app_env="prod",
            process_role="api",
            database_url="sqlite+aiosqlite:///./data/prod.db",
            database_schema_mode="alembic",
            **PRODUCTION_SECRETS,
        )

    with pytest.raises(ValueError, match="必须显式拆分"):
        Settings(
            _env_file=None,
            app_env="prod",
            process_role="all",
            database_url="postgresql+psycopg://terrain:secret@postgres/terrain",
            database_schema_mode="alembic",
            **PRODUCTION_SECRETS,
        )


def test_production_accepts_postgresql_alembic_and_api_role():
    settings = Settings(
        _env_file=None,
        app_env="prod",
        process_role="api",
        database_url="postgresql+psycopg://terrain:secret@postgres/terrain",
        database_schema_mode="alembic",
        **PRODUCTION_SECRETS,
    )
    assert settings.database_is_postgresql is True
    assert settings.database_is_sqlite is False


def test_production_rejects_wildcard_cors_and_placeholder_secrets():
    base = {
        "_env_file": None,
        "app_env": "prod",
        "process_role": "api",
        "database_url": "postgresql+psycopg://terrain:secret@postgres/terrain",
        "database_schema_mode": "alembic",
    }
    with pytest.raises(ValueError, match="CORS_ALLOWED_ORIGINS"):
        Settings(**base, **{**PRODUCTION_SECRETS, "cors_allowed_origins": "*"})

    with pytest.raises(ValueError, match="AUTH_TOKEN_PEPPER"):
        Settings(
            **base,
            **{
                **PRODUCTION_SECRETS,
                "auth_token_pepper": "replace_with_a_long_random_token_pepper",
            },
        )

    with pytest.raises(ValueError, match="AUTH_MODE=sessions"):
        Settings(**base, **{**PRODUCTION_SECRETS, "auth_mode": "legacy"})

    with pytest.raises(ValueError, match="HTTPS Origin"):
        Settings(
            **base,
            **{
                **PRODUCTION_SECRETS,
                "cors_allowed_origins": "http://admin.example.test",
            },
        )

    with pytest.raises(ValueError, match="必须不同"):
        Settings(
            **base,
            **{
                **PRODUCTION_SECRETS,
                "admin_mfa_encryption_key": PRODUCTION_SECRETS["pii_encryption_key"],
            },
        )

    with pytest.raises(ValueError, match="DEPLOY_DOMAIN"):
        Settings(
            **base,
            **{
                **PRODUCTION_SECRETS,
                "deploy_domain": "your.domain.example.com",
            },
        )


def test_pgvector_cosine_query_keeps_native_operator_for_ann_index():
    distance = MemoryEmbedding.vector_json.cosine_distance([0.0] * 1024)
    statement = select(MemoryEmbedding.claim_id).order_by(distance).limit(10)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "<=>" in compiled
    assert "CAST(" not in compiled


def test_worker_heartbeat_is_atomic_fresh_and_status_aware():
    HEARTBEAT_PATH.unlink(missing_ok=True)
    try:
        write_heartbeat(HEARTBEAT_PATH)
        now = datetime.now(timezone.utc)
        assert heartbeat_is_fresh(HEARTBEAT_PATH, stale_seconds=45, now=now)
        assert not heartbeat_is_fresh(
            HEARTBEAT_PATH,
            stale_seconds=45,
            now=now + timedelta(seconds=60),
        )

        write_heartbeat(HEARTBEAT_PATH, status="stopped")
        assert not heartbeat_is_fresh(HEARTBEAT_PATH, stale_seconds=45)
    finally:
        HEARTBEAT_PATH.unlink(missing_ok=True)
