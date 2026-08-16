from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astock.core.errors import PolicyError
from astock.core.hashing import content_hash
from astock.market_data.quality import cross_validate_batches
from astock.market_data.storage import CanonicalMarketStore
from astock.paper_trading import LedgerService, PaperReplayService, load_fee_schedule
from astock.paper_trading.ledger import ReplayFillPlan
from astock.schemas import (
    Frequency,
    Market,
    OrderSide,
    OrderStatus,
    QualityStatus,
    ReplayCheckpoint,
    ReplayQuality,
    TradingSession,
    VolumeUnit,
)
from tests.helpers import make_batch

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _CalendarFixture:
    def calendar(self, market: Market, release_id: str, *, visible_at: datetime):
        return [
            TradingSession(
                exchange=market,
                session_date=date(2026, 7, 10),
                is_open=True,
                source_snapshot_id="calendar-fixture",
                available_to_system_at=visible_at - timedelta(days=1),
            )
        ]


def _bind_order(state, order, schedule, market: Market = Market.XSHG) -> None:
    operation_id = content_hash({"formal-replay-order": order.order_id})
    request_hash = content_hash({"request": operation_id})
    confirmation_id = content_hash({"confirmation": operation_id})
    now = order.submitted_at.isoformat()
    schedule_hash = content_hash(schedule.model_dump(mode="json", exclude={"created_at"}))
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO paper_operation_request(operation_id,account_id,operation_type,"
            "idempotency_key,request_hash,request_object_hash,requested_at,expires_at,payload_json,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                order.account_id,
                "PLACE_ORDER",
                operation_id,
                request_hash,
                request_hash,
                now,
                (order.submitted_at + timedelta(hours=1)).isoformat(),
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO paper_operation_confirmation(confirmation_id,operation_id,request_hash,"
            "confirmed_at,expires_at,confirmation_hash,confirmation_object_hash,key_id,nonce,"
            "signature_algorithm,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                confirmation_id,
                operation_id,
                request_hash,
                now,
                (order.submitted_at + timedelta(hours=1)).isoformat(),
                confirmation_id,
                confirmation_id,
                "fixture-key",
                f"nonce-{operation_id}",
                "ED25519",
                "{}",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO paper_operation_execution(operation_id,status,attempt_count,result_json,"
            "result_hash,report_object_hash,completed_at) VALUES(?,?,?,?,?,?,?)",
            (operation_id, "COMPLETE", 1, "{}", content_hash({}), confirmation_id, now),
        )
        connection.execute(
            "INSERT INTO paper_order_rule_binding(order_id,operation_id,market,symbol,"
            "instrument_id,"
            "board,risk_status,trading_rule_version,validity,expires_at,calendar_release_id,"
            "instrument_release_id,daily_release_id,fee_rule_version,fee_schedule_hash,"
            "confirmation_id,authorization_key_id,confirmation_hash,previous_close_fen,"
            "price_limit_bps,is_st) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.order_id,
                operation_id,
                market.value,
                order.symbol,
                f"{market.value}:{order.symbol}",
                "MAIN",
                "NORMAL",
                "fixture-trading-rule",
                "DAY",
                (order.submitted_at + timedelta(hours=6)).isoformat(),
                "1" * 64,
                "2" * 64,
                "3" * 64,
                schedule.rule_version,
                schedule_hash,
                confirmation_id,
                "fixture-key",
                confirmation_id,
                10000,
                1000,
                0,
            ),
        )


def test_hourly_replay_is_default_grade_and_uses_conservative_limit_price(
    tmp_path: Path, state, object_store
) -> None:
    east = make_batch(
        "eastmoney-5m",
        volume_unit=VolumeUnit.LOT_100_SHARES,
        frequency=Frequency.H1,
    )
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE, frequency=Frequency.H1)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    hourly_quality = cross_validate_batches(east, sina)
    assert hourly_quality.quality_status is QualityStatus.PASS
    assert hourly_quality.replay_quality is ReplayQuality.PROVIDER_1H_APPROX
    store.publish(
        east,
        hourly_quality,
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="hourly-buy",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=9_990,
        fee_reserve_fen=2_000,
        submitted_at=east.bars[0].timestamp - timedelta(hours=2),
    )
    fee_schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, order, fee_schedule)
    service = PaperReplayService(ledger, store, _CalendarFixture())

    report = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[0].timestamp,
        fee_schedule=fee_schedule,
        maximum_participation_rate=Decimal("0.05"),
    )

    assert report.replay_quality is ReplayQuality.PROVIDER_1H_APPROX
    assert report.processed_bars == 1
    assert len(report.fill_ids) == 1
    checkpoint = ledger.replay_checkpoint("paper", east.request.symbol)
    assert checkpoint is not None
    assert checkpoint.requested_resolution == "60m"
    assert checkpoint.actual_resolution == "60m"
    assert checkpoint.fallback_reason is not None
    with state.connect() as connection:
        fill = connection.execute(
            "SELECT price_fen FROM fill WHERE fill_id=?", (report.fill_ids[0],)
        ).fetchone()
    assert fill[0] == 9_990


