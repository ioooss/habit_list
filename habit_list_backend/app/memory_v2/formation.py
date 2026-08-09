"""Formation layer: cross-event inference over already-grounded evidence.

The extractor answers "what did this person state in this one message".  That
can never produce the product's core promise, which is to surface something the
user *never said in one place* but that several real moments make visible.

This module adds that missing step in two stages:

* Stage 1 clusters existing claims and their evidence using only deterministic
  SQL and vector arithmetic.  It costs nothing per user and, critically, it is
  where the maturity thresholds are enforced.  A cluster that fails the evidence
  count, time span, or context count never reaches a model, so the model is
  structurally unable to assert something the evidence does not support.
* Stage 2 asks a model to name what the cluster shows.  It sees opaque ordinal
  refs instead of ids, cannot supply text or offsets, and its output is rejected
  outright on any contract violation rather than repaired.

Evidence is *inherited*, never regenerated: a formation claim reuses the exact
excerpt offsets that ``reconcile.ground_evidence`` already verified against the
user's original wording.  That keeps V2's strongest guarantee intact without
depending on the model being honest.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.database import get_db
from ..db.memory_models import (
    MemoryClaim,
    MemoryDeletionTombstone,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryRevision,
    UserEvent,
)
from ..db.models import _utcnow_iso
from ..providers import dashscope
from .domain import (
    ClaimType,
    EvidenceRole,
    FormationHypothesis,
    MemoryCategory,
    Sensitivity,
    SourceType,
    TerrainKind,
    UserStatus,
)
from .reconcile import claim_keys_from_fields

log = logging.getLogger("habit_list.memory_v2.formation")

FORMATION_SCAN_REQUESTED = "memory.formation.scan.requested"
FORMATION_TOMBSTONE_TYPE = "memory_formation"

_ACCELERATION_WINDOW_DAYS = 14

# Baseline 4.1 forbids diagnosis, fixed personality labels, moral judgement and
# unfalsifiable "you are just like this" statements.  A hypothesis that reaches
# for any of these is discarded rather than rewritten: a model that produced one
# is not reliably steerable back into the product's voice within one retry.
_BANNED = re.compile(
    r"你总是|你从来|你一向|你就是(?:这样|个)|你这个人|本质上(?:就)?是|"
    r"性格(?:缺陷|问题)|人格(?:障碍|缺陷)|诊断|抑郁症|焦虑症|躁郁|双相|"
    r"强迫症|自闭|人格分裂|精神(?:病|疾病)|病态|你应该|你必须|"
    r"我比你更(?:了解|懂)|只有我(?:懂|理解)"
)

# Terrain projection only accepts these categories, and formation claims are
# cross-category by nature.
_FORMATION_CATEGORY = MemoryCategory.OTHER

_KIND_TO_TERRAIN_STATE = {
    TerrainKind.GROWING: "growing",
    TerrainKind.RECURRING: "recurring",
    TerrainKind.LOOSENING: "loosening",
    TerrainKind.TWO_FORCES: "two_forces",
    TerrainKind.UNNAMED: "unnamed",
}

_ELIGIBLE_MEMBER_STATUSES = (
    UserStatus.CONFIRMED.value,
    UserStatus.CORRECTED.value,
    UserStatus.PROPOSED.value,
    UserStatus.DEFERRED.value,
)

_FORMATION_SYSTEM_PROMPT = """你是「内在地形」的形成层。

你面前是同一个人在不同时间、不同场景留下的若干条原话。你的任务不是复述它们，而是判断：这些原话放在一起，是否显示出这个人身上正在发生某种形成、反复、松动或张力。

硬规则：
1. 只能使用给出的原话。不得引入任何未出现的信息，不得补充常识性推测。
2. 不要引用原文，也不要给出位置。每条证据已有编号，你只需为每条标注角色：
   - supports：支持这个判断
   - contradicts：与判断相反，但同样真实
   - corrects：对更早表述的修正
