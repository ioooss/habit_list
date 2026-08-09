"""Typed domain contracts for Memory V2."""
from __future__ import annotations

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClaimType(StrEnum):
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryCategory(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    HEALTH = "health"
    GOAL = "goal"
    HABIT = "habit"
    LOCATION = "location"
    FINANCE = "finance"
    CREATIVITY = "creativity"
    CONSUMPTION = "consumption"
    CYCLE = "cycle"
    INTERACTION_PREFERENCE = "interaction_preference"
    OTHER = "other"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    PRIVATE = "private"
    SENSITIVE = "sensitive"
    CRISIS = "crisis"


class SourceType(StrEnum):
    USER_EXPLICIT = "user_explicit"
    SYSTEM_INFERRED = "system_inferred"
    USER_CONFIRMED = "user_confirmed"
    # Produced by the formation layer from a cluster of already-grounded
    # evidence.  This is the only source type that expresses something the user
    # never said in one place, which is why it always starts as a proposal.
    FORMATION = "formation"


class UserStatus(StrEnum):
    PROPOSED = "proposed"
    DEFERRED = "deferred"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    HIDDEN = "hidden"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class EvidenceRole(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CORRECTS = "corrects"


class RetrievalRoute(StrEnum):
    NONE = "none"
    WORKING_ONLY = "working_only"
    SEMANTIC = "semantic"
    TEMPORAL = "temporal"
    RELATIONSHIP = "relationship"


class MemoryAtom(BaseModel):
    """One proposed memory unit extracted from a single user-authored event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim_type: ClaimType = ClaimType.SEMANTIC
    category: MemoryCategory
    subject: str = Field(default="self", min_length=1, max_length=128)
    predicate: str = Field(min_length=1, max_length=128)
    object_value: str = Field(min_length=1, max_length=500)
    claim_text: str = Field(min_length=2, max_length=500)
    source_type: SourceType = SourceType.SYSTEM_INFERRED
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    sensitivity: Sensitivity = Sensitivity.NORMAL
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_text: str = Field(min_length=1, max_length=1000)
    evidence_start: Optional[int] = Field(default=None, ge=0)
    evidence_end: Optional[int] = Field(default=None, ge=0)

    @field_validator("predicate", "object_value", "claim_text", "evidence_text")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        return " ".join(value.replace("\x00", " ").split())


class MemoryExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    atoms: list[MemoryAtom] = Field(default_factory=list, max_length=12)


class ReconciliationResult(BaseModel):
    created_claim_ids: list[str] = Field(default_factory=list)
    updated_claim_ids: list[str] = Field(default_factory=list)
    superseded_claim_ids: list[str] = Field(default_factory=list)
    skipped_atoms: int = 0

    @property
    def touched_claim_ids(self) -> list[str]:
        return list(dict.fromkeys(self.created_claim_ids + self.updated_claim_ids))


class RetrievalCandidate(BaseModel):
    claim_id: str
    claim_text: str
    category: str
    user_status: str
    sensitivity: str
    confidence: float
    evidence_count: int
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    final_score: float
    features: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class RetrievalBatch(BaseModel):
    route: RetrievalRoute
    query_hash: str
    candidates: list[RetrievalCandidate] = Field(default_factory=list)
    selected: list[RetrievalCandidate] = Field(default_factory=list)
    used_in_response: bool = False


class TerrainKind(StrEnum):
    """The five terrain expressions allowed by product baseline 4.1."""

    GROWING = "growing"  # 正在长出来
    RECURRING = "recurring"  # 反复回到
    LOOSENING = "loosening"  # 正在松动
    TWO_FORCES = "two_forces"  # 两股力量
    UNNAMED = "unnamed"  # 尚未命名


class EvidenceLabel(BaseModel):
    """A role the model assigns to one already-grounded piece of evidence.

    The model is shown opaque ordinal refs (``E1``, ``E2``…), never event ids,
    text offsets, or excerpt content it could echo back.  It may only label
    evidence that an earlier stage located verbatim in the user's own wording.
    A ref outside the supplied set fails validation, so the formation layer
    cannot invent a citation even if the model tries.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^E\d{1,2}$")
    role: EvidenceRole


class FormationHypothesis(BaseModel):
    """A cross-event hypothesis about what is forming in someone."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    terrain_kind: TerrainKind
    claim_text: str = Field(min_length=4, max_length=200)
    why_now: str = Field(min_length=4, max_length=200)
    evidence_roles: list[EvidenceLabel] = Field(min_length=1, max_length=64)

    @field_validator("claim_text", "why_now")
    @classmethod
    def _single_line(cls, value: str) -> str:
        return " ".join(value.replace("\x00", " ").split())

    def refs_for(self, role: EvidenceRole) -> list[str]:
        return [label.ref for label in self.evidence_roles if label.role == role]


__all__ = [
    "ClaimType",
    "EvidenceLabel",
    "EvidenceRole",
    "FormationHypothesis",
    "MemoryAtom",
    "MemoryCategory",
    "MemoryExtraction",
    "ReconciliationResult",
    "RetrievalBatch",
    "RetrievalCandidate",
    "RetrievalRoute",
    "Sensitivity",
    "SourceType",
    "TerrainKind",
    "UserStatus",
]
