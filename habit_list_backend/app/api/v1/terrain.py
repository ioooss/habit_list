"""Terrain projection: formed features, unnamed leads, and shared naming.

Only the formation layer may assert that something is terrain.  Counting how
often a single claim recurred is not the same act: it says "this happened
repeatedly", never "this is what is forming in you".  So an evidence-mature
claim that no formation has named yet is surfaced as a *lead* rather than as
terrain, and the difference is visible in the response instead of hidden.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import get_settings
from ...db.database import get_db
from ...db.memory_models import MemoryClaim, MemoryEvidence, UserEvent
from ...db.models import User, Working, _utcnow_iso
from ...memory_v2.domain import SourceType
from ...memory_v2.service import get_claim_for_user
from ..v1.common import ApiError, BaseSchema, current_user, request_id

router = APIRouter()

TERRAIN_FADE_DAYS = 60

_VISIBLE_STATUSES = {"proposed", "deferred", "confirmed", "corrected"}
_TERRAIN_CATEGORIES = {"goal", "habit", "creativity", "cycle", "other"}

# The five expressions allowed by product baseline 4.1.  A formed claim carries
# its kind in ``terrain_state``; the read projection never invents a sixth.
_KIND_LABELS = {
    "growing": "正在长出来",
    "recurring": "反复回到",
    "loosening": "正在松动",
    "two_forces": "两股力量",
    "unnamed": "尚未命名",
}
_FADED_STATE = "faded"


class TerrainThresholds(BaseModel):
    min_evidence: int
    min_span_days: int
    min_contexts: int


class TerrainChangeOut(BaseSchema):
    at: str
    kind: str
    state: str
    reason: str
    evidence_count: int = 0
    span_days: int = 0


class TerrainCandidateOut(BaseSchema):
    claim_id: str
    title: str
    evidence_count: int
    span_days: int
    context_count: int
    first_seen_at: str
    last_seen_at: str
    user_status: str
    name_options: list[str] = Field(default_factory=list)


class TerrainItemOut(BaseSchema):
    claim_id: str
    terrain_type: str
    title: str
    maturity: str
    user_status: str
    evidence_count: int
    span_days: int
    context_count: int
    first_seen_at: str
    last_seen_at: str
    allow_proactive: bool
    # Every supporting moment's timestamp, so the client can draw where in time
    # this feature actually happened instead of only how long it lasted.
    evidence_at: list[str] = Field(default_factory=list)
    # The formation's own words for "why this surfaced now". Never a threshold count.
    why_now: str = ""
    state: str = "unnamed"
    user_label: str | None = None
    first_revealed_at: str | None = None
    valid_to: str | None = None
    is_first_reveal: bool = False
    name_options: list[str] = Field(default_factory=list)
    recent_changes: list[TerrainChangeOut] = Field(default_factory=list)


class TerrainWeatherOut(BaseSchema):
    """此刻天气：一个词，加上它是从哪一刻读到的。

    没有分数、没有表情、没有序列。词永远是用户自己写下的那个词——
    见 ``app/memory/weather.py`` 里为什么它是回声而不是推断。
    """

    word: str
    at: str


class TerrainListOut(BaseSchema):
    items: list[TerrainItemOut]
    candidates: list[TerrainCandidateOut] = Field(default_factory=list)
    recent_changes: list[TerrainChangeOut] = Field(default_factory=list)
    withheld_count: int
    thresholds: TerrainThresholds
    fade_after_days: int = TERRAIN_FADE_DAYS
    # 读不到就是 None，客户端因此把槽位藏起来而不是填一个编出来的词。
    weather: TerrainWeatherOut | None = None


class TerrainNamePatch(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kind_of(claim: MemoryClaim) -> str:
    state = claim.terrain_state
    return state if state in _KIND_LABELS else "unnamed"


def _maturity(claim: MemoryClaim, *, evidence_count: int, span_days: int, faded: bool) -> str:
    if faded:
        return "正在消退"
    if claim.user_status == "deferred":
        return "等你再看看"
    if claim.user_status in {"confirmed", "corrected"}:
        return "你已校正"
    if evidence_count >= 5 and span_days >= 21:
        return "已有较多支持"
    return "证据刚够"


def _name_options(claim: MemoryClaim, *, kind: str) -> list[str]:
    if claim.terrain_user_label:
        return [claim.terrain_user_label]
    options = [claim.claim_text[:48]]
    labels = {
        "growing": "正在长出来的部分",
        "recurring": "反复回到的地方",
        "loosening": "正在松动的旧路",
        "two_forces": "两股力量之间",
        "unnamed": "还说不清的部分",
    }
    if kind in labels:
        options.append(labels[kind])
    return list(dict.fromkeys(option for option in options if option))[:3]


def _history(claim: MemoryClaim) -> list[dict]:
    value = claim.terrain_history_json
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _change_output(rows: list[dict]) -> list[TerrainChangeOut]:
    return [TerrainChangeOut(**row) for row in rows[-5:]]


def _why_now(rows: list[dict]) -> str:
    """The most recent reason the formation layer itself gave for surfacing this.

    Lifecycle entries (fade / return) explain a state change, not the feature, so
    they are skipped; if nothing formation-authored is left the card shows no
    explanation rather than a fabricated one.
    """
    for row in reversed(rows):
        if row.get("kind") in {"formed", "formation_strengthened"}:
            return str(row.get("reason") or "")
    return ""


def _supporting(bindings: list[tuple[MemoryEvidence, UserEvent]]) -> list[UserEvent]:
    return list(
        {
            event.event_id: event
            for evidence, event in bindings
            if evidence.evidence_role in {"supports", "corrects"}
        }.values()
    )


async def _read_weather(db: AsyncSession, *, user_id: str, now: datetime) -> TerrainWeatherOut | None:
    """The most recent session-level weather word, if it has not dispersed yet.

    Weather lives on ``Working`` and is read, never accumulated: this returns at
    most one word and there is deliberately no endpoint that returns a series.
    A paused or muted user gets nothing, because a wisp on the terrain page still
    reads as the product saying something about them.
    """

    settings = get_settings()
    if not settings.terrain_weather_enabled:
        return None
    user = (
        await db.execute(select(User).where(User.user_id == user_id))
    ).scalar_one_or_none()
    prefs = (user.settings_json if user is not None else None) or {}
    if prefs.get("weather_muted") or prefs.get("memory_paused"):
        return None
    cutoff = _iso(now - timedelta(hours=settings.terrain_weather_ttl_hours))
    row = (
        await db.execute(
            select(Working)
            .where(
                Working.user_id == user_id,
                Working.role == "user",
                Working.mood.is_not(None),
                Working.created_at >= cutoff,
            )
            .order_by(Working.created_at.desc(), Working.working_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not (row.mood or "").strip():
        return None
    return TerrainWeatherOut(word=row.mood.strip(), at=row.created_at)


async def refresh_terrain_lifecycle(
    db: AsyncSession,
    *,
    user_id: str,
    claim_id: str,
    now: datetime | None = None,
) -> None:
    """Maintain the fade window of one formed terrain feature.

    The kind of a formation is authored by the formation layer, so this never
    rewrites it.  Fading is not a sixth kind: it is the absence of new evidence,
    and it has to be reversible when the person returns to that part of their
    life.  Only ``valid_to`` and the lifecycle history move here.
    """

    claim = (
        await db.execute(
            select(MemoryClaim).where(
                MemoryClaim.claim_id == claim_id,
                MemoryClaim.user_id == user_id,
                MemoryClaim.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if claim is None or claim.source_type != SourceType.FORMATION.value:
        return
    rows = (
        await db.execute(
            select(MemoryEvidence, UserEvent)
            .join(UserEvent, UserEvent.event_id == MemoryEvidence.event_id)
            .where(
                MemoryEvidence.claim_id == claim_id,
                UserEvent.user_id == user_id,
                UserEvent.status == "active",
                UserEvent.terrain_eligible.is_(True),
            )
        )
    ).all()
    supporting = _supporting(list(rows))
    if not supporting:
        return
    observed = sorted(_parse_time(event.occurred_at) for event in supporting)
    span_days = max(0, (observed[-1].date() - observed[0].date()).days)
    current = now or datetime.now(UTC).replace(microsecond=0)
    faded = current - observed[-1] > timedelta(days=TERRAIN_FADE_DAYS)
    projected_valid_to = (
        _iso(observed[-1] + timedelta(days=TERRAIN_FADE_DAYS)) if faded else None
    )
    if claim.valid_to == projected_valid_to:
        return
    history = _history(claim)
    history.append(
        {
            "at": _iso(current),
            "kind": "fade" if faded else "return",
            "state": _kind_of(claim),
            "reason": (
                "这块地形已经很久没有新的痕迹"
                if faded
                else "这块地形重新出现了新的痕迹"
            ),
            "evidence_count": len(supporting),
            "span_days": span_days,
        }
    )
    claim.valid_to = projected_valid_to
    claim.terrain_last_changed_at = _iso(current)
    claim.terrain_history_json = history
    claim.updated_at = _iso(current)
    claim.version = int(claim.version or 1) + 1


@router.get("", response_model=TerrainListOut)
async def list_terrain(user_id: str = Depends(current_user)):
    settings = get_settings()
    now = datetime.now(UTC).replace(microsecond=0)
    # This endpoint is a projection.  Reading the terrain must not reveal a
    # claim, append history, or change its validity; lifecycle writes happen
    # through explicit actions or the worker.
    async with get_db(read_only=True) as db:
        weather = await _read_weather(db, user_id=user_id, now=now)
        claims = list(
            (
                await db.execute(
                    select(MemoryClaim).where(
                        MemoryClaim.user_id == user_id,
                        MemoryClaim.deleted_at.is_(None),
                        MemoryClaim.user_status.in_(_VISIBLE_STATUSES),
                    )
                )
            ).scalars().all()
        )
        claim_ids = [claim.claim_id for claim in claims]
        rows = []
        if claim_ids:
            rows = (
                await db.execute(
                    select(MemoryEvidence, UserEvent)
                    .join(UserEvent, UserEvent.event_id == MemoryEvidence.event_id)
                    .where(
                        MemoryEvidence.claim_id.in_(claim_ids),
                        UserEvent.user_id == user_id,
                        UserEvent.status == "active",
                        UserEvent.terrain_eligible.is_(True),
                    )
                )
            ).all()

        evidence_by_claim: dict[str, list[tuple[MemoryEvidence, UserEvent]]] = defaultdict(list)
        for evidence, event in rows:
            evidence_by_claim[evidence.claim_id].append((evidence, event))

        items: list[TerrainItemOut] = []
        candidates: list[TerrainCandidateOut] = []
        recent: list[TerrainChangeOut] = []
        withheld_count = 0
        for claim in claims:
            if claim.sensitivity != "normal" or claim.category not in _TERRAIN_CATEGORIES:
                withheld_count += 1
                continue
            bindings = evidence_by_claim.get(claim.claim_id, [])
            supporting = _supporting(bindings)
            if not supporting:
                withheld_count += 1
                continue
            observed = sorted(_parse_time(event.occurred_at) for event in supporting)
            span_days = max(0, (observed[-1].date() - observed[0].date()).days)
            contexts = {event.session_id for event in supporting if event.session_id}
            mature = (
                len(supporting) >= settings.memory_v3_min_evidence
                and span_days >= settings.memory_v3_min_span_days
                and len(contexts) >= settings.memory_v3_min_contexts
            )
            is_formation = claim.source_type == SourceType.FORMATION.value
            if not is_formation or not mature:
                # A claim that only accumulated evidence is a lead, not terrain.
                # Evidence can also be deleted after a formation was written, so
                # a formed feature that fell back below the thresholds returns to
                # being a lead rather than staying on the map.
                if mature:
                    candidates.append(
                        TerrainCandidateOut(
                            claim_id=claim.claim_id,
                            title=claim.claim_text,
                            evidence_count=len(supporting),
                            span_days=span_days,
                            context_count=len(contexts),
                            first_seen_at=_iso(observed[0]),
                            last_seen_at=_iso(observed[-1]),
                            user_status=claim.user_status,
                            name_options=_name_options(claim, kind=_kind_of(claim)),
                        )
                    )
                withheld_count += 1
                continue

            kind = _kind_of(claim)
            faded = now - observed[-1] > timedelta(days=TERRAIN_FADE_DAYS)
            projected_valid_to = claim.valid_to
            if faded and not projected_valid_to:
                projected_valid_to = _iso(observed[-1] + timedelta(days=TERRAIN_FADE_DAYS))
            history = _history(claim)
            changes = _change_output(history)
            item = TerrainItemOut(
                claim_id=claim.claim_id,
                terrain_type=_KIND_LABELS[kind],
                title=claim.terrain_user_label or claim.claim_text,
                maturity=_maturity(
                    claim,
                    evidence_count=len(supporting),
                    span_days=span_days,
                    faded=faded,
                ),
                user_status=claim.user_status,
                evidence_count=len(supporting),
                span_days=span_days,
                context_count=len(contexts),
                first_seen_at=_iso(observed[0]),
                last_seen_at=_iso(observed[-1]),
                allow_proactive=bool(claim.allow_proactive),
                evidence_at=[_iso(moment) for moment in observed],
                why_now=_why_now(history),
                state=_FADED_STATE if faded else kind,
                user_label=claim.terrain_user_label,
                first_revealed_at=claim.terrain_first_revealed_at,
                valid_to=projected_valid_to,
                is_first_reveal=claim.terrain_first_revealed_at is None,
                name_options=_name_options(claim, kind=kind),
                recent_changes=changes,
            )
            items.append(item)
            recent.extend(changes[-1:])

    items.sort(key=lambda item: (item.last_seen_at, item.claim_id), reverse=True)
    candidates.sort(key=lambda item: (item.last_seen_at, item.claim_id), reverse=True)
    return TerrainListOut(
        items=items,
        candidates=candidates,
        recent_changes=sorted(recent, key=lambda row: row.at, reverse=True)[:8],
        withheld_count=withheld_count,
        thresholds=TerrainThresholds(
            min_evidence=settings.memory_v3_min_evidence,
            min_span_days=settings.memory_v3_min_span_days,
            min_contexts=settings.memory_v3_min_contexts,
        ),
        weather=weather,
    )


@router.delete("/weather")
async def disperse_weather(
    user_id: str = Depends(current_user),
    mute: bool = False,
):
    """Let the current weather disperse (baseline §4: 此刻天气 is user-controlled).

    Clearing forgets the reading, not the utterance it was read from: only the
    derived ``mood`` is nulled, so nothing the user actually said is lost.  With
    ``mute=true`` the product stops reading weather at all until re-enabled.
    """
    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
        rows = (
            await db.execute(
                select(Working).where(
                    Working.user_id == user_id,
                    Working.mood.is_not(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.mood = None
        if mute:
            user = (
                await db.execute(select(User).where(User.user_id == user_id))
            ).scalar_one_or_none()
            if user is not None:
                merged = dict(user.settings_json or {})
                merged["weather_muted"] = True
                merged["weather_muted_at"] = now
                user.settings_json = merged
    return {"ok": True, "dispersed": len(rows), "muted": mute}


@router.post("/{claim_id}/name", response_model=TerrainItemOut)
async def name_terrain(
    claim_id: str,
    body: TerrainNamePatch,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    now = _utcnow_iso()
    async with get_db(read_only=False) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("NOT_FOUND", "这块地形不存在", 404)
        claim.terrain_user_label = body.name.strip()
        claim.version = int(claim.version or 1) + 1
        claim.updated_at = now
        history = _history(claim)
        history.append(
            {
                "at": now,
                "kind": "rename",
                "state": _kind_of(claim),
                "reason": "用户共同命名",
                "evidence_count": int(claim.evidence_count or 0),
                "span_days": 0,
                "request_id": req_id,
            }
        )
        claim.terrain_history_json = history
    # Return the canonical projection, including current evidence statistics.
    payload = await list_terrain(user_id)
    for item in payload.items:
        if item.claim_id == claim_id:
            return item
    raise ApiError("NOT_FOUND", "这块地形暂时还没有达到展示门槛", 404)


@router.post("/{claim_id}/reveal")
async def reveal_terrain(
    claim_id: str,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    # A reveal is a user-visible lifecycle event, so a lead or one-off claim
    # cannot be forced into the terrain just by guessing its id.
    projection = await list_terrain(user_id)
    item = next((row for row in projection.items if row.claim_id == claim_id), None)
    if item is None:
        raise ApiError("NOT_READY", "这条线索还没有达到首次揭示的门槛", 409)
    async with get_db(read_only=False) as db:
        claim = await get_claim_for_user(db, user_id=user_id, claim_id=claim_id)
        if claim is None:
            raise ApiError("NOT_FOUND", "这块地形不存在", 404)
        if claim.terrain_first_revealed_at is None:
            now = _utcnow_iso()
            claim.terrain_first_revealed_at = now
            claim.terrain_last_changed_at = now
            claim.updated_at = now
            claim.version = int(claim.version or 1) + 1
            history = _history(claim)
            history.append(
                {
                    "at": now,
                    "kind": "reveal",
                    "state": item.state,
                    "reason": "用户主动确认首次揭示",
                    "evidence_count": item.evidence_count,
                    "span_days": item.span_days,
                    "request_id": req_id,
                }
            )
            claim.terrain_history_json = history
    return {"ok": True, "claim_id": claim_id, "revealed": True, "state": item.state}


__all__ = [
    "TERRAIN_FADE_DAYS",
    "refresh_terrain_lifecycle",
    "router",
]
