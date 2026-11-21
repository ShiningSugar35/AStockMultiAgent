"""Bounded read-only factor diagnostics over explicitly frozen observations.

This view neither selects a candidate nor alters the blind tranche. Returns and
costs must already come from an independently verified outcome producer; no price
download, future-data join, model fit, or ranking promotion happens here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import fsum, isfinite, sqrt
from typing import Any


@dataclass(frozen=True, slots=True)
class FactorObservation:
    instrument_id: str
    group_id: str
    signal_at: datetime
    feature_available_at: datetime
    outcome_end_at: datetime
    outcome_available_at: datetime
    feature_value: float | None
    forward_return: float | None
    cost_return: float | None
    dependency_fingerprint: str
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        times = (
            self.signal_at, self.feature_available_at,
            self.outcome_end_at, self.outcome_available_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in times):
            raise ValueError("factor observations require aware timestamps")
        if self.feature_available_at > self.signal_at:
            raise ValueError("feature was not available at the frozen signal time")
        if not self.signal_at < self.outcome_end_at <= self.outcome_available_at:
            raise ValueError("invalid factor forward-outcome information interval")
        if (not self.instrument_id.strip() or not self.group_id.strip()
                or not self.source_ids or any(not source.strip() for source in self.source_ids)):
            raise ValueError("factor identity, source and comparability group are required")
        if (len(self.dependency_fingerprint) != 64
                or set(self.dependency_fingerprint) - set("0123456789abcdef")):
            raise ValueError("factor dependency fingerprint is invalid")
        if any(value is not None and not isfinite(value)
               for value in (self.feature_value, self.forward_return, self.cost_return)):
            raise ValueError("factor values must be finite or explicitly missing")
        if self.forward_return is not None and self.forward_return < -1:
            raise ValueError("simple forward return cannot be below -100 percent")
        if self.cost_return is not None and self.cost_return < 0:
            raise ValueError("costs must use a nonnegative explicit return deduction")


def _midranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    first = 0
    while first < len(order):
        stop = first + 1
        while stop < len(order) and values[order[stop]] == values[order[first]]:
            stop += 1
        rank = (first + 1 + stop) / 2
        for index in order[first:stop]:
            ranks[index] = rank
        first = stop
    return ranks


def _rank_ic(rows: list[FactorObservation]) -> float | None:
    x = _midranks([float(row.feature_value) for row in rows if row.feature_value is not None])
    y = _midranks([float(row.forward_return) for row in rows if row.forward_return is not None])
    if len(x) < 2:
        return None
    a, b = fsum(x) / len(x), fsum(y) / len(y)
    denominator = sqrt(fsum((v - a)**2 for v in x) * fsum((v - b)**2 for v in y))
    return (
        fsum((u-a)*(v-b) for u, v in zip(x, y, strict=True)) / denominator if denominator else None
    )


def evaluate_factor_panel(
    observations: tuple[FactorObservation, ...],
    *,
    factor_id: str,
    formula_version: str,
    evaluated_at: datetime,
    quantiles: int = 5,
    max_rows: int = 10000,
) -> dict[str, Any]:
    """Date/industry cross-sectional Spearman IC; equal-weight descriptive bins.

    Missing outcomes stay missing. Costs unknown => net returns unknown. Turnover
    is the sum of absolute weight changes, not a claim of implementable fills.
    Timestamp ordering is checked here; lineage and vintage verification belong
    to the registered producer. Caller-supplied timestamps alone prove no PIT claim.
    """
    if not factor_id or not formula_version or not 2 <= quantiles <= 10:
        raise ValueError("frozen factor identity and bounded quantiles are required")
    if not 1 <= max_rows <= 100000 or len(observations) > max_rows:
        raise ValueError("factor diagnostic row budget exceeded")
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("factor evaluation time must be timezone-aware")
    keys = [(row.signal_at, row.instrument_id) for row in observations]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate factor observation or revised vintage in the same sample")
    grouped: dict[datetime, list[FactorObservation]] = defaultdict(list)
    excluded = {"MISSING_FEATURE": 0, "MISSING_OUTCOME": 0, "IMMATURE_OUTCOME": 0}
    for row in observations:
        if row.feature_value is None:
            excluded["MISSING_FEATURE"] += 1
        elif row.forward_return is None:
            excluded["MISSING_OUTCOME"] += 1
        elif row.outcome_available_at > evaluated_at:
            excluded["IMMATURE_OUTCOME"] += 1
        else:
            grouped[row.signal_at].append(row)
    results: list[dict[str, Any]] = []
    previous: dict[int, dict[str, float]] = {}
    for stamp, rows in sorted(grouped.items()):
        ranks = _midranks([
            float(row.feature_value) for row in rows if row.feature_value is not None
        ])
        bins: dict[int, list[FactorObservation]] = defaultdict(list)
        for row, rank in zip(rows, ranks, strict=True):
            # Equal feature values remain in the same bin; no ticker tie-break alpha.
            bucket = min(quantiles, 1 + int((rank - 1) * quantiles / len(rows)))
            bins[bucket].append(row)
        by_group: dict[str, list[FactorObservation]] = defaultdict(list)
        for row in rows:
            by_group[row.group_id].append(row)
        quantile_rows = []
        next_previous = {}
        for bucket in range(1, quantiles + 1):
            members = bins.get(bucket, [])
            weights = {row.instrument_id: 1.0 / len(members) for row in members}
            prior = previous.get(bucket)
            turnover = None if prior is None else fsum(
                abs(weights.get(key, 0.0) - prior.get(key, 0.0))
                for key in set(weights) | set(prior)
            )
            gross = (
                fsum(float(row.forward_return) for row in members if row.forward_return is not None)
                / len(members) if members else None
            )
            cost = (
                fsum(float(row.cost_return) for row in members if row.cost_return is not None)
                / len(members)
                    if members and all(row.cost_return is not None for row in members) else None)
            quantile_rows.append({
                "quantile": bucket, "count": len(members), "gross_return": gross,
                "net_return": gross - cost if gross is not None and cost is not None else None,
                "turnover_sum_abs_weights": turnover,
            })
            next_previous[bucket] = weights
        previous = next_previous
        results.append({
            "signal_at": stamp.isoformat(), "count": len(rows),
            "rank_ic": _rank_ic(rows),
            "group_rank_ic": {group: {"count": len(items), "rank_ic": _rank_ic(items)}
                              for group, items in sorted(by_group.items())},
            "quantiles": quantile_rows,
        })
    ics = [row["rank_ic"] for row in results if row["rank_ic"] is not None]
    return {
        "schema_version": "factor-diagnostics-v1",
        "factor_id": factor_id, "formula_version": formula_version,
        "input_count": len(observations),
        "evaluated_count": sum(len(rows) for rows in grouped.values()),
        "excluded": excluded, "date_count": len(results),
        "mean_date_rank_ic": fsum(ics) / len(ics) if ics else None,
        "dates": results, "group_basis": "CALLER_SUPPLIED",
        "source_ids": sorted({source for row in observations for source in row.source_ids}),
        "dependency_fingerprints": sorted({row.dependency_fingerprint for row in observations}),
        "statistical_authority": "DESCRIPTIVE_ONLY",
        "market_validation_claim": False, "recommendation_allowed": False,
        "blind_tranche_mutation_allowed": False,
    }
