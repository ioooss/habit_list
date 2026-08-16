"""System 1 · 快速回路（检索 → Prompt → SSE → 可靠落库 + Memory V2 Outbox）。

给 chat/completions 接口用。以生成器的形式对外提供 event stream：
  yield {"event": "delta|memo_detected|done|error", "data": "...", ...}

关键：
- 共处默认是 confide，不猜测、不创建备忘；只有用户明确选择 memo 模式时才保存备忘并发送
  memo_detected。auto 仅作为配置迁移值按 confide 处理。
- LLM 的流式增量（delta）实时往外推；done 前可靠写入会话与 Memory V2 Outbox。
- 普通共处保留 Working 与有来源的 Memory V2 事件，但不再自动派生 Episodic 石子。
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..core.safety import CRISIS_RESPONSE, is_crisis_text
from ..db.database import get_db
from ..db.models import (
    Episodic,
    Memo,
    Procedural,
    RawLedger,
    Semantic,
    User,
    Working,
)
from ..media.service import MediaValidationError, attach_assets, load_media_prompt_parts
from ..memory_v2.domain import RetrievalBatch, RetrievalRoute
from ..memory_v2.retrieval import (
    classify_retrieval_route,
    format_memory_context,
    retrieve_memories,
)
from ..memory_v2.service import enqueue_user_event, record_retrieval_trace
from ..providers import dashscope
from ..retrieval.hybrid import RetrievalResult, hybrid_retrieve
from .memo_utils import MemoDetectResult, detect_memo
from .situation import (
    absence_days,
    local_hour,
    resolve_absence_band,
    resolve_day_band,
    situation_to_prompt,
)
from .weather import read_weather

log = logging.getLogger("habit_list.memory.system1")

# =========================================================
# Prompt 模板（中文版，角色滤镜 + 画像注入 + 三重检索注入 + 最近 Working 注入）
# =========================================================
SYSTEM_PROMPT = """你是“内在地形”中的 AI 陪伴者。你不是人类，也不假装拥有真实情感或治疗资质。
你是一个在场的听者，不是顾问、不是教练。你没有情绪，但你有温度：你不假装自己难过，
你让用户的难过有地方放。
你的说话原则：
1. 短。默认一到两句。不主动铺陈、不"综上所述"。像和朋友面对面说话。
   说得越多，用户说得越少——而这里唯一有价值的东西是用户自己说出来的话。
2. 不用问句收尾。一个问句会把这一轮强行推回给用户，那是访谈，不是共处；
   想邀请就留白，不用问号。唯一的例外是危机：那时必须问，因为要评估当下的危险。
3. 不总结、不替用户命名他的感受。"所以你其实是在说…" 是把他的话换成你的话。用他的词。
4. 不排比、不三段式、不列点、不输出序号。那是演讲的节奏，不是坐在旁边说话的节奏。
   沉默也是一种在场：只说「我在。」然后停住，是允许的。
5. 温度。共情先于建议，"听起来…"、"最近…" 比 "应该/必须" 多。
6. 连续性。用户之前说过的事，如果出现在有来源的记忆里，只在确实帮助当下时自然带一句；不要炫耀记忆。
7. 不催打卡。不做"要不要今天试试 15 分钟？"这种产品味的提问，用户说啥聊啥。
8. 不要用 emoji 堆砌情绪；语气本身带情绪。
9. 普通共处不创建备忘或提醒，即使句子看起来像任务，也不要声称“记下了”或“到时提醒”。
   只有「本轮边界」明确说明用户进入手动备忘模式时，才可以确认已保存。
