"""备忘接口。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

import tests  # noqa: F401

pytestmark = pytest.mark.anyio


async def test_detect_memo(client: AsyncClient):
    r = await client.post("/api/v1/memos/detect", json={"text": "明天下午3点提醒我交周报"})
    assert r.status_code == 200
    j = r.json()
    assert j["hit"] is True
    assert "明天" in j["due_text"] or "下午" in j["due_text"]
    assert j["importance"] in {"yellow", "red"}
    assert "交周报" in j["clean_text"]


async def test_detect_memo_plain_confide(client: AsyncClient):
    r = await client.post("/api/v1/memos/detect", json={"text": "今天一个人吃饭有点孤单"})
    assert r.status_code == 200
    assert r.json()["hit"] is False


async def test_create_list_patch_batch_done(client: AsyncClient):
    # 1) create
    r = await client.post("/api/v1/memos", json={
        "text": "明天下午3点交周报",
        "importance": "yellow",
    })
    assert r.status_code == 200
    m1 = r.json()
    assert m1["status"] == "pending"
    mid1 = m1["memo_id"]

    r2 = await client.post("/api/v1/memos", json={
        "text": "买药",
        "due_text": "今晚",
        "importance": "red",
    })
    assert r2.status_code == 200
    m2 = r2.json()
    assert m2["group"] in {"today", "overdue"}

    # 2) list all
    lst = await client.get("/api/v1/memos", params={"filter": "all"})
    assert lst.status_code == 200
    data = lst.json()
    assert data["stats"]["todo"] >= 2
    ids = {i["memo_id"] for i in data["items"]}
    assert mid1 in ids and m2["memo_id"] in ids
    # group 键至少有 today/week/done 里一部分
    assert data["groups"]

    # 3) filter red
    r_red = await client.get("/api/v1/memos", params={"filter": "red"})
    j = r_red.json()
    assert all(i["importance"] == "red" for i in j["items"])

    # 4) patch status -> done
    r = await client.patch(f"/api/v1/memos/{mid1}", json={"status": "done"})
    assert r.status_code == 200
    assert r.json()["status"] == "done"

    # 5) batch_done
    r = await client.post("/api/v1/memos/batch_done", json={"memo_ids": [m2["memo_id"]]})
    assert r.status_code == 200
    assert r.json()["done_count"] >= 1
    # 切 done 过滤器都能看到
    r = await client.get("/api/v1/memos", params={"filter": "done"})
    j = r.json()
    assert mid1 in {i["memo_id"] for i in j["items"]}

    # 6) q 搜
    r = await client.get("/api/v1/memos", params={"filter": "all", "q": "交周报"})
    j = r.json()
    # 改 done 了不在 all 里：换成 done
    r = await client.get("/api/v1/memos", params={"filter": "done", "q": "交周报"})
    j = r.json()
    assert any("交周报" in i["text"] or "交周报" in i["clean_text"] for i in j["items"])


async def test_patch_not_found_404(client: AsyncClient):
    r = await client.patch("/api/v1/memos/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", json={"text": "x"})
    assert r.status_code == 404
