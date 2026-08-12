"""Pure deterministic calculations for institutional fundamental research."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
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


def forecast_period_from_nodes(tree: DriverTree, evaluated: Mapping[str, Decimal], period_end):
    """Convert evaluated drivers to the tree's archetype-specific standardized outputs."""

    binding = tree.draft.output_bindings
    metrics = {name: evaluated[node_id] for name, node_id in sorted(binding.items())}
    if tree.draft.forecast_template is not ForecastTemplate.OPERATING_FCFF:
        return ForecastPeriod(
            period_end=period_end,
            template=tree.draft.forecast_template,
            metrics=metrics,
            evaluated_nodes=dict(sorted(evaluated.items())),
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
    )


def dcf_fcff_value(
    periods: list[ForecastPeriod],
    assumption: ValuationScenarioAssumption,
) -> tuple[Decimal, Decimal]:
    """Return enterprise and equity value for an explicit FCFF DCF."""

    rate = assumption.discount_rate
    growth = assumption.terminal_growth
    if rate is None or growth is None:
        raise ValueError("DCF requires discount_rate and terminal_growth")
    if rate <= 0 or rate <= growth:
        raise ValueError("DCF requires positive discount rate above terminal growth")
    ordered = sorted(periods, key=lambda item: item.period_end)
    if not ordered:
        raise ValueError("DCF requires at least one forecast period")
    if any(
        period.template is not ForecastTemplate.OPERATING_FCFF or period.fcff is None
        for period in ordered
    ):
        raise ValueError("DCF requires an OPERATING_FCFF forecast template")
    present_value = _ZERO
    for index, period in enumerate(ordered, start=1):
        assert period.fcff is not None
        present_value += period.fcff / ((_ONE + rate) ** index)
    assert ordered[-1].fcff is not None
    terminal_fcff = ordered[-1].fcff * (_ONE + growth)
    terminal_value = terminal_fcff / (rate - growth)
    present_value += terminal_value / ((_ONE + rate) ** len(ordered))
    enterprise_value = present_value
    equity_value = enterprise_value - assumption.net_debt
    return enterprise_value, equity_value


def implied_terminal_growth(
    periods: list[ForecastPeriod],
    assumption: ValuationScenarioAssumption,
    target_enterprise_value: Decimal,
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
    explicit_pv = sum(
        (
            period.fcff / ((_ONE + rate) ** index)
            for index, period in enumerate(ordered, start=1)
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
        return explicit_pv + terminal / ((_ONE + rate) ** len(ordered))

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


__all__ = [
    "dcf_fcff_value",
    "evaluate_driver_period",
    "forecast_period_from_nodes",
    "implied_terminal_growth",
    "topological_order",
]