def test_replay_matches_partial_limit_fills_and_resumes(
    tmp_path: Path, state, object_store
) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="replay-buy",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=200,
        limit_price_fen=10_020,
        fee_reserve_fen=2_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=6),
    )
    fee_schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, order, fee_schedule)
    service = PaperReplayService(ledger, store, _CalendarFixture())

    first = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[0].timestamp,
        fee_schedule=fee_schedule,
        maximum_participation_rate=Decimal("0.001"),
    )
    assert first.processed_bars == 1
    assert first.matched_orders == 1
    assert len(first.fill_ids) == 1
    assert (
        ledger.open_orders("paper", east.request.symbol)[0].status is OrderStatus.PARTIALLY_FILLED
    )

    repeated = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[0].timestamp,
        fee_schedule=fee_schedule,
        maximum_participation_rate=Decimal("0.001"),
    )
    assert repeated.processed_bars == 0
    assert repeated.fill_ids == []

    second = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[1].timestamp,
        fee_schedule=fee_schedule,
        maximum_participation_rate=Decimal("0.001"),
    )
    assert second.processed_bars == 1
    assert len(second.fill_ids) == 1
    status = ledger.status("paper")
    assert status["open_orders"] == []
    assert status["positions"][0]["qty_total"] == 200
    assert status["positions"][0]["qty_available"] == 0
    assert status["imbalanced_events"] == 0
    checkpoint = ledger.replay_checkpoint("paper", east.request.symbol)
    assert checkpoint is not None
    assert checkpoint.market_cursor == east.bars[1].timestamp.isoformat()
    with state.connect() as connection:
        fees = connection.execute(
            "SELECT commission_fen FROM fill ORDER BY occurred_at,fill_id"
        ).fetchall()
        assert [row[0] for row in fees] == [500, 100]
        assert (
            connection.execute("SELECT commission_fen FROM paper_fee_accrual").fetchone()[0] == 600
        )


def test_bar_fill_and_checkpoint_roll_back_together(tmp_path: Path, state, object_store) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="atomic-buy",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=2_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=1),
    )
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    checkpoint = ReplayCheckpoint(
        account_id="paper",
        market=east.request.market,
        instrument_id=f"{east.request.market.value}:{east.request.symbol}",
        symbol=east.request.symbol,
        actual_resolution="5m",
        replay_quality=ReplayQuality.DUAL_SOURCE_5M_VERIFIED,
        coverage_start=east.bars[0].timestamp,
        coverage_end=east.bars[0].timestamp,
        market_cursor=east.bars[0].timestamp.isoformat(),
    )
    plan: ReplayFillPlan = {
        "fill_id": content_hash({"atomic": order.order_id}),
        "order_id": order.order_id,
        "qty": 100,
        "price_fen": 10_000,
    }
    _bind_order(state, order, schedule)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ledger.commit_replay_bar(
            account_id="paper",
            symbol=east.request.symbol,
            bar_observation_id=east.bars[0].observation_id,
            input_hash=content_hash({"bar": east.bars[0].observation_id}),
            fill_plans=[plan],
            checkpoint=checkpoint,
            fee_schedule=schedule,
            interrupt_after_fills=True,
        )
    assert ledger.get_order(order.order_id).filled_qty == 0
    assert ledger.replay_checkpoint("paper", east.request.symbol) is None
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paper_replay_bar_commit").fetchone()[0] == 0

    fills, committed = ledger.commit_replay_bar(
        account_id="paper",
        symbol=east.request.symbol,
        bar_observation_id=east.bars[0].observation_id,
        input_hash=content_hash({"bar": east.bars[0].observation_id}),
        fill_plans=[plan],
        checkpoint=checkpoint,
        fee_schedule=schedule,
    )
    assert [item.fill_id for item in fills] == [plan["fill_id"]]
    assert committed.market_cursor == east.bars[0].timestamp.isoformat()


