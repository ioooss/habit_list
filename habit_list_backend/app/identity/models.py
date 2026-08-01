"""Persistent user identity and opaque-token session models."""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base
from ..db.models import _utcnow_iso, uuid7


class UserIdentity(Base):
    __tablename__ = "user_identities"

    identity_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(24))
    subject: Mapped[str] = mapped_column(String(255))
    email_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)

    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_user_identity_provider_subject"),
    )


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    installation_id_hash: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    device_name: Mapped[str] = mapped_column(String(80), default="")
    app_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "installation_id_hash",
            name="uq_device_user_installation",
        ),
        Index("idx_device_user_status", "user_id", "status", "last_seen_at"),
    )


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    challenge_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    provider: Mapped[str] = mapped_column(String(24), index=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    expires_at_epoch: Mapped[int] = mapped_column(Integer, index=True)
    consumed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)

    __table_args__ = (
        Index("idx_auth_challenge_dispatch", "provider", "status", "expires_at_epoch"),
    )


class UserSession(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.device_id", ondelete="CASCADE"), index=True
    )
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires_at_epoch: Mapped[int] = mapped_column(Integer, index=True)
    refresh_family_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    last_seen_at_epoch: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("idx_session_user_status", "user_id", "status", "last_seen_at_epoch"),)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    refresh_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.session_id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    parent_refresh_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refresh_tokens.refresh_id", ondelete="SET NULL"), nullable=True
    )
    replaced_by_refresh_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refresh_tokens.refresh_id", ondelete="SET NULL"), nullable=True
    )
    expires_at_epoch: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso)
    consumed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_refresh_family_status", "family_id", "status", "expires_at_epoch"),
    )


__all__ = ["AuthChallenge", "Device", "RefreshToken", "UserIdentity", "UserSession"]
