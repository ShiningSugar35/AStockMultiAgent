"""Deterministic paired statistics for frozen shadow observations."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256
from itertools import combinations
from math import comb, e, fsum, isfinite, sqrt
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


def sample_sharpe(returns: list[float]) -> float | None:
    """Per-observation excess-return Sharpe; no implicit frequency conversion."""
    if any(not isfinite(value) for value in returns):
        raise ValueError("return observations must be finite")
    if len(returns) < 2:
        return None
    average = fsum(returns) / len(returns)
    variance = fsum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    return average / sqrt(variance) if variance > 1e-18 else None


def expected_maximum_sharpe(
    trial_sharpes: list[float], *, effective_trial_count: float
) -> float | None:
    """Bailey/Lopez de Prado (2014), Eq. (3): zero-skill selection threshold.

    All attempted trials must be represented at the same return frequency.
    Effective independent trials are an explicit research assumption, not a
    count of cross-validation paths. The trial Sharpe *dispersion* is essential.
    """
    if any(not isfinite(value) for value in trial_sharpes):
        raise ValueError("trial Sharpes must be finite")
    if not isfinite(effective_trial_count) or not 1 <= effective_trial_count <= len(trial_sharpes):
        raise ValueError("effective independent trials must be within the observed trial count")
    if len(trial_sharpes) < 2:
        return None
    average = fsum(trial_sharpes) / len(trial_sharpes)
    variance = fsum((value - average) ** 2 for value in trial_sharpes) / (len(trial_sharpes) - 1)
    if variance <= 1e-18:
        return None
    if effective_trial_count == 1:
        return 0.0
    normal = NormalDist()
    gamma = 0.5772156649015329
    threshold = (
        (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / effective_trial_count)
        + gamma * normal.inv_cdf(1.0 - 1.0 / (effective_trial_count * e))
    )
    # The extreme-value approximation is not reliable below two effective
    # trials. Refuse rather than return a negative zero-skill hurdle.
    return sqrt(variance) * threshold if effective_trial_count >= 2 else None


def deflated_sharpe_probability(
    returns: list[float],
    *,
    selection_candidate_count: int,
    trial_sharpes: list[float] | None = None,
    effective_trial_count: float | None = None,
) -> float | None:
    """Paper-defined DSR with disclosed trial dispersion and dependence assumption.

    Missing full-trial context is NOT_EVALUABLE, never a fabricated DSR.
    Inputs use one common observation frequency and excess-return convention.
    This asymptotic statistic is diagnostic; it does not establish IID returns.
    """
    sharpe = sample_sharpe(returns)
    if selection_candidate_count <= 1 or len(returns) < 8 or sharpe is None:
        return None
    if trial_sharpes is None or effective_trial_count is None:
        return None
    if len(trial_sharpes) != selection_candidate_count:
        raise ValueError("DSR requires every attempted trial Sharpe")
    threshold = expected_maximum_sharpe(
        trial_sharpes, effective_trial_count=effective_trial_count
    )
    if threshold is None:
        return None
    n = len(returns)
    average = fsum(returns) / n
    m2 = fsum((value - average) ** 2 for value in returns) / n
    if m2 <= 1e-18:
        return None
    skew = fsum((value - average) ** 3 for value in returns) / n / m2**1.5
    kurtosis = fsum((value - average) ** 4 for value in returns) / n / m2**2
    sampling_variance = (1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe**2) / (n - 1)
    if sampling_variance <= 1e-18:
        return None
    return NormalDist().cdf((sharpe - threshold) / sqrt(sampling_variance))


def cscv_probability_of_backtest_overfitting(
    strategy_returns: list[list[float]],
    *,
    block_count: int = 8,
    max_combinations: int = 4096,
    metric: str = "SHARPE",
) -> float | None:
    """Bounded CSCV of Bailey et al. (2015), all complementary half-block splits.

    Blocks are contiguous and equal length; no rows are discarded. Tied
    in-sample winners are averaged, OOS ranks use midranks, and exact median
    ties contribute one half. A constant cross-strategy rank is not evaluable.
    CSCV is a selection diagnostic, NOT a chronological deployment backtest.
    """
    if block_count < 2 or block_count % 2 or block_count > 16:
        raise ValueError("CSCV requires an even block count between 2 and 16")
    if metric not in {"SHARPE", "MEAN"}:
        raise ValueError("unsupported frozen CSCV selection metric")
    if not 1 <= max_combinations <= 20000 or comb(block_count, block_count // 2) > max_combinations:
        raise ValueError("CSCV combination budget exceeded")
    if len(strategy_returns) < 2:
        return None
    lengths = {len(values) for values in strategy_returns}
    if len(lengths) != 1:
        raise ValueError("CSCV return series must align on identical observations")
    if any(not isfinite(value) for values in strategy_returns for value in values):
        raise ValueError("CSCV return observations must be finite")
    n = lengths.pop()
    if len(strategy_returns) > 512 or n * len(strategy_returns) * comb(
        block_count, block_count // 2
    ) > 20_000_000:
        raise ValueError("CSCV observation-work budget exceeded")
    if n < block_count * 2 or n % block_count:
        return None
    if all(values == strategy_returns[0] for values in strategy_returns[1:]):
        return None
    block_size = n // block_count
    all_blocks = set(range(block_count))
    estimates: list[float] = []
    for selected in combinations(range(block_count), block_count // 2):
        test_blocks = all_blocks - set(selected)
        train_scores: list[float] = []
        test_scores: list[float] = []
        for values in strategy_returns:
            train = [
                values[i] for block in selected
                for i in range(block * block_size, (block + 1) * block_size)
            ]
            test = [
                values[i] for block in sorted(test_blocks)
                for i in range(block * block_size, (block + 1) * block_size)
            ]
            train_score = sample_sharpe(train) if metric == "SHARPE" else fsum(train) / len(train)
            test_score = sample_sharpe(test) if metric == "SHARPE" else fsum(test) / len(test)
            if train_score is None or test_score is None:
                return None
            train_scores.append(train_score)
            test_scores.append(test_score)
        best = max(train_scores)
        winners = [i for i, value in enumerate(train_scores) if value == best]
        probabilities = []
        for winner in winners:
            score = test_scores[winner]
            rank = 1 + sum(value < score for value in test_scores)
            rank += (sum(value == score for value in test_scores) - 1) / 2
            relative_rank = rank / (len(test_scores) + 1)
            probabilities.append(
                1.0 if relative_rank < 0.5 else 0.0 if relative_rank > 0.5 else 0.5
            )
        estimates.append(fsum(probabilities) / len(probabilities))
    return fsum(estimates) / len(estimates)


def time_fold_probability_of_backtest_overfitting(
    strategy_returns: list[list[float]],
    *,
    fold_count: int = 5,
) -> float | None:
    """Legacy blocked-mean rank diagnostic; explicitly NOT paper CSCV-PBO.

    Kept for historical callers. New production diagnostics use CSCV above.
    """
    if len(strategy_returns) <= 1:
        return None
    lengths = {len(values) for values in strategy_returns}
    if len(lengths) != 1:
        raise ValueError("blocked-mean series must have equal length")
    if any(not isfinite(value) for values in strategy_returns for value in values):
        raise ValueError("blocked-mean observations must be finite")
    observation_count = next(iter(lengths), 0)
    if observation_count < fold_count * 2 or fold_count < 2:
        return None
    boundaries = [round(index * observation_count / fold_count) for index in range(fold_count + 1)]
    overfit = 0
    for fold in range(fold_count):
        start, end = boundaries[fold], boundaries[fold + 1]
        train_scores = [
            fsum(values[:start] + values[end:]) / (observation_count - end + start)
            for values in strategy_returns
        ]
        test_scores = [fsum(values[start:end]) / (end - start) for values in strategy_returns]
        winner = max(range(len(train_scores)), key=lambda index: (train_scores[index], -index))
        median_test = sorted(test_scores)[len(test_scores) // 2]
        overfit += int(test_scores[winner] < median_test)
    return overfit / fold_count


__all__ = [
    "sample_sharpe",
    "expected_maximum_sharpe",
    "cscv_probability_of_backtest_overfitting",
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
