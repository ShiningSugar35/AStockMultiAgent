"""Versioned research allocation context and one post-solver constraint boundary.

Weights are fractions of pre-trade NAV; explicit one-off implementation costs
consume cash once. This module cannot submit orders or write an account ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import fsum, isfinite, sqrt
from typing import Literal

import numpy as np

from astock.core.hashing import content_hash
from astock.portfolio.analytics import AlignedPortfolioData, constrain_scores

ReturnBasis = Literal["PRICE_ONLY", "PARTIAL_GROSS_TOTAL_RETURN", "VERIFIED_GROSS_TOTAL_RETURN"]


@dataclass(frozen=True, slots=True)
class AllocationContext:
    aligned: AlignedPortfolioData
    current_weights: tuple[float, ...]
    groups: tuple[tuple[str, ...], ...]
    group_caps: dict[str, float]
    target_exposure: float
    max_single: float
    can_buy: tuple[bool, ...]
    can_sell: tuple[bool, ...]
    one_way_costs: tuple[float, ...]
    min_cash: float = 0.0
    max_turnover: float | None = None
    return_basis: ReturnBasis = "PRICE_ONLY"
    returns_include_implementation_costs: bool = False
    annualization: int = 252
    tolerance: float = 1e-8
    schema_version: str = "portfolio-allocation-context-v1"

    def __post_init__(self) -> None:
        n = len(self.aligned.company_ids)
        if not 1 <= n <= 20 or len(set(self.aligned.company_ids)) != n:
            raise ValueError("allocation requires one to twenty unique securities")
        vectors = (
            self.current_weights, self.groups, self.can_buy, self.can_sell, self.one_way_costs
        )
        if any(len(values) != n for values in vectors):
            raise ValueError("allocation vectors must share the exact security order")
        if any(
            type(value) is not bool for values in (self.can_buy, self.can_sell) for value in values
        ):
            raise ValueError("trade eligibility must be explicit booleans")
        numbers = (*self.current_weights, *self.one_way_costs,
                   self.target_exposure, self.max_single, self.min_cash, *self.group_caps.values())
        if any(not isfinite(value) or not 0 <= value <= 1 for value in numbers):
            raise ValueError("allocation weights, costs and caps must be finite fractions")
        if fsum(self.current_weights) > 1 + self.tolerance:
            raise ValueError("current portfolio exceeds NAV")
        if self.max_turnover is not None and (
            not isfinite(self.max_turnover) or not 0 <= self.max_turnover <= 2
        ):
            raise ValueError("total L1 turnover must be between zero and two")
        if not 1e-10 <= self.tolerance <= 1e-6 or self.annualization != 252:
            raise ValueError("allocation supports daily returns and bounded numerical tolerance")
        if self.return_basis not in {
            "PRICE_ONLY", "PARTIAL_GROSS_TOTAL_RETURN", "VERIFIED_GROSS_TOTAL_RETURN"
        }:
            raise ValueError("allocation return basis must be explicit")
        if self.returns_include_implementation_costs and any(self.one_way_costs):
            raise ValueError("implementation costs would be counted twice")
        if len(self.group_caps) > 60 or any(
            not names or len(names) != len(set(names))
            or any(not name.strip() or name not in self.group_caps for name in names)
            for names in self.groups
        ):
            raise ValueError("allocation requires bounded, explicit group memberships and caps")
        matrix = np.asarray(self.aligned.asset_returns)
        if (
            matrix.ndim != 2 or matrix.shape[1] != n
            or not 2 <= matrix.shape[0] <= 504
            or matrix.shape[0] != self.aligned.common_session_count
            or not np.isfinite(matrix).all() or np.any(matrix < -1)
            or self.aligned.covariance.shape != (n, n)
            or not np.isfinite(self.aligned.covariance).all()
            or not np.allclose(self.aligned.covariance, self.aligned.covariance.T, atol=1e-12)
            or np.linalg.eigvalsh(self.aligned.covariance).min() < -1e-10
            or self.aligned.correlation.shape != (n, n)
            or not np.isfinite(self.aligned.correlation).all()
            or not np.allclose(self.aligned.correlation, self.aligned.correlation.T, atol=1e-12)
            or not np.allclose(np.diag(self.aligned.correlation), 1.0, atol=1e-12)
            or np.max(np.abs(self.aligned.correlation)) > 1 + 1e-12
            or np.linalg.eigvalsh(self.aligned.correlation).min() < -1e-10
        ):
            raise ValueError(
                "allocation requires finite aligned returns and PSD covariance/correlation"
            )

    @property
    def fingerprint(self) -> str:
        return content_hash({
            "version": self.schema_version, "companies": self.aligned.company_ids,
            "returns": self.aligned.asset_returns.tolist(),
            "covariance": self.aligned.covariance.tolist(),
            "correlation": self.aligned.correlation.tolist(),
            "held": self.current_weights, "groups": self.groups, "caps": self.group_caps,
            "target": self.target_exposure, "single": self.max_single, "cash": self.min_cash,
            "buy": self.can_buy, "sell": self.can_sell, "costs": self.one_way_costs,
            "turnover": self.max_turnover, "basis": self.return_basis,
            "costs_in_returns": self.returns_include_implementation_costs,
            "annualization": self.annualization, "tolerance": self.tolerance,
        })

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        lower = np.array([0.0 if sell else held
                          for sell, held in zip(self.can_sell, self.current_weights, strict=True)])
        upper = np.array([self.max_single if buy else min(self.max_single, held)
                          for buy, held in zip(self.can_buy, self.current_weights, strict=True)])
        return lower, upper


@dataclass(frozen=True, slots=True)
class AllocationResult:
    method: str
    status: str
    weights: tuple[float, ...] = ()
    binding_codes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    optimality_claim: bool = False
    max_constraint_violation: float | None = None
    turnover: float | None = None
    one_off_cost_fraction: float | None = None
    cash_after_cost: float | None = None
    annualized_volatility: float | None = None
    target_deviation: float | None = None
    objective_value: float | None = None
    context_fingerprint: str | None = None
    schema_version: str = "portfolio-allocation-result-v1"


def validate_allocation(
    context: AllocationContext, weights: np.ndarray, *, method: str,
    status: str, optimality_claim: bool = False, objective_value: float | None = None,
) -> AllocationResult:
    """The same exit verifies heuristic, solver and implementation projections."""
    vector = np.asarray(weights, dtype=float)
    if objective_value is not None and not isfinite(objective_value):
        raise ValueError("allocator objective must be finite")
    n = len(context.current_weights)
    if vector.shape != (n,) or not np.isfinite(vector).all():
        raise ValueError("allocator returned invalid final weights")
    lower, upper = context.bounds()
    delta = np.abs(vector - np.asarray(context.current_weights))
    turnover = float(delta.sum())
    cost = float(delta @ np.asarray(context.one_way_costs))
    invested = float(vector.sum())
    residuals: dict[str, float] = {
        "SINGLE_OR_TRADE_ELIGIBILITY": max(
            float(np.max(lower-vector)), float(np.max(vector-upper))
        ),
        "TARGET_EXPOSURE": invested - context.target_exposure,
        "CASH_AFTER_COST": invested + cost + context.min_cash - 1,
    }
    for group, cap in context.group_caps.items():
        residuals["GROUP:" + group] = fsum(
            float(weight) for weight, memberships in zip(vector, context.groups, strict=True)
            if group in memberships
        ) - cap
    if context.max_turnover is not None:
        residuals["TOTAL_TURNOVER"] = turnover - context.max_turnover
    maximum = max(0.0, *residuals.values())
    if maximum > context.tolerance:
        return AllocationResult(
            method=method, status="INFEASIBLE", max_constraint_violation=maximum,
            reason_codes=tuple(sorted(
                key for key, value in residuals.items() if value > context.tolerance
            )),
        )
    # Do not clip/renormalize a solver's weights here: that changes its objective.
    variance = float(vector @ context.aligned.covariance @ vector)
    return AllocationResult(
        method=method, status=status, weights=tuple(float(v) for v in vector),
        binding_codes=tuple(sorted(
            key for key, value in residuals.items() if abs(value) <= context.tolerance
        )),
        optimality_claim=optimality_claim, max_constraint_violation=maximum,
        turnover=turnover, one_off_cost_fraction=cost,
        cash_after_cost=max(0.0, 1-invested-cost),
        annualized_volatility=sqrt(max(0.0, variance) * context.annualization),
        target_deviation=context.target_exposure-invested,
        objective_value=objective_value, context_fingerprint=context.fingerprint,
    )


def project_scores(
    context: AllocationContext, scores: np.ndarray, *, method: str
) -> AllocationResult:
    """Compatibility adapter; projection is never a risk-optimality assertion."""
    vector = np.asarray(scores, dtype=float)
    if vector.shape != (len(context.current_weights),) or not np.isfinite(vector).all():
        raise ValueError("portfolio allocator returned invalid scores")
    simple = (
        all(context.can_buy) and all(context.can_sell) and not any(context.current_weights)
        and not any(context.one_way_costs) and context.max_turnover is None
        and all(len(groups) == 1 for groups in context.groups)
        and len(set(context.group_caps.values())) == 1
    )
    if simple:
        weights, legacy_binding = constrain_scores(
            context.aligned.company_ids, vector,
            dict(zip(
                context.aligned.company_ids, (groups[0] for groups in context.groups), strict=True
            )),
            target=min(context.target_exposure, 1-context.min_cash),
            max_single=context.max_single, max_group=next(iter(context.group_caps.values())),
        )
        result = validate_allocation(
            context, np.array([weights[key] for key in context.aligned.company_ids]),
            method=method, status="PROJECTED_HEURISTIC",
        )
        return replace(result, reason_codes=("SCORE_PROJECTION_NOT_RISK_OPTIMAL",),
                       binding_codes=tuple(sorted({*result.binding_codes, *legacy_binding})))
    lower, upper = context.bounds()
    if np.any(lower > upper):
        return AllocationResult(
            method=method, status="INFEASIBLE", reason_codes=("LOCKED_POSITION_EXCEEDS_CAP",)
        )
    # A bounded linear projection with 3n variables: weights, |change| and
    # |distance from score target|. No second portfolio engine or ledger.
    from scipy.optimize import linprog

    n = len(vector)
    vector = np.maximum(vector, 0)
    if not np.any(vector):
        vector = np.ones(n)
    vector = vector / vector.max()
    desired = vector / vector.sum() * context.target_exposure
    objective = np.concatenate((np.zeros(n), np.full(n, 1e-9), np.ones(n)))
    rows, rhs = [], []
    for i in range(n):
        for sign in (-1.0, 1.0):
            change = np.zeros(3*n)
            change[i], change[n+i] = sign, -1
            rows.append(change)
            rhs.append(sign * context.current_weights[i])
            distance = np.zeros(3*n)
            distance[i], distance[2*n+i] = sign, -1
            rows.append(distance)
            rhs.append(sign * desired[i])
    budget = np.zeros(3*n)
    budget[:n] = 1
    rows.append(budget.copy())
    rhs.append(context.target_exposure)
    budget[n:2*n] = context.one_way_costs
    rows.append(budget)
    rhs.append(1-context.min_cash)
    for group, cap in context.group_caps.items():
        row = np.zeros(3*n)
        row[:n] = [float(group in memberships) for memberships in context.groups]
        rows.append(row)
        rhs.append(cap)
    if context.max_turnover is not None:
        row = np.zeros(3*n)
        row[n:2*n] = 1
        rows.append(row)
        rhs.append(context.max_turnover)
    result = linprog(
        objective, A_ub=np.array(rows), b_ub=np.array(rhs),
        bounds=[*zip(lower, upper, strict=True), *[(0, None)]*(2*n)],
        method="highs-ds",
        options={"time_limit": 2.0, "maxiter": 2000, "dual_feasibility_tolerance": 1e-8,
                 "primal_feasibility_tolerance": 1e-8},
    )
    if not result.success:
        return AllocationResult(
            method=method, status="INFEASIBLE" if result.status == 2 else "UNAVAILABLE",
                                reason_codes=("BOUNDED_PROJECTION_UNSOLVED",))
    validated = validate_allocation(
        context, result.x[:n], method=method, status="PROJECTED_HEURISTIC",
    )
    return replace(validated, reason_codes=tuple(sorted({
        *validated.reason_codes, "SCORE_PROJECTION_NOT_RISK_OPTIMAL",
    })))


def revalidate_implementation(
    context: AllocationContext, original: AllocationResult, projected_weights: tuple[float, ...],
    *, observed_current_weights: tuple[float, ...],
) -> AllocationResult:
    """Validate weights after the existing lot/fee/T+1/no-trade-band path.

    The caller supplies that path's result, not orders. Holding drift requires a
    fresh context. Changed weights always lose the solver's original objective.
    """
    if original.context_fingerprint != context.fingerprint:
        return AllocationResult(method=original.method, status="STALE_CONTEXT",
                                reason_codes=("ALLOCATION_CONTEXT_CHANGED",))
    if len(observed_current_weights) != len(context.current_weights) or any(
        abs(a-b) > context.tolerance or not isfinite(a)
        for a, b in zip(observed_current_weights, context.current_weights, strict=True)
    ):
        return AllocationResult(method=original.method, status="STALE_CONTEXT",
                                reason_codes=("CURRENT_HOLDINGS_CHANGED",))
    if not original.weights:
        raise ValueError("cannot project an unsolved allocation")
    changed = tuple(projected_weights) != original.weights
    return validate_allocation(
        context, np.asarray(projected_weights), method=original.method,
        status="IMPLEMENTATION_PROJECTED" if changed else original.status,
        optimality_claim=original.optimality_claim and not changed,
        objective_value=None if changed else original.objective_value,
    )
