"""Path-separated user and administrator authentication middleware."""

from __future__ import annotations

import hmac
import logging
import uuid

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from ..admin.service import authenticate_admin_access
from ..identity.service import authenticate_user_access
from .config import get_settings

log = logging.getLogger("habit_list")

_PUBLIC_STATIC_EXACT = frozenset(
    {
        "/health",
        "/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/admin/v1/auth/login",
    }
)


def _is_public(path: str, api_prefix: str) -> bool:
    user_auth_public = {
        f"{api_prefix}/auth/challenges",
        f"{api_prefix}/auth/apple",
        f"{api_prefix}/auth/refresh",
    }
    return (
        path in _PUBLIC_STATIC_EXACT
        or path in user_auth_public
        or path.startswith(("/docs/", "/redoc/", "/static/"))
    )


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()


def _error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "code": code, "message": message},
        headers={"X-Request-ID": request_id},
    )


async def auth_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    path = request.url.path
    settings = get_settings()

    if request.method == "OPTIONS" or _is_public(path, settings.api_prefix):
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request_id)
        return response

    token = _bearer_token(request)

    if path.startswith("/admin/v1/"):
        try:
            principal = await authenticate_admin_access(token, settings=settings)
        except Exception:  # noqa: BLE001 - do not leak auth storage internals
            log.exception("admin auth backend unavailable path=%s", path)
            return _error_response(
                request_id=request_id,
                code="AUTH_BACKEND_UNAVAILABLE",
                message="身份服务暂时不可用",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if principal is None:
            log.warning("admin_auth_fail path=%s has_auth=%s", path, bool(token))
            return _error_response(
                request_id=request_id,
                code="ADMIN_UNAUTHORIZED",
                message="需要有效的管理员会话",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        request.state.admin_principal = principal
        request.state.is_admin = True
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request_id)
        return response

    if path.startswith("/api/"):
        if settings.auth_mode == "legacy":
            valid = bool(token) and hmac.compare_digest(token, settings.api_auth_token)
            if valid:
                request.state.user_id = settings.default_user_id
                request.state.session_id = "legacy-dev-session"
                request.state.device_id = "legacy-dev-device"
        else:
            try:
                principal = await authenticate_user_access(token, settings=settings)
            except Exception:  # noqa: BLE001 - do not leak auth storage internals
                log.exception("user auth backend unavailable path=%s", path)
                return _error_response(
                    request_id=request_id,
                    code="AUTH_BACKEND_UNAVAILABLE",
                    message="身份服务暂时不可用",
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if principal is not None:
                request.state.user_id = principal.user_id
                request.state.session_id = principal.session_id
                request.state.device_id = principal.device_id

        if not getattr(request.state, "user_id", None):
            log.warning("user_auth_fail path=%s has_auth=%s", path, bool(token))
            return _error_response(
                request_id=request_id,
                code="UNAUTHORIZED",
                message="需要有效的用户会话",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        request.state.is_admin = False
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request_id)
        return response

    response = await call_next(request)
    response.headers.setdefault("X-Request-ID", request_id)
    return response


__all__ = ["auth_middleware"]
