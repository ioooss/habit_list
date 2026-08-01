"""Independent administrator password, TOTP, RBAC, and audit services."""

from __future__ import annotations

import hmac
import re
import time
import unicodedata
from dataclasses import dataclass

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.database import get_sessionmaker
from ..db.models import _utcnow_iso
from ..identity.crypto import identifier_digest, new_opaque_token, token_digest
from .models import (
    AdminAuditEvent,
    AdminRole,
    AdminRolePermission,
    AdminSession,
    AdminUser,
    AdminUserRole,
)

ADMIN_ACCESS_PREFIX = "it_admin_at_"
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-administrator-password")


class AdminAuthError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AdminPrincipal:
    admin_id: str
    admin_session_id: str
    username: str
    display_name: str
    roles: frozenset[str]
    permissions: frozenset[str]


@dataclass(frozen=True)
class AdminAccess:
    admin_id: str
    admin_session_id: str
    access_token: str
    expires_at_epoch: int
    roles: tuple[str, ...]
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class AdminBootstrap:
    admin_id: str
    username: str
    role_code: str
    totp_secret: str
    provisioning_uri: str


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not _USERNAME_RE.fullmatch(normalized):
        raise AdminAuthError(
            "INVALID_ADMIN_USERNAME",
            "管理员用户名只能包含小写字母、数字、点、下划线和连字符",
            422,
        )
    return normalized


def _encrypt_totp(secret: str, settings: Settings) -> str:
    if not settings.admin_mfa_encryption_key:
        raise RuntimeError("ADMIN_MFA_ENCRYPTION_KEY is required")
    return (
        Fernet(settings.admin_mfa_encryption_key.encode("ascii"))
        .encrypt(secret.encode("ascii"))
        .decode("ascii")
    )


def _decrypt_totp(ciphertext: str, settings: Settings) -> str:
    return (
        Fernet(settings.admin_mfa_encryption_key.encode("ascii"))
        .decrypt(ciphertext.encode("ascii"))
        .decode("ascii")
    )


def _context_hash(value: str | None, settings: Settings) -> str | None:
    if not value:
        return None
    return identifier_digest(f"admin-context:{value[:512]}", settings)


async def write_admin_audit(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    outcome: str,
    settings: Settings,
    actor_admin_id: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
) -> None:
    session.add(
        AdminAuditEvent(
            actor_admin_id=actor_admin_id,
            action=action[:96],
            resource_type=resource_type[:64],
            resource_id=resource_id[:128] if resource_id else None,
            outcome=outcome[:16],
            request_id=request_id[:64] if request_id else None,
            source_ip_hash=_context_hash(source_ip, settings),
            user_agent_hash=_context_hash(user_agent, settings),
            metadata_json=metadata or {},
        )
    )


async def _roles_and_permissions(
    session: AsyncSession,
    admin_id: str,
) -> tuple[frozenset[str], frozenset[str]]:
    roles = frozenset(
        (
            await session.execute(
                select(AdminUserRole.role_code).where(AdminUserRole.admin_id == admin_id)
            )
        )
        .scalars()
        .all()
    )
    if not roles:
        return roles, frozenset()
    permissions = frozenset(
        (
            await session.execute(
                select(AdminRolePermission.permission_code).where(
                    AdminRolePermission.role_code.in_(roles)
                )
            )
        )
        .scalars()
        .all()
    )
    return roles, permissions


