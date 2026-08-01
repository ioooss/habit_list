"""Transactional identity, device, and refresh-token rotation services."""

from __future__ import annotations

import hmac
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.bootstrap import PROCEDURAL_DEFAULTS
from ..db.database import get_sessionmaker
from ..db.models import Procedural, User, _utcnow_iso, uuid7
from .apple import (
    AppleIdentityClaims,
    AppleIdentityUnavailableError,
    verify_apple_identity_token,
)
from .crypto import (
    encrypt_pii,
    identifier_digest,
    mask_email,
    new_opaque_token,
    nonce_digest,
    token_digest,
)
from .models import AuthChallenge, Device, RefreshToken, UserIdentity, UserSession

ACCESS_PREFIX = "it_at_"
REFRESH_PREFIX = "it_rt_"


class AuthServiceError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: str
    session_id: str
    device_id: str


@dataclass(frozen=True)
class IssuedTokens:
    user_id: str
    session_id: str
    device_id: str
    access_token: str
    refresh_token: str
    access_expires_at_epoch: int
    refresh_expires_at_epoch: int


@dataclass(frozen=True)
class AppleExchangeResult:
    tokens: IssuedTokens
    is_new_user: bool
    email_hint: str | None


@dataclass(frozen=True)
class SessionView:
    session_id: str
    device_id: str
    platform: str
    device_name: str
    app_version: str
    created_at: str
    last_seen_at_epoch: int
    current: bool


AppleVerifier = Callable[..., Awaitable[AppleIdentityClaims]]


def _now_epoch() -> int:
    return int(time.time())


def _safe_token(token: str, prefix: str) -> bool:
    return bool(token) and token.startswith(prefix) and len(token) <= 256


def _validate_device_input(installation_id: str, platform: str) -> None:
    if not 16 <= len(installation_id) <= 200:
        raise AuthServiceError(
            "INVALID_DEVICE",
            "设备安装标识格式不正确",
            422,
        )
    if platform not in {"ios", "android", "web"}:
        raise AuthServiceError("INVALID_DEVICE", "不支持的设备平台", 422)


async def create_auth_challenge(settings: Settings | None = None) -> tuple[str, str, int]:
    settings = settings or get_settings()
    if settings.auth_mode != "sessions":
        raise AuthServiceError("AUTH_MODE_DISABLED", "会话登录尚未启用", 404)
    raw_nonce = new_opaque_token("")
    expires_at = _now_epoch() + settings.auth_challenge_ttl_seconds
    challenge = AuthChallenge(
        provider="apple",
        nonce_hash=nonce_digest(raw_nonce),
        status="pending",
        expires_at_epoch=expires_at,
    )
    maker = get_sessionmaker(settings)
    async with maker.begin() as session:
        session.add(challenge)
        await session.flush()
    return challenge.challenge_id, raw_nonce, expires_at


async def _load_pending_challenge(
    challenge_id: str,
    raw_nonce: str,
    settings: Settings,
) -> str:
    expected_hash = nonce_digest(raw_nonce)
    maker = get_sessionmaker(settings)
    async with maker() as session:
        challenge = (
            await session.execute(
                select(AuthChallenge).where(
                    AuthChallenge.challenge_id == challenge_id,
                    AuthChallenge.provider == "apple",
                )
            )
        ).scalar_one_or_none()
    if challenge is None or not hmac.compare_digest(challenge.nonce_hash, expected_hash):
        raise AuthServiceError("INVALID_AUTH_CHALLENGE", "登录挑战无效或已失效")
    if challenge.status != "pending" or challenge.expires_at_epoch < _now_epoch():
        raise AuthServiceError("INVALID_AUTH_CHALLENGE", "登录挑战无效或已失效")
    return expected_hash


async def _seed_new_user_preferences(session: AsyncSession, user_id: str) -> None:
    for key, value, confidence, reason in PROCEDURAL_DEFAULTS:
        session.add(
            Procedural(
                user_id=user_id,
                param_key=key,
                param_value_json=value,
                confidence=confidence,
                learned_reason=reason,
                learned_ev_count=0,
                ref_ledger_ids_json=[],
            )
        )