def test_two_orders_share_one_bar_capacity_in_stable_client_order(
    tmp_path: Path, state, object_store
) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    submitted = east.bars[0].timestamp - timedelta(minutes=6)
    first = ledger.place_order(
        account_id="paper",
        client_request_id="a-first",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=submitted,
    )
    second = ledger.place_order(
        account_id="paper",
        client_request_id="b-second",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=submitted,
    )
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, first, schedule)
    _bind_order(state, second, schedule)
    report = PaperReplayService(ledger, store, _CalendarFixture()).replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[0].timestamp,
        fee_schedule=schedule,
        maximum_participation_rate=Decimal("0.001"),
    )
    assert report.matched_orders == 1
    assert ledger.get_order(first.order_id).status is OrderStatus.FILLED
    assert ledger.get_order(second.order_id).status is OrderStatus.ACCEPTED


def test_single_source_quality_is_rejected_before_any_fill(
    tmp_path: Path, state, object_store
) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    report = cross_validate_batches(east, sina).model_copy(
        update={
            "quality_status": QualityStatus.PARTIAL,
            "replay_quality": ReplayQuality.SINGLE_SOURCE_5M,
        }
    )
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(east, report, source_batch_ids=[east.batch_id])
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="single-source",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=1),
    )
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, order, schedule)
    with pytest.raises(PolicyError, match="dual-source"):
        PaperReplayService(ledger, store, _CalendarFixture()).replay(
            account_id="paper",
            request=east.request,
            requested_cursor=east.bars[0].timestamp,
            fee_schedule=schedule,
        )
    assert ledger.get_order(order.order_id).filled_qty == 0
    assert ledger.replay_checkpoint("paper", east.request.symbol) is None


def test_zero_volume_suspension_bar_and_missing_bar_do_not_fill(
    tmp_path: Path, state, object_store
) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    east_bars = [east.bars[0].model_copy(update={"volume": Decimal(0)}), *east.bars[1:]]
    sina_bars = [sina.bars[0].model_copy(update={"volume": Decimal(0)}), *sina.bars[1:]]
    east = east.model_copy(update={"bars": east_bars})
    sina = sina.model_copy(update={"bars": sina_bars})
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="suspended",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=1),
    )
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, order, schedule)
    service = PaperReplayService(ledger, store, _CalendarFixture())
    missing = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[0].timestamp - timedelta(minutes=1),
        fee_schedule=schedule,
    )
    assert missing.processed_bars == 0
    assert ledger.replay_checkpoint("paper", east.request.symbol) is None
    with pytest.raises(PolicyError, match="Zero-volume boundary"):
        service.replay(
            account_id="paper",
            request=east.request,
            requested_cursor=east.bars[0].timestamp,
            fee_schedule=schedule,
        )
    assert ledger.replay_checkpoint("paper", east.request.symbol) is None
    assert ledger.get_order(order.order_id).filled_qty == 0


