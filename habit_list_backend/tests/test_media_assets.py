"""Original media remains addressable while transcript stays optional metadata."""
from __future__ import annotations

import io
import wave
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.database import get_db
from app.db.models import MediaAsset
from app.media.service import asset_file_path, cleanup_unattached_assets, load_media_prompt_parts
from app.providers import dashscope

pytestmark = pytest.mark.anyio


def _wav_bytes(duration_ms: int = 1000) -> bytes:
    rate = 8000
    frames = b"\x00\x00" * int(rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


async def test_audio_upload_keeps_original_and_can_be_attached_to_life_fragment(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    async def fake_asr(_raw: bytes, _filename: str) -> dashscope.Transcription:
        # 服务商没给置信度：转写可用于本轮理解，但不能成为地形证据。
        return dashscope.Transcription(text="今天在路边听见风了")

    monkeypatch.setattr(dashscope, "asr_transcribe", fake_asr)
    raw = b"RIFF-inner-terrain-test"
    response = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "true"},
        files={"file": ("life.webm", io.BytesIO(raw), "audio/webm")},
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["kind"] == "audio"
    assert asset["transcript"] == "今天在路边听见风了"

    playback = await client.get(asset["url"])
    assert playback.status_code == 200
    assert playback.content == raw

    created = await client.post(
        "/api/v1/moments",
        json={"media_asset_ids": [asset["asset_id"]], "allow_response": False},
    )
    assert created.status_code == 201, created.text
    assert created.json()["text"] == "今天在路边听见风了"
    assert created.json()["media"][0]["asset_id"] == asset["asset_id"]

    listing = await client.get("/api/v1/moments")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["media"][0]["transcript"] == "今天在路边听见风了"

    deleted = await client.delete(f"/api/v1/moments/{created.json()['moment_id']}")
    assert deleted.status_code == 200
    assert (await client.get(asset["url"])).status_code == 404


async def test_image_upload_rejects_audio_mime(client: AsyncClient):
    response = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image"},
        files={"file": ("wrong.wav", io.BytesIO(b"audio"), "audio/wav")},
    )
    assert response.status_code == 400


async def test_image_prompt_uses_compatible_multimodal_content_shape(
    client: AsyncClient,
    test_settings: Settings,
):
    response = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image"},
        files={"file": ("prompt.png", io.BytesIO(b"image-prompt"), "image/png")},
    )
    assert response.status_code == 201, response.text

    parts = await load_media_prompt_parts(
        user_id=test_settings.default_user_id,
        asset_ids=[response.json()["asset_id"]],
        settings=test_settings,
    )
    assert parts == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2UtcHJvbXB0"},
        }
    ]


async def test_wav_upload_records_duration_without_replacing_original(
    client: AsyncClient,
):
    raw = _wav_bytes(1250)
    response = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "false"},
        files={"file": ("short.wav", io.BytesIO(raw), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert 1240 <= asset["duration_ms"] <= 1260
    playback = await client.get(asset["url"])
    assert playback.content == raw
    await client.delete(asset["url"])


async def test_abandoned_unattached_media_is_reclaimed(
    client: AsyncClient,
    test_settings: Settings,
):
    response = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image"},
        files={"file": ("abandoned.png", io.BytesIO(b"not-a-real-image"), "image/png")},
    )
    assert response.status_code == 201, response.text
    asset_id = response.json()["asset_id"]
    async with get_db(read_only=False) as db:
        asset = (await db.execute(select(MediaAsset).where(MediaAsset.asset_id == asset_id))).scalar_one()
        asset.created_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        path = asset_file_path(asset, test_settings)
        assert path.is_file()
        removed = await cleanup_unattached_assets(
            db,
            settings=test_settings.model_copy(update={"media_unattached_retention_minutes": 5}),
            now=datetime.now(UTC),
        )
        assert removed == 1
        assert not path.exists()
    assert (await client.get(response.json()["url"])).status_code == 404


async def test_live_photo_pair_is_grouped_and_counts_as_one_visual_item(
    client: AsyncClient,
):
    # Minimal ISO-BMFF headers are enough to exercise the container guard; the
    # files remain opaque originals and are never converted to a still image.
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heic"
    mov = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  "
    group_id = "live-test-group-001"
    still = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image", "media_group_id": group_id, "media_role": "live_still"},
        files={"file": ("IMG_1001.HEIC", io.BytesIO(heic), "image/heic")},
    )
    motion = await client.post(
        "/api/v1/media/upload",
        data={"kind": "video", "media_group_id": group_id, "media_role": "live_motion"},
        files={"file": ("IMG_1001.MOV", io.BytesIO(mov), "video/quicktime")},
    )
    assert still.status_code == 201, still.text
    assert motion.status_code == 201, motion.text
    assert still.json()["is_live_photo_part"] is True
    assert motion.json()["role"] == "live_motion"

    created = await client.post(
        "/api/v1/moments",
        json={"media_asset_ids": [still.json()["asset_id"], motion.json()["asset_id"]]},
    )
    assert created.status_code == 201, created.text
    assert {item["kind"] for item in created.json()["media"]} == {"image", "video"}


async def test_live_photo_group_must_be_complete_and_deletes_as_one_unit(
    client: AsyncClient,
):
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heic"
    mov = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  "
    group_id = "live-test-group-incomplete"
    still = await client.post(
        "/api/v1/media/upload",
        data={"kind": "image", "media_group_id": group_id, "media_role": "live_still"},
        files={"file": ("IMG_2002.HEIC", io.BytesIO(heic), "image/heic")},
    )
    assert still.status_code == 201, still.text
    incomplete = await client.post(
        "/api/v1/moments",
        json={"media_asset_ids": [still.json()["asset_id"]]},
    )
    assert incomplete.status_code == 400, incomplete.text
    assert incomplete.json()["detail"]["code"] == "MEDIA_INVALID"

    motion = await client.post(
        "/api/v1/media/upload",
        data={"kind": "video", "media_group_id": group_id, "media_role": "live_motion"},
        files={"file": ("IMG_2002.MOV", io.BytesIO(mov), "video/quicktime")},
    )
    assert motion.status_code == 201, motion.text
    deleted = await client.delete(still.json()["url"])
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(motion.json()["url"])).status_code == 404


async def test_moment_rejects_more_than_nine_visual_media(client: AsyncClient):
    ids = []
    for index in range(10):
        response = await client.post(
            "/api/v1/media/upload",
            data={"kind": "image"},
            files={"file": (f"{index}.png", io.BytesIO(b"preview-image"), "image/png")},
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["asset_id"])
    response = await client.post("/api/v1/moments", json={"media_asset_ids": ids})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MEDIA_LIMIT"


async def test_audio_does_not_consume_the_nine_visual_slots(client: AsyncClient):
    visual_ids = []
    for index in range(9):
        response = await client.post(
            "/api/v1/media/upload",
            data={"kind": "image"},
            files={"file": (f"visual-{index}.png", io.BytesIO(b"preview-image"), "image/png")},
        )
        assert response.status_code == 201, response.text
        visual_ids.append(response.json()["asset_id"])
    voice = await client.post(
        "/api/v1/media/upload",
        data={"kind": "audio", "transcribe": "false"},
        files={"file": ("voice.webm", io.BytesIO(b"original-voice"), "audio/webm")},
    )
    assert voice.status_code == 201, voice.text
    created = await client.post(
        "/api/v1/moments",
        json={"media_asset_ids": [*visual_ids, voice.json()["asset_id"]]},
    )
    assert created.status_code == 201, created.text
