from __future__ import annotations

from math import fsum, isfinite, sqrt
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from astock.committee.config import load_committee_rules
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.allocators import PortfolioAllocatorPlugin, PortfolioAllocatorRegistry
from astock.portfolio.analytics import (
    AlignedPortfolioData,
    constrain_scores,
    portfolio_risk_statistics,
)
from astock.portfolio.service import PortfolioService
from astock.schemas.portfolio import PortfolioAllocationMethod

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _aligned(returns: list[float], asset_count: int = 1) -> AlignedPortfolioData:
    return AlignedPortfolioData(
        company_ids=[f"60000{index + 1}" for index in range(asset_count)],
        asset_returns=np.repeat(np.array(returns, dtype=float)[:, None], asset_count, axis=1),
        benchmark_returns=np.linspace(-0.01, 0.01, len(returns)),
        common_session_count=len(returns),
        covariance=np.eye(asset_count) * 0.01,
        correlation=np.eye(asset_count),
    )


@pytest.mark.parametrize(
    ("returns", "expected"),
    [
        ([-0.1, -0.1], 0.19),
        ([-0.1, 1.0 / 9.0], 0.1),
        ([-0.1, 0.2, -0.1], 0.1),
        ([0.1, -0.1, -0.2], 0.28),
        ([0.1, 0.2], 0.0),
        ([0.0, 0.0], 0.0),
        ([-1.0, 0.0], 1.0),
    ],
)
def test_drawdown_uses_initial_nav_and_later_peaks(
    returns: list[float], expected: float
) -> None:
    stats = portfolio_risk_statistics(_aligned(returns), np.array([1.0]))

    assert stats.max_drawdown == pytest.approx(expected)
    assert isfinite(stats.historical_cdar_95)


def test_initial_nav_does_not_add_a_cdar_or_return_observation() -> None:
    # Twenty descending NAV observations have one observation in the 95% tail.
    # Adding the initial NAV as a 21st observation would incorrectly select two.
    stats = portfolio_risk_statistics(_aligned([-0.01] * 20), np.array([1.0]))
    expected_loss = 1.0 - 0.99**20

    assert stats.max_drawdown == pytest.approx(expected_loss)
    assert stats.historical_cdar_95 == pytest.approx(expected_loss)
    assert stats.annualized_return == pytest.approx(0.99**252 - 1.0)
    assert stats.historical_var_95 == pytest.approx(0.01)
    assert stats.historical_cvar_95 == pytest.approx(0.01)


def test_cash_is_zero_return_and_does_not_renormalize_the_risky_sleeve() -> None:
    stats = portfolio_risk_statistics(_aligned([-0.1, -0.1]), np.array([0.5]))

    # The half-cash portfolio loses 5% in each period: NAV 1 -> .95 -> .9025.
    assert stats.max_drawdown == pytest.approx(0.0975)
    assert stats.historical_cdar_95 == pytest.approx(0.0975)
    assert stats.annualized_return == pytest.approx(0.95**252 - 1.0)
    assert stats.historical_var_95 == pytest.approx(0.05)
    assert stats.historical_cvar_95 == pytest.approx(0.05)
    assert stats.annualized_tracking_error == pytest.approx(sqrt(0.0002 * 252))
    assert stats.concentration_hhi == pytest.approx(1.0)


def test_all_cash_keeps_the_existing_positive_exposure_contract() -> None:
    with pytest.raises(ValueError, match="positive invested exposure"):
        portfolio_risk_statistics(_aligned([-0.1, -0.1]), np.array([0.0]))


@pytest.mark.parametrize("target", [0.0, 0.4, 0.9, 1.0])
def test_partial_zero_scores_never_exceed_target_or_receive_weight(target: float) -> None:
    weights, binding = constrain_scores(
        ["A", "B"], np.array([1.0, 0.0]), {"A": "G1", "B": "G2"},
        target=target, max_single=1.0, max_group=1.0,
    )

    assert weights == pytest.approx({"A": target, "B": 0.0})
    assert fsum(weights.values()) <= target + 1e-12
    assert "CASH_RESIDUAL_CONSTRAINT_BOUND" not in binding


