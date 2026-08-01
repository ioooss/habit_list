"""公共依赖 / 公共 schema。"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict


class ApiError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int = 400, details: Any = None):
        super().__init__(status_code=status_code, detail={"ok": False, "code": code, "message": message, "details": details})


def current_user(request: Request) -> str:
    """从 request.state.user_id 拿用户（中间件保证有）。
    业务层 **禁止**再从 Authorization header 读一次（避免 Experience 198734 教训）。
    """
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise ApiError("UNAUTHORIZED", "未登录或 session 无效", status.HTTP_401_UNAUTHORIZED)
    return str(uid)


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


class BaseSchema(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
