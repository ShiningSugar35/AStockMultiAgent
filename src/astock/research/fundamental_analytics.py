"""Pure deterministic calculations for institutional fundamental research."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal

from astock.schemas.institutional_research import (
    DriverNode,
    DriverOperation,
    DriverTree,
    ForecastPeriod,
    ForecastTemplate,
    ValuationScenarioAssumption,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")


def topological_order(nodes: list[DriverNode]) -> list[str]:
    """Return a deterministic topological order and reject cycles."""

    by_id = {node.node_id: node for node in nodes}
    indegree = {node_id: 0 for node_id in by_id}
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for parent in node.input_node_ids:
            if parent not in by_id:
                raise ValueError(f"unknown driver dependency: {parent}")
            indegree[node.node_id] += 1
            children[parent].append(node.node_id)
    ready = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    result: list[str] = []
    while ready:
        current = ready.popleft()
        result.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                insert_at = 0
                while insert_at < len(ready) and ready[insert_at] < child:
                    insert_at += 1
                ready.insert(insert_at, child)
    if len(result) != len(nodes):
        raise ValueError("driver tree contains a cycle")
    return result


def evaluate_driver_period(
    tree: DriverTree,
    input_values: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """Evaluate one forecast period from explicit input-node values."""

    nodes = {node.node_id: node for node in tree.draft.nodes}
    input_nodes = {
        node.node_id for node in tree.draft.nodes if node.operation is DriverOperation.INPUT
    }
    if any(not value.is_finite() for value in input_values.values()):
        raise ValueError("driver values must be finite")
    if set(input_values) != input_nodes:
        missing = sorted(input_nodes - set(input_values))
        extra = sorted(set(input_values) - input_nodes)
        raise ValueError(f"driver input coverage mismatch missing={missing} extra={extra}")
    result: dict[str, Decimal] = {}
    for node_id in tree.evaluation_order:
        node = nodes[node_id]
        if node.operation is DriverOperation.INPUT:
            result[node_id] = input_values[node_id]
            continue
        left = result[node.input_node_ids[0]]
        right = result[node.input_node_ids[1]]
        left_unit = nodes[node.input_node_ids[0]].unit.upper()
        right_unit = nodes[node.input_node_ids[1]].unit.upper()
        if node.operation in {DriverOperation.ADD, DriverOperation.SUBTRACT} and (
            left_unit != right_unit or node.unit.upper() != left_unit
        ):
            raise ValueError("driver additive units must match; normalize inputs first")
        if node.operation is DriverOperation.ADD:
            value = left + right
        elif node.operation is DriverOperation.SUBTRACT:
            value = left - right
        elif node.operation is DriverOperation.MULTIPLY:
            value = left * right
        elif node.operation is DriverOperation.DIVIDE:
            if right == 0:
                raise ZeroDivisionError(f"driver divisor is zero: {node.node_id}")
            value = left / right
        else:  # pragma: no cover - exhaustive enum guard
            raise ValueError(f"unsupported driver operation: {node.operation}")
        result[node_id] = value
    return result


def validate_forecast_units(tree: DriverTree) -> None:
    """Canonical valuation amounts are CNY; upstream adapters normalize other scales."""
    monetary, ratios = {
        ForecastTemplate.OPERATING_FCFF: (
            ("REVENUE", "D_AND_A", "CAPEX", "CHANGE_WORKING_CAPITAL"),
            ("OPERATING_MARGIN", "TAX_RATE"),
        ),
        ForecastTemplate.FINANCIAL_RESIDUAL_INCOME: (
            ("BOOK_VALUE", "NET_INCOME"), ("ROE",),
        ),
        ForecastTemplate.ASSET_NAV: (("ASSET_VALUE", "NET_DEBT"), ()),
        ForecastTemplate.PRE_PROFIT_SCENARIO: (
            ("REVENUE", "CASH_BURN", "CASH_BALANCE"), ("GROSS_MARGIN",),
        ),
    }[tree.draft.forecast_template]
    units = {node.node_id: node.unit.upper() for node in tree.draft.nodes}
    binding = tree.draft.output_bindings
    if any(units[binding[name]] != "CNY" for name in monetary) or any(
        units[binding[name]] != "RATIO" for name in ratios
    ):
        raise ValueError("forecast requires matching monetary units in CNY and ratio rates")
    if "EPS" in binding and units[binding["EPS"]] != "CNY_PER_SHARE":
        raise ValueError("EPS requires CNY_PER_SHARE")


def forecast_period_from_nodes(tree: DriverTree, evaluated: Mapping[str, Decimal], period_end):
    """Convert evaluated drivers to the tree's archetype-specific standardized outputs."""

    validate_forecast_units(tree)
    binding = tree.draft.output_bindings
    metrics = {name: evaluated[node_id] for name, node_id in sorted(binding.items())}
    if tree.draft.forecast_template is not ForecastTemplate.OPERATING_FCFF:
        return ForecastPeriod(
            period_end=period_end,
            template=tree.draft.forecast_template,
            metrics=metrics,
            evaluated_nodes=dict(sorted(evaluated.items())),
            created_at=tree.as_of,
        )
    revenue = metrics["REVENUE"]
    operating_margin = metrics["OPERATING_MARGIN"]
    tax_rate = metrics["TAX_RATE"]
    d_and_a = metrics["D_AND_A"]
    capex = metrics["CAPEX"]
    change_working_capital = metrics["CHANGE_WORKING_CAPITAL"]
    if revenue < 0:
        raise ValueError("forecast revenue cannot be negative")
    if tax_rate < 0 or tax_rate > 1:
        raise ValueError("forecast tax rate must be within zero and one")
    ebit = revenue * operating_margin
    nopat = ebit * (_ONE - tax_rate)
    fcff = nopat + d_and_a - capex - change_working_capital
    return ForecastPeriod(
        period_end=period_end,
        template=tree.draft.forecast_template,
        metrics=metrics,
        revenue=revenue,
        operating_margin=operating_margin,
        ebit=ebit,
        tax_rate=tax_rate,
        nopat=nopat,
        d_and_a=d_and_a,
        capex=capex,
        change_working_capital=change_working_capital,
        fcff=fcff,
        evaluated_nodes=dict(sorted(evaluated.items())),
        created_at=tree.as_of,
    )


