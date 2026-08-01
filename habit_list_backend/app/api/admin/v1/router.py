"""Administrator API v1 router."""

from __future__ import annotations

from fastapi import APIRouter

from . import audit, auth

router = APIRouter()
router.include_router(auth.router, tags=["管理员身份"])
router.include_router(audit.router, tags=["管理员审计"])

__all__ = ["router"]
