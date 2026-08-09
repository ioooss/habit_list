"""pytest mock helpers（DashScope API mock + 每个用例后单例清理）。"""
from __future__ import annotations

from typing import Any


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
    import httpx

    from app.core.config import Settings

    assert isinstance(settings, Settings)
    base = httpx.URL(settings.dashscope_base_url)
    root = str(base.copy_with(path="", query=None, fragment=None)).rstrip("/")
    respx_mock.post(f"{root}/api/v1/services/aigc/multimodal-generation/generation").respond(
        200,
        json={
            "request_id": "asr-submit-test-1",
            "output": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": text}],
                        }
                    }
                ]
            },
        },
    )


def mock_dashscope_tts(respx_mock, settings, payload=b"RIFF----WAVEfmt fake"):
    import base64

    import httpx

    from app.core.config import Settings

    assert isinstance(settings, Settings)
    endpoint = str(
        httpx.URL(settings.dashscope_base_url).copy_with(
            path="/api/v1/services/audio/tts/SpeechSynthesizer", query=None, fragment=None
        )
    )
    respx_mock.post(
        endpoint
    ).respond(
        200,
        json={
            "request_id": "tts-req-test-1",
            "output": {
                "finish_reason": "stop",
                "audio": {
                    "data": base64.b64encode(payload).decode("ascii"),
                    "url": "",
                    "id": "audio_test",
                },
            },
            "usage": {"characters": 1},
        },
    )