async def bootstrap_admin(
    *,
    username: str,
    display_name: str,
    password: str,
    role_code: str = "super_admin",
    settings: Settings | None = None,
) -> AdminBootstrap:
    settings = settings or get_settings()
    normalized = normalize_username(username)
    if len(password) < 14 or len(password) > 256:
        raise AdminAuthError(
            "WEAK_ADMIN_PASSWORD",
            "管理员密码长度必须为 14 到 256 个字符",
            422,
        )
    display = display_name.strip()
    if not display or len(display) > 128:
        raise AdminAuthError("INVALID_DISPLAY_NAME", "管理员显示名称格式不正确", 422)
    secret = pyotp.random_base32()
    maker = get_sessionmaker(settings)
    async with maker.begin() as session:
        role = (
            await session.execute(select(AdminRole).where(AdminRole.role_code == role_code))
        ).scalar_one_or_none()
        if role is None:
            raise AdminAuthError("ADMIN_ROLE_NOT_FOUND", "管理员角色不存在", 422)
        existing = (
            await session.execute(
                select(AdminUser).where(AdminUser.username_normalized == normalized)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AdminAuthError("ADMIN_ALREADY_EXISTS", "管理员账号已存在", 409)
        admin = AdminUser(
            username_normalized=normalized,
            display_name=display,
            password_hash=_PASSWORD_HASHER.hash(password),
            totp_secret_ciphertext=_encrypt_totp(secret, settings),
            status="active",
        )
        session.add(admin)
        await session.flush()
        session.add(AdminUserRole(admin_id=admin.admin_id, role_code=role_code))
        await write_admin_audit(
            session,
            actor_admin_id=admin.admin_id,
            action="admin.bootstrap",
            resource_type="admin_user",
            resource_id=admin.admin_id,
            outcome="success",
            settings=settings,
            metadata={"role_code": role_code},
        )
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=normalized,
        issuer_name=settings.admin_totp_issuer,
    )
    return AdminBootstrap(
        admin_id=admin.admin_id,
        username=normalized,
        role_code=role_code,
        totp_secret=secret,
        provisioning_uri=uri,
    )


def _matching_totp_step(secret: str, code: str, now_epoch: int) -> int | None:
    if not code.isdigit() or len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    base_step = now_epoch // totp.interval
    for offset in (-1, 0, 1):
        step = base_step + offset
        if hmac.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


async def login_admin(
    *,
    username: str,
    password: str,
    totp_code: str,
    request_id: str | None,
    source_ip: str | None,
    user_agent: str | None,
    settings: Settings | None = None,
) -> AdminAccess:
    settings = settings or get_settings()
    try:
        normalized = normalize_username(username)
    except AdminAuthError:
        normalized = "invalid-admin-username"
    maker = get_sessionmaker(settings)
    deferred_error: AdminAuthError | None = None
    access: AdminAccess | None = None
    async with maker.begin() as session:
        admin = (
            await session.execute(
                select(AdminUser)
                .where(AdminUser.username_normalized == normalized)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if admin is None:
            try:
                _PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, password)
            except (VerificationError, InvalidHashError):
                pass
            await write_admin_audit(
                session,
                action="admin.login",
                resource_type="admin_user",
                resource_id=identifier_digest(f"unknown-admin:{normalized}", settings),
                outcome="denied",
                request_id=request_id,
                source_ip=source_ip,
                user_agent=user_agent,
                settings=settings,
                metadata={"reason": "invalid_credentials"},
            )
            deferred_error = AdminAuthError(
                "INVALID_ADMIN_CREDENTIALS",
                "管理员凭据或验证码无效",
            )
        else:
            now = int(time.time())
            password_valid = False
            try:
                password_valid = _PASSWORD_HASHER.verify(admin.password_hash, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                password_valid = False
            locked = bool(admin.locked_until_epoch and admin.locked_until_epoch > now)
            totp_step: int | None = None
            if password_valid and not locked and admin.status == "active":
                try:
                    secret = _decrypt_totp(admin.totp_secret_ciphertext, settings)
                    totp_step = _matching_totp_step(secret, totp_code, now)
                except Exception:  # noqa: BLE001 - never expose encrypted MFA failures
                    totp_step = None
            mfa_valid = totp_step is not None and (
                admin.last_totp_step is None or totp_step > admin.last_totp_step
            )
            if not password_valid or not mfa_valid or locked or admin.status != "active":
                if not locked:
                    admin.failed_login_count = int(admin.failed_login_count or 0) + 1
                    if admin.failed_login_count >= settings.admin_max_failed_attempts:
                        admin.locked_until_epoch = now + settings.admin_lockout_seconds
                await write_admin_audit(
                    session,
                    actor_admin_id=admin.admin_id,
                    action="admin.login",
                    resource_type="admin_user",
                    resource_id=admin.admin_id,
                    outcome="denied",
                    request_id=request_id,
                    source_ip=source_ip,
                    user_agent=user_agent,
                    settings=settings,
                    metadata={"reason": "invalid_credentials_or_mfa"},
                )
                deferred_error = AdminAuthError(
                    "INVALID_ADMIN_CREDENTIALS",
                    "管理员凭据或验证码无效",
                )
            else:
                if _PASSWORD_HASHER.check_needs_rehash(admin.password_hash):
                    admin.password_hash = _PASSWORD_HASHER.hash(password)
                admin.failed_login_count = 0
                admin.locked_until_epoch = None
                admin.last_totp_step = totp_step
                admin.last_login_at = _utcnow_iso()
                now_iso = _utcnow_iso()
                await session.execute(
                    update(AdminSession)
                    .where(AdminSession.admin_id == admin.admin_id, AdminSession.status == "active")
                    .values(status="revoked", revoked_at=now_iso, revoke_reason="superseded_login")
                )
                token = new_opaque_token(ADMIN_ACCESS_PREFIX)
                expires = now + settings.admin_access_ttl_seconds
                admin_session = AdminSession(
                    admin_id=admin.admin_id,
                    access_token_hash=token_digest(token, settings),
                    expires_at_epoch=expires,
                    mfa_verified=True,
                    status="active",
                    last_seen_at_epoch=now,
                )
                session.add(admin_session)
                await session.flush()
                roles, permissions = await _roles_and_permissions(session, admin.admin_id)
                await write_admin_audit(
                    session,
                    actor_admin_id=admin.admin_id,
                    action="admin.login",
                    resource_type="admin_session",
                    resource_id=admin_session.admin_session_id,
                    outcome="success",
                    request_id=request_id,
                    source_ip=source_ip,
                    user_agent=user_agent,
                    settings=settings,
                    metadata={"mfa": "totp"},
                )
                access = AdminAccess(
                    admin_id=admin.admin_id,
                    admin_session_id=admin_session.admin_session_id,
                    access_token=token,
                    expires_at_epoch=expires,
                    roles=tuple(sorted(roles)),
                    permissions=tuple(sorted(permissions)),
                )
    if deferred_error is not None:
        raise deferred_error
    if access is None:  # pragma: no cover
        raise RuntimeError("admin login completed without a result")
    return access


async def authenticate_admin_access(
    access_token: str,
    *,
    settings: Settings | None = None,
) -> AdminPrincipal | None:
    settings = settings or get_settings()
    if (
        not access_token
        or not access_token.startswith(ADMIN_ACCESS_PREFIX)
        or len(access_token) > 256
    ):
        return None
    digest = token_digest(access_token, settings)
    now = int(time.time())
    maker = get_sessionmaker(settings)
    async with maker.begin() as session:
        row = (
            await session.execute(
                select(AdminSession, AdminUser)
                .join(AdminUser, AdminUser.admin_id == AdminSession.admin_id)
                .where(
                    AdminSession.access_token_hash == digest,
                    AdminSession.status == "active",
                    AdminSession.mfa_verified.is_(True),
                    AdminSession.expires_at_epoch >= now,
                    AdminUser.status == "active",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        admin_session, admin = row
        if admin_session.last_seen_at_epoch <= now - 300:
            admin_session.last_seen_at_epoch = now
        roles, permissions = await _roles_and_permissions(session, admin.admin_id)
        return AdminPrincipal(
            admin_id=admin.admin_id,
            admin_session_id=admin_session.admin_session_id,
            username=admin.username_normalized,
            display_name=admin.display_name,
            roles=roles,
            permissions=permissions,
        )


async def logout_admin(
    *,
    principal: AdminPrincipal,
    request_id: str | None,
    source_ip: str | None,
    user_agent: str | None,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    maker = get_sessionmaker(settings)
    async with maker.begin() as session:
        admin_session = (
            await session.execute(
                select(AdminSession)
                .where(AdminSession.admin_session_id == principal.admin_session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if admin_session is not None and admin_session.status == "active":
            admin_session.status = "revoked"
            admin_session.revoked_at = _utcnow_iso()
            admin_session.revoke_reason = "admin_logout"
        await write_admin_audit(
            session,
            actor_admin_id=principal.admin_id,
            action="admin.logout",
            resource_type="admin_session",
            resource_id=principal.admin_session_id,
            outcome="success",
            request_id=request_id,
            source_ip=source_ip,
            user_agent=user_agent,
            settings=settings,
        )


__all__ = [
    "ADMIN_ACCESS_PREFIX",
    "AdminAccess",
    "AdminAuthError",
    "AdminBootstrap",
    "AdminPrincipal",
    "authenticate_admin_access",
    "bootstrap_admin",
    "login_admin",
    "logout_admin",
    "normalize_username",
    "write_admin_audit",
]
