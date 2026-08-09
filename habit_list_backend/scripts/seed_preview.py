"""Seed a realistic, self-contained local preview dataset.

This utility is intentionally development-only.  Every row is tagged with
``SEED_KEY`` so ``--reset`` can rebuild the preview without touching records
created by the user or by the running app.

Run from ``habit_list_backend``:

    ..\\.conda\\python.exe -m scripts.seed_preview --reset

The script does not call the model provider and does not enqueue worker jobs;
the resulting state is deterministic and immediately visible in the local UI.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.database import get_db, init_db
from app.db.memory_models import MemoryClaim, MemoryEvidence, UserEvent
from app.db.models import Episodic, Memo, MomentInteraction, RawLedger

SEED_KEY = "inner-terrain-preview-v1"
SEED_POLICY_VERSION = SEED_KEY
USER_ID = "01920000-0000-0000-0000-000000000001"


def _id(kind: str, name: str) -> str:
    """Return a stable UUID for one preview entity."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{SEED_KEY}:{kind}:{name}"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(days_ago: int, hour: int = 12, minute: int = 0) -> str:
    now = datetime.now(UTC).replace(microsecond=0)
    value = now - timedelta(days=days_ago)
    value = value.replace(hour=hour, minute=minute, second=0)
    return value.isoformat().replace("+00:00", "Z")


def _has_seed(value: Any) -> bool:
    return isinstance(value, dict) and value.get("seed_key") == SEED_KEY


def _seeded(rows: Iterable[Any], attr: str) -> list[Any]:
    result: list[Any] = []
    for row in rows:
        if _has_seed(getattr(row, attr, None)):
            result.append(row)
    return result


