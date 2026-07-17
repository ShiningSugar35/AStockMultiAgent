from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.research import load_research_core_config, load_research_skill_registry
from astock.schemas import (
    BASE_CASE_SECTIONS,
    BaseCasePack,
    EvidenceFreezeRequest,
    ResearchCoverageStatus,
    SpecialistDeltaBuildRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_research_core_config_covers_every_section_and_confidence_state() -> None:
    config = load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml")
    assert set(config.required_sections) == set(BASE_CASE_SECTIONS)
    assert config.confidence_caps[ResearchCoverageStatus.COMPLETE] == 0.9
    assert config.confidence_caps[ResearchCoverageStatus.INSUFFICIENT] == 0.4


def test_research_skill_registry_has_exact_versioned_contracts_and_three_skill_cap() -> None:
    registry = load_research_skill_registry(
        PROJECT_ROOT / "configs" / "research_skills.yaml"
    )
    assert registry.max_specialists == 3
    assert len(registry.skills) == 7
    assert len({item.skill_id for item in registry.skills}) == 7
    assert sum(item.counts_as_specialist for item in registry.skills) == 6
    memo = next(item for item in registry.skills if item.skill_id == "ResearchMemoComposer")
    assert not memo.counts_as_specialist


def test_evidence_freeze_scope_is_unique_and_approximation_is_explicit() -> None:
    with pytest.raises(ValidationError, match="claim ids"):
        EvidenceFreezeRequest(
            company_id="company:fixture",
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            claim_ids=["claim:1", "claim:1"],
        )
    with pytest.raises(ValidationError, match="formal historical"):
        EvidenceFreezeRequest(
            company_id="company:fixture",
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            formal_historical=False,
            allow_approximated=True,
        )


def test_base_case_cited_evidence_union_cannot_drift() -> None:
    with pytest.raises(ValidationError, match="evidence ids"):
        BaseCasePack(
            base_case_id="base:fixture",
            evidence_pack_id="evidence-pack:fixture",
            company_id="company:fixture",
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            kernel_version="base-case-v1",
            findings_by_section={section: [] for section in BASE_CASE_SECTIONS},
            evidence_gaps=[],
            specialist_tags=[],
            requested_base_confidence=0,
            base_confidence=0,
            confidence_cap=0,
            coverage_by_section={section: 0 for section in BASE_CASE_SECTIONS},
            coverage_status=ResearchCoverageStatus.INSUFFICIENT,
            degradation_codes=["EMPTY_REQUIRED_SECTION"],
            evidence_ids=["evidence:orphan"],
        )


def test_specialist_delta_contract_rejects_full_company_rewrite_fields() -> None:
    with pytest.raises(ValidationError, match="company_summary"):
        SpecialistDeltaBuildRequest.model_validate(
            {
                "base_case_id": "base:fixture",
                "route_plan_id": "route:fixture",
                "skill_id": "IndustryBottleneckSkill",
                "skill_version": "v1",
                "incremental_findings": [],
                "base_case_corrections": [],
                "industry_specific_metrics": [],
                "additional_evidence_requests": [],
                "failure_modes": [],
                "confidence_delta": 0,
                "valuation_adjustments": [],
                "risk_adjustments": [],
                "coverage_delta": {},
                "company_summary": "This full rewrite field is forbidden.",
            }
        )
