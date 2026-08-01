"""Alembic environment for async SQLite development and PostgreSQL production."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db import memory_models as _memory_models  # noqa: F401
from app.db import models as _models  # noqa: F401
from app.db.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def _include_object(obj, name: str | None, type_: str, reflected: bool, compare_to) -> bool:
    del obj, compare_to
    if type_ == "index" and name == "idx_memory_embeddings_vector_hnsw":
        return context.get_context().dialect.name == "postgresql"
    # SQLite FTS5/vss create implementation-specific shadow tables. They are
    # local search infrastructure, not relational schema owned by Alembic.
    if reflected and type_ == "table" and name:
        return not name.startswith(("episodic_fts", "episodic_vec"))
    return True


def _configure(connection=None, *, url: str | None = None) -> None:
    is_sqlite = bool(url and url.startswith("sqlite")) or bool(
        connection is not None and connection.dialect.name == "sqlite"
    )
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=is_sqlite,
        include_object=_include_object,
    )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    _configure(url=url)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
