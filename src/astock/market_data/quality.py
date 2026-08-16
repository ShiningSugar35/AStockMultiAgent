"""Deterministic 5m quality gates and cross-provider reconciliation."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import ceil
from zoneinfo import ZoneInfo

from astock.core.hashing import content_hash
from astock.schemas import (
    DataQualityReport,
    Frequency,
    MarketBar,
    MarketDataBatch,
    ProviderStatus,
    QualityStatus,
    ReplayQuality,
    TimestampSemantics,
    VolumeUnit,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PRICE_TOLERANCE = Decimal("0.01")
_CROSS_RULE_VERSION = "cross-5m-empirical-v1"
_MIN_COMMON_BARS = 48
_MIN_COMMON_HOURLY_BARS = 4
_MIN_COVERAGE_RATIO = Decimal("0.98")
_MAX_CLOSE_RELATIVE_P95 = Decimal("0.0015")
_MAX_OHLC_RELATIVE_P95 = Decimal("0.0020")
_MAX_OHLC_RELATIVE_MAX = Decimal("0.0100")
_MAX_VOLUME_RELATIVE_P95 = Decimal("0.0500")


def normalize_volume_to_shares(bar: MarketBar) -> Decimal:
    if bar.volume_unit == VolumeUnit.SHARE:
        return bar.volume
    if bar.volume_unit == VolumeUnit.LOT_100_SHARES:
        return bar.volume * 100
    raise ValueError(f"Unknown volume unit for {bar.provider_id}: {bar.volume_unit}")


def validate_batch(batch: MarketDataBatch) -> DataQualityReport:
    seen: set[datetime] = set()
    duplicates = 0
    ohlc_errors = 0
    for bar in batch.bars:
        if bar.timestamp in seen:
            duplicates += 1
        seen.add(bar.timestamp)
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
            bar.open, bar.close, bar.high
        ):
            ohlc_errors += 1
    missing = _missing_bar_labels(batch)
    hard_valid = (
        batch.bar_count > 0
        and duplicates == 0
        and ohlc_errors == 0
        and batch.bars[0].volume_unit != VolumeUnit.UNKNOWN
        and batch.bars[0].timestamp_semantics != TimestampSemantics.UNKNOWN
    )
    if not hard_valid:
        status = QualityStatus.FAIL
        replay = ReplayQuality.UNREPLAYABLE
    elif missing:
        status = QualityStatus.PARTIAL
        replay = (
            ReplayQuality.PROVIDER_1H_APPROX
            if batch.request.frequency is Frequency.H1
            else ReplayQuality.SINGLE_SOURCE_5M
        )
    else:
        status = QualityStatus.PASS
        replay = (
            ReplayQuality.PROVIDER_1H_APPROX
            if batch.request.frequency is Frequency.H1
            else ReplayQuality.SINGLE_SOURCE_5M
        )
    reasons: list[str] = []
    if batch.bar_count == 0:
        reasons.append("provider returned no bars")
    if duplicates:
        reasons.append(f"{duplicates} duplicate timestamps")
    if ohlc_errors:
        reasons.append(f"{ohlc_errors} OHLC relation errors")
    if missing:
        reasons.append(f"{len(missing)} missing bars within observed trading dates")
    report_payload = {
        "batch_ids": [batch.batch_id],
        "duplicates": duplicates,
        "ohlc_errors": ohlc_errors,
        "missing": missing,
        "status": status.value,
    }
    first_bar = batch.bars[0] if batch.bars else None
    return DataQualityReport(
        report_id=content_hash(report_payload),
        batch_ids=[batch.batch_id],
        symbol=batch.request.symbol,
        frequency=batch.request.frequency,
        requested_start=batch.requested_start,
        requested_end=batch.requested_end,
        actual_start=batch.actual_start,
        actual_end=batch.actual_end,
        bar_count=batch.bar_count,
        missing_sessions=missing,
        duplicate_bars=duplicates,
        ohlc_errors=ohlc_errors,
        volume_unit=first_bar.volume_unit if first_bar else VolumeUnit.UNKNOWN,
        adjustment_mode=batch.request.adjustment_mode,
        timestamp_semantics=(
            first_bar.timestamp_semantics if first_bar else TimestampSemantics.UNKNOWN
        ),
        provider_latency_ms=batch.provider_latency_ms,
        provider_status=batch.provider_status,
        quality_status=status,
        replay_quality=replay,
        reasons=reasons,
    )


def cross_validate_batches(
    primary: MarketDataBatch,
    secondary: MarketDataBatch,
) -> DataQualityReport:
    primary_report = validate_batch(primary)
    secondary_report = validate_batch(secondary)
    primary_by_time = {bar.timestamp: bar for bar in primary.bars}
    secondary_by_time = {bar.timestamp: bar for bar in secondary.bars}
    common_start = max(filter(None, [primary.actual_start, secondary.actual_start]), default=None)
    common_end = min(filter(None, [primary.actual_end, secondary.actual_end]), default=None)
    if common_start is not None and common_end is not None and common_start <= common_end:
        primary_keys = {key for key in primary_by_time if common_start <= key <= common_end}
        secondary_keys = {key for key in secondary_by_time if common_start <= key <= common_end}
    else:
        primary_keys = set()
        secondary_keys = set()
    overlap = primary_keys & secondary_keys
    union = primary_keys | secondary_keys
    coverage_ratio = len(overlap) / len(union) if union else 0.0
    ohlc_differences = 0
    volume_differences = 0
    close_relative_differences: list[Decimal] = []
    ohlc_relative_differences: list[Decimal] = []
    volume_relative_differences: list[Decimal] = []
    for timestamp in overlap:
        left = primary_by_time[timestamp]
        right = secondary_by_time[timestamp]
        absolute_price_differences = [
            abs(getattr(left, field) - getattr(right, field))
            for field in ("open", "high", "low", "close")
        ]
        relative_price_differences = [
            _relative_difference(getattr(left, field), getattr(right, field))
            for field in ("open", "high", "low", "close")
        ]
        close_relative_differences.append(relative_price_differences[-1])
        ohlc_relative_differences.append(max(relative_price_differences))
        if any(value > _PRICE_TOLERANCE for value in absolute_price_differences):
            ohlc_differences += 1
        try:
            left_volume = normalize_volume_to_shares(left)
            right_volume = normalize_volume_to_shares(right)
            denominator = max(left_volume, right_volume, Decimal("1"))
            volume_relative = abs(left_volume - right_volume) / denominator
            volume_relative_differences.append(volume_relative)
            if volume_relative > Decimal("0.01"):
                volume_differences += 1
        except ValueError:
            volume_differences += 1
            volume_relative_differences.append(Decimal("1"))
    close_relative_p95 = _nearest_rank_percentile(close_relative_differences, Decimal("0.95"))
    ohlc_relative_p95 = _nearest_rank_percentile(ohlc_relative_differences, Decimal("0.95"))
    ohlc_relative_max = max(ohlc_relative_differences, default=Decimal("1"))
    volume_relative_p95 = _nearest_rank_percentile(volume_relative_differences, Decimal("0.95"))
    minimum_common_bars = (
        _MIN_COMMON_HOURLY_BARS if primary.request.frequency is Frequency.H1 else _MIN_COMMON_BARS
    )
    dual_verified = (
        primary_report.quality_status != QualityStatus.FAIL
        and secondary_report.quality_status != QualityStatus.FAIL
        and len(overlap) >= minimum_common_bars
        and Decimal(str(coverage_ratio)) >= _MIN_COVERAGE_RATIO
        and close_relative_p95 <= _MAX_CLOSE_RELATIVE_P95
        and ohlc_relative_p95 <= _MAX_OHLC_RELATIVE_P95
        and ohlc_relative_max <= _MAX_OHLC_RELATIVE_MAX
        and volume_relative_p95 <= _MAX_VOLUME_RELATIVE_P95
        and primary_report.timestamp_semantics == secondary_report.timestamp_semantics
    )
    primary_usable = primary_report.quality_status != QualityStatus.FAIL
    if dual_verified:
        quality_status = QualityStatus.PASS
        replay_quality = (
            ReplayQuality.PROVIDER_1H_APPROX
            if primary.request.frequency is Frequency.H1
            else ReplayQuality.DUAL_SOURCE_5M_VERIFIED
        )
    elif primary_usable:
        quality_status = QualityStatus.PARTIAL
        replay_quality = (
            ReplayQuality.PROVIDER_1H_APPROX
            if primary.request.frequency is Frequency.H1
            else ReplayQuality.SINGLE_SOURCE_5M
        )
    else:
        quality_status = QualityStatus.FAIL
        replay_quality = ReplayQuality.UNREPLAYABLE
    reasons: list[str] = []
    if not dual_verified:
        reasons.append("dual-source verification threshold not met")
    if len(overlap) < minimum_common_bars:
        reasons.append(
            f"common bar count {len(overlap)} is below {minimum_common_bars} required bars"
        )
    if Decimal(str(coverage_ratio)) < _MIN_COVERAGE_RATIO:
        reasons.append(f"common-window timestamp coverage is {coverage_ratio:.3f}")
    if close_relative_p95 > _MAX_CLOSE_RELATIVE_P95:
        reasons.append(f"close relative p95 is {float(close_relative_p95):.6f}")
    if ohlc_relative_p95 > _MAX_OHLC_RELATIVE_P95:
        reasons.append(f"OHLC relative p95 is {float(ohlc_relative_p95):.6f}")
    if ohlc_relative_max > _MAX_OHLC_RELATIVE_MAX:
        reasons.append(f"OHLC relative max is {float(ohlc_relative_max):.6f}")
    if volume_relative_p95 > _MAX_VOLUME_RELATIVE_P95:
        reasons.append(f"normalized volume relative p95 is {float(volume_relative_p95):.6f}")
    cross_diffs: dict[str, int | float | str] = {
        "cross_rule_version": _CROSS_RULE_VERSION,
        "common_bar_count": len(overlap),
        "common_window_union_count": len(union),
        "coverage_ratio": round(coverage_ratio, 6),
        "ohlc_difference_count": ohlc_differences,
        "volume_difference_count": volume_differences,
        "close_relative_p95": round(float(close_relative_p95), 8),
        "ohlc_relative_p95": round(float(ohlc_relative_p95), 8),
        "ohlc_relative_max": round(float(ohlc_relative_max), 8),
        "volume_relative_p95": round(float(volume_relative_p95), 8),
        "minimum_common_bars": minimum_common_bars,
        "minimum_coverage_ratio": float(_MIN_COVERAGE_RATIO),
        "maximum_close_relative_p95": float(_MAX_CLOSE_RELATIVE_P95),
        "maximum_ohlc_relative_p95": float(_MAX_OHLC_RELATIVE_P95),
        "maximum_ohlc_relative_max": float(_MAX_OHLC_RELATIVE_MAX),
        "maximum_volume_relative_p95": float(_MAX_VOLUME_RELATIVE_P95),
        "secondary_quality": secondary_report.quality_status.value,
    }
    payload = {
        "batch_ids": [primary.batch_id, secondary.batch_id],
        "cross_diffs": cross_diffs,
        "quality": quality_status.value,
        "replay": replay_quality.value,
    }
    return DataQualityReport(
        report_id=content_hash(payload),
        batch_ids=[primary.batch_id, secondary.batch_id],
        symbol=primary.request.symbol,
        frequency=primary.request.frequency,
        requested_start=primary.requested_start,
        requested_end=primary.requested_end,
        actual_start=common_start,
        actual_end=common_end,
        bar_count=len(overlap),
        missing_sessions=sorted(
            set(primary_report.missing_sessions) | set(secondary_report.missing_sessions)
        ),
        duplicate_bars=primary_report.duplicate_bars + secondary_report.duplicate_bars,
        ohlc_errors=primary_report.ohlc_errors + secondary_report.ohlc_errors,
        volume_unit=VolumeUnit.SHARE,
        adjustment_mode=primary.request.adjustment_mode,
        timestamp_semantics=primary_report.timestamp_semantics,
        provider_latency_ms=primary.provider_latency_ms + secondary.provider_latency_ms,
        provider_status=(ProviderStatus.AVAILABLE if dual_verified else ProviderStatus.PARTIAL),
        cross_source_diffs=cross_diffs,
        quality_status=quality_status,
        replay_quality=replay_quality,
        reasons=reasons,
    )


def _missing_bar_labels(batch: MarketDataBatch) -> list[str]:
    if not batch.bars:
        return []
    semantics = batch.bars[0].timestamp_semantics
    if semantics not in {TimestampSemantics.BAR_END, TimestampSemantics.BAR_START}:
        return []
    grouped: dict[date, set[time]] = defaultdict(set)
    for bar in batch.bars:
        grouped[bar.timestamp.astimezone(_SHANGHAI).date()].add(
            bar.timestamp.astimezone(_SHANGHAI).time().replace(tzinfo=None)
        )
    now = datetime.now(_SHANGHAI)
    requested_start = batch.requested_start.astimezone(_SHANGHAI)
    requested_end = batch.requested_end.astimezone(_SHANGHAI)
    missing: list[str] = []
    for trading_date, observed in sorted(grouped.items()):
        expected = [
            item
            for item in _expected_times(semantics, batch.request.frequency)
            if requested_start
            <= datetime.combine(trading_date, item, tzinfo=_SHANGHAI)
            <= requested_end
        ]
        if trading_date == now.date() and now.time() < time(15, 0):
            expected = [item for item in expected if item <= now.time()]
        for item in expected:
            if item not in observed:
                missing.append(f"{trading_date.isoformat()}T{item.strftime('%H:%M')}")
    return missing


def _expected_times(semantics: TimestampSemantics, frequency: Frequency) -> list[time]:
    if frequency is Frequency.H1:
        if semantics == TimestampSemantics.BAR_END:
            return [time(10, 30), time(11, 30), time(14, 0), time(15, 0)]
        return [time(9, 30), time(10, 30), time(13, 0), time(14, 0)]
    if semantics == TimestampSemantics.BAR_END:
        morning_start, morning_end = time(9, 35), time(11, 30)
        afternoon_start, afternoon_end = time(13, 5), time(15, 0)
    else:
        morning_start, morning_end = time(9, 30), time(11, 25)
        afternoon_start, afternoon_end = time(13, 0), time(14, 55)
    return _time_range(morning_start, morning_end) + _time_range(afternoon_start, afternoon_end)


def _time_range(start: time, end: time) -> list[time]:
    anchor = date(2000, 1, 1)
    current = datetime.combine(anchor, start)
    final = datetime.combine(anchor, end)
    values: list[time] = []
    while current <= final:
        values.append(current.time())
        current += timedelta(minutes=5)
    return values


def _relative_difference(left: Decimal, right: Decimal) -> Decimal:
    denominator = max(abs(left), abs(right), Decimal("0.01"))
    return abs(left - right) / denominator


def _nearest_rank_percentile(values: list[Decimal], percentile: Decimal) -> Decimal:
    if not values:
        return Decimal("1")
    ordered = sorted(values)
    rank = max(1, ceil(float(percentile) * len(ordered)))
    return ordered[rank - 1]
