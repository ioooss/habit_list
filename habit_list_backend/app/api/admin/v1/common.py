"""Administrator principal and permission dependencies."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request, status

from ....admin.service import AdminPrincipal
from ...v1.common import ApiError


def current_admin(request: Request) -> AdminPrincipal:
    principal = getattr(request.state, "admin_principal", None)
    if not isinstance(principal, AdminPrincipal):
        raise ApiError("ADMIN_UNAUTHORIZED", "管理员会话无效", status.HTTP_401_UNAUTHORIZED)
    return principal


def require_permission(permission: str) -> Callable[[Request], AdminPrincipal]:
    def _dependency(request: Request) -> AdminPrincipal:
        principal = current_admin(request)
        if permission not in principal.permissions:
            raise ApiError(
                "ADMIN_FORBIDDEN",
                "当前管理员角色没有此操作权限",
                status.HTTP_403_FORBIDDEN,
            )
        return principal

    return _dependency


__all__ = ["current_admin", "require_permission"]
