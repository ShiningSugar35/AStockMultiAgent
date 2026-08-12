"""Deterministic paired statistics for frozen shadow observations."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256
from statistics import NormalDist

from astock.schemas import ShadowMetricInterval


def mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def percentile(values: list[Decimal], quantile: Decimal) -> Decimal:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not Decimal("0") <= quantile <= Decimal("1"):
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * quantile
    lower_index = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - Decimal(lower_index)
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def deterministic_block_bootstrap(
    values: list[Decimal],
    *,
    seed: str,
    replicates: int,
    block_length: int,
    confidence_level: Decimal,
    metric: str,
    created_at: datetime,
) -> tuple[ShadowMetricInterval, Decimal | None]:
    if not values:
        return ShadowMetricInterval(
            metric=metric,
            sample_count=0,
            created_at=created_at,
        ), None
    if replicates < 1 or block_length < 1:
        raise ValueError("bootstrap replicates and block length must be positive")
    n = len(values)
    estimates: list[Decimal] = []
    block_count = (n + block_length - 1) // block_length
    for replicate in range(replicates):
        sampled: list[Decimal] = []
        for block in range(block_count):
            digest = sha256(f"{seed}:{replicate}:{block}".encode()).digest()
            start = int.from_bytes(digest[:8], "big") % n
            sampled.extend(values[(start + offset) % n] for offset in range(block_length))
        sampled = sampled[:n]
        estimate = mean(sampled)
        assert estimate is not None
        estimates.append(estimate)
    alpha = Decimal("1") - confidence_level
    lower = percentile(estimates, alpha / Decimal("2"))
    upper = percentile(estimates, Decimal("1") - alpha / Decimal("2"))
    estimate = mean(values)
    assert estimate is not None
    lower = min(lower, estimate)
    upper = max(upper, estimate)
    non_positive = sum(value <= 0 for value in estimates)
    p_value = Decimal(non_positive + 1) / Decimal(replicates + 1)
    return (
        ShadowMetricInterval(
            metric=metric,
            sample_count=n,
            estimate=estimate,
            lower=lower,
            upper=upper,
            created_at=created_at,
        ),
        p_value,
    )


def wilson_interval(
    wins: int,
    total: int,
    *,
    confidence_level: Decimal,
    created_at: datetime,
) -> ShadowMetricInterval:
    if wins < 0 or total < 0 or wins > total:
        raise ValueError("Wilson counts must satisfy 0 <= wins <= total")
    if total == 0:
        return ShadowMetricInterval(
            metric="WIN_RATE",
            sample_count=0,
            created_at=created_at,
        )
    alpha = float((Decimal("1") - confidence_level) / Decimal("2"))
    z = Decimal(str(NormalDist().inv_cdf(1 - alpha)))
    n = Decimal(total)
    proportion = Decimal(wins) / n
    z_squared = z * z
    denominator = Decimal("1") + z_squared / n
    center = (proportion + z_squared / (Decimal("2") * n)) / denominator
    half_width = (
        z
        * (
            (
                proportion * (Decimal("1") - proportion) / n + z_squared / (Decimal("4") * n * n)
            ).sqrt()
        )
        / denominator
    )
    return ShadowMetricInterval(
        metric="WIN_RATE",
        sample_count=total,
        estimate=proportion,
        lower=max(Decimal("0"), center - half_width),
        upper=min(Decimal("1"), center + half_width),
        created_at=created_at,
    )


def holm_adjust(p_values: dict[str, Decimal]) -> dict[str, Decimal]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    adjusted: dict[str, Decimal] = {}
    running = Decimal("0")
    total = len(ordered)
    for index, (key, value) in enumerate(ordered):
        candidate = min(Decimal("1"), Decimal(total - index) * value)
        running = max(running, candidate)
        adjusted[key] = running
    return adjusted


def maximum_drawdown(returns: list[Decimal]) -> Decimal:
    nav = Decimal("1")
    peak = nav
    drawdown = Decimal("0")
    for value in returns:
        nav *= Decimal("1") + value
        peak = max(peak, nav)
        if peak > 0:
            drawdown = max(drawdown, (peak - nav) / peak)
    return min(Decimal("1"), max(Decimal("0"), drawdown))


def maximum_drawdown_from_pnl(
    initial_capital_fen: int,
    pnl_fen: list[int],
) -> Decimal:
    if initial_capital_fen <= 0:
        raise ValueError("drawdown reconstruction requires positive initial capital")
    nav = Decimal(initial_capital_fen)
    peak = nav
    drawdown = Decimal("0")
    for pnl in pnl_fen:
        nav += Decimal(pnl)
        if nav <= 0:
            return Decimal("1")
        peak = max(peak, nav)
        drawdown = max(drawdown, (peak - nav) / peak)
    return min(Decimal("1"), max(Decimal("0"), drawdown))


def deterministic_cluster_bootstrap(
    values: list[Decimal],
    cluster_ids: list[str],
    *,
    seed: str,
    replicates: int,
    confidence_level: Decimal,
    metric: str,
    created_at: datetime,
) -> tuple[ShadowMetricInterval, Decimal | None]:
    """Bootstrap whole dependence clusters rather than pretending every row is independent."""

    if len(values) != len(cluster_ids):
        raise ValueError("cluster bootstrap values and cluster ids must have equal length")
    if not values:
        return ShadowMetricInterval(metric=metric, sample_count=0, created_at=created_at), None
    if replicates < 1:
        raise ValueError("cluster bootstrap requires positive replicates")
    grouped: dict[str, list[Decimal]] = {}
    for value, cluster_id in zip(values, cluster_ids, strict=True):
        if not cluster_id:
            raise ValueError("cluster bootstrap ids cannot be blank")
        grouped.setdefault(cluster_id, []).append(value)
    cluster_keys = sorted(grouped)
    estimates: list[Decimal] = []
    for replicate in range(replicates):
        sampled: list[Decimal] = []
        for draw in range(len(cluster_keys)):
            digest = sha256(f"{seed}:{replicate}:{draw}".encode()).digest()
            selected = cluster_keys[int.from_bytes(digest[:8], "big") % len(cluster_keys)]
            sampled.extend(grouped[selected])
        estimate = mean(sampled)
        assert estimate is not None
        estimates.append(estimate)
    alpha = Decimal("1") - confidence_level
    estimate = mean(values)
    assert estimate is not None
    lower = min(percentile(estimates, alpha / Decimal("2")), estimate)
    upper = max(percentile(estimates, Decimal("1") - alpha / Decimal("2")), estimate)
    non_positive = sum(value <= 0 for value in estimates)
    p_value = Decimal(non_positive + 1) / Decimal(replicates + 1)
    return (
        ShadowMetricInterval(
            metric=metric,
            sample_count=len(grouped),
            estimate=estimate,
            lower=lower,
            upper=upper,
            created_at=created_at,
        ),
        p_value,
    )


def deflated_sharpe_probability(
    returns: list[float],
    *,
    selection_candidate_count: int,
) -> float | None:
    """Diagnostic DSR-style probability; never a production admission badge."""

    if selection_candidate_count <= 1:
        return None
    if len(returns) < 8:
        return None
    n = len(returns)
    average = sum(returns) / n
    variance = sum((value - average) ** 2 for value in returns) / (n - 1)
    if variance <= 1e-18:
        return None
    standard_deviation = variance**0.5
    sharpe = average / standard_deviation
    centered = [(value - average) / standard_deviation for value in returns]
    skew = sum(value**3 for value in centered) / n
    kurtosis = sum(value**4 for value in centered) / n
    normal = NormalDist()
    trials = float(selection_candidate_count)
    euler_gamma = 0.5772156649015329
    z_one = normal.inv_cdf(max(1e-12, min(1 - 1e-12, 1.0 - 1.0 / trials)))
    z_two = normal.inv_cdf(max(1e-12, min(1 - 1e-12, 1.0 - 1.0 / (trials * 2.718281828459045))))
    expected_max = (1.0 - euler_gamma) * z_one + euler_gamma * z_two
    sampling_variance = (1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe) / max(
        n - 1, 1
    )
    if sampling_variance <= 1e-18:
        return None
    z_score = (sharpe - expected_max) / sampling_variance**0.5
    return max(0.0, min(1.0, normal.cdf(z_score)))


def time_fold_probability_of_backtest_overfitting(
    strategy_returns: list[list[float]],
    *,
    fold_count: int = 5,
) -> float | None:
    """Time-ordered PBO diagnostic for repeated strategy/parameter selection."""

    if len(strategy_returns) <= 1:
        return None
    lengths = {len(values) for values in strategy_returns}
    if len(lengths) != 1:
        raise ValueError("PBO strategy return series must have equal length")
    observation_count = next(iter(lengths), 0)
    if observation_count < fold_count * 2 or fold_count < 2:
        return None
    boundaries = [round(index * observation_count / fold_count) for index in range(fold_count + 1)]
    overfit = 0
    evaluated = 0
    for fold in range(fold_count):
        start, end = boundaries[fold], boundaries[fold + 1]
        train_scores: list[float] = []
        test_scores: list[float] = []
        for values in strategy_returns:
            test = values[start:end]
            train = values[:start] + values[end:]
            if not train or not test:
                break
            train_scores.append(sum(train) / len(train))
            test_scores.append(sum(test) / len(test))
        if len(train_scores) != len(strategy_returns):
            continue
        winner = max(range(len(train_scores)), key=lambda index: (train_scores[index], -index))
        ordered_test = sorted(test_scores)
        median_test = ordered_test[len(ordered_test) // 2]
        overfit += int(test_scores[winner] < median_test)
        evaluated += 1
    return overfit / evaluated if evaluated else None


__all__ = [
    "deflated_sharpe_probability",
    "deterministic_block_bootstrap",
    "deterministic_cluster_bootstrap",
    "holm_adjust",
    "maximum_drawdown",
    "maximum_drawdown_from_pnl",
    "mean",
    "percentile",
    "time_fold_probability_of_backtest_overfitting",
    "wilson_interval",
]
