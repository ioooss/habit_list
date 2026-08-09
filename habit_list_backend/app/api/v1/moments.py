"""Explicit life fragments and their isolated interaction threads.

A fragment is always a user-owned life record first.  Terrain evidence and AI
interaction are independent, explicit permissions.  Replies in a fragment
thread never enter tasks, terrain evidence, or the conversational memory path.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from ...core.safety import is_crisis_text
from ...db.database import get_db
from ...db.memory_models import OutboxEvent, UserEvent
from ...db.models import Episodic, MediaAsset, MomentInteraction, RawLedger, User, _utcnow_iso
from ...media.service import (
    MediaValidationError,
    asset_response,
    attach_assets,
    delete_asset,
    get_asset_for_user,
    transcript_is_terrain_trusted,
)
from ...memory_v2.service import enqueue_user_event
from ...moments.policy import (
    THROTTLE_MAX_LEVEL,
    append_feedback_rule,
    append_rewrite_preference,
    append_suppression,
    extract_theme_keywords,
    suppressed_source_ids,
    suppressed_theme_keywords,
    text_hits_suppression,
)
from ...moments.service import (
    MOMENT_ECHO_REVISIT_REQUESTED,
    MOMENT_RESPONSE_REQUESTED,
    _invalidate_memory_sources,
    delete_moment_cascade,
    enqueue_moment_response,
    fragment_text,
    invalidate_echoes_for_sources,
    is_sensitive_text,
    normalize_life_reply_mode,
    prepare_echo_revisit,
)
from .common import ApiError, BaseSchema, current_user, request_id

router = APIRouter()

FEEDBACK_KINDS = frozenset(
    {"not_like_me", "less_responses", "stop_source", "stop_category", "unsure"}
)
ECHO_HINT_MAX_AGE_DAYS = 7


class MomentCreate(BaseModel):
    text: str = Field(default="", max_length=4000)
    # A Live Photo is two assets (still + motion) but one visual item.  The
    # transport therefore allows up to 18 asset ids; the semantic 9-item limit
    # is enforced again after ownership and group metadata are loaded.
    media_asset_ids: list[str] = Field(default_factory=list, max_length=18)
    use_for_terrain: bool = False
    allow_proactive: bool = False
    allow_response: bool = True
    save_only: bool = False

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _requires_text_or_media(self):
        if not self.text and not self.media_asset_ids:
            raise ValueError("请写下一句话或加入一段图片/语音")
        return self


class MomentSourceOut(BaseSchema):
    moment_id: str
    excerpt: str
    created_at: str


class MomentInteractionOut(BaseSchema):
    interaction_id: str
    moment_id: str
    actor: str
    kind: str
    content: str
    reaction: str | None
    source_moments: list[MomentSourceOut]
    audio_asset_id: str | None = None
    why_now: str = ""
    user_feedback: str | None = None
    rewritten_by_user: bool = False
    original_content: str | None = None
    created_at: str


class MomentMediaOut(BaseSchema):
    asset_id: str
    kind: str
    mime_type: str
    original_name: str
    byte_size: int
    duration_ms: int | None = None
    transcript: str | None = None
    group_id: str | None = None
    role: str | None = None
    is_live_photo_part: bool = False
    url: str
    created_at: str


class MomentOut(BaseSchema):
    moment_id: str
    text: str
    use_for_terrain: bool
    allow_proactive: bool
    allow_response: bool = True
    save_only: bool = False
    created_at: str
    user_event_id: str | None = None
    interaction_count: int = 0
    latest_agent_interaction: MomentInteractionOut | None = None
    response_pending: bool = False
    response_failed: bool = False
    media: list[MomentMediaOut] = Field(default_factory=list)


class MomentListOut(BaseSchema):
    items: list[MomentOut]


class MomentInteractionCreate(BaseModel):
    content: str = Field(default="", max_length=1200)
    audio_asset_id: str | None = Field(default=None, max_length=36)

    @field_validator("content")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        value = value.strip()
        return value

    @field_validator("audio_asset_id")
    @classmethod
    def _clean_audio_asset_id(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None

    @model_validator(mode="after")
    def _requires_content_or_audio(self):
        if not self.content and not self.audio_asset_id:
            raise ValueError("写一句回应或录一段语音")
        return self


class MomentInteractionCreateOut(BaseSchema):
    interaction: MomentInteractionOut
    response_pending: bool


class MomentThreadOut(BaseSchema):
    moment_id: str
    items: list[MomentInteractionOut]
    response_pending: bool
    response_failed: bool = False


class MomentRetryOut(BaseSchema):
    moment_id: str
    response_pending: bool
    response_failed: bool = False
    retried: bool = False
    reason: str | None = None


class MomentPatch(BaseModel):
    use_for_terrain: bool | None = None
    allow_proactive: bool | None = None
    allow_response: bool | None = None
    save_only: bool | None = None


class MomentFeedbackCreate(BaseModel):
    feedback: Literal[
        "not_like_me", "less_responses", "stop_source", "stop_category", "unsure"
    ]
    keyword: str | None = Field(default=None, max_length=40)


class MomentFeedbackOut(BaseSchema):
    ok: bool
    feedback: str
    suppressions_added: list[str]
    throttle_level: int


class MomentRewriteIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def _clean(cls, value: str) -> str:
        return value.strip()


class MomentRewriteOut(BaseSchema):
    interaction: MomentInteractionOut


class EchoHintOut(BaseSchema):
    interaction: MomentInteractionOut | None = None
    why_now: str = ""
    pending: bool = False
    visit_id: str | None = None


class EchoDismissOut(BaseSchema):
    ok: bool


def _source_ids(interaction: MomentInteraction) -> list[str]:
    return [
        str(value)
        for value in ((interaction.metadata_json or {}).get("source_moment_ids") or [])
        if value
    ][:2]


def _interaction_out(
    interaction: MomentInteraction,
    sources: dict[str, Episodic],
) -> MomentInteractionOut:
    metadata = interaction.metadata_json or {}
    return MomentInteractionOut(
        interaction_id=interaction.interaction_id,
        moment_id=interaction.moment_id,
        actor=interaction.actor,
        kind=interaction.kind,
        content=interaction.content,
        reaction=interaction.reaction,
        audio_asset_id=(
            str((metadata.get("media_asset_ids") or [])[0])
            if isinstance(metadata.get("media_asset_ids"), list)
            and metadata.get("media_asset_ids")
            else None
        ),
        source_moments=[
            MomentSourceOut(
                moment_id=source.episodic_id,
                excerpt=fragment_text(source)[:160],
                created_at=source.created_at,
            )
            for source_id in _source_ids(interaction)
            if (source := sources.get(source_id)) is not None
        ],
        why_now=str(metadata.get("why_now") or ""),
        user_feedback=(
            str(metadata["user_feedback"])
            if isinstance(metadata.get("user_feedback"), str)
            else None
        ),
        rewritten_by_user=bool(metadata.get("rewritten_by_user")),
        original_content=(
            str(metadata["original_content"])
            if metadata.get("original_content") is not None
            else None
        ),
        created_at=interaction.created_at,
    )


async def _load_sources(
    db,
    interactions: list[MomentInteraction],
    *,
    user_id: str,
) -> dict[str, Episodic]:
    source_ids = {
        source_id for interaction in interactions for source_id in _source_ids(interaction)
    }
    if not source_ids:
        return {}
    user = (
        await db.execute(select(User).where(User.user_id == user_id))
    ).scalar_one_or_none()
    settings = user.settings_json if user is not None else {}
    blocked_sources = suppressed_source_ids(settings)
    blocked_keywords = suppressed_theme_keywords(settings)
    rows = list(
        (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id.in_(source_ids),
                    Episodic.user_id == user_id,
                    Episodic.status == "active",
                    Episodic.kind == "life_fragment",
                )
            )
        ).scalars().all()
    )
    # Revoked permission hides the source everywhere an echo could show it.
    return {
        row.episodic_id: row
        for row in rows
        if bool((row.media_json or {}).get("allow_proactive"))
        and not is_sensitive_text(fragment_text(row))
        and row.episodic_id not in blocked_sources
        and not text_hits_suppression(fragment_text(row), blocked_keywords)
    }


def _pending_moment_ids(events: list[OutboxEvent]) -> set[str]:
    return {
        str((event.payload_json or {}).get("moment_id"))
        for event in events
        if (event.payload_json or {}).get("moment_id")
    }


async def _load_moment_outbox_events(db, *, user_id: str) -> list[OutboxEvent]:
    return list(
        (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.user_id == user_id,
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                    OutboxEvent.status.in_(["pending", "processing", "dead"]),
                )
            )
        ).scalars().all()
    )


async def _load_moment_media(
    db, *, user_id: str, moment_ids: list[str]
) -> dict[str, list[MediaAsset]]:
    if not moment_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(MediaAsset)
                .where(
                    MediaAsset.user_id == user_id,
                    MediaAsset.owner_type == "moment",
                    MediaAsset.owner_id.in_(moment_ids),
                    MediaAsset.status == "active",
                )
                .order_by(MediaAsset.created_at.asc(), MediaAsset.asset_id.asc())
            )
        ).scalars().all()
    )
    grouped: dict[str, list[MediaAsset]] = defaultdict(list)
    for asset in rows:
        if asset.owner_id:
            grouped[asset.owner_id].append(asset)
    return grouped


def _media_output(assets: list[MediaAsset]) -> list[MomentMediaOut]:
    return [MomentMediaOut(**asset_response(asset)) for asset in assets]


def _media_visual_count(assets: list[MediaAsset]) -> int:
    """Count user-visible media items, treating a Live Photo pair as one."""

    groups: set[str] = set()
    for asset in assets:
        # Audio is a separate original record, not one of the nine visible
        # image/video slots. A moment may therefore contain nine visual items
        # plus voice and text without being rejected by the media contract.
        if asset.asset_kind == "audio":
            continue
        group_id = str(asset.media_group_id or "").strip()
        if group_id and asset.media_role in {"live_still", "live_motion"}:
            groups.add(f"group:{group_id}")
        else:
            groups.add(f"asset:{asset.asset_id}")
    return len(groups)


def _validate_live_photo_groups(assets: list[MediaAsset]) -> None:
    """Require every explicitly grouped Live Photo to contain one still + motion.

    Uploads are intentionally allowed to remain unattached while a composer is
    open. The semantic check belongs at moment creation, where the complete
    group is known and a half-pair cannot silently enter the life stream.
    """

    grouped: dict[str, list[MediaAsset]] = defaultdict(list)
    for asset in assets:
        group_id = str(asset.media_group_id or "").strip()
        if group_id and asset.media_role in {"live_still", "live_motion"}:
            grouped[group_id].append(asset)
    for group_id, members in grouped.items():
        roles = [member.media_role for member in members]
        if len(members) != 2 or roles.count("live_still") != 1 or roles.count("live_motion") != 1:
            raise MediaValidationError(
                f"Live Photo 媒体组 {group_id} 必须同时包含 1 个静态部分和 1 个动态部分"
            )


def _failed_moment_ids(events: list[OutboxEvent]) -> set[str]:
    return {
        str((event.payload_json or {}).get("moment_id"))
        for event in events
        if event.status == "dead" and (event.payload_json or {}).get("moment_id")
    }


@router.get("", response_model=MomentListOut)
async def list_moments(
    limit: int = Query(default=100, ge=1, le=200),
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=True) as db:
        moments = list(
            (
                await db.execute(
                    select(Episodic)
                    .where(
                        Episodic.user_id == user_id,
                        Episodic.kind == "life_fragment",
                        Episodic.status == "active",
                    )
                    .order_by(Episodic.created_at.desc(), Episodic.episodic_id.desc())
                    .limit(limit)
                )
            ).scalars().all()
        )
        moment_ids = [moment.episodic_id for moment in moments]
        interactions: list[MomentInteraction] = []
        if moment_ids:
            interactions = list(
                (
                    await db.execute(
                        select(MomentInteraction)
                        .where(
                            MomentInteraction.user_id == user_id,
                            MomentInteraction.moment_id.in_(moment_ids),
                            MomentInteraction.status == "active",
                        )
                        .order_by(
                            MomentInteraction.created_at.asc(),
                            MomentInteraction.interaction_id.asc(),
                        )
                    )
                ).scalars().all()
            )
        pending = await _load_moment_outbox_events(db, user_id=user_id)
        sources = await _load_sources(db, interactions, user_id=user_id)
        media_by_moment = await _load_moment_media(db, user_id=user_id, moment_ids=moment_ids)

    interactions_by_moment: dict[str, list[MomentInteraction]] = defaultdict(list)
    for interaction in interactions:
        interactions_by_moment[interaction.moment_id].append(interaction)
    pending_ids = _pending_moment_ids(
        [event for event in pending if event.status in {"pending", "processing"}]
    )
    failed_ids = _failed_moment_ids(pending)
    items: list[MomentOut] = []
    for moment in moments:
        thread = interactions_by_moment[moment.episodic_id]
        agent_rows = [row for row in thread if row.actor == "assistant"]
        latest_agent = agent_rows[-1] if agent_rows else None
        permissions = moment.media_json or {}
        items.append(
            MomentOut(
                moment_id=moment.episodic_id,
                text=moment.raw_user_text,
                use_for_terrain=bool(permissions.get("use_for_terrain")),
                allow_proactive=bool(permissions.get("allow_proactive")),
                allow_response=bool(permissions.get("allow_response", True)),
                save_only=not bool(permissions.get("allow_response", True)),
                created_at=moment.created_at,
                interaction_count=len(thread),
                latest_agent_interaction=(
                    _interaction_out(latest_agent, sources) if latest_agent else None
                ),
                response_pending=moment.episodic_id in pending_ids,
                response_failed=moment.episodic_id in failed_ids,
                media=_media_output(media_by_moment.get(moment.episodic_id, [])),
            )
        )
    return MomentListOut(items=items)


@router.post("", response_model=MomentOut, status_code=status.HTTP_201_CREATED)
async def create_moment(
    body: MomentCreate,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
        user = (
            await db.execute(select(User).where(User.user_id == user_id))
        ).scalar_one()
        try:
            media_assets = await attach_assets(
                db,
                user_id=user_id,
                asset_ids=body.media_asset_ids,
                owner_type="moment",
                owner_id="pending",
            )
        except MediaValidationError as exc:
            raise ApiError("MEDIA_INVALID", str(exc), 400) from exc
        try:
            _validate_live_photo_groups(media_assets)
        except MediaValidationError as exc:
            raise ApiError("MEDIA_INVALID", str(exc), 400) from exc
        if _media_visual_count(media_assets) > 9:
            raise ApiError("MEDIA_LIMIT", "一片生活最多保存 9 个媒体内容；Live Photo 静态与动态部分算 1 个", 400)
        transcripts = [asset.transcript.strip() for asset in media_assets if asset.transcript]
        content_for_memory = body.text or "\n".join(transcripts)
        # The user wrote nothing, so anything here is the machine's guess at what
        # they said.  Baseline 8.3: that is not material to infer a person from
        # unless the provider vouched for it.
        untrusted_transcript = not body.text and bool(transcripts) and not (
            transcript_is_terrain_trusted(media_assets)
        )
        media_fallback = (
            "一段生活语音"
            if any(asset.asset_kind == "audio" for asset in media_assets)
            else "一段动态影像"
            if any(asset.asset_kind == "video" for asset in media_assets)
            else "一张生活图片"
        )
        crisis = is_crisis_text(content_for_memory)
        allow_response = bool(body.allow_response) and not bool(body.save_only)
        formation_paused = bool((user.settings_json or {}).get("memory_formation_paused"))
        # Safety text remains private and cannot be promoted into terrain or a
        # proactive echo source, even when an older client sends both flags.
        effective_use_for_terrain = (
            bool(body.use_for_terrain)
            and bool(content_for_memory.strip())
            and not crisis
            and not untrusted_transcript
            and not bool(body.save_only)
            and not formation_paused
        )
        effective_allow_proactive = (
            bool(body.allow_proactive) and not crisis and not bool(body.save_only)
        )
        response_mode = normalize_life_reply_mode(
            (user.settings_json or {}).get("life_reply_mode")
        )
        ledger = RawLedger(
            user_id=user_id,
            entry_type="moment_explicit",
            session_id=f"moment-{req_id[:24]}",
            payload_json={
                "text": body.text,
                "media_asset_ids": [asset.asset_id for asset in media_assets],
                "use_for_terrain": effective_use_for_terrain,
                "allow_proactive": effective_allow_proactive,
                "allow_response": allow_response,
                "save_only": not allow_response,
                "safety_class": "crisis" if crisis else "normal",
            },
            trace_json={"request_id": req_id},
        )
        db.add(ledger)
        await db.flush()

        moment = Episodic(
            user_id=user_id,
            created_at=now,
            source="moment_explicit",
            kind="life_fragment",
            summary_1line=(body.text or (transcripts[0] if transcripts else media_fallback))[:120],
            emotion="-",
            entities_json=[],
            raw_user_text=body.text or (transcripts[0] if transcripts else ""),
            raw_assistant_text=None,
            media_json={
                "use_for_terrain": effective_use_for_terrain,
                "allow_proactive": effective_allow_proactive,
                "allow_response": allow_response,
                "save_only": not allow_response,
                "safety_class": "crisis" if crisis else "normal",
                "media_asset_ids": [asset.asset_id for asset in media_assets],
                "transcript": "\n".join(transcripts) if transcripts else None,
            },
            ref_ledger_ids_json=[ledger.ledger_id],
        )
        db.add(moment)
        await db.flush()
        for asset in media_assets:
            asset.owner_id = moment.episodic_id

        user_event_id: str | None = None
        if effective_use_for_terrain:
            enqueued = await enqueue_user_event(
                db,
                user_id=user_id,
                session_id=f"moment-{moment.episodic_id}",
                request_id=req_id,
                content=content_for_memory,
                mode="moment",
                source="moment",
                terrain_eligible=effective_use_for_terrain,
                occurred_at=now,
                source_ref_id=ledger.ledger_id,
            )
            if enqueued is not None:
                user_event_id = enqueued.event_id
        response_event = await enqueue_moment_response(
            db,
            user_id=user_id,
            moment_id=moment.episodic_id,
            response_mode=response_mode,
            request_id=req_id,
        )

        return MomentOut(
            moment_id=moment.episodic_id,
            text=content_for_memory,
            use_for_terrain=effective_use_for_terrain,
            allow_proactive=effective_allow_proactive,
            allow_response=allow_response,
            save_only=not allow_response,
            created_at=now,
            user_event_id=user_event_id,
            response_pending=response_event is not None,
            media=_media_output(media_assets),
        )


@router.get("/echo/latest", response_model=EchoHintOut)
async def get_echo_hint(
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    """One low-frequency in-app echo hint for the life page.

    An echo only shown while every cited source stays active, explicitly
    allowed for proactive use, and free of user suppression.
    """

    from datetime import datetime, timedelta

    cutoff = (
        datetime.now(UTC) - timedelta(days=ECHO_HINT_MAX_AGE_DAYS)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with get_db(read_only=False) as db:
        user = (
            await db.execute(select(User).where(User.user_id == user_id))
        ).scalar_one_or_none()
        if user is None:
            raise ApiError("NOT_FOUND", "用户不存在", 404)
        revisit_event = await prepare_echo_revisit(
            db, user_id=user_id, request_id=req_id
        )
        if revisit_event is not None:
            payload = revisit_event.payload_json or {}
            return EchoHintOut(
                pending=True,
                visit_id=str(payload.get("visit_id") or "") or None,
            )
        settings = user.settings_json or {}
        dismissed = {str(value) for value in (settings.get("echo_dismissed_ids") or [])}
        blocked_sources = suppressed_source_ids(settings)
        blocked_keywords = suppressed_theme_keywords(settings)
        echoes = list(
            (
                await db.execute(
                    select(MomentInteraction)
                    .where(
                        MomentInteraction.user_id == user_id,
                        MomentInteraction.actor == "assistant",
                        MomentInteraction.kind == "echo",
                        MomentInteraction.status == "active",
                        MomentInteraction.created_at >= cutoff,
                    )
                    .order_by(
                        MomentInteraction.created_at.desc(),
                        MomentInteraction.interaction_id.desc(),
                    )
                    .limit(12)
                )
            ).scalars().all()
        )
        pending_revisit = next(
            (
                event
                for event in (
                    await db.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.user_id == user_id,
                            OutboxEvent.event_type == MOMENT_ECHO_REVISIT_REQUESTED,
                            OutboxEvent.status.in_(["pending", "processing"]),
                        )
                    )
                ).scalars().all()
                if event
            ),
            None,
        )
        if pending_revisit is not None:
            payload = pending_revisit.payload_json or {}
            return EchoHintOut(
                pending=True,
                visit_id=str(payload.get("visit_id") or "") or None,
            )
        for echo in echoes:
            if echo.interaction_id in dismissed:
                continue
            if (echo.metadata_json or {}).get("trigger_type") != "revisit":
                continue
            source_ids = _source_ids(echo)
            if not source_ids:
                continue
            sources = list(
                (
                    await db.execute(
                        select(Episodic).where(
                            Episodic.episodic_id.in_(source_ids),
                            Episodic.user_id == user_id,
                            Episodic.kind == "life_fragment",
                            Episodic.status == "active",
                        )
                    )
                ).scalars().all()
            )
            valid_sources = {
                source.episodic_id: source
                for source in sources
                if bool((source.media_json or {}).get("allow_proactive"))
                and not is_sensitive_text(source.raw_user_text)
                and source.episodic_id not in blocked_sources
                and not text_hits_suppression(
                    source.raw_user_text or "", blocked_keywords
                )
            }
            if not valid_sources:
                continue
            handed_off = {
                str(value) for value in (settings.get("echo_handoff_ids") or [])
            }
            if echo.interaction_id in handed_off:
                continue
            updated = dict(settings)
            updated["echo_handoff_ids"] = [*handed_off, echo.interaction_id][-50:]
            user.settings_json = updated
            return EchoHintOut(
                interaction=_interaction_out(echo, valid_sources),
                why_now=str((echo.metadata_json or {}).get("why_now") or ""),
                visit_id=str((echo.metadata_json or {}).get("visit_id") or "") or None,
            )
        # One-time compatibility handoff for echoes produced on a fragment
        # before the revisit delivery path existed. It is tracked so a life
        # page refresh cannot keep replaying the same old interaction.
        handed_off = {str(value) for value in (settings.get("echo_handoff_ids") or [])}
        for echo in echoes:
            if echo.interaction_id in dismissed or echo.interaction_id in handed_off:
                continue
            if (echo.metadata_json or {}).get("trigger_type") not in {None, "initial"}:
                continue
            source_ids = _source_ids(echo)
            if not source_ids:
                continue
            sources = list(
                (
                    await db.execute(
                        select(Episodic).where(
                            Episodic.episodic_id.in_(source_ids),
                            Episodic.user_id == user_id,
                            Episodic.kind == "life_fragment",
                            Episodic.status == "active",
                        )
                    )
                ).scalars().all()
            )
            valid_sources = {
                source.episodic_id: source
                for source in sources
                if bool((source.media_json or {}).get("allow_proactive"))
                and not is_sensitive_text(source.raw_user_text)
                and source.episodic_id not in blocked_sources
                and not text_hits_suppression(source.raw_user_text or "", blocked_keywords)
            }
            if not valid_sources:
                continue
            updated = dict(settings)
            updated["echo_handoff_ids"] = [*handed_off, echo.interaction_id][-50:]
            user.settings_json = updated
            return EchoHintOut(
                interaction=_interaction_out(echo, valid_sources),
                why_now=str((echo.metadata_json or {}).get("why_now") or ""),
            )
    return EchoHintOut()


@router.post("/echo/{interaction_id}/dismiss", response_model=EchoDismissOut)
async def dismiss_echo_hint(
    interaction_id: str,
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=False) as db:
        interaction = (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.interaction_id == interaction_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.actor == "assistant",
                    MomentInteraction.kind == "echo",
                    MomentInteraction.status == "active",
                )
            )
        ).scalar_one_or_none()
        if interaction is None:
            raise ApiError("NOT_FOUND", "这条回声不存在", 404)
        user = (
            await db.execute(select(User).where(User.user_id == user_id))
        ).scalar_one()
        settings = dict(user.settings_json or {})
        dismissed = [str(value) for value in (settings.get("echo_dismissed_ids") or [])]
        if interaction_id not in dismissed:
            dismissed.append(interaction_id)
        settings["echo_dismissed_ids"] = dismissed[-50:]
        user.settings_json = settings
    return EchoDismissOut(ok=True)


@router.get("/{moment_id}/interactions", response_model=MomentThreadOut)
async def get_moment_interactions(
    moment_id: str,
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=True) as db:
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
            raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
        interactions = list(
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
        sources = await _load_sources(db, interactions, user_id=user_id)
        pending = await _load_moment_outbox_events(db, user_id=user_id)
    return MomentThreadOut(
        moment_id=moment_id,
        items=[_interaction_out(item, sources) for item in interactions],
        response_pending=moment_id
        in _pending_moment_ids(
            [event for event in pending if event.status in {"pending", "processing"}]
        ),
        response_failed=moment_id in _failed_moment_ids(pending),
    )


@router.post(
    "/{moment_id}/interactions",
    response_model=MomentInteractionCreateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_moment_interaction(
    moment_id: str,
    body: MomentInteractionCreate,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
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
            raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
        audio_asset = None
        if body.audio_asset_id:
            audio_asset = await get_asset_for_user(
                db,
                user_id=user_id,
                asset_id=body.audio_asset_id,
                kind="audio",
            )
            if audio_asset is None:
                raise ApiError("MEDIA_NOT_FOUND", "这段语音不存在或已被删除", 404)
        content = body.content or (audio_asset.transcript.strip() if audio_asset and audio_asset.transcript else "")
        ledger = RawLedger(
            user_id=user_id,
            entry_type="moment_reply_explicit",
            session_id=f"moment-{moment_id[:24]}",
            payload_json={
                "moment_id": moment_id,
                "content": content,
                "audio_asset_id": body.audio_asset_id,
            },
            trace_json={"request_id": req_id},
        )
        db.add(ledger)
        await db.flush()
        interaction = MomentInteraction(
            moment_id=moment_id,
            user_id=user_id,
            actor="user",
            kind="comment",
            content=content,
            metadata_json={
                "ledger_id": ledger.ledger_id,
                "request_id": req_id,
                "media_asset_ids": [],
            },
            created_at=now,
        )
        db.add(interaction)
        await db.flush()
        if audio_asset is not None:
            try:
                await attach_assets(
                    db,
                    user_id=user_id,
                    asset_ids=[audio_asset.asset_id],
                    owner_type="moment_interaction",
                    owner_id=interaction.interaction_id,
                )
            except MediaValidationError as exc:
                raise ApiError("MEDIA_INVALID", str(exc), 400) from exc
            metadata = dict(interaction.metadata_json or {})
            metadata["media_asset_ids"] = [audio_asset.asset_id]
            interaction.metadata_json = metadata
            await db.flush()
        event = await enqueue_moment_response(
            db,
            user_id=user_id,
            moment_id=moment_id,
            response_mode="always",
            request_id=req_id,
            trigger_interaction_id=interaction.interaction_id,
        )
        out = _interaction_out(interaction, {})
    return MomentInteractionCreateOut(interaction=out, response_pending=event is not None)


@router.delete(
    "/{moment_id}/interactions/{interaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_moment_interaction(
    moment_id: str,
    interaction_id: str,
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=False) as db:
        interaction = (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.interaction_id == interaction_id,
                    MomentInteraction.moment_id == moment_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.status == "active",
                )
            )
        ).scalar_one_or_none()
        if interaction is None:
            raise ApiError("NOT_FOUND", "这条回应不存在", 404)
        for asset_id in [
            str(value)
            for value in (interaction.metadata_json or {}).get("media_asset_ids") or []
            if value
        ]:
            await delete_asset(db, user_id=user_id, asset_id=asset_id)
        interaction.status = "deleted"
        metadata = dict(interaction.metadata_json or {})
        metadata["deleted_at"] = _utcnow_iso()
        interaction.metadata_json = metadata
    return None


@router.post("/{moment_id}/retry", response_model=MomentRetryOut)
async def retry_moment_response(
    moment_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    """Re-queue a fragment whose automatic response previously failed (dead).

    We reset the original dead outbox row rather than insert a new one: the
    worker is idempotent by ``outbox_id`` and re-checks fragment state before
    writing, so a retried event can never produce a duplicate interaction.
    No-op (retried=False) when the fragment already has an assistant response
    or is currently pending.
    """

    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
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
            raise ApiError("NOT_FOUND", "这条生活碎片不存在", 404)

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
        has_assistant = any(item.actor == "assistant" for item in interactions)
        if has_assistant:
            return MomentRetryOut(
                moment_id=moment_id,
                response_pending=False,
                response_failed=False,
                retried=False,
                reason="already_responded",
            )

        permissions = moment.media_json or {}
        crisis = is_crisis_text(fragment_text(moment))
        if not crisis and not bool(permissions.get("allow_response", True)):
            return MomentRetryOut(
                moment_id=moment_id,
                response_pending=False,
                response_failed=False,
                retried=False,
                reason="save_only",
            )

        event = (
            await db.execute(
                select(OutboxEvent).where(
                    OutboxEvent.user_id == user_id,
                    OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                    OutboxEvent.aggregate_id == moment_id,
                ).order_by(OutboxEvent.created_at.desc())
            )
        ).scalars().first()

        if event is not None and event.status in ("pending", "processing"):
            return MomentRetryOut(
                moment_id=moment_id,
                response_pending=True,
                response_failed=False,
                retried=False,
                reason="already_pending",
            )

        if event is not None and event.status == "dead":
            event.status = "pending"
            event.attempts = 0
            event.locked_at = None
            event.available_at = now
            event.last_error = None
            payload = dict(event.payload_json or {})
            payload["request_id"] = req_id
            payload["retried_at"] = now
            event.payload_json = payload
            return MomentRetryOut(
                moment_id=moment_id,
                response_pending=True,
                response_failed=False,
                retried=True,
            )

        # No dead/initial event exists (e.g. silent mode saved only): enqueue
        # a fresh response so the user gets an explicit "try again" path.
        mode = normalize_life_reply_mode((moment.media_json or {}).get("life_reply_mode"))
        if not crisis and mode == "silent":
            mode = "occasional"
        enqueued = await enqueue_moment_response(
            db,
            user_id=user_id,
            moment_id=moment_id,
            response_mode=mode,
            request_id=req_id,
        )
        if enqueued is None:
            return MomentRetryOut(
                moment_id=moment_id,
                response_pending=False,
                response_failed=False,
                retried=False,
                reason="not_applicable",
            )
        return MomentRetryOut(
            moment_id=moment_id,
            response_pending=True,
            response_failed=False,
            retried=True,
        )


@router.patch(
    "/{moment_id}/interactions/{interaction_id}",
    response_model=MomentRewriteOut,
)
async def rewrite_moment_interaction(
    moment_id: str,
    interaction_id: str,
    body: MomentRewriteIn,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    """Let the user rewrite an assistant line in their own words.

    The user's text becomes the visible content; the original is preserved in
    ``metadata.original_content`` so we keep an auditable trail of what the
    model produced versus what the user kept. Rewriting also signals "this is
    how I'd rather be spoken to" back into the feedback rules.
    """

    now = _utcnow_iso()
    new_content = body.content
    async with get_db(read_only=False) as db:
        interaction = (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.interaction_id == interaction_id,
                    MomentInteraction.moment_id == moment_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.status == "active",
                )
            )
        ).scalar_one_or_none()
        if interaction is None:
            raise ApiError("NOT_FOUND", "这条回应不存在", 404)
        if interaction.actor != "assistant":
            raise ApiError(
                "INVALID_REWRITE",
                "只有它的回应可以修改表达",
                status.HTTP_400_BAD_REQUEST,
            )
        if new_content == (interaction.content or "").strip():
            # Idempotent: same text → nothing to do.
            sources = await _load_sources(db, interactions=[interaction], user_id=user_id)
            return MomentRewriteOut(
                interaction=_interaction_out(interaction, sources),
            )

        metadata = dict(interaction.metadata_json or {})
        # Preserve the first model-produced version once; subsequent rewrites
        # keep the earliest original so the audit trail always points at what
        # the model actually said.
        if not metadata.get("rewritten_by_user"):
            metadata["original_content"] = interaction.content
        metadata["rewritten_by_user"] = True
        metadata["rewritten_at"] = now
        metadata["rewrite_request_id"] = req_id
        if isinstance(metadata.get("rewrite_count"), int):
            metadata["rewrite_count"] = int(metadata["rewrite_count"]) + 1
        else:
            metadata["rewrite_count"] = 1
        interaction.metadata_json = metadata
        interaction.content = new_content

        # Fold the correction into user feedback rules — same signal as
        # "not_like_me" but scoped to this exact wording.
        user = (
            await db.execute(select(User).where(User.user_id == user_id))
        ).scalar_one()
        settings = dict(user.settings_json or {})
        settings = append_feedback_rule(
            settings,
            kind=interaction.kind,
            reaction=interaction.reaction,
            created_at=now,
        )
        settings = append_rewrite_preference(
            settings,
            text=new_content,
            created_at=now,
        )
        user.settings_json = settings

        db.add(
            RawLedger(
                user_id=user_id,
                entry_type="moment_interaction_rewrite",
                session_id=f"moment-{moment_id[:24]}",
                payload_json={
                    "moment_id": moment_id,
                    "interaction_id": interaction_id,
                    "original_excerpt": (metadata.get("original_content") or "")[:400],
                    "rewrite_excerpt": new_content[:400],
                    "rewrite_count": metadata["rewrite_count"],
                },
                trace_json={"request_id": req_id},
            )
        )

        sources = await _load_sources(db, interactions=[interaction], user_id=user_id)
        return MomentRewriteOut(
            interaction=_interaction_out(interaction, sources),
        )


@router.post(
    "/{moment_id}/interactions/{interaction_id}/feedback",
    response_model=MomentFeedbackOut,
)
async def create_moment_feedback(
    moment_id: str,
    interaction_id: str,
    body: MomentFeedbackCreate,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    """User feedback that must change future responses, not just show a toast."""

    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
        interaction = (
            await db.execute(
                select(MomentInteraction).where(
                    MomentInteraction.interaction_id == interaction_id,
                    MomentInteraction.moment_id == moment_id,
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.status == "active",
                )
            )
        ).scalar_one_or_none()
        if interaction is None:
            raise ApiError("NOT_FOUND", "这条回应不存在", 404)
        if interaction.actor != "assistant":
            raise ApiError(
                "INVALID_FEEDBACK", "反馈只用于它的回应", status.HTTP_400_BAD_REQUEST
            )
        user = (
            await db.execute(select(User).where(User.user_id == user_id))
        ).scalar_one()
        settings = dict(user.settings_json or {})
        added: list[str] = []
        source_ids = _source_ids(interaction)

        if body.feedback == "not_like_me":
            # For a normal comment there is no cited source.  Still bind the
            # correction to the current fragment and its concrete themes so a
            # later fragment cannot silently replay the same interpretation.
            for source_id in source_ids or [moment_id]:
                settings = append_suppression(
                    settings, entry_type="source", value=source_id, created_at=now
                )
                added.append(f"source:{source_id}")
            anchor = (
                await db.execute(
                    select(Episodic).where(
                        Episodic.episodic_id == moment_id,
                        Episodic.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            keywords = extract_theme_keywords(
                f"{(anchor.raw_user_text if anchor else '')} {interaction.content}",
                limit=2,
            )
            for keyword in keywords:
                settings = append_suppression(
                    settings, entry_type="theme", value=keyword, created_at=now
                )
                added.append(f"theme:{keyword}")
            settings = append_feedback_rule(
                settings,
                kind=interaction.kind,
                reaction=interaction.reaction,
                created_at=now,
            )
        elif body.feedback == "less_responses":
            level = min(
                int(settings.get("life_reply_throttle_level") or 0) + 1,
                THROTTLE_MAX_LEVEL,
            )
            settings["life_reply_throttle_level"] = level
        elif body.feedback == "stop_source":
            for source_id in source_ids or [moment_id]:
                settings = append_suppression(
                    settings, entry_type="source", value=source_id, created_at=now
                )
                added.append(f"source:{source_id}")
        elif body.feedback == "stop_category":
            for source_id in source_ids:
                settings = append_suppression(
                    settings, entry_type="source", value=source_id, created_at=now
                )
                added.append(f"source:{source_id}")
            keyword = (body.keyword or "").strip()
            if not keyword:
                anchor_ids = source_ids or [moment_id]
                anchors = list(
                    (
                        await db.execute(
                            select(Episodic).where(
                                Episodic.episodic_id.in_(anchor_ids),
                                Episodic.user_id == user_id,
                            )
                        )
                    ).scalars().all()
                )
                anchor_text = " ".join(
                    (anchor.raw_user_text or "")[:300] for anchor in anchors
                )
                keywords = extract_theme_keywords(anchor_text, limit=1)
                keyword = keywords[0] if keywords else ""
            if keyword:
                settings = append_suppression(
                    settings, entry_type="theme", value=keyword, created_at=now
                )
                added.append(f"theme:{keyword}")

        metadata = dict(interaction.metadata_json or {})
        metadata["user_feedback"] = body.feedback
        metadata["feedback_at"] = now
        metadata["feedback_request_id"] = req_id
        interaction.metadata_json = metadata
        user.settings_json = settings
        db.add(
            RawLedger(
                user_id=user_id,
                entry_type="moment_feedback",
                session_id=f"moment-{moment_id[:24]}",
                payload_json={
                    "moment_id": moment_id,
                    "interaction_id": interaction_id,
                    "feedback": body.feedback,
                    "keyword": (body.keyword or "").strip() or None,
                    "applied": added,
                },
                trace_json={"request_id": req_id},
            )
        )
        return MomentFeedbackOut(
            ok=True,
            feedback=body.feedback,
            suppressions_added=added,
            throttle_level=int(settings.get("life_reply_throttle_level") or 0),
        )


@router.patch("/{moment_id}", response_model=MomentOut)
async def patch_moment(
    moment_id: str,
    body: MomentPatch,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    """Change a fragment's purposes after saving.

    Turning terrain use off revokes the derived user event and cancels queued
    extraction; turning proactive quoting off removes the fragment from every
    future echo candidate and hides existing echo source displays.
    """

    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
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
            raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
        user = (
            await db.execute(select(User).where(User.user_id == user_id))
        ).scalar_one()
        permissions = dict(moment.media_json or {})
        previous_allow_proactive = bool(permissions.get("allow_proactive"))
        crisis = is_crisis_text(moment.raw_user_text)
        # Granting terrain use later must not smuggle in an unverified transcript.
        # Provenance is derived from the data rather than a stored flag so that
        # fragments written before this gate existed are judged the same way: the
        # text is the machine's only if it is exactly what ASR produced.
        stored_transcript = str(permissions.get("transcript") or "").strip()
        authored_text = (moment.raw_user_text or "").strip()
        machine_only_text = bool(stored_transcript) and authored_text in {
            stored_transcript,
            stored_transcript.split("\n", 1)[0].strip(),
        }
        untrusted_transcript = machine_only_text and not transcript_is_terrain_trusted(
            (await _load_moment_media(db, user_id=user_id, moment_ids=[moment_id])).get(
                moment_id, []
            )
        )
        changed: list[str] = []
        if body.allow_proactive is not None and bool(
            permissions.get("allow_proactive")
        ) != body.allow_proactive:
            permissions["allow_proactive"] = body.allow_proactive
            changed.append("allow_proactive")
        terrain_changed = False
        if body.use_for_terrain is not None and bool(
            permissions.get("use_for_terrain")
        ) != body.use_for_terrain:
            permissions["use_for_terrain"] = (
                bool(body.use_for_terrain) and not crisis and not untrusted_transcript
            )
            changed.append("use_for_terrain")
            terrain_changed = True
        current_allow_response = bool(permissions.get("allow_response", True))
        next_allow_response = current_allow_response
        if body.save_only is not None:
            next_allow_response = not bool(body.save_only)
        if body.allow_response is not None:
            next_allow_response = bool(body.allow_response)
        if crisis:
            # The user may not accidentally turn a safety fragment into an
            # proactive or terrain source later.
            if permissions.get("use_for_terrain"):
                changed.append("use_for_terrain")
            if permissions.get("allow_proactive"):
                changed.append("allow_proactive")
            permissions["use_for_terrain"] = False
            permissions["allow_proactive"] = False
        if body.save_only:
            # The explicit per-fragment save-only choice is stronger than
            # independently supplied legacy permission flags.
            if permissions.get("use_for_terrain"):
                changed.append("use_for_terrain")
                terrain_changed = True
            if permissions.get("allow_proactive"):
                changed.append("allow_proactive")
            permissions["use_for_terrain"] = False
            permissions["allow_proactive"] = False
        if previous_allow_proactive and not bool(permissions.get("allow_proactive")):
            await invalidate_echoes_for_sources(
                db,
                user_id=user_id,
                source_moment_ids={moment_id},
                now=now,
            )
        changed = list(dict.fromkeys(changed))
        if next_allow_response != current_allow_response:
            permissions["allow_response"] = next_allow_response
            permissions["save_only"] = not next_allow_response
            changed.append("allow_response")
        response_changed = next_allow_response != current_allow_response
        if not changed:
            return MomentOut(
                moment_id=moment.episodic_id,
                text=moment.raw_user_text,
                use_for_terrain=bool(permissions.get("use_for_terrain")),
                allow_proactive=bool(permissions.get("allow_proactive")),
                allow_response=current_allow_response,
                save_only=not current_allow_response,
                created_at=moment.created_at,
            )
        moment.media_json = permissions
        ledger_ids = list(moment.ref_ledger_ids_json or [])
        user_event_id: str | None = None
        if terrain_changed:
            if bool(permissions.get("use_for_terrain")):
                enqueued = await enqueue_user_event(
                    db,
                    user_id=user_id,
                    session_id=f"moment-{moment.episodic_id}",
                    request_id=req_id,
                    content=moment.raw_user_text,
                    mode="moment",
                    source="moment",
                    terrain_eligible=True,
                    occurred_at=moment.created_at,
                    source_ref_id=ledger_ids[0] if ledger_ids else None,
                )
                if enqueued is not None:
                    user_event_id = enqueued.event_id
            elif ledger_ids:
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
                for event in events:
                    # Retracting the terrain use of a fragment must also revoke
                    # the row-level permission, otherwise a later formation scan
                    # would still treat the source as eligible material.
                    event.terrain_eligible = False
                await _invalidate_memory_sources(
                    db,
                    user_id=user_id,
                    event_ids=event_ids,
                    now=now,
                    request_id=req_id,
                )
        response_event = None
        if response_changed and not next_allow_response:
            pending = list(
                (
                    await db.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.user_id == user_id,
                            OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                            OutboxEvent.status.in_(["pending", "processing"]),
                        )
                    )
                ).scalars().all()
            )
            for event in pending:
                payload = event.payload_json or {}
                if (
                    str(payload.get("moment_id") or "") == moment_id
                    and str(payload.get("trigger_type") or "initial") == "initial"
                ):
                    event.status = "cancelled"
                    event.locked_at = None
                    event.last_error = "save_only"
        if response_changed and next_allow_response and not crisis:
            response_event = await enqueue_moment_response(
                db,
                user_id=user_id,
                moment_id=moment.episodic_id,
                response_mode=normalize_life_reply_mode(
                    (user.settings_json or {}).get("life_reply_mode")
                ),
                request_id=req_id,
            )
        db.add(
            RawLedger(
                user_id=user_id,
                entry_type="moment_permission_change",
                session_id=f"moment-{moment_id[:24]}",
                payload_json={"moment_id": moment_id, "changed": changed},
                trace_json={"request_id": req_id},
            )
        )
        return MomentOut(
            moment_id=moment.episodic_id,
            text=moment.raw_user_text,
            use_for_terrain=bool(permissions.get("use_for_terrain")),
            allow_proactive=bool(permissions.get("allow_proactive")),
            allow_response=bool(permissions.get("allow_response", True)),
            save_only=not bool(permissions.get("allow_response", True)),
            created_at=moment.created_at,
            user_event_id=user_event_id,
            response_pending=response_event is not None,
        )


@router.delete("/{moment_id}")
async def delete_moment(
    moment_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    """Delete a fragment with its full closed loop.

    Thread interactions are soft-deleted, queued responses are cancelled, and
    terrain-derived events are revoked so nothing can revive the fragment.
    """

    async with get_db(read_only=False) as db:
        deleted = await delete_moment_cascade(
            db, user_id=user_id, moment_id=moment_id, request_id=req_id
        )
    if not deleted:
        raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
    return {"ok": True, "deleted": True}


__all__ = ["router"]
