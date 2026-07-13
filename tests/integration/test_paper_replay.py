from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from astock.market_data.quality import cross_validate_batches
from astock.market_data.storage import CanonicalMarketStore
from astock.paper_trading import LedgerService, PaperReplayService, load_fee_schedule
from astock.schemas import OrderSide, OrderStatus, VolumeUnit
from tests.helpers import make_batch

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_replay_matches_partial_limit_fills_and_resumes(tmp_path: Path, state) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    store = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    store.publish(
        east,
        cross_validate_batches(east, sina),
        source_batch_ids=[east.batch_id, sina.batch_id],
    )
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 5_000_000)
    ledger.place_order(
        account_id="paper",
        client_request_id="replay-buy",
        symbol=east.request.symbol,
        side=OrderSide.BUY,
        qty=200,
        limit_price_fen=10_020,
        fee_reserve_fen=2_000,
        submitted_at=east.bars[0].timestamp - timedelta(minutes=1),
    )
    service = PaperReplayService(ledger, store)
    fee_schedule = load_fee_schedule(PROJECT_ROOT / "configs" / "fee_rules.yaml")

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