async def _remove_previous_preview() -> int:
    """Delete only rows owned by this preview seed."""

    removed = 0
    async with get_db(read_only=False) as db:
        episodics = list(
            (
                await db.execute(
                    select(Episodic).where(Episodic.user_id == USER_ID)
                )
            ).scalars().all()
        )
        seeded_episodics = _seeded(episodics, "media_json")
        seeded_moment_ids = {row.episodic_id for row in seeded_episodics}

        interactions = list(
            (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.user_id == USER_ID
                    )
                )
            ).scalars().all()
        )
        seeded_interactions = _seeded(interactions, "metadata_json")
        seeded_interaction_ids = {row.interaction_id for row in seeded_interactions}

        events = list(
            (
                await db.execute(
                    select(UserEvent).where(UserEvent.user_id == USER_ID)
                )
            ).scalars().all()
        )
        seeded_events = _seeded(events, "metadata_json")
        seeded_event_ids = {row.event_id for row in seeded_events}

        claims = list(
            (
                await db.execute(
                    select(MemoryClaim).where(MemoryClaim.user_id == USER_ID)
                )
            ).scalars().all()
        )
        seeded_claims = [
            row
            for row in claims
            if row.created_by_policy_version == SEED_POLICY_VERSION
        ]
        seeded_claim_ids = {row.claim_id for row in seeded_claims}

        evidence = list((await db.execute(select(MemoryEvidence))).scalars().all())
        seeded_evidence = [
            row
            for row in evidence
            if row.claim_id in seeded_claim_ids or row.event_id in seeded_event_ids
        ]

        memos = list(
            (
                await db.execute(select(Memo).where(Memo.user_id == USER_ID))
            ).scalars().all()
        )
        seeded_memos = _seeded(memos, "detect_meta_json")
        seeded_memo_ids = {row.memo_id for row in seeded_memos}

        ledgers = list(
            (
                await db.execute(
                    select(RawLedger).where(RawLedger.user_id == USER_ID)
                )
            ).scalars().all()
        )
        seeded_ledgers = _seeded(ledgers, "payload_json")

        # A previous preview visit may have acknowledged/dismissed the seeded
        # echo in the user's settings.  Remove only those deterministic IDs so
        # a fresh ``--reset`` can show the same preview again without erasing
        # real feedback or unrelated echo history.
        settings_row = (
            await db.execute(
                text("SELECT settings_json FROM users WHERE user_id = :user_id"),
                {"user_id": USER_ID},
            )
        ).scalar_one_or_none()
        if settings_row is not None:
            try:
                settings = (
                    json.loads(settings_row)
                    if isinstance(settings_row, str)
                    else dict(settings_row or {})
                )
            except (TypeError, ValueError):
                settings = {}
            changed_settings = False
            for key in ("echo_handoff_ids", "echo_dismissed_ids"):
                values = list(settings.get(key) or [])
                filtered = [
                    value for value in values if str(value) not in seeded_interaction_ids
                ]
                if filtered != values:
                    changed_settings = True
                    if filtered:
                        settings[key] = filtered[-50:]
                    else:
                        settings.pop(key, None)
            visit_state = settings.get("echo_visit_state")
            if isinstance(visit_state, dict) and (
                str(visit_state.get("last_anchor_id") or "") in seeded_moment_ids
                or str(visit_state.get("visit_id") or "") == _id("visit", "walk-4")
            ):
                settings.pop("echo_visit_state", None)
                changed_settings = True
            if changed_settings:
                await db.execute(
                    text(
                        "UPDATE users SET settings_json = :settings_json "
                        "WHERE user_id = :user_id"
                    ),
                    {"settings_json": json.dumps(settings, ensure_ascii=False), "user_id": USER_ID},
                )

        # Delete dependent rows first.  Core DELETE statements keep the
        # operation deterministic and avoid ORM row-count warnings when a
        # parent row's SQLite cascade has already removed a child.
        if seeded_evidence:
            await db.execute(
                delete(MemoryEvidence).where(
                    MemoryEvidence.evidence_id.in_(
                        [row.evidence_id for row in seeded_evidence]
                    )
                )
            )
        if seeded_interactions:
            await db.execute(
                delete(MomentInteraction).where(
                    MomentInteraction.interaction_id.in_(
                        [row.interaction_id for row in seeded_interactions]
                    )
                )
            )
        if seeded_claims:
            await db.execute(
                delete(MemoryClaim).where(
                    MemoryClaim.claim_id.in_([row.claim_id for row in seeded_claims])
                )
            )
        if seeded_events:
            await db.execute(
                delete(UserEvent).where(
                    UserEvent.event_id.in_([row.event_id for row in seeded_events])
                )
            )
        if seeded_memos:
            await db.execute(
                delete(Memo).where(Memo.memo_id.in_([row.memo_id for row in seeded_memos]))
            )
        if seeded_episodics:
            await db.execute(
                delete(Episodic).where(
                    Episodic.episodic_id.in_([row.episodic_id for row in seeded_episodics])
                )
            )
        if seeded_ledgers:
            await db.execute(
                delete(RawLedger).where(
                    RawLedger.ledger_id.in_([row.ledger_id for row in seeded_ledgers])
                )
            )
        await db.flush()

        removed = (
            len(seeded_evidence)
            + len(seeded_interactions)
            + len(seeded_claims)
            + len(seeded_events)
            + len(seeded_memos)
            + len(seeded_episodics)
            + len(seeded_ledgers)
        )
        # ``seeded_moment_ids`` and ``seeded_memo_ids`` are deliberately kept
        # above as named sets: they make the ownership boundary obvious while
        # reviewing this destructive development-only operation.
        _ = seeded_moment_ids, seeded_memo_ids
    return removed


