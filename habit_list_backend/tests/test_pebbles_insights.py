"""pebbles / insights 接口。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

import tests  # noqa: F401

pytestmark = pytest.mark.anyio


async def _create_memo(client, text, importance="yellow", due_text="明天"):
    r = await client.post("/api/v1/memos", json={"text": text, "importance": importance, "due_text": due_text})
    assert r.status_code == 200
    return r.json()


async def _create_insight(client, payload=None):
    """直接在库里插一条假 insight（REST 没暴露 create；通过 SQL 插）。"""
    from app.db.database import get_sessionmaker
    from app.db.models import Insight
    maker = get_sessionmaker()
    async with maker() as s:
        ins = Insight(
            user_id=client.headers.get("X-Test-Uid") or "01920000-0000-0000-0000-000000000001",
            type=payload.get("type") if payload else "关联·pattern",
            text_html=payload.get("text_html") if payload else "你<em>冥想</em>的那天，<em>阅读时长</em>也更高。",
            meta=payload.get("meta") if payload else "基于 6 周数据 · 信心度高",
            confidence=payload.get("confidence") if payload else 0.78,
            evidence_json={"episodic_ids": []},
        )
        s.add(ins)
        await s.commit()
        await s.refresh(ins)
        return ins.insight_id


async def test_pebble_patch_kind_then_filter(client: AsyncClient):
    await _create_memo(client, "写周报", importance="yellow", due_text="明天晚上")
    r = await client.get("/api/v1/pebbles")
    assert r.status_code == 200
    j = r.json()
    assert j["total"] >= 1
    # 取第一颗存在的石子
    pid = None
    for g in j["groups"]:
        for it in g["items"]:
            pid = it["episodic_id"]
            break
        if pid:
            break
    assert pid is not None
    # 改 kind confide → life_fragment，并捞起
    r = await client.patch(f"/api/v1/pebbles/{pid}", json={
        "kind": "life_fragment",
        "emotion": "🍊",
        "summary_1line": "这周写周报写得很顺手",
        "land_it": True,
    })
    assert r.status_code == 200
    patched = r.json()
    assert patched["kind"] == "life_fragment"
    assert patched["kind_fixed_from"] in {"memo", "confide"}
    assert patched["emotion"] == "🍊"

    # filter=life_fragment 能找到它
    r = await client.get("/api/v1/pebbles", params={"kind": "life_fragment"})
    assert r.status_code == 200
    j = r.json()
    ids = []
    for g in j["groups"]:
        for it in g["items"]:
            ids.append(it["episodic_id"])
    assert pid in ids


async def test_pebble_archive(client: AsyncClient):
    await _create_memo(client, "临时备忘", importance="green", due_text="下周")
    pid = None
    for g in (await client.get("/api/v1/pebbles")).json()["groups"]:
        for it in g["items"]:
            pid = it["episodic_id"]
            break
        if pid:
            break
    r = await client.request("DELETE", f"/api/v1/pebbles/{pid}")
    assert r.status_code == 200
    r = await client.get("/api/v1/pebbles")
    remaining = []
    for g in r.json()["groups"]:
        remaining += g["items"]
    assert pid not in {x["episodic_id"] for x in remaining}


async def test_insight_confirm_then_deny(client: AsyncClient):
    iid = await _create_insight(client)
    r = await client.post(f"/api/v1/insights/{iid}/confirm", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    iid2 = await _create_insight(client, {"text_html": "再一条，不想要了", "type": "趋势·drift", "confidence": 0.6})
    r = await client.post(f"/api/v1/insights/{iid2}/deny", params={"reason": "不对"})
    assert r.status_code == 200

    r = await client.get("/api/v1/insights", params={"status": "all"})
    assert r.status_code == 200
    j = r.json()
    types = {x["insight_id"]: x["status"] for x in j["items"]}
    assert types[iid] == "confirmed"
    assert types[iid2] == "denied"
