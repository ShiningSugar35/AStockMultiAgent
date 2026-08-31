"""Deterministic, resumable 5-minute limit-order matching for the paper account."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import yaml

from astock.core.errors import FailureClass, PolicyError
from astock.core.hashing import content_hash
from astock.market_data.quality import normalize_volume_to_shares
from astock.market_data.storage import CanonicalMarketStore
from astock.paper_trading.ledger import LedgerService, ReplayFillPlan
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    Frequency,
    InstrumentType,
    Market,
    MarketBar,
    Order,
    OrderSide,
    QualityStatus,
    ReplayCheckpoint,
    ReplayExecutionReport,
    ReplayFeeSchedule,
    ReplayQuality,
    TimestampSemantics,
    TradingSession,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class PaperReplayReferenceVerifier(Protocol):
    def calendar(
        self, market: Market, release_id: str, *, visible_at: datetime
    ) -> list[TradingSession]: ...


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
        references: PaperReplayReferenceVerifier | None = None,
    ) -> None:
        self.ledger = ledger
        self.canonical_store = canonical_store
        self.references = references

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
        if request.instrument_type is not InstrumentType.STOCK:
            raise PolicyError(
                "Paper replay currently admits STOCK instruments only",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if request.market not in fee_schedule.applicable_markets:
            raise PolicyError(
                f"Fee rule {fee_schedule.rule_version} does not cover {request.market.value}",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        unbound = self.ledger.unbound_open_order_ids(account_id, request.symbol)
        if unbound:
            raise PolicyError(
                "Formal replay refuses legacy orders without verified operation bindings",
                failure_class=FailureClass.DATA_QUALITY,
                details={"unbound_order_ids": unbound},
            )
        if self.references is None:
            raise PolicyError(
                "Formal replay requires a verified calendar reference boundary",
                failure_class=FailureClass.DATA_QUALITY,
            )
        manifest = self.canonical_store.load_manifest(request)
        if manifest is None:
            raise PolicyError(
                f"Canonical {request.frequency.value} manifest is missing",
                failure_class=FailureClass.DATA_QUALITY,
            )
        replay_quality = ReplayQuality(str(manifest["replay_quality"]))
        supported_quality = (
            replay_quality is ReplayQuality.DUAL_SOURCE_5M_VERIFIED
            if request.frequency is Frequency.M5
            else replay_quality is ReplayQuality.PROVIDER_1H_APPROX
        )
        if request.frequency not in {Frequency.M5, Frequency.H1} or not supported_quality:
            raise PolicyError(
                (
                    "Paper fills require dual-source verified 5m data or "
                    "dual-source checked approximate 60m data"
                ),
                failure_class=FailureClass.DATA_QUALITY,
            )
        manifest_identity = {
            "market": request.market.value,
            "instrument_type": request.instrument_type.value,
            "symbol": request.symbol,
            "frequency": request.frequency.value,
            "adjustment_mode": request.adjustment_mode.value,
        }
        if any(manifest.get(key) != value for key, value in manifest_identity.items()):
            raise PolicyError(
                "Canonical manifest request identity mismatch",
                failure_class=FailureClass.DATA_QUALITY,
            )
        manifest_without_hash = dict(manifest)
        stored_manifest_hash = manifest_without_hash.pop("content_hash", None)
        if stored_manifest_hash != content_hash(manifest_without_hash):
            raise PolicyError(
                "Canonical manifest content hash mismatch",
                failure_class=FailureClass.DATA_QUALITY,
            )
        if QualityStatus(str(manifest["quality_status"])) is not QualityStatus.PASS:
            raise PolicyError(
                f"Canonical {request.frequency.value} quality report is not PASS",
                failure_class=FailureClass.DATA_QUALITY,
            )
        if request.adjustment_mode is not AdjustmentMode.NONE:
            raise PolicyError(
                "Paper fills require unadjusted executable prices",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        previous = self.ledger.replay_checkpoint(account_id, request.symbol)
        if previous is not None and (
            previous.market is not request.market
            or previous.instrument_id != f"{request.market.value}:{request.symbol}"
        ):
            raise PolicyError(
                "Legacy or mismatched replay checkpoint lacks formal instrument identity",
                failure_class=FailureClass.DATA_QUALITY,
            )
        resolution_changed = (
            previous is not None and previous.actual_resolution != request.frequency.value
        )
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
        all_bars = self.canonical_store.read_bars(request)
        expected_provider = f"canonical:{manifest['selected_provider']}"
        if any(
            bar.market is not request.market
            or bar.symbol != request.symbol
            or bar.frequency is not request.frequency
            or bar.adjustment_mode is not request.adjustment_mode
            or bar.provider_id != expected_provider
            for bar in all_bars
        ):
            raise PolicyError(
                "Canonical bar observation identity mismatches the replay request",
                failure_class=FailureClass.DATA_QUALITY,
            )
        actual_start = all_bars[0].timestamp.isoformat() if all_bars else None
        actual_end = all_bars[-1].timestamp.isoformat() if all_bars else None
        source_ids = manifest.get("source_batch_ids")
        if (
            manifest.get("bar_count") != len(all_bars)
            or manifest.get("actual_start") != actual_start
            or manifest.get("actual_end") != actual_end
            or not isinstance(source_ids, list)
            or len(set(str(item) for item in source_ids)) < 2
            or not manifest.get("quality_report_id")
        ):
            raise PolicyError(
                "Canonical manifest coverage or dual-source evidence is inconsistent",
                failure_class=FailureClass.DATA_QUALITY,
            )
        self._validate_continuity(all_bars)
        if previous is not None and previous.coverage_end is not None and not resolution_changed:
            previous_bar = next(
                (bar for bar in all_bars if bar.timestamp == previous.coverage_end), None
            )
            if previous_bar is None:
                raise PolicyError(
                    "Replay checkpoint coverage bar is absent from canonical data",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            if requested_cursor < self._bar_visible_at(previous_bar):
                raise PolicyError(
                    "Replay cursor cannot precede the prior bar availability boundary",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
        bars = [
            bar
            for bar in all_bars
            if self._bar_visible_at(bar) <= requested_cursor
            and (previous_cursor is None or bar.timestamp > previous_cursor)
        ]
        fill_ids: list[str] = []
        matched_order_ids: set[str] = set()
        checkpoint = previous
        processed_bars = 0
        verified_calendars: set[str] = set()
        for bar in bars:
            open_orders = self.ledger.open_orders(account_id, request.symbol)
            if not open_orders:
                break
            if bar.adjustment_mode is not AdjustmentMode.NONE:
                raise PolicyError(
                    "Canonical replay contains an adjusted price",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            bar_start = self._bar_start(bar)
            local_start = bar_start.astimezone(_SHANGHAI)
            local_end = (bar_start + self._bar_duration(bar)).astimezone(_SHANGHAI)
            if not self._inside_session(local_start.time(), local_end.time()):
                raise PolicyError(
                    "Canonical bar is outside the continuous trading sessions",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            if normalize_volume_to_shares(bar) <= 0:
                raise PolicyError(
                    "Zero-volume boundary cannot prove a non-suspended executable interval",
                    failure_class=FailureClass.DATA_QUALITY,
                )
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
            fill_plans: list[ReplayFillPlan] = []
            bar_order_ids: set[str] = set()
            for order in open_orders:
                binding = self.ledger.order_rule_binding(order.order_id)
                if binding is None:
                    raise PolicyError(
                        "Formal replay encountered an unbound order",
                        failure_class=FailureClass.DATA_QUALITY,
                    )
                if (
                    binding["market"] != request.market.value
                    or binding["instrument_id"] != f"{request.market.value}:{request.symbol}"
                ):
                    raise PolicyError(
                        "Open order instrument identity mismatches replay request",
                        failure_class=FailureClass.CONFLICT,
                    )
                expires_at = binding.get("expires_at")
                if (
                    binding.get("validity") == "DAY"
                    and expires_at
                    and datetime.fromisoformat(str(expires_at)) < self._bar_visible_at(bar)
                ):
                    continue
                calendar_release_id = str(binding["calendar_release_id"])
                sessions = self.references.calendar(
                    request.market,
                    calendar_release_id,
                    visible_at=order.submitted_at,
                )
                if any(
                    item.exchange is not request.market
                    or item.available_to_system_at > order.submitted_at
                    for item in sessions
                ):
                    raise PolicyError(
                        "Order calendar release identity or availability is mismatched",
                        failure_class=FailureClass.DATA_QUALITY,
                    )
                if calendar_release_id not in verified_calendars:
                    self._validate_calendar_coverage(all_bars, sessions)
                    verified_calendars.add(calendar_release_id)
                session = next(
                    (item for item in sessions if item.session_date == local_start.date()), None
                )
                if session is None or not session.is_open:
                    raise PolicyError(
                        "Order calendar release does not prove this replay session open",
                        failure_class=FailureClass.DATA_QUALITY,
                    )
                eligible = (
                    order.submitted_at < bar_start
                    if bar.timestamp_semantics is TimestampSemantics.BAR_START
                    else order.submitted_at <= bar_start
                )
                if remaining_capacity < 100 or not eligible:
                    continue
                price_fen = self._match_price_fen(order, bar)
                if price_fen is None:
                    continue
                remaining_order_qty = order.qty - order.filled_qty
                fill_qty = min(remaining_order_qty, remaining_capacity)
                fill_qty -= fill_qty % 100
                if fill_qty <= 0:
                    continue
                fill_id = content_hash(
                    {
                        "rule_version": fee_schedule.rule_version,
                        "order_id": order.order_id,
                        "bar_observation_id": bar.observation_id,
                        "qty": fill_qty,
                        "price_fen": price_fen,
                    }
                )
                fill_plans.append(
                    {
                        "fill_id": fill_id,
                        "order_id": order.order_id,
                        "qty": fill_qty,
                        "price_fen": price_fen,
                    }
                )
                bar_order_ids.add(order.order_id)
                remaining_capacity -= fill_qty
            planned_checkpoint = ReplayCheckpoint(
                account_id=account_id,
                market=request.market,
                instrument_id=f"{request.market.value}:{request.symbol}",
                symbol=request.symbol,
                requested_resolution=request.frequency.value,
                actual_resolution=request.frequency.value,
                replay_quality=replay_quality,
                provider_id=str(manifest["selected_provider"]),
                coverage_start=(
                    checkpoint.coverage_start
                    if checkpoint is not None and checkpoint.coverage_start
                    else bars[0].timestamp
                ),
                coverage_end=bar.timestamp,
                missing_bars=0,
                fallback_reason=(
                    None
                    if replay_quality == ReplayQuality.DUAL_SOURCE_5M_VERIFIED
                    else (
                        "60m OHLC can simulate price-touch fills but cannot prove "
                        "queue priority or intrabar path"
                    )
                ),
                last_event_seq=(checkpoint.last_event_seq if checkpoint else 0),
                market_cursor=bar.timestamp.isoformat(),
            )
            input_hash = content_hash(
                {
                    "bar": bar.model_dump(mode="json", exclude={"created_at"}),
                    "fill_plans": fill_plans,
                    "maximum_participation_rate": str(maximum_participation_rate),
                    "fee_schedule": fee_schedule.model_dump(mode="json", exclude={"created_at"}),
                    "manifest_content_hash": stored_manifest_hash,
                    "request": request.model_dump(mode="json", exclude={"created_at"}),
                    "checkpoint": planned_checkpoint.model_dump(
                        mode="json", exclude={"created_at"}
                    ),
                }
            )
            committed_fills, checkpoint = self.ledger.commit_replay_bar(
                account_id=account_id,
                symbol=request.symbol,
                bar_observation_id=bar.observation_id,
                input_hash=input_hash,
                fill_plans=fill_plans,
                checkpoint=planned_checkpoint,
                fee_schedule=fee_schedule,
            )
            fill_ids.extend(fill.fill_id for fill in committed_fills)
            matched_order_ids.update(bar_order_ids)
            processed_bars += 1
        return ReplayExecutionReport(
            account_id=account_id,
            market=request.market,
            symbol=request.symbol,
            requested_cursor=requested_cursor,
            previous_cursor=previous_cursor,
            processed_bars=processed_bars,
            matched_orders=len(matched_order_ids),
            fill_ids=fill_ids,
            replay_quality=replay_quality,
            fee_rule_version=fee_schedule.rule_version,
            fee_assumptions_require_broker_confirmation=(fee_schedule.requires_broker_confirmation),
            maximum_participation_rate=maximum_participation_rate,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _bar_duration(bar: MarketBar) -> timedelta:
        if bar.frequency is Frequency.M5:
            return timedelta(minutes=5)
        if bar.frequency is Frequency.H1:
            return timedelta(minutes=60)
        raise PolicyError(
            "Paper replay supports only 5m or 60m bars",
            failure_class=FailureClass.DATA_QUALITY,
        )

    @classmethod
    def _bar_start(cls, bar: MarketBar) -> datetime:
        if bar.timestamp_semantics is TimestampSemantics.BAR_START:
            return bar.timestamp
        if bar.timestamp_semantics is TimestampSemantics.BAR_END:
            return bar.timestamp - cls._bar_duration(bar)
        raise PolicyError(
            "Paper replay requires BAR_START or BAR_END timestamp semantics",
            failure_class=FailureClass.DATA_QUALITY,
        )

    @classmethod
    def _bar_visible_at(cls, bar: MarketBar) -> datetime:
        if bar.timestamp_semantics is TimestampSemantics.BAR_START:
            return bar.timestamp + cls._bar_duration(bar)
        if bar.timestamp_semantics is TimestampSemantics.BAR_END:
            return bar.timestamp
        return cls._bar_start(bar)

    @staticmethod
    def _inside_session(start: time, end: time) -> bool:
        return (time(9, 30) <= start and end <= time(11, 30)) or (
            time(13, 0) <= start and end <= time(15, 0)
        )

    @classmethod
    def _validate_continuity(cls, bars: list[MarketBar]) -> None:
        for previous, current in zip(bars, bars[1:], strict=False):
            previous_start = cls._bar_start(previous).astimezone(_SHANGHAI)
            current_start = cls._bar_start(current).astimezone(_SHANGHAI)
            duration = cls._bar_duration(previous)
            expected = previous_start + duration
            if previous.frequency is Frequency.H1:
                lunch_resume = previous_start.time() == time(
                    10, 30
                ) and current_start.time() == time(13, 0)
                last_start = time(14, 0)
            else:
                lunch_resume = previous_start.time() == time(
                    11, 25
                ) and current_start.time() == time(13, 0)
                last_start = time(14, 55)
            next_session = (
                previous_start.date() < current_start.date()
                and previous_start.time() == last_start
                and current_start.time() == time(9, 30)
            )
            if current_start != expected and not lunch_resume and not next_session:
                raise PolicyError(
                    f"Canonical {previous.frequency.value} series has a non-session continuity gap",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={
                        "previous": previous.timestamp.isoformat(),
                        "current": current.timestamp.isoformat(),
                    },
                )

    @classmethod
    def _validate_calendar_coverage(
        cls, bars: list[MarketBar], sessions: list[TradingSession]
    ) -> None:
        if not bars:
            return
        bar_dates = {cls._bar_start(bar).astimezone(_SHANGHAI).date() for bar in bars}
        first_date = min(bar_dates)
        last_date = max(bar_dates)
        open_dates = {
            item.session_date
            for item in sessions
            if item.is_open and first_date <= item.session_date <= last_date
        }
        if bar_dates != open_dates:
            raise PolicyError(
                "Canonical replay session coverage disagrees with the frozen calendar",
                failure_class=FailureClass.DATA_QUALITY,
                details={
                    "missing_open_dates": sorted(
                        item.isoformat() for item in open_dates - bar_dates
                    ),
                    "unexpected_bar_dates": sorted(
                        item.isoformat() for item in bar_dates - open_dates
                    ),
                },
            )

    @staticmethod
    def _bar_capacity(bar: MarketBar, maximum_participation_rate: Decimal) -> int:
        raw_capacity = int(normalize_volume_to_shares(bar) * maximum_participation_rate)
        return raw_capacity - raw_capacity % 100

    @staticmethod
    def _match_price_fen(order: Order, bar: MarketBar) -> int | None:
        if order.limit_price_fen is None:
            raise PolicyError(
                "Paper replay only supports explicit limit orders",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        limit_yuan = Decimal(order.limit_price_fen) / 100
        if order.side == OrderSide.BUY:
            if bar.low > limit_yuan:
                return None
            matched_yuan = (
                limit_yuan if bar.frequency is Frequency.H1 else min(bar.open, limit_yuan)
            )
        else:
            if bar.high < limit_yuan:
                return None
            matched_yuan = (
                limit_yuan if bar.frequency is Frequency.H1 else max(bar.open, limit_yuan)
            )
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
