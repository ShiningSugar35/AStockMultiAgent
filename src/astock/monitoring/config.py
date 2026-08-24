"""Versioned configuration for the continuous investment monitor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from astock.schemas.continuous_monitoring import MonitorSource


@dataclass(frozen=True, slots=True)
class MonitorMarketConfig:
    lookback_days: int
    one_day_bars: int
    five_day_bars: int
    volume_ratio_window: int


@dataclass(frozen=True, slots=True)
class MonitorNewsConfig:
    endpoint: str
    max_records_per_target: int
    lookback_minutes: int
    timeout_seconds: float
    material_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MonitorDaemonConfig:
    lease_seconds: int
    heartbeat_seconds: int
    pid_file: str
    log_file: str


@dataclass(frozen=True, slots=True)
class ContinuousMonitorConfig:
    policy_version: str
    wake_interval_seconds: int
    max_targets_per_cycle: int
    source_cadence_seconds: dict[MonitorSource, int]
    retry_backoff_seconds: tuple[int, ...]
    market: MonitorMarketConfig
    news: MonitorNewsConfig
    daemon: MonitorDaemonConfig
    scheduled_review_days: int = 30

    def cadence(self, source: MonitorSource) -> int:
        return self.source_cadence_seconds[source]


def load_continuous_monitor_config(path: Path) -> ContinuousMonitorConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid continuous monitor configuration: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "continuous-monitor-v1":
        raise ValueError("Unsupported continuous monitor configuration")
    safety = _mapping(payload, "safety")
    if safety.get("broker_execution_allowed") is not False:
        raise ValueError("continuous monitor may never enable broker execution")
    if safety.get("news_can_directly_trade") is not False:
        raise ValueError("news leads may never directly trade")
    if safety.get("natural_language_rule_execution_allowed") is not False:
        raise ValueError("natural-language rule execution must remain disabled")

    cadence_raw = _mapping(payload, "source_cadence_seconds")
    cadence: dict[MonitorSource, int] = {}
    for source in MonitorSource:
        if source is MonitorSource.PAPER:
            cadence[source] = int(cadence_raw.get(source.value, cadence_raw.get("MARKET_60M", 900)))
            continue
        if source.value not in cadence_raw:
            raise ValueError(f"continuous monitor cadence missing: {source.value}")
        cadence[source] = int(cadence_raw[source.value])
    if any(value < 30 for value in cadence.values()):
        raise ValueError("continuous monitor source cadence must be >= 30 seconds")

    retry = tuple(int(item) for item in payload.get("retry_backoff_seconds", []))
    if not retry or tuple(sorted(retry)) != retry or any(item <= 0 for item in retry):
        raise ValueError("continuous monitor retry backoff must be positive and sorted")

    market = _mapping(payload, "market")
    news = _mapping(payload, "news")
    daemon = _mapping(payload, "daemon")
    if str(news.get("authority")) != "LEAD_ONLY":
        raise ValueError("continuous monitor news authority must remain LEAD_ONLY")
    config = ContinuousMonitorConfig(
        policy_version=str(payload["policy_version"]),
        wake_interval_seconds=int(payload["wake_interval_seconds"]),
        max_targets_per_cycle=int(payload["max_targets_per_cycle"]),
        source_cadence_seconds=cadence,
        retry_backoff_seconds=retry,
        market=MonitorMarketConfig(
            lookback_days=int(market["lookback_days"]),
            one_day_bars=int(market["one_day_bars"]),
            five_day_bars=int(market["five_day_bars"]),
            volume_ratio_window=int(market["volume_ratio_window"]),
        ),
        news=MonitorNewsConfig(
            endpoint=str(news["endpoint"]),
            max_records_per_target=int(news["max_records_per_target"]),
            lookback_minutes=int(news["lookback_minutes"]),
            timeout_seconds=float(news["timeout_seconds"]),
            material_keywords=tuple(str(item).casefold() for item in news["material_keywords"]),
        ),
        daemon=MonitorDaemonConfig(
            lease_seconds=int(daemon["lease_seconds"]),
            heartbeat_seconds=int(daemon["heartbeat_seconds"]),
            pid_file=str(daemon["pid_file"]),
            log_file=str(daemon["log_file"]),
        ),
        scheduled_review_days=int(payload.get("scheduled_review_days", 30)),
    )
    if not (30 <= config.wake_interval_seconds <= 3600):
        raise ValueError("continuous monitor wake interval must be in 30..3600 seconds")
    if not (1 <= config.max_targets_per_cycle <= 500):
        raise ValueError("continuous monitor max target budget must be in 1..500")
    if not (60 <= config.daemon.lease_seconds <= 3600):
        raise ValueError("continuous monitor daemon lease must be in 60..3600 seconds")
    if not (5 <= config.daemon.heartbeat_seconds < config.daemon.lease_seconds):
        raise ValueError("continuous monitor heartbeat must be below its lease")
    return config


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"continuous monitor {key} must be a mapping")
    return value


__all__ = ["ContinuousMonitorConfig", "load_continuous_monitor_config"]
