"""Selective, source-grounded AI interactions for explicit life fragments."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.safety import CRISIS_RESPONSE, is_crisis_text
from ..db.database import get_db
from ..db.memory_models import (
    MemoryClaim,
    MemoryDeletionTombstone,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryRelation,
    OutboxEvent,
    UserEvent,
)
from ..db.models import Episodic, MomentInteraction, RawLedger, User, _utcnow_iso
from ..media.service import delete_asset, load_media_prompt_parts
from ..memory_v2.domain import MemoryCategory, Sensitivity
from ..memory_v2.extractor import infer_sensitivity
from ..memory_v2.reconcile import EMBEDDING_REQUESTED
from ..memory_v2.service import EXTRACTION_REQUESTED
from ..providers import dashscope
from .policy import (
    MOMENT_POLICY_VERSION,
    GateDecision,
    echo_budget_available,
    evaluate_response_gate,
    feedback_rules,
    load_rewrite_preferences,
    recently_used_source_ids,
    suppressed_source_ids,
    suppressed_theme_keywords,
    text_hits_suppression,
)

log = logging.getLogger("habit_list.moments")

MOMENT_RESPONSE_REQUESTED = "moment.response.requested"
MOMENT_ECHO_REVISIT_REQUESTED = "moment.echo.revisit.requested"
MOMENT_RESPONSE_POLICY_VERSION = "moment-witness-v2"
MOMENT_PROMPT_VERSION = "moment-witness-prompt-v2"
LIFE_REPLY_MODES = frozenset({"silent", "occasional", "always"})


def fragment_text(moment: Episodic) -> str:
    """Return text available to policy/model while retaining original media."""

    if moment.raw_user_text:
        return moment.raw_user_text
    metadata = moment.media_json or {}
    transcript = metadata.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        return transcript.strip()
    return "（一段生活语音，没有文字转写）"


def is_sensitive_text(text: str | None) -> bool:
    """Conservative source filter for proactive recall."""

    return infer_sensitivity(text or "", MemoryCategory.OTHER) != Sensitivity.NORMAL


class MomentAgentDecision(BaseModel):
    """Strict output contract for one optional agent interaction."""

    should_respond: bool
    reaction: Literal["none", "seen", "paused", "echo"] = "none"
    kind: Literal["reaction", "comment", "echo"] = "comment"
    comment: str = Field(default="", max_length=220)
    source_moment_ids: list[str] = Field(default_factory=list, max_length=2)
    why_now: str = Field(
        default="",
        max_length=160,
        description="回声必须回答：为什么现在提起这一条旧片段。",
    )

    @field_validator("comment", "why_now")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return value.strip()


def normalize_life_reply_mode(value: object) -> str:
    mode = str(value or "occasional").strip().lower()
    return mode if mode in LIFE_REPLY_MODES else "occasional"


async def enqueue_moment_response(
    db: AsyncSession,
    *,
    user_id: str,
    moment_id: str,
    response_mode: str,
    request_id: str,
    trigger_interaction_id: str | None = None,
) -> OutboxEvent | None:
    """Enqueue by identifiers only; raw fragment text never enters the outbox."""

    trigger_type = "user_reply" if trigger_interaction_id else "initial"
    moment = (
        await db.execute(
            select(Episodic).where(
                Episodic.episodic_id == moment_id,
                Episodic.user_id == user_id,
                Episodic.kind == "life_fragment",
                Episodic.status == "active",
            )
        )
    ).scalar_one_or_none()
    if moment is None:
        return None
    mode = normalize_life_reply_mode(response_mode)
    trigger_text = fragment_text(moment)
    if trigger_interaction_id:
        trigger = (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.interaction_id == trigger_interaction_id,
                    MomentInteraction.moment_id == moment_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.actor == "user",
                    MomentInteraction.status == "active",
                )
            )
        ).scalar_one_or_none()
        if trigger is None:
            return None
        trigger_text = trigger.content or ""
    safety_override = is_crisis_text(trigger_text)
    permissions = moment.media_json or {}
    # `save_only` is an explicit per-fragment choice.  Crisis safety responses
    # are the only deliberate exception: silence and ordinary throttles cannot
    # suppress a safety handoff.
    if trigger_type == "initial" and not safety_override:
        if not bool(permissions.get("allow_response", True)):
            return None
        if mode == "silent":
            return None
    if trigger_type == "user_reply":
        mode = "always"
    outbox = OutboxEvent(
        user_id=user_id,
        aggregate_type="moment",
        aggregate_id=trigger_interaction_id or moment_id,
        event_type=MOMENT_RESPONSE_REQUESTED,
        payload_json={
            "moment_id": moment_id,
            "trigger_interaction_id": trigger_interaction_id,
            "trigger_type": trigger_type,
            "response_mode": mode,
            "safety_override": safety_override,
            "request_id": request_id,
        },
    )
    db.add(outbox)
    await db.flush()
    return outbox


async def _load_generation_context(
    outbox: OutboxEvent,
) -> tuple[Episodic, User, list[MomentInteraction], list[Episodic], GateDecision | None] | None:
    payload = outbox.payload_json or {}
    moment_id = str(payload.get("moment_id") or "")
    if not moment_id or not outbox.user_id:
        return None
    async with get_db(read_only=True) as db:
        moment = (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id == moment_id,
                    Episodic.user_id == outbox.user_id,
                    Episodic.kind == "life_fragment",
                    Episodic.status == "active",
                )
            )
        ).scalar_one_or_none()
        user = (
            await db.execute(select(User).where(User.user_id == outbox.user_id))
        ).scalar_one_or_none()
        if moment is None or user is None:
            return None
        thread = list(
            await _select_thread(db, moment_id=moment_id, user_id=str(outbox.user_id))
        )
        candidates = await _select_echo_candidates(
            db, user_id=str(outbox.user_id), moment_id=moment_id, settings_json=user.settings_json
        )
        gate = await evaluate_response_gate(
            db,
            user_id=str(outbox.user_id),
            moment=moment,
            response_mode=normalize_life_reply_mode(payload.get("response_mode")),
            trigger_type=str(payload.get("trigger_type") or "initial"),
            settings_json=user.settings_json,
        )
    return moment, user, thread, candidates, gate


async def _select_thread(
    db: AsyncSession, *, moment_id: str, user_id: str
) -> list[MomentInteraction]:
    return list(
        (
            await db.execute(
                select(MomentInteraction)
                .where(
                    MomentInteraction.moment_id == moment_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.status == "active",
                )
                .order_by(
                    MomentInteraction.created_at.asc(),
                    MomentInteraction.interaction_id.asc(),
                )
            )
        ).scalars().all()
    )


async def _select_echo_candidates(
    db: AsyncSession,
    *,
    user_id: str,
    moment_id: str,
    settings_json: dict | None,
) -> list[Episodic]:
    """Echo sources must stay active, explicitly allowed, and unsuppressed."""

    candidates = list(
        (
            await db.execute(
                select(Episodic)
                .where(
                    Episodic.user_id == user_id,
                    Episodic.kind == "life_fragment",
                    Episodic.status == "active",
                    Episodic.episodic_id != moment_id,
                )
                .order_by(Episodic.created_at.desc(), Episodic.episodic_id.desc())
                .limit(20)
            )
        ).scalars().all()
    )
    blocked_sources = suppressed_source_ids(settings_json)
    blocked_keywords = suppressed_theme_keywords(settings_json)
    used_sources = await recently_used_source_ids(db, user_id=user_id)
    allowed: list[Episodic] = []
    for item in candidates:
        if not bool((item.media_json or {}).get("allow_proactive")):
            continue
        # Sensitive and crisis fragments are never a surprising proactive
        # source, even if an older client accidentally stored the permission.
        if is_sensitive_text(fragment_text(item)):
            continue
        if item.episodic_id in blocked_sources or item.episodic_id in used_sources:
            continue
        if text_hits_suppression(fragment_text(item), blocked_keywords):
            continue
        allowed.append(item)
        if len(allowed) >= 10:
            break
    return allowed


async def prepare_echo_revisit(
    db: AsyncSession,
    *,
    user_id: str,
    request_id: str,
) -> OutboxEvent | None:
    """Create one identifier-only revisit request for a life-page visit.

    Existing fragment replies are deliberately not returned here. A visit is a
    new delivery opportunity: it selects a current anchor plus still-authorized
    historical candidates and asks the worker to make a fresh decision.
    """

    now = datetime.now(UTC).replace(microsecond=0)
    cutoff = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    user = (
        await db.execute(
            select(User).where(User.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        return None
    active_revisit = list(
        (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.actor == "assistant",
                    MomentInteraction.kind == "echo",
                    MomentInteraction.status == "active",
                )
            )
        ).scalars().all()
    )
    dismissed = {str(value) for value in (user.settings_json or {}).get("echo_dismissed_ids", [])}
    for interaction in active_revisit:
        metadata = interaction.metadata_json or {}
        if (
            metadata.get("trigger_type") == "revisit"
            and interaction.created_at >= cutoff
            and interaction.interaction_id not in dismissed
        ):
            return None
    pending = list(
        (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.user_id == user_id,
                    OutboxEvent.event_type == MOMENT_ECHO_REVISIT_REQUESTED,
                    OutboxEvent.status.in_(["pending", "processing"]),
                )
            )
        ).scalars().all()
    )
    if pending:
        return None
    state = (user.settings_json or {}).get("echo_visit_state") or {}
    if str(state.get("last_scheduled_at") or "") >= cutoff:
        return None
    anchors = list(
        (
            await db.execute(
                select(Episodic)
                .where(
                    Episodic.user_id == user_id,
                    Episodic.kind == "life_fragment",
                    Episodic.status == "active",
                )
                .order_by(Episodic.created_at.desc(), Episodic.episodic_id.desc())
                .limit(1)
            )
        ).scalars().all()
    )
    if not anchors:
        return None
    anchor = anchors[0]
    # A safety or explicit save-only fragment can remain in the private life
    # stream, but it must never create a proactive revisit placeholder.
    if (
        is_sensitive_text(fragment_text(anchor))
        or is_crisis_text(fragment_text(anchor))
        or not bool((anchor.media_json or {}).get("allow_response", True))
    ):
        return None
    candidates = await _select_echo_candidates(
        db,
        user_id=user_id,
        moment_id=anchor.episodic_id,
        settings_json=user.settings_json,
    )
    if not candidates:
        return None
    visit_id = hashlib.sha256(
        f"{user_id}|{anchor.episodic_id}|{now.isoformat()}".encode()
    ).hexdigest()[:32]
    event = OutboxEvent(
        user_id=user_id,
        aggregate_type="moment",
        aggregate_id=anchor.episodic_id,
        event_type=MOMENT_ECHO_REVISIT_REQUESTED,
        payload_json={
            "anchor_moment_id": anchor.episodic_id,
            "source_moment_ids": [item.episodic_id for item in candidates[:6]],
            "visit_id": visit_id,
            "trigger_type": "revisit",
            "request_id": request_id,
        },
    )
    db.add(event)
    settings = dict(user.settings_json or {})
    settings["echo_visit_state"] = {
        "last_scheduled_at": now.isoformat().replace("+00:00", "Z"),
        "last_anchor_id": anchor.episodic_id,
        "visit_id": visit_id,
    }
    user.settings_json = settings
    await db.flush()
    return event


async def generate_agent_decision(
    *,
    user_id: str,
    moment_text: str,
    thread: list[dict[str, str]],
    echo_candidates: list[dict[str, str]],
    response_mode: str,
    style: str,
    trigger_type: str,
    request_id: str,
    settings: Settings,
    media_asset_ids: list[str] | None = None,
    rewrite_preferences: list[str] | None = None,
) -> MomentAgentDecision:
    system_prompt = f"""
