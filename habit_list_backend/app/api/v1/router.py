"""总路由：把 chat/memos/pebbles/insights/me/asr/tts 都挂进来。"""
from __future__ import annotations

from fastapi import APIRouter

from . import chat, insights, me, memories, memos, pebbles, speech

router = APIRouter()
router.include_router(chat.router, tags=["共处·聊天"])
router.include_router(memos.router, prefix="/memos", tags=["备忘"])
router.include_router(pebbles.router, prefix="/pebbles", tags=["河·记忆石子"])
router.include_router(insights.router, prefix="/insights", tags=["见·洞察"])
router.include_router(memories.router, prefix="/memories", tags=["Memory V2·可信记忆"])
router.include_router(me.router, prefix="/me", tags=["它·风格参数"])
router.include_router(speech.router, prefix="/speech", tags=["语音(ASR/TTS)"])

__all__ = ["router"]
