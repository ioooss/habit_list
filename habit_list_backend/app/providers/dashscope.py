"""阿里云 DashScope 兼容 OpenAI /compatible-mode/v1 模式的异步封装。

覆盖：
- chat/completions       SSE 流式（共处 AI 回应）
- embeddings             qwen-embedding-v3（向量化用于向量检索）
- audio/transcriptions   paraformer-v2（ASR 语音转文字，multipart）
- audio/speech           cosyvoice-v1（TTS 文字转语音）
- moderations            内容审核（敏感词）

使用 tenacity 做 2 次指数退避；用 sliding log 做 RPM 限流。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Optional,
)

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ..core.config import Settings, get_settings

log = logging.getLogger("habit_list.providers.dashscope")


# =========================================================
# 极简速率限制（每分钟请求数，滑动窗口）
# =========================================================
class RPMThrottler:
    def __init__(self, rpm: int):
        self.rpm = max(rpm, 1)
        self._hits: deque[float] = deque()

    async def wait(self) -> None:
        now = time.monotonic()
        # 弹出 60 秒以前的
        while self._hits and now - self._hits[0] >= 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.rpm:
            sleep_s = 60.0 - (now - self._hits[0]) + 0.05
            log.info("RPM throttle: sleeping %.2fs", sleep_s)
            await asyncio.sleep(max(sleep_s, 0.0))
            # 醒了再递归查一次（其他并发可能也占满了）
            await self.wait()
            return
        self._hits.append(time.monotonic())


_throttlers: dict[int, RPMThrottler] = {}


def _throttler(rpm: int) -> RPMThrottler:
    if rpm not in _throttlers:
        _throttlers[rpm] = RPMThrottler(rpm)
    return _throttlers[rpm]


# =========================================================
# HTTP 客户端（单例 AsyncClient）
# =========================================================
_clients: dict[str, httpx.AsyncClient] = {}


def _get_client(settings: Settings) -> httpx.AsyncClient:
    key = settings.dashscope_base_url
    if key not in _clients:
        _clients[key] = httpx.AsyncClient(
            base_url=settings.dashscope_base_url,
            timeout=httpx.Timeout(settings.dashscope_timeout_sec, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
            headers={
                "User-Agent": "habit-list-backend/0.1",
            },
        )
    return _clients[key]


async def shutdown_clients() -> None:  # pragma: no cover - 测试结束用
    for c in _clients.values():
        await c.aclose()
    _clients.clear()


# =========================================================
# 通用重试
# =========================================================
async def _run_with_retry(
    fn: Callable[[], Awaitable[Any]],
    settings: Settings,
    *,
    description: str,
) -> Any:
    await _throttler(settings.rpm_limit_per_min).wait()
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max(settings.dashscope_max_retry, 0) + 1),
            wait=wait_random_exponential(multiplier=1.2, max=8),
            retry=retry_if_exception_type((httpx.HTTPError, httpx.RemoteProtocolError, asyncio.TimeoutError)),
            reraise=True,
        ):
            with attempt:
                if attempt.retry_state.attempt_number > 1:
                    log.warning("DashScope retry %s: %s", attempt.retry_state.attempt_number, description)
                return await fn()
    except RetryError as e:
        log.error("DashScope 最终失败: %s", description)
        raise RuntimeError(f"DashScope {description} 失败") from e


# =========================================================
# 聊天（SSE 流式）
# =========================================================
@dataclass
class SSEEvent:
    kind: Literal["delta", "done", "error", "meta"]
    data: str = ""
    request_id: Optional[str] = None
    usage: Optional[dict[str, int]] = None


async def chat_stream(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.85,
    max_tokens: int = 1024,
    top_p: float = 0.95,
    model: Optional[str] = None,
    request_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> AsyncIterator[SSEEvent]:
    settings = settings or get_settings()
    model = model or settings.dashscope_llm_model
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if request_id:
        headers["X-Request-ID"] = request_id
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stream": True,
    }

    async def _call() -> httpx.Response:
        client = _get_client(settings)
        resp = await client.post("/chat/completions", headers=headers, json=payload)
        if resp.status_code >= 400:
            await resp.aread()
            log.error("DashScope chat HTTP %s model=%s", resp.status_code, model)
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
        return resp

    try:
        resp: httpx.Response = await _run_with_retry(
            _call, settings, description=f"chat/stream model={model}"
        )
    except Exception as e:  # noqa: BLE001
        yield SSEEvent(kind="error", data=str(e))
        return

    llm_req_id = resp.headers.get("X-Request-ID") or request_id
    full = []
    try:
        async for raw in resp.aiter_lines():
            raw = raw.strip()
            if not raw:
                continue
            if not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                yield SSEEvent(kind="done", request_id=llm_req_id)
                return
            # parse delta
            try:
                import orjson
                obj = orjson.loads(data)
            except Exception as e:  # noqa: BLE001
                log.warning("SSE parse fail: %s", e)
                continue
            try:
                chunk = obj["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                chunk = ""
            if chunk:
                full.append(chunk)
                yield SSEEvent(kind="delta", data=chunk, request_id=llm_req_id)
            if "usage" in obj and obj["usage"]:
                yield SSEEvent(kind="meta", usage=obj["usage"], request_id=llm_req_id)
    except httpx.StreamError as e:
        yield SSEEvent(kind="error", data=f"stream断开: {e}")
    finally:
        try:
            await resp.aclose()
        except Exception:  # noqa: BLE001
            pass


# =========================================================
# 结构化生成（Memory V2 / 安全分类 / 动作预览）
# =========================================================
async def chat_json(
    messages: list[dict[str, Any]],
    *,
    json_schema: dict[str, Any],
    schema_name: str,
    temperature: float = 0.1,
    max_tokens: int = 1200,
    model: Optional[str] = None,
    request_id: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Generate and strictly parse one JSON object.

    The provider receives both ``response_format=json_object`` and the JSON
    schema in the payload.  Some compatible endpoints ignore the schema field,
    so callers must still validate the returned object with Pydantic.  Invalid
    JSON is an explicit failure and is never repaired with regular expressions.
    """

    settings = settings or get_settings()
    model = model or settings.dashscope_llm_model
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key.strip()}",
        "Content-Type": "application/json",
    }
    if request_id:
        headers["X-Request-ID"] = request_id
    try:
        import orjson

        schema_text = orjson.dumps(json_schema).decode("utf-8")
    except Exception:  # pragma: no cover - orjson 是项目依赖，仅作保险
        import json

        schema_text = json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
    schema_instruction = {
        "role": "system",
        "content": (
            f"输出必须是符合 JSON Schema `{schema_name}` 的单个 JSON 对象；"
            f"不得输出 Markdown 或解释。Schema: {schema_text}"
        ),
    }
    payload = {
        "model": model,
        "messages": [schema_instruction, *messages],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {"type": "json_object"},
    }

    async def _call() -> dict[str, Any]:
        client = _get_client(settings)
        resp = await client.post("/chat/completions", headers=headers, json=payload)
        if resp.status_code >= 400:
            await resp.aread()
            log.error("DashScope JSON HTTP %s schema=%s", resp.status_code, schema_name)
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"structured response missing content: {schema_name}") from exc
        if isinstance(content, dict):
            return content
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"structured response empty: {schema_name}")
        try:
            import orjson

            parsed = orjson.loads(content)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"structured response is not valid JSON: {schema_name}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"structured response must be an object: {schema_name}")
        return parsed

    return await _run_with_retry(_call, settings, description=f"chat/json schema={schema_name}")


