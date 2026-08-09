"""阿里云 DashScope 的异步封装。

覆盖：
- chat/completions       SSE 流式（共处 AI 回应）
- embeddings             qwen-embedding-v3（向量化用于向量检索）
- multimodal-generation  qwen3-asr-flash（原始语音内联 ASR）
- /api/v1/services/audio/tts/SpeechSynthesizer（非实时 TTS 文字转语音）
- moderations            内容审核（敏感词）

使用 tenacity 做 2 次指数退避；用 sliding log 做 RPM 限流。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import mimetypes
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
from urllib.parse import urlsplit

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
        # ``post()`` eagerly buffers the response body.  Build/send the request
        # with ``stream=True`` so the caller can forward each provider chunk as
        # soon as it arrives; the response is closed by the generator below.
        request = client.build_request(
            "POST", "/chat/completions", headers=headers, json=payload
        )
        resp = await client.send(request, stream=True)
        if resp.status_code >= 400:
            await resp.aread()
            await resp.aclose()
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
# ASR（原始音频 → 文字）
# =========================================================
ASR_INLINE_ENDPOINT = "/api/v1/services/aigc/multimodal-generation/generation"


def _absolute_provider_url(settings: Settings, path: str) -> str:
    """Build a root-relative provider URL while preserving the configured host.

    The shared client uses ``/compatible-mode/v1`` as its base path for the
    OpenAI-compatible APIs.  Native DashScope services live at the host root,
    so passing a plain string to ``AsyncClient`` would append the path to the
    compatibility prefix.
    """
    base = httpx.URL(settings.dashscope_base_url)
    return str(base.copy_with(path=path, query=None, fragment=None))


def _raise_provider_http_error(resp: httpx.Response, description: str) -> None:
    """Raise a compact provider error without logging request bodies or audio."""
    if resp.status_code < 400:
        return
    code = ""
    message = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
            message = str(payload.get("message") or "")
    except (ValueError, TypeError):
        pass
    detail = f" {code}: {message}" if code or message else ""
    raise httpx.HTTPStatusError(
        f"{description} HTTP {resp.status_code}{detail}",
        request=resp.request,
        response=resp,
    )


def _extract_asr_text(payload: Any) -> str:
    """Extract text from DashScope's Qwen ASR response."""
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    choices = (payload.get("output") or {}).get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(item.get("text") or "").strip()
                for item in content
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
            if parts:
                return "\n".join(parts)
    transcripts = payload.get("transcripts")
    if isinstance(transcripts, list):
        parts = [
            str(item.get("text") or "").strip()
            for item in transcripts
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if parts:
            return "\n".join(parts)
    output = payload.get("output")
    if isinstance(output, dict):
        return _extract_asr_text(output)
    return ""


def _extract_asr_confidence(payload: Any) -> float | None:
    """The worst segment confidence the provider reported, or ``None``.

    Qwen's inline ASR endpoint currently reports no confidence at all, so this
    returns ``None`` for the path the app actually uses.  That is deliberate:
    an unknown confidence must stay unknown rather than be optimistically
    filled in, because ``memory_v3_min_asr_confidence`` decides whether a
    machine's guess at someone's words may be used to infer things about them.

    The worst segment governs the whole transcript: one misheard clause is
    enough to poison an inference drawn from the sentence around it.
    """

    scores: list[float] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "confidence" and isinstance(value, (int, float)):
                    scores.append(float(value))
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    if not scores:
        return None
    return max(0.0, min(1.0, min(scores)))


@dataclass(frozen=True)
class Transcription:
    """A transcript plus how much the provider vouched for it."""

    text: str
    confidence: float | None = None


async def asr_transcribe(
    audio_bytes: bytes,
    filename: str,
    *,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> Transcription:
    settings = settings or get_settings()
    if not audio_bytes:
        return Transcription(text="")
    requested_model = model or settings.dashscope_asr_model
    # The batch Paraformer endpoint needs a provider-reachable file URL.  This
    # API receives bytes from a private local media store, so use the inline
    # Qwen ASR endpoint instead; it avoids copying the recording to provider OSS.
    asr_model = (
        requested_model
        if requested_model.startswith("qwen3-asr")
        else settings.dashscope_asr_inline_model
    )
    content_type = mimetypes.guess_type(filename or "audio.bin")[0] or "application/octet-stream"
    audio_data_url = f"data:{content_type};base64,{base64.b64encode(audio_bytes).decode('ascii')}"
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": asr_model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"audio": audio_data_url}],
                }
            ]
        },
        "parameters": {"result_format": "message"},
    }

    async def _call() -> dict[str, Any]:
        client = _get_client(settings)
        resp = await client.post(
            _absolute_provider_url(settings, ASR_INLINE_ENDPOINT),
            headers=headers,
            json=payload,
        )
        _raise_provider_http_error(resp, f"DashScope ASR model={asr_model}")
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("DashScope ASR response is not an object")
        return data

    result = await _run_with_retry(
        _call, settings, description=f"asr request len={len(audio_bytes)} model={asr_model}"
    )
    return Transcription(
        text=_extract_asr_text(result),
        confidence=_extract_asr_confidence(result),
    )


# =========================================================
# TTS（文字 → 音频 bytes）
# =========================================================
TTS_ENDPOINT = "/api/v1/services/audio/tts/SpeechSynthesizer"


def _decode_tts_audio_data(value: Any) -> bytes:
    """Decode the provider's optional inline base64 audio payload."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str) and value.strip():
        encoded = value.strip()
        if encoded.startswith("data:"):
            try:
                encoded = encoded.split(",", 1)[1]
            except IndexError as exc:  # pragma: no cover - defensive
                raise ValueError("TTS inline audio data URL is malformed") from exc
        encoded = "".join(encoded.split())
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("TTS inline audio data is not valid base64") from exc
    else:
        raise ValueError("TTS response contains no inline audio data")
    if not raw:
        raise ValueError("TTS inline audio data is empty")
    return raw


def _validate_tts_audio_url(value: Any) -> str:
    """Accept only absolute HTTP(S) URLs returned by DashScope."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("TTS response contains neither audio data nor audio URL")
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("TTS audio URL must be an absolute HTTP(S) URL")
    return url


async def tts_synthesize(
    text: str,
    *,
    voice: str = "longanhuan_v3.6",
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
        "input": {
            "text": text,
            "voice": voice,
            "format": response_format,
            "sample_rate": 24000,
        },
    }

    async def _call() -> dict[str, Any]:
        client = _get_client(settings)
        resp = await client.post(
            _absolute_provider_url(settings, TTS_ENDPOINT), headers=headers, json=payload
        )
        if resp.status_code >= 400:
            await resp.aread()
            log.error("DashScope TTS HTTP %s model=%s", resp.status_code, model)
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
        data = resp.json()
        try:
            audio = data["output"]["audio"]
        except (KeyError, TypeError) as exc:
            raise ValueError("TTS response missing output.audio") from exc
        if not isinstance(audio, dict):
            raise ValueError("TTS response output.audio must be an object")
        return audio

    audio = await _run_with_retry(
        _call, settings, description=f"tts request len={len(text)} model={model}"
    )

    inline_data = audio.get("data")
    if inline_data:
        return _decode_tts_audio_data(inline_data)

    audio_url = _validate_tts_audio_url(audio.get("url"))

    async def _download() -> bytes:
        client = _get_client(settings)
        resp = await client.get(audio_url)
        resp.raise_for_status()
        raw = await resp.aread()
        if not raw:
            raise ValueError("TTS audio URL returned an empty body")
        return raw

    return await _run_with_retry(
        _download, settings, description=f"tts download model={model}"
    )


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