def _moment_rows() -> tuple[list[Episodic], list[RawLedger], dict[str, str]]:
    """Build life fragments and their append-only ledger entries."""

    specs = [
        (
            "walk-1",
            28,
            8,
            "雨停后我在楼下走了一圈，心里慢慢安静下来。",
            True,
            True,
            True,
        ),
        (
            "walk-2",
            20,
            19,
            "事情很多，我还是绕去河边走了十分钟。",
            True,
            True,
            True,
        ),
        (
            "walk-3",
            11,
            7,
            "今天没有力气解决什么，只想沿着河边走一小段。",
            True,
            True,
            True,
        ),
        (
            "walk-4",
            3,
            21,
            "散步回来后，我比出门前安静了一点。",
            True,
            True,
            True,
        ),
        (
            "window-light",
            1,
            18,
            "只想把傍晚窗边的光留下来，不需要回应。",
            False,
            False,
            False,
        ),
        (
            "balcony",
            0,
            7,
            "把阳台收拾出一个放书的角落，终于有地方坐下来了。",
            False,
            False,
            True,
        ),
    ]
    moments: list[Episodic] = []
    ledgers: list[RawLedger] = []
    ids: dict[str, str] = {}
    for name, days_ago, hour, text, use_terrain, allow_echo, allow_response in specs:
        created_at = _timestamp(days_ago, hour)
        ledger_id = _id("ledger", name)
        moment_id = _id("moment", name)
        ids[name] = moment_id
        ledger = RawLedger(
            ledger_id=ledger_id,
            user_id=USER_ID,
            created_at=created_at,
            entry_type="moment_explicit",
            session_id=f"{SEED_KEY}:{name}",
            payload_json={
                "seed_key": SEED_KEY,
                "text": text,
                "use_for_terrain": use_terrain,
                "allow_proactive": allow_echo,
                "allow_response": allow_response,
            },
            trace_json={"seed_key": SEED_KEY},
        )
        ledgers.append(ledger)
        moments.append(
            Episodic(
                episodic_id=moment_id,
                user_id=USER_ID,
                created_at=created_at,
                source="moment_explicit",
                kind="life_fragment",
                summary_1line=text[:120],
                emotion="-",
                entities_json=[],
                raw_user_text=text,
                raw_assistant_text=None,
                media_json={
                    "seed_key": SEED_KEY,
                    "use_for_terrain": use_terrain,
                    "allow_proactive": allow_echo,
                    "allow_response": allow_response,
                    "save_only": not allow_response,
                },
                ref_ledger_ids_json=[ledger_id],
            )
        )
    return moments, ledgers, ids


def _interaction_rows(ids: dict[str, str]) -> list[MomentInteraction]:
    return [
        MomentInteraction(
            interaction_id=_id("interaction", "walk-1-response"),
            moment_id=ids["walk-1"],
            user_id=USER_ID,
            actor="assistant",
            kind="comment",
            content="你给自己留了一小段安静，我看见了。",
            reaction="seen",
            metadata_json={
                "seed_key": SEED_KEY,
                "trigger_type": "initial",
                "policy_version": "preview",
            },
            created_at=_timestamp(28, 8, 2),
        ),
        MomentInteraction(
            interaction_id=_id("interaction", "balcony-response"),
            moment_id=ids["balcony"],
            user_id=USER_ID,
            actor="assistant",
            kind="comment",
            content="这个角落像是你给今天留的一把椅子。",
            reaction="paused",
            metadata_json={
                "seed_key": SEED_KEY,
                "trigger_type": "initial",
                "policy_version": "preview",
            },
            created_at=_timestamp(0, 7, 4),
        ),
        MomentInteraction(
            interaction_id=_id("interaction", "walk-4-echo"),
            moment_id=ids["walk-4"],
            user_id=USER_ID,
            actor="assistant",
            kind="echo",
            content="你今天又走了一小段。三周前的那场雨，也许还在这一步里。",
            reaction="echo",
            metadata_json={
                "seed_key": SEED_KEY,
                "trigger_type": "revisit",
                "visit_id": _id("visit", "walk-4"),
                "source_moment_ids": [ids["walk-1"], ids["walk-2"]],
                "why_now": "最近这几次留下的生活，都回到了走路之后的安静。",
                "policy_version": "preview",
            },
            created_at=_timestamp(0, 8, 30),
        ),
    ]