3. 如果这些原话只是重复同一句话，或只是若干互不相关的事，就选 terrain_kind = "unnamed"，并在 claim_text 里说明这里有信号但还说不清。宁可说不清，不要凑一个结论。
4. claim_text 只描述方向或张力，不描述固定人格。禁止「你总是」「你从来」「你就是」「本质上」，禁止任何诊断、症状、人格标签、道德评价和建议。
5. 使用可修订语言：似乎、最近、在这些情境里、有一部分。
6. why_now 用自然语言说明为什么这几条放在一起、现在值得被看见。不要提证据条数、天数或任何门槛。
7. terrain_kind 选择：
   - growing 正在长出来：新的能力、边界、价值或愿望在出现
   - recurring 反复回到：跨场景持续回到同一个需要或在意
   - loosening 正在松动：过去牢固的模式开始变化
   - two_forces 两股力量：两种同时真实的力量在拉扯。选这个时必须至少标注一条 contradicts
   - unnamed 尚未命名：有信号，但当前证据不足以给出稳定表达
8. 用中文。claim_text 一句话，不超过 60 字。why_now 不超过 60 字。
"""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def formation_fingerprint(*, user_id: str, event_ids: list[str]) -> str:
    """Stable identity for one supporting-evidence set.

    Used only to block resurrection: when a user permanently deletes a formed
    terrain feature, a rescan would otherwise rebuild it from the same evidence
    on the next pass.
    """

    return _sha256(f"{user_id}|" + "|".join(sorted(set(event_ids))))


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass(frozen=True)
class ClusterEvidence:
    """One inherited, already-grounded evidence binding."""

    event_id: str
    claim_id: str
    role: str
    excerpt_start: int
    excerpt_end: int
    excerpt_text: str
    source_weight: float
    occurred_at: str
    session_id: str


@dataclass
class FormationCluster:
    """A group of related claims whose evidence may show a formation."""

    user_id: str
    claim_ids: list[str]
    slot_keys: list[str]
    evidence: list[ClusterEvidence]
    signals: set[str] = field(default_factory=set)

    @property
    def supporting(self) -> list[ClusterEvidence]:
        return [item for item in self.evidence if item.role == EvidenceRole.SUPPORTS.value]

    @property
    def contradicting(self) -> list[ClusterEvidence]:
        return [item for item in self.evidence if item.role == EvidenceRole.CONTRADICTS.value]

    @property
    def supporting_event_ids(self) -> list[str]:
        return sorted({item.event_id for item in self.supporting})

    @property
    def span_days(self) -> int:
        observed = sorted(_parse_time(item.occurred_at) for item in self.supporting)
        if not observed:
            return 0
        return max(0, (observed[-1].date() - observed[0].date()).days)

    @property
    def contexts(self) -> set[str]:
        return {item.session_id for item in self.supporting if item.session_id}

    @property
    def fingerprint(self) -> str:
        return formation_fingerprint(
            user_id=self.user_id, event_ids=self.supporting_event_ids
        )

    @property
    def priority(self) -> tuple[int, int, int]:
        return (len(self.supporting_event_ids), self.span_days, len(self.contexts))


@dataclass
class FormationScanResult:
    clusters_considered: int = 0
    clusters_admitted: int = 0
    hypotheses_requested: int = 0
    hypotheses_discarded: int = 0
    created_claim_ids: list[str] = field(default_factory=list)
    strengthened_claim_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 1: deterministic clustering and admission
# ---------------------------------------------------------------------------


async def _load_member_claims(
    session: AsyncSession, *, user_id: str
) -> list[MemoryClaim]:
    """Claims eligible to be clustered.

    Rejected and hidden claims are excluded here rather than filtered later:
    a user rejection is a durable correction, and a rejected interpretation must
    not be able to re-enter through a broader hypothesis.  Formation claims are
    also excluded so the layer never infers on top of its own inferences.
    """

    rows = (
        await session.execute(
            select(MemoryClaim).where(
                MemoryClaim.user_id == user_id,
                MemoryClaim.deleted_at.is_(None),
                MemoryClaim.user_status.in_(_ELIGIBLE_MEMBER_STATUSES),
                MemoryClaim.sensitivity == Sensitivity.NORMAL.value,
                MemoryClaim.source_type != SourceType.FORMATION.value,
            )
        )
    ).scalars().all()
    return list(rows)


async def _crisis_windows(
    session: AsyncSession, *, user_id: str, settings: Settings
) -> list[tuple[str, datetime, datetime]]:
    """Session windows that formation must not look inside (baseline 8.3).

    What someone says in the hours after a crisis moment is not representative
    material to infer their character from — it is the crisis speaking.  The
    crisis event itself is already ineligible; this excludes what surrounds it.

    Forward-looking only: the window opens at the crisis and closes after
    ``memory_v3_crisis_window_minutes``.  Earlier turns in the same session were
    said before anything happened and stay usable.
    """

    rows = (
        await session.execute(
            select(UserEvent.session_id, UserEvent.occurred_at).where(
                UserEvent.user_id == user_id,
                UserEvent.sensitivity == Sensitivity.CRISIS.value,
                UserEvent.deleted_at.is_(None),
            )
        )
    ).all()
    span = timedelta(minutes=settings.memory_v3_crisis_window_minutes)
    windows: list[tuple[str, datetime, datetime]] = []
    for session_id, occurred_at in rows:
        if not session_id:
            continue
        started = _parse_time(occurred_at)
        windows.append((session_id, started, started + span))
    return windows


async def _load_evidence(
    session: AsyncSession, *, user_id: str, claim_ids: list[str], settings: Settings
) -> dict[str, list[ClusterEvidence]]:
    """Inherit grounded evidence, honouring the terrain permission on the row."""

    if not claim_ids:
        return {}
    windows = await _crisis_windows(session, user_id=user_id, settings=settings)
    rows = (
        await session.execute(
            select(MemoryEvidence, UserEvent)
            .join(UserEvent, UserEvent.event_id == MemoryEvidence.event_id)
            .where(
                MemoryEvidence.claim_id.in_(claim_ids),
                UserEvent.user_id == user_id,
                UserEvent.status == "active",
                UserEvent.deleted_at.is_(None),
                UserEvent.terrain_eligible.is_(True),
            )
        )
    ).all()
    grouped: dict[str, list[ClusterEvidence]] = defaultdict(list)
    for evidence, event in rows:
        if _within_crisis_window(event, windows):
            continue
        grouped[evidence.claim_id].append(
            ClusterEvidence(
                event_id=event.event_id,
                claim_id=evidence.claim_id,
                role=evidence.evidence_role,
                excerpt_start=evidence.excerpt_start,
                excerpt_end=evidence.excerpt_end,
                excerpt_text=evidence.excerpt_text,
                source_weight=float(evidence.source_weight or 1.0),
                occurred_at=event.occurred_at,
                session_id=event.session_id or "",
            )
        )
    return grouped


def _within_crisis_window(
    event: UserEvent, windows: list[tuple[str, datetime, datetime]]
) -> bool:
    if not windows or not event.session_id:
        return False
    occurred = _parse_time(event.occurred_at)
    return any(
        event.session_id == session_id and start <= occurred <= end
        for session_id, start, end in windows
    )


async def _load_vectors(
    session: AsyncSession, *, user_id: str, claim_ids: list[str], settings: Settings
) -> dict[str, list[float]]:
    if not claim_ids or not settings.memory_v2_embedding_enabled:
        return {}
    rows = (
        await session.execute(
            select(MemoryEmbedding).where(
                MemoryEmbedding.user_id == user_id,
                MemoryEmbedding.claim_id.in_(claim_ids),
                MemoryEmbedding.status == "active",
            )
        )
    ).scalars().all()
    return {row.claim_id: list(row.vector_json or []) for row in rows}


def _group_claims(
    claims: list[MemoryClaim], vectors: dict[str, list[float]], threshold: float
) -> list[list[MemoryClaim]]:
    """Greedy union of semantically close claims, plus singletons.

    Without embeddings every claim becomes its own group.  That still yields
    ``recurring`` formations from a single claim observed across time and
    contexts; only the cross-theme ``growing`` signal needs vectors.  Degrading
    to fewer signals is correct behaviour, not an error.
    """

    parent = {claim.claim_id: claim.claim_id for claim in claims}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for index, left in enumerate(claims):
        left_vector = vectors.get(left.claim_id)
        if not left_vector:
            continue
        for right in claims[index + 1 :]:
            right_vector = vectors.get(right.claim_id)
            if not right_vector:
                continue
            # Merging two values of the same slot would just restate one theme;
            # a growing formation needs at least two distinct slots.
            if left.slot_key == right.slot_key:
                continue
            if _cosine(left_vector, right_vector) >= threshold:
                union(left.claim_id, right.claim_id)

    grouped: dict[str, list[MemoryClaim]] = defaultdict(list)
    for claim in claims:
        grouped[find(claim.claim_id)].append(claim)
    return list(grouped.values())


def _detect_signals(cluster: FormationCluster, members: list[MemoryClaim]) -> set[str]:
    signals: set[str] = set()
    if len({claim.slot_key for claim in members}) >= 2:
        signals.add("semantic_cluster")
    if len(cluster.contexts) >= 2 and cluster.span_days >= 7:
        signals.add("recurrence")
    if cluster.contradicting:
        signals.add("conflict")
    if any(claim.supersedes_claim_id or claim.valid_to for claim in members):
        signals.add("temporal_shift")

    observed = sorted(_parse_time(item.occurred_at) for item in cluster.supporting)
    if len(observed) >= 3:
        cutoff = observed[-1] - timedelta(days=_ACCELERATION_WINDOW_DAYS)
        recent = [stamp for stamp in observed if stamp >= cutoff]
        earlier = [stamp for stamp in observed if stamp < cutoff]
        if len(recent) >= 2 and len(recent) > len(earlier):
            signals.add("acceleration")
    if not observed:
        return signals
    if datetime.now(UTC) - observed[-1] > timedelta(days=60):
        signals.add("fading")
    return signals


async def _has_tombstone(session: AsyncSession, fingerprint: str) -> bool:
    row = (
        await session.execute(
            select(MemoryDeletionTombstone.tombstone_id).where(
                MemoryDeletionTombstone.resource_type == FORMATION_TOMBSTONE_TYPE,
                MemoryDeletionTombstone.resource_hash == fingerprint,
            )
        )
    ).first()
    return row is not None


async def build_clusters(
    session: AsyncSession, *, user_id: str, settings: Settings | None = None
) -> tuple[list[FormationCluster], int]:
    """Stage 1.  Return admitted clusters and how many were considered.

    Admission enforces the maturity thresholds *before* any model sees the
    material, which is what makes it structurally impossible for the formation
    layer to assert an under-evidenced conclusion.
    """

    settings = settings or get_settings()
    claims = await _load_member_claims(session, user_id=user_id)
    if not claims:
        return [], 0
    claim_ids = [claim.claim_id for claim in claims]
    evidence = await _load_evidence(
        session, user_id=user_id, claim_ids=claim_ids, settings=settings
    )
    vectors = await _load_vectors(
        session, user_id=user_id, claim_ids=claim_ids, settings=settings
    )
    groups = _group_claims(claims, vectors, settings.memory_v3_cluster_similarity)

    admitted: list[FormationCluster] = []
    for members in groups:
        bindings: list[ClusterEvidence] = []
        for member in members:
            bindings.extend(evidence.get(member.claim_id, []))
        if not bindings:
            continue
        cluster = FormationCluster(
            user_id=user_id,
            claim_ids=sorted(member.claim_id for member in members),
            slot_keys=sorted({member.slot_key for member in members}),
            evidence=bindings,
        )
        if len(cluster.supporting_event_ids) < settings.memory_v3_min_evidence:
            continue
        if cluster.span_days < settings.memory_v3_min_span_days:
            continue
        if len(cluster.contexts) < settings.memory_v3_min_contexts:
            continue
        if await _has_tombstone(session, cluster.fingerprint):
            continue
        cluster.signals = _detect_signals(cluster, members)
        if "fading" in cluster.signals and len(cluster.signals) == 1:
            # A cluster that only stopped producing evidence is a season change,
            # not a new formation.
            continue
        admitted.append(cluster)

    admitted.sort(key=lambda item: item.priority, reverse=True)
    return admitted, len(groups)


# ---------------------------------------------------------------------------
# Stage 2: constrained hypothesis generation
# ---------------------------------------------------------------------------


def _build_prompt_payload(cluster: FormationCluster) -> tuple[str, dict[str, str]]:
    """Render evidence as opaque refs and return the ref -> event_id mapping."""

    ordered = sorted(cluster.supporting, key=lambda item: item.occurred_at)
    ordered += sorted(cluster.contradicting, key=lambda item: item.occurred_at)

    seen: dict[str, str] = {}
    context_labels: dict[str, str] = {}
    lines: list[str] = []
    for item in ordered:
        if item.event_id in seen.values():
            continue
        ref = f"E{len(seen) + 1}"
        seen[ref] = item.event_id
        if item.session_id not in context_labels:
            context_labels[item.session_id] = chr(ord("A") + len(context_labels))
        day = _parse_time(item.occurred_at).date().isoformat()
        lines.append(
            f"{ref}｜{day}｜场景{context_labels[item.session_id]}｜{item.excerpt_text}"
        )

    signal_names = {
        "semantic_cluster": "多个不同主题的表述聚在一起",
        "recurrence": "同一件事跨场景反复出现",
        "conflict": "存在互相矛盾但都真实的表述",
        "temporal_shift": "同一处的表述随时间改变过",
        "acceleration": "最近出现得比以前更密集",
    }
    hints = [signal_names[name] for name in sorted(cluster.signals) if name in signal_names]
    hint_text = "；".join(hints) if hints else "无特别信号"

    prompt = (
        f"这些原话的形式特征：{hint_text}\n"
        f"（形式特征只是提示，不能替代你对内容的判断。）\n\n"
        "证据（编号｜日期｜场景｜原话）：\n" + "\n".join(lines)
    )
    return prompt, seen


def _reject(reason: str, cluster: FormationCluster) -> None:
    log.info(
        "Formation hypothesis discarded reason=%s claims=%d evidence=%d",
        reason,
        len(cluster.claim_ids),
        len(cluster.supporting_event_ids),
    )


def validate_hypothesis(
    hypothesis: FormationHypothesis,
    *,
    cluster: FormationCluster,
    ref_map: dict[str, str],
    settings: Settings,
) -> bool:
    """Reject on any contract violation.  Never repair."""

    labelled = {label.ref for label in hypothesis.evidence_roles}
    if labelled - set(ref_map):
        _reject("unknown_ref", cluster)
        return False
    if set(ref_map) - labelled:
        _reject("incomplete_labelling", cluster)
        return False
    if len({label.ref for label in hypothesis.evidence_roles}) != len(
        hypothesis.evidence_roles
    ):
        _reject("duplicate_ref", cluster)
        return False

    supports = hypothesis.refs_for(EvidenceRole.SUPPORTS)
    if len(supports) < settings.memory_v3_min_evidence:
        # The model could not assemble the required support from material that
        # passed the count threshold, which means the cluster was noise.
        _reject("insufficient_support", cluster)
        return False

    support_events = {ref_map[ref] for ref in supports}
    observed = sorted(
        _parse_time(item.occurred_at)
        for item in cluster.supporting
        if item.event_id in support_events
    )
    span = max(0, (observed[-1].date() - observed[0].date()).days) if observed else 0
    if span < settings.memory_v3_min_span_days:
        _reject("support_span_too_short", cluster)
        return False
    contexts = {
        item.session_id
        for item in cluster.supporting
        if item.event_id in support_events and item.session_id
    }
    if len(contexts) < settings.memory_v3_min_contexts:
        _reject("support_contexts_too_few", cluster)
        return False

    if hypothesis.terrain_kind == TerrainKind.TWO_FORCES and not hypothesis.refs_for(
        EvidenceRole.CONTRADICTS
    ):
        _reject("two_forces_without_conflict", cluster)
        return False

    if _BANNED.search(hypothesis.claim_text) or _BANNED.search(hypothesis.why_now):
        _reject("banned_language", cluster)
        return False
    return True


async def generate_hypothesis(
    cluster: FormationCluster,
    *,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> tuple[FormationHypothesis, dict[str, str]] | None:
    """Stage 2.  Returns the accepted hypothesis and its ref mapping."""

    settings = settings or get_settings()
    if not settings.dashscope_api_key:
        return None
    prompt, ref_map = _build_prompt_payload(cluster)

    for attempt in range(2):
        try:
            payload = await dashscope.chat_json(
                [
                    {"role": "system", "content": _FORMATION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                json_schema=FormationHypothesis.model_json_schema(),
                schema_name="formation_hypothesis_v1",
                temperature=0.2,
                max_tokens=900,
                request_id=request_id,
                settings=settings,
            )
            hypothesis = FormationHypothesis.model_validate(payload)
        except Exception:
            log.exception("Formation hypothesis generation failed")
            return None
        if validate_hypothesis(
            hypothesis, cluster=cluster, ref_map=ref_map, settings=settings
        ):
            return hypothesis, ref_map
        # Only banned language is worth one retry; a structural violation means
        # the model misread the contract and will likely repeat it.
        if attempt == 0 and (
            _BANNED.search(hypothesis.claim_text) or _BANNED.search(hypothesis.why_now)
        ):
            continue
        return None
    return None


# ---------------------------------------------------------------------------
# Stage 3: persistence
# ---------------------------------------------------------------------------


def _formation_keys(cluster: FormationCluster, claim_text: str) -> tuple[str, str]:
    """Identity of a terrain feature.

    ``terrain_kind`` is deliberately excluded: a feature that moves from growing
    to loosening is the same feature in a new state, not a new one.
    """

    return claim_keys_from_fields(
        category=_FORMATION_CATEGORY.value,
        subject="self",
        predicate="formation",
        object_value="+".join(cluster.slot_keys),
        claim_text=claim_text,
    )


def _confidence_for(cluster: FormationCluster) -> float:
    """Explainable band, not a model self-report.

    Baseline 8.5 forbids showing decimal confidence to users, and a model's own
    certainty does not track accuracy.  Derive it from the evidence instead.
    """

    supports = len(cluster.supporting_event_ids)
    contexts = len(cluster.contexts)
    span = cluster.span_days
    score = 0.45
    score += min(0.20, 0.05 * max(0, supports - 3))
    score += min(0.10, 0.05 * max(0, contexts - 2))
    score += min(0.10, 0.01 * max(0, span - 7))
    if cluster.contradicting:
        score -= 0.08
    return round(min(max(score, 0.20), 0.85), 4)


async def _attach_inherited_evidence(
    session: AsyncSession,
    *,
    claim: MemoryClaim,
    cluster: FormationCluster,
    hypothesis: FormationHypothesis,
    ref_map: dict[str, str],
    settings: Settings,
) -> int:
    """Copy verified offsets across.  Nothing here is model-generated."""

    role_by_event = {
        ref_map[label.ref]: label.role.value for label in hypothesis.evidence_roles
    }
    best: dict[tuple[str, str], ClusterEvidence] = {}
    for item in cluster.evidence:
        role = role_by_event.get(item.event_id)
        if role is None:
            continue
        key = (item.event_id, role)
        # One event may ground several member claims; keep the longest excerpt so
        # the source view shows the most complete original wording.
        existing = best.get(key)
        if existing is None or len(item.excerpt_text) > len(existing.excerpt_text):
            best[key] = item

    existing_rows = (
        await session.execute(
            select(MemoryEvidence.event_id, MemoryEvidence.evidence_role).where(
                MemoryEvidence.claim_id == claim.claim_id
            )
        )
    ).all()
    already = {(event_id, role) for event_id, role in existing_rows}

    added = 0
    for (event_id, role), item in best.items():
        if (event_id, role) in already:
            continue
        session.add(
            MemoryEvidence(
                claim_id=claim.claim_id,
                event_id=event_id,
                evidence_role=role,
                excerpt_start=item.excerpt_start,
                excerpt_end=item.excerpt_end,
                excerpt_text=item.excerpt_text,
                source_weight=item.source_weight,
                extractor_version=settings.memory_v3_policy_version,
            )
        )
        added += 1
    claim.evidence_count = int(claim.evidence_count or 0) + added
    return added


async def persist_hypothesis(
    session: AsyncSession,
    *,
    cluster: FormationCluster,
    hypothesis: FormationHypothesis,
    ref_map: dict[str, str],
    request_id: str | None = None,
    settings: Settings | None = None,
) -> tuple[str | None, bool]:
    """Write or strengthen a formation claim.  Returns (claim_id, created)."""

    settings = settings or get_settings()
    now = _utcnow_iso()
    slot_key, content_hash = _formation_keys(cluster, hypothesis.claim_text)

    # The user may have permanently deleted this feature while the model call was
    # in flight.  Stage 1's check is therefore not sufficient on its own.
    if await _has_tombstone(session, cluster.fingerprint):
        _reject("tombstoned_during_generation", cluster)
        return None, False

    existing = (
        await session.execute(
            select(MemoryClaim)
            .where(
                MemoryClaim.user_id == cluster.user_id,
                MemoryClaim.slot_key == slot_key,
                MemoryClaim.source_type == SourceType.FORMATION.value,
                MemoryClaim.deleted_at.is_(None),
            )
            .order_by(MemoryClaim.version.desc())
        )
    ).scalars().all()

    # A rejected or hidden feature must not come back under a new id.
    if any(
        claim.user_status in {UserStatus.REJECTED.value, UserStatus.HIDDEN.value}
        for claim in existing
    ):
        _reject("previously_rejected", cluster)
        return None, False

    live = next(
        (
            claim
            for claim in existing
            if claim.user_status
            in {
                UserStatus.CONFIRMED.value,
                UserStatus.CORRECTED.value,
                UserStatus.PROPOSED.value,
                UserStatus.DEFERRED.value,
            }
        ),
        None,
    )

    if live is not None:
        before = {
            "claim_text": live.claim_text,
            "evidence_count": live.evidence_count,
            "terrain_state": live.terrain_state,
            "version": live.version,
        }
        added = await _attach_inherited_evidence(
            session,
            claim=live,
            cluster=cluster,
            hypothesis=hypothesis,
            ref_map=ref_map,
            settings=settings,
        )
        if added == 0:
            return live.claim_id, False
        live.confidence = _confidence_for(cluster)
        live.updated_at = now
        live.version = int(live.version or 1) + 1
        # The user owns the wording once they have confirmed or renamed it.
        if live.user_status == UserStatus.PROPOSED.value and not live.terrain_user_label:
            live.claim_text = hypothesis.claim_text
            live.terrain_state = _KIND_TO_TERRAIN_STATE[hypothesis.terrain_kind]
            live.content_hash = content_hash
        session.add(
            MemoryRevision(
                claim_id=live.claim_id,
                actor_type="system",
                action="formation_strengthened",
                before_json=before,
                after_json={
                    "claim_text": live.claim_text,
                    "evidence_count": live.evidence_count,
                    "terrain_state": live.terrain_state,
                    "version": live.version,
                },
                reason=hypothesis.why_now,
                request_id=request_id,
                policy_version=settings.memory_v3_policy_version,
            )
        )
        return live.claim_id, False

    observed = sorted(_parse_time(item.occurred_at) for item in cluster.supporting)
    claim = MemoryClaim(
        user_id=cluster.user_id,
        claim_type=ClaimType.SEMANTIC.value,
        category=_FORMATION_CATEGORY.value,
        subject="self",
        predicate="formation",
        object_value="+".join(cluster.slot_keys),
        claim_text=hypothesis.claim_text,
        slot_key=slot_key,
        content_hash=content_hash,
        source_type=SourceType.FORMATION.value,
        confidence=_confidence_for(cluster),
        # A formation is an inference about someone, so it is always a proposal
        # and never speaks up before the user has seen and accepted it.
        user_status=UserStatus.PROPOSED.value,
        sensitivity=Sensitivity.NORMAL.value,
        allow_proactive=False,
        valid_from=observed[0].isoformat().replace("+00:00", "Z") if observed else now,
        observed_at=observed[-1].isoformat().replace("+00:00", "Z") if observed else now,
        terrain_state=_KIND_TO_TERRAIN_STATE[hypothesis.terrain_kind],
        terrain_history_json=[
            {
                "at": now,
                "kind": "formed",
                "state": _KIND_TO_TERRAIN_STATE[hypothesis.terrain_kind],
                "reason": hypothesis.why_now,
                "evidence_count": len(cluster.supporting_event_ids),
                "span_days": cluster.span_days,
            }
        ],
        evidence_count=0,
        importance=0.7,
        version=1,
        created_by_policy_version=settings.memory_v3_policy_version,
        created_at=now,
        updated_at=now,
    )
    session.add(claim)
    await session.flush()
    added = await _attach_inherited_evidence(
        session,
        claim=claim,
        cluster=cluster,
        hypothesis=hypothesis,
        ref_map=ref_map,
        settings=settings,
    )
    if added < settings.memory_v3_min_evidence:
        # Defensive: inherited evidence disappeared between staging and write.
        await session.delete(claim)
        _reject("evidence_vanished", cluster)
        return None, False
    session.add(
        MemoryRevision(
            claim_id=claim.claim_id,
            actor_type="system",
            action="formation_created",
            before_json={},
            after_json={
                "claim_text": claim.claim_text,
                "terrain_state": claim.terrain_state,
                "evidence_count": claim.evidence_count,
            },
            reason=hypothesis.why_now,
            request_id=request_id,
            policy_version=settings.memory_v3_policy_version,
        )
    )
    return claim.claim_id, True


async def run_formation_scan(
    *,
    user_id: str,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> FormationScanResult:
    """One bounded formation pass for a single user.

    The three stages hold separate sessions on purpose: a model call must never
    run inside an open write transaction, and clustering must read a committed
    snapshot rather than its own partial writes.

    Producing nothing is the expected outcome most of the time and is not an
    error: baseline P3 requires that most use produces no long-term memory.
    """

    settings = settings or get_settings()
    result = FormationScanResult()
    if not settings.memory_v3_formation_enabled:
        return result

    async with get_db(read_only=True) as db:
        clusters, considered = await build_clusters(
            db, user_id=user_id, settings=settings
        )
    result.clusters_considered = considered
    result.clusters_admitted = len(clusters)

    for cluster in clusters[: settings.memory_v3_max_hypotheses_per_scan]:
        result.hypotheses_requested += 1
        generated = await generate_hypothesis(
            cluster, request_id=request_id, settings=settings
        )
        if generated is None:
            result.hypotheses_discarded += 1
            continue
        hypothesis, ref_map = generated
        async with get_db(read_only=False) as db:
            claim_id, created = await persist_hypothesis(
                db,
                cluster=cluster,
                hypothesis=hypothesis,
                ref_map=ref_map,
                request_id=request_id,
                settings=settings,
            )
        if claim_id is None:
            result.hypotheses_discarded += 1
        elif created:
            result.created_claim_ids.append(claim_id)
        else:
            result.strengthened_claim_ids.append(claim_id)
    return result


__all__ = [
    "FORMATION_SCAN_REQUESTED",
    "FORMATION_TOMBSTONE_TYPE",
    "ClusterEvidence",
    "FormationCluster",
    "FormationScanResult",
    "build_clusters",
    "formation_fingerprint",
    "generate_hypothesis",
    "persist_hypothesis",
    "run_formation_scan",
    "validate_hypothesis",
]
