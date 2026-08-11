from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from astock.committee.config import load_committee_rules
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.service import PortfolioService, _Aligned, _History
from astock.schemas.market import Market
from astock.schemas.portfolio import (
    PortfolioAllocationMethod,
    PortfolioAnalysisRequest,
    PortfolioConstructionRequest,
    PortfolioHoldingInput,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> PortfolioService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    reference = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    return PortfolioService(
        state,
        objects,
        reference,
        load_committee_rules(PROJECT_ROOT / "configs" / "committee_rules.yaml"),
    )


def _aligned() -> _Aligned:
    returns = np.array(
        [
            [0.010, 0.004, -0.002, 0.003],
            [-0.005, 0.002, 0.003, 0.001],
            [0.007, -0.003, 0.001, 0.004],
            [0.002, 0.005, -0.004, 0.002],
            [-0.004, -0.001, 0.005, 0.003],
            [0.006, 0.003, 0.002, -0.002],
        ],
        dtype=float,
    )
    covariance = np.cov(returns, rowvar=False)
    correlation = np.corrcoef(returns, rowvar=False)
    return _Aligned(
        company_ids=["600001", "600002", "600003", "600004"],
        asset_returns=returns,
        benchmark_returns=np.array([0.004, -0.002, 0.003, 0.001, -0.001, 0.002]),
        common_session_count=len(returns),
        covariance=np.asarray(covariance, dtype=float),
        correlation=np.asarray(correlation, dtype=float),
    )


def test_portfolio_request_requires_exactly_one_source() -> None:
    holding = PortfolioHoldingInput(company_id="600001", market=Market.XSHG, weight=0.1)
    with pytest.raises(ValidationError, match="exactly one"):
        PortfolioAnalysisRequest(
            portfolio_id="p",
            as_of=NOW,
            account_id="paper",
            holdings=[holding],
        )
    with pytest.raises(ValidationError, match="exactly one"):
        PortfolioAnalysisRequest(portfolio_id="p", as_of=NOW)


def test_missing_paper_account_returns_structured_needs_info(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = service.analyze(
        PortfolioAnalysisRequest(
            portfolio_id="paper:missing",
            account_id="missing",
            as_of=NOW,
        )
    )

    assert report.status.value == "NEEDS_INFO"
    assert report.assets == []
    assert report.metrics is None
    assert report.warning_codes == ["PORTFOLIO_ACCOUNT_REQUIRED"]
    assert not report.broker_execution_allowed


def test_portfolio_alignment_uses_common_sessions_and_returns() -> None:
    dates = [f"2026-07-{day:02d}" for day in range(1, 8)]
    histories = [
        _History(
            company_id="600001",
            market=Market.XSHG,
            closes_by_date={date: 100.0 + index for index, date in enumerate(dates)},
            latest_close_fen=10_600,
            release_id="release:a",
            release_object_hash="a" * 64,
            cutoff_at=NOW,
        ),
        _History(
            company_id="600002",
            market=Market.XSHG,
            closes_by_date={date: 80.0 + 0.5 * index for index, date in enumerate(dates)},
            latest_close_fen=8_300,
            release_id="release:b",
            release_object_hash="b" * 64,
            cutoff_at=NOW,
        ),
    ]
    benchmark = _History(
        company_id="000300",
        market=Market.INDEX,
        closes_by_date={date: 4000.0 + 5 * index for index, date in enumerate(dates)},
        latest_close_fen=403_000,
        release_id="release:benchmark",
        release_object_hash="c" * 64,
        cutoff_at=NOW,
    )

    aligned = PortfolioService._align(
        histories,
        benchmark,
        lookback_sessions=6,
        minimum_sessions=5,
    )

    assert aligned.common_session_count == 6
    assert aligned.asset_returns.shape == (6, 2)
    assert aligned.covariance.shape == (2, 2)
    assert np.isfinite(aligned.covariance).all()
    assert np.isfinite(aligned.correlation).all()


def test_robust_weight_models_are_long_only_and_sum_to_one() -> None:
    aligned = _aligned()

    min_var = PortfolioService._minimum_variance_weights(aligned.covariance)
    hrp = PortfolioService._hierarchical_weights(aligned)

    for weights in (min_var, hrp):
        assert np.all(weights >= -1e-12)
        assert float(weights.sum()) == pytest.approx(1.0, abs=1e-9)


def test_constraints_cap_single_group_and_total_exposure(tmp_path: Path) -> None:
    service = _service(tmp_path)
    companies = [f"60000{index}" for index in range(1, 7)]
    groups = {
        "600001": "A",
        "600002": "A",
        "600003": "A",
        "600004": "B",
        "600005": "B",
        "600006": "C",
    }

    weights, binding = service._constrain_scores(
        companies,
        np.ones(len(companies)),
        groups,
        target=0.8,
    )

    assert sum(weights.values()) <= 0.8 + 1e-9
    assert max(weights.values()) <= 0.1 + 1e-9
    group_a = sum(weights[item] for item in companies if groups[item] == "A")
    assert group_a <= 0.25 + 1e-9
    assert "MAX_SINGLE_POSITION_BOUND" in binding


def test_all_four_methods_are_proposed_with_equal_weight_default(tmp_path: Path) -> None:
    service = _service(tmp_path)
    aligned = _aligned()
    groups = {
        "600001": "A",
        "600002": "B",
        "600003": "C",
        "600004": "D",
    }

    proposals = service._proposals(aligned, groups, target=0.4)

    assert [item.method for item in proposals] == list(PortfolioAllocationMethod)
    assert proposals[0].method is PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED
    for proposal in proposals:
        assert sum(proposal.weights.values()) == pytest.approx(0.4, abs=1e-8)
        assert proposal.cash_weight == pytest.approx(0.6, abs=1e-8)
        assert proposal.max_single_weight <= 0.1 + 1e-9
        assert proposal.ex_ante_annualized_volatility >= 0


def test_construction_schema_requires_unique_approved_candidate_inputs() -> None:
    payload = {
        "portfolio_id": "portfolio:test",
        "as_of": NOW,
        "candidates": [
            {
                "company_id": "600001",
                "classified_protocol_artifact_id": "ClassifiedTradeProtocol:a",
                "risk_group": "A",
            },
            {
                "company_id": "600001",
                "classified_protocol_artifact_id": "ClassifiedTradeProtocol:b",
                "risk_group": "B",
            },
        ],
    }
    with pytest.raises(ValidationError, match="unique"):
        PortfolioConstructionRequest.model_validate(payload)
