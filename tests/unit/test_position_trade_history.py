from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.investor_orchestration.position_trade_history import (
    DocumentTrade,
    position_trade_history,
    trade_episodes,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading import LedgerService
from astock.schemas import OrderSide
from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
from astock.schemas.market import Market

NOW = datetime(2026, 9, 10, 7, tzinfo=UTC)
KEY = "PAPER:account:XSHG:600519"


def _trade(seq: int, action: str, qty: int, price: str, fee: str | None = "1") -> DocumentTrade:
    value = Decimal(price)
    return DocumentTrade(
        f"event-{seq}",
        KEY,
        NOW + timedelta(days=seq),
        seq,
        qty,
        action,
        value,
        value * qty,
        Decimal(fee) if fee is not None else None,
    )


def test_episode_covers_partial_sales_additions_and_exact_net_return() -> None:
    rows = [
        _trade(0, "BUY", 100, "10"),
        _trade(1, "BUY", 100, "12"),
        _trade(2, "SELL", 50, "15"),
        _trade(3, "SELL", 150, "11"),
    ]
    result = trade_episodes(rows)[KEY]
    episode = result["closed"][0]
    assert result["complete"] and result["active"] is None
    assert episode["opened_at"] == NOW.isoformat()
    assert episode["closed_at"] == (NOW + timedelta(days=3)).isoformat()
    assert Decimal(episode["gross_pnl"]) == Decimal("200")
    assert Decimal(episode["net_pnl"]) == Decimal("196")
    assert Decimal(episode["net_return"]) == Decimal("196") / Decimal("2202")
    assert "未含分红" in episode["return_basis"]


def test_liquidation_then_reentry_uses_a_new_stable_episode_even_between_activations() -> None:
    rows = [_trade(0, "BUY", 100, "10"), _trade(1, "SELL", 100, "12"), _trade(2, "BUY", 100, "9")]
    history = trade_episodes(rows)[KEY]
    assert history["closed"][0]["episode_id"] != history["active"]["episode_id"]
    assert history["active"]["entry_price"] == "9"
    assert history["active"]["opened_at"] == rows[-1].occurred_at.isoformat()
    assert trade_episodes(rows) == trade_episodes(list(reversed(rows)))


def test_unknown_actual_fees_do_not_turn_gross_return_into_net_return() -> None:
    result = trade_episodes(
        [_trade(0, "BUY", 100, "10", None), _trade(1, "SELL", 100, "12", None)]
    )[KEY]["closed"][0]
    assert result["gross_pnl"] == "200"
    assert result["net_pnl"] is None and result["net_return"] is None
    assert not result["performance_known"]


def test_transfer_out_is_not_an_observed_sale_or_known_profit() -> None:
    result = trade_episodes(
        [_trade(0, "BUY", 100, "10"), _trade(1, "TRANSFER_OUT", 100, "0", None)]
    )[KEY]["closed"][0]
    assert result["closed_at"] is not None
    assert result["gross_pnl"] is None and result["net_pnl"] is None


def test_incomplete_share_history_cannot_certify_a_closed_cycle() -> None:
    result = trade_episodes([_trade(0, "BUY", 100, "10"), _trade(1, "SELL", 150, "12")])[KEY]
    assert not result["complete"]
    assert result["active"] is None and result["closed"] == []


def test_first_verified_trade_episode_rebuilds_entry_snapshot_at_trade_time() -> None:
    from astock.investor_orchestration.position_trade_history import apply_trade_history

    active = {
        KEY: {
            "quantity": "100",
            "coverage": "CHECKED",
            "instrument_id": "XSHG:600519",
            "average_cost": "10",
            "first_seen_at": (NOW + timedelta(days=2)).isoformat(),
            "entry_snapshot": {"later_observation": True},
        }
    }
    observed: list[tuple[str, str | None, datetime, str]] = []

    def snapshot(instrument: str, price: str | None, at: datetime, kind: str) -> dict[str, object]:
        observed.append((instrument, price, at, kind))
        return {"cutoff": at.isoformat(), "kind": kind}

    updated, _ = apply_trade_history(
        active,
        {},
        trade_episodes([_trade(0, "BUY", 100, "10")]),
        as_of=NOW + timedelta(days=2),
        checked_accounts={("PAPER", "account")},
        entry_snapshot=snapshot,
    )
    assert observed == [("XSHG:600519", "10", NOW, "TRADE")]
    assert updated[KEY]["entry_snapshot"] == {"cutoff": NOW.isoformat(), "kind": "TRADE"}


def test_document_episode_reentry_and_correction_are_idempotent() -> None:
    from astock.investor_orchestration.position_trade_history import apply_trade_history

    prior_history = trade_episodes([_trade(0, "BUY", 100, "10")])
    active = {
        KEY: {
            "quantity": "100",
            "coverage": "CHECKED",
            "instrument_id": "XSHG:600519",
            "average_cost": "10",
            "first_seen_at": NOW.isoformat(),
            "entry_snapshot": {"old": True},
        }
    }
    first, closed = apply_trade_history(
        active,
        {},
        prior_history,
        as_of=NOW,
        checked_accounts={("PAPER", "account")},
        entry_snapshot=lambda *_: {"new": True},
    )
    rows = [_trade(0, "BUY", 100, "10"), _trade(1, "SELL", 100, "12"), _trade(2, "BUY", 100, "9")]
    updated, archived = apply_trade_history(
        active,
        {"active": first, "closed": closed},
        trade_episodes(rows),
        as_of=NOW + timedelta(days=3),
        checked_accounts={("PAPER", "account")},
        entry_snapshot=lambda *_: {"new": True},
    )
    assert updated[KEY]["entry_snapshot"] == {"new": True}
    assert updated[KEY]["entry_trade_price"] == "9"
    assert len(archived) == 1
    repeated, repeated_closed = apply_trade_history(
        updated,
        {"active": updated, "closed": archived},
        trade_episodes(rows),
        as_of=NOW + timedelta(days=4),
        checked_accounts={("PAPER", "account")},
        entry_snapshot=lambda *_: {"unexpected": True},
    )
    assert repeated == updated and repeated_closed == archived
    _, corrected = apply_trade_history(
        active,
        {"active": updated, "closed": archived},
        prior_history,
        as_of=NOW + timedelta(days=4),
        checked_accounts={("PAPER", "account")},
        entry_snapshot=lambda *_: {},
    )
    assert all(item["superseded_by_correction"] for item in corrected.values())


def test_duplicate_trade_is_not_silently_counted_twice() -> None:
    item = _trade(0, "BUY", 100, "10")
    with pytest.raises(ValueError, match="duplicate"):
        trade_episodes([item, item])


def test_read_history_empty_initialized_canonical_store(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    assert position_trade_history(store, as_of=NOW) == {}


def test_real_ledger_fill_history_has_exact_times_and_known_fees(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    ledger = LedgerService(state)
    ledger.initialize_account("account", 10000000)
    order = ledger.place_order(
        account_id="account",
        client_request_id="buy-history",
        symbol="600519",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=10000,
        fee_reserve_fen=500,
    )
    ledger.record_fill(
        fill_id="history-buy",
        order_id=order.order_id,
        qty=100,
        price_fen=10000,
        commission_fen=500,
        occurred_at=NOW,
    )
    ledger.settle_buys("account", as_of=NOW + timedelta(days=1), trading_calendar_confirmed=True)
    sell = ledger.place_order(
        account_id="account",
        client_request_id="sell-history",
        symbol="600519",
        side=OrderSide.SELL,
        qty=100,
        limit_price_fen=11000,
    )
    ledger.record_fill(
        fill_id="history-sell",
        order_id=sell.order_id,
        qty=100,
        price_fen=11000,
        commission_fen=500,
        tax_fen=500,
        occurred_at=NOW + timedelta(days=2),
    )
    # Exact identity is a test source fact; production never guesses an exchange from a code.
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO paper_position_identity(account_id,symbol,market,"
            "instrument_id) VALUES(?,?,?,?)",
            ("account", "600519", "XSHG", "XSHG:600519"),
        )
    result = position_trade_history(store, as_of=NOW + timedelta(days=3))[KEY]["closed"][0]
    assert result["opened_at"] == NOW.isoformat()
    assert result["closed_at"] == (NOW + timedelta(days=2)).isoformat()
    assert result["net_pnl"] == "985"
    assert Decimal(result["net_return"]) == Decimal("985") / Decimal("10005")

    from astock.investor_orchestration.models import LaneSnapshot, PortfolioLane
    from astock.investor_orchestration.position_documents import PositionDocumentProjector
    from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
    from tests.unit.test_wp24_tracking_edges import _receipt

    receipt = _receipt(empty=True)
    context = receipt.context.model_copy(
        update={
            "paper": LaneSnapshot(
                lane=PortfolioLane.PAPER,
                account_ids=("account",),
                source_revision="after-sale",
                audit_status="PASS",
            ),
        }
    )
    receipt = receipt.model_copy(update={"context": context})
    projector = PositionDocumentProjector(tmp_path, ResearchSubjectRegistryService(store))
    projector.update(receipt)
    document = (projector.root / "已结束交易.md").read_text(encoding="utf-8")
    assert "985" in document and "9.85%" in document
    assert (NOW + timedelta(days=2)).isoformat() in document
    assert "600519" not in (projector.root / "当前持仓.md").read_text(encoding="utf-8")
    assert not projector.update(receipt)["changed"]


def test_external_reversal_is_resolved_by_canonical_semantics(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    repo = ExternalAccountRepository(StateStore(store.path), ObjectStore(tmp_path / "objects"))
    repo.create_account(account_id="actual", display_name="External test")
    buy = ExternalAccountEventDraft(
        account_id="actual",
        event_type=ExternalAccountEventType.TRADE,
        occurred_at=NOW,
        sequence_no=0,
        available_to_system_at=NOW,
        market=Market.XSHG,
        symbol="600519",
        side="BUY",
        quantity=100,
        price_cny=Decimal("10"),
        idempotency_key="buy",
    )
    ids, _ = repo.append_drafts([buy])
    sell = ExternalAccountEventDraft(
        account_id="actual",
        event_type=ExternalAccountEventType.TRADE,
        occurred_at=NOW + timedelta(days=1),
        sequence_no=1,
        available_to_system_at=NOW + timedelta(days=1),
        market=Market.XSHG,
        symbol="600519",
        side="SELL",
        quantity=100,
        price_cny=Decimal("12"),
        idempotency_key="sell",
    )
    sell_ids, _ = repo.append_drafts([sell])
    reversal = ExternalAccountEventDraft(
        account_id="actual",
        event_type=ExternalAccountEventType.REVERSAL,
        occurred_at=NOW + timedelta(days=2),
        sequence_no=2,
        available_to_system_at=NOW + timedelta(days=2),
        reverses_event_id=sell_ids[0],
        idempotency_key="reverse-sell",
    )
    repo.append_drafts([reversal])
    result = position_trade_history(store, as_of=NOW + timedelta(days=3))[
        "ACTUAL:actual:XSHG:600519"
    ]
    assert result["complete"] and result["quantity"] == 100
    assert result["closed"] == []
    assert result["active"]["event_ids"] == ids
