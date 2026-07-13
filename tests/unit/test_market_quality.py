from __future__ import annotations

from decimal import Decimal

from astock.market_data.quality import cross_validate_batches, validate_batch
from astock.schemas import QualityStatus, ReplayQuality, VolumeUnit
from tests.helpers import make_batch


def test_full_session_cross_source_is_dual_verified() -> None:
    eastmoney = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    report = cross_validate_batches(eastmoney, sina)
    assert report.quality_status is QualityStatus.PASS
    assert report.replay_quality is ReplayQuality.DUAL_SOURCE_5M_VERIFIED
    assert report.cross_source_diffs["common_bar_count"] == 48
    assert report.cross_source_diffs["volume_difference_count"] == 0


def test_one_missing_bar_downgrades_to_single_source() -> None:
    eastmoney = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE, missing_index=10)
    report = cross_validate_batches(eastmoney, sina)
    assert report.quality_status is QualityStatus.PARTIAL
    assert report.replay_quality is ReplayQuality.SINGLE_SOURCE_5M


def test_ohlc_relation_error_is_unreplayable() -> None:
    report = validate_batch(make_batch("bad", bad_ohlc=True))
    assert report.ohlc_errors == 1
    assert report.quality_status is QualityStatus.FAIL
    assert report.replay_quality is ReplayQuality.UNREPLAYABLE


def test_empirical_cross_source_noise_is_reported_but_can_verify() -> None:
    eastmoney = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    noisy_bars = []
    for index, bar in enumerate(sina.bars):
        if index % 5 == 0:
            bar = bar.model_copy(
                update={
                    "high": bar.high + Decimal("0.10"),
                    "volume": bar.volume * Decimal("1.02"),
                }
            )
        noisy_bars.append(bar)
    noisy = sina.model_copy(update={"bars": noisy_bars})
    report = cross_validate_batches(eastmoney, noisy)
    assert report.replay_quality is ReplayQuality.DUAL_SOURCE_5M_VERIFIED
    difference_count = report.cross_source_diffs["ohlc_difference_count"]
    assert isinstance(difference_count, int)
    assert difference_count > 0
    assert report.cross_source_diffs["cross_rule_version"] == "cross-5m-empirical-v1"


def test_material_cross_source_divergence_downgrades() -> None:
    eastmoney = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    divergent = sina.model_copy(
        update={
            "bars": [
                bar.model_copy(
                    update={
                        "open": bar.open * Decimal("1.02"),
                        "high": bar.high * Decimal("1.02"),
                        "low": bar.low * Decimal("1.02"),
                        "close": bar.close * Decimal("1.02"),
                    }
                )
                for bar in sina.bars
            ]
        }
    )
    report = cross_validate_batches(eastmoney, divergent)
    assert report.replay_quality is ReplayQuality.SINGLE_SOURCE_5M
    assert report.quality_status is QualityStatus.PARTIAL
