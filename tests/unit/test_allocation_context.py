"""Negative economic contracts and independent constraint examples."""
from dataclasses import replace

import numpy as np
import pytest

from astock.portfolio.allocation import (
    AllocationContext,
    project_scores,
    revalidate_implementation,
    validate_allocation,
)
from astock.portfolio.allocators import default_portfolio_allocator_registry
from astock.portfolio.analytics import AlignedPortfolioData, hierarchical_risk_weights
from astock.schemas.portfolio import PortfolioAllocationMethod


def context(**changes):
    returns = np.array([[.01, -.02, .003], [-.01, .01, .002], [.02, .005, -.004]]*20)
    aligned = AlignedPortfolioData(
        ["A", "B", "C"], returns, returns[:, 0], 60,
        np.cov(returns, rowvar=False), np.corrcoef(returns, rowvar=False),
    )
    base = AllocationContext(
        aligned=aligned, current_weights=(0, 0, 0),
        groups=(("A",), ("B",), ("C",)), group_caps={"A": 1, "B": 1, "C": 1},
        target_exposure=.8, max_single=.6, can_buy=(True,)*3, can_sell=(True,)*3,
        one_way_costs=(0, 0, 0),
    )
    return replace(base, **changes)


def test_legacy_registry_uses_versioned_context_without_claiming_optimality():
    registry = default_portfolio_allocator_registry()
    result = registry.allocate(PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED, context())
    assert sum(result.weights) == pytest.approx(.8)
    assert result.weights == pytest.approx((.8/3,)*3)
    assert result.status == "PROJECTED_HEURISTIC" and not result.optimality_claim
    assert result.one_off_cost_fraction == 0
    one = context().aligned
    one = replace(
        one, company_ids=["A"], covariance=np.array([[.001]]), correlation=np.ones((1, 1))
    )
    assert hierarchical_risk_weights(one) == pytest.approx([1])


def test_overlapping_groups_locked_stock_and_turnover_share_one_constraint_exit():
    ctx = context(
        current_weights=(.3, .3, 0), can_sell=(False, True, True),
        groups=(("sector-A", "customer-X"), ("sector-B", "customer-X"), ("sector-C",)),
        group_caps={"sector-A": .5, "sector-B": .5, "sector-C": .5, "customer-X": .5},
        max_turnover=.25,
    )
    result = project_scores(ctx, np.ones(3), method="equal")
    assert result.weights and result.weights[0] >= .3-ctx.tolerance
    assert sum(result.weights[:2]) <= .5+ctx.tolerance
    assert result.turnover is not None and result.turnover <= .25+ctx.tolerance
    assert result.target_deviation is not None and result.target_deviation > 0
    assert not result.optimality_claim


def test_cost_reserve_and_minimum_cash_are_not_spent_twice():
    ctx = context(target_exposure=1, min_cash=.2, one_way_costs=(.01,)*3)
    result = project_scores(ctx, np.ones(3), method="equal")
    # One percent one-way cost charged once: invested + .01*invested = .8.
    assert sum(result.weights) == pytest.approx(.8/1.01, abs=1e-8)
    assert result.one_off_cost_fraction == pytest.approx(.008/1.01, abs=1e-8)
    assert result.cash_after_cost == pytest.approx(.2, abs=1e-8)
    with pytest.raises(ValueError, match="twice"):
        replace(ctx, returns_include_implementation_costs=True)


def test_unsellable_breach_is_explicit_and_nan_cannot_become_equal_weight():
    result = project_scores(
        context(current_weights=(.7, 0, 0), can_sell=(False, True, True)),
        np.ones(3), method="equal",
    )
    assert result.status == "INFEASIBLE" and not result.weights
    with pytest.raises(ValueError, match="scores"):
        project_scores(context(), np.array([1, float("nan"), 1]), method="equal")


def test_implementation_projection_recomputes_risk_and_invalidates_objective():
    ctx = context()
    original = validate_allocation(
        ctx, np.array([.3, .3, .2]), method="solver", status="OPTIMAL_RESEARCH",
        optimality_claim=True, objective_value=.01,
    )
    projected = revalidate_implementation(
        ctx, original, (.2, .2, .2), observed_current_weights=(0, 0, 0),
    )
    assert projected.status == "IMPLEMENTATION_PROJECTED"
    assert not projected.optimality_claim and projected.objective_value is None
    assert projected.annualized_volatility != original.annualized_volatility
    assert projected.target_deviation == pytest.approx(.2)
    stale = revalidate_implementation(
        ctx, original, original.weights, observed_current_weights=(.1, 0, 0),
    )
    assert stale.status == "STALE_CONTEXT" and not stale.weights
    changed_context = replace(ctx, min_cash=.3)
    stale = revalidate_implementation(
        changed_context, original, original.weights, observed_current_weights=(0, 0, 0),
    )
    assert stale.status == "STALE_CONTEXT" and "ALLOCATION_CONTEXT_CHANGED" in stale.reason_codes


def test_correlation_is_checked_and_part_of_the_frozen_context():
    ctx = context()
    changed = replace(ctx.aligned, correlation=np.eye(3))
    other = replace(ctx, aligned=changed)
    assert ctx.fingerprint != other.fingerprint
    original = validate_allocation(
        ctx, np.array([.3, .3, .2]), method="solver", status="OPTIMAL_RESEARCH",
        optimality_claim=True,
    )
    result = revalidate_implementation(
        other, original, original.weights, observed_current_weights=(0, 0, 0),
    )
    assert result.status == "STALE_CONTEXT"
    with pytest.raises(ValueError, match="aligned"):
        replace(ctx, aligned=replace(changed, correlation=np.full((3, 3), float("nan"))))


def test_symmetric_unit_diagonal_correlation_still_requires_psd():
    ctx = context()
    # All entries are in range, but the three pairwise claims cannot coexist:
    # this matrix has eigenvalues -0.8, 1.9 and 1.9.
    impossible = np.array([[1, .9, .9], [.9, 1, -.9], [.9, -.9, 1]])
    with pytest.raises(ValueError, match="PSD"):
        replace(ctx, aligned=replace(ctx.aligned, correlation=impossible))
