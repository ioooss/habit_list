"""Strict Sign in with Apple identity-token verification."""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from ..core.config import Settings

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_JWKS_BY_KID: dict[str, tuple[float, dict[str, Any]]] = {}


class AppleIdentityError(ValueError):
    """The provider token failed cryptographic or claim validation."""


class AppleIdentityUnavailableError(RuntimeError):
    """Apple's key service could not be reached or returned unusable data."""


@dataclass(frozen=True)
class AppleIdentityClaims:
    subject: str
    audience: str
    nonce: str
    email: str | None
    email_verified: bool
    is_private_email: bool
    real_user_status: int | None


def _bool_claim(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).casefold() == "true"


async def _fetch_jwks(settings: Settings) -> dict[str, dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                APPLE_JWKS_URL,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppleIdentityUnavailableError("Apple JWKS is temporarily unavailable") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, list) or not keys:
        raise AppleIdentityUnavailableError("Apple JWKS response is invalid")
    now = time.time()
    result: dict[str, dict[str, Any]] = {}
    for item in keys:
        if not isinstance(item, dict):
            continue
        kid = str(item.get("kid") or "")
        if not kid or item.get("kty") != "RSA":
            continue
        if item.get("alg") not in {None, "RS256"}:
            continue
        result[kid] = item
        _JWKS_BY_KID[kid] = (now + settings.apple_jwks_cache_seconds, item)
    if not result:
        raise AppleIdentityUnavailableError("Apple JWKS contains no supported signing key")
    return result


async def _key_for_kid(kid: str, settings: Settings):
    cached = _JWKS_BY_KID.get(kid)
    if cached and cached[0] > time.time():
        key_data = cached[1]
    else:
        keys = await _fetch_jwks(settings)
        key_data = keys.get(kid)
    if key_data is None:
        # A key rotation may race the cache. One forced refresh is bounded and safe.
        _JWKS_BY_KID.clear()
        key_data = (await _fetch_jwks(settings)).get(kid)
    if key_data is None:
        raise AppleIdentityError("Apple signing key is unknown")
    return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))


async def verify_apple_identity_token(
    identity_token: str,
    *,
    expected_nonce_hash: str,
    settings: Settings,
) -> AppleIdentityClaims:
    if not settings.apple_client_id_list:
        raise RuntimeError("APPLE_CLIENT_IDS is required for Sign in with Apple")
    if not identity_token or len(identity_token) > 8192:
        raise AppleIdentityError("Apple identity token has an invalid size")
    try:
        header = jwt.get_unverified_header(identity_token)
    except jwt.PyJWTError as exc:
        raise AppleIdentityError("Apple identity token header is invalid") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise AppleIdentityError("Apple identity token algorithm is not allowed")

    try:
        key = await _key_for_kid(str(header["kid"]), settings)
        claims = jwt.decode(
            identity_token,
            key=key,
            algorithms=["RS256"],
            audience=settings.apple_client_id_list,
            issuer=APPLE_ISSUER,
            leeway=30,
            options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
        )
    except AppleIdentityUnavailableError:
        raise
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise AppleIdentityError("Apple identity token verification failed") from exc

    nonce = str(claims.get("nonce") or "")
    if not nonce or not hmac.compare_digest(nonce, expected_nonce_hash):
        raise AppleIdentityError("Apple identity token nonce does not match")
    subject = str(claims.get("sub") or "")
    if not subject or len(subject) > 255:
        raise AppleIdentityError("Apple identity token subject is invalid")
    audience_claim = claims.get("aud")
    audience = (
        str(audience_claim[0])
        if isinstance(audience_claim, list) and audience_claim
        else str(audience_claim or "")
    )
    email_value = claims.get("email")
    email = str(email_value).strip().casefold() if email_value else None
    real_status = claims.get("real_user_status")
    return AppleIdentityClaims(
        subject=subject,
        audience=audience,
        nonce=nonce,
        email=email,
        email_verified=_bool_claim(claims.get("email_verified")),
        is_private_email=_bool_claim(claims.get("is_private_email")),
        real_user_status=int(real_status) if isinstance(real_status, int) else None,
    )


def clear_apple_jwks_cache() -> None:
    _JWKS_BY_KID.clear()


__all__ = [
    "APPLE_ISSUER",
    "APPLE_JWKS_URL",
    "AppleIdentityClaims",
    "AppleIdentityError",
    "AppleIdentityUnavailableError",
    "clear_apple_jwks_cache",
    "verify_apple_identity_token",
]
