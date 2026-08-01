"""Pure unit tests for Memory V2 extraction and evidence invariants."""
from __future__ import annotations

from types import SimpleNamespace

from app.memory.consolidate import _build_extract_user_msg
from app.memory_v2.domain import (
    ClaimType,
    MemoryAtom,
    MemoryCategory,
    Sensitivity,
    SourceType,
)
from app.memory_v2.extractor import extract_rule_based
from app.memory_v2.reconcile import claim_keys, ground_evidence

NOW = "2026-08-01T08:00:00Z"


def _atom(
    *,
    category: MemoryCategory,
    predicate: str,
    object_value: str,
    evidence_text: str,
) -> MemoryAtom:
    return MemoryAtom(
        category=category,
        predicate=predicate,
        object_value=object_value,
        claim_text=f"测试：{object_value}",
        source_type=SourceType.USER_EXPLICIT,
        confidence=0.9,
        evidence_text=evidence_text,
    )


def test_rule_extraction_is_explicit_grounded_and_conservative():
    text = "我叫小岚。我喜欢手冲咖啡。以后请你少用反问。"
    extraction = extract_rule_based(text, occurred_at=NOW)

    assert {atom.category for atom in extraction.atoms} == {
        MemoryCategory.IDENTITY,
        MemoryCategory.PREFERENCE,
        MemoryCategory.INTERACTION_PREFERENCE,
    }
    for atom in extraction.atoms:
        assert atom.source_type == SourceType.USER_EXPLICIT
        assert atom.evidence_text == text[atom.evidence_start : atom.evidence_end]

    interaction = extraction.atoms[-1]
    assert interaction.claim_type == ClaimType.PROCEDURAL
    assert interaction.sensitivity == Sensitivity.NORMAL
    assert extract_rule_based("今天好累，只想休息。", occurred_at=NOW).atoms == []


def test_sensitive_and_crisis_atoms_are_classified_before_reconciliation():
    sensitive = extract_rule_based("以后请你不要主动提我的工资。", occurred_at=NOW)
    assert len(sensitive.atoms) == 1
    assert sensitive.atoms[0].sensitivity == Sensitivity.SENSITIVE

    crisis = extract_rule_based("我打算自杀。", occurred_at=NOW)
    assert len(crisis.atoms) == 1
    assert crisis.atoms[0].sensitivity == Sensitivity.CRISIS


def test_grounding_rejects_invented_evidence_and_repairs_wrong_offsets():
    invented = _atom(
        category=MemoryCategory.PREFERENCE,
        predicate="likes",
        object_value="咖啡",
        evidence_text="我喜欢咖啡",
    )
    assert ground_evidence(invented, "我今天喝了咖啡") is None

    recoverable = invented.model_copy(
        update={"evidence_start": 99, "evidence_end": 100}
    )
    grounded = ground_evidence(recoverable, "其实我喜欢咖啡。")
    assert grounded is not None
    assert grounded.text == "我喜欢咖啡"
    assert grounded.start == 2


def test_multi_value_preferences_do_not_share_a_temporal_slot():
    coffee = _atom(
        category=MemoryCategory.PREFERENCE,
        predicate="likes",
        object_value="咖啡",
        evidence_text="我喜欢咖啡",
    )
    tea = coffee.model_copy(
        update={
            "object_value": "茶",
            "claim_text": "测试：茶",
            "evidence_text": "我喜欢茶",
        }
    )
    assert claim_keys(coffee)[0] != claim_keys(tea)[0]

    first_name = _atom(
        category=MemoryCategory.IDENTITY,
        predicate="name",
        object_value="小岚",
        evidence_text="我叫小岚",
    )
    second_name = first_name.model_copy(
        update={
            "object_value": "小雨",
            "claim_text": "测试：小雨",
            "evidence_text": "我叫小雨",
        }
    )
    assert claim_keys(first_name)[0] == claim_keys(second_name)[0]
    assert claim_keys(first_name)[1] != claim_keys(second_name)[1]


def test_legacy_consolidation_never_sends_assistant_text_as_user_evidence():
    message = _build_extract_user_msg(
        [
            SimpleNamespace(
                episodic_id="ep-1",
                created_at=NOW,
                kind="confide",
                raw_user_text="我喜欢爵士乐",
                raw_assistant_text="你一定每天都听爵士乐",
            )
        ]
    )
    assert "我喜欢爵士乐" in message
    assert "你一定每天都听爵士乐" not in message
