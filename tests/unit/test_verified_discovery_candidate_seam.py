from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.candidates.promotion import ResearchSeedPromotionService
from astock.candidates.service import CandidateScanService
from astock.schemas.candidates import (
    CandidateEvidenceSeverity,
    CandidateSignalDisposition,
    CandidateSignalType,
)

NOW = datetime(2026, 9, 20, 5, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("origins", "reasons", "expected"),
    [
        (["EXPERT_SKILL"], ["VERIFIED_DISCOVERY_THESIS"], True),
        ([], ["VERIFIED_DISCOVERY_THESIS"], True),
        (["MARKET"], ["VERIFIED_DISCOVERY_THESIS"], True),
        (["EXISTING_CANDIDATE"], ["VERIFIED_DISCOVERY_THESIS"], True),
        (["BREADTH_CHALLENGER"], [], True),
        (["LONG_HORIZON_VALUE"], [], True),
        (
            ["BREADTH_CHALLENGER", "EXPERT_SKILL"],
            ["VERIFIED_DISCOVERY_THESIS", "DISCOVERY_CHANNEL:EVENT"],
            True,
        ),
        (
            ["LONG_HORIZON_VALUE", "EXPERT_SKILL"],
            ["VERIFIED_DISCOVERY_THESIS", "VERIFIED_DISCOVERY_THESIS"],
            True,
        ),
        (["EXPERT_SKILL"], ["DISCOVERY_CHANNEL:EVENT"], False),
        (["EXPERT_SKILL"], ["verified_discovery_thesis"], False),
        (["EXPERT_SKILL"], ["VERIFIED_DISCOVERY_THESIS_V2"], False),
        (["MARKET"], ["DISCOVERY_THESIS:abc"], False),
    ],
)
def test_verified_discovery_marker_candidate_seam_is_exact(
    origins: list[str],
    reasons: list[str],
    expected: bool,
) -> None:
    service = object.__new__(CandidateScanService)
    service.config = cast(Any, SimpleNamespace(rules_version="candidate-test-v1"))
    company = cast(
        Any,
        SimpleNamespace(
            company_id="600001",
            instrument_id="XSHG:600001",
            instrument_artifact_id="instrument:a",
            research_seed_origins=origins,
            research_seed_reason_codes=reasons,
        ),
    )
    artifact = cast(
        Any,
        SimpleNamespace(
            artifact_id="instrument:a",
            available_to_system_at=NOW,
            source_snapshot_ids=["snapshot:a"],
            source_family="fixture-instrument",
        ),
    )
    request = cast(Any, SimpleNamespace(as_of=NOW))

    signal = service._research_seed_signal(
        "a" * 64,
        request,
        company,
        {"instrument:a": artifact},
    )

    if not expected:
        assert signal is None
        return
    assert signal is not None
    assert signal.signal_type is CandidateSignalType.RESEARCH_SEED_PRIOR
    assert signal.disposition is CandidateSignalDisposition.SUPPORT
    assert signal.severity is CandidateEvidenceSeverity.MEDIUM
    assert signal.reason_codes.count("VERIFIED_DISCOVERY_THESIS") <= 1
    if "VERIFIED_DISCOVERY_THESIS" in reasons:
        assert "VERIFIED_DISCOVERY_THESIS" in signal.reason_codes


def test_production_promotion_preserves_seed_origins_and_reason_codes() -> None:
    source = inspect.getsource(ResearchSeedPromotionService._promote_company)
    assert "research_seed_origins=sorted(item.value for item in seed.origins)" in source
    assert "research_seed_reason_codes=sorted(seed.reason_codes)" in source
