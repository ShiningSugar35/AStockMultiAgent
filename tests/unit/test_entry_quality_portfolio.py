from __future__ import annotations

from decimal import Decimal

from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.schemas.entry_quality import EntryQualityState
from astock.schemas.full_research import (
    CandidateRankingEntry,
    FactorSnapshot,
    RecommendationValuationSnapshot,
    ValuationScenario,
)


def _factor() -> FactorSnapshot:
    return FactorSnapshot(
        instrument_id="600001",
        value=Decimal("0.5"),
        quality=Decimal("0.5"),
        growth=Decimal("0.5"),
        momentum=Decimal("0.5"),
        low_volatility=Decimal("0.5"),
        liquidity=Decimal("0.5"),
        size=Decimal("0.5"),
        earnings_revision=Decimal("0.5"),
        profitability=Decimal("0.5"),
        crowding=Decimal("0.5"),
    )


def _candidate(entry_state: EntryQualityState | None) -> CandidateRankingEntry:
    return CandidateRankingEntry(
        instrument_id="600001",
        industry_id="INDUSTRY-A",
        expected_return=Decimal("0.20"),
        downside=Decimal("0.15"),
        quality=Decimal("0.80"),
        valuation=Decimal("0.25"),
        catalyst=Decimal("0.50"),
        macro_fit=Decimal("0.60"),
        industry_fit=Decimal("1"),
        entry_quality_state=entry_state,
        entry_quality_score=Decimal("0.40") if entry_state else None,
        entry_timing_risk=entry_state is EntryQualityState.EXTENDED,
        momentum=Decimal("0.50"),
        liquidity=Decimal("0.80"),
        accounting_risk=Decimal("0.10"),
        governance_risk=Decimal("0.10"),
        evidence_confidence=Decimal("0.90"),
        eligible=True,
        factor_snapshot=_factor(),
    )


def _valuation() -> RecommendationValuationSnapshot:
    return RecommendationValuationSnapshot(
        instrument_id="600001",
        method_family="MULTI_LENS",
        methods=("DCF", "PE"),
        current_price=Decimal("10"),
        scenarios=(
            ValuationScenario(
                scenario="BEAR",
                per_share_value=Decimal("8"),
                probability=Decimal("0.2"),
                expected_return=Decimal("-0.2"),
            ),
            ValuationScenario(
                scenario="BASE",
                per_share_value=Decimal("12"),
                probability=Decimal("0.5"),
                expected_return=Decimal("0.2"),
            ),
            ValuationScenario(
                scenario="BULL",
                per_share_value=Decimal("15"),
                probability=Decimal("0.3"),
                expected_return=Decimal("0.5"),
            ),
        ),
        expected_return_mean=Decimal("0.21"),
        expected_return_downside=Decimal("-0.2"),
        margin_of_safety=Decimal("0.1666666667"),
        source_artifact_ids=("valuation:test",),
        source_object_hashes=("a" * 64,),
    )


def test_entry_quality_only_scales_already_eligible_initial_weight() -> None:
    common = {
        "capital": Decimal("1000000"),
        "valuations": {"600001": _valuation()},
        "max_single": Decimal("0.25"),
        "max_industry": Decimal("0.40"),
        "lot": 100,
        "transaction_cost_rate": Decimal("0"),
        "slippage_rate": Decimal("0"),
        "entry_quality_multipliers": {"EXTENDED": Decimal("0.50")},
    }
    base_positions, _, _ = FullResearchRecommendationService._build_positions(
        eligible=(_candidate(None),),
        **common,
    )
    extended_positions, _, _ = FullResearchRecommendationService._build_positions(
        eligible=(_candidate(EntryQualityState.EXTENDED),),
        **common,
    )

    assert base_positions[0].target_weight == Decimal("0.25")
    assert extended_positions[0].target_weight == Decimal("0.125")
    assert extended_positions[0].target_weight < base_positions[0].target_weight
