"""ASR/TTS 接口（走 DashScope mock）。"""
from __future__ import annotations

import io
import json

import httpx
import pytest
import respx
from httpx import AsyncClient

import tests  # noqa: F401
from app.providers import dashscope
from tests import mock_dashscope_asr, mock_dashscope_tts

pytestmark = pytest.mark.anyio


@pytest.mark.anyio
@respx.mock
async def test_asr_upload(client: AsyncClient, test_settings):
    mock_dashscope_asr(respx, test_settings, text="明天下午3点提醒我交周报")
    f = io.BytesIO(b"RIFF....fake wave")
    f.name = "voice.wav"
    files = {"file": ("voice.wav", f, "audio/wav")}
    r = await client.post("/api/v1/speech/transcriptions", files=files)
    assert r.status_code == 200
    j = r.json()
    assert j["text"] == "明天下午3点提醒我交周报"
    assert j["bytes"] > 0

    submit = next(
        call.request
        for call in respx.calls
        if call.request.url.path.endswith("/services/aigc/multimodal-generation/generation")
    )
    payload = json.loads(submit.content)
    assert payload["model"] == test_settings.dashscope_asr_inline_model
    audio = payload["input"]["messages"][0]["content"][0]["audio"]
    assert audio.startswith("data:audio/") and ";base64," in audio


@pytest.mark.anyio
async def test_asr_unsupported_ext(client: AsyncClient):
    f = io.BytesIO(b"xxxx")
    f.name = "foo.xyz"
    files = {"file": ("foo.xyz", f, "application/octet-stream")}
    r = await client.post("/api/v1/speech/transcriptions", files=files)
    assert r.status_code == 400


@pytest.mark.anyio
@respx.mock
async def test_tts_synthesize(client: AsyncClient, test_settings):
    mock_dashscope_tts(respx, test_settings, payload=b"RIFF--WAV--fake")
    r = await client.post("/api/v1/speech/synthesize", data={
        "text": "晚安啦，明天也要做你自己。",
        "voice": "longanhuan_v3.6",
        "fmt": "wav",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content[:4] == b"RIFF"
    request = json.loads(respx.calls.last.request.content)
    assert request["model"] == test_settings.dashscope_tts_model
    assert request["input"] == {
        "text": "晚安啦，明天也要做你自己。",
        "voice": "longanhuan_v3.6",
        "format": "wav",
        "sample_rate": 24000,
    }


@pytest.mark.anyio
@respx.mock
async def test_tts_downloads_signed_audio_url(test_settings):
    endpoint = str(
        httpx.URL(test_settings.dashscope_base_url).copy_with(
            path="/api/v1/services/audio/tts/SpeechSynthesizer", query=None, fragment=None
        )
    )
    audio_url = "https://audio.example.test/tts/test.wav?signature=test"
    payload = b"RIFF--downloaded--fake"
    respx.post(endpoint).respond(
        200,
        json={
            "request_id": "tts-req-url",
            "output": {
                "finish_reason": "stop",
                "audio": {"data": "", "url": audio_url, "id": "audio_url"},
            },
        },
    )
    download = respx.get(audio_url).respond(200, content=payload)

    data = await dashscope.tts_synthesize("你好。", settings=test_settings)

    assert data == payload
    assert download.called
