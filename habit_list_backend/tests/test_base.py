"""基础：健康检查 / 鉴权 / CORS。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
import tests  # noqa: F401  fixtures


pytestmark = pytest.mark.anyio


async def test_health_no_auth(client: AsyncClient):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_auth_required(client: AsyncClient):
    # 不带 Authorization 应该 401
    req = client.build_request("GET", "/api/v1/me/profile")
    req.headers.pop("Authorization", None)
    r = await client.send(req)
    assert r.status_code == 401


async def test_auth_admin_equals_valid(client: AsyncClient, test_settings):
    # ADMIN_TOKEN 同样应通过
    req = client.build_request("GET", "/api/v1/memos?filter=all")
    req.headers["Authorization"] = f"Bearer {test_settings.admin_token}"
    r = await client.send(req)
    assert r.status_code == 200


async def test_profile_default(client: AsyncClient, test_settings):
    r = await client.get("/api/v1/me/profile")
    assert r.status_code == 200
    j = r.json()
    assert j["user_id"] == test_settings.default_user_id
    # 默认 procedural 有 reply_speed 等 7 条
    keys = {p["param_key"] for p in j["params"]}
    assert {"reply_speed", "reply_length", "proactivity", "tone_gentle"} <= keys
