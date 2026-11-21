"""Recorded, no-provider contract for the low-resource technical research kernel."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from astock.core.hashing import content_hash
from astock.schemas.market import AdjustmentMode, Market, VolumeUnit
from astock.schemas.reference_data import DailyBarObservation
from astock.schemas.technical import TechnicalIndicatorSnapshot
from astock.technical import TechnicalAnalysisService, load_technical_analysis_policy

ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _bars(
    closes: list[float],
    *,
    volumes: list[float] | None = None,
    symbol: str = "600001",
) -> list[DailyBarObservation]:
    volumes = volumes or [1000.0] * len(closes)
    assert len(volumes) == len(closes)
    result = []
    previous = Decimal(str(closes[0])) if closes else Decimal("1")
    for i, (close_value, volume_value) in enumerate(zip(closes, volumes, strict=True)):
        day = date(2025, 1, 2) + timedelta(days=i)
        close = Decimal(str(close_value))
        high = max(previous, close) + Decimal("1")
        low = min(previous, close) - Decimal("1")
        when = datetime.combine(day, time(15, 0), tzinfo=SHANGHAI)
        seen = when.astimezone(UTC) + timedelta(minutes=1)
        result.append(
            DailyBarObservation(
                observation_id=content_hash({"n": i, "symbol": symbol, "price": str(close)}),
                instrument_id=f"XSHG:{symbol}",
                market=Market.XSHG,
                symbol=symbol,
                session_date=day,
                session_close_at=when,
                open=previous,
                high=high,
                low=low,
                close=close,
                previous_close=previous if i > 0 else None,
                volume=Decimal(str(volume_value)),
                source_snapshot_id="snapshot:technical-test",
                available_to_system_at=seen,
                created_at=seen,
            )
        )
        previous = close
    return result


def _service() -> TechnicalAnalysisService:
    return TechnicalAnalysisService(
        load_technical_analysis_policy(ROOT / "configs" / "technical_analysis.yaml")
    )


def _build(bars: list[DailyBarObservation], **kwargs: object) -> TechnicalIndicatorSnapshot:
    args = {
        "as_of": bars[-1].available_to_system_at,
        "source_artifact_id": "market-reference:" + "a" * 64,
        "source_object_hash": "b" * 64,
    }
    args.update(kwargs)
    return _service().build(bars, **args)  # type: ignore[arg-type]


def test_flat_market_indicator_conventions_and_research_boundary() -> None:
    bars = _bars([100.0] * 100)
    result = _build(
        bars, expected_session_dates=[b.session_date for b in bars], corporate_action_dates=set()
    )
    assert result.ema_fast == 100.0
    assert result.ema_slow == 100.0
    assert result.macd_dif == result.macd_signal == result.macd_histogram == 0.0
    assert result.rsi == 50.0
    assert result.atr == 2.0
    assert result.bollinger_mid == result.bollinger_upper == result.bollinger_lower == 100.0
    assert result.volume_ratio == 1.0
    assert result.breakout_state == "NO_BREAKOUT"
    assert result.caution_codes == ()
    assert result.recommendation_allowed is False
    assert result.paper_order_authority_allowed is False


def test_wilder_rsi_rising_falling_and_early_history() -> None:
    rising = _build(_bars([100.0 + 0.3 * i for i in range(70)]))
    falling = _build(_bars([100.0 - 0.3 * i for i in range(70)]))
    short = _build(_bars([100.0] * 10))
    assert rising.rsi == 100.0
    assert falling.rsi == 0.0
    assert short.rsi is None and short.macd_signal is None and short.atr is None
    assert short.breakout_state == "INSUFFICIENT_HISTORY"
    assert "INDICATOR_HISTORY_PARTIAL" in short.caution_codes


def test_breakout_requires_complete_bar_and_relative_volume_confirmation() -> None:
    base = _bars([100.0] * 60 + [104.0], volumes=[1000.0] * 60 + [3000.0])
    confirmed = _build(base, corporate_action_dates=set())
    assert confirmed.breakout_reference_high == 101.0
    assert confirmed.volume_ratio == 3.0
    assert confirmed.breakout_state == "VOLUME_CONFIRMED"

    low_volume = _build(_bars([100.0] * 60 + [104.0]))
    assert low_volume.breakout_state == "PRICE_ONLY"
    no_trades = _build(_bars([100.0] * 60 + [104.0], volumes=[1000.0] * 60 + [0.0]))
    assert no_trades.breakout_state == "NO_TRADE"
    with pytest.raises(ValueError, match="future or open daily bar"):
        _build(base, as_of=base[-1].session_close_at)


def test_wrong_instrument_unordered_duplicate_and_adjusted_bars_are_rejected() -> None:
    bars = _bars([100.0 + 0.01 * i for i in range(60)])
    other = _bars([99.0] * 61, symbol="600002")[-1]
    with pytest.raises(ValueError, match="exact instrument"):
        _build([*bars, other])
    with pytest.raises(ValueError, match="ordered and unique"):
        _build([*bars, bars[-1]])
    with pytest.raises(ValueError, match="ordered and unique"):
        _build([*bars[:-2], bars[-1], bars[-2]], as_of=bars[-1].available_to_system_at)
    adjusted = bars[-1].model_copy(update={"adjustment_mode": AdjustmentMode.QFQ})
    with pytest.raises(ValueError, match="unadjusted"):
        _build([*bars[:-1], adjusted])
    wrong_volume = bars[-1].model_copy(update={"volume_unit": VolumeUnit.LOT_100_SHARES})
    with pytest.raises(ValueError, match="SHARE"):
        _build([*bars[:-1], wrong_volume])


def test_official_calendar_gap_is_not_silently_interpolated() -> None:
    bars = _bars([100.0] * 70)
    with pytest.raises(ValueError, match="official session dates"):
        _build([*bars[:35], *bars[36:]], expected_session_dates=[bar.session_date for bar in bars])
    assert "TRADING_CALENDAR_NOT_BOUND" in _build(bars).caution_codes


def test_raw_corporate_action_is_explicitly_non_actionable() -> None:
    bars = _bars([100.0] * 60 + [104.0], volumes=[1000.0] * 60 + [3000.0])
    result = _build(bars, corporate_action_dates={bars[-2].session_date})
    assert result.breakout_state == "CORPORATE_ACTION_REVIEW"
    assert "RAW_PRICE_CORPORATE_ACTION_REVIEW_REQUIRED" in result.caution_codes


def test_deterministic_hash_and_fixed_low_resource_window() -> None:
    bars = _bars([100.0 + 0.02 * i for i in range(600)])
    first = _build(bars)
    second = _build(bars)
    assert first == second
    assert first.snapshot_id == second.snapshot_id
    assert first.included_bar_count == 512
    assert first.input_sha256 == second.input_sha256
    changed_source = _build(bars, source_object_hash="e" * 64)
    assert changed_source.snapshot_id != first.snapshot_id
    with pytest.raises(ValueError, match="content hash mismatch"):
        TechnicalIndicatorSnapshot.model_validate(
            {**first.model_dump(mode="python"), "ema_fast": 999}
        )


def test_parameter_policy_disallows_trading_authority_and_extreme_costs() -> None:
    default = yaml.safe_load(
        (ROOT / "configs" / "technical_analysis.yaml").read_text(encoding="utf-8")
    )
    for update in (
        {"safety": {**default["safety"], "standalone_recommendation_allowed": True}},
        {"max_history_bars": 1_000_000},
        {"indicators": {**default["indicators"], "ema_fast": 50}},
    ):
        invalid = {**default, **update}
        with patch.object(Path, "read_text", return_value=yaml.safe_dump(invalid)):
            with pytest.raises(ValueError, match="technical analysis"):
                load_technical_analysis_policy(Path("unused.yaml"))
    real = _service().policy
    assert replace(real).max_history_bars == 512


def test_nontrivial_numeric_golden_vector_uses_independent_rational_results() -> None:
    policy = replace(
        _service().policy,
        ema_fast=3,
        ema_slow=5,
        macd_signal=2,
        rsi_period=3,
        atr_period=3,
        bollinger_period=3,
        volume_period=3,
        breakout_period=3,
    )
    bars = _bars([10, 11, 10, 13, 12, 15, 14, 16], volumes=[1, 2, 3, 4, 5, 6, 7, 10])
    result = TechnicalAnalysisService(policy).build(
        bars,
        as_of=bars[-1].available_to_system_at,
        source_artifact_id="reference:golden",
        source_object_hash="a" * 64,
    )
    assert result.ema_fast == pytest.approx(713 / 48)
    assert result.ema_slow == pytest.approx(1888 / 135)
    assert result.macd_dif == pytest.approx(1877 / 2160)
    assert result.macd_signal == pytest.approx(112 / 135)
    assert result.macd_histogram == pytest.approx(17 / 432)
    assert result.rsi == pytest.approx(8350 / 107)
    assert result.atr == pytest.approx(914 / 243)
    assert result.bollinger_mid == 15
    assert result.bollinger_upper == pytest.approx(15 + 2 * (2 / 3) ** 0.5)
    assert result.bollinger_lower == pytest.approx(15 - 2 * (2 / 3) ** 0.5)
    assert result.volume_ratio == pytest.approx(5 / 3)
    assert result.support_reference_low == 11
    assert result.breakout_reference_high == 16


def test_policy_parameters_are_hashed_even_with_same_version_and_equal_outputs() -> None:
    bars = _bars([100.0] * 100)
    original = _build(bars)
    other_policy = replace(_service().policy, bollinger_std=3.0)
    changed = TechnicalAnalysisService(other_policy).build(
        bars,
        as_of=bars[-1].available_to_system_at,
        source_artifact_id=original.source_artifact_id,
        source_object_hash=original.source_object_hash,
    )
    assert original.bollinger_upper == changed.bollinger_upper
    assert original.policy_version == changed.policy_version
    assert original.policy_sha256 != changed.policy_sha256
    assert original.snapshot_id != changed.snapshot_id
    assert TechnicalIndicatorSnapshot.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("ema_fast", True),
        ("rsi_period", 3.5),
        ("max_history_bars", 0),
        ("bollinger_std", float("nan")),
        ("breakout_volume_multiplier", float("inf")),
    ],
)
def test_direct_policy_construction_cannot_skip_configuration_boundary(field: str, value: object):
    with pytest.raises(ValueError, match="technical analysis"):
        replace(_service().policy, **{field: value})


@pytest.mark.parametrize(
    "updates",
    [
        {"close": Decimal("NaN")},
        {"high": Decimal("80")},
        {"volume": Decimal("-1")},
        {"close": Decimal("1e400")},
    ],
)
def test_invalid_unsafe_model_copy_is_rejected_at_calculation_boundary(updates):
    bars = _bars([100.0] * 70)
    bars[-1] = bars[-1].model_copy(update=updates)
    with pytest.raises(ValueError, match="OHLCV"):
        _build(bars)


def test_missing_latest_closed_session_is_not_misreported_as_current() -> None:
    bars = _bars([100.0] * 70)
    tomorrow = bars[-1].session_date + timedelta(days=1)
    with pytest.raises(ValueError, match="official session dates"):
        _build(
            bars,
            as_of=bars[-1].available_to_system_at + timedelta(days=1),
            expected_session_dates=[b.session_date for b in bars] + [tomorrow],
        )


def test_same_instant_different_timezone_produces_same_snapshot_identity() -> None:
    bars = _bars([100.0] * 70)
    original = _build(bars)
    changed = _build(bars, as_of=bars[-1].available_to_system_at.astimezone(SHANGHAI))
    assert original.snapshot_id == changed.snapshot_id