async def _lock_identity_subject(
    session: AsyncSession,
    *,
    provider: str,
    subject: str,
    settings: Settings,
) -> None:
    """Serialize first-login creation for one external identity on PostgreSQL."""

    if not settings.database_is_postgresql:
        return
    digest = identifier_digest(f"identity-lock:{provider}:{subject}", settings)
    unsigned = int(digest[:16], 16)
    lock_id = unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )


async def _get_or_create_device(
    session: AsyncSession,
    *,
    user_id: str,
    installation_id: str,
    platform: str,
    device_name: str,
    app_version: str,
    settings: Settings,
) -> Device:
    installation_hash = identifier_digest(f"device:{installation_id}", settings)
    device = (
        await session.execute(
            select(Device).where(
                Device.user_id == user_id,
                Device.installation_id_hash == installation_hash,
            )
        )
    ).scalar_one_or_none()
    now_iso = _utcnow_iso()
    if device is None:
        device = Device(
            user_id=user_id,
            installation_id_hash=installation_hash,
            platform=platform,
            device_name=device_name,
            app_version=app_version,
            status="active",
            last_seen_at=now_iso,
        )
        session.add(device)
        await session.flush()
    else:
        device.platform = platform
        device.device_name = device_name
        device.app_version = app_version
        device.status = "active"
        device.last_seen_at = now_iso
    return device


