"""Administrator identity, RBAC, sessions, and append-only audit models."""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base
from ..db.models import _utcnow_iso, uuid7


class AdminUser(Base):
    __tablename__ = "admin_users"

    admin_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    username_normalized: Mapped[str] = mapped_column(String(128), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(Text)
    totp_secret_ciphertext: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_totp_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    password_changed_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    last_login_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, onupdate=_utcnow_iso)


class AdminRole(Base):
    __tablename__ = "admin_roles"

    role_code: Mapped[str] = mapped_column(String(48), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(96))
    description: Mapped[str] = mapped_column(String(256), default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)


class AdminPermission(Base):
    __tablename__ = "admin_permissions"

    permission_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)


class AdminRolePermission(Base):
    __tablename__ = "admin_role_permissions"

    role_code: Mapped[str] = mapped_column(
        String(48),
        ForeignKey("admin_roles.role_code", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("admin_permissions.permission_code", ondelete="CASCADE"),
        primary_key=True,
    )


class AdminUserRole(Base):
    __tablename__ = "admin_user_roles"

    admin_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("admin_users.admin_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_code: Mapped[str] = mapped_column(
        String(48),
        ForeignKey("admin_roles.role_code", ondelete="CASCADE"),
        primary_key=True,
    )
    assigned_by_admin_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    admin_session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    admin_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("admin_users.admin_id", ondelete="CASCADE"), index=True
    )
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at_epoch: Mapped[int] = mapped_column(Integer, index=True)
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    last_seen_at_epoch: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_admin_session_principal", "admin_id", "status", "expires_at_epoch"),
    )


class AdminAuditEvent(Base):
    __tablename__ = "admin_audit_events"

    audit_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    actor_admin_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(96), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)

    __table_args__ = (Index("idx_admin_audit_time_action", "created_at", "action", "outcome"),)


__all__ = [
    "AdminAuditEvent",
    "AdminPermission",
    "AdminRole",
    "AdminRolePermission",
    "AdminSession",
    "AdminUser",
    "AdminUserRole",
]