# =========================================================
# Embedding（批量）
# =========================================================
async def embed_texts(
    texts: Iterable[str],
    *,
    model: Optional[str] = None,
    dimensions: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> list[list[float]]:
    """调用 DashScope 兼容模式 embedding。
    注意：兼容模式下 DashScope 模型普遍不支持传 dimensions 参数，
    dimensions 仅用于「API 返回数量不足时零向量补齐」和「返回维度校验」，
    不会写入 payload；不传时读 settings.dashscope_embedding_dim（默认 1024）。
    """
    settings = settings or get_settings()
    model = model or settings.dashscope_embedding_model
    dim = dimensions if dimensions is not None else settings.dashscope_embedding_dim
    arr = list(texts)
    if not arr:
        return []
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key.strip()}",
        "Content-Type": "application/json",
    }
    # DashScope 兼容模式不支持 dimensions 参数；qwen3.7-text-embedding=1024 / text-embedding-v3=1536
    payload: dict = {"model": model, "input": arr, "encoding_format": "float"}

    async def _call():
        client = _get_client(settings)
        resp = await client.post("/embeddings", headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    data = await _run_with_retry(_call, settings, description=f"embedding n={len(arr)} model={model}")
    # DashScope compatible-mode 返回与 OpenAI 同形
    results = data.get("data") or []
    results.sort(key=lambda x: x.get("index", 0))
    out: list[list[float]] = []
    for d in results:
        emb = d.get("embedding") or []
        out.append([float(x) for x in emb])
    if len(out) != len(arr):
        log.warning("embedding 返回数量不一致: req=%s res=%s", len(arr), len(out))
        # 用零向量补齐，避免后面炸
        while len(out) < len(arr):
            out.append([0.0] * dim)
    # 维度兜底：返回长度不一致的条目按 dim 截断/补齐，防止后续 sqlite-vss packing 越界
    for i, v in enumerate(out):
        if len(v) != dim:
            if len(v) > dim:
                out[i] = v[:dim]
            else:
                out[i] = v + [0.0] * (dim - len(v))
    return out


# =========================================================
# ASR（上传音频文件 → 文字）
# =========================================================
async def asr_transcribe(
    audio_bytes: bytes,
    filename: str,
    *,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> str:
    settings = settings or get_settings()
    model = model or settings.dashscope_asr_model
    headers = {"Authorization": f"Bearer {settings.dashscope_api_key.strip()}"}
    files = {"file": (filename, audio_bytes, "application/octet-stream")}
    data = {"model": model, "response_format": "json"}

    async def _call():
        client = _get_client(settings)
        resp = await client.post("/audio/transcriptions", headers=headers, data=data, files=files)
        resp.raise_for_status()
        return resp.json()

    data = await _run_with_retry(_call, settings, description=f"asr {filename}")
    return str(data.get("text") or "").strip()


# =========================================================
# TTS（文字 → 音频 bytes）
# =========================================================
async def tts_synthesize(
    text: str,
    *,
    voice: str = "longxiaochun",
    model: Optional[str] = None,
    response_format: str = "wav",
    settings: Optional[Settings] = None,
) -> bytes:
    settings = settings or get_settings()
    model = model or settings.dashscope_tts_model
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": response_format,
    }

    async def _call():
        client = _get_client(settings)
        resp = await client.post("/audio/speech", headers=headers, json=payload)
        resp.raise_for_status()
        return await resp.aread()

    return await _run_with_retry(_call, settings, description=f"tts len={len(text)}")


# =========================================================
# 内容审核（敏感词）
# =========================================================
async def moderation_check(
    texts: Iterable[str],
    *,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    model = model or settings.dashscope_moderation_model
    arr = list(texts)
    if not arr:
        return []
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "input": [{"text": t} for t in arr]}

    async def _call():
        client = _get_client(settings)
        resp = await client.post("/moderations", headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    data = await _run_with_retry(_call, settings, description=f"moderation n={len(arr)}")
    return data.get("results") or []
