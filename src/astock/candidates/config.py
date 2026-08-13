"""Strict candidate-scan-v1 configuration loader."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from astock.schemas.candidates import CandidatePitStatus


@dataclass(frozen=True, slots=True)
class CandidateScanConfig:
    rules_version: str
    minimum_trading_days: int
    minimum_median_turnover_cny: Decimal
    minimum_nonzero_turnover_ratio: Decimal
    minimum_absolute_price_change: Decimal
    minimum_volume_ratio: Decimal
    canonical_announcement_events: frozenset[str]
    formal_historical_pit_statuses: frozenset[CandidatePitStatus]


def load_candidate_scan_config(path: Path) -> CandidateScanConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Candidate scan configuration is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("Unsupported candidate scan configuration")
    rules_version = str(raw.get("schema_version") or "")
    if not rules_version.startswith("candidate-scan-v") or not rules_version.removeprefix(
        "candidate-scan-v"
    ).isdigit():
        raise ValueError("Unsupported candidate scan configuration")
    events = raw.get("canonical_announcement_events")
    pit_statuses = raw.get("formal_historical_pit_statuses")
    if not isinstance(events, list) or not events:
        raise ValueError("Canonical announcement event list is empty")
    if not isinstance(pit_statuses, list) or not pit_statuses:
        raise ValueError("Formal historical PIT status list is empty")
    config = CandidateScanConfig(
        rules_version=str(raw["schema_version"]),
        minimum_trading_days=int(raw["minimum_trading_days"]),
        minimum_median_turnover_cny=Decimal(str(raw["minimum_median_turnover_cny"])),
        minimum_nonzero_turnover_ratio=Decimal(str(raw["minimum_nonzero_turnover_ratio"])),
        minimum_absolute_price_change=Decimal(str(raw["minimum_absolute_price_change"])),
        minimum_volume_ratio=Decimal(str(raw["minimum_volume_ratio"])),
        canonical_announcement_events=frozenset(str(item) for item in events),
        formal_historical_pit_statuses=frozenset(
            CandidatePitStatus(str(item)) for item in pit_statuses
        ),
    )
    if config.minimum_trading_days < 5 or config.minimum_trading_days > 252:
        raise ValueError("candidate scan trading-day window must be in 5..252")
    if config.minimum_median_turnover_cny < 0:
        raise ValueError("candidate scan turnover threshold cannot be negative")
    if not Decimal("0") <= config.minimum_nonzero_turnover_ratio <= Decimal("1"):
        raise ValueError("candidate scan nonzero ratio must be within 0..1")
    if config.minimum_absolute_price_change < 0:
        raise ValueError("candidate scan price-change threshold cannot be negative")
    if config.minimum_volume_ratio <= 0:
        raise ValueError("candidate scan volume ratio must be positive")
    if config.formal_historical_pit_statuses != {
        CandidatePitStatus.CERTIFIED,
        CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
    }:
        raise ValueError("candidate-scan-v1 formal PIT gate is frozen")
    return config


__all__ = ["CandidateScanConfig", "load_candidate_scan_config"]
