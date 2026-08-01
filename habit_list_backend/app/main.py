"""FastAPI 入口 + lifespan（启 APScheduler、初始化 DB、注册中间件、挂载路由）。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import Settings, get_settings
from .core.security import auth_middleware
from .core.logging import setup_logging
from .db.database import init_db
from .memory.system2 import get_scheduler
from .api.v1.router import router as api_router


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

    # 2) 启 APScheduler（System2 慢速回路：睡眠巩固 / 艾宾浩斯 / 通知扫描）
    scheduler = get_scheduler(settings)
    scheduler.start()

    try:
        yield
    finally:
        log.info("shutting down scheduler")
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
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Trace-LLM-ID"],
    )

    # 鉴权中间件：解析 Authorization → 写入 request.state.user_id / trace_id
    app.middleware("http")(auth_middleware)

    # 健康检查（免鉴权，Docker/Nginx 探针用）
    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"ok": True, "env": settings.app_env, "service": "habit_list"}

    app.include_router(api_router, prefix=settings.api_prefix)
    return app
