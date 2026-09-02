"""Deterministic local fixtures for external quant pattern benchmarks.

All data is synthetic, fixed, and fully reproducible.  No network access required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

TZ_SHANGHAI = timezone(timedelta(hours=8))


def _sha256_hex(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _deterministic_int(content: str) -> int:
    """Derive a stable integer from content (unlike builtin hash(), which is
    randomized across processes via PYTHONHASHSEED)."""
    return int(_sha256_hex(content)[:8], 16)


# ---------------------------------------------------------------------------
# 1. Qlib Recorder fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MockRecorderRun:
    run_id: str
    params: dict[str, Any]
    metrics: dict[str, float]
    artifact_hashes: dict[str, str]
    created_at: str = "2026-09-01T00:00:00Z"


def make_qlib_recorder_runs() -> list[MockRecorderRun]:
    """Three deterministic runs with identical structure."""
    runs = []
    seeds = ["alpha-v1", "alpha-v2", "alpha-v3"]
    for seed in seeds:
        params = {"seed": seed, "lookback": 20, "top_k": 5}
        metrics = {
            "ic": 0.05 + _deterministic_int(seed) % 100 / 10000,
            "rank_ic": 0.04 + _deterministic_int(seed) % 100 / 10000,
            "turnover": 0.3 + _deterministic_int(seed) % 100 / 10000,
        }
        artifact_hashes = {
            "model": _sha256_hex(f"model-{seed}"),
            "signals": _sha256_hex(f"signals-{seed}"),
        }
        runs.append(MockRecorderRun(
            run_id=_sha256_hex(f"run-{seed}"),
            params=params,
            metrics=metrics,
            artifact_hashes=artifact_hashes,
        ))
    return runs


# ---------------------------------------------------------------------------
# 2. RD-Agent Hypothesis fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MockHypothesis:
    hypothesis_id: str
    description: str
    factor_code: str
    expected_ic: float
    iteration: int
    parent_id: str | None = None


@dataclass(frozen=True)
class MockHypothesisResult:
    hypothesis_id: str
    actual_ic: float
    turnover: float
    sharpe: float
    passed: bool
    feedback: str


def make_rdagent_hypotheses() -> list[MockHypothesis]:
    """Three iterations of factor hypotheses."""
    return [
        MockHypothesis(
            hypothesis_id="h1",
            description="Momentum 20d",
            factor_code="ret_20d",
            expected_ic=0.05,
            iteration=1,
        ),
        MockHypothesis(
            hypothesis_id="h2",
            description="Volume-price divergence",
            factor_code="vp_div",
            expected_ic=0.08,
            iteration=2,
            parent_id="h1",
        ),
        MockHypothesis(
            hypothesis_id="h3",
            description="Volatility-adjusted momentum",
            factor_code="vol_adj_mom",
            expected_ic=0.06,
            iteration=3,
            parent_id="h2",
        ),
    ]


def make_rdagent_results() -> list[MockHypothesisResult]:
    """Deterministic results for each hypothesis."""
    return [
        MockHypothesisResult("h1", actual_ic=0.042, turnover=0.35, sharpe=1.2, passed=False,
                             feedback="IC below threshold"),
        MockHypothesisResult("h2", actual_ic=0.071, turnover=0.28, sharpe=1.8, passed=True,
                             feedback="Meets criteria"),
        MockHypothesisResult("h3", actual_ic=0.055, turnover=0.31, sharpe=1.5, passed=True,
                             feedback="Improvement over h2"),
    ]


# ---------------------------------------------------------------------------
# 3. LEAN Event Ordering fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MockEvent:
    event_type: str
    timestamp: str
    symbol: str
    price_fen: int
    qty: int
    side: str  # BUY or SELL
    event_id: str = ""


def make_lean_events() -> list[MockEvent]:
    """Deterministic event sequence: market data → signal → order → fill → settle."""
    base = datetime(2026, 9, 1, 9, 30, 0, tzinfo=TZ_SHANGHAI)
    symbol = "600938"
    specs = [
        ("MARKET_DATA", 0, 2850, 0, "NONE", "e1"),
        ("SIGNAL", 1, 2850, 0, "NONE", "e2"),
        ("ORDER", 2, 2850, 100, "BUY", "e3"),
        ("FILL", 3, 2850, 100, "BUY", "e4"),
        ("SETTLE", 4, 2850, 100, "BUY", "e5"),
        ("ACCOUNTING", 5, 2850, 100, "BUY", "e6"),
    ]
    events = []
    for event_type, offset, price_fen, qty, side, event_id in specs:
        events.append(MockEvent(
            event_type=event_type,
            timestamp=(base + timedelta(seconds=offset)).isoformat(),
            symbol=symbol,
            price_fen=price_fen,
            qty=qty,
            side=side,
            event_id=event_id,
        ))
    return events


def make_astock_events() -> list[MockEvent]:
    """AStockMultiAgent's stricter ordering: classification → protocol → execution → ledger."""
    base = datetime(2026, 9, 1, 9, 30, 0, tzinfo=TZ_SHANGHAI)
    symbol = "600938"
    specs = [
        ("CLASSIFICATION", 0, 2850, 0, "NONE", "a1"),
        ("COMMITTEE_PROTOCOL", 1, 2850, 0, "NONE", "a2"),
        ("CLASSIFIED_PROTOCOL", 2, 2850, 0, "NONE", "a3"),
        ("EXECUTION_PREPARE", 3, 2850, 100, "BUY", "a4"),
        ("EXECUTION_CONFIRM", 4, 2850, 100, "BUY", "a5"),
        ("LEDGER_FILL", 5, 2850, 100, "BUY", "a6"),
    ]
    events = []
    for event_type, offset, price_fen, qty, side, event_id in specs:
        events.append(MockEvent(
            event_type=event_type,
            timestamp=(base + timedelta(seconds=offset)).isoformat(),
            symbol=symbol,
            price_fen=price_fen,
            qty=qty,
            side=side,
            event_id=event_id,
        ))
    return events