async def _enforce_session_limit(
    session: AsyncSession,
    *,
    user_id: str,
    settings: Settings,
) -> None:
    active = list(
        (
            await session.execute(
                select(UserSession)
                .where(UserSession.user_id == user_id, UserSession.status == "active")
                .order_by(UserSession.last_seen_at_epoch.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    overflow = len(active) - settings.auth_max_sessions_per_user + 1
    if overflow <= 0:
        return
    now_iso = _utcnow_iso()
    for old in active[:overflow]:
        old.status = "revoked"
        old.revoked_at = now_iso
        old.revoke_reason = "session_limit"
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == old.session_id, RefreshToken.status == "active")
            .values(status="revoked", consumed_at=now_iso)
        )


async def _issue_session(
    session: AsyncSession,
    *,
    user_id: str,
    device: Device,
    settings: Settings,
) -> IssuedTokens:
    await _enforce_session_limit(session, user_id=user_id, settings=settings)
    now = _now_epoch()
    access_token = new_opaque_token(ACCESS_PREFIX)
    refresh_token = new_opaque_token(REFRESH_PREFIX)
    access_expires = now + settings.auth_access_ttl_seconds
    refresh_expires = now + settings.auth_refresh_ttl_days * 86400
    family_id = str(uuid7())
    user_session = UserSession(
        user_id=user_id,
        device_id=device.device_id,
        access_token_hash=token_digest(access_token, settings),
        access_expires_at_epoch=access_expires,
        refresh_family_id=family_id,
        status="active",
        last_seen_at_epoch=now,
    )
    session.add(user_session)
    await session.flush()
    session.add(
        RefreshToken(
            session_id=user_session.session_id,
            family_id=family_id,
            token_hash=token_digest(refresh_token, settings),
            status="active",
            expires_at_epoch=refresh_expires,
        )
    )
    return IssuedTokens(
        user_id=user_id,
        session_id=user_session.session_id,
        device_id=device.device_id,
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at_epoch=access_expires,
        refresh_expires_at_epoch=refresh_expires,
    )


async def exchange_apple_identity(
    *,
    challenge_id: str,
    raw_nonce: str,
    identity_token: str,
    installation_id: str,
    platform: str,
    device_name: str,
    app_version: str,
    locale: str,
    timezone_name: str,
    settings: Settings | None = None,
    verifier: AppleVerifier = verify_apple_identity_token,
) -> AppleExchangeResult:
    settings = settings or get_settings()
    if settings.auth_mode != "sessions":
        raise AuthServiceError("AUTH_MODE_DISABLED", "会话登录尚未启用", 404)
    _validate_device_input(installation_id, platform)
    expected_nonce_hash = await _load_pending_challenge(challenge_id, raw_nonce, settings)
    try:
        claims = await verifier(
            identity_token,
            expected_nonce_hash=expected_nonce_hash,
            settings=settings,
        )
    except AppleIdentityUnavailableError as exc:
        raise AuthServiceError(
            "IDENTITY_PROVIDER_UNAVAILABLE",
            "Apple 身份服务暂时不可用，请稍后重试",
            503,
        ) from exc
    except AuthServiceError:
        raise
    except Exception as exc:  # provider errors are deliberately normalized
        raise AuthServiceError("INVALID_IDENTITY_TOKEN", "Apple 身份验证失败") from exc

    maker = get_sessionmaker(settings)
    is_new_user = False
    async with maker.begin() as session:
        challenge = (
            await session.execute(
                select(AuthChallenge)
                .where(AuthChallenge.challenge_id == challenge_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            challenge is None
            or challenge.status != "pending"
            or challenge.expires_at_epoch < _now_epoch()
            or not hmac.compare_digest(challenge.nonce_hash, expected_nonce_hash)
        ):
            raise AuthServiceError("INVALID_AUTH_CHALLENGE", "登录挑战无效或已失效")

        await _lock_identity_subject(
            session,
            provider="apple",
            subject=claims.subject,
            settings=settings,
        )
        identity = (
            await session.execute(
                select(UserIdentity)
                .where(
                    UserIdentity.provider == "apple",
                    UserIdentity.subject == claims.subject,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        now_iso = _utcnow_iso()
        if identity is None:
            is_new_user = True
            user = User(
                user_id=str(uuid7()),
                locale=locale,
                timezone=timezone_name,
                status="active",
                updated_at=now_iso,
            )
            session.add(user)
            await session.flush()
            await _seed_new_user_preferences(session, user.user_id)
            identity = UserIdentity(
                user_id=user.user_id,
                provider="apple",
                subject=claims.subject,
                email_verified=claims.email_verified,
                provider_metadata_json={
                    "audience": claims.audience,
                    "is_private_email": claims.is_private_email,
                    "real_user_status": claims.real_user_status,
                },
                last_seen_at=now_iso,
            )
            session.add(identity)
        else:
            user = (
                await session.execute(
                    select(User).where(User.user_id == identity.user_id).with_for_update()
                )
            ).scalar_one()
            if user.status != "active":
                raise AuthServiceError("ACCOUNT_UNAVAILABLE", "账号当前不可登录", 403)
            identity.last_seen_at = now_iso
            identity.email_verified = claims.email_verified
            identity.provider_metadata_json = {
                "audience": claims.audience,
                "is_private_email": claims.is_private_email,
                "real_user_status": claims.real_user_status,
            }
        if claims.email and claims.email_verified:
            identity.email_ciphertext = encrypt_pii(claims.email, settings)
            identity.email_hash = identifier_digest(f"email:{claims.email}", settings)

        challenge.status = "consumed"
        challenge.consumed_at = now_iso
        device = await _get_or_create_device(
            session,
            user_id=user.user_id,
            installation_id=installation_id,
            platform=platform,
            device_name=device_name,
            app_version=app_version,
            settings=settings,
        )
        tokens = await _issue_session(
            session,
            user_id=user.user_id,
            device=device,
            settings=settings,
        )
    return AppleExchangeResult(
        tokens=tokens,
        is_new_user=is_new_user,
        email_hint=mask_email(claims.email if claims.email_verified else None),
    )


async def issue_session_for_user(
    *,
    user_id: str,
    installation_id: str,
    platform: str = "ios",
    device_name: str = "",
    app_version: str = "",
    settings: Settings | None = None,
) -> IssuedTokens:
    """Internal/test bootstrap. It never creates a user or bypasses account status."""

    settings = settings or get_settings()
    _validate_device_input(installation_id, platform)
    maker = get_sessionmaker(settings)
    async with maker.begin() as session:
        user = (
            await session.execute(select(User).where(User.user_id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None or user.status != "active":
            raise AuthServiceError("ACCOUNT_UNAVAILABLE", "账号当前不可登录", 403)
        device = await _get_or_create_device(
            session,
            user_id=user_id,
            installation_id=installation_id,
            platform=platform,
            device_name=device_name,
            app_version=app_version,
            settings=settings,
        )
        return await _issue_session(session, user_id=user_id, device=device, settings=settings)


async def _revoke_refresh_family(
    session: AsyncSession,
    *,
    family_id: str,
    reason: str,
) -> None:
    now_iso = _utcnow_iso()
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.status != "revoked")
        .values(status="revoked", consumed_at=now_iso)
    )
    await session.execute(
        update(UserSession)
        .where(UserSession.refresh_family_id == family_id, UserSession.status == "active")
        .values(status="revoked", revoked_at=now_iso, revoke_reason=reason)
    )


async def rotate_refresh_token(
    refresh_token: str,
    *,
    settings: Settings | None = None,
) -> IssuedTokens:
    settings = settings or get_settings()
    if not _safe_token(refresh_token, REFRESH_PREFIX):
        raise AuthServiceError("INVALID_REFRESH_TOKEN", "Refresh Token 无效或已失效")
    digest = token_digest(refresh_token, settings)
    maker = get_sessionmaker(settings)
    deferred_error: AuthServiceError | None = None
    issued: IssuedTokens | None = None
    async with maker.begin() as session:
        stored = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == digest).with_for_update()
            )
        ).scalar_one_or_none()
        if stored is None:
            deferred_error = AuthServiceError(
                "INVALID_REFRESH_TOKEN",
                "Refresh Token 无效或已失效",
            )
        else:
            user_session = (
                await session.execute(
                    select(UserSession)
                    .where(UserSession.session_id == stored.session_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if stored.status != "active":
                await _revoke_refresh_family(
                    session,
                    family_id=stored.family_id,
                    reason="refresh_reuse_detected",
                )
                deferred_error = AuthServiceError(
                    "REFRESH_TOKEN_REUSE_DETECTED",
                    "检测到 Refresh Token 重放，该设备会话已撤销",
                )
            elif (
                stored.expires_at_epoch < _now_epoch()
                or user_session is None
                or user_session.status != "active"
            ):
                stored.status = "revoked"
                stored.consumed_at = _utcnow_iso()
                deferred_error = AuthServiceError(
                    "INVALID_REFRESH_TOKEN",
                    "Refresh Token 无效或已失效",
                )
            else:
                user = (
                    await session.execute(select(User).where(User.user_id == user_session.user_id))
                ).scalar_one_or_none()
                device = (
                    await session.execute(
                        select(Device).where(Device.device_id == user_session.device_id)
                    )
                ).scalar_one_or_none()
                if (
                    user is None
                    or user.status != "active"
                    or device is None
                    or device.status != "active"
                ):
                    await _revoke_refresh_family(
                        session,
                        family_id=stored.family_id,
                        reason="principal_unavailable",
                    )
                    deferred_error = AuthServiceError(
                        "ACCOUNT_UNAVAILABLE",
                        "账号或设备当前不可用",
                        403,
                    )
                else:
                    now = _now_epoch()
                    new_access = new_opaque_token(ACCESS_PREFIX)
                    new_refresh = new_opaque_token(REFRESH_PREFIX)
                    access_expires = now + settings.auth_access_ttl_seconds
                    refresh_expires = now + settings.auth_refresh_ttl_days * 86400
                    replacement = RefreshToken(
                        session_id=user_session.session_id,
                        family_id=stored.family_id,
                        token_hash=token_digest(new_refresh, settings),
                        status="active",
                        parent_refresh_id=stored.refresh_id,
                        expires_at_epoch=refresh_expires,
                    )
                    session.add(replacement)
                    await session.flush()
                    stored.status = "rotated"
                    stored.consumed_at = _utcnow_iso()
                    stored.replaced_by_refresh_id = replacement.refresh_id
                    user_session.access_token_hash = token_digest(new_access, settings)
                    user_session.access_expires_at_epoch = access_expires
                    user_session.last_seen_at_epoch = now
                    device.last_seen_at = _utcnow_iso()
                    issued = IssuedTokens(
                        user_id=user.user_id,
                        session_id=user_session.session_id,
                        device_id=device.device_id,
                        access_token=new_access,
                        refresh_token=new_refresh,
                        access_expires_at_epoch=access_expires,
                        refresh_expires_at_epoch=refresh_expires,
                    )
    if deferred_error is not None:
        raise deferred_error
    if issued is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("refresh rotation completed without a result")
    return issued


async def authenticate_user_access(
    access_token: str,
    *,
    settings: Settings | None = None,
) -> AuthPrincipal | None:
    settings = settings or get_settings()
    if not _safe_token(access_token, ACCESS_PREFIX):
        return None
    digest = token_digest(access_token, settings)
    now = _now_epoch()
    maker = get_sessionmaker(settings)
    async with maker.begin() as session:
        row = (
            await session.execute(
                select(UserSession, User, Device)
                .join(User, User.user_id == UserSession.user_id)
                .join(Device, Device.device_id == UserSession.device_id)
                .where(
                    UserSession.access_token_hash == digest,
                    UserSession.status == "active",
                    UserSession.access_expires_at_epoch >= now,
                    User.status == "active",
                    Device.status == "active",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        user_session, user, device = row
        if user_session.last_seen_at_epoch <= now - 300:
            user_session.last_seen_at_epoch = now
            device.last_seen_at = _utcnow_iso()
        return AuthPrincipal(
            user_id=user.user_id,
            session_id=user_session.session_id,
            device_id=device.device_id,
        )


async def list_user_sessions(
    *,
    user_id: str,
    current_session_id: str,
    settings: Settings | None = None,
) -> list[SessionView]:
    settings = settings or get_settings()
    maker = get_sessionmaker(settings)
    async with maker() as session:
        rows = (
            await session.execute(
                select(UserSession, Device)
                .join(Device, Device.device_id == UserSession.device_id)
                .where(UserSession.user_id == user_id, UserSession.status == "active")
                .order_by(UserSession.last_seen_at_epoch.desc())
            )
        ).all()
    return [
        SessionView(
            session_id=user_session.session_id,
            device_id=device.device_id,
            platform=device.platform,
            device_name=device.device_name,
            app_version=device.app_version,
            created_at=user_session.created_at,
            last_seen_at_epoch=user_session.last_seen_at_epoch,
            current=user_session.session_id == current_session_id,
        )
        for user_session, device in rows
    ]


async def revoke_user_sessions(
    *,
    user_id: str,
    session_id: str | None,
    reason: str,
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    maker = get_sessionmaker(settings)
    now_iso = _utcnow_iso()
    async with maker.begin() as session:
        stmt = select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.status == "active",
        )
        if session_id is not None:
            stmt = stmt.where(UserSession.session_id == session_id)
        rows = list((await session.execute(stmt.with_for_update())).scalars().all())
        for user_session in rows:
            user_session.status = "revoked"
            user_session.revoked_at = now_iso
            user_session.revoke_reason = reason[:64]
            await session.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.session_id == user_session.session_id,
                    RefreshToken.status == "active",
                )
                .values(status="revoked", consumed_at=now_iso)
            )
        return len(rows)


__all__ = [
    "ACCESS_PREFIX",
    "REFRESH_PREFIX",
    "AppleExchangeResult",
    "AuthPrincipal",
    "AuthServiceError",
    "IssuedTokens",
    "SessionView",
    "authenticate_user_access",
    "create_auth_challenge",
    "exchange_apple_identity",
    "issue_session_for_user",
    "list_user_sessions",
    "revoke_user_sessions",
    "rotate_refresh_token",
]
