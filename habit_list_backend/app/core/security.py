"""Bearer Token 鉴权中间件（MVP 固定 token 版）。

原则：
1. 业务 handler **统一只读 request.state.user_id / request.state.is_admin / request.state.request_id**，
   不直接读 headers，避免来源分裂（Experience 198734 的教训）。
2. 没有 Authorization 或 token 不对的 /auth 相关路由直接 401；非 API 路由（/health /docs）免鉴权。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from .config import get_settings

log = logging.getLogger("habit_list")

# 免鉴权白名单（纯前缀匹配）
_PUBLIC_PREFIXES = (
    "/health",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/static",
)


async def auth_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    # 每请求 request_id（即使日志中间件没挂也能工作）
    req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = req_id

    # 公共路径：直接过，不设 user_id
    if any(request.url.path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    # CORS 预检（OPTIONS）：浏览器跨域先发 OPTIONS，不带 Authorization，必须先放
    # CORSMiddleware 会加响应头；如果在鉴权层直接 401，浏览器看不到 CORS 头就报 net::ERR_FAILED
    if request.method == "OPTIONS":
        return await call_next(request)

    # 所有 `/api/...` 必须带 Authorization
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()

    settings = get_settings()
    # 鉴权：单用户 MVP = API_AUTH_TOKEN；管理员多一个 ADMIN_TOKEN
    is_admin = bool(token) and token == settings.admin_token
    if not is_admin and token != settings.api_auth_token:
        log.warning(
            "auth_fail path=%s token_len=%s has_auth=%s",
            request.url.path,
            len(token),
            bool(auth),
            extra={"req_id": req_id},
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"ok": False, "code": "UNAUTHORIZED", "message": "需要有效的 Authorization: Bearer <token>"},
            headers={"X-Request-ID": req_id},
        )

    # 唯一来源：中间件写 -> handler 只读这里（禁止在业务层从 headers 再读一次）
    request.state.user_id = settings.default_user_id
    request.state.is_admin = is_admin
    request.state.auth_token = token
    return await call_next(request)
