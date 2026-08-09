"""Explainable response gate for life-fragment interactions.

The gate decides whether an automatic agent reaction may happen for one newly
saved fragment.  It replaces the old plain fragment counter with explicit,
auditable signals: response density, repeated content, user feedback throttle,
echo frequency budget, and user-owned suppression entries.

The gate only *routes*.  It never defines the user, never creates memory
objects, and every denial reason is machine readable so behavior changes can
be traced back to a concrete policy signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Episodic, MomentInteraction

MOMENT_POLICY_VERSION = "moment-witness-gate-v2"

# At most one automatic interaction across this many fragments (base budget).
OCCASIONAL_BASE_WINDOW = 3
# Each "少这样回应" feedback widens the density window by this many fragments.
THROTTLE_WINDOW_STEP = 3
THROTTLE_MAX_LEVEL = 2
# Proactive echoes are limited to one per rolling window.
ECHO_WINDOW_HOURS = 24
# A source that was already cited should not be re-cited within this period.
SOURCE_COOLDOWN_DAYS = 14
# Fragments compared for naive repetition detection.
DUPLICATE_LOOKBACK = 6
DUPLICATE_SIMILARITY_THRESHOLD = 0.85


class GateDecision(BaseModel):
    """One explainable routing decision for an automatic fragment response."""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    policy_version: str = MOMENT_POLICY_VERSION


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def throttle_level(settings_json: dict[str, Any] | None) -> int:
    try:
        level = int((settings_json or {}).get("life_reply_throttle_level") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(level, THROTTLE_MAX_LEVEL))


def occasional_window(settings_json: dict[str, Any] | None) -> int:
    return OCCASIONAL_BASE_WINDOW + throttle_level(settings_json) * THROTTLE_WINDOW_STEP


# ---------------------------------------------------------------------------
# Suppressions: user feedback entries stored in ``User.settings_json``.
# Shape: {"echo_suppressions": [{"type": "source"|"theme", "value": str,
#                                "created_at": iso}]}
# ---------------------------------------------------------------------------
def load_suppressions(settings_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (settings_json or {}).get("echo_suppressions") or []
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type") or "")
            value = str(entry.get("value") or "").strip()
            if entry_type in {"source", "theme"} and value:
                items.append({"type": entry_type, "value": value, "created_at": str(entry.get("created_at") or "")})
    return items


def suppressed_source_ids(settings_json: dict[str, Any] | None) -> set[str]:
    return {
        entry["value"]
        for entry in load_suppressions(settings_json)
        if entry["type"] == "source"
    }


def suppressed_theme_keywords(settings_json: dict[str, Any] | None) -> list[str]:
    return [
        entry["value"]
        for entry in load_suppressions(settings_json)
        if entry["type"] == "theme"
    ]


def text_hits_suppression(text: str, keywords: list[str]) -> str | None:
    """Return the first suppressed keyword contained in ``text``, if any."""

    normalized = (text or "").strip()
    if not normalized:
        return None
    for keyword in keywords:
        if keyword and keyword in normalized:
            return keyword
    return None


def append_suppression(
    settings_json: dict[str, Any] | None,
    *,
    entry_type: str,
    value: str,
    created_at: str,
) -> dict[str, Any]:
    """Return a new settings dict with the suppression appended (deduplicated)."""

    merged = dict(settings_json or {})
    existing = [
        entry
        for entry in load_suppressions(merged)
        if not (entry["type"] == entry_type and entry["value"] == value)
    ]
    existing.append({"type": entry_type, "value": value, "created_at": created_at})
    merged["echo_suppressions"] = existing[-64:]
    return merged


def feedback_rules(settings_json: dict[str, Any] | None) -> dict[str, list[str]]:
    """Return the small, user-owned behavior adjustments learned from feedback.

    This is intentionally stored as policy data rather than as a model prompt
    hint.  It lets the gate and the worker enforce a correction even when the
    model is unavailable or a later prompt changes.
    """

    raw = (settings_json or {}).get("moment_feedback_rules") or {}
    if not isinstance(raw, dict):
        return {"kinds": [], "reactions": []}
    result: dict[str, list[str]] = {"kinds": [], "reactions": []}
    for key in result:
        values = raw.get(key)
        if isinstance(values, list):
            result[key] = [str(value) for value in values if str(value).strip()]
    return result


def append_feedback_rule(
    settings_json: dict[str, Any] | None,
    *,
    kind: str | None,
    reaction: str | None,
    created_at: str,
) -> dict[str, Any]:
    """Persist a bounded correction for future response shape selection."""

    merged = dict(settings_json or {})
    rules = feedback_rules(merged)
    if kind and kind not in rules["kinds"]:
        rules["kinds"].append(kind)
    if reaction and reaction not in rules["reactions"]:
        rules["reactions"].append(reaction)
    merged["moment_feedback_rules"] = {
        "kinds": rules["kinds"][-12:],
        "reactions": rules["reactions"][-12:],
        "updated_at": created_at,
    }
    return merged


REWRITE_PREFERENCE_MAX = 6


def append_rewrite_preference(
    settings_json: dict[str, Any] | None,
    *,
    text: str,
    created_at: str,
) -> dict[str, Any]:
    """Record a user-kept rewrite as a preferred expression sample.

    The rewrite is stored as bounded, deduplicated policy data so the
    generation path can echo the user's preferred tone without hard-coding it
    into a prompt.  It is a style signal, never a factual conclusion about the
    user.
    """

    merged = dict(settings_json or {})
    raw_prefs = merged.get("moment_rewrite_preferences") or {}
    examples = list(
        (raw_prefs.get("examples") or [])
        if isinstance(raw_prefs, dict)
        else []
    )
    cleaned = str(text).strip()
    if not cleaned:
        return merged
    existing_texts = {
        str(entry.get("text") or "").strip()
        for entry in examples
        if isinstance(entry, dict)
    }
    if cleaned not in existing_texts:
        examples.append({"text": cleaned[:200], "created_at": created_at})
    merged["moment_rewrite_preferences"] = {
        "examples": examples[-REWRITE_PREFERENCE_MAX:],
        "updated_at": created_at,
    }
    return merged


def load_rewrite_preferences(
    settings_json: dict[str, Any] | None, *, limit: int = 3
) -> list[str]:
    """Return recent user-kept rewrite texts for the generation prompt."""

    raw = (settings_json or {}).get("moment_rewrite_preferences") or {}
    examples = raw.get("examples") if isinstance(raw, dict) else None
    if not isinstance(examples, list):
        return []
    texts: list[str] = []
    for entry in reversed(examples):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def extract_theme_keywords(text: str, *, limit: int = 2) -> list[str]:
    """Pick concrete content words from a fragment for category suppression."""

    normalized = " ".join((text or "").split())
    if not normalized:
        return []
    try:
        import jieba.analyse as analyse

        words = analyse.extract_tags(normalized, topK=limit * 3)
    except Exception:  # noqa: BLE001 - jieba failure must not break feedback
        words = []
    if not words:
        words = [token for token in normalized.replace("，", " ").split() if token]
    picked: list[str] = []
    for word in words:
        word = str(word).strip()
        if len(word) >= 2 and word not in picked:
            picked.append(word)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Signal queries
# ---------------------------------------------------------------------------
def _normalize_text(value: str) -> str:
    return "".join("".join(value.split()).lower())


def _bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[i : i + 2] for i in range(len(value) - 1)}


def _similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def _recent_fragments(
    db: AsyncSession,
    *,
    user_id: str,
    exclude_id: str,
    limit: int,
) -> list[Episodic]:
    return list(
        (
            await db.execute(
                select(Episodic)
                .where(
                    Episodic.user_id == user_id,
                    Episodic.kind == "life_fragment",
                    Episodic.status == "active",
                    Episodic.episodic_id != exclude_id,
                )
                .order_by(Episodic.created_at.desc(), Episodic.episodic_id.desc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def evaluate_response_gate(
    db: AsyncSession,
    *,
    user_id: str,
    moment: Episodic,
    response_mode: str,
    trigger_type: str,
    settings_json: dict[str, Any] | None,
) -> GateDecision:
    """Decide whether an automatic response may be generated for ``moment``.

    A user-initiated reply always passes: the user is speaking, the agent must
    answer.  Automatic ("initial") triggers walk the restraint signals in order
    and return every reason that fired, so silence stays explainable.
    """

    if trigger_type == "user_reply":
        return GateDecision(allowed=True, reasons=["user_initiated"])

    # A user correction is a hard policy input.  It must be evaluated before
    # the `always` shortcut, otherwise the UI would claim a correction was
    # learned while the next automatic response ignored it.
    blocked_keyword = text_hits_suppression(
        moment.raw_user_text or "", suppressed_theme_keywords(settings_json)
    )
    if blocked_keyword:
        return GateDecision(
            allowed=False,
            reasons=[f"feedback_theme_suppressed:{blocked_keyword}"],
        )
    if response_mode == "silent":
        return GateDecision(allowed=False, reasons=["mode_silent"])
    if response_mode == "always":
        return GateDecision(allowed=True, reasons=["mode_always"])

    reasons: list[str] = []

    # Signal 1: density budget across the most recent fragments.
    window = occasional_window(settings_json)
    previous_ids = [
        item.episodic_id
        for item in await _recent_fragments(
            db, user_id=user_id, exclude_id=moment.episodic_id, limit=window - 1
        )
    ]
    if previous_ids:
        recent_auto = (
            await db.execute(
                select(MomentInteraction.interaction_id)
                .where(
                    MomentInteraction.moment_id.in_(previous_ids),
                    MomentInteraction.actor == "assistant",
                    MomentInteraction.status == "active",
                )
                .limit(20)
            )
        ).scalars().all()
        # Interactions created by an automatic trigger carry trigger_type=initial.
        if recent_auto:
            rows = (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.interaction_id.in_(recent_auto)
                    )
                )
            ).scalars().all()
            if any(
                (row.metadata_json or {}).get("trigger_type") == "initial"
                for row in rows
            ):
                reasons.append("density_budget")

    # Signal 2: repeated or near-duplicate content should stay quiet.
    current_text = _normalize_text(moment.raw_user_text or "")
    if current_text:
        for previous in await _recent_fragments(
            db, user_id=user_id, exclude_id=moment.episodic_id, limit=DUPLICATE_LOOKBACK
        ):
            previous_text = _normalize_text(previous.raw_user_text or "")
            if not previous_text:
                continue
            if previous_text == current_text or _similarity(
                current_text, previous_text
            ) >= DUPLICATE_SIMILARITY_THRESHOLD:
                reasons.append("repeated_content")
                break

    # Signal 3: explicit user feedback can demand overall quietness.
    if throttle_level(settings_json) >= THROTTLE_MAX_LEVEL and reasons:
        reasons.append("user_requested_quieter")

    return GateDecision(allowed=not reasons, reasons=reasons or ["occasional_allowed"])


async def echo_budget_available(
    db: AsyncSession,
    *,
    user_id: str,
    window_hours: int = ECHO_WINDOW_HOURS,
) -> bool:
    """At most one proactive echo inside the rolling window."""

    cutoff = (
        datetime.now(UTC) - timedelta(hours=window_hours)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = (
        await db.execute(
            select(MomentInteraction)
            .where(
                MomentInteraction.user_id == user_id,
                MomentInteraction.actor == "assistant",
                MomentInteraction.kind == "echo",
                MomentInteraction.status == "active",
                MomentInteraction.created_at >= cutoff,
            )
            .limit(8)
        )
    ).scalars().all()
    return not bool(rows)


async def recently_used_source_ids(
    db: AsyncSession,
    *,
    user_id: str,
    cooldown_days: int = SOURCE_COOLDOWN_DAYS,
) -> set[str]:
    """Source fragments already cited by an echo inside the cooldown period."""

    cutoff = (
        datetime.now(UTC) - timedelta(days=cooldown_days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = (
        await db.execute(
            select(MomentInteraction)
            .where(
                MomentInteraction.user_id == user_id,
                MomentInteraction.actor == "assistant",
                MomentInteraction.kind == "echo",
                MomentInteraction.created_at >= cutoff,
            )
            .limit(32)
        )
    ).scalars().all()
    used: set[str] = set()
    for row in rows:
        for source_id in (row.metadata_json or {}).get("source_moment_ids") or []:
            if source_id:
                used.add(str(source_id))
    return used


__all__ = [
    "DUPLICATE_LOOKBACK",
    "DUPLICATE_SIMILARITY_THRESHOLD",
    "ECHO_WINDOW_HOURS",
    "GateDecision",
    "MOMENT_POLICY_VERSION",
    "OCCASIONAL_BASE_WINDOW",
    "SOURCE_COOLDOWN_DAYS",
    "THROTTLE_MAX_LEVEL",
    "THROTTLE_WINDOW_STEP",
    "append_suppression",
    "append_feedback_rule",
    "append_rewrite_preference",
    "load_rewrite_preferences",
    "echo_budget_available",
    "evaluate_response_gate",
    "extract_theme_keywords",
    "feedback_rules",
    "load_suppressions",
    "occasional_window",
    "parse_iso",
    "recently_used_source_ids",
    "suppressed_source_ids",
    "suppressed_theme_keywords",
    "text_hits_suppression",
    "throttle_level",
]