def dcf_fcff_value(
    periods: list[ForecastPeriod],
    assumption: ValuationScenarioAssumption,
    *,
    valuation_date: date | None = None,
) -> tuple[Decimal, Decimal]:
    """Return enterprise and equity value for an explicit FCFF DCF."""

    rate = assumption.discount_rate
    growth = assumption.terminal_growth
    if rate is None or growth is None:
        raise ValueError("DCF requires discount_rate and terminal_growth")
    if (
        not rate.is_finite() or not growth.is_finite()
        or rate <= 0 or rate <= growth or growth <= -1
    ):
        raise ValueError("DCF requires positive discount rate above terminal growth")
    ordered = sorted(periods, key=lambda item: item.period_end)
    if not ordered:
        raise ValueError("DCF requires at least one forecast period")
    if any(
        period.template is not ForecastTemplate.OPERATING_FCFF or period.fcff is None
        for period in ordered
    ):
        raise ValueError("DCF requires an OPERATING_FCFF forecast template")
    times = _discount_times(ordered, valuation_date)
    present_value = _ZERO
    for elapsed, period in zip(times, ordered, strict=True):
        assert period.fcff is not None
        present_value += period.fcff / ((_ONE + rate) ** elapsed)
    assert ordered[-1].fcff is not None
    terminal_fcff = ordered[-1].fcff * (_ONE + growth)
    terminal_value = terminal_fcff / (rate - growth)
    present_value += terminal_value / ((_ONE + rate) ** times[-1])
    enterprise_value = present_value
    equity_value = equity_from_enterprise(enterprise_value, assumption)
    return enterprise_value, equity_value


def implied_terminal_growth(
    periods: list[ForecastPeriod],
    assumption: ValuationScenarioAssumption,
    target_enterprise_value: Decimal,
    *,
    valuation_date: date | None = None,
) -> Decimal | None:
    """Solve a bounded reverse-DCF terminal growth rate by bisection."""

    rate = assumption.discount_rate
    if rate is None or rate <= 0 or not periods or target_enterprise_value <= 0:
        return None
    ordered = sorted(periods, key=lambda item: item.period_end)
    if any(
        period.template is not ForecastTemplate.OPERATING_FCFF or period.fcff is None
        for period in ordered
    ):
        return None
    times = _discount_times(ordered, valuation_date)
    explicit_pv = sum(
        (
            period.fcff / ((_ONE + rate) ** elapsed)
            for elapsed, period in zip(times, ordered, strict=True)
            if period.fcff is not None
        ),
        _ZERO,
    )
    target_terminal_pv = target_enterprise_value - explicit_pv
    if target_terminal_pv <= 0:
        return None
    last_fcff = ordered[-1].fcff
    assert last_fcff is not None
    if last_fcff <= 0:
        return None

    def enterprise(growth: Decimal) -> Decimal:
        terminal = last_fcff * (_ONE + growth) / (rate - growth)
        return explicit_pv + terminal / ((_ONE + rate) ** times[-1])

    low = Decimal("-0.10")
    high = min(rate - Decimal("0.005"), Decimal("0.15"))
    if high <= low:
        return None
    low_value = enterprise(low)
    high_value = enterprise(high)
    if target_enterprise_value < low_value or target_enterprise_value > high_value:
        return None
    for _ in range(100):
        mid = (low + high) / Decimal("2")
        value = enterprise(mid)
        if value < target_enterprise_value:
            low = mid
        else:
            high = mid
    return (low + high) / Decimal("2")


