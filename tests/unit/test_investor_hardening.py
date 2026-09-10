"""Regression evidence for the independent investor-orchestration review.

Uses the canonical migration/account/ledger services, never production state.
These focused regressions are not a substitute for the 68 business E2E cases.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.ledger import LedgerService
from astock.schemas import OrderSide
from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
from astock.schemas.market import Market


@pytest.fixture
def canonical_state(tmp_path: Path) -> tuple[StateStore, InvestorOrchestrationStore]:
    state = StateStore(tmp_path / "state.sqlite")
    state.migrate()
    metadata = InvestorOrchestrationStore(state.path)
    metadata.initialize()
    return state, metadata


def _request(key: str, *, at: datetime | None = None) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope(
        request_id=key,
        idempotency_key=key,
        question_time=at or datetime.now(UTC),
        user_timezone="Asia/Shanghai",
        raw_text="recorded regression",
        normalized_intent=RequestIntent.PAPER_STATUS,
        side_effect=SideEffectClass.READ,
    )


def test_nav_snapshot_rejects_cross_account_and_is_consumed_once(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, _ = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("alpha", 111111)
    ledger.initialize_account("beta", 222222)
    ledger.prime_status_snapshot(ledger.status("alpha"))
    with pytest.raises(ValueError, match="different account"):
        ledger.portfolio_nav("beta")
    assert ledger.portfolio_nav("beta").cash_fen == 222222


def test_nav_snapshot_is_detached_and_avoids_duplicate_full_audit(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, _ = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    snapshot = ledger.status("paper")
    ledger.prime_status_snapshot(snapshot)
    snapshot["balances_fen"]["CASH"] = 0
    with patch.object(ledger, "status", side_effect=AssertionError("duplicate full audit")):
        nav = ledger.portfolio_nav("paper")
    assert nav.cash_fen == 1000000


def test_nav_snapshot_invalidates_after_new_ledger_event(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, _ = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    ledger.prime_status_snapshot(ledger.status("paper"))
    ledger.place_order(
        account_id="paper",
        client_request_id="reserve-new-cash",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=0,
    )
    with patch.object(ledger, "status", wraps=ledger.status) as status:
        nav = ledger.portfolio_nav("paper")
    assert status.call_count == 1
    assert nav.frozen_cash_fen > 0
    assert nav.cash_fen < 1000000


def test_preflight_reads_real_paper_account_positions_orders_and_cash(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, metadata = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="first-buy",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=0,
    )
    ledger.record_fill(
        fill_id="recorded-fill",
        order_id=order.order_id,
        qty=100,
        price_fen=1000,
        occurred_at=datetime.now(UTC),
    )
    open_order = ledger.place_order(
        account_id="paper",
        client_request_id="still-open",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=900,
        fee_reserve_fen=0,
    )
    expected = ledger.status("paper")
    receipt = InvestorSessionPreflightService(metadata).build(_request("canonical-paper"))
    assert receipt.context.paper.account_ids == ("paper",)
    assert len(receipt.context.paper.positions) == 1
    assert receipt.context.paper.positions[0].quantity == Decimal(100)
    assert receipt.context.paper.positions[0].average_cost == Decimal(10)
    assert [item.order_id for item in receipt.context.paper.open_orders] == [open_order.order_id]
    assert receipt.context.paper.known_cash == Decimal(expected["balances_fen"]["CASH"]) / 100
    assert not receipt.context.paper.unknown_cash
    assert not receipt.context.empty_holdings
    assert receipt.context.actual.positions == ()


def test_preflight_actual_accounts_use_append_only_events_not_fake_projection_tables(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
    tmp_path: Path,
) -> None:
    state, metadata = canonical_state
    repository = ExternalAccountRepository(state, ObjectStore(tmp_path / "objects"))
    at = datetime.now(UTC) - timedelta(seconds=1)
    for account, quantity in (("alpha", 100), ("beta", 200)):
        repository.create_account(account_id=account, display_name=account, created_at=at)
        repository.append_drafts(
            [
                ExternalAccountEventDraft(
                    account_id=account,
                    event_type=ExternalAccountEventType.TRADE,
                    occurred_at=at,
                    available_to_system_at=at,
                    created_at=at,
                    market=Market.XSHG,
                    symbol="600519",
                    side="BUY",
                    quantity=quantity,
                    price_cny=Decimal("10"),
                    idempotency_key=f"{account}-trade",
                )
            ]
        )
    # An unrelated projection must not be admitted merely because its name looks right.
    with state.transaction() as connection:
        connection.execute(
            "CREATE TABLE misleading_positions(account_id TEXT, symbol TEXT, quantity INT)"
        )
        connection.execute("INSERT INTO misleading_positions VALUES('fake','000000',99999)")
    receipt = InvestorSessionPreflightService(metadata).build(_request("canonical-actual"))
    positions = receipt.context.actual.positions
    assert {item.account_id: item.quantity for item in positions} == {
        "alpha": Decimal(100),
        "beta": Decimal(200),
    }
    assert receipt.context.actual.account_ids == ("alpha", "beta")
    assert receipt.context.actual.unknown_cash
    assert receipt.context.actual.known_cash is None
    assert receipt.context.paper.positions == ()


def test_preflight_warm_revision_detects_same_size_position_update(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, metadata = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    service = InvestorSessionPreflightService(metadata)
    first = service.build(_request("revision-before"))
    ledger.place_order(
        account_id="paper",
        client_request_id="new-order",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=0,
    )
    second = service.build(_request("revision-after"))
    assert first.source_revision_vector["paper"] != second.source_revision_vector["paper"]
    assert len(second.context.paper.open_orders) == 1
    with closing(state.connect()) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


def test_preflight_cache_cannot_be_polluted_by_mutating_returned_cash(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, metadata = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    service = InvestorSessionPreflightService(metadata)
    at = datetime.now(UTC)
    first = service.build(_request("cache-copy-1", at=at))
    first.context.paper.cash_by_account["paper"] = Decimal("9999999")
    second = service.build(_request("cache-copy-2", at=at))
    assert second.built_from_cache
    assert second.context.paper.cash_by_account == {"paper": Decimal("10000")}
    assert first.context.paper.known_cash == second.context.paper.known_cash


def test_preflight_cache_respects_availability_advancing_without_database_changes(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
    tmp_path: Path,
) -> None:
    state, metadata = canonical_state
    repository = ExternalAccountRepository(state, ObjectStore(tmp_path / "objects"))
    base = datetime.now(UTC) - timedelta(minutes=10)
    repository.create_account(account_id="alpha", display_name="alpha", created_at=base)
    repository.append_drafts(
        [
            ExternalAccountEventDraft(
                account_id="alpha",
                event_type=ExternalAccountEventType.TRADE,
                occurred_at=base,
                available_to_system_at=base + timedelta(seconds=30),
                created_at=base + timedelta(seconds=30),
                market=Market.XSHG,
                symbol="600519",
                side="BUY",
                quantity=100,
                price_cny=Decimal("10"),
                idempotency_key="available-later",
            )
        ]
    )
    service = InvestorSessionPreflightService(metadata)
    first = service.build(_request("pit-before", at=base + timedelta(seconds=10)))
    later = service.build(_request("pit-after", at=base + timedelta(seconds=40)))
    earlier_again = service.build(_request("pit-backward", at=base + timedelta(seconds=10)))
    assert first.source_revision_vector == later.source_revision_vector
    assert first.context.actual.positions == ()
    assert len(later.context.actual.positions) == 1
    assert not later.built_from_cache
    assert earlier_again.context.actual.positions == ()
    assert not earlier_again.built_from_cache


def test_current_paper_projection_is_not_echoed_into_a_historical_request(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, metadata = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    with pytest.raises(ValueError, match="before its ledger events"):
        InvestorSessionPreflightService(metadata).build(
            _request("history-not-supported", at=datetime.now(UTC) - timedelta(days=1))
        )


def test_canonical_metadata_transaction_rolls_back_nested_writes(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    _, metadata = canonical_state
    with pytest.raises(RuntimeError, match="injected crash"):
        with metadata.transaction():
            with metadata.connect() as connection:
                connection.execute(
                    "INSERT INTO orchestration_schema_migrations "
                    "VALUES('rollback-probe','hash','time')"
                )
            raise RuntimeError("injected crash")
    with metadata.connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM orchestration_schema_migrations WHERE version='rollback-probe'"
            ).fetchone()
            is None
        )


def test_canonical_read_transaction_rejects_even_metadata_writes(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    import sqlite3

    _, metadata = canonical_state
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with metadata.transaction(read_only=True):
            with metadata.connect() as connection:
                connection.execute(
                    "INSERT INTO orchestration_schema_migrations "
                    "VALUES('readonly-probe','hash','time')"
                )
    with metadata.connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM orchestration_schema_migrations WHERE version='readonly-probe'"
            ).fetchone()
            is None
        )


def test_canonical_revision_changes_for_in_place_order_state_changes(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    state, metadata = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 1000000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="cancel-me",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        fee_reserve_fen=0,
    )
    service = InvestorSessionPreflightService(metadata)
    first = service.build(_request("order-open"))
    # Test-only injected in-place mutation: row count and ID remain unchanged.
    with state.transaction() as connection:
        connection.execute(
            "UPDATE order_record SET status='CANCELLED' WHERE order_id=?", (order.order_id,)
        )
    assert first.source_revision_vector["paper"] != service.reader.revision_vector()["paper"]
    # Invalid cancellation did not release frozen cash; it must fail, not hide the problem.
    with pytest.raises(ValueError, match="frozen cash"):
        service.build(_request("invalid-cancellation"))


def test_canonical_concurrent_same_revision_builds_one_snapshot(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    state, metadata = canonical_state
    LedgerService(state).initialize_account("paper", 1000000)
    service = InvestorSessionPreflightService(metadata)
    at = datetime.now(UTC)
    requests = [_request(f"coalesced-{index}", at=at) for index in range(8)]
    with patch.object(service, "_build_state", wraps=service._build_state) as build:
        with ThreadPoolExecutor(max_workers=8) as executor:
            receipts = tuple(executor.map(service.build, requests))
    assert build.call_count == 1
    assert sum(receipt.built_from_cache for receipt in receipts) == 7
    assert all(
        receipt.context.paper.cash_by_account == {"paper": Decimal("10000")} for receipt in receipts
    )


def test_canonical_preflight_100_warm_requests_meet_per_request_p95(
    canonical_state: tuple[StateStore, InvestorOrchestrationStore],
) -> None:
    import json
    import math
    import time

    state, metadata = canonical_state
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 100000000)
    for index in range(20):
        ledger.place_order(
            account_id="paper",
            client_request_id=f"benchmark-order-{index}",
            symbol="600519",
            side=OrderSide.BUY,
            qty=100,
            limit_price_fen=1000,
            fee_reserve_fen=0,
        )
    service = InvestorSessionPreflightService(metadata)
    economic_tables = (
        "external_account_event",
        "paper_account",
        "journal",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
        "position_settlement",
    )

    def economic_rows() -> dict[str, list[tuple[object, ...]]]:
        with closing(state.connect()) as connection:
            return {
                table: [
                    tuple(row)
                    for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
                ]
                for table in economic_tables
            }

    before = economic_rows()
    start = time.perf_counter()
    service.build(_request("benchmark-cold"))
    cold_seconds = time.perf_counter() - start
    elapsed = []
    for index in range(100):
        start = time.perf_counter()
        receipt = service.build(_request(f"benchmark-warm-{index}"))
        elapsed.append(time.perf_counter() - start)
        assert receipt.built_from_cache
        assert len(receipt.context.paper.open_orders) == 20
    after = economic_rows()
    economic_table_changes = sum(before[table] != after[table] for table in economic_tables)
    assert economic_table_changes == 0
    ordered = sorted(elapsed)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    print(
        json.dumps(
            {
                "benchmark": "canonical-preflight-v2",
                "accounts": 1,
                "open_orders": 20,
                "warm_samples": 100,
                "cold_seconds": cold_seconds,
                "p50_seconds": ordered[49],
                "p95_seconds": p95,
                "max_seconds": max(elapsed),
                "total_warm_seconds": sum(elapsed),
                "economic_table_changes": economic_table_changes,
                "note": "Includes durable receipts; excludes fixture setup. No external calls.",
            },
            sort_keys=True,
        )
    )
    assert p95 <= 2.0
