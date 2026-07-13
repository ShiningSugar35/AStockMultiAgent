"""Deterministic, resumable 5-minute limit-order matching for the paper account."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from astock.core.errors import FailureClass, PolicyError
from astock.core.hashing import content_hash
from astock.market_data.quality import normalize_volume_to_shares
from astock.market_data.storage import CanonicalMarketStore
from astock.paper_trading.ledger import LedgerService
from astock.schemas import (
    BarRequest,
    MarketBar,
    Order,
    OrderSide,
    ReplayCheckpoint,
    ReplayExecutionReport,
    ReplayFeeSchedule,
    ReplayQuality,
)


def load_fee_schedule(path: Path) -> ReplayFeeSchedule:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Fee schedule must be a YAML object: {path}")
    return ReplayFeeSchedule.model_validate(raw)


class PaperReplayService:
    def __init__(
        self,
        ledger: LedgerService,
        canonical_store: CanonicalMarketStore,
    ) -> None:
        self.ledger = ledger
        self.canonical_store = canonical_store

    def replay(
        self,
        *,
        account_id: str,
        request: BarRequest,
        requested_cursor: datetime,
        fee_schedule: ReplayFeeSchedule,
        maximum_participation_rate: Decimal = Decimal("0.10"),
    ) -> ReplayExecutionReport:
        if not (Decimal("0") < maximum_participation_rate <= Decimal("1")):
            raise ValueError("maximum_participation_rate must be in (0, 1]")
        if request.market not in fee_schedule.applicable_markets:
            raise PolicyError(
                f"Fee rule {fee_schedule.rule_version} does not cover {request.market.value}",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        manifest = self.canonical_store.load_manifest(request)
        if manifest is None:
            raise PolicyError(
                "Canonical 5m manifest is missing",
                failure_class=FailureClass.DATA_QUALITY,
            )
        replay_quality = ReplayQuality(str(manifest["replay_quality"]))
        if replay_quality == ReplayQuality.UNREPLAYABLE:
            raise PolicyError(
                "Canonical data is marked unreplayable",
                failure_class=FailureClass.DATA_QUALITY,
            )
        previous = self.ledger.replay_checkpoint(account_id, request.symbol)
        previous_cursor = (
            datetime.fromisoformat(previous.market_cursor)
            if previous is not None and previous.market_cursor
            else None
        )
        if previous_cursor is not None and requested_cursor < previous_cursor:
            raise PolicyError(
                "Replay cursor cannot move backwards",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        bars = [
            bar
            for bar in self.canonical_store.read_bars(request)
            if bar.timestamp <= requested_cursor
            and (previous_cursor is None or bar.timestamp > previous_cursor)
        ]
        fill_ids: list[str] = []
        matched_order_ids: set[str] = set()
        checkpoint = previous
        for bar in bars:
            if bar.timestamp.date() < fee_schedule.effective_from:
                raise PolicyError(
                    "Replay data predates the selected effective-dated fee rule",
                    failure_class=FailureClass.POLICY_REJECTED,
                    details={
                        "bar_date": bar.timestamp.date().isoformat(),
                        "fee_rule_effective_from": fee_schedule.effective_from.isoformat(),
                    },
                )
            remaining_capacity = self._bar_capacity(bar, maximum_participation_rate)
            for order in self.ledger.open_orders(account_id, request.symbol):
                if remaining_capacity < 100 or order.submitted_at > bar.timestamp:
                    continue
                price_fen = self._match_price_fen(order, bar)
                if price_fen is None:
                    continue
                remaining_order_qty = order.qty - order.filled_qty
                fill_qty = min(remaining_order_qty, remaining_capacity)
                fill_qty -= fill_qty % 100
                if fill_qty <= 0:
                    continue
                commission, tax, transfer = self._fees(
                    order.side, fill_qty * price_fen, fee_schedule
                )
                fill_id = content_hash(
                    {
                        "rule_version": fee_schedule.rule_version,
                        "order_id": order.order_id,
                        "bar_observation_id": bar.observation_id,
                        "qty": fill_qty,
                        "price_fen": price_fen,
                    }
                )
                fill = self.ledger.record_fill(
                    fill_id=fill_id,
                    order_id=order.order_id,
                    qty=fill_qty,
                    price_fen=price_fen,
                    commission_fen=commission,
                    tax_fen=tax,
                    transfer_fee_fen=transfer,
                    replay_quality=replay_quality,
                    occurred_at=bar.timestamp,
                )
                fill_ids.append(fill.fill_id)
                matched_order_ids.add(order.order_id)
                remaining_capacity -= fill_qty
            checkpoint = ReplayCheckpoint(
                account_id=account_id,
                symbol=request.symbol,
                actual_resolution="5m",
                replay_quality=replay_quality,
                provider_id=str(manifest["selected_provider"]),
                coverage_start=(
                    previous.coverage_start
                    if previous is not None and previous.coverage_start
                    else bars[0].timestamp
                ),
                coverage_end=bar.timestamp,
                missing_bars=0,
                fallback_reason=(
                    None
                    if replay_quality == ReplayQuality.DUAL_SOURCE_5M_VERIFIED
                    else "dual-source verification threshold not met"
                ),
                last_event_seq=self.ledger.status(account_id)["last_event_seq"],
                market_cursor=bar.timestamp.isoformat(),
            )
            self.ledger.save_replay_checkpoint(checkpoint)
        return ReplayExecutionReport(
            account_id=account_id,
            market=request.market,
            symbol=request.symbol,
            requested_cursor=requested_cursor,
            previous_cursor=previous_cursor,
            processed_bars=len(bars),
            matched_orders=len(matched_order_ids),
            fill_ids=fill_ids,
            replay_quality=replay_quality,
            fee_rule_version=fee_schedule.rule_version,
            fee_assumptions_require_broker_confirmation=(fee_schedule.requires_broker_confirmation),
            maximum_participation_rate=maximum_participation_rate,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _bar_capacity(bar: MarketBar, maximum_participation_rate: Decimal) -> int:
        raw_capacity = int(normalize_volume_to_shares(bar) * maximum_participation_rate)
        return raw_capacity - raw_capacity % 100

    @staticmethod
    def _match_price_fen(order: Order, bar: MarketBar) -> int | None:
        if order.limit_price_fen is None:
            raise PolicyError(
                "M1 replay only supports explicit limit orders",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        limit_yuan = Decimal(order.limit_price_fen) / 100
        if order.side == OrderSide.BUY:
            if bar.low > limit_yuan:
                return None
            matched_yuan = min(bar.open, limit_yuan)
        else:
            if bar.high < limit_yuan:
                return None
            matched_yuan = max(bar.open, limit_yuan)
        return int((matched_yuan * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _fees(
        side: OrderSide,
        gross_fen: int,
        schedule: ReplayFeeSchedule,
    ) -> tuple[int, int, int]:
        commission = _round_fen(Decimal(gross_fen) * schedule.commission_rate)
        if schedule.commission_rate > 0:
            commission = max(commission, schedule.minimum_commission_fen)
        tax = (
            _round_fen(Decimal(gross_fen) * schedule.stamp_tax_sell_rate)
            if side == OrderSide.SELL
            else 0
        )
        transfer = _round_fen(Decimal(gross_fen) * schedule.transfer_fee_rate)
        return commission, tax, transfer


def _round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
