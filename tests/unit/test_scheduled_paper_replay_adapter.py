from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.paper_replay import CanonicalConfirmedPaperReplayAdapter
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.etf_policy import load_etf_execution_policy
from astock.paper_trading.ledger import LedgerService
from astock.paper_trading.replay import PaperReplayService, load_fee_schedule
from astock.schemas import Order, OrderSide, ReplayExecutionReport, ReplayQuality
from tests.unit.test_paper_operations import NOW, _confirmation, _request, _service

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _ManifestStore:
    @staticmethod
    def load_manifest(request):
        del request
        return {"content_hash": "a" * 64}


class _ReplayRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.canonical_store = _ManifestStore()

    def replay(self, *, account_id, request, requested_cursor, fee_schedule):
        self.calls.append(
            {
                "account_id": account_id,
                "request": request,
                "requested_cursor": requested_cursor,
                "fee_schedule": fee_schedule,
            }
        )
        return ReplayExecutionReport(
            account_id=account_id,
            market=request.market,
            symbol=request.symbol,
            requested_cursor=requested_cursor,
            processed_bars=0,
            matched_orders=0,
            fill_ids=[],
            replay_quality=ReplayQuality.PROVIDER_1H_APPROX,
            fee_rule_version=fee_schedule.rule_version,
            fee_assumptions_require_broker_confirmation=(
                fee_schedule.requires_broker_confirmation
            ),
            maximum_participation_rate=Decimal("0.10"),
            checkpoint=None,
        )


def _confirmed_order(
    state: StateStore,
    object_store: ObjectStore,
) -> tuple[InvestorOrchestrationStore, LedgerService, Order]:
    paper, ledger = _service(state, object_store)
    request = _request()
    paper.execute(request, _confirmation(request))
    order = ledger.open_orders("paper")[0]
    store = InvestorOrchestrationStore(state.path)
    store.initialize()
    return store, ledger, order


def _adapter(
    state: StateStore,
    object_store: ObjectStore,
    engine: _ReplayRecorder,
) -> CanonicalConfirmedPaperReplayAdapter:
    return CanonicalConfirmedPaperReplayAdapter(
        state=state,
        objects=object_store,
        replay=cast(PaperReplayService, cast(Any, engine)),
        stock_fee_schedule=load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml"),
        etf_policy=load_etf_execution_policy(
            PROJECT_ROOT / "configs" / "etf_paper_trading_rules.yaml"
        ),
        lookback_days=45,
    )


def test_canonical_scheduled_replay_accepts_exact_confirmed_open_group(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    _store, _ledger, order = _confirmed_order(state, object_store)
    engine = _ReplayRecorder()
    adapter = _adapter(state, object_store, engine)
    requested_at = NOW + timedelta(minutes=5)

    artifact_ids = adapter.replay_confirmed_orders(
        run_id="scheduled-confirmed-replay",
        requested_at=requested_at,
        order_ids=(order.order_id,),
    )

    assert len(artifact_ids) == 1
    assert artifact_ids[0].startswith("ReplayExecutionReport:scheduled:")
    assert len(engine.calls) == 1
    assert engine.calls[0]["account_id"] == "paper"
    record = state.artifact_record(artifact_ids[0])
    assert record is not None and record["type"] == "ReplayExecutionReport"


def test_canonical_scheduled_replay_refuses_mixed_confirmed_and_unconfirmed_group(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    _store, ledger, confirmed = _confirmed_order(state, object_store)
    ledger.place_order(
        account_id="paper",
        client_request_id="unconfirmed-same-symbol",
        symbol=confirmed.symbol,
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=1000,
        submitted_at=NOW + timedelta(minutes=3),
    )
    engine = _ReplayRecorder()
    adapter = _adapter(state, object_store, engine)

    with pytest.raises(ValueError, match="partial account/instrument open-order group"):
        adapter.replay_confirmed_orders(
            run_id="scheduled-mixed-replay",
            requested_at=NOW + timedelta(minutes=5),
            order_ids=(confirmed.order_id,),
        )
    assert engine.calls == []


def test_canonical_scheduled_replay_rejects_future_run_time(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    _store, _ledger, confirmed = _confirmed_order(state, object_store)
    engine = _ReplayRecorder()
    adapter = _adapter(state, object_store, engine)

    with pytest.raises(ValueError, match="cannot be in the future"):
        adapter.replay_confirmed_orders(
            run_id="scheduled-future-replay",
            requested_at=datetime.now(UTC) + timedelta(days=1),
            order_ids=(confirmed.order_id,),
        )
    assert engine.calls == []


def test_canonical_scheduled_replay_rejects_order_created_after_run_time(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    _store, _ledger, confirmed = _confirmed_order(state, object_store)
    engine = _ReplayRecorder()
    adapter = _adapter(state, object_store, engine)

    with pytest.raises(ValueError, match="submitted after the run time"):
        adapter.replay_confirmed_orders(
            run_id="scheduled-backdated-replay",
            requested_at=NOW - timedelta(minutes=1),
            order_ids=(confirmed.order_id,),
        )
    assert engine.calls == []