[Prompt 版本 {MOMENT_PROMPT_VERSION}]
你是手机应用“内在地形”里明确标注身份的 AI 陪伴者。用户写下的是生活碎片，
不是求分析的素材，也不是待办。你的角色是有分寸的见证者。

必须遵守：
1. 不把记录改造成目标、建议、待办或人格结论；不诊断，不冒充真人感受。
2. 只回应片段里的具体细节，不使用“你总是”“这说明你是”之类定性表达。
3. 评论最多两句、尽量少于 80 个中文字符；最多问一个很轻的问题。
4. occasional 模式下，多数普通或信息很薄的记录应该 should_respond=false。
5. always 模式下应回应，但仍可只给 seen/paused 反应和一句短评。
6. 只有确实与候选旧片段中的具体细节有关，才使用 echo；必须返回候选里的来源 ID，
   并在 why_now 里用一句自然语言回答“为什么现在提起它”。
7. 不要写泛化套话，如“感谢分享”“为你点赞”“继续加油”。
8. reaction: seen=安静看见，paused=被具体细节留住，echo=与旧片段形成回声。
9. 回声是为了让用户看见连续性或变化，不是为了证明你记得很多。
""".strip()
    user_payload = {
        "trigger_type": trigger_type,
        "response_mode": response_mode,
        "interaction_style": style,
        "current_fragment": moment_text[:2000],
        "thread": thread[-8:],
        "echo_candidates": echo_candidates,
    }
    if rewrite_preferences:
        # 这些是用户亲手改过的表达示例：作为“用户更喜欢的语气/分寸”参考，
        # 不是可逐字复读的固定话术，也不是关于用户的结论。
        user_payload["rewrite_preferences"] = [
            pref[:200] for pref in rewrite_preferences
        ]
    media_parts = await load_media_prompt_parts(
        user_id=user_id,
        asset_ids=media_asset_ids,
        settings=settings,
    )
    model_content: str | list[dict[str, Any]] = str(user_payload)
    if media_parts:
        # DashScope's OpenAI-compatible multimodal endpoint requires every
        # content part to carry an explicit type.  The legacy shorthand
        # ``{"text": ...}`` is rejected when an image/audio part is present.
        model_content = [{"type": "text", "text": str(user_payload)}, *media_parts]
    raw = await dashscope.chat_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": model_content},
        ],
        json_schema=MomentAgentDecision.model_json_schema(),
        schema_name="moment_agent_decision",
        temperature=0.35,
        max_tokens=420,
        request_id=request_id,
        settings=settings,
    )
    return MomentAgentDecision.model_validate(raw)


async def process_moment_response(outbox: OutboxEvent, settings: Settings) -> None:
    """Generate at most one idempotent, fragment-scoped assistant interaction."""

    context = await _load_generation_context(outbox)
    if context is None:
        return
    moment, user, interactions, echo_candidates, gate = context
    # 用户在“我们”页暂停了记忆形成：除危机安全回应外，不生成 AI 回应、不沉淀记忆。
    if (user.settings_json or {}).get("memory_paused") and not is_crisis_text(
        fragment_text(moment)
    ):
        log.info(
            "moment response skipped: memory_paused user=%s moment=%s",
            outbox.user_id,
            moment.episodic_id,
        )
        return
    if any(
        interaction.actor == "assistant"
        and (interaction.metadata_json or {}).get("outbox_id") == outbox.outbox_id
        for interaction in interactions
    ):
        return

    payload = outbox.payload_json or {}
    trigger_type = str(payload.get("trigger_type") or "initial")
    response_mode = normalize_life_reply_mode(payload.get("response_mode"))
    trigger_interaction_id = str(payload.get("trigger_interaction_id") or "")
    trigger_text = fragment_text(moment)
    model_media_ids = [
        str(value)
        for value in ((moment.media_json or {}).get("media_asset_ids") or [])
        if value
    ]
    if trigger_interaction_id:
        trigger = next(
            (
                item
                for item in interactions
                if item.interaction_id == trigger_interaction_id and item.actor == "user"
            ),
            None,
        )
        if trigger is None:
            return
        trigger_text = trigger.content
        model_media_ids = [
            str(value)
            for value in ((trigger.metadata_json or {}).get("media_asset_ids") or [])
            if value
        ]

    safety_override = is_crisis_text(trigger_text)
    if not safety_override and trigger_type == "initial":
        if not bool((moment.media_json or {}).get("allow_response", True)):
            return
        if gate is not None and not gate.allowed:
            log.info(
                "moment response gate closed user=%s moment=%s reasons=%s policy=%s",
                outbox.user_id,
                moment.episodic_id,
                ",".join(gate.reasons),
                gate.policy_version,
            )
            return
    elif not safety_override and gate is not None and not gate.allowed:
        log.info(
            "moment response gate closed user=%s moment=%s reasons=%s policy=%s",
            outbox.user_id,
            moment.episodic_id,
            ",".join(gate.reasons),
            gate.policy_version,
        )
        return
    gate_reasons = list(gate.reasons) if gate is not None else []
    if safety_override:
        gate_reasons = ["safety_override", *gate_reasons]

    if safety_override:
        decision = MomentAgentDecision(
            should_respond=True,
            reaction="paused",
            kind="comment",
            comment=CRISIS_RESPONSE,
        )
    else:
        candidate_payload = [
            {
                "moment_id": item.episodic_id,
                "created_at": item.created_at,
                "text": fragment_text(item)[:500],
            }
            for item in echo_candidates
        ]
        thread_payload = [
            {"actor": item.actor, "content": item.content[:500]}
            for item in interactions
        ]
        decision = await generate_agent_decision(
            user_id=str(outbox.user_id),
            moment_text=trigger_text,
            thread=thread_payload,
            echo_candidates=candidate_payload,
            response_mode=response_mode,
            style=user.current_style,
            trigger_type=trigger_type,
            request_id=str(payload.get("request_id") or outbox.outbox_id),
            settings=settings,
            media_asset_ids=model_media_ids,
            rewrite_preferences=load_rewrite_preferences(user.settings_json),
        )

    if not decision.should_respond:
        return
    allowed_source_ids = {item.episodic_id for item in echo_candidates}
    source_ids = [
        source_id
        for source_id in decision.source_moment_ids
        if source_id in allowed_source_ids and source_id != moment.episodic_id
    ][:2]
    reaction = decision.reaction
    kind = decision.kind
    why_now = decision.why_now
    downgrade_reasons: list[str] = []
    constraints = feedback_rules(user.settings_json)
    if not safety_override and (
        kind in set(constraints.get("kinds") or [])
        or reaction in set(constraints.get("reactions") or [])
    ):
        # A correction changes the stored interaction shape even when the model
        # proposes the same style again.  A minimal seen/paused marker is less
        # intrusive than silently replaying a rejected comment or reaction.
        downgrade_reasons.append("feedback_not_like_me")
        kind = "reaction"
        why_now = ""
        reaction = "paused" if reaction == "seen" else "seen"
        decision.comment = ""
    if source_ids:
        # Echoes are only valid with a concrete, explainable connection point.
        if trigger_type == "initial":
            async with get_db(read_only=True) as db:
                if not await echo_budget_available(db, user_id=str(outbox.user_id)):
                    source_ids = []
                    downgrade_reasons.append("echo_budget_exhausted")
        if source_ids and not why_now:
            source_ids = []
            downgrade_reasons.append("echo_missing_why_now")
    if source_ids:
        kind = "echo"
        reaction = "echo"
    else:
        why_now = ""
        if kind == "echo":
            kind = "comment" if decision.comment else "reaction"
            reaction = "paused" if reaction == "echo" else reaction
    if not decision.comment and reaction == "none":
        return

    async with get_db(read_only=False) as db:
        current_moment = (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id == moment.episodic_id,
                    Episodic.user_id == outbox.user_id,
                    Episodic.status == "active",
                )
            )
        ).scalar_one_or_none()
        current_user = (
            await db.execute(select(User).where(User.user_id == outbox.user_id))
        ).scalar_one_or_none()
        if current_moment is None or current_user is None:
            return
        if (
            trigger_type == "initial"
            and not safety_override
            and not bool((current_moment.media_json or {}).get("allow_response", True))
        ):
            return

        # The model call happens outside this transaction. Re-check every
        # cited source at write time so a concurrent delete, permission revoke,
        # sensitivity update, or suppression cannot leave a derived echo alive.
        if source_ids:
            blocked_sources = suppressed_source_ids(current_user.settings_json)
            blocked_keywords = suppressed_theme_keywords(current_user.settings_json)
            current_sources = list(
                (
                    await db.execute(
                        select(Episodic).where(
                            Episodic.user_id == outbox.user_id,
                            Episodic.episodic_id.in_(source_ids),
                            Episodic.kind == "life_fragment",
                            Episodic.status == "active",
                        )
                    )
                ).scalars().all()
            )
            valid_source_ids = {
                source.episodic_id
                for source in current_sources
                if bool((source.media_json or {}).get("allow_proactive"))
                and not is_sensitive_text(fragment_text(source))
                and source.episodic_id not in blocked_sources
                and not text_hits_suppression(
                    fragment_text(source), blocked_keywords
                )
            }
            if valid_source_ids != set(source_ids):
                # Do not persist a model response whose text may contain a
                # source that was deleted or revoked while generation ran.
                return

        latest_constraints = feedback_rules(current_user.settings_json)
        if not safety_override and (
            kind in set(latest_constraints.get("kinds") or [])
            or reaction in set(latest_constraints.get("reactions") or [])
        ):
            kind = "reaction"
            reaction = "paused" if reaction == "seen" else "seen"
            source_ids = []
            why_now = ""
            decision.comment = ""
            if "feedback_not_like_me" not in downgrade_reasons:
                downgrade_reasons.append("feedback_not_like_me")
        existing = list(
            (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.moment_id == moment.episodic_id,
                        MomentInteraction.actor == "assistant",
                    )
                )
            ).scalars().all()
        )
        if any(
            (item.metadata_json or {}).get("outbox_id") == outbox.outbox_id
            for item in existing
        ):
            return
        db.add(
            MomentInteraction(
                moment_id=moment.episodic_id,
                user_id=str(outbox.user_id),
                actor="assistant",
                kind=kind,
                content=decision.comment,
                reaction=None if reaction == "none" else reaction,
                metadata_json={
                    "outbox_id": outbox.outbox_id,
                    "trigger_type": trigger_type,
                    "source_moment_ids": source_ids,
                    "response_mode": response_mode,
                    "policy_version": MOMENT_RESPONSE_POLICY_VERSION,
                    "prompt_version": MOMENT_PROMPT_VERSION,
                    "gate_policy_version": MOMENT_POLICY_VERSION,
                    "gate_reasons": gate_reasons,
                    "downgrade_reasons": downgrade_reasons,
                    "why_now": why_now,
                    "request_id": str(payload.get("request_id") or outbox.outbox_id),
                    "safety_response": safety_override,
                },
                created_at=_utcnow_iso(),
            )
        )


async def process_moment_echo_revisit(outbox: OutboxEvent, settings: Settings) -> None:
    """Turn a life-page visit request into one fresh, source-grounded echo."""

    payload = outbox.payload_json or {}
    anchor_id = str(payload.get("anchor_moment_id") or "")
    requested_sources = {
        str(value) for value in (payload.get("source_moment_ids") or []) if value
    }
    if not anchor_id or not requested_sources or not outbox.user_id:
        return
    async with get_db(read_only=True) as db:
        anchor = (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id == anchor_id,
                    Episodic.user_id == outbox.user_id,
                    Episodic.kind == "life_fragment",
                    Episodic.status == "active",
                )
            )
        ).scalar_one_or_none()
        user = (
            await db.execute(select(User).where(User.user_id == outbox.user_id))
        ).scalar_one_or_none()
        if anchor is None or user is None or is_sensitive_text(fragment_text(anchor)):
            return
        if not bool((anchor.media_json or {}).get("allow_response", True)):
            return
        source_rows = list(
            (
                await db.execute(
                    select(Episodic).where(
                        Episodic.user_id == outbox.user_id,
                        Episodic.episodic_id.in_(requested_sources),
                        Episodic.kind == "life_fragment",
                        Episodic.status == "active",
                    )
                )
            ).scalars().all()
        )
        blocked_sources = suppressed_source_ids(user.settings_json)
        blocked_keywords = suppressed_theme_keywords(user.settings_json)
        used_sources = await recently_used_source_ids(
            db, user_id=str(outbox.user_id)
        )
        sources = [
            row
            for row in source_rows
            if row.episodic_id != anchor_id
            and bool((row.media_json or {}).get("allow_proactive"))
            and not is_sensitive_text(fragment_text(row))
            and row.episodic_id not in blocked_sources
            and row.episodic_id not in used_sources
            and not text_hits_suppression(fragment_text(row), blocked_keywords)
        ]
        if not sources:
            return
        thread = await _select_thread(
            db, moment_id=anchor_id, user_id=str(outbox.user_id)
        )
        if not await echo_budget_available(db, user_id=str(outbox.user_id)):
            return
    candidate_payload = [
        {
            "moment_id": item.episodic_id,
            "created_at": item.created_at,
            "text": fragment_text(item)[:500],
        }
        for item in sources
    ]
    decision = await generate_agent_decision(
        user_id=str(outbox.user_id),
        moment_text=fragment_text(anchor),
        thread=[{"actor": item.actor, "content": item.content[:500]} for item in thread],
        echo_candidates=candidate_payload,
        response_mode="occasional",
        style=user.current_style,
        trigger_type="revisit",
        request_id=str(payload.get("request_id") or outbox.outbox_id),
        settings=settings,
        media_asset_ids=[
            str(value)
            for value in ((anchor.media_json or {}).get("media_asset_ids") or [])
            if value
        ],
        rewrite_preferences=load_rewrite_preferences(user.settings_json),
    )
    if not decision.should_respond:
        return
    allowed_ids = {item.episodic_id for item in sources}
    source_ids = [item for item in decision.source_moment_ids if item in allowed_ids][:2]
    if not source_ids or not decision.why_now:
        return
    if not decision.comment and decision.reaction == "none":
        return
    async with get_db(read_only=False) as db:
        anchor_now = (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id == anchor_id,
                    Episodic.user_id == outbox.user_id,
                    Episodic.status == "active",
                )
            )
        ).scalar_one_or_none()
        current_user = (
            await db.execute(select(User).where(User.user_id == outbox.user_id))
        ).scalar_one_or_none()
        if (
            anchor_now is None
            or current_user is None
            or is_sensitive_text(fragment_text(anchor_now))
            or not bool((anchor_now.media_json or {}).get("allow_response", True))
        ):
            return
        latest_sources = list(
            (
                await db.execute(
                    select(Episodic).where(
                        Episodic.user_id == outbox.user_id,
                        Episodic.episodic_id.in_(source_ids),
                        Episodic.kind == "life_fragment",
                        Episodic.status == "active",
                    )
                )
            ).scalars().all()
        )
        blocked_sources = suppressed_source_ids(current_user.settings_json)
        blocked_keywords = suppressed_theme_keywords(current_user.settings_json)
        latest_source_ids = {
            source.episodic_id
            for source in latest_sources
            if bool((source.media_json or {}).get("allow_proactive"))
                and not is_sensitive_text(fragment_text(source))
            and source.episodic_id not in blocked_sources
                and not text_hits_suppression(fragment_text(source), blocked_keywords)
        }
        if latest_source_ids != set(source_ids):
            # A source can be revoked while the model is generating. Abandon
            # the response instead of retaining text derived from that source.
            return
        if not await echo_budget_available(db, user_id=str(outbox.user_id)):
            return
        latest_constraints = feedback_rules(current_user.settings_json)
        if (
            "echo" in set(latest_constraints.get("kinds") or [])
            or "echo" in set(latest_constraints.get("reactions") or [])
        ):
            return
        duplicate = list(
            (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.moment_id == anchor_id,
                        MomentInteraction.user_id == outbox.user_id,
                        MomentInteraction.actor == "assistant",
                        MomentInteraction.kind == "echo",
                    )
                )
            ).scalars().all()
        )
        if any(
            (item.metadata_json or {}).get("outbox_id") == outbox.outbox_id
            for item in duplicate
        ):
            return
        db.add(
            MomentInteraction(
                moment_id=anchor_id,
                user_id=str(outbox.user_id),
                actor="assistant",
                kind="echo",
                content=decision.comment,
                reaction="echo",
                metadata_json={
                    "outbox_id": outbox.outbox_id,
                    "trigger_type": "revisit",
                    "visit_id": str(payload.get("visit_id") or ""),
                    "source_moment_ids": source_ids,
                    "why_now": decision.why_now,
                    "response_mode": "occasional",
                    "policy_version": MOMENT_RESPONSE_POLICY_VERSION,
                    "prompt_version": MOMENT_PROMPT_VERSION,
                    "request_id": str(payload.get("request_id") or outbox.outbox_id),
                },
                created_at=_utcnow_iso(),
            )
        )


async def cancel_pending_moment_events(
    db: AsyncSession,
    *,
    user_id: str,
    moment_ids: set[str],
) -> int:
    """Stop queued fragment responses so deleted/revoked content cannot revive.

    Cancelled outbox rows are never claimed by the worker, and the generation
    path re-checks fragment state before writing anything.
    """

    if not moment_ids:
        return 0
    events = list(
        (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.user_id == user_id,
                    OutboxEvent.event_type.in_([
                        MOMENT_RESPONSE_REQUESTED,
                        MOMENT_ECHO_REVISIT_REQUESTED,
                    ]),
                    OutboxEvent.status.in_(["pending", "processing"]),
                )
            )
        ).scalars().all()
    )
    cancelled = 0
    for event in events:
        payload = event.payload_json or {}
        source_ids = {
            str(value)
            for value in (payload.get("source_moment_ids") or [])
            if value
        }
        if (
            str(payload.get("moment_id") or "") in moment_ids
            or str(payload.get("anchor_moment_id") or "") in moment_ids
            or source_ids.intersection(moment_ids)
        ):
            event.status = "cancelled"
            event.locked_at = None
            event.last_error = "source_deleted"
            cancelled += 1
    return cancelled


async def invalidate_echoes_for_sources(
    db: AsyncSession,
    *,
    user_id: str,
    source_moment_ids: set[str],
    now: str,
    reason: str = "source_permission_revoked",
) -> int:
    """Hide derived echoes and revisit work after source authorization changes."""

    source_moment_ids = {str(value) for value in source_moment_ids if value}
    if not source_moment_ids:
        return 0
    echoes = list(
        (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.actor == "assistant",
                    MomentInteraction.kind == "echo",
                    MomentInteraction.status == "active",
                )
            )
        ).scalars().all()
    )
    invalidated = 0
    for echo in echoes:
        cited_sources = {
            str(value)
            for value in (echo.metadata_json or {}).get("source_moment_ids") or []
            if value
        }
        if not cited_sources.intersection(source_moment_ids):
            continue
        echo.status = "deleted"
        metadata = dict(echo.metadata_json or {})
        metadata["invalidated_at"] = now
        metadata["invalidated_reason"] = reason
        echo.metadata_json = metadata
        invalidated += 1

    pending = list(
        (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.user_id == user_id,
                    OutboxEvent.event_type == MOMENT_ECHO_REVISIT_REQUESTED,
                    OutboxEvent.status.in_(["pending", "processing"]),
                )
            )
        ).scalars().all()
    )
    for event in pending:
        payload = event.payload_json or {}
        cited_sources = {
            str(value) for value in (payload.get("source_moment_ids") or []) if value
        }
        if not cited_sources.intersection(source_moment_ids):
            continue
        event.status = "cancelled"
        event.locked_at = None
        event.last_error = reason
    return invalidated


async def _invalidate_memory_sources(
    db: AsyncSession,
    *,
    user_id: str,
    event_ids: set[str],
    now: str,
    request_id: str | None,
) -> set[str]:
    """Invalidate evidence and queued work that came from deleted sources.

    Claims may have other independent evidence. In that case only the deleted
    binding is removed; when the last binding is gone the claim and its vector
    are made unusable as well.
    """

    if not event_ids:
        return set()
    evidence_rows = list(
        (
            await db.execute(
                select(MemoryEvidence).where(MemoryEvidence.event_id.in_(event_ids))
            )
        ).scalars().all()
    )
    claim_ids = {row.claim_id for row in evidence_rows}
    for row in evidence_rows:
        evidence_hash = hashlib.sha256(
            f"{user_id}|{row.event_id}|{hashlib.sha256((row.excerpt_text or '').encode('utf-8')).hexdigest()}".encode()
        ).hexdigest()
        exists = (
            await db.execute(
                select(MemoryDeletionTombstone.tombstone_id).where(
                    MemoryDeletionTombstone.resource_type == "memory_claim_evidence",
                    MemoryDeletionTombstone.resource_hash == evidence_hash,
                )
            )
        ).first()
        if exists is None:
            db.add(
                MemoryDeletionTombstone(
                    resource_type="memory_claim_evidence",
                    resource_hash=evidence_hash,
                    actor_type="user",
                    request_id=request_id,
                )
            )
    await db.execute(delete(MemoryEvidence).where(MemoryEvidence.event_id.in_(event_ids)))
    await db.execute(delete(MemoryRelation).where(MemoryRelation.source_event_id.in_(event_ids)))

    deleted_claim_ids: set[str] = set()
    if claim_ids:
        claims = list(
            (
                await db.execute(
                    select(MemoryClaim).where(
                        MemoryClaim.user_id == user_id,
                        MemoryClaim.claim_id.in_(claim_ids),
                        MemoryClaim.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        for claim in claims:
            remaining = len(
                list(
                    (
                        await db.execute(
                            select(MemoryEvidence.evidence_id)
                            .join(UserEvent, UserEvent.event_id == MemoryEvidence.event_id)
                            .where(
                                MemoryEvidence.claim_id == claim.claim_id,
                                UserEvent.user_id == user_id,
                                UserEvent.status == "active",
                            )
                        )
                    ).scalars().all()
                )
            )
            claim.evidence_count = remaining
            if remaining:
                continue
            claim.deleted_at = now
            claim.valid_to = now
            claim.allow_proactive = False
            claim.user_status = "hidden"
            deleted_claim_ids.add(claim.claim_id)
            claim_hash = hashlib.sha256(
                f"{user_id}|memory_claim|{claim.claim_id}".encode()
            ).hexdigest()
            exists = (
                await db.execute(
                    select(MemoryDeletionTombstone.tombstone_id).where(
                        MemoryDeletionTombstone.resource_type == "memory_claim",
                        MemoryDeletionTombstone.resource_hash == claim_hash,
                    )
                )
            ).first()
            if exists is None:
                db.add(
                    MemoryDeletionTombstone(
                        resource_type="memory_claim",
                        resource_hash=claim_hash,
                        actor_type="user",
                        request_id=request_id,
                    )
                )
    if deleted_claim_ids:
        await db.execute(
            delete(MemoryEmbedding).where(MemoryEmbedding.claim_id.in_(deleted_claim_ids))
        )
        await db.execute(
            delete(MemoryRelation).where(
                or_(
                    (MemoryRelation.src_type == "claim")
                    & MemoryRelation.src_id.in_(deleted_claim_ids),
                    (MemoryRelation.dst_type == "claim")
                    & MemoryRelation.dst_id.in_(deleted_claim_ids),
                )
            )
        )

    pending = list(
        (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.user_id == user_id,
                    OutboxEvent.event_type.in_([EXTRACTION_REQUESTED, EMBEDDING_REQUESTED]),
                    OutboxEvent.status.in_(["pending", "processing"]),
                )
            )
        ).scalars().all()
    )
    for event in pending:
        payload = event.payload_json or {}
        aggregate_id = str(event.aggregate_id or "")
        if (
            str(payload.get("event_id") or "") in event_ids
            or aggregate_id in event_ids
            or aggregate_id in deleted_claim_ids
        ):
            event.status = "cancelled"
            event.locked_at = None
            event.last_error = "source_deleted"

    for event_id in event_ids:
        source = (
            await db.execute(
                select(UserEvent).where(
                    UserEvent.event_id == event_id,
                    UserEvent.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if source is not None:
            source.status = "deleted"
            source.deleted_at = now
    return deleted_claim_ids


async def delete_moment_cascade(
    db: AsyncSession,
    *,
    user_id: str,
    moment_id: str,
    request_id: str | None = None,
) -> bool:
    """Full deletion closed loop for one life fragment.

    Archives the fragment, soft-deletes every thread interaction, cancels all
    pending response outbox events, and revokes terrain-derived user events so
    nothing can be regenerated from the deleted source.
    """

    moment = (
        await db.execute(
            select(Episodic).where(
                Episodic.episodic_id == moment_id,
                Episodic.user_id == user_id,
                Episodic.kind == "life_fragment",
            )
        )
    ).scalar_one_or_none()
    if moment is None:
        return False
    now = _utcnow_iso()
    if moment.status != "archived":
        moment.status = "archived"
    media_ids = [
        str(value)
        for value in (moment.media_json or {}).get("media_asset_ids") or []
        if value
    ]
    for asset_id in media_ids:
        await delete_asset(db, user_id=user_id, asset_id=asset_id)
    interactions = list(
        (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.moment_id == moment_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.status == "active",
                )
            )
        ).scalars().all()
    )
    for interaction in interactions:
        for asset_id in [
            str(value)
            for value in (interaction.metadata_json or {}).get("media_asset_ids") or []
            if value
        ]:
            await delete_asset(db, user_id=user_id, asset_id=asset_id)
        interaction.status = "deleted"
        metadata = dict(interaction.metadata_json or {})
        metadata["deleted_at"] = now
        interaction.metadata_json = metadata
    await cancel_pending_moment_events(db, user_id=user_id, moment_ids={moment_id})

    ledger_ids = set(moment.ref_ledger_ids_json or [])
    if ledger_ids:
        events = list(
            (
                await db.execute(
                    select(UserEvent).where(
                        UserEvent.user_id == user_id,
                        UserEvent.source_ref_id.in_(ledger_ids),
                        UserEvent.status == "active",
                    )
                )
            ).scalars().all()
        )
        event_ids = {event.event_id for event in events}
        await _invalidate_memory_sources(
            db,
            user_id=user_id,
            event_ids=event_ids,
            now=now,
            request_id=request_id,
        )

    # An echo stored on another fragment is derived content too. Hide it so a
    # later list or visit cannot resurrect a citation to the deleted source.
    echoes = list(
        (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.actor == "assistant",
                    MomentInteraction.kind == "echo",
                    MomentInteraction.status == "active",
                )
            )
        ).scalars().all()
    )
    for echo in echoes:
        source_ids = {
            str(value)
            for value in (echo.metadata_json or {}).get("source_moment_ids") or []
        }
        if moment_id not in source_ids:
            continue
        echo.status = "deleted"
        metadata = dict(echo.metadata_json or {})
        metadata["invalidated_at"] = now
        metadata["invalidated_reason"] = "source_deleted"
        echo.metadata_json = metadata
    db.add(
        RawLedger(
            user_id=user_id,
            entry_type="moment_delete",
            session_id=f"moment-{moment_id[:24]}",
            payload_json={"moment_id": moment_id},
            trace_json={"request_id": request_id or ""},
        )
    )
    return True


__all__ = [
    "LIFE_REPLY_MODES",
    "MOMENT_ECHO_REVISIT_REQUESTED",
    "MOMENT_PROMPT_VERSION",
    "MOMENT_RESPONSE_POLICY_VERSION",
    "MOMENT_RESPONSE_REQUESTED",
    "MomentAgentDecision",
    "_invalidate_memory_sources",
    "cancel_pending_moment_events",
    "delete_moment_cascade",
    "enqueue_moment_response",
    "generate_agent_decision",
    "invalidate_echoes_for_sources",
    "is_sensitive_text",
    "normalize_life_reply_mode",
    "process_moment_response",
    "process_moment_echo_revisit",
    "prepare_echo_revisit",
]
