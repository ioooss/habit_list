"""User authentication, refresh rotation, and device-session control APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ...identity.service import (
    AuthServiceError,
    IssuedTokens,
    create_auth_challenge,
    exchange_apple_identity,
    list_user_sessions,
    revoke_user_sessions,
    rotate_refresh_token,
)
from .common import ApiError, current_session, current_user

router = APIRouter(prefix="/auth")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChallengeOut(StrictSchema):
    challenge_id: str
    nonce: str
    expires_at_epoch: int


class AppleExchangeIn(StrictSchema):
    challenge_id: str = Field(min_length=36, max_length=36)
    raw_nonce: SecretStr
    identity_token: SecretStr
    installation_id: str = Field(min_length=16, max_length=200)
    platform: str = Field(default="ios", pattern=r"^(ios|android|web)$")
    device_name: str = Field(default="", max_length=80)
    app_version: str = Field(default="", max_length=32)
    locale: str = Field(default="zh-CN", min_length=2, max_length=16)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)


class RefreshIn(StrictSchema):
    refresh_token: SecretStr


class TokenOut(StrictSchema):
    token_type: str = "Bearer"
    access_token: str
    refresh_token: str
    access_expires_at_epoch: int
    refresh_expires_at_epoch: int
    user_id: str
    session_id: str
    device_id: str


class AppleExchangeOut(TokenOut):
    is_new_user: bool
    email_hint: str | None = None


class SessionOut(StrictSchema):
    session_id: str
    device_id: str
    platform: str
    device_name: str
    app_version: str
    created_at: str
    last_seen_at_epoch: int
    current: bool


class SessionListOut(StrictSchema):
    items: list[SessionOut]


class RevocationOut(StrictSchema):
    revoked: int


def _api_error(exc: AuthServiceError) -> ApiError:
    return ApiError(exc.code, exc.message, exc.status_code)


def _tokens_out(tokens: IssuedTokens) -> TokenOut:
    return TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at_epoch=tokens.access_expires_at_epoch,
        refresh_expires_at_epoch=tokens.refresh_expires_at_epoch,
        user_id=tokens.user_id,
        session_id=tokens.session_id,
        device_id=tokens.device_id,
    )


@router.post("/challenges", response_model=ChallengeOut, status_code=status.HTTP_201_CREATED)
async def create_challenge() -> ChallengeOut:
    try:
        challenge_id, nonce, expires_at = await create_auth_challenge()
    except AuthServiceError as exc:
        raise _api_error(exc) from exc
    return ChallengeOut(
        challenge_id=challenge_id,
        nonce=nonce,
        expires_at_epoch=expires_at,
    )


@router.post("/apple", response_model=AppleExchangeOut)
async def exchange_apple(body: AppleExchangeIn) -> AppleExchangeOut:
    try:
        result = await exchange_apple_identity(
            challenge_id=body.challenge_id,
            raw_nonce=body.raw_nonce.get_secret_value(),
            identity_token=body.identity_token.get_secret_value(),
            installation_id=body.installation_id,
            platform=body.platform,
            device_name=body.device_name,
            app_version=body.app_version,
            locale=body.locale,
            timezone_name=body.timezone,
        )
    except AuthServiceError as exc:
        raise _api_error(exc) from exc
    token_out = _tokens_out(result.tokens)
    return AppleExchangeOut(
        **token_out.model_dump(),
        is_new_user=result.is_new_user,
        email_hint=result.email_hint,
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn) -> TokenOut:
    try:
        tokens = await rotate_refresh_token(body.refresh_token.get_secret_value())
    except AuthServiceError as exc:
        raise _api_error(exc) from exc
    return _tokens_out(tokens)


@router.get("/sessions", response_model=SessionListOut)
async def sessions(
    user_id: str = Depends(current_user),
    session_id: str = Depends(current_session),
) -> SessionListOut:
    items = await list_user_sessions(user_id=user_id, current_session_id=session_id)
    return SessionListOut(items=[SessionOut(**item.__dict__) for item in items])


@router.post("/logout", response_model=RevocationOut)
async def logout(
    user_id: str = Depends(current_user),
    session_id: str = Depends(current_session),
) -> RevocationOut:
    revoked = await revoke_user_sessions(
        user_id=user_id,
        session_id=session_id,
        reason="user_logout",
    )
    return RevocationOut(revoked=revoked)


@router.delete("/sessions/{target_session_id}", response_model=RevocationOut)
async def revoke_session(
    target_session_id: str,
    user_id: str = Depends(current_user),
) -> RevocationOut:
    revoked = await revoke_user_sessions(
        user_id=user_id,
        session_id=target_session_id,
        reason="user_device_revocation",
    )
    if revoked == 0:
        raise ApiError("SESSION_NOT_FOUND", "会话不存在", 404)
    return RevocationOut(revoked=revoked)


@router.post("/logout-all", response_model=RevocationOut)
async def logout_all(user_id: str = Depends(current_user)) -> RevocationOut:
    revoked = await revoke_user_sessions(
        user_id=user_id,
        session_id=None,
        reason="user_logout_all",
    )
    return RevocationOut(revoked=revoked)


@router.get("/me")
async def auth_me(request: Request) -> dict[str, str]:
    user_id = current_user(request)
    session_id = current_session(request)
    return {"user_id": user_id, "session_id": session_id}


__all__ = ["router"]
