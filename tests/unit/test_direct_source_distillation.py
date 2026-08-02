from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.errors import DataQualityError
from astock.core.hashing import sha256_bytes
from astock.knowledge.direct_source_distillation_service import (
    DirectSourceDistillationService,
)
from astock.schemas.direct_source_distillation import (
    DirectRunInitManifest,
    DirectSkillModule,
    DirectSolBatchOutput,
    DirectSourceKind,
    parse_direct_source_locator,
)

_EMPTY_HASH = sha256_bytes(b"")

_HASH_CONTRACT = {
    "algorithm": "SHA-256",
    "normalization": (
        "Unicode NFKC; collapse contiguous whitespace to U+0020; strip"
    ),
    "batch_serialization": (
        "UTF-8 page=<1-based page>\\n<normalized text>, joined with \\n\\f\\n"
    ),
    "source_locator_offsets": (
        "0-based half-open Python code-point offsets in normalized page text"
    ),
}


def _public_batch(**skill_updates: object) -> dict[str, object]:
    skill: dict[str, object] = {
        "skill_name": "Synthetic evidence rule",
        "primary_module": "FUNDAMENTAL_RESEARCH",
        "decision_question": "Does the frozen evidence support the rule?",
        "core_principle": "Trace the claim to the precise frozen source slice.",
        "confidence": 0.8,
        "status": "READY_FOR_SHADOW",
        "uncertainty_reason": None,
        "source_refs": [
            {
                "source_file_hash": "a" * 64,
                "source_kind": "PDF",
                "page_number": 6,
                "locator": "pdf-page-6;normalized-page-text;chars=2:7",
                "source_object_hash": sha256_bytes(b"slice"),
                "visual_evidence_ids": [],
                "paragraph_head": "slice",
            }
        ],
    }
    skill.update(skill_updates)
    return {
        "schema_version": "direct-source-skill-batch-v1",
        "source_kind": "PDF",
        "batch_id": "b01",
        "section_title": "Synthetic section",
        "locator": {"page_start": 6, "page_end": 6},
        "source_file_hash": "a" * 64,
        "batch_text_object_hash": "b" * 64,
        "sol_distillation_version": "sol-direct-v1",
        "hash_contract": _HASH_CONTRACT,
        "visual_evidence_refs": [],
        "skills": [skill],
        "no_skill_reason": None,
        "open_questions": [],
    }


def _init_manifest(
    *,
    current_indexes: list[int],
    empty_indexes: list[int],
    include_range: bool = True,
) -> dict[str, object]:
    batch: dict[str, object] = {
        "batch_id": "docx-001",
        "source_id": "source:docx",
        "chapter_unit_id": "chapter:docx",
        "ordinal": 1,
        "current_fragments": [
            {
                "fragment_id": f"fragment:{index}",
                "object_hash": "b" * 64,
                "locator": {
                    "source_kind": "DOCX",
                    "unit_index": index,
                    "start_offset": 0,
                    "end_offset": 1,
                },
            }
            for index in current_indexes
        ],
        "audited_empty_units": [
            {
                "object_hash": _EMPTY_HASH,
                "locator": {
                    "source_kind": "DOCX",
                    "unit_index": index,
                    "start_offset": 0,
                    "end_offset": 0,
                },
            }
            for index in empty_indexes
        ],
    }
    if include_range:
        batch["source_unit_start"] = 1
        batch["source_unit_end"] = 3
    return {
        "schema_version": "direct-source-run-init-v1",
        "run_id": "direct-run:empty-contract",
        "pipeline_version": "direct-pipeline-v1",
        "sources": [
            {
                "source_id": "source:docx",
                "source_kind": "DOCX",
                "source_file_hash": "a" * 64,
            }
        ],
        "batches": [batch],
        "formal_committee_weight_allowed": False,
    }


def test_six_fixed_investment_modules_are_exact() -> None:
    assert {item.value for item in DirectSkillModule} == {
        "SOURCING_SCREENING",
        "FUNDAMENTAL_RESEARCH",
        "VALUATION_PRICING",
        "PORTFOLIO_CONSTRUCTION",
        "POSITION_RISK_MANAGEMENT",
        "PSYCHOLOGY_BEHAVIOR",
    }


def test_existing_skill_shape_accepts_absent_optional_arrays() -> None:
    batch = DirectSolBatchOutput.model_validate(_public_batch())
    skill = batch.skills[0]
    assert skill.secondary_modules == []
    assert skill.applicable_conditions == []
    assert skill.reasoning_steps == []
    assert skill.required_evidence == []
    assert skill.positive_signals == []
    assert skill.negative_signals == []
    assert skill.invalidation_conditions == []
    assert skill.failure_modes == []

    with pytest.raises(ValidationError, match="actions"):
        DirectSolBatchOutput.model_validate(
            _public_batch(actions=["An invented legacy field."])
        )


