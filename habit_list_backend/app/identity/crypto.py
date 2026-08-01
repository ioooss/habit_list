"""Secret handling primitives for identities and opaque bearer tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

from ..core.config import Settings


def new_opaque_token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def token_digest(token: str, settings: Settings) -> str:
    if not settings.auth_token_pepper:
        raise RuntimeError("AUTH_TOKEN_PEPPER is required in sessions mode")
    return hmac.new(
        settings.auth_token_pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def identifier_digest(value: str, settings: Settings) -> str:
    return token_digest(f"identifier:{value}", settings)


def nonce_digest(raw_nonce: str) -> str:
    return hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()


def encrypt_pii(value: str, settings: Settings) -> str:
    if not settings.pii_encryption_key:
        raise RuntimeError("PII_ENCRYPTION_KEY is required in sessions mode")
    return (
        Fernet(settings.pii_encryption_key.encode("ascii"))
        .encrypt(value.encode("utf-8"))
        .decode("ascii")
    )


def decrypt_pii(value: str, settings: Settings) -> str:
    return (
        Fernet(settings.pii_encryption_key.encode("ascii"))
        .decrypt(value.encode("ascii"))
        .decode("utf-8")
    )


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    if not local or not domain:
        return None
    visible = local[:1]
    return f"{visible}{'*' * min(max(len(local) - 1, 2), 8)}@{domain}"


__all__ = [
    "decrypt_pii",
    "encrypt_pii",
    "identifier_digest",
    "mask_email",
    "new_opaque_token",
    "nonce_digest",
    "token_digest",
]
