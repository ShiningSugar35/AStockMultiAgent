"""Pure numerical portfolio analytics with no storage, network, or ledger access."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.covariance import LedoitWolf

_ANNUALIZATION = 252.0


@dataclass(frozen=True, slots=True)
class AlignedPortfolioData:
    company_ids: list[str]
    asset_returns: np.ndarray
    benchmark_returns: np.ndarray
    common_session_count: int
    covariance: np.ndarray
    correlation: np.ndarray


@dataclass(frozen=True, slots=True)
class PortfolioRiskStatistics:
    annualized_return: float
    annualized_volatility: float
    annualized_downside_deviation: float
    beta_to_benchmark: float
    annualized_tracking_error: float
    max_drawdown: float
    historical_var_95: float
    historical_cvar_95: float
    historical_cdar_95: float
    concentration_hhi: float
    effective_number_of_positions: float
    max_abs_pair_correlation: float
    risk_contribution_fractions: np.ndarray


def align_return_histories(
    company_ids: list[str],
    close_histories: list[dict[str, float]],
    benchmark_closes: dict[str, float],
    *,
    lookback_sessions: int,
    minimum_sessions: int,
) -> AlignedPortfolioData:
    common_dates = set(benchmark_closes)
    for history in close_histories:
        common_dates &= set(history)
    dates = sorted(common_dates)[-(lookback_sessions + 1) :]
    if len(dates) < minimum_sessions + 1:
        raise ValueError("portfolio has insufficient common point-in-time sessions")
    asset_prices = np.array(
        [[history[date] for history in close_histories] for date in dates],
        dtype=float,
    )
    benchmark_prices = np.array([benchmark_closes[date] for date in dates], dtype=float)
    asset_returns = asset_prices[1:] / asset_prices[:-1] - 1.0
    benchmark_returns = benchmark_prices[1:] / benchmark_prices[:-1] - 1.0
    if not np.isfinite(asset_returns).all() or not np.isfinite(benchmark_returns).all():
        raise ValueError("portfolio return matrix contains non-finite values")
    if asset_returns.shape[1] == 1:
        variance = float(np.var(asset_returns[:, 0], ddof=1))
        covariance = np.array([[max(variance, 1e-12)]], dtype=float)
        correlation = np.array([[1.0]], dtype=float)
    else:
        covariance = np.asarray(LedoitWolf().fit(asset_returns).covariance_, dtype=float)
        correlation = np.asarray(
            np.nan_to_num(
                np.corrcoef(asset_returns, rowvar=False),
                nan=0.0,
                posinf=0.0,
                neginf=0.0,
            ),
            dtype=float,
        )
        np.fill_diagonal(correlation, 1.0)
    return AlignedPortfolioData(
        company_ids=company_ids,
        asset_returns=asset_returns,
        benchmark_returns=benchmark_returns,
        common_session_count=asset_returns.shape[0],
        covariance=covariance,
        correlation=correlation,
    )


def portfolio_risk_statistics(
    aligned: AlignedPortfolioData,
    weights: np.ndarray,
) -> PortfolioRiskStatistics:
    invested = float(weights.sum())
    if invested <= 0:
        raise ValueError("portfolio weights require positive invested exposure")
    portfolio_returns = aligned.asset_returns @ weights
    benchmark_returns = aligned.benchmark_returns
    variance = float(weights @ aligned.covariance @ weights)
    annualized_volatility = sqrt(max(variance, 0.0) * _ANNUALIZATION)
    downside = np.minimum(portfolio_returns, 0.0)
    annualized_downside_deviation = float(
        sqrt(max(float(np.mean(downside**2)), 0.0) * _ANNUALIZATION)
    )
    benchmark_variance = float(np.var(benchmark_returns, ddof=1))
    beta = (
        float(np.cov(portfolio_returns, benchmark_returns, ddof=1)[0, 1])
        / benchmark_variance
        if benchmark_variance > 1e-15
        else 0.0
    )
    tracking_error = float(
        np.std(portfolio_returns - benchmark_returns, ddof=1) * sqrt(_ANNUALIZATION)
    )
    compounded = float(np.prod(1.0 + portfolio_returns))
    annualized_return = (
        compounded ** (_ANNUALIZATION / aligned.common_session_count) - 1.0
        if compounded > 0
        else -1.0
    )
    cumulative = np.cumprod(1.0 + portfolio_returns)
    running_peak = np.maximum.accumulate(cumulative)
    drawdowns = 1.0 - cumulative / running_peak
    max_drawdown = float(np.max(drawdowns))
    drawdown_quantile = float(np.quantile(drawdowns, 0.95))
    drawdown_tail = drawdowns[drawdowns >= drawdown_quantile]
    cdar95 = float(drawdown_tail.mean()) if drawdown_tail.size else max_drawdown
    quantile = float(np.quantile(portfolio_returns, 0.05))
    tail = portfolio_returns[portfolio_returns <= quantile]
    var95 = max(0.0, -quantile)
    cvar95 = max(0.0, -float(tail.mean()) if tail.size else var95)
    normalized = weights / invested
    hhi = float(np.sum(normalized**2))
    effective_n = 1.0 / hhi if hhi > 0 else 0.0
    if len(weights) > 1:
        off_diag = aligned.correlation.copy()
        np.fill_diagonal(off_diag, 0.0)
        max_corr = float(np.max(np.abs(off_diag)))
    else:
        max_corr = 0.0
    marginal = aligned.covariance @ weights
    contributions = weights * marginal
    total_risk = float(contributions.sum())
    fractions = (
        contributions / total_risk
        if abs(total_risk) > 1e-15
        else np.zeros_like(weights)
    )
    return PortfolioRiskStatistics(
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        annualized_downside_deviation=annualized_downside_deviation,
        beta_to_benchmark=beta,
        annualized_tracking_error=tracking_error,
        max_drawdown=max_drawdown,
        historical_var_95=var95,
        historical_cvar_95=cvar95,
        historical_cdar_95=cdar95,
        concentration_hhi=hhi,
        effective_number_of_positions=effective_n,
        max_abs_pair_correlation=max_corr,
        risk_contribution_fractions=fractions,
    )


def minimum_variance_weights(covariance: np.ndarray) -> np.ndarray:
    n = covariance.shape[0]
    weights = np.full(n, 1.0 / n, dtype=float)
    largest = max(float(np.linalg.eigvalsh(covariance).max()), 1e-12)
    step = 1.0 / (2.0 * largest)
    for _ in range(1000):
        candidate = project_simplex(weights - step * (2.0 * covariance @ weights))
        if float(np.linalg.norm(candidate - weights, ord=1)) < 1e-10:
            weights = candidate
            break
        weights = candidate
    return weights


def project_simplex(values: np.ndarray) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    ordered = np.sort(vector)[::-1]
    cumulative = np.cumsum(ordered)
    condition = ordered - (cumulative - 1.0) / (np.arange(len(vector)) + 1) > 0
    indices = np.nonzero(condition)[0]
    if not len(indices):
        return np.full(len(vector), 1.0 / len(vector), dtype=float)
    rho = int(indices[-1])
    theta = float((cumulative[rho] - 1.0) / (rho + 1))
    return np.maximum(vector - theta, 0.0)


def hierarchical_risk_weights(aligned: AlignedPortfolioData) -> np.ndarray:
    n = len(aligned.company_ids)
    if n == 2:
        variances = np.maximum(np.diag(aligned.covariance), 1e-12)
        inverse = 1.0 / variances
        return inverse / inverse.sum()
    distance = np.sqrt(np.maximum(0.0, 0.5 * (1.0 - aligned.correlation)))
    np.fill_diagonal(distance, 0.0)
    model = AgglomerativeClustering(
        n_clusters=1,
        metric="precomputed",
        linkage="average",
        compute_full_tree="auto",
    ).fit(distance)
    children = model.children_
    leaves: dict[int, list[int]] = {index: [index] for index in range(n)}
    for offset, (left, right) in enumerate(children):
        leaves[n + offset] = [*leaves[int(left)], *leaves[int(right)]]
    weights = np.ones(n, dtype=float)

    def cluster_variance(indices: list[int]) -> float:
        sub = aligned.covariance[np.ix_(indices, indices)]
        diagonal = np.maximum(np.diag(sub), 1e-12)
        inverse = 1.0 / diagonal
        inverse /= inverse.sum()
        return max(float(inverse @ sub @ inverse), 1e-12)

    def allocate(node: int, scale: float) -> None:
        if node < n:
            weights[node] = scale
            return
        left, right = children[node - n]
        left_indices = leaves[int(left)]
        right_indices = leaves[int(right)]
        left_variance = cluster_variance(left_indices)
        right_variance = cluster_variance(right_indices)
        left_share = right_variance / (left_variance + right_variance)
        allocate(int(left), scale * left_share)
        allocate(int(right), scale * (1.0 - left_share))

    allocate(n + len(children) - 1, 1.0)
    return weights / weights.sum()


def constrain_scores(
    company_ids: list[str],
    raw_scores: np.ndarray,
    groups: dict[str, str],
    *,
    target: float,
    max_single: float,
    max_group: float,
) -> tuple[dict[str, float], set[str]]:
    scores = np.maximum(np.asarray(raw_scores, dtype=float), 0.0)
    if not np.isfinite(scores).all() or float(scores.sum()) <= 0:
        scores = np.ones(len(company_ids), dtype=float)
    scores = scores / float(scores.sum())
    weights = {company_id: 0.0 for company_id in company_ids}
    binding: set[str] = set()
    remaining = target
    for _ in range(100):
        if remaining <= 1e-10:
            break
        group_used: dict[str, float] = {}
        for company_id, weight in weights.items():
            group = groups[company_id]
            group_used[group] = group_used.get(group, 0.0) + weight
        eligible: list[tuple[int, str]] = []
        for index, company_id in enumerate(company_ids):
            single_room = max(0.0, max_single - weights[company_id])
            group_room = max(
                0.0,
                max_group - group_used.get(groups[company_id], 0.0),
            )
            if min(single_room, group_room) > 1e-12:
                eligible.append((index, company_id))
        if not eligible:
            binding.add("CASH_RESIDUAL_CONSTRAINT_BOUND")
            break
        eligible_score = sum(float(scores[index]) for index, _ in eligible)
        if eligible_score <= 0:
            eligible_score = float(len(eligible))
        distributed = 0.0
        for index, company_id in eligible:
            group = groups[company_id]
            single_room = max(0.0, max_single - weights[company_id])
            group_room = max(0.0, max_group - group_used.get(group, 0.0))
            room = min(single_room, group_room)
            score = float(scores[index]) if float(scores[index]) > 0 else 1.0
            allocation = min(room, remaining * score / eligible_score)
            weights[company_id] += allocation
            group_used[group] = group_used.get(group, 0.0) + allocation
            distributed += allocation
        if distributed <= 1e-12:
            binding.add("CASH_RESIDUAL_CONSTRAINT_BOUND")
            break
        remaining -= distributed
    if any(abs(value - max_single) <= 1e-8 for value in weights.values()):
        binding.add("MAX_SINGLE_POSITION_BOUND")
    group_totals: dict[str, float] = {}
    for company_id, weight in weights.items():
        group = groups[company_id]
        group_totals[group] = group_totals.get(group, 0.0) + weight
    if any(abs(value - max_group) <= 1e-8 for value in group_totals.values()):
        binding.add("MAX_GROUP_EXPOSURE_BOUND")
    return weights, binding


__all__ = [
    "AlignedPortfolioData",
    "PortfolioRiskStatistics",
    "align_return_histories",
    "constrain_scores",
    "hierarchical_risk_weights",
    "minimum_variance_weights",
    "portfolio_risk_statistics",
    "project_simplex",
]
