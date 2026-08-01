"""聊天接口（共处页主入口）：POST /chat/completions，SSE 流式。"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...memory.system1 import run_chat_stream
from ..v1.common import current_user, request_id

log = logging.getLogger("habit_list.api.chat")
router = APIRouter()


class ChatRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000, description="用户本轮说的话")
    session_id: Optional[str] = Field(default=None, description="会话ID，不传后端自动生成")
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.6, description="留空=后端默认")
    mode: Literal["auto", "confide", "memo", "life"] = Field(
        default="auto",
        description="新客户端应显式选择 confide/memo/life；auto 仅兼容旧客户端",
    )
    stream: bool = True


@router.post("/chat/completions")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    if not body.stream:
        # 非流式：收集 SSE，找到 event=done 的那条，直接 JSON 解析 assistant_text
        last_done: dict | None = None
        try:
            import orjson as _jsonlib
        except Exception:  # noqa: BLE001
            import json as _jsonlib  # type: ignore[no-redef]
        async for b in run_chat_stream(
            user_id=user_id,
            user_text=body.text,
            session_id=body.session_id,
            request_id=req_id,
            temperature=body.temperature,
            mode=body.mode,
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
            user_text=body.text,
            session_id=body.session_id,
            request_id=req_id,
            temperature=body.temperature,
            mode=body.mode,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Request-ID": req_id,
        },
    )
