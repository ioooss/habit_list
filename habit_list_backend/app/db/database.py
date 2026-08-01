"""SQLAlchemy 2.0 async 引擎 + get_db 依赖注入（读不建事务/写自动提交）。

参考经验 1086132：统一入口，区分读/写会话。
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ..core.config import Settings, get_settings

log = logging.getLogger("habit_list")


class Base(DeclarativeBase):
    pass


_engines: dict[str, AsyncEngine] = {}
_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {}


def _sqlite_file_path(database_url: str) -> Optional[Path]:
    m = re.match(r"^sqlite\+aiosqlite:///(.+)$", database_url)
    if not m:
        return None
    raw = m.group(1)
    return Path(raw) if raw != ":memory:" else None


def get_engine(settings: Optional[Settings] = None) -> AsyncEngine:
    settings = settings or get_settings()
    key = settings.database_url
    if key in _engines:
        return _engines[key]

    p = _sqlite_file_path(settings.database_url)
    if p and not str(p).startswith(":"):
        p.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
        pool_pre_ping=True,
    )

    # SQLite pragmas + sqlite-vss extension 加载
    if "sqlite" in settings.database_url:
        @event.listens_for(engine.sync_engine, "connect")
        def _on_connect(dbapi_connection, _connection_record):  # noqa: ANN001
            # SQLAlchemy's aiosqlite dialect supplies a synchronous adapter,
            # not a raw sqlite3.Connection. Both expose the DB-API cursor used
            # here; skipping the adapter would silently disable FK cascades.
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA foreign_keys=ON;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA mmap_size=268435456;")
            except Exception as exc:  # pragma: no cover - 基础设施
                log.warning("sqlite pragma failed: %s", exc)
            # 加载 sqlite-vss（可选，失败降级）
            ext = settings.sqlite_vss_ext_path or None
            _try_load_vss(dbapi_connection, ext)
            cur.close()

    _engines[key] = engine
    return engine


def _try_load_vss(conn: Any, hint: Optional[str]) -> None:
    """加载 sqlite-vss 扩展（vector0 + vss0），失败只记日志不崩。"""
    import platform
    import sysconfig

    def _candidates():
        if hint:
            p = Path(hint)
            yield str(p / "vector0"), str(p / "vss0")
        # 常见 prebuilt 目录
        plat = platform.system().lower()
        ext = "dylib" if plat == "darwin" else "dll" if plat == "win32" else "so"
        base = Path(sysconfig.get_paths()["purelib"])
        for folder in [
            base / "sqlite_vss",
            Path("/app"),
            Path("/usr/local/lib"),
            Path("/opt/homebrew/lib"),
        ]:
            yield str(folder / f"vector0.{ext}"), str(folder / f"vss0.{ext}")
        # pip install 直接放 site-packages 下的
        for folder in [base]:
            yield str(folder / f"sqlite_vss_vector0.{ext}"), str(folder / f"sqlite_vss_vss0.{ext}")

    cur = conn.cursor()
    for v0, vs0 in _candidates():
        try:
            cur.execute(f"SELECT load_extension('{v0}');")
            cur.execute(f"SELECT load_extension('{vs0}');")
            log.info("sqlite-vss loaded: %s / %s", v0, vs0)
            return
        except Exception:
            continue
    log.info("sqlite-vss not available, vector retrieval will be skipped.")


def get_sessionmaker(settings: Optional[Settings] = None) -> async_sessionmaker[AsyncSession]:
    settings = settings or get_settings()
    key = settings.database_url
    if key in _sessionmakers:
        return _sessionmakers[key]
    engine = get_engine(settings)
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    _sessionmakers[key] = maker
    return maker


@asynccontextmanager
async def get_db(read_only: bool = False) -> AsyncIterator[AsyncSession]:
    """统一的会话入口：

    - read_only=True: 不启事务，SELECT 直接跑，不写 db 不需要 commit
    - read_only=False: 正常事务，出块自动 commit，异常自动 rollback
    """
    maker = get_sessionmaker()
    async with maker() as session:
        if read_only:
            try:
                yield session
            finally:
                await session.close()
            return
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db(settings: Optional[Settings] = None) -> None:
    """建表 + 虚表(FTS5/BM25 / vss 向量 / 图谱) + 默认用户。幂等可重复执行。"""
    settings = settings or get_settings()
    engine = get_engine(settings)
    # 1) ORM 表
    from . import memory_models as _memory_models  # noqa: F401, WPS433 - 注册 V2 表
    from . import migrations  # noqa: WPS433 - 建虚表/默认数据
    from .models import Base  # noqa: WPS433 - 延迟导入触发注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 2) 虚表 + 默认数据
        await migrations.apply(conn, settings)

    log.info("db init ok")
