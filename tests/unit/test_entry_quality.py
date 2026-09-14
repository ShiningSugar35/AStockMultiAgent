from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.entry_quality import (
    EntryQualityService,
    load_entry_quality_policy,
    persist_entry_quality_snapshot,
)
from astock.schemas.entry_quality import EntryQualityState
from astock.schemas.market import Market
from astock.schemas.reference_data import DailyBarObservation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _bars(prices: list[float], *, start: date = date(2025, 1, 2)) -> list[DailyBarObservation]:
    rows: list[DailyBarObservation] = []
    previous: Decimal | None = None
    for index, raw_price in enumerate(prices):
        session = start + timedelta(days=index)
        close = Decimal(str(round(raw_price, 4)))
        opened = previous or close
        high = max(opened, close) * Decimal("1.01")
        low = min(opened, close) * Decimal("0.99")
        close_at = datetime.combine(session, time(15, 0), tzinfo=SHANGHAI)
        payload = {
            "instrument_id": "XSHG:600001",
            "session_date": session.isoformat(),
            "close": str(close),
            "index": index,
        }
        rows.append(
            DailyBarObservation(
                observation_id=content_hash(payload),
                instrument_id="XSHG:600001",
                market=Market.XSHG,
                symbol="600001",
                session_date=session,
                session_close_at=close_at,
                open=opened,
                high=high,
                low=low,
                close=close,
                previous_close=previous,
                volume=Decimal("1000000"),
                amount=close * Decimal("1000000"),
                source_snapshot_id="snapshot:entry-quality",
                available_to_system_at=close_at.astimezone(UTC) + timedelta(minutes=1),
                created_at=close_at.astimezone(UTC) + timedelta(minutes=1),
            )
        )
        previous = close
    return rows


def _service() -> EntryQualityService:
    return EntryQualityService(
        load_entry_quality_policy(PROJECT_ROOT / "configs" / "entry_quality.yaml")
    )


def test_entry_quality_flags_falling_knife_instead_of_calling_it_low() -> None:
    prices = [100.0] * 230 + [100.0 - 2.0 * index for index in range(1, 21)]
    bars = _bars(prices)
    as_of = bars[-1].available_to_system_at

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "a" * 64,
        source_object_hash="b" * 64,
        as_of=as_of,
    )

    assert snapshot.state is EntryQualityState.FALLING_KNIFE_RISK
    assert snapshot.falling_knife_risk
    assert "DOWNTREND_NOT_LOW_RISK_ENTRY" in snapshot.reason_codes
    assert not snapshot.recommendation_allowed
    assert not snapshot.portfolio_weight_authority_allowed


def test_entry_quality_distinguishes_stabilized_dislocation_from_falling_price() -> None:
    prices = [120.0] * 100
    prices += [120.0 - (35.0 / 90.0) * index for index in range(1, 91)]
    prices += [86.0 + 0.08 * index for index in range(1, 61)]
    bars = _bars(prices)
    as_of = bars[-1].available_to_system_at

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "c" * 64,
        source_object_hash="d" * 64,
        as_of=as_of,
    )

    assert snapshot.state in {
        EntryQualityState.ATTRACTIVE_DISLOCATION,
        EntryQualityState.BASE_BUILDING,
    }
    assert not snapshot.falling_knife_risk
    assert snapshot.windows[-1].range_position < 0.5


def test_entry_quality_marks_extended_trend_without_turning_it_into_sell_authority() -> None:
    prices = [60.0 * (1.004**index) for index in range(250)]
    bars = _bars(prices)

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "e" * 64,
        source_object_hash="f" * 64,
        as_of=bars[-1].available_to_system_at,
    )

    assert snapshot.state in {EntryQualityState.EXTENDED, EntryQualityState.TREND_CONFIRMED}
    assert snapshot.trend_alignment_score >= 0.75
    assert not snapshot.recommendation_allowed


