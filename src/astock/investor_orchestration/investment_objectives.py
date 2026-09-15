"""Lightweight goal/P&L projections over admitted research, not a return forecaster."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from typing import Any

from astock.schemas.full_research import (
    ModelPortfolioAssumptions,
    PortfolioPositionPlan,
    RecommendationValuationSnapshot,
)


def compounded_target(annual_return: Decimal, months: Decimal) -> Decimal:
    """A hypothetical compounded target path; never a forecast of realized return."""
    with localcontext() as ctx:
        ctx.prec = 28
        if annual_return == 0:
            return Decimal("0")
        if months == 12:
            return annual_return
        return (Decimal("1") + annual_return) ** (months / Decimal("12")) - Decimal("1")


def objective_projection(
    assumptions: ModelPortfolioAssumptions,
    positions: Sequence[PortfolioPositionPlan],
    valuations: Mapping[str, RecommendationValuationSnapshot],
) -> dict[str, Any]:
    """Project net scenario P&L and compare with the goal only on a verified common horizon."""
    target = assumptions.target_annual_return
    if target is None:
        return {}  # Preserve old immutable contracts; new intake resolves this explicitly.
    capital = assumptions.capital_rmb
    entry_cost = sum(
        (position.estimated_cost + position.estimated_slippage for position in positions),
        Decimal("0"),
    )
    # Scenario P&L is based on stock notional. Cash receives no fabricated return.
    profit = sum(
        (
            position.reference_price
            * position.target_shares
            * valuations[position.instrument_id].expected_return_mean
            for position in positions
        ),
        Decimal("0"),
    ) - entry_cost
    downside = sum(
        (
            position.reference_price
            * position.target_shares
            * max(
                Decimal("0"),
                -valuations[position.instrument_id].expected_return_downside,
            )
            for position in positions
        ),
        Decimal("0"),
    ) + entry_cost
    expected = profit / capital

    base: dict[str, Any] = {
        "target_annual_return": target,
        "annual_profit_target": capital * target,
        "expected_research_return": expected,
        "expected_research_profit": profit,
        "modeled_downside_loss": downside,
    }
    if not positions:
        return {
            **base,
            "target_horizon_months": None,
            "target_horizon_return": None,
            "target_horizon_profit": None,
            "return_objective_gap": None,
            "objective_status": "NO_ELIGIBLE_POSITIONS",
        }

    horizons = {valuations[position.instrument_id].return_horizon_months for position in positions}
    comparable = None not in horizons and len(horizons) == 1
    months = next(iter(horizons)) if comparable else None
    if months is not None and not (
        Decimal(assumptions.horizon_min_months)
        <= months
        <= Decimal(assumptions.horizon_max_months)
    ):
        comparable = False
        months = None
    if not comparable or months is None:
        return {
            **base,
            "target_horizon_months": None,
            "target_horizon_return": None,
            "target_horizon_profit": None,
            "return_objective_gap": None,
            "objective_status": "HORIZON_NOT_COMPARABLE",
        }

    goal_return = compounded_target(target, months)
    gap = goal_return - expected
    return {
        **base,
        "target_horizon_months": months,
        "target_horizon_return": goal_return,
        "target_horizon_profit": capital * goal_return,
        "return_objective_gap": gap,
        "objective_status": (
            "MEETS_HORIZON_TARGET" if gap <= 0 else "BELOW_HORIZON_TARGET"
        ),
    }


def goal_entry_ceiling(
    annual_target: Decimal | None,
    valuation: RecommendationValuationSnapshot,
    entry_cost_rate: Decimal = Decimal("0"),
) -> Decimal | None:
    """Conditional base-scenario hurdle, separate from valuation and order authority."""
    months = valuation.return_horizon_months
    if annual_target is None or months is None:
        return None
    base = next(value.per_share_value for value in valuation.scenarios if value.scenario == "BASE")
    return base / (
        (Decimal("1") + compounded_target(annual_target, months))
        * (Decimal("1") + entry_cost_rate)
    )
