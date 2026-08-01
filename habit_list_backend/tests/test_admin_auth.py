"""Independent administrator MFA, RBAC, audit, and auth-plane isolation."""

from __future__ import annotations

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.admin.models import AdminAuditEvent, AdminSession, AdminUser
from app.admin.service import bootstrap_admin
from app.core.config import Settings
from app.db.database import get_sessionmaker

pytestmark = pytest.mark.anyio

ADMIN_PASSWORD = "VeryStrongAdminPassword!2026"


async def _login(
    client: AsyncClient,
    *,
    username: str,
    password: str,
    totp_code: str,
):
    return await client.post(
        "/admin/v1/auth/login",
        json={
            "username": username,
            "password": password,
            "totp_code": totp_code,
        },
        headers={"User-Agent": "pytest-admin-client/1.0"},
    )


async def test_admin_mfa_rbac_audit_and_auth_planes_are_isolated(
    client: AsyncClient,
    test_settings: Settings,
):
    bootstrap = await bootstrap_admin(
        username="Primary.Admin",
        display_name="Primary Operator",
        password=ADMIN_PASSWORD,
        role_code="super_admin",
        settings=test_settings,
    )
    code = pyotp.TOTP(bootstrap.totp_secret).now()
    response = await _login(
        client,
        username="PRIMARY.ADMIN",
        password=ADMIN_PASSWORD,
        totp_code=code,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"].startswith("it_admin_at_")
    assert payload["roles"] == ["super_admin"]
    assert "audit.read" in payload["permissions"]
    admin_headers = {"Authorization": f"Bearer {payload['access_token']}"}

    me = await client.get("/admin/v1/auth/me", headers=admin_headers)
    assert me.status_code == 200
    assert me.json()["username"] == "primary.admin"
    assert me.json()["display_name"] == "Primary Operator"

    audit = await client.get("/admin/v1/audit-events", headers=admin_headers)
    assert audit.status_code == 200
    audit_payload = audit.json()
    assert {item["action"] for item in audit_payload["items"]} >= {
        "admin.bootstrap",
        "admin.login",
    }
    assert all("source_ip_hash" not in item for item in audit_payload["items"])
    assert all("user_agent_hash" not in item for item in audit_payload["items"])

    replay = await _login(
        client,
        username="primary.admin",
        password=ADMIN_PASSWORD,
        totp_code=code,
    )
    assert replay.status_code == 401

    user_plane_with_admin_token = await client.get(
        "/api/v1/auth/me",
        headers=admin_headers,
    )
    assert user_plane_with_admin_token.status_code == 401

    user_token = client.headers["Authorization"]
    admin_plane_with_user_token = await client.get(
        "/admin/v1/auth/me",
        headers={"Authorization": user_token},
    )
    assert admin_plane_with_user_token.status_code == 401

    maker = get_sessionmaker(test_settings)
    async with maker() as session:
        admin = (
            await session.execute(select(AdminUser).where(AdminUser.admin_id == bootstrap.admin_id))
        ).scalar_one()
        stored_session = (
            await session.execute(
                select(AdminSession).where(
                    AdminSession.admin_session_id == payload["admin_session_id"]
                )
            )
        ).scalar_one()
        audit_rows = list((await session.execute(select(AdminAuditEvent))).scalars())
    assert admin.password_hash.startswith("$argon2")
    assert ADMIN_PASSWORD not in admin.password_hash
    assert bootstrap.totp_secret not in admin.totp_secret_ciphertext
    assert stored_session.access_token_hash != payload["access_token"]
    assert all(ADMIN_PASSWORD not in str(row.metadata_json) for row in audit_rows)
    assert all(bootstrap.totp_secret not in str(row.metadata_json) for row in audit_rows)

    logout = await client.post("/admin/v1/auth/logout", headers=admin_headers)
    assert logout.status_code == 204
    assert (await client.get("/admin/v1/auth/me", headers=admin_headers)).status_code == 401


async def test_analyst_cannot_read_administrator_audit_log(
    client: AsyncClient,
    test_settings: Settings,
):
    bootstrap = await bootstrap_admin(
        username="metrics.analyst",
        display_name="Metrics Analyst",
        password=ADMIN_PASSWORD,
        role_code="analyst",
        settings=test_settings,
    )
    response = await _login(
        client,
        username=bootstrap.username,
        password=ADMIN_PASSWORD,
        totp_code=pyotp.TOTP(bootstrap.totp_secret).now(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["roles"] == ["analyst"]
    assert payload["permissions"] == ["metrics.read"]

    audit = await client.get(
        "/admin/v1/audit-events",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert audit.status_code == 403