def test_entry_quality_same_current_inputs_are_deterministic() -> None:
    bars = _bars([80.0 + index * 0.1 for index in range(250)])
    as_of = bars[-1].available_to_system_at
    kwargs = {
        "source_artifact_id": "market-reference:" + "9" * 64,
        "source_object_hash": "8" * 64,
        "as_of": as_of,
        "current_price_override": Decimal("105.25"),
        "price_source_artifact_id": "price-anchor:" + "7" * 64,
        "price_source_object_hash": "6" * 64,
    }

    first = _service().build(bars, **kwargs)
    second = _service().build(bars, **kwargs)

    assert first == second
    assert first.entry_quality_id == second.entry_quality_id
    assert first.created_at == as_of
    assert all(window.created_at == as_of for window in first.windows)


def test_entry_quality_uses_current_market_anchor_override() -> None:
    bars = _bars([100.0 + index * 0.1 for index in range(250)])
    override = Decimal("131.23")

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "1" * 64,
        source_object_hash="2" * 64,
        as_of=bars[-1].available_to_system_at,
        current_price_override=override,
        price_source_artifact_id="price-anchor:" + "3" * 64,
        price_source_object_hash="4" * 64,
    )

    assert snapshot.current_price == override
    assert snapshot.moving_average_distance["MA20"] > 0


def test_entry_quality_ignores_unbound_current_price_override() -> None:
    bars = _bars([100.0 + index * 0.1 for index in range(250)])
    override = Decimal("131.23")

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "5" * 64,
        source_object_hash="6" * 64,
        as_of=bars[-1].available_to_system_at,
        current_price_override=override,
    )

    assert snapshot.current_price == bars[-1].close
    assert "PRICE_OVERRIDE_LINEAGE_UNAVAILABLE" in snapshot.reason_codes
    assert snapshot.price_source_artifact_id is None
    assert snapshot.price_source_object_hash is None


def test_entry_quality_degrades_when_history_is_too_short() -> None:
    bars = _bars([100.0 + index * 0.1 for index in range(30)])

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "3" * 64,
        source_object_hash="4" * 64,
        as_of=bars[-1].available_to_system_at,
    )

    assert snapshot.state is EntryQualityState.INSUFFICIENT_HISTORY
    assert snapshot.score <= 0.40


def test_entry_quality_does_not_promote_short_history_to_long_horizon_low() -> None:
    prices = [120.0] * 20
    prices += [120.0 - index for index in range(1, 31)]
    prices += [90.0 + 0.05 * index for index in range(1, 31)]
    bars = _bars(prices)

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "7" * 64,
        source_object_hash="8" * 64,
        as_of=bars[-1].available_to_system_at,
    )

    assert "PARTIAL_LONG_HORIZON_HISTORY" in snapshot.reason_codes
    assert "LONG_HORIZON_LOCATION_NOT_ADMITTED" in snapshot.reason_codes
    assert snapshot.state not in {
        EntryQualityState.ATTRACTIVE_DISLOCATION,
        EntryQualityState.BASE_BUILDING,
        EntryQualityState.EXTENDED,
    }
    assert snapshot.score <= _service().policy.partial_history_score_cap


def test_entry_quality_relative_strength_excludes_benchmark_rows_not_yet_available() -> None:
    bars = _bars([100.0 + index for index in range(60)])
    benchmark = [
        item.model_copy(
            update={
                "instrument_id": "XSHG:000300",
                "symbol": "000300",
            }
        )
        for item in _bars([100.0 + index * 0.5 for index in range(60)])
    ]
    as_of = bars[-1].available_to_system_at
    benchmark[-1] = benchmark[-1].model_copy(
        update={
            "available_to_system_at": as_of + timedelta(minutes=1),
            "created_at": as_of + timedelta(minutes=1),
        }
    )

    snapshot = _service().build(
        bars,
        source_artifact_id="market-reference:" + "5" * 64,
        source_object_hash="6" * 64,
        as_of=as_of,
        benchmark_bars=benchmark,
        benchmark_source_artifact_id="market-reference:" + "7" * 64,
        benchmark_source_object_hash="8" * 64,
    )

    assert snapshot.relative_strength_60d is None


