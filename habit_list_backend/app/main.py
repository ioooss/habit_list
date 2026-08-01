"""FastAPI 入口 + lifespan（启 APScheduler、初始化 DB、注册中间件、挂载路由）。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api.v1.router import router as api_router
from .core.config import Settings, get_settings
from .core.logging import setup_logging
from .core.security import auth_middleware
from .db.database import get_db, init_db
from .memory.system2 import get_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    setup_logging(settings.log_level, settings.app_env)
    log = logging.getLogger("habit_list")
    log.info("booting app env=%s", settings.app_env)

    # 确保 data 目录存在
    db_path = getattr(settings, "_sqlite_local_path", None)
    if db_path:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # 1) 建表 + 虚表（FTS5 / vector）+ 默认用户
    await init_db(settings)

    # 本地 all 模式保留单进程便利性；生产 api 进程绝不运行后台任务。
    scheduler = None
    if settings.process_role == "all":
        scheduler = get_scheduler(settings)
        scheduler.start()

    try:
        yield
    finally:
        if scheduler is not None:
            log.info("shutting down embedded scheduler")
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Habit List · 陪伴式自我记录 Backend",
        version="0.1.0",
        description="四层认知记忆 OS（Working/Episodic/Semantic/Procedural）+ DashScope 代理",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = settings

    # CORS：iOS 端用 WKWebView 其实不需要；本地 app.html 联调需要
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Trace-LLM-ID"],
    )

    # 鉴权中间件：解析 Authorization → 写入 request.state.user_id / trace_id
    app.middleware("http")(auth_middleware)

    # 健康检查（免鉴权，Docker/Nginx 探针用）
    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {
            "ok": True,
            "env": settings.app_env,
            "service": "habit_list",
            "role": settings.process_role,
        }

    @app.get("/ready", tags=["meta"])
    async def ready():
        try:
            async with get_db(read_only=True) as db:
                await db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - readiness must not leak DB details
            return JSONResponse(
                status_code=503,
                content={"ok": False, "service": "habit_list", "ready": False},
            )
        return {"ok": True, "service": "habit_list", "ready": True}

    app.include_router(api_router, prefix=settings.api_prefix)
    return app
