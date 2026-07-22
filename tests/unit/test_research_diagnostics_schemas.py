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
