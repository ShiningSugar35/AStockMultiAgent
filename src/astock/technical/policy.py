"""One immutable parameter contract; no model, provider or execution dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from math import isfinite
from pathlib import Path

import yaml

from astock.core.hashing import content_hash


@dataclass(frozen=True, slots=True)
class TechnicalAnalysisPolicy:
    policy_version: str
    max_history_bars: int
    max_release_rows: int
    ema_fast: int
    ema_slow: int
    macd_signal: int
    rsi_period: int
    atr_period: int
    bollinger_period: int
    bollinger_std: float
    volume_period: int
    breakout_period: int
    breakout_volume_multiplier: float

    def __post_init__(self) -> None:
        # This is the single configuration boundary, including direct API construction.
        windows = (
            self.ema_fast,
            self.ema_slow,
            self.macd_signal,
            self.rsi_period,
            self.atr_period,
            self.bollinger_period,
            self.volume_period,
            self.breakout_period,
        )
        if any(type(value) is not int or value < 2 for value in windows):
            raise ValueError("technical analysis periods must be integers >= 2")
        if type(self.max_history_bars) is not int or not 2 <= self.max_history_bars <= 2048:
            raise ValueError("technical analysis history must be a bounded integer")
        if (
            type(self.max_release_rows) is not int
            or not self.max_history_bars <= self.max_release_rows <= 20000
        ):
            raise ValueError("technical analysis canonical release scan exceeds resource budget")
        scalars = (self.bollinger_std, self.breakout_volume_multiplier)
        if any(type(value) not in (int, float) or not isfinite(value) for value in scalars):
            raise ValueError("technical analysis multipliers must be finite numbers")
        required = max(
            self.ema_slow + self.macd_signal - 1,
            self.rsi_period + 1,
            self.atr_period + 1,
            self.bollinger_period,
            self.volume_period + 1,
            self.breakout_period + 1,
        )
        if (
            not isinstance(self.policy_version, str)
            or not self.policy_version.strip()
            or self.ema_fast >= self.ema_slow
            or not 0 < self.bollinger_std <= 10
            or not 0 < self.breakout_volume_multiplier <= 100
            or required > self.max_history_bars
        ):
            raise ValueError("invalid technical analysis parameter bounds")

    @property
    def sha256(self) -> str:
        return content_hash(asdict(self))


def load_technical_analysis_policy(path: Path) -> TechnicalAnalysisPolicy:
    """Reject coercion/unknown keys rather than silently accepting typos or floats as periods."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "technical-analysis-policy-v1":
        raise ValueError("unsupported technical analysis policy")
    if set(raw) != {
        "schema_version",
        "policy_version",
        "max_history_bars",
        "max_release_rows",
        "indicators",
        "safety",
    }:
        raise ValueError("unknown technical analysis policy field")
    indicators = raw["indicators"]
    safety = raw["safety"]
    if not isinstance(indicators, dict) or not isinstance(safety, dict):
        raise ValueError("technical analysis policy sections are missing")
    required_safety = {
        "daily_only_initial_release": True,
        "network_allowed": False,
        "gpu_required": False,
        "standalone_recommendation_allowed": False,
        "paper_order_authority_allowed": False,
    }
    if set(safety) != set(required_safety) or any(
        safety[key] is not value for key, value in required_safety.items()
    ):
        raise ValueError("technical analysis must remain local, daily-only, and research-only")
    expected = {item.name for item in fields(TechnicalAnalysisPolicy)} - {
        "policy_version",
        "max_history_bars",
        "max_release_rows",
    }
    if set(indicators) != expected:
        raise ValueError("technical analysis indicator fields must match the policy contract")
    return TechnicalAnalysisPolicy(
        policy_version=raw["policy_version"],
        max_history_bars=raw["max_history_bars"],
        max_release_rows=raw["max_release_rows"],
        **indicators,
    )


__all__ = ["TechnicalAnalysisPolicy", "load_technical_analysis_policy"]
