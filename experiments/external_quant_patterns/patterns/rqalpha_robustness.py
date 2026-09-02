"""Modeled parameter-robustness pattern benchmark.

The experiment independently models a small multi-run parameter-perturbation
report on local fixtures. It does not install, execute, or benchmark RQAlpha.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from statistics import mean, pstdev

from experiments.external_quant_patterns.fixtures.sample_data import MockBacktestResult


@dataclass(frozen=True)
class RobustnessRun:
    """Single robustness backtest run under parameter perturbation."""
    run_id: str
    params: dict[str, int]
    sharpe: float
    max_drawdown: float
    turnover: float
    object_hash: str


@dataclass(frozen=True)
class RobustnessReport:
    """Aggregated robustness report across parameter variants."""
    runs: tuple[RobustnessRun, ...]
    run_count: int
    sharpe_mean: float
    sharpe_std: float
    max_drawdown_worst: float
    best_params: dict[str, int]
    object_hash: str


@dataclass(frozen=True)
class RobustnessComparisonResult:
    """Quantitative comparison of robustness reporting approaches."""
    variant_count: int
    sharpe_mean: float
    sharpe_std: float
    max_drawdown_worst: float
    coverage_description: str
    marginal_value: str
    recommendation: str


def _hash_content(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def build_robustness_runs(results: list[MockBacktestResult]) -> list[RobustnessRun]:
    """Build modeled parameter-perturbation runs from mock results."""
    runs: list[RobustnessRun] = []
    for result in results:
        runs.append(RobustnessRun(
            run_id=result.run_id,
            params=result.params,
            sharpe=result.sharpe,
            max_drawdown=result.max_drawdown,
            turnover=result.turnover,
            object_hash=_hash_content(f"robust-{result.run_id}"),
        ))
    return runs


def build_robustness_report(results: list[MockBacktestResult]) -> RobustnessReport:
    """Aggregate robustness runs into a report."""
    runs = build_robustness_runs(results)
    sharpes = [r.sharpe for r in runs]
    mean_sharpe = mean(sharpes) if sharpes else 0.0
    std_sharpe = pstdev(sharpes) if sharpes else 0.0
    worst_drawdown = max((r.max_drawdown for r in runs), default=0.0)
    best_run = max(runs, key=lambda r: r.sharpe) if runs else None

    return RobustnessReport(
        runs=tuple(runs),
        run_count=len(runs),
        sharpe_mean=round(mean_sharpe, 4),
        sharpe_std=round(std_sharpe, 4),
        max_drawdown_worst=worst_drawdown,
        best_params=best_run.params if best_run else {},
        object_hash=_hash_content(f"robust-report-{len(runs)}"),
    )


def compare_robustness_reports(
    results: list[MockBacktestResult],
) -> RobustnessComparisonResult:
    """Compare the modeled robustness report with AStock existing governance."""
    report = build_robustness_report(results)

    return RobustnessComparisonResult(
        variant_count=report.run_count,
        sharpe_mean=report.sharpe_mean,
        sharpe_std=report.sharpe_std,
        max_drawdown_worst=report.max_drawdown_worst,
        coverage_description=(
            f"The local fixture contains {report.run_count} parameter variants and reports "
            f"mean Sharpe {report.sharpe_mean:.4f}, std {report.sharpe_std:.4f}, "
            f"and worst max drawdown {report.max_drawdown_worst:.4f}."
        ),
        marginal_value=(
            "AStockMultiAgent already has block bootstrap and deflated Sharpe for "
            "robustness. The modeled small-grid perturbation adds "
            "granular per-parameter sensitivity that could complement the existing "
            "shadow walk-forward folds, but provides no additional value as a "
            "standalone framework. "
            "Decision: WATCH_PATTERN_ONLY / REJECT_WHOLESALE."
        ),
        recommendation="WATCH_PATTERN_ONLY",
    )