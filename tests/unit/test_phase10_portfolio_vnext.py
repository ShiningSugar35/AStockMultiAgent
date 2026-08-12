from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.portfolio.service import PortfolioService
from astock.portfolio.vnext import PortfolioVNextService
from astock.schemas.portfolio_vnext import (
    AssetAttributionInput,
    AttributionComponent,
    AttributionResearchLink,
    PortfolioAttributionRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> PortfolioVNextService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return PortfolioVNextService(
        state,
        objects,
        cast(PortfolioService, cast(Any, object())),
        PROJECT_ROOT,
    )


def test_phase10_attribution_reconciles_and_never_mutates_execution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = PortfolioAttributionRequest(
        portfolio_id="portfolio:phase10",
        period_start=NOW,
        period_end=NOW + timedelta(days=20),
        assets=[
            AssetAttributionInput(
                company_id="600001",
                beginning_weight=0.4,
                realized_return=0.12,
                benchmark_return=0.04,
                sector_contribution=0.01,
                compact_factor_contribution=0.02,
                timing_contribution=-0.005,
                implementation_cost_return=0.002,
                research_links=AttributionResearchLink(
                    research_memo_id="memo:600001",
                    skill_ids=["IndustryBottleneckSkill"],
                    specialist_delta_ids=["delta:600001"],
                    created_at=NOW,
                ),
                created_at=NOW,
            ),
            AssetAttributionInput(
                company_id="600002",
                beginning_weight=0.3,
                realized_return=-0.01,
                benchmark_return=0.04,
                sector_contribution=-0.01,
                compact_factor_contribution=0.0,
                timing_contribution=0.003,
                implementation_cost_return=0.001,
                research_links=AttributionResearchLink(created_at=NOW),
                created_at=NOW,
            ),
        ],
        created_at=NOW,
    )

    report = service.attribute(request)

    component_total = sum(report.component_totals.values())
    assert component_total == pytest.approx(report.realized_excess_return)
    assert report.total_residual == pytest.approx(0.0, abs=1e-12)
    assert report.component_totals[AttributionComponent.IMPLEMENTATION_COST] < 0
    assert report.research_attribution.research_memo_contributions["memo:600001"] != 0
    assert not report.causal_credit_claimed
    assert not report.automatic_skill_modification_allowed
    assert not report.paper_ledger_write_allowed
    assert not report.broker_execution_allowed
    assert service.audit(report.report_id)["status"] == "PASS"


def test_phase10_liquidity_cost_is_explicit_and_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)

    estimate = service._liquidity_estimate(
        company_id="600001",
        avg_volume=2_000_000,
        avg_amount_fen=200_000_000,
        range_fraction=0.03,
        notional_fen=50_000_000,
        participation_cap=0.10,
        round_trip=True,
    )

    assert estimate.days_to_liquidate == pytest.approx(2.5)
    assert estimate.estimated_slippage_bps is not None
    assert estimate.estimated_slippage_bps > 0
    assert estimate.estimated_round_trip_cost_fen is not None
    assert estimate.estimated_round_trip_cost_fen > 0
    assert "POSITION_EXCEEDS_ONE_DAY_PARTICIPATION_CAP" in estimate.warning_codes