def test_primary_and_secondary_modules_are_strict_and_unique() -> None:
    with pytest.raises(ValidationError, match="primary_module"):
        DirectSolBatchOutput.model_validate(
            _public_batch(
                secondary_modules=[
                    "FUNDAMENTAL_RESEARCH",
                ]
            )
        )
    with pytest.raises(ValidationError, match="de-duplicated"):
        DirectSolBatchOutput.model_validate(
            _public_batch(
                secondary_modules=[
                    "VALUATION_PRICING",
                    "VALUATION_PRICING",
                ]
            )
        )


def test_ready_and_needs_status_contracts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="precise source_refs"):
        DirectSolBatchOutput.model_validate(_public_batch(source_refs=[]))
    with pytest.raises(ValidationError, match="cannot retain uncertainty"):
        DirectSolBatchOutput.model_validate(
            _public_batch(
                uncertainty_reason=(
                    "The chapter boundary is genuinely unclear."
                )
            )
        )
    needs = _public_batch(
        status="NEEDS_USER_REVIEW",
        source_refs=[],
        uncertainty_reason=(
            "The frozen section omits the limiting condition for this rule."
        ),
    )
    assert (
        DirectSolBatchOutput.model_validate(needs).skills[0].status.value
        == "NEEDS_USER_REVIEW"
    )
    with pytest.raises(ValidationError, match="not concrete"):
        DirectSolBatchOutput.model_validate(
            _public_batch(
                status="NEEDS_USER_REVIEW",
                source_refs=[],
                uncertainty_reason="待定",
            )
        )


def test_pdf_and_reserved_docx_locators_are_precise() -> None:
    pdf = parse_direct_source_locator(
        DirectSourceKind.PDF,
        "pdf-page-12;normalized-page-text;chars=4:19",
    )
    assert pdf.model_dump(mode="json") == {
        "source_kind": "PDF",
        "unit_index": 12,
        "start_offset": 4,
        "end_offset": 19,
    }
    docx = parse_direct_source_locator(
        DirectSourceKind.DOCX,
        "docx-paragraph-8;normalized-paragraph-text;chars=1:9",
    )
    assert docx.model_dump(mode="json") == {
        "source_kind": "DOCX",
        "unit_index": 8,
        "start_offset": 1,
        "end_offset": 9,
    }
    with pytest.raises(ValueError, match="expected"):
        parse_direct_source_locator(
            DirectSourceKind.PDF,
            "pdf-page-12;chars=4:19",
        )


def test_empty_batch_requires_concrete_reason() -> None:
    batch = _public_batch()
    batch["skills"] = []
    batch["no_skill_reason"] = (
        "The section contains no decision-relevant operating rule."
    )
    assert DirectSolBatchOutput.model_validate(batch).skills == []
    batch["no_skill_reason"] = "none"
    with pytest.raises(ValidationError, match="concrete no_skill_reason"):
        DirectSolBatchOutput.model_validate(batch)


def test_strict_json_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    forged = tmp_path / "forged.json"
    forged.write_text(
        '{"batch_id":"b01","batch_id":"forged"}',
        encoding="utf-8",
    )
    with pytest.raises(DataQualityError, match="duplicate JSON key"):
        DirectSourceDistillationService.load_json_file(forged)

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"batch_id": "b01"}), encoding="utf-8")
    assert DirectSourceDistillationService.load_json_file(valid) == {
        "batch_id": "b01"
    }


def test_audited_empty_units_complete_the_frozen_docx_range() -> None:
    manifest = DirectRunInitManifest.model_validate(
        _init_manifest(current_indexes=[1, 3], empty_indexes=[2])
    )
    batch = manifest.batches[0]
    assert [item.locator.unit_index for item in batch.audited_empty_units] == [2]
    assert batch.audited_empty_units[0].locator.start_offset == 0
    assert batch.audited_empty_units[0].locator.end_offset == 0
    assert batch.audited_empty_units[0].object_hash == _EMPTY_HASH


@pytest.mark.parametrize(
    ("current_indexes", "empty_indexes", "expected"),
    [
        ([1, 3], [], "exactly cover"),
        ([1, 3], [2, 4], "exactly cover"),
        ([1, 3], [2, 2], "must be unique"),
        ([1, 2, 3], [2], "must not overlap"),
    ],
)
def test_audited_empty_unit_delete_add_duplicate_and_overlap_fail_closed(
    current_indexes: list[int],
    empty_indexes: list[int],
    expected: str,
) -> None:
    with pytest.raises(ValidationError, match=expected):
        DirectRunInitManifest.model_validate(
            _init_manifest(
                current_indexes=current_indexes,
                empty_indexes=empty_indexes,
            )
        )


def test_legacy_manifest_without_empty_units_remains_strictly_compatible() -> None:
    manifest = DirectRunInitManifest.model_validate(
        _init_manifest(
            current_indexes=[1, 2, 3],
            empty_indexes=[],
            include_range=False,
        )
    )
    batch = manifest.batches[0]
    assert batch.source_unit_start is None
    assert batch.source_unit_end is None
    assert batch.audited_empty_units == []
