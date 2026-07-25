from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.core.errors import PolicyError
from astock.core.state import StateStore
from astock.paper_trading import LedgerService
from astock.schemas import OrderSide, ReplayCheckpoint, ReplayQuality


def test_buy_t1_sell_and_restart_recovery(state: StateStore) -> None:
    ledger = LedgerService(state)
    initialized = ledger.initialize_account("paper", 10_000_000)
    assert initialized.created
    assert not ledger.initialize_account("paper", 10_000_000).created

    order = ledger.place_order(
        account_id="paper",
        client_request_id="buy-1",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_000,
        fee_reserve_fen=500,
    )
    assert order.reserved_fen == 1_000_500
    assert (
        ledger.place_order(
            account_id="paper",
            client_request_id="buy-1",
            symbol="600519",
            side=OrderSide.BUY,
            qty=100,
            limit_price_fen=10_000,
            fee_reserve_fen=500,
        ).order_id
        == order.order_id
    )
    with pytest.raises(PolicyError, match="identity collision"):
        ledger.place_order(
            account_id="paper",
            client_request_id="buy-1",
            symbol="600519",
            side=OrderSide.BUY,
            qty=200,
            limit_price_fen=10_000,
            fee_reserve_fen=500,
        )

    trade_time = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    fill = ledger.record_fill(
        fill_id="fill-buy-1",
        order_id=order.order_id,
        qty=100,
        price_fen=10_000,
        commission_fen=500,
        occurred_at=trade_time,
    )
    assert (
        ledger.record_fill(
            fill_id="fill-buy-1",
            order_id=order.order_id,
            qty=100,
            price_fen=10_000,
            commission_fen=500,
            occurred_at=trade_time,
        ).fill_id
        == fill.fill_id
    )
    with pytest.raises(PolicyError, match="identity collision"):
        ledger.record_fill(
            fill_id="fill-buy-1",
            order_id=order.order_id,
            qty=100,
            price_fen=9_999,
            commission_fen=500,
            occurred_at=trade_time,
        )
    after_buy = ledger.status("paper")
    assert after_buy["positions"][0]["qty_total"] == 100
    assert after_buy["positions"][0]["qty_available"] == 0
    assert len(after_buy["pending_settlements"]) == 1
    with pytest.raises(PolicyError):
        ledger.settle_buys(
            "paper", as_of=trade_time + timedelta(days=1), trading_calendar_confirmed=False
        )
    assert (
        ledger.settle_buys(
            "paper", as_of=trade_time + timedelta(days=1), trading_calendar_confirmed=True
        )
        == 100
    )

    sell = ledger.place_order(
        account_id="paper",
        client_request_id="sell-1",
        symbol="600519",
        side=OrderSide.SELL,
        qty=100,
        limit_price_fen=11_000,
    )
    ledger.record_fill(
        fill_id="fill-sell-1",
        order_id=sell.order_id,
        qty=100,
        price_fen=11_000,
        commission_fen=500,
        tax_fen=500,
        occurred_at=trade_time + timedelta(days=2),
    )
    recovered = LedgerService(state).status("paper")
    assert recovered["positions"][0]["qty_total"] == 0
    assert recovered["balances_fen"]["CASH"] == 10_098_500
    assert recovered["imbalanced_events"] == 0
    assert recovered["integrity"] == "ok"
    assert LedgerService(state).portfolio_nav("paper").nav_fen == 10_098_500


def test_cancel_releases_frozen_cash(state: StateStore) -> None:
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 2_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="cancel-buy",
        symbol="000001",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_000,
    )
    assert ledger.status("paper")["balances_fen"]["FROZEN_CASH"] == 1_000_000
    ledger.cancel_order(order.order_id)
    status = ledger.status("paper")
    assert status["balances_fen"]["FROZEN_CASH"] == 0
    assert status["balances_fen"]["CASH"] == 2_000_000


def test_replay_cursor_is_monotonic(state: StateStore) -> None:
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1_000_000)
    first = ReplayCheckpoint(
        account_id="paper",
        symbol="600519",
        actual_resolution="5m",
        replay_quality=ReplayQuality.SINGLE_SOURCE_5M,
        market_cursor="2026-07-10T10:00:00+08:00",
    )
    ledger.save_replay_checkpoint(first)
    with pytest.raises(PolicyError, match="backwards"):
        ledger.save_replay_checkpoint(
            first.model_copy(update={"market_cursor": "2026-07-10T09:55:00+08:00"})
        )


def test_simulated_crash_rolls_back_half_journal(state: StateStore) -> None:
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1_000_000)
    with pytest.raises(RuntimeError, match="crash"):
        with state.transaction() as connection:
            connection.execute(
                "INSERT INTO journal(event_id,paper_account_id,event_type,occurred_at,"
                "idempotency_key,payload_json) VALUES(?,?,?,?,?,?)",
                ("half", "paper", "BROKEN", datetime.now(UTC).isoformat(), "half", "{}"),
            )
            raise RuntimeError("crash")
    with state.connect() as connection:
        assert connection.execute("SELECT 1 FROM journal WHERE event_id='half'").fetchone() is None


def test_t1_uses_next_verified_open_session_not_calendar_day(state: StateStore) -> None:
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 2_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="friday-buy",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_000,
        fee_reserve_fen=500,
    )
    friday = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    ledger.record_fill(
        fill_id="friday-fill",
        order_id=order.order_id,
        qty=100,
        price_fen=10_000,
        commission_fen=500,
        occurred_at=friday,
    )
    monday = (friday + timedelta(days=3)).date()
    assert (
        ledger.settle_buys_with_calendar(
            "paper",
            as_of=friday + timedelta(days=2),
            open_session_dates=[monday],
            calendar_release_id="calendar-v1",
        )
        == 0
    )
    assert (
        ledger.settle_buys_with_calendar(
            "paper",
            as_of=friday + timedelta(days=3),
            open_session_dates=[monday],
            calendar_release_id="calendar-v1",
        )
        == 100
    )
