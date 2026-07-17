from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.research import load_research_core_config
from astock.schemas import (
    BASE_CASE_SECTIONS,
    BaseCasePack,
    EvidenceFreezeRequest,
    ResearchCoverageStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_research_core_config_covers_every_section_and_confidence_state() -> None:
    config = load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml")
    assert set(config.required_sections) == set(BASE_CASE_SECTIONS)
    assert config.confidence_caps[ResearchCoverageStatus.COMPLETE] == 0.9
    assert config.confidence_caps[ResearchCoverageStatus.INSUFFICIENT] == 0.4


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