def test_entry_quality_benchmark_lineage_is_identity_bearing_and_optional() -> None:
    bars = _bars([100.0 + index for index in range(60)])
    benchmark_a = [
        item.model_copy(update={"instrument_id": "XSHG:000300", "symbol": "000300"})
        for item in _bars([100.0 + index * 0.2 for index in range(60)])
    ]
    benchmark_b = [
        item.model_copy(update={"instrument_id": "XSHG:000905", "symbol": "000905"})
        for item in _bars([100.0 + index * 0.4 for index in range(60)])
    ]
    as_of = bars[-1].available_to_system_at

    first = _service().build(
        bars,
        source_artifact_id="market-reference:" + "9" * 64,
        source_object_hash="a" * 64,
        as_of=as_of,
        benchmark_bars=benchmark_a,
        benchmark_source_artifact_id="market-reference:" + "b" * 64,
        benchmark_source_object_hash="c" * 64,
    )
    second = _service().build(
        bars,
        source_artifact_id="market-reference:" + "9" * 64,
        source_object_hash="a" * 64,
        as_of=as_of,
        benchmark_bars=benchmark_b,
        benchmark_source_artifact_id="market-reference:" + "d" * 64,
        benchmark_source_object_hash="e" * 64,
    )
    degraded = _service().build(
        bars,
        source_artifact_id="market-reference:" + "9" * 64,
        source_object_hash="a" * 64,
        as_of=as_of,
        benchmark_bars=benchmark_a,
    )

    assert first.relative_strength_60d is not None
    assert second.relative_strength_60d is not None
    assert first.entry_quality_id != second.entry_quality_id
    assert degraded.relative_strength_60d is None
    assert "BENCHMARK_LINEAGE_UNAVAILABLE" in degraded.reason_codes


def test_persist_entry_quality_verifies_benchmark_parent(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects" / "sha256")
    source_id = "MarketReference:stock-bars"
    benchmark_id = "MarketReference:benchmark-bars"
    price_id = "MarketReference:price-anchor"
    source_ref = objects.put_json({"kind": "stock-bars"})
    benchmark_ref = objects.put_json({"kind": "benchmark-bars"})
    price_ref = objects.put_json({"kind": "price-anchor"})
    state.register_artifact(
        artifact_id=source_id,
        artifact_type="MarketReference",
        schema_version="test-v1",
        object_hash=source_ref.sha256,
        input_hashes=[],
    )
    state.register_artifact(
        artifact_id=benchmark_id,
        artifact_type="MarketReference",
        schema_version="test-v1",
        object_hash=benchmark_ref.sha256,
        input_hashes=[],
    )
    state.register_artifact(
        artifact_id=price_id,
        artifact_type="MarketReference",
        schema_version="test-v1",
        object_hash=price_ref.sha256,
        input_hashes=[],
    )
    bars = _bars([100.0 + index for index in range(60)])
    benchmark = [
        item.model_copy(update={"instrument_id": "XSHG:000300", "symbol": "000300"})
        for item in _bars([100.0 + index * 0.2 for index in range(60)])
    ]
    snapshot = _service().build(
        bars,
        source_artifact_id=source_id,
        source_object_hash=source_ref.sha256,
        as_of=bars[-1].available_to_system_at,
        current_price_override=Decimal("159.75"),
        price_source_artifact_id=price_id,
        price_source_object_hash=price_ref.sha256,
        benchmark_bars=benchmark,
        benchmark_source_artifact_id=benchmark_id,
        benchmark_source_object_hash=benchmark_ref.sha256,
    )

    artifact_id = persist_entry_quality_snapshot(state, objects, snapshot)
    assert state.artifact_record(artifact_id) is not None

    price_drifted = snapshot.model_copy(
        update={
            "entry_quality_id": snapshot.entry_quality_id + ":price-drifted",
            "price_source_object_hash": "0" * 64,
        }
    )
    with pytest.raises(ValueError, match="price artifact is unavailable or drifted"):
        persist_entry_quality_snapshot(state, objects, price_drifted)

    drifted = snapshot.model_copy(
        update={
            "entry_quality_id": snapshot.entry_quality_id + ":drifted",
            "benchmark_source_object_hash": "f" * 64,
        }
    )
    with pytest.raises(ValueError, match="benchmark artifact is unavailable or drifted"):
        persist_entry_quality_snapshot(state, objects, drifted)