def equity_adjustment(assumption: ValuationScenarioAssumption) -> Decimal:
    bridge = assumption.equity_adjustments
    if bridge is None or assumption.net_debt is None:
        raise ValueError("VALUATION_EQUITY_BRIDGE_INCOMPLETE")
    if not assumption.net_debt.is_finite() or not assumption.shares_outstanding.is_finite():
        raise ValueError("VALUATION_EQUITY_BRIDGE_NON_FINITE")
    return (
        -assumption.net_debt + bridge.non_operating_assets_excluding_cash
        - bridge.minority_interests - bridge.other_senior_claims
    )


def equity_from_enterprise(value: Decimal, assumption: ValuationScenarioAssumption) -> Decimal:
    return value + equity_adjustment(assumption)


def _discount_times(periods: list[ForecastPeriod], valuation_date: date | None) -> list[Decimal]:
    dates = [period.period_end for period in periods]
    if len(set(dates)) != len(dates) or any(
        not 364 <= (right - left).days <= 366 for left, right in zip(dates, dates[1:], strict=False)
    ):
        raise ValueError("DCF requires distinct annual forecast periods")
    if valuation_date is None:
        # Historical pure-function callers explicitly retain end-of-year 1..N.
        return [Decimal(index) for index in range(1, len(dates) + 1)]
    if any(day <= valuation_date for day in dates):
        raise ValueError("DCF cash-flow dates must follow its valuation date")
    return [Decimal((day - valuation_date).days) / Decimal("365") for day in dates]


def affected_driver_nodes(tree: DriverTree, changed_inputs: set[str]) -> list[str]:
    if not changed_inputs <= {node.node_id for node in tree.draft.nodes}:
        raise ValueError("unknown changed driver")
    affected = set(changed_inputs)
    nodes = {node.node_id: node for node in tree.draft.nodes}
    for node_id in tree.evaluation_order:
        if set(nodes[node_id].input_node_ids) & affected:
            affected.add(node_id)
    return [node_id for node_id in tree.evaluation_order if node_id in affected]


def solve_bounded_driver(
    evaluate: Callable[[Decimal], Decimal],
    *,
    target: Decimal,
    lower: Decimal,
    upper: Decimal,
    grid_points: int = 17,
) -> tuple[str, list[Decimal], list[tuple[Decimal, Decimal]]]:
    """Bounded grid/bracket diagnostic; never promises global uniqueness.

    Tangent roots between grid points are not proven absent. The returned status
    explicitly limits a no-crossing finding to the sampled domain.
    """
    if not 3 <= grid_points <= 65 or lower >= upper or not all(
        value.is_finite() for value in (target, lower, upper)
    ):
        raise ValueError("invalid driver solve domain")
    grid = [
        lower + (upper - lower) * Decimal(index) / Decimal(grid_points - 1)
        for index in range(grid_points)
    ]
    points = [(value, evaluate(value)) for value in grid]
    if any(not value.is_finite() for _, value in points):
        raise ValueError("non-finite driver valuation")
    tolerance = max(Decimal("1e-12"), abs(target) * Decimal("1e-10"))
    roots = [x for x, value in points if abs(value - target) <= tolerance]
    if len(roots) == len(points):
        return "NON_IDENTIFIABLE_FLAT", [], points
    for (left, lv), (right, rv) in zip(points, points[1:], strict=False):
        if abs(lv - target) <= tolerance or abs(rv - target) <= tolerance:
            continue
        if (lv - target) * (rv - target) >= 0:
            continue
        for _ in range(64):
            middle = (left + right) / 2
            value = evaluate(middle)
            if abs(value - target) <= tolerance:
                left = right = middle
                break
            if (value - target) * (lv - target) > 0:
                left, lv = middle, value
            else:
                right = middle
        roots.append((left + right) / 2)
    roots = sorted(set(roots))
    status = (
        "NO_BRACKET_IN_SAMPLED_DOMAIN" if not roots
        else "MULTIPLE_BRACKETS" if len(roots) > 1
        else "BOUNDARY" if roots[0] in {lower, upper}
        else "BRACKETED_NOT_GLOBAL_UNIQUENESS"
    )
    return status, roots, points


__all__ = [
    "affected_driver_nodes",
    "equity_adjustment",
    "equity_from_enterprise",
    "solve_bounded_driver",
    "dcf_fcff_value",
    "evaluate_driver_period",
    "forecast_period_from_nodes",
    "implied_terminal_growth",
    "topological_order",
    "validate_forecast_units",
]
