from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.research import load_research_diagnostic_config
from astock.schemas import (
    DailyTrendDiagnosticRequest,
    Frequency,
    GrowthProbabilityDiagnosticRequest,
    GrowthScenario,
    JuglarCounterEvidenceV1,
    JuglarCycleDimension,
    JuglarCycleStageContractV1,
    JuglarDimensionScoreV1,
    JuglarMigrationSignalV1,
    JuglarStage,
    JuglarStageProbabilityV1,
    QualityStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_diagnostic_config_keeps_daily_and_hourly_rules_independent() -> None:
    config = load_research_diagnostic_config(PROJECT_ROOT / "configs" / "research_diagnostics.yaml")
    assert config.diagnostics_version == "research-diagnostics-v2"
    assert config.daily.frequency is Frequency.D1
    assert config.hourly.frequency is Frequency.H1
    assert config.daily.minimum_bars == 200
    assert config.hourly.minimum_bars == 40
    assert config.daily.drawdown_alert != config.hourly.drawdown_alert


def test_growth_probability_scenarios_must_conserve_without_auto_normalization() -> None:
    scenario = GrowthScenario(
        scenario_id="base",
        probability=Decimal("0.6"),
        annual_growth_rate=Decimal("0.1"),
        duration_years=3,
        driver="synthetic driver",
        failure_condition="synthetic failure",
        evidence_ids=["evidence:1"],
    )
    with pytest.raises(ValidationError, match="sum exactly to one"):
        GrowthProbabilityDiagnosticRequest(
            base_case_id="base:1",
            route_plan_id="route:1",
            scenarios=[
                scenario,
                scenario.model_copy(
                    update={"scenario_id": "upside", "probability": Decimal("0.3")}
                ),
            ],
            consensus_available=False,
            consensus_evidence_ids=[],
        )


def test_daily_diagnostic_rejects_hourly_frequency_at_schema_boundary() -> None:
    with pytest.raises(ValidationError, match="1d"):
        DailyTrendDiagnosticRequest.model_validate(
            {
                "base_case_id": "base:1",
                "route_plan_id": "route:1",
                "frequency": Frequency.H1,
                "quality_report_id": "quality:1",
                "quality_status": QualityStatus.PASS,
                "bar_count": 60,
                "close_vs_ma20": Decimal("0.01"),
                "ma20_slope": Decimal("0.01"),
                "ma60_slope": Decimal("0.01"),
                "drawdown_from_60d_high": Decimal("-0.02"),
                "volume_ratio_20d": Decimal("1"),
                "evidence_ids": ["evidence:1"],
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )


def test_juglar_contract_requires_all_dimensions_and_probability_conservation() -> None:
    timestamp = datetime(2026, 8, 11, tzinfo=UTC)
    evidence_ids = ["evidence:1"]
    dimensions = [
        JuglarDimensionScoreV1(
            dimension=dimension,
            score=0,
            explanation="synthetic evidence",
            evidence_ids=evidence_ids,
        )
        for dimension in JuglarCycleDimension
    ]
    common = {
        "target_company_id": "600001",
        "as_of": timestamp,
        "core_industry": "synthetic industry",
        "industry_stage": JuglarStage.EXPANSION,
        "company_operating_stage": JuglarStage.EXPANSION,
        "stock_pricing_stage": JuglarStage.OVERHEATING,
        "counterevidence": [
            JuglarCounterEvidenceV1(
                counterevidence_id="counter:1",
                statement="synthetic counter evidence",
                evidence_ids=evidence_ids,
            )
        ],
        "migration_signals": [
            JuglarMigrationSignalV1(
                signal_id="migration:1",
                target_stage=JuglarStage.OVERHEATING,
                observable="capex acceleration",
                interpretation="cycle would migrate toward overheating",
                evidence_ids=evidence_ids,
            )
        ],
        "evidence_ids": evidence_ids,
    }
    bad_probabilities = [
        JuglarStageProbabilityV1(stage=stage, probability=Decimal("0.18")) for stage in JuglarStage
    ]
    with pytest.raises(ValidationError, match="probabilities must sum exactly to one"):
        JuglarCycleStageContractV1(
            dimension_scores=dimensions,
            stage_probabilities=bad_probabilities,
            **common,
        )

    duplicate_dimensions = [*dimensions[:-1], dimensions[0]]
    valid_probabilities = [
        JuglarStageProbabilityV1(stage=stage, probability=Decimal("0.2")) for stage in JuglarStage
    ]
    with pytest.raises(ValidationError, match="eight dimensions once"):
        JuglarCycleStageContractV1(
            dimension_scores=duplicate_dimensions,
            stage_probabilities=valid_probabilities,
            **common,
        )
