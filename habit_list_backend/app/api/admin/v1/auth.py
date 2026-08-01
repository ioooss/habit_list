"""Independent administrator password + TOTP login API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ....admin.service import (
    AdminAuthError,
    AdminPrincipal,
    login_admin,
    logout_admin,
)
from ...v1.common import ApiError
from .common import current_admin

router = APIRouter(prefix="/auth")
CurrentAdmin = Annotated[AdminPrincipal, Depends(current_admin)]


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminLoginIn(StrictSchema):
    username: str = Field(min_length=3, max_length=128)
    password: SecretStr
    totp_code: SecretStr


class AdminLoginOut(StrictSchema):
    token_type: str = "Bearer"
    access_token: str
    expires_at_epoch: int
    admin_id: str
    admin_session_id: str
    roles: list[str]
    permissions: list[str]


class AdminMeOut(StrictSchema):
    admin_id: str
    admin_session_id: str
    username: str
    display_name: str
    roles: list[str]
    permissions: list[str]


def _request_context(request: Request) -> tuple[str | None, str | None, str | None]:
    request_id = str(getattr(request.state, "request_id", "")) or None
    source_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")
    return request_id, source_ip, user_agent


@router.post("/login", response_model=AdminLoginOut)
async def login(body: AdminLoginIn, request: Request) -> AdminLoginOut:
    request_id, source_ip, user_agent = _request_context(request)
    try:
        access = await login_admin(
            username=body.username,
            password=body.password.get_secret_value(),
            totp_code=body.totp_code.get_secret_value(),
            request_id=request_id,
            source_ip=source_ip,
            user_agent=user_agent,
        )
    except AdminAuthError as exc:
        raise ApiError(exc.code, exc.message, exc.status_code) from exc
    return AdminLoginOut(
        access_token=access.access_token,
        expires_at_epoch=access.expires_at_epoch,
        admin_id=access.admin_id,
        admin_session_id=access.admin_session_id,
        roles=list(access.roles),
        permissions=list(access.permissions),
    )


@router.get("/me", response_model=AdminMeOut)
async def me(principal: CurrentAdmin) -> AdminMeOut:
    return AdminMeOut(
        admin_id=principal.admin_id,
        admin_session_id=principal.admin_session_id,
        username=principal.username,
        display_name=principal.display_name,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
    )


@router.post("/logout", status_code=204, response_class=Response)
async def logout(
    request: Request,
    principal: CurrentAdmin,
) -> Response:
    request_id, source_ip, user_agent = _request_context(request)
    await logout_admin(
        principal=principal,
        request_id=request_id,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return Response(status_code=204)


__all__ = ["router"]
