"""Canonical adapter for scheduled replay of already-confirmed paper orders.

The scheduled layer may select a bounded set of confirmed open order IDs, but it
must never implement its own matching engine. This adapter revalidates the
persisted confirmation/rule bindings and delegates the actual fill simulation to
PaperReplayService over the canonical market store.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import utc_now
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.market_data.storage import CanonicalMarketStore
from astock.monitoring.config import load_continuous_monitor_config
from astock.paper_trading.etf_policy import ETFExecutionPolicy, load_etf_execution_policy
from astock.paper_trading.ledger import LedgerService
from astock.paper_trading.operation import MarketReferencePaperVerifier
from astock.paper_trading.replay import PaperReplayService, load_fee_schedule
from astock.schemas import AdjustmentMode, BarRequest, Frequency, InstrumentType, Market
from astock.schemas.paper import ReplayFeeSchedule
from astock.settings import ProjectPaths

_OPEN_STATUSES = ("ACCEPTED", "PARTIALLY_FILLED")


class CanonicalConfirmedPaperReplayAdapter:
    """Replay exactly the scheduled order groups through the canonical engine."""

    def __init__(
        self,
        *,
        state: StateStore,
        objects: ObjectStore,
        replay: PaperReplayService,
        stock_fee_schedule: ReplayFeeSchedule,
        etf_policy: ETFExecutionPolicy,
        lookback_days: int,
    ) -> None:
        if lookback_days <= 0:
            raise ValueError("scheduled paper replay lookback must be positive")
        self.state = state
        self.objects = objects
        self.replay = replay
        self.stock_fee_schedule = stock_fee_schedule
        self.etf_policy = etf_policy
        self.lookback_days = lookback_days

    @classmethod
    def from_store(
        cls,
        store: InvestorOrchestrationStore,
        *,
        paths: ProjectPaths | None = None,
    ) -> CanonicalConfirmedPaperReplayAdapter:
        paths = paths or ProjectPaths.discover()
        state = StateStore(store.path)
        objects_root = (
            paths.objects if store.path.resolve() == paths.state_db.resolve()
            else store.path.parent / "objects" / "sha256"
        )
        objects = ObjectStore(objects_root)
        references = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(paths.parquet),
            paths.root / "tests" / "fixtures" / "reference",
        )
        etf_policy = load_etf_execution_policy(
            paths.root / "configs" / "etf_paper_trading_rules.yaml"
        )
        replay = PaperReplayService(
            LedgerService(state, objects),
            CanonicalMarketStore(paths.parquet, paths.manifests),
            MarketReferencePaperVerifier(references),
            etf_execution_policy=etf_policy,
        )
        monitor = load_continuous_monitor_config(paths.root / "configs" / "continuous_monitor.yaml")
        return cls(
            state=state,
            objects=objects,
            replay=replay,
            stock_fee_schedule=load_fee_schedule(paths.root / "configs" / "fee_rules.yaml"),
            etf_policy=etf_policy,
            lookback_days=monitor.market.lookback_days,
        )

    def replay_confirmed_orders(
        self,
        *,
        run_id: str,
        requested_at: datetime,
        order_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not run_id.strip():
            raise ValueError("scheduled paper replay run id is required")
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("scheduled paper replay timestamp must be timezone-aware")
        requested_at = requested_at.astimezone(UTC)
        if requested_at > utc_now():
            raise ValueError("scheduled paper replay timestamp cannot be in the future")
        if not order_ids or any(not order_id.strip() for order_id in order_ids):
            raise ValueError("scheduled paper replay requires explicit order ids")
        if tuple(sorted(set(order_ids))) != order_ids:
            raise ValueError("scheduled paper replay order ids must be sorted and unique")

        rows = self._requested_rows(order_ids)
        if len(rows) != len(order_ids):
            raise ValueError("scheduled paper replay contains an unavailable order")
        groups: dict[
            tuple[str, Market, str, InstrumentType], list[dict[str, Any]]
        ] = defaultdict(list)
        for row in rows:
            self._validate_confirmed_row(row, requested_at=requested_at)
            key = (
                str(row["account_id"]),
                Market(str(row["market"])),
                str(row["symbol"]),
                InstrumentType(str(row["instrument_type"])),
            )
            groups[key].append(row)

        artifact_ids: list[str] = []
        for (account_id, market, symbol, instrument_type), group in sorted(
            groups.items(), key=lambda item: tuple(str(value) for value in item[0])
        ):
            requested_group = tuple(sorted(str(row["order_id"]) for row in group))
            open_group = self._all_open_order_ids(account_id, symbol)
            if requested_group != open_group:
                raise ValueError(
                    "scheduled replay refuses a partial account/instrument open-order group"
                )
            fee_schedule = self._fee_schedule(group, instrument_type)
            request = BarRequest(
                symbol=symbol,
                market=market,
                instrument_type=instrument_type,
                frequency=Frequency.H1,
                requested_start=requested_at - timedelta(days=self.lookback_days),
                requested_end=requested_at,
                adjustment_mode=AdjustmentMode.NONE,
            )
            manifest = self.replay.canonical_store.load_manifest(request)
            if manifest is None or not isinstance(manifest.get("content_hash"), str):
                raise ValueError("scheduled replay requires a canonical 60m market manifest")
            report = self.replay.replay(
                account_id=account_id,
                request=request,
                requested_cursor=requested_at,
                fee_schedule=fee_schedule,
            )
            reference = self.objects.put_json(report.model_dump(mode="json"))
            identity = content_hash(
                {
                    "schema_version": "scheduled-confirmed-paper-replay-v1",
                    "run_id": run_id,
                    "requested_at": requested_at,
                    "order_ids": requested_group,
                    "report_object_hash": reference.sha256,
                    "canonical_manifest_hash": manifest["content_hash"],
                }
            )
            artifact_id = f"ReplayExecutionReport:scheduled:{identity}"
            input_hashes = {
                str(manifest["content_hash"]),
                *(str(row["confirmation_hash"]) for row in group),
                content_hash(
                    {
                        "run_id": run_id,
                        "requested_at": requested_at,
                        "order_ids": requested_group,
                    }
                ),
            }
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="ReplayExecutionReport",
                schema_version=str(report.schema_version),
                object_hash=reference.sha256,
                input_hashes=sorted(input_hashes),
            )
            artifact_ids.append(artifact_id)
        return tuple(artifact_ids)

    def _requested_rows(self, order_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in order_ids)
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT o.order_id,o.account_id,o.symbol,o.status,o.submitted_at,"
                "b.market,b.instrument_id,"
                "b.instrument_type,b.fee_rule_version,b.confirmation_id,b.confirmation_hash,"
                "c.confirmation_hash AS stored_confirmation_hash "
                "FROM order_record o "
                "LEFT JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
                "LEFT JOIN paper_operation_confirmation c ON c.confirmation_id=b.confirmation_id "
                f"WHERE o.order_id IN ({placeholders}) ORDER BY o.order_id",
                order_ids,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_confirmed_row(
        row: dict[str, Any], *, requested_at: datetime
    ) -> None:
        if row.get("status") not in _OPEN_STATUSES:
            raise ValueError("scheduled paper replay only admits open orders")
        submitted_at = datetime.fromisoformat(str(row.get("submitted_at", "")))
        if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
            raise ValueError("scheduled paper replay order timestamp must be timezone-aware")
        if submitted_at.astimezone(UTC) > requested_at:
            raise ValueError("scheduled paper replay refuses an order submitted after the run time")
        required = (
            "market",
            "instrument_id",
            "instrument_type",
            "fee_rule_version",
            "confirmation_id",
            "confirmation_hash",
            "stored_confirmation_hash",
        )
        if any(row.get(key) in (None, "") for key in required):
            raise ValueError("scheduled paper replay requires a complete formal order binding")
        if row["confirmation_hash"] != row["stored_confirmation_hash"]:
            raise ValueError("scheduled paper replay confirmation binding is inconsistent")
        if row["instrument_id"] != f"{row['market']}:{row['symbol']}":
            raise ValueError("scheduled paper replay instrument identity is inconsistent")

    def _all_open_order_ids(self, account_id: str, symbol: str) -> tuple[str, ...]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT order_id FROM order_record WHERE account_id=? AND symbol=? "
                "AND status IN ('ACCEPTED','PARTIALLY_FILLED') ORDER BY order_id",
                (account_id, symbol),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _fee_schedule(
        self,
        group: list[dict[str, Any]],
        instrument_type: InstrumentType,
    ) -> ReplayFeeSchedule:
        versions = {str(row["fee_rule_version"]) for row in group}
        if len(versions) != 1:
            raise ValueError("scheduled paper replay group has mixed fee rules")
        if instrument_type is InstrumentType.STOCK:
            schedule = self.stock_fee_schedule
        elif instrument_type is InstrumentType.ETF:
            if not self.etf_policy.execution_enabled:
                raise ValueError("ETF paper replay is independently disabled")
            schedule = self.etf_policy.fee_schedule
        else:
            raise ValueError("scheduled paper replay admits only STOCK or governed ETF orders")
        if versions != {schedule.rule_version}:
            raise ValueError(
                "scheduled paper replay fee rule differs from the frozen order binding"
            )
        return schedule


__all__ = ["CanonicalConfirmedPaperReplayAdapter"]
