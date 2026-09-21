from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from astock.candidates.service import CandidateScanService
from astock.schemas.candidate_promotion import SeedPromotionRequest
from astock.schemas.research_seeds import ResearchSeedOrigin
from tests.unit.test_candidate_promotion import (
    NOW,
    PROJECT_ROOT,
    _FakeCandidateService,
    _FakePromotionService,
    _register_single_seed_report,
    _runtime,
    _seed,
)


@pytest.mark.parametrize(
    ("control_case", "origins", "reason_codes", "expected_prior"),
    [
        (
            "operating_evidence_event_without_price_or_volume_gate",
            [ResearchSeedOrigin.EXPERT_SKILL],
            [
                "CONTROL:OPERATING_EVIDENCE",
                "CONTROL:PRICE_CHANGE_LT_15PCT",
                "CONTROL:VOLUME_RATIO_LT_1_5X",
                "DISCOVERY_CHANNEL:EVENT",
                "VERIFIED_DISCOVERY_THESIS",
            ],
            True,
        ),
        (
            "operating_evidence_quality_without_price_or_volume_gate",
            [ResearchSeedOrigin.EXPERT_SKILL],
            [
                "CONTROL:OPERATING_EVIDENCE",
                "CONTROL:PRICE_CHANGE_LT_15PCT",
                "CONTROL:VOLUME_RATIO_LT_1_5X",
                "DISCOVERY_CHANNEL:QUALITY_VALUE",
                "VERIFIED_DISCOVERY_THESIS",
            ],
            True,
        ),
        (
            "operating_evidence_bottleneck_without_price_or_volume_gate",
            [ResearchSeedOrigin.MARKET],
            [
                "CONTROL:OPERATING_EVIDENCE",
                "CONTROL:PRICE_CHANGE_LT_15PCT",
                "CONTROL:VOLUME_RATIO_LT_1_5X",
                "DISCOVERY_CHANNEL:BOTTLENECK",
                "VERIFIED_DISCOVERY_THESIS",
            ],
            True,
        ),
        (
            "operating_evidence_cycle_without_price_or_volume_gate",
            [ResearchSeedOrigin.MARKET],
            [
                "CONTROL:OPERATING_EVIDENCE",
                "CONTROL:PRICE_CHANGE_LT_15PCT",
                "CONTROL:VOLUME_RATIO_LT_1_5X",
                "DISCOVERY_CHANNEL:CYCLE",
                "VERIFIED_DISCOVERY_THESIS",
            ],
            True,
        ),
        (
            "pure_heat_without_business_support",
            [ResearchSeedOrigin.EXPERT_SKILL],
            ["CONTROL:PURE_HEAT_ONLY", "DISCOVERY_CHANNEL:EVENT"],
            False,
        ),
        (
            "duplicate_skill_support_without_business_support",
            [ResearchSeedOrigin.EXPERT_SKILL],
            ["CONTROL:DUPLICATE_SKILL_SUPPORT", "DISCOVERY_METHOD:EventToAlphaSkill"],
            False,
        ),
        (
            "same_source_news_duplicates_without_business_support",
            [ResearchSeedOrigin.EXPERT_SKILL],
            ["CONTROL:SAME_SOURCE_DUPLICATES", "DISCOVERY_CHANNEL:BOTTLENECK"],
            False,
        ),
        (
            "theme_membership_without_company_exposure",
            [ResearchSeedOrigin.MARKET],
            ["CONTROL:NO_COMPANY_BUSINESS_EXPOSURE", "DISCOVERY_THESIS:test"],
            False,
        ),
        (
            "partial_data_stays_unverified",
            [ResearchSeedOrigin.EXPERT_SKILL],
            ["CONTROL:PARTIAL_DATA", "DISCOVERY_CHANNEL:QUALITY_VALUE"],
            False,
        ),
        (
            "source_conflict_stays_unverified",
            [ResearchSeedOrigin.EXPERT_SKILL],
            ["CONTROL:SOURCE_CONFLICT", "DISCOVERY_CHANNEL:EVENT"],
            False,
        ),
        (
            "channel_interruption_stays_unverified",
            [ResearchSeedOrigin.EXPERT_SKILL],
            ["CONTROL:CHANNEL_INTERRUPTED", "DISCOVERY_CHANNEL:CYCLE"],
            False,
        ),
        (
            "revised_skill_recovery_can_restore_verified_marker",
            [ResearchSeedOrigin.EXPERT_SKILL],
            [
                "CONTROL:REVISED_SKILL_RECOVERY",
                "DISCOVERY_CHANNEL:BOTTLENECK",
                "VERIFIED_DISCOVERY_THESIS",
            ],
            True,
        ),
    ],
)
def test_seed_promotion_candidate_verified_discovery_controls(
    tmp_path: Path,
    control_case: str,
    origins: list[ResearchSeedOrigin],
    reason_codes: list[str],
    expected_prior: bool,
) -> None:
    # These 12 named cases are the WP41 chain controls. Upstream discovery-thesis
    # tests own fact/condition semantics; this test proves the production
    # Seed -> Promotion -> Candidate seam admits only the exact frozen marker.
    assert control_case
    state, objects = _runtime(tmp_path)
    fake_candidates = _FakeCandidateService(state, objects)
    service = _FakePromotionService(
        project_root=PROJECT_ROOT,
        state=state,
        objects=objects,
        reference=cast(Any, object()),
        candidates=cast(CandidateScanService, fake_candidates),
        financial_sources=cast(Any, object()),
        trading_classification=cast(Any, object()),
        cninfo=cast(Any, object()),
    )
    seed = _seed(
        "610001",
        origins=origins,
        reason_codes=reason_codes,
    )
    seed_artifact = _register_single_seed_report(
        state,
        objects,
        seed,
        "research-seeds:verified-discovery-control",
    )

    report = service.promote(
        SeedPromotionRequest(
            seed_report_artifact_id=seed_artifact,
            max_seeds=1,
            live=True,
            created_at=NOW,
        )
    )

    assert report.promoted_company_count == 1
    assert fake_candidates.last_release is not None
    company = fake_candidates.last_release.companies[0]
    assert company.research_seed_origins == sorted(item.value for item in seed.origins)
    assert company.research_seed_reason_codes == sorted(seed.reason_codes)
    assert len(fake_candidates.prior_signals) == int(expected_prior)
    if expected_prior:
        signal = cast(Any, fake_candidates.prior_signals[0])
        assert signal.signal_type.value == "RESEARCH_SEED_PRIOR"
        assert signal.disposition.value == "SUPPORT"
        assert signal.reason_codes.count("VERIFIED_DISCOVERY_THESIS") <= 1