# ---------------------------------------------------------------------------
# 4. RQAlpha / Robustness fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MockBacktestResult:
    run_id: str
    strategy_returns: tuple[float, ...]
    sharpe: float
    max_drawdown: float
    turnover: float
    params: dict[str, Any]


def make_robustness_backtest_results() -> list[MockBacktestResult]:
    """Five deterministic backtest variants for robustness comparison.

    Each variant combines an additive drift (raises mean return) with a distinct
    dispersion factor so Sharpe and max drawdown genuinely differ across the
    grid.  Values are fixed and reproducible; no random state is used.
    """
    base_returns = [0.01, -0.005, 0.008, -0.003, 0.012, -0.007, 0.006, 0.009, -0.002, 0.011]
    results = []
    param_variants = [
        {"lookback": 20, "top_k": 5},
        {"lookback": 25, "top_k": 5},
        {"lookback": 20, "top_k": 8},
        {"lookback": 30, "top_k": 3},
        {"lookback": 15, "top_k": 10},
    ]
    drift = [0.0015, 0.0025, 0.0035, 0.0045, 0.0020]
    vol_scale = [1.0, 0.9, 0.8, 0.7, 1.25]
    for i, params in enumerate(param_variants):
        shifted = tuple(
            r * vol_scale[i] + drift[i]
            for r in base_returns
        )
        mean_r = sum(shifted) / len(shifted)
        var_r = sum((r - mean_r) ** 2 for r in shifted) / len(shifted)
        sharpe = mean_r / (var_r ** 0.5 + 1e-10) * (252 ** 0.5)
        cum = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in shifted:
            cum *= 1 + r
            peak = max(peak, cum)
            dd = (peak - cum) / peak
            max_dd = max(max_dd, dd)
        results.append(MockBacktestResult(
            run_id=_sha256_hex(f"bt-{params}"),
            strategy_returns=shifted,
            sharpe=round(sharpe, 4),
            max_drawdown=round(max_dd, 4),
            turnover=0.25 + i * 0.02,
            params=params,
        ))
    return results
