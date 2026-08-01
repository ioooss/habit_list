import os
from collections.abc import AsyncIterator
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent
DATA_DIR = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 在 import app 任何东西之前覆写环境 → 确保所有 get_settings() 读的是测试库
# 注意：必须用 os.environ[k]=v 直接赋值，不能 setdefault（pydantic-settings 在多 pytest 进程里会缓存）
os.environ["APP_ENV"] = "dev"
# 注意: 不用 :memory:（aiosqlite 每连接独立，init_db 建的表查不到）
# 用临时文件 db，放在 data 目录下；每个 test 开始前清掉重来
TEST_DB_PATH = str((REPO_DIR / "data" / "pytest_integration.db").resolve()).replace("\\", "/")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_PATH}"
os.environ["API_AUTH_TOKEN"] = "test-token"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["DASHSCOPE_API_KEY"] = "sk-test-xxxxxxxxxxxxxxxxxxxx"
os.environ["DEFAULT_USER_ID"] = "01920000-0000-0000-0000-000000000001"
os.environ["SQLITE_VSS_EXT_PATH"] = ""
os.environ["FTS5_TOKENIZER"] = "unicode61"
os.environ["SYSTEM2_SLEEP_CONSOLIDATION_CRON"] = "0 23 31 2 4"
os.environ["SYSTEM2_EBBINGHAUS_CRON"] = "0 23 31 2 4"


def _wipe_test_db():
    """删掉测试 DB 文件及其 wal/shm 缓存，保证从空表开始。"""
    try:
        Path(TEST_DB_PATH).unlink(missing_ok=True)
        for suf in ("-wal", "-shm", "-journal"):
            Path(TEST_DB_PATH + suf).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# 会话开始前先删一次旧 DB
_wipe_test_db()


import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.db import database as db_mod  # noqa: E402
from app.memory import system2 as system2_mod  # noqa: E402
from app.providers import dashscope as dashscope_provider  # noqa: E402
from app.retrieval import graph as graph_mod  # noqa: E402

# 让 lru_cache 立刻重新拿测试值
get_settings.cache_clear()


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return get_settings()


@pytest.fixture()
async def app_no_scheduler(monkeypatch, test_settings: Settings) -> FastAPI:
    """禁止 APScheduler 真起定时任务；每个用例前都重建空表 + 显式跑 init_db。
    注意：改成 async fixture 是为了能在返回前 await init_db，
    因为有些测试会绕过 ASGI 直接调 get_sessionmaker() 写库（比如 _create_insight）。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    def _fake(_settings):
        return AsyncIOScheduler(timezone=test_settings.default_user_timezone)

    monkeypatch.setattr(system2_mod, "get_scheduler", _fake)

    # Windows 会锁住仍在连接池中的 SQLite 文件。必须先释放旧引擎，
    # 再清理测试库，否则删除失败会被容错吞掉，造成跨用例“幽灵记忆”。
    for engine in list(db_mod._engines.values()):
        await engine.dispose()
    db_mod._engines.clear()
    db_mod._sessionmakers.clear()
    _wipe_test_db()

    # 清掉其余单例缓存，保证每个用例从空状态启动。
    dashscope_provider._clients.clear()
    system2_mod._scheduler = None
    graph_mod._GRAPHS_BY_USER.clear()
    graph_mod._NODE_NORM_NAMES_BY_USER.clear()
    graph_mod._GRAPH_VERSION += 1
    get_settings.cache_clear()

    from app.main import create_app
    app = create_app()

    # 关键：显式立刻跑 init_db（create_all + migrations.apply + 默认用户）
    # lifespan 里也会跑一次，但那要等 ASGI 客户端连上才会触发；
    # 测试里直接用 get_sessionmaker() 绕过 FastAPI 的场景必须在这里就建好表
    from app.db.database import init_db
    await init_db(test_settings)

    return app


@pytest.fixture()
async def client(
    app_no_scheduler: FastAPI, test_settings: Settings
) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_no_scheduler)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {test_settings.api_auth_token}"},
        timeout=30,
    ) as c:
        yield c