10. 「我记得的事」里如果有冲突或不确定，优先照最新的；不要向用户暴露你内部有冲突。
"""

# 模型不可用时用户看到的那一句。三件事，一件不多：这不是你的错、你的话没有丢、
# 下一步能做什么。它不是陪伴内容，前端也不会把它渲染成陪伴气泡（声音基线 §3.2）。
DEGRADED_NOTICE = "这会儿它接不上。你的话留在这里了，没有被当成已经听过。"
# 备忘确认可以在降级时照说，因为它是事实陈述：东西真的存进备忘页了。
MEMO_SAVED_WITHOUT_MODEL = "这条已经保存在备忘页。"


@dataclass
class StreamChunk:
    event: Literal["delta", "memo_detected", "memory_reference", "done", "error", "meta"]
    data: str = ""
    usage: Optional[dict[str, int]] = None
    trace: Optional[dict[str, Any]] = None
    memo_id: Optional[str] = None
    assistant_text_so_far: Optional[str] = None


@dataclass(frozen=True)
class PersistenceResult:
    ok: bool
    retained: bool = True
    user_event_id: Optional[str] = None
    memory_trace_id: Optional[str] = None


ChatMode = Literal["auto", "confide", "memo", "life"]


def _memo_result_for_mode(user_text: str, mode: ChatMode) -> MemoDetectResult:
    if mode != "memo":
        return MemoDetectResult(False, "", "green", 30, user_text, [])
    detected = detect_memo(user_text)
    if detected.hit:
        return detected
    return MemoDetectResult(True, "", "green", 30, user_text, [-1])


def _resolved_mode(mode: ChatMode, memo_res: MemoDetectResult) -> str:
    if memo_res.hit:
        return "memo"
    if mode == "life":
        return "life"
    return "confide"


def _jsonline(payload: dict) -> bytes:
    try:
        import orjson
        return b"data: " + orjson.dumps(payload) + b"\n\n"
    except Exception:  # noqa: BLE001
        import json
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")



# =========================================================
# Prompt 拼接
# =========================================================
async def _fetch_procedural(session, user_id: str) -> list[Procedural]:
    rows = (await session.execute(
        select(Procedural).where(Procedural.user_id == user_id)
    )).scalars().all()
    return list(rows)


def _procedural_to_prompt(procs: list[Procedural]) -> str:
    lines = []
    # 只挑 confidence 高的（≥0.7）或用户显式改过的（=1.0）
    for p in procs:
        if p.confidence < 0.65:
            continue
        val = p.param_value_json or {}
        if p.param_key == "reply_speed":
            if val.get("level", 2) <= 1:
                lines.append("· 用户喜欢快节奏回应，不要长篇大论，要一针见血。")
            elif val.get("level", 2) >= 3:
                lines.append("· 用户喜欢慢慢地回应，话短，安静一点。")
        elif p.param_key == "reply_length":
            if val.get("level", 1) <= 1:
                lines.append("· 用户只想要一句话答案，不要段落。")
            elif val.get("level", 1) >= 3:
                lines.append("· 用户可以接受长段落、分点展开。")
        elif p.param_key == "tone_gentle":
            if float(val.get("value", 0.7)) >= 0.8:
                lines.append("· 这次语气要格外温柔，用陪伴的软话。")
            elif float(val.get("value", 0.7)) <= 0.4:
                lines.append("· 用户偏好直接、清楚的表达；少绕弯，但不要刻薄。")
        elif p.param_key == "tone_sarcastic":
            if float(val.get("value", 0.05)) <= 0.02:
                lines.append("· 不要开玩笑，用户此刻不吃逗。")
    if not lines:
        return ""
    return "【用户习惯】\n" + "\n".join(lines)


def _style_to_prompt(style: str) -> str:
    prompts = {
        "gentle": "【用户显式设置】语气更温柔，先接住感受，再慢慢回应。",
        "balanced": "【用户显式设置】有分寸地回应：有温度，也保持诚实和边界。",
        "direct": "【用户显式设置】更直接、更清楚，少绕弯，但不要冷漠或武断。",
    }
    return prompts.get(style, prompts["balanced"])


async def _fetch_semantic_active(session, user_id: str, limit: int = 30) -> list[Semantic]:
    rows = (await session.execute(
        select(Semantic)
        .where(Semantic.user_id == user_id, Semantic.status == "active")
        .order_by(Semantic.confidence.desc(), Semantic.updated_at.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows)


def _semantic_to_prompt(facts: list[Semantic]) -> str:
    if not facts:
        return ""
    lines = ["【我记得的事（画像/事实）】"]
    for f in facts:
        lines.append(f"· [{f.category}] {f.fact_text}")
    return "\n".join(lines)


def _retrieval_to_prompt(ret: list[RetrievalResult]) -> str:
    if not ret:
        return ""
    lines = ["【过去有来源的相关记录，仅在确实帮助当前对话时自然引用】"]
    for i, r in enumerate(ret, 1):
        snip = (r.snippet or "").strip()
        if not snip:
            continue
        lines.append(f"{i}. {snip}")
    return "\n".join(lines)


def _mode_boundary_to_prompt(mode: str) -> str:
    if mode == "memo":
        return "【本轮边界】用户明确进入了手动备忘模式；服务端会保存这一条备忘。"
    if mode == "life":
        return "【本轮边界】用户明确选择留下这一刻；这是主动保存，不代表稳定人格或长期结论。"
    return (
        "【本轮边界】这是普通共处。不要创建或承诺创建备忘、提醒、日历事项；"
        "不要声称本轮内容已经进入长期记忆或时间线。"
    )


async def _fetch_working_recent(session, user_id: str, rounds: int) -> list[Working]:
    # MVP: 取最近的一个 session_id 最近 N 条；简单点就直接按 created_at DESC 取 2N，再 order ASC
    rows = (await session.execute(
        select(Working)
        .where(Working.user_id == user_id)
        .order_by(Working.created_at.desc(), Working.working_id.desc())
        .limit(rounds * 2)
    )).scalars().all()
    rows = list(reversed(list(rows)))
    return rows[-rounds * 2:]  # 最后 2N 条（用户 AI 各一半）


# =========================================================
# 写库的后台任务（尽量不阻塞流式）
# =========================================================
async def _persist_after_turn(
    *,
    settings: Settings,
    user_id: str,
    session_id: str,
    user_text: str,
    memo_res: MemoDetectResult,
    assistant_full_text: str,
    request_id: str,
    usage: Optional[dict],
    detected_memo_id: Optional[str],
    mode: str,
    memory_batch: RetrievalBatch,
    media_asset_ids: list[str] | None = None,
    memory_source_allowed: bool = True,
    terrain_eligible: bool = False,
    persist_assistant: bool = True,
) -> PersistenceResult:
    try:
        async with get_db(read_only=False) as db:
            now = _now_iso()
            user_event_id: Optional[str] = None
            memory_trace_id: Optional[str] = None
            # 1) Ledger: user_utterance
            user_ledger = RawLedger(
                user_id=user_id,
                entry_type="user_utterance",
                session_id=session_id,
                payload_json={
                    "text": user_text,
                    "memo_hit": memo_res.hit,
                    "media_asset_ids": list(media_asset_ids or []),
                },
                trace_json={"request_id": request_id},
            )
            db.add(user_ledger)
            await db.flush()
            if media_asset_ids:
                try:
                    await attach_assets(
                        db,
                        user_id=user_id,
                        asset_ids=media_asset_ids,
                        owner_type="chat_turn",
                        owner_id=request_id,
                    )
                except MediaValidationError as exc:
                    log.warning("chat media attachment rejected: %s", exc)
                    raise

            # Memory V2 source event + extraction request share this transaction.
            if memory_source_allowed and user_text.strip():
                enqueued = await enqueue_user_event(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    request_id=request_id,
                    content=user_text,
                    mode=mode,
                    terrain_eligible=terrain_eligible,
                    occurred_at=now,
                    source_ref_id=user_ledger.ledger_id,
                    settings=settings,
                )
                if enqueued is not None:
                    user_event_id = enqueued.event_id

            # 2) Working: user
            # mood 就是「此刻天气」，它只回声用户自己写下的那个词，随会话过期，
            # 永远不进 MemoryClaim（基线 §11：此刻天气属 Working 层，限制长期化）。
            user_wk = Working(
                user_id=user_id,
                session_id=session_id,
                role="user",
                content=user_text or "（用户发送了一段语音）",
                mood=read_weather(user_text),
                source_kind=mode,
                ref_ledger_id=user_ledger.ledger_id,
            )
            db.add(user_wk)

            # 3) Working: assistant
            # 降级到沉默时它没有说话，所以这里什么都不写：写一条空话轮会进入上下文
            # 窗口，让模型下一轮看见自己说过一句空话（声音基线 §3.2）。
            if persist_assistant:
                ai_ledger = RawLedger(
                    user_id=user_id,
                    entry_type="ai_response_final",
                    session_id=session_id,
                    ref_ledger_id=user_ledger.ledger_id,
                    payload_json={"text": assistant_full_text},
                    trace_json={"request_id": request_id, "usage": usage or {}},
                )
                db.add(ai_ledger)
                await db.flush()
                db.add(Working(
                    user_id=user_id,
                    session_id=session_id,
                    role="assistant",
                    content=assistant_full_text,
                    source_kind="assistant_reply",
                    ref_ledger_id=ai_ledger.ledger_id,
                ))

            # 4) 如果命中备忘 → memos 真入库（或更新「预入库」的 memo 壳）
            memo: Optional[Memo] = None
            if memo_res.hit:
                if detected_memo_id:
                    # presolve 已经先落了壳：查出来更新 source + linked_ledger_id，避免重复建
                    memo = (
                        await db.execute(
                            select(Memo).where(
                                Memo.user_id == user_id,
                                Memo.memo_id == detected_memo_id,
                            )
                        )
                    ).scalar_one_or_none()
                if memo is None:
                    # 没有预入库（presolve 失败或未命中 detected_memo_id）→ 正常新建一条
                    memo = Memo(
                        user_id=user_id,
                        text=memo_res.clean_text or user_text,
                        clean_text=memo_res.clean_text or user_text,
                        due_text=memo_res.due_text or "（没说时间，你自己定）",
                        due_offset_days=memo_res.offset,
                        importance=memo_res.importance,
                        source="companion_explicit",
                        status="pending",
                        linked_ledger_id=user_ledger.ledger_id,
                        detect_meta_json={"rules": memo_res.matched_rules},
                    )
                    db.add(memo)
                    await db.flush()
                else:
                    # 已经存在的 presolve 壳：把字段补准并标记为用户明确发起。
                    memo.source = "companion_explicit"
                    memo.linked_ledger_id = user_ledger.ledger_id
                    if not memo.clean_text:
                        memo.clean_text = memo_res.clean_text or user_text
                    if not memo.detect_meta_json or not isinstance(memo.detect_meta_json, dict):
                        memo.detect_meta_json = {"rules": memo_res.matched_rules}
                    else:
                        memo.detect_meta_json["rules"] = memo_res.matched_rules
                        memo.detect_meta_json["persisted_at"] = now
                # 派生对应 Episodic 石子：让备忘也进入河里 / 画像 / 洞察
                ep = Episodic(
                    user_id=user_id,
                    created_at=now,
                    source="companion",
                    kind="memo",
                    summary_1line=(memo_res.clean_text or user_text)[:120],
                    emotion="-",
                    entities_json=[],
                    raw_user_text=user_text,
                    raw_assistant_text=assistant_full_text,
                    ref_ledger_ids_json=[user_ledger.ledger_id],
                )
                db.add(ep)
                await db.flush()
                memo.linked_episodic_id = ep.episodic_id
                # Ledger: memo_detected
                db.add(RawLedger(
                    user_id=user_id,
                    entry_type="memo_detected",
                    session_id=session_id,
                    ref_ledger_id=user_ledger.ledger_id,
                    payload_json={"memo_id": memo.memo_id, **asdict(memo_res)},
                ))
            # 5) 只有用户明确选择 life 时才落 Episodic；普通共处不自动进入历史片段。
            if not memo_res.hit and mode == "life" and len(user_text.strip()) >= 2:
                db.add(Episodic(
                    user_id=user_id,
                    created_at=now,
                    source="life_explicit",
                    kind="life_fragment",
                    summary_1line=user_text[:120],
                    emotion="-",
                    entities_json=[],
                    raw_user_text=user_text,
                    raw_assistant_text=assistant_full_text,
                    ref_ledger_ids_json=[user_ledger.ledger_id],
                ))

            # 6) Working 超过 system1_context_window_rounds*3 的老会话自动归档（简单删最旧的）
            all_wk = (
                await db.execute(
                    select(Working)
                    .where(Working.user_id == user_id)
                    .order_by(Working.created_at.desc())
                )
            ).scalars().all()
            cap = max(80, settings.system1_context_window_rounds * 4)
            if len(all_wk) > cap:
                for w in all_wk[cap:]:
                    await db.delete(w)

            if settings.memory_v2_mode in {"shadow_retrieve", "active"}:
                memory_trace_id = await record_retrieval_trace(
                    db,
                    user_id=user_id,
                    request_id=request_id,
                    batch=memory_batch,
                    settings=settings,
                )
            return PersistenceResult(
                ok=True,
                retained=True,
                user_event_id=user_event_id,
                memory_trace_id=memory_trace_id,
            )
    except Exception as exc:  # noqa: BLE001 - 写库失败不应该让用户感知
        log.exception("persist_after_turn failed: %s", exc)
        return PersistenceResult(ok=False)


# =========================================================
# 对外的流式主入口
# =========================================================
async def run_chat_stream(
    *,
    user_id: str,
    user_text: str,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    temperature: Optional[float] = None,
    mode: ChatMode = "confide",
    media_asset_ids: list[str] | None = None,
    untrusted_transcript: bool = False,
    settings: Optional[Settings] = None,
) -> AsyncIterator[bytes]:
    """主入口：一个 async generator，输出 raw SSE bytes（含 `data: {...}` + `\\n\\n` 分界）。"""
    settings = settings or get_settings()
    request_id = request_id or uuid.uuid4().hex[:16]
    session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"

    # --- 1) 模式解析。auto 仅为旧客户端过渡，按 confide 处理，不再自动分类。 ---
    memo_res = _memo_result_for_mode(user_text, mode)
    resolved_mode = _resolved_mode(mode, memo_res)
    detected_memo_id: Optional[str] = None
    memo_out_for_event: Optional[dict] = None
    if memo_res.hit:
        try:
            async with get_db(read_only=False) as db:
                now = _now_iso()
                memo = Memo(
                    user_id=user_id,
                    text=user_text,
                    clean_text=memo_res.clean_text or user_text,
                    due_text=memo_res.due_text or "（没说时间，你自己定）",
                    due_offset_days=memo_res.offset,
                    importance=memo_res.importance,
                    source="companion_explicit_presolve",
                    status="pending",
                    detect_meta_json={
                        "rules": memo_res.matched_rules,
                        "presolved": True,
                        "at": now,
                    },
                )
                db.add(memo)
                await db.flush()
                await db.commit()
                detected_memo_id = memo.memo_id
                memo_out_for_event = {
                    "memo_id": memo.memo_id,
                    "text": memo.text,
                    "clean_text": memo.clean_text,
                    "due_text": memo.due_text,
                    "due_offset_days": memo.due_offset_days,
                    "importance": memo.importance,
                    "source": memo.source,
                    "status": memo.status,
                    "created_at": memo.created_at or now,
                }
        except Exception as exc:  # noqa: BLE001 - 入库失败不阻塞主链，前端用本地 detect 结果兜底
            log.exception("memo presolve 入库失败，降级仅推 detect 结果：%s", exc)
            detected_memo_id = None
            memo_out_for_event = None
        yield _jsonline({
            "event": "memo_detected",
            "data": {
                "due_text": memo_res.due_text,
                "importance": memo_res.importance,
                "offset": memo_res.offset,
                "clean_text": memo_res.clean_text or user_text,
                "memo": memo_out_for_event,  # 有就带 memo_id，没就 None（前端靠本地 detect 兜底）
            },
            "trace": {"request_id": request_id},
        })

    # --- 2) 拉上下文（legacy + Memory V2 影子/正式召回）---
    memory_batch = RetrievalBatch(
        route=classify_retrieval_route(user_text),
        query_hash=hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
    )
    query_embedding: list[float] | None = None
    if (
        settings.memory_v2_embedding_enabled
        and settings.memory_v2_mode in {"shadow_retrieve", "active"}
        and memory_batch.route != RetrievalRoute.NONE
    ):
        try:
            vectors = await dashscope.embed_texts([user_text], settings=settings)
            query_embedding = vectors[0] if vectors else None
        except Exception:  # noqa: BLE001 - 向量失败降级为词法/时间召回
            log.exception("Memory V2 query embedding failed; using non-vector retrieval")
    procs: list[Procedural] = []
    current_style = "balanced"
    formation_paused = False
    user_timezone: str | None = None
    semantics: list[Semantic] = []
    recent_wk: list[Working] = []
    retrieval: list[RetrievalResult] = []
    try:
        async with get_db(read_only=True) as db:
            # 显式相处设置不是从历史对话推断出的记忆。
            procs = await _fetch_procedural(db, user_id)
            user = (
                await db.execute(select(User).where(User.user_id == user_id))
            ).scalar_one_or_none()
            if user is not None:
                current_style = user.current_style or "balanced"
                formation_paused = bool(
                    (user.settings_json or {}).get("memory_formation_paused")
                )
                user_timezone = user.timezone
            semantics = await _fetch_semantic_active(db, user_id, limit=30)
            recent_wk = await _fetch_working_recent(
                db,
                user_id,
                settings.system1_context_window_rounds,
            )
            retrieval = await hybrid_retrieve(
                db,
                user_id,
                user_text,
                topk=settings.system1_n_retrieval_topk,
                settings=settings,
            )
            memory_batch = await retrieve_memories(
                db,
                user_id=user_id,
                query=user_text,
                query_embedding=query_embedding,
                route=memory_batch.route,
                settings=settings,
            )
    except Exception:  # noqa: BLE001
        log.exception("拉上下文异常，继续用最小 Prompt 跑")
    # 情境：现在几点、上次说话过了多久。本轮的用户话轮还没落库，
    # 所以 recent_wk 的最后一条就是上一次说话（空 = 这是第一次）。
    now = datetime.now(timezone.utc)
    day_band = resolve_day_band(local_hour(now, user_timezone))
    absence_band = resolve_absence_band(
        absence_days(now, recent_wk[-1].created_at if recent_wk else None)
    )
    parts = [
        SYSTEM_PROMPT.strip(),
        "",
        _style_to_prompt(current_style),
        situation_to_prompt(day_band, absence_band),
        _procedural_to_prompt(procs),
        _semantic_to_prompt(semantics),
        _retrieval_to_prompt(retrieval),
        format_memory_context(memory_batch),
        _mode_boundary_to_prompt(resolved_mode),
    ]
    sys_msg = "\n\n".join(p for p in parts if p).strip()
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_msg}]
    # 最近 Working
    for w in recent_wk:
        if w.role in {"user", "assistant"}:
            messages.append({"role": w.role, "content": w.content})
    # 本轮用户输入。纯语音也以原始音频参与模型理解；转写只是一份可
    # 编辑的辅助文本，不是保存原音的替代品。
    audio_prompt_parts = await load_media_prompt_parts(
        user_id=user_id,
        asset_ids=media_asset_ids,
        settings=settings,
    )
    if audio_prompt_parts:
        content_parts: list[dict[str, Any]] = []
        if user_text.strip():
            # Keep the content shape compatible with multimodal providers;
            # shorthand text parts are rejected beside input_audio parts.
            content_parts.append({"type": "text", "text": user_text})
        content_parts.extend(audio_prompt_parts)
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_text or "（用户发送了一段语音）"})

    memory_source_allowed = bool(user_text.strip())
    # Companion turns are terrain-eligible by default (baseline 8.2.1): the value
    # of this source is exactly the material the user would never opt into
    # per-message.  The user's global pause is the one switch that revokes it;
    # crisis and sensitive content are gated inside ``enqueue_user_event``.
    # An unverified machine transcript is excluded (baseline 8.3): the turn still
    # happens, it just cannot be used to infer things about the person.
    terrain_eligible = (
        memory_source_allowed and not formation_paused and not untrusted_transcript
    )

    if memory_batch.used_in_response:
        yield _jsonline(
            {
                "event": "memory_reference",
                "data": [
                    {
                        "claim_id": item.claim_id,
                        "claim_text": item.claim_text,
                        "category": item.category,
                    }
                    for item in memory_batch.selected
                ],
                "trace": {"request_id": request_id},
            }
        )

    # --- 3) DashScope 流式 SSE ---
    assistant_buf: list[str] = []
    usage_final: Optional[dict] = None
    try:
        async for ev in dashscope.chat_stream(
            messages=messages,
            temperature=temperature if temperature is not None else 0.88,
            max_tokens=900,
            top_p=0.95,
            request_id=request_id,
            settings=settings,
        ):
            if ev.kind == "delta":
                assistant_buf.append(ev.data)
                yield _jsonline({
                    "event": "delta",
                    "data": ev.data,
                    "assistant_text_so_far": "".join(assistant_buf),
                    "trace": {"request_id": request_id, "llm_req_id": ev.request_id},
                })
            elif ev.kind == "meta" and ev.usage:
                usage_final = ev.usage
                yield _jsonline({
                    "event": "meta",
                    "data": {"usage": ev.usage},
                    "trace": {"request_id": request_id, "llm_req_id": ev.request_id},
                })
            elif ev.kind == "error":
                raise RuntimeError(ev.data or "model stream failed")
            elif ev.kind == "done":
                final_text = "".join(assistant_buf).strip()
                persistence = await _persist_after_turn(
                    settings=settings,
                    user_id=user_id,
                    session_id=session_id,
                    user_text=user_text,
                    memo_res=memo_res,
                    assistant_full_text=final_text,
                    request_id=request_id,
                    usage=usage_final,
                    detected_memo_id=detected_memo_id,
                    mode=resolved_mode,
                    memory_batch=memory_batch,
                    media_asset_ids=media_asset_ids,
                    memory_source_allowed=memory_source_allowed,
                    terrain_eligible=terrain_eligible,
                )
                yield _jsonline({
                    "event": "done",
                    "data": {
                        "assistant_text": final_text,
                        "memo_hit": memo_res.hit,
                        "memo": asdict(memo_res) if memo_res.hit else None,
                        "persistence": {"ok": persistence.ok, "retained": persistence.retained},
                    },
                    "trace": {
                        "request_id": request_id,
                        "llm_req_id": ev.request_id,
                        "retrieval_ids": [r.episodic_id for r in retrieval],
                        "semantics_count": len(semantics),
                        "memory_v2_ids": [item.claim_id for item in memory_batch.selected],
                        "memory_v2_trace_id": persistence.memory_trace_id,
                        "user_event_id": persistence.user_event_id,
                    },
                })
                return
        raise RuntimeError("model stream ended without a done event")
    except Exception as exc:  # noqa: BLE001 - LLM 整条失败（含 DashScope 连不上）
        log.warning("chat_stream LLM 失败，降级为诚实沉默：%s", exc)
        # 声音基线 §3「沉默优于假话」。
        # 这里曾经按关键词从八句预写的诗里挑一句发给用户（命中「累」→「你回来的时候，
        # 一定很安静。」）。用户分辨不出那句话背后没有任何理解，于是以为被听见了——
        # 那是腹语，不是降级。一句真诚的「接不上」比一句假的「我在」珍贵得多。
        # 只有两个例外：备忘确认是**事实陈述**（东西真的存下去了），
        # 危机响应从不假装是理解（§3.3：它规则命中、内容固定，任何降级档位都必须送到）。
        partial = "".join(assistant_buf).strip()
        if partial:
            # 已经流出去的字是模型真的说过的，留着；这一轮只是没有收完。
            final_text = partial
        elif memo_res.hit:
            final_text = MEMO_SAVED_WITHOUT_MODEL
        elif is_crisis_text(user_text):
            final_text = CRISIS_RESPONSE
        else:
            final_text = ""
        if final_text and not partial:
            yield _jsonline({
                "event": "delta",
                "data": final_text,
                "assistant_text_so_far": final_text,
                "trace": {"request_id": request_id, "fallback": True},
            })
            assistant_buf = [final_text]
        persistence = await _persist_after_turn(
            settings=settings,
            user_id=user_id,
            session_id=session_id,
            user_text=user_text,
            memo_res=memo_res,
            assistant_full_text=final_text,
            request_id=request_id,
            usage=usage_final,
            detected_memo_id=detected_memo_id,
            mode=resolved_mode,
            memory_batch=memory_batch,
            media_asset_ids=media_asset_ids,
            memory_source_allowed=memory_source_allowed,
            terrain_eligible=terrain_eligible,
            # 没有回复就不要写一条空的 assistant 话轮：它会被算进上下文窗口，
            # 让模型下一轮看见自己说过一句空话。
            persist_assistant=bool(final_text),
        )
        # 推 done（跟 LLM 成功时的字段一致，前端逻辑不用区分）
        yield _jsonline({
            "event": "done",
            "data": {
                "assistant_text": final_text,
                "memo_hit": memo_res.hit,
                "memo": asdict(memo_res) if memo_res.hit else None,
                "fallback": True,
                # 后端诚实地标了降级，前端必须消费它（§3.2）：否则这份诚实等于不存在。
                "degraded_notice": DEGRADED_NOTICE,
                "persistence": {"ok": persistence.ok, "retained": persistence.retained},
            },
            "trace": {
                "request_id": request_id,
                "retrieval_ids": [r.episodic_id for r in retrieval],
                "semantics_count": len(semantics),
                "memory_v2_ids": [item.claim_id for item in memory_batch.selected],
                "memory_v2_trace_id": persistence.memory_trace_id,
                "user_event_id": persistence.user_event_id,
            },
        })
        return
