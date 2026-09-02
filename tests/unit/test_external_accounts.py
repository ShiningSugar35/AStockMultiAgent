from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import (
    ExternalAccountConflictError,
    ExternalAccountRepository,
)
from astock.local_portfolio import LocalPortfolioService
from astock.paper_trading import LedgerService
from astock.schemas import OrderSide
from astock.schemas.external_accounts import (
    ExternalAccountEventDraft,
    ExternalAccountEventType,
    bind_external_account_event,
)
from astock.schemas.market import Market

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)


def _repository(tmp_path: Path) -> tuple[ExternalAccountRepository, StateStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return ExternalAccountRepository(state, ObjectStore(tmp_path / "objects")), state


def _draft(
    account_id: str,
    event_type: ExternalAccountEventType,
    idempotency_key: str,
    *,
    occurred_at: datetime = NOW,
    available_to_system_at: datetime | None = None,
    market: Market | None = None,
    symbol: str | None = None,
    side: Literal["BUY", "SELL"] | None = None,
    quantity: int | None = None,
    price_cny: Decimal | str | None = None,
    amount_cny: Decimal | str | None = None,
    reverses_event_id: str | None = None,
    replaces_event_id: str | None = None,
) -> ExternalAccountEventDraft:
    return ExternalAccountEventDraft(
        account_id=account_id,
        event_type=event_type,
        occurred_at=occurred_at,
        available_to_system_at=available_to_system_at or occurred_at,
        market=market,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price_cny=(Decimal(str(price_cny)) if price_cny is not None else None),
        amount_cny=(Decimal(str(amount_cny)) if amount_cny is not None else None),
        reverses_event_id=reverses_event_id,
        replaces_event_id=replaces_event_id,
        idempotency_key=idempotency_key,
        created_at=available_to_system_at or occurred_at,
    )


def _buy(
    account_id: str,
    key: str,
    *,
    quantity: int = 100,
    price: str = "10",
    occurred_at: datetime = NOW,
    replaces_event_id: str | None = None,
) -> ExternalAccountEventDraft:
    return _draft(
        account_id,
        ExternalAccountEventType.TRADE,
        key,
        occurred_at=occurred_at,
        market=Market.XSHG,
        symbol="600519",
        side="BUY",
        quantity=quantity,
        price_cny=price,
        replaces_event_id=replaces_event_id,
    )


def test_migration_is_append_only_and_account_creation_is_idempotent(
    tmp_path: Path,
) -> None:
    repository, state = _repository(tmp_path)
    account = repository.create_account(
        account_id="default",
        display_name="默认外部账户",
        created_at=NOW,
    )
    repeated = repository.create_account(
        account_id="default",
        display_name="默认外部账户",
        created_at=NOW + timedelta(seconds=1),
    )
    assert repeated.account_id == account.account_id

    event = bind_external_account_event(_buy("default", "buy-1"))
    inserted, duplicates = repository.append_events([event])
    assert inserted == [event.event_id]
    assert duplicates == []

    with closing(state.connect()) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE external_account_event SET payload_json='{}' WHERE event_id=?",
                (event.event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM external_account_event WHERE event_id=?",
                (event.event_id,),
            )


def test_same_instrument_is_isolated_between_accounts(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="alpha", display_name="账户甲", created_at=NOW)
    repository.create_account(account_id="beta", display_name="账户乙", created_at=NOW)
    repository.append_drafts(
        [
            _buy("alpha", "alpha-buy", quantity=100, price="10"),
            _buy("beta", "beta-buy", quantity=200, price="20"),
        ]
    )

    alpha = repository.projection("alpha", as_of=NOW)
    beta = repository.projection("beta", as_of=NOW)
    assert alpha.positions[0].quantity == 100
    assert alpha.positions[0].average_cost_cny == Decimal("10")
    assert beta.positions[0].quantity == 200
    assert beta.positions[0].average_cost_cny == Decimal("20")
    assert not alpha.cash_known and alpha.cash_cny is None
    assert not beta.cash_known and beta.cash_cny is None


def test_cash_trade_and_security_transfer_replay(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    repository.append_drafts(
        [
            _draft(
                "default",
                ExternalAccountEventType.CASH_DEPOSIT,
                "deposit",
                amount_cny="10000",
            ),
            _buy("default", "buy", quantity=100, price="10"),
            _draft(
                "default",
                ExternalAccountEventType.SECURITY_TRANSFER_IN,
                "transfer-in",
                market=Market.XSHG,
                symbol="600519",
                quantity=50,
                price_cny="12",
            ),
            _draft(
                "default",
                ExternalAccountEventType.SECURITY_TRANSFER_OUT,
                "transfer-out",
                market=Market.XSHG,
                symbol="600519",
                quantity=20,
            ),
            _draft(
                "default",
                ExternalAccountEventType.CASH_WITHDRAWAL,
                "withdrawal",
                amount_cny="500",
            ),
        ]
    )

    projection = repository.projection("default", as_of=NOW)
    assert projection.cash_known
    assert projection.cash_cny == Decimal("8500")
    assert projection.positions[0].quantity == 130
    assert projection.positions[0].average_cost_cny == Decimal("1600") / Decimal("150")


def test_reversal_and_replacement_preserve_history(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    original = bind_external_account_event(_buy("default", "original"))
    repository.append_events([original])
    replacement = bind_external_account_event(
        _buy(
            "default",
            "replacement",
            quantity=120,
            price="11",
            occurred_at=NOW,
            replaces_event_id=original.event_id,
        )
    )
    repository.append_events([replacement])

    projection = repository.projection("default", as_of=NOW)
    assert projection.positions[0].quantity == 120
    assert projection.positions[0].average_cost_cny == Decimal("11")
    assert projection.total_event_count == 2
    assert [item.event_id for item in repository.list_events("default")] == sorted(
        [original.event_id, replacement.event_id]
    )

    reversal = _draft(
        "default",
        ExternalAccountEventType.REVERSAL,
        "late-reversal",
        reverses_event_id=original.event_id,
    )
    with pytest.raises(ExternalAccountConflictError, match="already has a correction"):
        repository.append_drafts([reversal])
    assert len(repository.list_events("default")) == 2


def test_plain_reversal_removes_effect_without_deleting_event(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    original = bind_external_account_event(_buy("default", "buy"))
    repository.append_events([original])
    reversal = bind_external_account_event(
        _draft(
            "default",
            ExternalAccountEventType.REVERSAL,
            "reverse",
            occurred_at=NOW + timedelta(minutes=1),
            reverses_event_id=original.event_id,
        )
    )
    repository.append_events([reversal])

    projection = repository.projection("default", as_of=NOW + timedelta(minutes=1))
    assert projection.positions == []
    assert projection.active_event_count == 0
    assert projection.total_event_count == 2
    assert len(repository.list_events("default")) == 2


def test_batch_correction_insert_order_does_not_depend_on_file_row_order(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    original = bind_external_account_event(_buy("default", "ordered-original"))
    replacement = bind_external_account_event(
        _buy(
            "default",
            "ordered-replacement",
            quantity=150,
            price="12",
            occurred_at=NOW + timedelta(minutes=1),
            replaces_event_id=original.event_id,
        )
    )

    inserted, duplicates = repository.append_events([replacement, original])
    assert set(inserted) == {original.event_id, replacement.event_id}
    assert duplicates == []
    projection = repository.projection("default", as_of=NOW + timedelta(minutes=2))
    assert projection.positions[0].quantity == 150
    assert projection.positions[0].average_cost_cny == Decimal("12")


def test_cross_account_correction_is_atomic_and_rejected(tmp_path: Path) -> None:
    repository, state = _repository(tmp_path)
    repository.create_account(account_id="alpha", display_name="账户甲", created_at=NOW)
    repository.create_account(account_id="beta", display_name="账户乙", created_at=NOW)
    original = bind_external_account_event(_buy("alpha", "buy"))
    repository.append_events([original])

    reversal = _draft(
        "beta",
        ExternalAccountEventType.REVERSAL,
        "cross-account-reversal",
        reverses_event_id=original.event_id,
    )
    with pytest.raises(ExternalAccountConflictError, match="another account"):
        repository.append_drafts([reversal])
    with closing(state.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM external_account_event").fetchone()[0]
    assert count == 1


def test_event_reappend_is_idempotent_but_key_drift_fails_closed(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    first = bind_external_account_event(_buy("default", "stable-key"))
    repeated = bind_external_account_event(
        _buy("default", "stable-key").model_copy(update={"created_at": NOW + timedelta(days=1)})
    )
    repository.append_events([first])
    inserted, duplicates = repository.append_events([repeated])
    assert inserted == []
    assert duplicates == [first.event_id]

    drifted = bind_external_account_event(_buy("default", "stable-key", quantity=200))
    with pytest.raises(ExternalAccountConflictError, match="idempotency conflict"):
        repository.append_events([drifted])


def test_csv_import_is_atomic_and_confirm_is_idempotent(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    path = tmp_path / "events.csv"
    path.write_text(
        "account_id,event_type,occurred_at,market,symbol,side,quantity,price_cny,amount_cny\n"
        "default,CASH_DEPOSIT,2026-09-01T01:00:00+00:00,,,,,,5000\n"
        "default,TRADE,2026-09-01T01:00:00+00:00,XSHG,600519,BUY,100,10,\n",
        encoding="utf-8",
    )

    preview = repository.preview_import(path, previewed_at=NOW)
    assert preview.row_count == 2
    receipt = repository.confirm_import(preview.batch_id, source_path=path, confirmed_at=NOW)
    assert receipt.status == "IMPORTED"
    assert len(receipt.inserted_event_ids) == 2
    duplicate = repository.confirm_import(
        preview.batch_id,
        source_path=path,
        confirmed_at=NOW + timedelta(seconds=1),
    )
    assert duplicate.status == "DUPLICATE"
    assert duplicate.duplicate_event_ids == preview.event_ids
    projection = repository.projection("default", as_of=NOW)
    assert projection.cash_cny == Decimal("4000")
    assert projection.positions[0].quantity == 100


def test_json_import_and_source_drift_guard(tmp_path: Path) -> None:
    repository, state = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            [
                {
                    "account_id": "default",
                    "event_type": "CASH_ADJUSTMENT",
                    "occurred_at": "2026-09-01T01:00:00+00:00",
                    "amount_cny": "1200",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    preview = repository.preview_import(path, previewed_at=NOW)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ExternalAccountConflictError, match="changed after preview"):
        repository.confirm_import(preview.batch_id, source_path=path, confirmed_at=NOW)
    with closing(state.connect()) as connection:
        count = connection.execute("SELECT COUNT(*) FROM external_account_event").fetchone()[0]
        status = connection.execute(
            "SELECT status FROM external_account_import_batch WHERE batch_id=?",
            (preview.batch_id,),
        ).fetchone()[0]
    assert count == 0
    assert status == "PREVIEWED"


def test_invalid_batch_writes_no_event_or_batch_row(tmp_path: Path) -> None:
    repository, state = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    path = tmp_path / "invalid.csv"
    path.write_text(
        "account_id,event_type,occurred_at,market,symbol,side,quantity,price_cny\n"
        "default,TRADE,2026-09-01T01:00:00+00:00,XSHG,600519,BUY,100,10\n"
        "default,TRADE,2026-09-01T01:00:00+00:00,XSHG,600519,BUY,-1,10\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="row 2 is invalid"):
        repository.preview_import(path, previewed_at=NOW)
    with closing(state.connect()) as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM external_account_event").fetchone()[
            0
        ]
        batch_count = connection.execute(
            "SELECT COUNT(*) FROM external_account_import_batch"
        ).fetchone()[0]
    assert event_count == 0
    assert batch_count == 0


def test_broker_secret_fields_are_rejected(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    path = tmp_path / "secret.json"
    path.write_text(
        json.dumps(
            [
                {
                    "account_id": "default",
                    "event_type": "CASH_DEPOSIT",
                    "occurred_at": "2026-09-01T01:00:00+00:00",
                    "amount_cny": "100",
                    "token": "[REDACTED_SECRET]",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbidden broker secret fields"):
        repository.preview_import(path, previewed_at=NOW)

    with pytest.raises(ValidationError, match="Extra inputs"):
        ExternalAccountEventDraft.model_validate(
            {
                "account_id": "default",
                "event_type": "CASH_DEPOSIT",
                "occurred_at": NOW,
                "available_to_system_at": NOW,
                "amount_cny": "100",
                "idempotency_key": "secret-schema",
                "password": "[REDACTED_SECRET]",
            }
        )


def test_reversal_of_replacement_restores_the_original_economic_event(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    original = bind_external_account_event(_buy("default", "original", quantity=100, price="10"))
    replacement = bind_external_account_event(
        _buy(
            "default",
            "replacement",
            quantity=200,
            price="11",
            occurred_at=NOW + timedelta(minutes=1),
            replaces_event_id=original.event_id,
        )
    )
    reversal = bind_external_account_event(
        _draft(
            "default",
            ExternalAccountEventType.REVERSAL,
            "reverse-replacement",
            occurred_at=NOW + timedelta(minutes=2),
            reverses_event_id=replacement.event_id,
        )
    )
    repository.append_events([original, replacement, reversal])

    replaced = repository.projection("default", as_of=NOW + timedelta(minutes=1, seconds=30))
    restored = repository.projection("default", as_of=NOW + timedelta(minutes=3))
    assert replaced.positions[0].quantity == 200
    assert replaced.positions[0].average_cost_cny == Decimal("11")
    assert restored.positions[0].quantity == 100
    assert restored.positions[0].average_cost_cny == Decimal("10")


def test_external_trade_matching_a_paper_fill_is_rejected_before_insert(tmp_path: Path) -> None:
    repository, state = _repository(tmp_path)
    repository.create_account(account_id="default", display_name="默认账户", created_at=NOW)
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 10_000_000)
    order = ledger.place_order(
        account_id="paper",
        client_request_id="paper-buy",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10_000,
        fee_reserve_fen=0,
    )
    ledger.record_fill(
        fill_id="paper-fill-buy",
        order_id=order.order_id,
        qty=100,
        price_fen=10_000,
        occurred_at=NOW,
    )

    with pytest.raises(ExternalAccountConflictError, match="paper fill"):
        repository.append_drafts([_buy("default", "external-copy", price="100")])
    assert repository.list_events("default") == []


def test_legacy_single_account_imports_migrate_once_to_default_and_markdown_is_projection(
    tmp_path: Path,
) -> None:
    repository, state = _repository(tmp_path)
    local = LocalPortfolioService(tmp_path, state)
    local.record_trade(
        market="XSHG",
        symbol="600519",
        side="BUY",
        quantity=100,
        price_cny="10",
        source="IMPORT",
        occurred_at=NOW - timedelta(days=2),
    )
    local.record_trade(
        market="XSHE",
        symbol="000001",
        side="BUY",
        quantity=100,
        price_cny="20",
        source="PAPER_FILL",
        occurred_at=NOW - timedelta(days=1),
    )

    first = repository.migrate_legacy_default_account(local, migrated_at=NOW)
    second = repository.migrate_legacy_default_account(
        local,
        migrated_at=NOW + timedelta(hours=1),
    )
    assert first["account_id"] == "default"
    assert len(cast(list[str], first["inserted_event_ids"])) == 1
    assert first["skipped_paper_fill_trade_count"] == 1
    assert second["inserted_event_ids"] == []
    assert len(cast(list[str], second["duplicate_event_ids"])) == 1
    projection = repository.projection("default", as_of=NOW + timedelta(hours=2))
    assert [(item.symbol, item.quantity) for item in projection.positions] == [("600519", 100)]

    receipt = repository.write_markdown_projection(tmp_path, as_of=NOW + timedelta(hours=2))
    assert receipt["relative_path"] == "user_state/external_accounts.md"
    projection_path = tmp_path / "user_state" / "external_accounts.md"
    assert projection_path.is_file()
    text = projection_path.read_text(encoding="utf-8")
    assert "只读投影" in text
    assert "default" in text
