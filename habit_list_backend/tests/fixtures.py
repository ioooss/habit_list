"""pytest mock helpers（DashScope API mock + 每个用例后单例清理）。"""
from __future__ import annotations

from typing import Any

import pytest


# ---------- DashScope mock helpers（从 tests 包 re-export，供测试用例直接调用）----------
def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


def _json_str2(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def mock_dashscope_chat_stream(respx_mock, settings, parts: list[str], usage: dict | None = None):
    """让 POST compatible-mode/v1/chat/completions 返回 SSE data lines。"""
    from app.core.config import Settings
    assert isinstance(settings, Settings)

    def _sse_bytes():
        # 直接拼成完整 bytes（新版 httpx 不再导出 Stream 类，全量返回不影响测试用异步迭代）
        out: list[bytes] = []
        for p in parts:
            payload = 'data: {"choices":[{"delta":{"content":' + _json_str(p) + '}}]}\n\n'
            out.append(payload.encode())
        if usage:
            payload = 'data: {"usage":' + _json_str2(usage) + '}\n\n'
            out.append(payload.encode())
        out.append(b"data: [DONE]\n\n")
        return b"".join(out)

    def _handler(req):
        from httpx import Response
        return Response(
            200,
            content=_sse_bytes(),
            headers={"Content-Type": "text/event-stream", "X-Request-ID": "llm-req-test-1"},
        )

    respx_mock.post(f"{settings.dashscope_base_url}/chat/completions").side_effect = _handler


def mock_dashscope_embeddings(respx_mock, settings, dim=None, n=1):
    """Embedding 返回 n 条 dim 维全 0.01 向量（够测排序流程）。
    dim 默认读 settings.dashscope_embedding_dim（qwen3.7-text-embedding=1024, text-embedding-v3=1536）。"""
    from app.core.config import Settings
    assert isinstance(settings, Settings)
    if dim is None:
        dim = int(getattr(settings, "dashscope_embedding_dim", 1024) or 1024)
    data = [{"index": i, "embedding": [0.01] * dim} for i in range(n)]
    respx_mock.post(f"{settings.dashscope_base_url}/embeddings").respond(
        200, json={"object": "list", "data": data, "model": settings.dashscope_embedding_model},
    )


def mock_dashscope_asr(respx_mock, settings, text="今天下午3点提醒我交周报"):
    from app.core.config import Settings
    assert isinstance(settings, Settings)
    respx_mock.post(f"{settings.dashscope_base_url}/audio/transcriptions").respond(
        200, json={"text": text}
    )


def mock_dashscope_tts(respx_mock, settings, payload=b"RIFF----WAVEfmt fake"):
    from app.core.config import Settings
    assert isinstance(settings, Settings)
    respx_mock.post(f"{settings.dashscope_base_url}/audio/speech").respond(
        200, content=payload, headers={"Content-Type": "audio/wav"}
    )


@pytest.fixture(autouse=True)
def _clear_singletons_between_tests():
    """每个用例后清空 httpx client / APScheduler / sqlite engine / 知识图谱 单例，避免串数据。"""
    yield
    from app.db import database as db_mod
    from app.memory import system2 as system2_mod
    from app.providers import dashscope as dashscope_provider
    from app.retrieval import graph as graph_mod

    db_mod._engines.clear()
    db_mod._sessionmakers.clear()
    dashscope_provider._clients.clear()
    system2_mod._scheduler = None
    graph_mod._GRAPHS_BY_USER.clear()
    graph_mod._NODE_NORM_NAMES_BY_USER.clear()
    graph_mod._GRAPH_VERSION += 1
    # 清 settings 缓存，确保下次取的是最新
    from app.core.config import get_settings
    get_settings.cache_clear()