def _terrain_rows(ids: dict[str, str]) -> tuple[MemoryClaim, MemoryClaim, list[UserEvent], list[MemoryEvidence]]:
    claim = MemoryClaim(
        claim_id=_id("claim", "walking-calm"),
        user_id=USER_ID,
        claim_type="semantic",
        category="habit",
        subject="self",
        predicate="returns_to",
        object_value="散步后更容易安静下来",
        claim_text="你似乎反复在散步后重新安静下来",
        slot_key="habit:walking-calm",
        content_hash=_hash(f"{SEED_KEY}:walking-calm"),
        source_type="system_inferred",
        confidence=0.78,
        user_status="proposed",
        sensitivity="normal",
        observed_at=_timestamp(28, 8),
        evidence_count=4,
        allow_proactive=False,
        importance=0.68,
        created_by_policy_version=SEED_POLICY_VERSION,
        created_at=_timestamp(3, 21),
        updated_at=_timestamp(0, 21),
    )
    forming = MemoryClaim(
        claim_id=_id("claim", "quiet-space"),
        user_id=USER_ID,
        claim_type="semantic",
        category="cycle",
        subject="self",
        predicate="needs",
        object_value="一个可以坐下来的小空间",
        claim_text="你可能在忙乱时先整理出一个可以坐下来的小空间",
        slot_key="cycle:quiet-space",
        content_hash=_hash(f"{SEED_KEY}:quiet-space"),
        source_type="system_inferred",
        confidence=0.61,
        user_status="proposed",
        sensitivity="normal",
        observed_at=_timestamp(20, 19),
        evidence_count=2,
        allow_proactive=False,
        importance=0.44,
        created_by_policy_version=SEED_POLICY_VERSION,
        created_at=_timestamp(1, 18),
        updated_at=_timestamp(0, 18),
    )
    event_specs = [
        ("walk-1", 28, "preview-morning", "雨停后我走了一圈，慢慢安静下来。"),
        ("walk-2", 20, "preview-evening", "事情很多，但河边走了十分钟后安静了一点。"),
        ("walk-3", 11, "preview-weekend", "沿着河边走一小段，脑子没有那么吵了。"),
        ("walk-4", 3, "preview-evening", "散步回来后，我比出门前安静了一点。"),
    ]
    events: list[UserEvent] = []
    evidence: list[MemoryEvidence] = []
    for index, (moment_name, days_ago, session_id, text) in enumerate(event_specs):
        event_id = _id("event", moment_name)
        event = UserEvent(
            event_id=event_id,
            user_id=USER_ID,
            session_id=session_id,
            request_id=f"{SEED_KEY}:{moment_name}",
            client_event_id=f"{SEED_KEY}:{moment_name}",
            source="moment",
            mode="moment",
            content=text,
            content_hash=_hash(f"{SEED_KEY}:event:{moment_name}"),
            occurred_at=_timestamp(days_ago, 8 if index % 2 == 0 else 19),
            recorded_at=_timestamp(days_ago, 8 if index % 2 == 0 else 19),
            sensitivity="normal",
            status="active",
            source_ref_id=_id("ledger", moment_name),
            metadata_json={
                "seed_key": SEED_KEY,
                "moment_id": ids[moment_name],
            },
        )
        events.append(event)
        evidence.append(
            MemoryEvidence(
                evidence_id=_id("evidence", f"walking-calm-{moment_name}"),
                claim_id=claim.claim_id,
                event_id=event_id,
                evidence_role="supports",
                excerpt_start=0,
                excerpt_end=len(text),
                excerpt_text=text,
                source_weight=1.0,
                extractor_version=SEED_POLICY_VERSION,
            )
        )
    evidence.extend(
        [
            MemoryEvidence(
                evidence_id=_id("evidence", "quiet-space-walk-2"),
                claim_id=forming.claim_id,
                event_id=_id("event", "walk-2"),
                evidence_role="supports",
                excerpt_start=0,
                excerpt_end=21,
                excerpt_text="事情很多，但河边走了十分钟后安静了一点。",
                source_weight=1.0,
                extractor_version=SEED_POLICY_VERSION,
            ),
            MemoryEvidence(
                evidence_id=_id("evidence", "quiet-space-balcony"),
                claim_id=forming.claim_id,
                event_id=_id("event", "walk-4"),
                evidence_role="supports",
                excerpt_start=0,
                excerpt_end=20,
                excerpt_text="散步回来后，我比出门前安静了一点。",
                source_weight=1.0,
                extractor_version=SEED_POLICY_VERSION,
            ),
        ]
    )
    return claim, forming, events, evidence


