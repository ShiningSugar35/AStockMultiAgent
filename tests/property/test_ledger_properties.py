from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from astock.core.state import StateStore
from astock.paper_trading.ledger import LedgerLine, LedgerService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@given(amount=st.integers(min_value=1, max_value=10_000_000))
@settings(max_examples=25, deadline=None)
def test_any_balanced_transfer_stays_balanced(amount: int) -> None:
    with TemporaryDirectory() as directory:
        state = StateStore(Path(directory) / "state.sqlite", PROJECT_ROOT / "migrations")
        state.migrate()
        ledger = LedgerService(state)
        ledger.initialize_account("property", amount * 2)
        ledger.post_event(
            account_id="property",
            event_type="PROPERTY_TRANSFER",
            idempotency_key=f"property:{amount}",
            payload={"amount": amount},
            lines=[
                LedgerLine("FROZEN_CASH", debit_fen=amount),
                LedgerLine("CASH", credit_fen=amount),
            ],
        )
        assert ledger.status("property")["imbalanced_events"] == 0


def test_unbalanced_event_is_rejected_without_partial_rows(state: StateStore) -> None:
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 100_000)
    before = ledger.status("paper")["last_event_seq"]
    with pytest.raises(ValueError, match="Unbalanced"):
        ledger.post_event(
            account_id="paper",
            event_type="BROKEN",
            idempotency_key="broken",
            payload={},
            lines=[LedgerLine("CASH", debit_fen=100)],
        )
    assert ledger.status("paper")["last_event_seq"] == before