def test_zero_score_stays_zero_after_the_positive_asset_hits_its_cap() -> None:
    weights, binding = constrain_scores(
        ["A", "B"], np.array([1.0, 0.0]), {"A": "G1", "B": "G2"},
        target=0.8, max_single=0.3, max_group=1.0,
    )

    assert weights == pytest.approx({"A": 0.3, "B": 0.0})
    assert 1.0 - fsum(weights.values()) == pytest.approx(0.7)
    assert {"CASH_RESIDUAL_CONSTRAINT_BOUND", "MAX_SINGLE_POSITION_BOUND"} <= binding


@pytest.mark.parametrize("scores", [[0.0, 0.0], [-2.0, 0.0], [-1.0, -2.0]])
def test_all_effective_zero_scores_keep_the_equal_weight_fallback(scores: list[float]) -> None:
    weights, _ = constrain_scores(
        ["A", "B"], np.array(scores), {"A": "G1", "B": "G2"},
        target=0.8, max_single=1.0, max_group=1.0,
    )

    assert weights == pytest.approx({"A": 0.4, "B": 0.4})


def test_negative_score_has_no_exposure_when_a_positive_score_exists() -> None:
    weights, _ = constrain_scores(
        ["A", "B"], np.array([2.0, -1.0]), {"A": "G1", "B": "G2"},
        target=0.4, max_single=1.0, max_group=1.0,
    )

    assert weights == pytest.approx({"A": 0.4, "B": 0.0})


def test_conflicting_position_and_group_caps_leave_cash() -> None:
    weights, binding = constrain_scores(
        ["A", "B"], np.ones(2), {"A": "G", "B": "G"},
        target=0.8, max_single=0.2, max_group=0.25,
    )

    assert fsum(weights.values()) == pytest.approx(0.25)
    assert max(weights.values()) <= 0.2
    assert {
        "CASH_RESIDUAL_CONSTRAINT_BOUND",
        "MAX_SINGLE_POSITION_BOUND",
        "MAX_GROUP_EXPOSURE_BOUND",
    } <= binding


@pytest.mark.parametrize(("max_single", "max_group"), [(0.0, 1.0), (1.0, 0.0)])
def test_zero_capacity_leaves_the_entire_target_unallocated(
    max_single: float, max_group: float
) -> None:
    weights, binding = constrain_scores(
        ["A"], np.array([1.0]), {"A": "G"},
        target=0.8, max_single=max_single, max_group=max_group,
    )

    assert weights == {"A": 0.0}
    assert "CASH_RESIDUAL_CONSTRAINT_BOUND" in binding


def test_no_assets_leave_the_target_in_cash() -> None:
    weights, binding = constrain_scores(
        [], np.array([]), {}, target=0.8, max_single=0.1, max_group=0.25,
    )

    assert weights == {}
    assert binding == {"CASH_RESIDUAL_CONSTRAINT_BOUND"}


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_scores_are_rejected_instead_of_replaced_with_equal_weights(
    invalid: float,
) -> None:
    with pytest.raises(ValueError, match="scores must be finite"):
        constrain_scores(
            ["A", "B"], np.array([1.0, invalid]), {"A": "G1", "B": "G2"},
            target=0.4, max_single=1.0, max_group=1.0,
        )


@pytest.mark.parametrize("field", ["target", "max_single", "max_group"])
@pytest.mark.parametrize("invalid", [-0.01, 1.01, float("nan"), float("inf"), float("-inf")])
def test_invalid_budget_or_cap_is_rejected(field: str, invalid: float) -> None:
    bounds = {"target": 0.8, "max_single": 1.0, "max_group": 1.0}
    bounds[field] = invalid

    with pytest.raises(ValueError, match="finite fractions"):
        constrain_scores(["A"], np.array([1.0]), {"A": "G"}, **bounds)


@pytest.mark.parametrize("scores", [np.ones((2, 1)), np.array([1.0]), np.ones(3)])
def test_score_shape_must_bind_exactly_to_assets(scores: np.ndarray) -> None:
    with pytest.raises(ValueError, match="match the company vector"):
        constrain_scores(
            ["A", "B"], scores, {"A": "G1", "B": "G2"},
            target=0.4, max_single=1.0, max_group=1.0,
        )


