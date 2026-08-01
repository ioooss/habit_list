"""Gated, explainable hybrid retrieval for Memory V2."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Iterable

import jieba
import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..db.memory_models import MemoryClaim, MemoryEmbedding
from .domain import RetrievalBatch, RetrievalCandidate, RetrievalRoute, UserStatus

_TEMPORAL_RE = re.compile(r"之前|以前|上次|最近|这段时间|过去|后来|当时|什么时候|变了|变化")
_MEMORY_RE = re.compile(r"记得|还记得|我说过|我提过|你知道我|关于我|我喜欢什么|我的习惯")
_RELATION_RE = re.compile(r"妈妈|爸爸|父母|家人|朋友|同事|老板|伴侣|对象|老公|老婆|关系|我们")
_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def classify_retrieval_route(query: str) -> RetrievalRoute:
    """Conservative gate: absence of a clear cue means no long-term recall."""

    if not query.strip():
        return RetrievalRoute.NONE
    if _TEMPORAL_RE.search(query):
        return RetrievalRoute.TEMPORAL
    if _MEMORY_RE.search(query):
        return RetrievalRoute.SEMANTIC
    if _RELATION_RE.search(query) and re.search(r"怎么样|怎么|为什么|之前|最近|又|还是", query):
        return RetrievalRoute.RELATIONSHIP
    return RetrievalRoute.NONE


def _tokens(text: str) -> list[str]:
    normalized = " ".join(_TOKEN_RE.findall((text or "").casefold()))
    if not normalized:
        return []
    return [token.strip() for token in jieba.lcut(normalized) if token.strip()]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _recency(value: str | None, *, half_life_days: float = 90.0) -> float:
    parsed = _parse_iso(value)
    if parsed is None:
        return 0.25
    days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    return float(math.exp(-math.log(2.0) * days / half_life_days))


def _activation(claim: MemoryClaim) -> float:
    if claim.pinned:
        return 1.0
    anchor = claim.last_landed_at or claim.updated_at or claim.observed_at
    parsed = _parse_iso(anchor)
    if parsed is None:
        return 0.2
    days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    # Power-law activation; computed from absolute time and never compounded.
    return float(min(1.0, (days + 1.0) ** -0.35))


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    va = np.asarray(list(a), dtype=np.float32)
    vb = np.asarray(list(b), dtype=np.float32)
    if va.size == 0 or va.size != vb.size:
        return 0.0
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom <= 1e-12:
        return 0.0
    return float(max(0.0, min(1.0, np.dot(va, vb) / denom)))


def _valid_now(claim: MemoryClaim, now_iso: str) -> bool:
    if claim.valid_from and claim.valid_from > now_iso:
        return False
    if claim.valid_to and claim.valid_to <= now_iso:
        return False
    return True


def _select_diverse(
    candidates: list[RetrievalCandidate],
    claims_by_id: dict[str, MemoryClaim],
    *,
    topk: int,
) -> list[RetrievalCandidate]:
    selected: list[RetrievalCandidate] = []
    seen_slots: set[str] = set()
    category_counts: dict[str, int] = {}
    for candidate in candidates:
        claim = claims_by_id[candidate.claim_id]
        if claim.slot_key in seen_slots:
            continue
        if category_counts.get(claim.category, 0) >= 2:
            continue
        selected.append(candidate)
        seen_slots.add(claim.slot_key)
        category_counts[claim.category] = category_counts.get(claim.category, 0) + 1
        if len(selected) >= topk:
            break
    return selected


async def retrieve_memories(
    session: AsyncSession,
    *,
    user_id: str,
    query: str,
    query_embedding: list[float] | None = None,
    route: RetrievalRoute | None = None,
    settings: Settings | None = None,
) -> RetrievalBatch:
    settings = settings or get_settings()
    route = route or classify_retrieval_route(query)
    empty = RetrievalBatch(route=route, query_hash=_query_hash(query))
    if settings.memory_v2_mode not in {"shadow_retrieve", "active"}:
        return empty
    if route in {RetrievalRoute.NONE, RetrievalRoute.WORKING_ONLY}:
        return empty

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    claims = list(
        (
            await session.execute(
                select(MemoryClaim)
                .where(
                    MemoryClaim.user_id == user_id,
                    MemoryClaim.deleted_at.is_(None),
                    MemoryClaim.user_status.in_(
                        [UserStatus.CONFIRMED.value, UserStatus.CORRECTED.value]
                    ),
                )
                .order_by(MemoryClaim.pinned.desc(), MemoryClaim.updated_at.desc())
                .limit(settings.memory_v2_candidate_limit)
            )
        ).scalars().all()
    )
    claims = [claim for claim in claims if _valid_now(claim, now_iso)]
    if not claims:
        return empty

    corpus_tokens = [
        _tokens(
            " ".join(
                [
                    claim.claim_text,
                    claim.category,
                    claim.subject,
                    claim.predicate,
                    claim.object_value,
                ]
            )
        )
        for claim in claims
    ]
    query_tokens = _tokens(query)
    lexical_scores = [0.0] * len(claims)
    if query_tokens and any(corpus_tokens):
        raw = BM25Okapi(corpus_tokens).get_scores(query_tokens)
        max_score = max(float(score) for score in raw) if len(raw) else 0.0
        if max_score > 0:
            lexical_scores = [max(0.0, float(score) / max_score) for score in raw]

    semantic_scores: dict[str, float] = {}
    if query_embedding:
        if len(query_embedding) != settings.dashscope_embedding_dim:
            raise ValueError("query embedding dimension does not match configured dimension")
        claim_ids = [claim.claim_id for claim in claims]
        if session.get_bind().dialect.name == "postgresql":
            distance = MemoryEmbedding.vector_json.cosine_distance(query_embedding)
            rows = (
                await session.execute(
                    select(MemoryEmbedding.claim_id, distance.label("distance"))
                    .where(
                        MemoryEmbedding.claim_id.in_(claim_ids),
                        MemoryEmbedding.model == settings.dashscope_embedding_model,
                        MemoryEmbedding.status == "active",
                    )
                    .order_by(distance)
                    .limit(settings.memory_v2_candidate_limit)
                )
            ).all()
            semantic_scores = {
                str(row.claim_id): max(0.0, min(1.0, 1.0 - float(row.distance)))
                for row in rows
            }
        else:
            embeddings = (
                await session.execute(
                    select(MemoryEmbedding).where(
                        MemoryEmbedding.claim_id.in_(claim_ids),
                        MemoryEmbedding.model == settings.dashscope_embedding_model,
                        MemoryEmbedding.status == "active",
                    )
                )
            ).scalars().all()
            for embedding in embeddings:
                vector = embedding.vector_json
                if isinstance(vector, list):
                    semantic_scores[embedding.claim_id] = _cosine(query_embedding, vector)

    candidates: list[RetrievalCandidate] = []
    claims_by_id = {claim.claim_id: claim for claim in claims}
    for idx, claim in enumerate(claims):
        lexical = lexical_scores[idx]
        semantic = semantic_scores.get(claim.claim_id, 0.0)
        temporal = _recency(claim.valid_from or claim.observed_at)
        if route == RetrievalRoute.TEMPORAL:
            temporal = min(1.0, temporal + 0.15)
        continuity = 0.2
        if claim.category == "relationship" and route == RetrievalRoute.RELATIONSHIP:
            continuity = 1.0
        elif lexical >= 0.35:
            continuity = min(1.0, 0.4 + lexical)
        importance = min(max(float(claim.importance or 0.5), 0.0), 1.0)
        pin = 1.0 if claim.pinned else 0.0
        activation = _activation(claim)

        # Sensitive memories require an explicit opt-in and a strong query match.
        if claim.sensitivity != "normal" and (not claim.allow_proactive or lexical < 0.35):
            continue

        base = (
            0.30 * semantic
            + 0.18 * lexical
            + 0.15 * temporal
            + 0.10 * continuity
            + 0.10 * importance
            + 0.05 * pin
            + 0.12 * activation
        )
        trust = min(max(float(claim.confidence or 0.0), 0.0), 1.0)
        final = round(base * trust, 6)
        if final < settings.memory_v2_min_retrieval_score:
            continue
        reasons: list[str] = []
        if semantic >= 0.55:
            reasons.append("semantic_match")
        if lexical >= 0.35:
            reasons.append("lexical_match")
        if temporal >= 0.65:
            reasons.append("temporally_relevant")
        if claim.pinned:
            reasons.append("user_pinned")
        candidates.append(
            RetrievalCandidate(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                category=claim.category,
                user_status=claim.user_status,
                sensitivity=claim.sensitivity,
                confidence=round(trust, 4),
                evidence_count=int(claim.evidence_count or 0),
                valid_from=claim.valid_from,
                valid_to=claim.valid_to,
                final_score=final,
                features={
                    "semantic": round(semantic, 4),
                    "lexical": round(lexical, 4),
                    "temporal": round(temporal, 4),
                    "continuity": round(continuity, 4),
                    "importance": round(importance, 4),
                    "pin": pin,
                    "activation": round(activation, 4),
                },
                reasons=reasons,
            )
        )

    candidates.sort(key=lambda item: item.final_score, reverse=True)
    candidate_window = candidates[: max(settings.memory_v2_retrieval_topk * 4, 12)]
    selected = _select_diverse(
        candidate_window,
        claims_by_id,
        topk=settings.memory_v2_retrieval_topk,
    )
    return RetrievalBatch(
        route=route,
        query_hash=_query_hash(query),
        candidates=candidate_window,
        selected=selected,
        used_in_response=settings.memory_v2_mode == "active" and bool(selected),
    )


def format_memory_context(batch: RetrievalBatch) -> str:
    if not batch.used_in_response or not batch.selected:
        return ""
    lines = [
        "【经过证据校验的长期记忆】",
        "只在当前话题自然需要时引用；不要为了表现记得而主动重复。",
    ]
    for item in batch.selected:
        time_hint = ""
        if item.valid_from:
            time_hint = f"（自 {item.valid_from[:10]} 起）"
        lines.append(f"· [{item.category}] {item.claim_text}{time_hint}")
    return "\n".join(lines)


__all__ = [
    "classify_retrieval_route",
    "format_memory_context",
    "retrieve_memories",
]
