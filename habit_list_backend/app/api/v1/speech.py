"""语音：原始音频内联 ASR -> 文字；TTS 文字 -> 原始音频。"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

from ...providers import dashscope
from ..v1.common import ApiError, current_user

log = logging.getLogger("habit_list.api.speech")
router = APIRouter()

ALLOW_ASR_EXT = {"wav", "mp3", "m4a", "aac", "flac", "ogg", "opus"}


@router.post("/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    user_id: str = Depends(current_user),
):
    fn = (file.filename or "audio.bin").lower()
    ext = fn.rsplit(".", 1)[-1] if "." in fn else "bin"
    if ext not in ALLOW_ASR_EXT:
        raise ApiError("BAD_FORMAT", f"暂不支持 .{ext}，支持: {','.join(sorted(ALLOW_ASR_EXT))}")
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise ApiError("TOO_BIG", "音频请小于 25MB", 413)
    if not raw:
        return {"ok": True, "text": ""}
    try:
        result = await dashscope.asr_transcribe(raw, fn)
    except Exception as exc:  # noqa: BLE001
        log.exception("ASR fail")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"ok": False, "code": "ASR_FAIL", "message": f"语音识别失败：{exc}"},
        )
    return {
        "ok": True,
        "text": result.text,
        "confidence": result.confidence,
        "filename": fn,
        "bytes": len(raw),
    }


@router.post("/synthesize")
async def synthesize(
    text: str = Form(..., min_length=1, max_length=2000),
    voice: str = Form("longanhuan_v3.6"),
    fmt: Literal["wav", "mp3"] = Form("wav"),
    user_id: str = Depends(current_user),
):
    try:
        data = await dashscope.tts_synthesize(text, voice=voice, response_format=fmt)
    except Exception as exc:  # noqa: BLE001
        log.exception("TTS fail")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"ok": False, "code": "TTS_FAIL", "message": f"语音合成失败：{exc}"},
        )
    ct = "audio/wav" if fmt == "wav" else "audio/mpeg"
    return Response(content=data, media_type=ct, headers={
        "Content-Disposition": f'attachment; filename="tts.{fmt}"',
    })
