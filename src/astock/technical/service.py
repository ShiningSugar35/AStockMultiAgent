"""Bounded O(W) technical calculations; no provider, GPU, ledger, or model calls.

EMA/MACD/Wilder accumulators are constant-space. Only the requested tail (W<=2048,
512 by default) is retained for provenance and finite rolling price/volume windows.
MACD histogram uses DIF minus signal (1x, not the domestic charting 2x convention).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from math import fsum, isfinite, sqrt
from typing import Any
from zoneinfo import ZoneInfo

from astock.core.hashing import content_hash
from astock.schemas.market import AdjustmentMode, VolumeUnit
from astock.schemas.reference_data import DailyBarObservation
from astock.schemas.technical import TechnicalIndicatorSnapshot
from astock.technical.policy import TechnicalAnalysisPolicy

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class _Average:
    period: int
    alpha: float
    count: int = 0
    total: float = 0.0
    value: float | None = None

    def add(self, sample: float) -> float | None:
        self.count += 1
        if self.value is None:
            self.total += sample
            if self.count == self.period:
                self.value = self.total / self.period
        else:
            self.value += self.alpha * (sample - self.value)
        return self.value


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 8)


class TechnicalAnalysisService:
    """Compute research features from canonical closed daily bars, never an order signal."""

    def __init__(self, policy: TechnicalAnalysisPolicy):
        self.policy = policy

    def build(
        self,
        bars: Sequence[DailyBarObservation],
        *,
        as_of: datetime,
        source_artifact_id: str,
        source_object_hash: str,
        expected_session_dates: Sequence[date] | None = None,
        corporate_action_dates: set[date] | None = None,
    ) -> TechnicalIndicatorSnapshot:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("technical analysis as_of must be timezone-aware")
        if not bars:
            raise ValueError("technical analysis requires canonical daily bars")
        as_of = as_of.astimezone(UTC)
        visible = bars[-self.policy.max_history_bars :]
        instrument = visible[0].instrument_id
        policy = self.policy
        fast = _Average(policy.ema_fast, 2.0 / (policy.ema_fast + 1))
        slow = _Average(policy.ema_slow, 2.0 / (policy.ema_slow + 1))
        signal = _Average(policy.macd_signal, 2.0 / (policy.macd_signal + 1))
        gains = _Average(policy.rsi_period, 1.0 / policy.rsi_period)
        losses = _Average(policy.rsi_period, 1.0 / policy.rsi_period)
        ranges = _Average(policy.atr_period, 1.0 / policy.atr_period)
        previous_session: date | None = None
        prior_close: float | None = None
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        volumes: list[float] = []
        dif: float | None = None
        for bar in visible:
            if bar.instrument_id != instrument or instrument != f"{bar.market.value}:{bar.symbol}":
                raise ValueError("technical analysis requires one exact instrument identity")
            if bar.adjustment_mode is not AdjustmentMode.NONE:
                raise ValueError("technical analysis requires unadjusted canonical daily bars")
            if bar.volume_unit is not VolumeUnit.SHARE:
                raise ValueError("technical analysis requires SHARE-denominated volume")
            if previous_session is not None and bar.session_date <= previous_session:
                raise ValueError("technical bars must be strictly ordered and unique")
            expected_close = datetime.combine(bar.session_date, time(15), tzinfo=SHANGHAI)
            if (
                bar.session_close_at != expected_close
                or bar.available_to_system_at < expected_close
            ):
                raise ValueError("daily bar must bind its real Shanghai session close")
            if bar.available_to_system_at > as_of or bar.session_close_at > as_of:
                raise ValueError("technical analysis cannot consume a future or open daily bar")
            opened, high, low, close, volume = (
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
            )
            if (
                not all(isfinite(v) for v in (opened, high, low, close, volume))
                or min(opened, high, low, close) <= 0
                or volume < 0
                or high < max(opened, close, low)
                or low > min(opened, close, high)
            ):
                raise ValueError("invalid finite OHLCV data at technical calculation boundary")
            a, b = fast.add(close), slow.add(close)
            if a is not None and b is not None:
                dif = a - b
                signal.add(dif)
            if prior_close is not None:
                delta = close - prior_close
                gains.add(max(delta, 0.0))
                losses.add(max(-delta, 0.0))
                ranges.add(max(high - low, abs(high - prior_close), abs(low - prior_close)))
            closes.append(close)
            highs.append(high)
            lows.append(low)
            volumes.append(volume)
            previous_session, prior_close = bar.session_date, close

        # Verify the supplied calendar once, including the latest expected closed session.
        calendar: tuple[date, ...] | None = None
        if expected_session_dates is not None:
            calendar = tuple(expected_session_dates)
            if any(a >= b for a, b in zip(calendar, calendar[1:], strict=False)):
                raise ValueError("official session dates must be ordered and unique")
            relevant = tuple(
                day
                for day in calendar
                if day >= visible[0].session_date
                and datetime.combine(day, time(15), tzinfo=SHANGHAI) <= as_of
            )
            if not relevant or relevant != tuple(bar.session_date for bar in visible):
                raise ValueError("technical daily-bar coverage mismatches official session dates")

        rsi: float | None = None
        if gains.value is not None and losses.value is not None:
            total = gains.value + losses.value
            rsi = 50.0 if total == 0 else 100.0 * gains.value / total
        midpoint = upper = lower = None
        if len(closes) >= policy.bollinger_period:
            window = closes[-policy.bollinger_period :]
            midpoint = fsum(window) / len(window)
            sigma = sqrt(fsum((value - midpoint) ** 2 for value in window) / len(window))
            upper = midpoint + policy.bollinger_std * sigma
            lower = midpoint - policy.bollinger_std * sigma
        volume_ratio = None
        if len(volumes) > policy.volume_period:
            mean_volume = fsum(volumes[-policy.volume_period - 1 : -1]) / policy.volume_period
            if mean_volume > 0:
                volume_ratio = volumes[-1] / mean_volume
        resistance = support = None
        if len(visible) > policy.breakout_period:
            resistance = max(highs[-policy.breakout_period - 1 : -1])
            support = min(lows[-policy.breakout_period - 1 : -1])
        histogram = dif - signal.value if dif is not None and signal.value is not None else None
        cautions = []
        if calendar is None:
            cautions.append("TRADING_CALENDAR_NOT_BOUND")
        if corporate_action_dates is None:
            cautions.append("CORPORATE_ACTION_COVERAGE_NOT_BOUND")
        affected = bool(
            corporate_action_dates
            and any(
                visible[0].session_date <= day <= visible[-1].session_date
                for day in corporate_action_dates
            )
        )
        if affected:
            cautions.append("RAW_PRICE_CORPORATE_ACTION_REVIEW_REQUIRED")
        if None in (signal.value, rsi, ranges.value, midpoint):
            cautions.append("INDICATOR_HISTORY_PARTIAL")
        if volume_ratio is None:
            cautions.append("VOLUME_BASELINE_UNAVAILABLE")
        if volumes[-1] == 0:
            cautions.append("LATEST_BAR_HAS_NO_TRADES")
        if resistance is None:
            breakout = "INSUFFICIENT_HISTORY"
        elif volumes[-1] == 0:
            breakout = "NO_TRADE"
        elif affected:
            breakout = "CORPORATE_ACTION_REVIEW"
        elif closes[-1] <= resistance:
            breakout = "NO_BREAKOUT"
        elif volume_ratio is not None and volume_ratio >= policy.breakout_volume_multiplier:
            breakout = "VOLUME_CONFIRMED"
        else:
            breakout = "PRICE_ONLY"
        values: dict[str, Any] = {
            "schema_version": "technical-indicator-snapshot-v1",
            "created_at": as_of,
            "as_of": as_of,
            "instrument_id": instrument,
            "latest_session_date": visible[-1].session_date,
            "frequency": "1d",
            "adjustment_mode": "NONE",
            "policy_version": policy.policy_version,
            "policy_sha256": policy.sha256,
            "source_artifact_id": source_artifact_id,
            "source_object_hash": source_object_hash,
            "source_snapshot_ids": tuple(sorted({bar.source_snapshot_id for bar in visible})),
            "input_sha256": content_hash(
                [
                    {
                        "id": bar.observation_id,
                        "date": bar.session_date,
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": str(bar.volume),
                        "source": bar.source_snapshot_id,
                        "visible_at": bar.available_to_system_at.astimezone(UTC),
                    }
                    for bar in visible
                ]
            ),
            "context_sha256": content_hash(
                {
                    "calendar": calendar,
                    "corporate_action_dates": (
                        None if corporate_action_dates is None else sorted(corporate_action_dates)
                    ),
                }
            ),
            "included_bar_count": len(visible),
            "ema_fast": _rounded(fast.value),
            "ema_slow": _rounded(slow.value),
            "macd_dif": _rounded(dif),
            "macd_signal": _rounded(signal.value),
            "macd_histogram": _rounded(histogram),
            "rsi": _rounded(rsi),
            "atr": _rounded(ranges.value),
            "bollinger_mid": _rounded(midpoint),
            "bollinger_upper": _rounded(upper),
            "bollinger_lower": _rounded(lower),
            "volume_ratio": _rounded(volume_ratio),
            "breakout_reference_high": _rounded(resistance),
            "support_reference_low": _rounded(support),
            "breakout_state": breakout,
            "caution_codes": tuple(sorted(cautions)),
            "recommendation_allowed": False,
            "paper_order_authority_allowed": False,
        }
        return TechnicalIndicatorSnapshot(
            snapshot_id="technical:" + content_hash(values),
            **values,
        )


__all__ = ["TechnicalAnalysisService"]