def test_formal_replay_rejects_legacy_order_without_advancing_checkpoint(
    tmp_path: Path, state, object_store
) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    ledger.place_order(
        account_id="paper",
        client_request_id="legacy-unbound",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=6),
    )
    with pytest.raises(PolicyError, match="legacy orders"):
        PaperReplayService(ledger, store, _CalendarFixture()).replay(
            account_id="paper",
            request=east.request,
            requested_cursor=east.bars[0].timestamp,
            fee_schedule=load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        )
    assert ledger.replay_checkpoint("paper", east.request.symbol) is None


def test_bar_end_mid_bar_order_waits_until_next_bar(tmp_path: Path, state, object_store) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="mid-bar",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=4),
    )
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, order, schedule)
    service = PaperReplayService(ledger, store, _CalendarFixture())
    first = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[0].timestamp,
        fee_schedule=schedule,
    )
    assert first.fill_ids == []
    second = service.replay(
        account_id="paper",
        request=east.request,
        requested_cursor=east.bars[1].timestamp,
        fee_schedule=schedule,
    )
    assert len(second.fill_ids) == 1


def test_canonical_gap_is_rejected_before_checkpoint(tmp_path: Path, state, object_store) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES, missing_index=1)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE, missing_index=1)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    forced_report = cross_validate_batches(east, sina).model_copy(
        update={
            "quality_status": QualityStatus.PASS,
            "replay_quality": ReplayQuality.DUAL_SOURCE_5M_VERIFIED,
        }
    )
    store.publish(
        east,
        forced_report,
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="gap",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_020,
        fee_reserve_fen=1_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=6),
    )
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    _bind_order(state, order, schedule)
    with pytest.raises(PolicyError, match="continuity gap"):
        PaperReplayService(ledger, store, _CalendarFixture()).replay(
            account_id="paper",
            request=east.request,
            requested_cursor=east.bars[1].timestamp,
            fee_schedule=schedule,
        )
    assert ledger.replay_checkpoint("paper", east.request.symbol) is None


def test_formal_fifo_lots_preserve_exact_remainder_cost(state, object_store) -> None:
    ledger = LedgerService(state, object_store)
    ledger.initialize_account("paper", 5_000_000)
    schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")
    acquired = datetime(2026, 7, 10, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    for index, price in enumerate((1001, 1002), start=1):
        order = ledger.place_order(
            account_id="paper",
            client_request_id=f"fifo-buy-{index}",
            symbol="600519",
            side=OrderSide.BUY,
            qty=100,
            limit_price_fen=price,
            fee_reserve_fen=1000,
            submitted_at=acquired - timedelta(minutes=1),
        )
        _bind_order(state, order, schedule)
        ledger.record_fill(
            fill_id=f"fifo-buy-fill-{index}",
            order_id=order.order_id,
            qty=100,
            price_fen=price,
            occurred_at=acquired + timedelta(minutes=index),
        )
    ledger.settle_buys(
        "paper",
        as_of=acquired + timedelta(days=1),
        trading_calendar_confirmed=True,
    )
    sell = ledger.place_order(
        account_id="paper",
        client_request_id="fifo-sell",
        symbol="600519",
        side=OrderSide.SELL,
        qty=100,
        limit_price_fen=1100,
        submitted_at=acquired + timedelta(days=1),
    )
    _bind_order(state, sell, schedule)
    ledger.record_fill(
        fill_id="fifo-sell-fill",
        order_id=sell.order_id,
        qty=100,
        price_fen=1100,
        occurred_at=acquired + timedelta(days=1, minutes=1),
    )
    position = ledger.status("paper")["positions"][0]
    assert (position["qty_total"], position["avg_cost_fen"]) == (100, 1002)
    with state.connect() as connection:
        assert (
            connection.execute("SELECT total_cost_fen FROM paper_position_cost").fetchone()[0]
            == 100200
        )
        lots = connection.execute(
            "SELECT remaining_qty,total_cost_fen FROM paper_position_lot ORDER BY acquired_at"
        ).fetchall()
    assert [tuple(row) for row in lots] == [(0, 0), (100, 100200)]
    assert (
        ledger.recover("paper", as_of=acquired + timedelta(days=1), expire_day_orders=False)[
            "status"
        ]
        == "HEALTHY_NOOP"
    )