def _memo_rows() -> tuple[list[Memo], list[Episodic]]:
    specs = [
        ("call-family", "周五前给家里回电话", "周五", 3, "red", "pending"),
        ("buy-lamp", "买一盏暖灯放在窗边", "这周末", 6, "yellow", "pending"),
        ("tidy-desk", "整理书桌", "昨天", -1, "green", "done"),
    ]
    memos: list[Memo] = []
    episodics: list[Episodic] = []
    for name, text, due_text, offset, importance, status in specs:
        memo_id = _id("memo", name)
        episodic_id = _id("memo-episodic", name)
        created_at = _timestamp(1 if status == "done" else max(offset, 0), 14)
        memos.append(
            Memo(
                memo_id=memo_id,
                user_id=USER_ID,
                text=text,
                clean_text=text,
                due_text=due_text,
                due_iso=_timestamp(0, 18) if offset == 0 else None,
                due_offset_days=offset,
                importance=importance,
                source="memo_page_manual",
                status=status,
                status_changed_at=_timestamp(0, 12) if status == "done" else None,
                created_at=created_at,
                linked_episodic_id=episodic_id,
                detect_meta_json={"seed_key": SEED_KEY},
            )
        )
        episodics.append(
            Episodic(
                episodic_id=episodic_id,
                user_id=USER_ID,
                created_at=created_at,
                source="memo_page",
                kind="memo",
                summary_1line=text[:120],
                emotion="-",
                entities_json=[],
                raw_user_text=text,
                raw_assistant_text=None,
                media_json={"seed_key": SEED_KEY},
                ref_ledger_ids_json=[],
            )
        )
    return memos, episodics


async def seed_preview(*, reset: bool) -> dict[str, int]:
    settings = get_settings()
    if not settings.is_dev or not settings.database_is_sqlite:
        raise RuntimeError("seed_preview 仅允许在 dev + SQLite 本地库执行")
    if settings.default_user_id != USER_ID:
        raise RuntimeError("当前 DEFAULT_USER_ID 不是本地预览用户，已停止写入")

    try:
        await init_db(settings)
    except OperationalError as exc:
        # The running local preview may point at the pre-identity SQLite
        # schema.  Its content tables are compatible with this seed, but the
        # startup bootstrap still expects the newer ``users.updated_at``
        # column.  Keep the existing database intact and continue only for
        # that known compatibility case; all other schema failures remain
        # fatal.
        if "users" not in str(exc) or "updated_at" not in str(exc):
            raise
        print("Using existing compatible local schema; startup bootstrap skipped")
    removed = await _remove_previous_preview() if reset else 0

    moments, ledgers, ids = _moment_rows()
    interactions = _interaction_rows(ids)
    mature_claim, forming_claim, events, evidence = _terrain_rows(ids)
    memos, memo_episodics = _memo_rows()

    async with get_db(read_only=False) as db:
        db.add_all(ledgers)
        db.add_all(moments)
        db.add_all(memo_episodics)
        db.add_all(memos)
        await db.flush()
        db.add_all(interactions)
        await db.flush()
        db.add_all([mature_claim, forming_claim])
        db.add_all(events)
        await db.flush()
        db.add_all(evidence)
        await db.flush()
    return {
        "removed": removed,
        "moments": len(moments),
        "interactions": len(interactions),
        "terrain_claims": 2,
        "terrain_evidence": len(evidence),
        "memos": len(memos),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="删除此前由本脚本写入的演示数据后再重建",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(seed_preview(reset=args.reset))
    print("Inner Terrain preview seed ready")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