def test_duplicate_asset_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="unique company ids"):
        constrain_scores(
            ["A", "A"], np.ones(2), {"A": "G"},
            target=0.4, max_single=1.0, max_group=1.0,
        )


def test_missing_asset_group_is_rejected() -> None:
    with pytest.raises(ValueError, match="group for every company"):
        constrain_scores(
            ["A", "B"], np.ones(2), {"A": "G"},
            target=0.4, max_single=1.0, max_group=1.0,
        )


def test_large_finite_scores_do_not_overflow_or_lose_their_relative_weights() -> None:
    with np.errstate(over="raise", invalid="raise"):
        weights, _ = constrain_scores(
            ["A", "B"], np.array([1.2e308, 8e307]), {"A": "G1", "B": "G2"},
            target=0.9, max_single=1.0, max_group=1.0,
        )

    assert weights == pytest.approx({"A": 0.54, "B": 0.36})


def test_tiny_positive_score_can_receive_residual_after_the_largest_score_is_capped() -> None:
    weights, _ = constrain_scores(
        ["A", "B"], np.array([1e308, 1e-308]), {"A": "G1", "B": "G2"},
        target=0.2, max_single=0.1, max_group=1.0,
    )

    assert weights == pytest.approx({"A": 0.1, "B": 0.1})


@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    raw_scores=st.lists(
        st.one_of(st.just(0.0), st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)),
        min_size=1,
        max_size=12,
    ),
    target=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    max_single=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    max_group=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
)
def test_allocation_invariants_and_reproducibility(
    raw_scores: list[float], target: float, max_single: float, max_group: float
) -> None:
    ids = [f"A{index}" for index in range(len(raw_scores))]
    groups = {item: f"G{index % 3}" for index, item in enumerate(ids)}
    scores = np.array(raw_scores)
    original = scores.copy()
    arguments = {"target": target, "max_single": max_single, "max_group": max_group}

    weights, binding = constrain_scores(ids, scores, groups, **arguments)

    assert (weights, binding) == constrain_scores(ids, scores, groups, **arguments)
    np.testing.assert_array_equal(scores, original)
    assert set(weights) == set(ids)
    assert all(isfinite(value) and 0.0 <= value <= max_single + 1e-10 for value in weights.values())
    assert fsum(weights.values()) <= target + 1e-10
    for group in set(groups.values()):
        assert fsum(weights[item] for item in ids if groups[item] == group) <= max_group + 1e-10
    positive_ids = [item for item, score in zip(ids, raw_scores, strict=True) if score > 0]
    if positive_ids:
        assert all(weights[item] == 0.0 for item in ids if item not in positive_ids)
    else:
        positive_ids = ids
    capacity = fsum(
        min(max_group, max_single * sum(groups[item] == group for item in positive_ids))
        for group in set(groups.values())
    )
    assert fsum(weights.values()) == pytest.approx(min(target, capacity), abs=1e-9)
    if target - fsum(weights.values()) > 1e-10:
        assert "CASH_RESIDUAL_CONSTRAINT_BOUND" in binding


def test_real_proposal_path_keeps_sparse_allocation_below_target_and_retains_cash(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    objects = ObjectStore(tmp_path / "objects")
    reference = MarketReferenceService(
        state, objects, ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    registry = PortfolioAllocatorRegistry()
    for method in PortfolioAllocationMethod:
        registry.register(
            PortfolioAllocatorPlugin(
                method=method,
                build_scores=lambda _aligned: np.array([1.0, 0.0]),
                model_risk_codes=(),
            )
        )
    service = PortfolioService(
        state, objects, reference,
        load_committee_rules(PROJECT_ROOT / "configs" / "committee_rules.yaml"),
        allocator_registry=registry,
    )
    aligned = _aligned([-0.1, 0.1], asset_count=2)

    proposals = service._proposals(
        aligned, {"600001": "G1", "600002": "G2"}, target=0.04,
    )

    assert len(proposals) == len(PortfolioAllocationMethod)
    for proposal in proposals:
        # The old .04 + .04 result passed the <=100% schema but exceeded target.
        assert proposal.weights == pytest.approx({"600001": 0.04, "600002": 0.0})
        assert proposal.cash_weight == pytest.approx(0.96)
        assert fsum(proposal.weights.values()) + proposal.cash_weight == pytest.approx(1.0)
