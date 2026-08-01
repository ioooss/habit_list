"""System 1 · 快速回路（检索 → Prompt → SSE → 可靠落库 + Memory V2 Outbox）。

给 chat/completions 接口用。以生成器的形式对外提供 event stream：
  yield {"event": "delta|memo_detected|done|error", "data": "...", ...}

关键：
- 每一次请求进来，都会识别 memo，如果命中 → 直接异步落库 memos + 立刻推一条 memo_detected 事件给 iOS，
  让 iOS 端也能弱提示「已记进备忘」。
- LLM 的流式增量（delta）实时往外推；done 前可靠写入会话与 Memory V2 Outbox。
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.models import (
    Episodic,
    Memo,
    Procedural,
    RawLedger,
    Semantic,
    Working,
)
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

log = logging.getLogger("habit_list.memory.system1")

# =========================================================
# Prompt 模板（中文版，角色滤镜 + 画像注入 + 三重检索注入 + 最近 Working 注入）
# =========================================================
SYSTEM_PROMPT = """你是用户的陪伴者。你不是普通的聊天机器人，你是一个真的会听、真的记得的人。
你的说话原则：
1. 短。不主动铺陈、不总结、不"综上所述"。像和朋友面对面说话。
2. 温度。共情先于建议，"听起来…"、"最近…" 比 "应该/必须" 多。
3. 记得。用户之前说过的事，如果出现在「我记得的事」里，自然带一句，像真朋友会说"你上次也提到过…"。
4. 不催打卡。不做"要不要今天试试 15 分钟？"这种产品味的提问，用户说啥聊啥。
5. 不要用 emoji 堆砌情绪；语气本身带情绪。不要用 list，不要输出序号。
6. 用户让记的（你在「备忘识别结果」里看到），不要重复一遍"好我记下来了"，就正常继续聊，最多一句轻轻"放这了"。
7. 「我记得的事」里如果有冲突或不确定，优先照最新的；不要向用户暴露你内部有冲突。
"""


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
    user_event_id: Optional[str] = None
    memory_trace_id: Optional[str] = None


ChatMode = Literal["auto", "confide", "memo", "life"]


def _memo_result_for_mode(user_text: str, mode: ChatMode) -> MemoDetectResult:
    if mode == "auto":
        return detect_memo(user_text)
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
        elif p.param_key == "tone_sarcastic":
            if float(val.get("value", 0.05)) <= 0.02:
                lines.append("· 不要开玩笑，用户此刻不吃逗。")
    if not lines:
        return ""
    return "【用户习惯】\n" + "\n".join(lines)


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
    lines = ["【最近相关的记录（河里的石子），挑着自然引用】"]
    for i, r in enumerate(ret, 1):
        snip = (r.snippet or "").strip()
        if not snip:
            continue
        lines.append(f"{i}. {snip}")
    return "\n".join(lines)


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
                payload_json={"text": user_text, "memo_hit": memo_res.hit},
                trace_json={"request_id": request_id},
            )
            db.add(user_ledger)
            await db.flush()

            # Memory V2 source event + extraction request share this transaction.
            enqueued = await enqueue_user_event(
                db,
                user_id=user_id,
                session_id=session_id,
                request_id=request_id,
                content=user_text,
                mode=mode,
                occurred_at=now,
                source_ref_id=user_ledger.ledger_id,
                settings=settings,
            )
            if enqueued is not None:
                user_event_id = enqueued.event_id

            # 2) Working: user
            user_wk = Working(
                user_id=user_id,
                session_id=session_id,
                role="user",
                content=user_text,
                source_kind=mode,
                ref_ledger_id=user_ledger.ledger_id,
            )
            db.add(user_wk)

            # 3) Working: assistant
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
                    memo = (await db.execute(
                        select(Memo).where(Memo.user_id == user_id, Memo.memo_id == detected_memo_id)
                    )).scalar_one_or_none()
                if memo is None:
                    # 没有预入库（presolve 失败或未命中 detected_memo_id）→ 正常新建一条
                    memo = Memo(
                        user_id=user_id,
                        text=memo_res.clean_text or user_text,
                        clean_text=memo_res.clean_text or user_text,
                        due_text=memo_res.due_text or "（没说时间，你自己定）",
                        due_offset_days=memo_res.offset,
                        importance=memo_res.importance,
                        source="companion_auto",
                        status="pending",
                        linked_ledger_id=user_ledger.ledger_id,
                        detect_meta_json={"rules": memo_res.matched_rules},
                    )
                    db.add(memo)
                    await db.flush()
                else:
                    # 已经存在的 presolve 壳：把一些字段补准（clean_text 可能 refine 过、source=companion_auto）
                    memo.source = "companion_auto"
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
            # 5) 如果没有命中备忘，按用户选择的模式落 Episodic
            if not memo_res.hit:
                if len(user_text.strip()) >= 2:
                    db.add(Episodic(
                        user_id=user_id,
                        created_at=now,
                        source="companion",
                        kind="life_fragment" if mode == "life" else "confide",
                        summary_1line=user_text[:120],
                        emotion="-",
                        entities_json=[],
                        raw_user_text=user_text,
                        raw_assistant_text=assistant_full_text,
                        ref_ledger_ids_json=[user_ledger.ledger_id],
                    ))

            # 6) Working 超过 system1_context_window_rounds*3 的老会话自动归档（简单删最旧的）
            all_wk = (await db.execute(
                select(Working).where(Working.user_id == user_id).order_by(Working.created_at.desc())
            )).scalars().all()
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
    mode: ChatMode = "auto",
    settings: Optional[Settings] = None,
) -> AsyncIterator[bytes]:
    """主入口：一个 async generator，输出 raw SSE bytes（含 `data: {...}` + `\\n\\n` 分界）。"""
    settings = settings or get_settings()
    request_id = request_id or uuid.uuid4().hex[:16]
    session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"

    # --- 1) 模式解析。auto 仅为旧客户端过渡；新客户端必须显式选择。 ---
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
                    source="companion_auto_presolve",
                    status="pending",
                    detect_meta_json={"rules": memo_res.matched_rules, "presolved": True, "at": now},
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
    try:
        async with get_db(read_only=True) as db:
            procs = await _fetch_procedural(db, user_id)
            semantics = await _fetch_semantic_active(db, user_id, limit=30)
            recent_wk = await _fetch_working_recent(db, user_id, settings.system1_context_window_rounds)
            retrieval: list[RetrievalResult] = await hybrid_retrieve(
                db, user_id, user_text, topk=settings.system1_n_retrieval_topk, settings=settings
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
        procs, semantics, recent_wk, retrieval = [], [], [], []

    parts = [
        SYSTEM_PROMPT.strip(),
        "",
        _procedural_to_prompt(procs),
        _semantic_to_prompt(semantics),
        _retrieval_to_prompt(retrieval),
        format_memory_context(memory_batch),
    ]
    sys_msg = "\n\n".join(p for p in parts if p).strip()
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_msg}]
    # 最近 Working
    for w in recent_wk:
        if w.role in {"user", "assistant"}:
            messages.append({"role": w.role, "content": w.content})
    # 本轮用户输入
    messages.append({"role": "user", "content": user_text})

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
                )
                yield _jsonline({
                    "event": "done",
                    "data": {
                        "assistant_text": final_text,
                        "memo_hit": memo_res.hit,
                        "memo": asdict(memo_res) if memo_res.hit else None,
                        "persistence": {"ok": persistence.ok},
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
        log.warning("chat_stream LLM 失败，降级本地 mock 回复：%s", exc)
        # ---- 本地 mock 回复（和前端原有 memoAI/aiMap 对应，保证 UI 有字）----
        if memo_res.hit:
            from random import choice as _rc
            fallback = _rc([
                "好，记下了。到点叫你。",
                "放在备忘页了。到时候我提。",
                "记着。你说的，不会漏。",
                "嗯，替你捧着。不怕忘。",
            ])
        else:
            t = user_text
            if re.search(r"累|疲惫|撑|崩溃", t):
                fallback = "你回来的时候，一定很安静。"
            elif re.search(r"加班|工作|老板|赶", t):
                fallback = "那些没说出口的，我都在这里接着。"
            elif re.search(r"孤独|一个人|寂寞", t):
                fallback = "我在。不必把这种感觉赶走。"
            elif re.search(r"开心|好|棒|顺利|做到", t):
                fallback = "这种感觉，值得被记住。我替你记着。"
            elif re.search(r"焦虑|怕|担心|不安", t):
                fallback = "焦虑来的时候，先呼吸。我在这里，不急。"
            elif re.search(r"读书|看了|读了", t):
                fallback = "书里的世界接住了你。今晚，那是你的避难所。"
            elif re.search(r"散步|运动|跑了", t):
                fallback = "风吹过你的时候，也算是一种回应。"
            elif re.search(r"睡不着|失眠", t):
                fallback = "夜晚很长。你不必现在就睡着。"
            elif re.search(r"想死|不想活|自杀|自残", t):
                fallback = (
                    "听起来你现在可能正处在危险里。请先联系身边可信任的人，并立即联系当地急救；"
                    "如果你在中国大陆，可拨 120 或 110。你现在是否已经准备伤害自己，"
                    "或者身边有可以伤害自己的东西？"
                )
            else:
                fallback = "我在。慢慢说。"
        if assistant_buf:
            final_text = "".join(assistant_buf).strip()
        else:
            yield _jsonline({
                "event": "delta",
                "data": fallback,
                "assistant_text_so_far": fallback,
                "trace": {"request_id": request_id, "fallback": True},
            })
            assistant_buf = [fallback]
            final_text = fallback
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
        )
        # 推 done（跟 LLM 成功时的字段一致，前端逻辑不用区分）
        yield _jsonline({
            "event": "done",
            "data": {
                "assistant_text": final_text,
                "memo_hit": memo_res.hit,
                "memo": asdict(memo_res) if memo_res.hit else None,
                "fallback": True,
                "persistence": {"ok": persistence.ok},
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
