"""聊天接口（共处页主入口）：POST /chat/completions，SSE 流式。"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...db.database import get_db
from ...media.service import get_asset_for_user, transcript_is_terrain_trusted
from ...memory.system1 import run_chat_stream
from ..v1.common import ApiError, current_user, request_id

log = logging.getLogger("habit_list.api.chat")
router = APIRouter()


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=4000, description="用户本轮说的话或编辑后的转写")
    audio_asset_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已上传的原始语音；可以和文字同时存在",
    )
    session_id: Optional[str] = Field(default=None, description="会话ID，不传后端自动生成")
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.6, description="留空=后端默认")
    mode: Literal["auto", "confide", "memo", "life"] = Field(
        default="confide",
        description=(
            "共处默认 confide；memo/life 只接受用户明确选择；"
            "auto 仅兼容旧客户端且按 confide 处理，不再自动分类"
        ),
    )
    stream: bool = True

    @model_validator(mode="after")
    def _validate_input(self):
        self.text = self.text.strip()
        if not self.text and not self.audio_asset_id:
            raise ValueError("请写下一句话或先上传一段语音")
        return self


async def _resolve_chat_input(
    *,
    user_id: str,
    body: ChatRequest,
) -> tuple[str, list[str], bool]:
    """Resolve an optional uploaded voice without discarding the original file.

    The third value says whether the resulting text is an unverified machine
    transcript, which decides terrain eligibility (baseline 8.3) but not whether
    the turn happens.
    """
    if not body.audio_asset_id:
        return body.text, [], False
    async with get_db(read_only=True) as db:
        asset = await get_asset_for_user(
            db,
            user_id=user_id,
            asset_id=body.audio_asset_id,
            kind="audio",
        )
    if asset is None:
        raise ApiError("MEDIA_NOT_FOUND", "这段语音不存在或已被删除", 404)
    transcript = (asset.transcript or "").strip()
    # A transcript is an optional convenience, not the source of truth.  The
    # original audio remains available to the multimodal model, so a failed or
    # delayed ASR must not block a voice-only turn.
    if body.text:
        return body.text, [asset.asset_id], False
    return transcript, [asset.asset_id], not transcript_is_terrain_trusted([asset])


@router.post("/chat/completions")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    user_text, media_asset_ids, untrusted_transcript = await _resolve_chat_input(
        user_id=user_id, body=body
    )
    if not body.stream:
        # 非流式：收集 SSE，找到 event=done 的那条，直接 JSON 解析 assistant_text
        last_done: dict | None = None
        try:
            import orjson as _jsonlib
        except Exception:  # noqa: BLE001
            import json as _jsonlib  # type: ignore[no-redef]
        async for b in run_chat_stream(
            user_id=user_id,
            user_text=user_text,
            session_id=body.session_id,
            request_id=req_id,
            temperature=body.temperature,
            mode=body.mode,
            media_asset_ids=media_asset_ids,
            untrusted_transcript=untrusted_transcript,
        ):
            raw = b
            # 一帧 SSE bytes 可能有多条 data；逐行过
            for seg in raw.split(b"\n\n"):
                seg = seg.strip()
                if not seg.startswith(b"data:"):
                    continue
                data = seg[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    obj = _jsonlib.loads(data)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict) and obj.get("event") == "done":
                    last_done = obj
        if last_done and isinstance(last_done.get("data"), dict):
            txt = str(last_done["data"].get("assistant_text") or "")
        else:
            txt = ""
        return {"ok": True, "assistant_text": txt, "request_id": req_id}

    return StreamingResponse(
        run_chat_stream(
            user_id=user_id,
            user_text=user_text,
            session_id=body.session_id,
            request_id=req_id,
            temperature=body.temperature,
            mode=body.mode,
            media_asset_ids=media_asset_ids,
            untrusted_transcript=untrusted_transcript,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Request-ID": req_id,
        },
    )
