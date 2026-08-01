"""Identity provider verification, opaque sessions, and tenant isolation."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_sessionmaker
from app.db.models import User, uuid7
from app.identity.apple import (
    APPLE_ISSUER,
    APPLE_JWKS_URL,
    AppleIdentityClaims,
    AppleIdentityError,
    AppleIdentityUnavailableError,
    clear_apple_jwks_cache,
    verify_apple_identity_token,
)
from app.identity.crypto import decrypt_pii, nonce_digest
from app.identity.models import AuthChallenge, RefreshToken, UserIdentity, UserSession
from app.identity.service import (
    AuthServiceError,
    authenticate_user_access,
    create_auth_challenge,
    exchange_apple_identity,
    issue_session_for_user,
    rotate_refresh_token,
)

pytestmark = pytest.mark.anyio


async def test_apple_provider_outage_is_retryable_and_does_not_consume_challenge(
    app_no_scheduler,
    test_settings: Settings,
):
    challenge_id, raw_nonce, _expires = await create_auth_challenge(test_settings)

    async def unavailable_verifier(
        identity_token: str,
        *,
        expected_nonce_hash: str,
        settings: Settings,
    ) -> AppleIdentityClaims:
        del identity_token, expected_nonce_hash, settings
        raise AppleIdentityUnavailableError("provider outage")

    with pytest.raises(AuthServiceError) as unavailable:
        await exchange_apple_identity(
            challenge_id=challenge_id,
            raw_nonce=raw_nonce,
            identity_token="temporarily-unverifiable-token",
            installation_id="provider-outage-installation-001",
            platform="ios",
            device_name="Offline iPhone",
            app_version="1.0.0",
            locale="zh-CN",
            timezone_name="Asia/Shanghai",
            settings=test_settings,
            verifier=unavailable_verifier,
        )
    assert unavailable.value.code == "IDENTITY_PROVIDER_UNAVAILABLE"
    assert unavailable.value.status_code == 503

    maker = get_sessionmaker(test_settings)
    async with maker() as session:
        challenge = await session.get(AuthChallenge, challenge_id)
    assert challenge is not None
    assert challenge.status == "pending"


async def test_apple_exchange_encrypts_identity_and_detects_refresh_reuse(
    app_no_scheduler,
    test_settings: Settings,
):
    challenge_id, raw_nonce, _expires = await create_auth_challenge(test_settings)
    expected_hash = nonce_digest(raw_nonce)

    async def verifier(
        identity_token: str,
        *,
        expected_nonce_hash: str,
        settings: Settings,
    ) -> AppleIdentityClaims:
        assert identity_token == "signed-by-apple"
        assert expected_nonce_hash == expected_hash
        assert settings is test_settings
        return AppleIdentityClaims(
            subject="apple-subject-user-001",
            audience=test_settings.apple_client_id_list[0],
            nonce=expected_nonce_hash,
            email="alice@example.test",
            email_verified=True,
            is_private_email=True,
            real_user_status=2,
        )

    result = await exchange_apple_identity(
        challenge_id=challenge_id,
        raw_nonce=raw_nonce,
        identity_token="signed-by-apple",
        installation_id="identity-test-installation-0001",
        platform="ios",
        device_name="Alice iPhone",
        app_version="1.0.0",
        locale="zh-CN",
        timezone_name="Asia/Shanghai",
        settings=test_settings,
        verifier=verifier,
    )
    assert result.is_new_user is True
    assert result.email_hint == "a****@example.test"
    assert (
        await authenticate_user_access(
            result.tokens.access_token,
            settings=test_settings,
        )
        is not None
    )

    maker = get_sessionmaker(test_settings)
    async with maker() as session:
        identity = (
            await session.execute(
                select(UserIdentity).where(UserIdentity.user_id == result.tokens.user_id)
            )
        ).scalar_one()
        stored_session = await session.get(UserSession, result.tokens.session_id)
        refresh_rows = list(
            (
                await session.execute(
                    select(RefreshToken).where(RefreshToken.session_id == result.tokens.session_id)
                )
            ).scalars()
        )
    assert identity.email_ciphertext != "alice@example.test"
    assert decrypt_pii(identity.email_ciphertext or "", test_settings) == "alice@example.test"
    assert identity.email_hash != "alice@example.test"
    assert stored_session is not None
    assert stored_session.access_token_hash != result.tokens.access_token
    assert len(refresh_rows) == 1
    assert refresh_rows[0].token_hash != result.tokens.refresh_token

    with pytest.raises(AuthServiceError) as reused_challenge:
        await exchange_apple_identity(
            challenge_id=challenge_id,
            raw_nonce=raw_nonce,
            identity_token="signed-by-apple",
            installation_id="identity-test-installation-0001",
            platform="ios",
            device_name="Alice iPhone",
            app_version="1.0.0",
            locale="zh-CN",
            timezone_name="Asia/Shanghai",
            settings=test_settings,
            verifier=verifier,
        )
    assert reused_challenge.value.code == "INVALID_AUTH_CHALLENGE"

    rotated = await rotate_refresh_token(
        result.tokens.refresh_token,
        settings=test_settings,
    )
    assert rotated.access_token != result.tokens.access_token
    assert rotated.refresh_token != result.tokens.refresh_token
    assert (
        await authenticate_user_access(
            result.tokens.access_token,
            settings=test_settings,
        )
        is None
    )
    assert await authenticate_user_access(rotated.access_token, settings=test_settings) is not None

    with pytest.raises(AuthServiceError) as replay:
        await rotate_refresh_token(
            result.tokens.refresh_token,
            settings=test_settings,
        )
    assert replay.value.code == "REFRESH_TOKEN_REUSE_DETECTED"
    assert await authenticate_user_access(rotated.access_token, settings=test_settings) is None

    async with maker() as session:
        final_refresh_rows = list(
            (
                await session.execute(
                    select(RefreshToken).where(RefreshToken.session_id == result.tokens.session_id)
                )
            ).scalars()
        )
        final_session = await session.get(UserSession, result.tokens.session_id)
    assert final_session is not None
    assert final_session.status == "revoked"
    assert final_session.revoke_reason == "refresh_reuse_detected"
    assert {row.status for row in final_refresh_rows} == {"revoked"}


async def test_apple_jwt_verifier_enforces_signature_audience_and_nonce(
    respx_mock,
    test_settings: Settings,
):
    clear_apple_jwks_cache()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "apple-test-key", "alg": "RS256", "use": "sig"})
    respx_mock.get(APPLE_JWKS_URL).mock(
        return_value=httpx.Response(200, json={"keys": [public_jwk]})
    )
    now = int(time.time())
    expected_nonce = nonce_digest("raw-native-nonce")
    payload = {
        "iss": APPLE_ISSUER,
        "aud": test_settings.apple_client_id_list[0],
        "exp": now + 300,
        "iat": now,
        "sub": "apple-jwt-subject",
        "nonce": expected_nonce,
        "email": "JWT.User@Example.Test",
        "email_verified": "true",
        "is_private_email": "false",
        "real_user_status": 2,
    }
    token = jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": "apple-test-key"},
    )

    claims = await verify_apple_identity_token(
        token,
        expected_nonce_hash=expected_nonce,
        settings=test_settings,
    )
    assert claims.subject == "apple-jwt-subject"
    assert claims.email == "jwt.user@example.test"
    assert claims.email_verified is True

    with pytest.raises(AppleIdentityError, match="nonce"):
        await verify_apple_identity_token(
            token,
            expected_nonce_hash=nonce_digest("different-nonce"),
            settings=test_settings,
        )

    wrong_audience_token = jwt.encode(
        {**payload, "aud": "com.example.wrong-app"},
        private_key,
        algorithm="RS256",
        headers={"kid": "apple-test-key"},
    )
    with pytest.raises(AppleIdentityError, match="verification failed"):
        await verify_apple_identity_token(
            wrong_audience_token,
            expected_nonce_hash=expected_nonce,
            settings=test_settings,
        )
    clear_apple_jwks_cache()


async def test_session_api_is_public_only_where_intended_and_tenant_scoped(
    client: AsyncClient,
    test_settings: Settings,
):
    challenge_request = client.build_request("POST", "/api/v1/auth/challenges")
    challenge_request.headers.pop("Authorization", None)
    challenge_response = await client.send(challenge_request)
    assert challenge_response.status_code == 201
    assert challenge_response.json()["nonce"]

    near_match_request = client.build_request("POST", "/api/v1/auth/challenges-extra")
    near_match_request.headers.pop("Authorization", None)
    assert (await client.send(near_match_request)).status_code == 401

    other_user_id = str(uuid7())
    maker = get_sessionmaker(test_settings)
    async with maker.begin() as session:
        session.add(User(user_id=other_user_id, locale="en-US", timezone="UTC"))
    other_tokens = await issue_session_for_user(
        user_id=other_user_id,
        installation_id="other-user-installation-0001",
        platform="android",
        device_name="Other phone",
        settings=test_settings,
    )

    response = await client.delete(f"/api/v1/auth/sessions/{other_tokens.session_id}")
    assert response.status_code == 404
    assert (
        await authenticate_user_access(
            other_tokens.access_token,
            settings=test_settings,
        )
        is not None
    )

    sessions = await client.get("/api/v1/auth/sessions")
    assert sessions.status_code == 200
    assert all(item["session_id"] != other_tokens.session_id for item in sessions.json()["items"])
