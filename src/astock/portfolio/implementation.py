"""Read-only transition assessment using the existing account projection and lot bands."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import numpy as np

from astock.core.errors import AStockError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import CanonicalStateProjectionReader
from astock.investor_orchestration.models import PortfolioLane, PositionView
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.portfolio.allocation import (
    AllocationContext,
    ReturnBasis,
    revalidate_implementation,
    validate_allocation,
)
from astock.portfolio.analytics import AlignedPortfolioData
from astock.schemas.knowledge import PositionAction
from astock.schemas.portfolio import (
    PortfolioAllocationConstraints,
    PortfolioAllocationProposal,
    PortfolioConstructionReport,
    PortfolioConstructionRequest,
)
from astock.schemas.portfolio_decision import (
    PortfolioImplementationAssessment,
    PortfolioTransitionRequest,
    PositionTargetBand,
)


class _MissingProof(ValueError):
    pass


@dataclass(frozen=True)
class _Account:
    reader: CanonicalStateProjectionReader
    revision: str
    lane: PortfolioLane
    positions: dict[str, PositionView]
    cash_cny: Decimal


def _account(
    state: StateStore, objects: ObjectStore, request: PortfolioTransitionRequest,
) -> _Account:
    if request.implementation_account_id is None or request.implementation_lane is None:
        raise _MissingProof("CANONICAL_ACCOUNT_BINDING_REQUIRED")
    reader = CanonicalStateProjectionReader(InvestorOrchestrationStore(state.path), objects)
    healthy, reason = reader.bounded_health_check()
    lane = PortfolioLane(request.implementation_lane)
    revision = reader.revision_vector()[lane.value.lower()]
    view = reader.lane_snapshot(
        lane, revision, health_ok=healthy, health_reason=reason, as_of=request.intent.as_of,
    )
    account = request.implementation_account_id
    if account not in view.account_ids or view.audit_status != "PASS":
        raise _MissingProof("CANONICAL_ACCOUNT_UNAVAILABLE")
    cash = view.cash_by_account.get(account)
    if cash is None or any(item.account_id == account for item in view.open_orders):
        raise _MissingProof("KNOWN_CASH_WITHOUT_PENDING_ORDERS_REQUIRED")
    positions = {item.instrument_id: item for item in view.positions if item.account_id == account}
    if any(item.quantity < 0 or item.quantity != item.quantity.to_integral_value()
           for item in positions.values()):
        raise _MissingProof("CANONICAL_INTEGRAL_POSITION_REQUIRED")
    return _Account(reader, revision, lane, positions, cash)


def _construction_request(
    state: StateStore, objects: ObjectStore, report: PortfolioConstructionReport,
) -> PortfolioConstructionRequest:
    ids = [item for item in report.source_artifact_ids
           if item.startswith("PortfolioConstructionRequest:")]
    if len(ids) != 1:
        raise _MissingProof("FROZEN_CONSTRUCTION_CONSTRAINTS_REQUIRED")
    record = state.artifact_record(ids[0])
    if (record is None or record["type"] != "PortfolioConstructionRequest"
            or record["object_hash"] not in report.source_object_hashes
            or not objects.verify(str(record["object_hash"]))):
        raise _MissingProof("CONSTRUCTION_CONSTRAINT_LINEAGE_INVALID")
    raw = objects.get_bytes(str(record["object_hash"]))
    if len(raw) > 262144:
        raise _MissingProof("CONSTRUCTION_CONSTRAINT_BYTE_BUDGET")
    original = PortfolioConstructionRequest.model_validate_json(raw)
    if original.portfolio_id != report.portfolio_id or original.as_of != report.as_of:
        raise _MissingProof("CONSTRUCTION_CONSTRAINT_IDENTITY_MISMATCH")
    return original


def _observed_weights(
    account: _Account, request: PortfolioTransitionRequest, bands: list[PositionTargetBand],
    prices: dict[str, int], expected: dict[str, float],
) -> tuple[float, ...]:
    nav = request.portfolio_nav_fen
    if nav is None:
        raise _MissingProof("EXPLICIT_NAV_REQUIRED")
    identities = {item.instrument_id for item in bands}
    if set(account.positions) - identities:
        raise _MissingProof("ACCOUNT_HAS_UNMODELED_POSITIONS")
    actual_weights: dict[str, float] = {}
    market_value = Decimal(0)
    for band in bands:
        symbol = band.instrument_id.split(":", 1)[1]
        position = account.positions.get(band.instrument_id)
        quantity = int(position.quantity) if position else 0
        if request.current_quantities.get(symbol) != quantity:
            raise _MissingProof("CURRENT_QUANTITY_DRIFT")
        value = Decimal(quantity * prices[symbol])
        actual_weights[symbol] = float(value / nav)
        market_value += value
    if abs(market_value + account.cash_cny*100 - nav) > 1:
        raise _MissingProof("CURRENT_NAV_DRIFT")
    if any(abs(actual_weights[symbol]-expected.get(symbol, 0)) > 1e-6
           for symbol in actual_weights):
        raise _MissingProof("CURRENT_WEIGHT_DRIFT")
    return tuple(actual_weights[symbol] for symbol in sorted(actual_weights))


def _project_bands(
    account: _Account, request: PortfolioTransitionRequest, bands: list[PositionTargetBand],
    prices: dict[str, int],
) -> tuple[dict[str, int], tuple[float, ...]]:
    quantities: dict[str, int] = {}
    assert request.portfolio_nav_fen is not None
    for band in bands:
        symbol = band.instrument_id.split(":", 1)[1]
        current = request.current_quantities[symbol]
        if band.action is PositionAction.HOLD:
            chosen = current
        elif band.target_quantity_min is None or band.target_quantity_max is None:
            raise _MissingProof("EXACT_LOT_PROJECTION_UNAVAILABLE:" + symbol)
        else:
            # Existing band endpoint makes the smallest necessary trade. Never
            # invent a finer lot, a fill price, an order, or an execution approval.
            chosen = (band.target_quantity_min if band.action is PositionAction.ADD
                      else band.target_quantity_max)
        if chosen < current:
            position = account.positions.get(band.instrument_id)
            available = position.available_quantity if position is not None else None
            if available is None:
                raise _MissingProof("SETTLEMENT_AVAILABILITY_UNKNOWN:" + symbol)
            if current-chosen > available:
                raise _MissingProof("T1_AVAILABLE_QUANTITY_EXCEEDED:" + symbol)
        quantities[symbol] = chosen
    weights = tuple(
        quantities[symbol]*prices[symbol]/request.portfolio_nav_fen
        for symbol in sorted(quantities)
    )
    return quantities, weights


def _context(
    original: PortfolioConstructionRequest, request: PortfolioTransitionRequest,
    aligned: AlignedPortfolioData, current: tuple[float, ...],
    target: tuple[float, ...], *, max_total: float, max_single: float, max_group: float,
    return_basis: ReturnBasis,
) -> AllocationContext:
    assumptions = original.allocation_constraints or PortfolioAllocationConstraints()
    ids = aligned.company_ids
    primary = {item.company_id: item.risk_group for item in original.candidates}
    if set(ids) - set(primary):
        raise _MissingProof("ORIGINAL_GROUP_CONTEXT_INCOMPLETE")
    if assumptions.current_weights and any(
        abs(assumptions.current_weights.get(symbol, 0)-held) > 1e-6
        for symbol, held in zip(ids, current, strict=True)
    ):
        raise _MissingProof("ALLOCATION_HOLDING_CONTEXT_CHANGED")
    costs = {item.instrument_id.split(":", 1)[1]: item for item in request.implementation_costs}
    one_way = []
    for symbol, held, desired in zip(ids, current, target, strict=True):
        cost = costs.get(symbol)
        if abs(desired-held) > 1e-12 and (cost is None or not cost.verified):
            raise _MissingProof("VERIFIED_IMPLEMENTATION_COST_REQUIRED:" + symbol)
        one_way.append(cost.estimated_round_trip_cost_bps/20000 if cost and cost.verified else 0)
    caps = {group: max_group for group in primary.values()}
    for group, cap in assumptions.group_caps.items():
        caps[group] = min(caps.get(group, 1), cap)
    turn_limits = [value for value in (
        assumptions.max_turnover,
        2*request.intent.maximum_turnover_weight
        if request.intent.maximum_turnover_weight is not None else None,
    ) if value is not None]
    return AllocationContext(
        aligned=aligned, current_weights=current,
        groups=tuple(tuple(sorted({primary[key], *assumptions.additional_groups.get(key, [])}))
                     for key in ids),
        group_caps=caps, target_exposure=min(
            max_total, original.target_total_exposure or max_total
        ), max_single=max_single,
        min_cash=max(assumptions.min_cash, request.intent.minimum_cash_weight),
        max_turnover=min(turn_limits) if turn_limits else None,
        can_buy=tuple(assumptions.can_buy.get(key, True) for key in ids),
        can_sell=tuple(assumptions.can_sell.get(key, True)
                       and key not in request.intent.user_locked_company_ids for key in ids),
        one_way_costs=tuple(one_way), return_basis=return_basis,
    )


def assess_transition_implementation(
    state: StateStore, objects: ObjectStore, request: PortfolioTransitionRequest,
    construction: PortfolioConstructionReport, proposal: PortfolioAllocationProposal,
    aligned: AlignedPortfolioData, bands: list[PositionTargetBand], prices: dict[str, int],
    current_weights: dict[str, float], target_weights: dict[str, float],
    *, max_total: float, max_single: float, max_group: float,
    return_basis: ReturnBasis = "PRICE_ONLY",
) -> PortfolioImplementationAssessment:
    """Called by the real transition exit, after its bands, lot rounding and cost checks."""
    def declined(
        code: str, *, status: Literal["UNQUALIFIED", "INFEASIBLE", "STALE_CONTEXT"] = "UNQUALIFIED",
    ) -> PortfolioImplementationAssessment:
        return PortfolioImplementationAssessment(
            status=status, reason_codes=[code], created_at=request.intent.as_of,
        )
    try:
        if aligned.company_ids != sorted(current_weights.keys() | target_weights.keys()):
            raise _MissingProof("IMPLEMENTATION_SECURITY_ORDER_MISMATCH")
        account = _account(state, objects, request)
        original_request = _construction_request(state, objects, construction)
        observed = _observed_weights(account, request, bands, prices, current_weights)
        quantities, projected = _project_bands(account, request, bands, prices)
        target = tuple(target_weights.get(key, 0) for key in aligned.company_ids)
        context = _context(
            original_request, request, aligned, observed, target,
            max_total=max_total, max_single=max_single, max_group=max_group,
            return_basis=return_basis,
        )
        # The account/cost context is explicitly rebuilt here. A prior solver's
        # objective/optimality cannot be carried into this different context.
        baseline = validate_allocation(
            context, np.array(target), method=str(proposal.method), status="REBASED_RESEARCH",
        )
        if not baseline.weights:
            return declined("ORIGINAL_TARGET_VIOLATES_CURRENT_CONSTRAINTS", status="INFEASIBLE")
        result = revalidate_implementation(
            context, baseline, projected, observed_current_weights=observed,
        )
        if account.reader.revision_vector()[account.lane.value.lower()] != account.revision:
            return declined("ACCOUNT_REVISION_CHANGED_DURING_ASSESSMENT", status="STALE_CONTEXT")
        if not result.weights:
            return PortfolioImplementationAssessment(
                status="INFEASIBLE", reason_codes=list(result.reason_codes),
                account_revision=account.revision, created_at=request.intent.as_of,
            )
        return PortfolioImplementationAssessment(
            status="RESEARCH_PROJECTED", projected_quantities=quantities,
            projected_weights=dict(zip(aligned.company_ids, result.weights, strict=True)),
            reason_codes=["READ_ONLY_PROJECTION_NOT_EXECUTION_AUTHORIZATION",
                          "REBASED_ACCOUNT_AND_COST_CONTEXT"],
            context_fingerprint=result.context_fingerprint, account_revision=account.revision,
            total_l1_turnover=result.turnover, one_off_cost_fraction=result.one_off_cost_fraction,
            cash_after_cost=result.cash_after_cost,
            annualized_volatility=result.annualized_volatility,
            target_exposure_deviation=result.target_deviation, created_at=request.intent.as_of,
        )
    except _MissingProof as exc:
        return declined(
            str(exc), status=(
                "STALE_CONTEXT"
                if "DRIFT" in str(exc) or "CONTEXT_CHANGED" in str(exc) else "UNQUALIFIED"
            ),
        )
    except (OSError, ValueError, AStockError, sqlite3.Error):
        return declined("CANONICAL_IMPLEMENTATION_PROOF_INVALID")
