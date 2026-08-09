"""Authenticated upload, playback and deletion for user-owned media."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from ...core.config import get_settings
from ...db.database import get_db
from ...media.service import (
    MediaValidationError,
    asset_file_path,
    asset_response,
    delete_asset,
    get_asset_for_user,
    store_asset,
)
from ...providers import dashscope
from ..v1.common import ApiError, BaseSchema, current_user

router = APIRouter()


class MediaAssetOut(BaseSchema):
    asset_id: str
    kind: str
    mime_type: str
    original_name: str
    byte_size: int
    duration_ms: int | None = None
    transcript: str | None = None
    # None = 服务商没有给出置信度，等同于"未经核对的机器猜测"，不是低分。
    transcript_confidence: float | None = None
    group_id: str | None = None
    role: str | None = None
    is_live_photo_part: bool = False
    url: str
    created_at: str


@router.post("/upload", response_model=MediaAssetOut, status_code=201)
async def upload_media(
    file: UploadFile = File(...),
    kind: Literal["audio", "image", "video"] = Form(...),
    transcribe: bool = Form(False),
    media_group_id: str | None = Form(None, max_length=64),
    media_role: Literal["live_still", "live_motion"] | None = Form(None),
    user_id: str = Depends(current_user),
):
    settings = get_settings()
    raw = await file.read((settings.media_max_image_bytes if kind == "image" else settings.media_max_bytes) + 1)
    try:
        async with get_db(read_only=False) as db:
            asset = await store_asset(
                db,
                user_id=user_id,
                kind=kind,
                filename=file.filename or "upload.bin",
                mime_type=file.content_type,
                data=raw,
                settings=settings,
                transcribe=transcribe,
                media_group_id=media_group_id,
                media_role=media_role,
            )
            return MediaAssetOut(**asset_response(asset, settings))
    except MediaValidationError as exc:
        raise ApiError("MEDIA_INVALID", str(exc), 400) from exc


@router.get("/{asset_id}")
async def playback_media(asset_id: str, user_id: str = Depends(current_user)):
    settings = get_settings()
    async with get_db(read_only=True) as db:
        asset = await get_asset_for_user(db, user_id=user_id, asset_id=asset_id)
    if asset is None:
        raise ApiError("NOT_FOUND", "媒体不存在", 404)
    path = asset_file_path(asset, settings)
    if not path.is_file():
        raise ApiError("NOT_FOUND", "媒体文件已不可用", 404)
    return FileResponse(path, media_type=asset.mime_type, filename=asset.original_name)


@router.post("/{asset_id}/transcribe", response_model=MediaAssetOut)
async def transcribe_media(asset_id: str, user_id: str = Depends(current_user)):
    """Generate optional text metadata without replacing the original audio."""

    settings = get_settings()
    async with get_db(read_only=False) as db:
        asset = await get_asset_for_user(db, user_id=user_id, asset_id=asset_id, kind="audio")
        if asset is None:
            raise ApiError("NOT_FOUND", "语音不存在或已被删除", 404)
        path = asset_file_path(asset, settings)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ApiError("MEDIA_UNAVAILABLE", "原始语音文件不可用", 404) from exc
        try:
            result = await dashscope.asr_transcribe(raw, asset.original_name)
        except Exception as exc:  # noqa: BLE001 - original remains available
            raise ApiError("ASR_FAIL", "转写暂时不可用，原始语音没有改变", 502) from exc
        transcript = result.text.strip() or None
        asset.transcript = transcript
        asset.transcript_confidence = result.confidence if transcript else None
        metadata = dict(asset.metadata_json or {})
        metadata["transcribed"] = transcript is not None
        metadata.pop("transcription_error", None)
        asset.metadata_json = metadata
        await db.flush()
        return MediaAssetOut(**asset_response(asset, settings))


@router.delete("/{asset_id}")
async def remove_media(asset_id: str, user_id: str = Depends(current_user)):
    settings = get_settings()
    async with get_db(read_only=False) as db:
        removed = await delete_asset(db, user_id=user_id, asset_id=asset_id, settings=settings)
    if not removed:
        raise ApiError("NOT_FOUND", "媒体不存在", 404)
    return {"ok": True, "deleted": True}
