"""ASR/TTS 接口（走 DashScope mock）。"""
from __future__ import annotations

import io

import pytest
import respx
from httpx import AsyncClient
import tests  # noqa: F401
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
        "voice": "longxiaochun",
        "fmt": "wav",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content[:4] == b"RIFF"
