"""Read-only adapters over the canonical account, ledger and monitor contracts.

No table-name inference, parallel holdings table, migration, network call or
account mutation is permitted on the preflight read path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from astock.core.hashing import content_hash as canonical_content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import replay_external_account_events
from astock.investor_orchestration.models import (
    LaneSnapshot,
    MaterialEventView,
    OrderView,
    PendingSettlementView,
    PortfolioLane,
    PositionView,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash
from astock.paper_trading.ledger import LedgerService
from astock.schemas.continuous_monitoring import MonitorEvent
from astock.schemas.external_accounts import ExternalAccountEvent


def aware_time(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("canonical state timestamp must be timezone-aware")
    return result.astimezone(UTC)


def normalized_instrument(value: str) -> str:
    """Normalize an explicit market identity, never infer a market from a code."""
    if ":" in value:
        market, symbol = value.split(":", 1)
    elif "." in value:
        symbol, market = value.rsplit(".", 1)
    else:
        return value
    if market not in {"XSHG", "XSHE", "BJSE", "INDEX"} or not symbol:
        raise ValueError("unsupported explicit instrument identity")
    return f"{market}:{symbol}"


class CanonicalStateProjectionReader:
    policy_version = "canonical-preflight-reader-v2"

    def __init__(
        self, store: InvestorOrchestrationStore, objects: ObjectStore | None = None
    ) -> None:
        self.store = store
        self.objects = objects or ObjectStore(store.path.parent / "objects" / "sha256")
        self.ledger = LedgerService(StateStore(store.path))

    def revision_vector(
        self,
        extra_groups: Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, str]:
        scopes = {"actual", "paper", "monitor", *(extra_groups or {})}
        if not scopes <= {"actual", "paper", "monitor", "subjects", "regime"}:
            raise ValueError("unregistered preflight revision scope")
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT scope,generation,revision FROM orchestration_state_revision ORDER BY scope"
            ).fetchall()
        result = {
            str(row["scope"]): content_hash(
                {
                    "generation": row["generation"],
                    "revision": row["revision"],
                    "reader_policy": self.policy_version,
                }
            )
            for row in rows
            if row["scope"] in scopes
        }
        if set(result) != scopes:
            raise ValueError("canonical preflight revision migration is incomplete")
        return result

    def bounded_health_check(self) -> tuple[bool, str | None]:
        # quick_check(1) limits reported errors, not the database pages scanned.
        try:
            with self.store.connect() as connection:
                connection.execute("SELECT 1").fetchone()
                version = connection.execute(
                    "SELECT version FROM schema_migration WHERE version='0068'"
                ).fetchone()
            return (True, None) if version else (False, "CANONICAL_MIGRATION_REQUIRED")
        except sqlite3.DatabaseError as exc:
            return False, type(exc).__name__

    def lane_snapshot(
        self,
        lane: PortfolioLane,
        revision: str,
        *,
        health_ok: bool,
        health_reason: str | None,
        as_of: datetime,
    ) -> LaneSnapshot:
        if not health_ok:
            raise ValueError(f"canonical account state is unavailable: {health_reason}")
        with self.store.connect() as connection:
            if lane is PortfolioLane.ACTUAL:
                return self._actual(connection, revision, as_of)
            if lane is PortfolioLane.PAPER:
                return self._paper(connection, revision, as_of)
        raise ValueError("only actual and paper are economic account lanes")

    def _actual(
        self, connection: sqlite3.Connection, revision: str, as_of: datetime
    ) -> LaneSnapshot:
        accounts = connection.execute(
            "SELECT account_id,created_at FROM external_account ORDER BY account_id"
        ).fetchall()
        positions: list[PositionView] = []
        cash: dict[str, Decimal | None] = {}
        for account in accounts:
            if aware_time(str(account["created_at"])) > as_of:
                continue
            account_id = str(account["account_id"])
            rows = connection.execute(
                "SELECT payload_json FROM external_account_event WHERE account_id=? "
                "ORDER BY occurred_at,sequence_no,available_to_system_at,event_id",
                (account_id,),
            ).fetchall()
            events = [ExternalAccountEvent.model_validate_json(row[0]) for row in rows]
            projection = replay_external_account_events(account_id, events, as_of=as_of)
            cash[account_id] = projection.cash_cny
            positions.extend(
                PositionView(
                    account_id=account_id,
                    lane=PortfolioLane.ACTUAL,
                    instrument_id=f"{item.market.value}:{item.symbol}",
                    quantity=Decimal(item.quantity),
                    average_cost=item.average_cost_cny,
                    cost_status="EXACT",
                    source_revision=revision,
                )
                for item in projection.positions
            )
        known = bool(cash) and all(value is not None for value in cash.values())
        return LaneSnapshot(
            lane=PortfolioLane.ACTUAL,
            account_ids=tuple(cash),
            positions=tuple(positions),
            known_cash=sum((value for value in cash.values() if value is not None), Decimal(0))
            if known
            else None,
            unknown_cash=not known,
            cash_by_account=cash,
            source_revision=revision,
            audit_status="PASS",
        )

    def _paper(
        self, connection: sqlite3.Connection, revision: str, as_of: datetime
    ) -> LaneSnapshot:
        accounts = connection.execute(
            "SELECT account_id FROM paper_account ORDER BY account_id"
        ).fetchall()
        positions: list[PositionView] = []
        orders: list[OrderView] = []
        settlements: list[PendingSettlementView] = []
        cash: dict[str, Decimal | None] = {}
        frozen_cash = Decimal(0)
        warnings: set[str] = set()
        for account in accounts:
            account_id = str(account["account_id"])
            imbalanced = connection.execute(
                "SELECT j.event_id FROM journal j "
                "LEFT JOIN ledger_entry e ON e.event_id=j.event_id "
                "WHERE j.paper_account_id=? GROUP BY j.event_id "
                "HAVING COALESCE(SUM(e.debit_fen),0)<>COALESCE(SUM(e.credit_fen),0) LIMIT 1",
                (account_id,),
            ).fetchone()
            if imbalanced is not None:
                raise ValueError(
                    "paper journal is unbalanced; no empty-account fallback is allowed"
                )
            latest = connection.execute(
                "SELECT occurred_at FROM journal WHERE paper_account_id=? "
                "ORDER BY julianday(occurred_at) DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            if latest and aware_time(str(latest[0])) > as_of:
                raise ValueError("current paper projection cannot be used before its ledger events")
            cash[account_id] = Decimal(self.ledger._balance(connection, account_id, "CASH")) / 100
            frozen = self.ledger._balance(connection, account_id, "FROZEN_CASH")
            frozen_cash += Decimal(frozen) / 100
            rows = connection.execute(
                "SELECT p.*,i.instrument_id,c.total_cost_fen FROM position p "
                "LEFT JOIN paper_position_identity i "
                "ON i.account_id=p.account_id AND i.symbol=p.symbol "
                "LEFT JOIN paper_position_cost c "
                "ON c.account_id=p.account_id AND c.symbol=p.symbol "
                "WHERE p.account_id=? AND p.qty_total>0 ORDER BY p.symbol",
                (account_id,),
            ).fetchall()
            for row in rows:
                quantity = Decimal(row["qty_total"])
                available = Decimal(row["qty_available"])
                if not 0 <= available <= quantity:
                    raise ValueError("paper available quantity is inconsistent")
                if row["instrument_id"] is None:
                    warnings.add("PAPER_POSITION_IDENTITY_UNBOUND")
                cost = Decimal(row["avg_cost_fen"]) / 100
                if row["total_cost_fen"] is not None:
                    cost = Decimal(row["total_cost_fen"]) / (quantity * 100)
                positions.append(
                    PositionView(
                        account_id=account_id,
                        lane=PortfolioLane.PAPER,
                        instrument_id=str(row["instrument_id"] or row["symbol"]),
                        quantity=quantity,
                        available_quantity=available,
                        average_cost=cost,
                        cost_status="EXACT",
                        source_revision=revision,
                    )
                )
            rows = connection.execute(
                "SELECT o.*,b.instrument_id,b.confirmation_id,b.confirmation_hash,"
                "c.confirmation_hash AS actual_confirmation_hash FROM order_record o "
                "LEFT JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
                "LEFT JOIN paper_operation_confirmation c ON c.confirmation_id=b.confirmation_id "
                "WHERE o.account_id=? AND o.status IN ('ACCEPTED','PARTIALLY_FILLED') "
                "ORDER BY o.submitted_at,o.order_id",
                (account_id,),
            ).fetchall()
            reserved = sum(int(row["reserved_fen"]) for row in rows if row["side"] == "BUY")
            if frozen != reserved:
                raise ValueError("paper frozen cash does not reconcile to open orders")
            for row in rows:
                if row["side"] not in {"BUY", "SELL"} or not 0 <= row["filled_qty"] <= row["qty"]:
                    raise ValueError("paper order has invalid side or quantity")
                fill_qty = connection.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM fill WHERE order_id=?",
                    (row["order_id"],),
                ).fetchone()[0]
                if fill_qty != row["filled_qty"]:
                    raise ValueError("paper fills do not reconcile to order quantity")
                confirmed = bool(
                    row["confirmation_id"]
                    and row["confirmation_hash"] == row["actual_confirmation_hash"]
                )
                raw_price = row["limit_price_milli_yuan"]
                price = (
                    Decimal(raw_price) / 1000
                    if raw_price is not None
                    else (
                        Decimal(row["limit_price_fen"]) / 100
                        if row["limit_price_fen"] is not None
                        else None
                    )
                )
                orders.append(
                    OrderView(
                        account_id=account_id,
                        order_id=str(row["order_id"]),
                        instrument_id=str(row["instrument_id"] or row["symbol"]),
                        side=row["side"],
                        quantity=Decimal(row["qty"]),
                        filled_quantity=Decimal(row["filled_qty"]),
                        limit_price=price,
                        status=str(row["status"]),
                        confirmed=confirmed,
                        source_revision=revision,
                    )
                )
            for row in connection.execute(
                "SELECT s.*,i.instrument_id FROM position_settlement s "
                "LEFT JOIN paper_settlement_identity i ON i.settlement_id=s.settlement_id "
                "WHERE s.account_id=? AND s.status<>'SETTLED' "
                "ORDER BY s.eligible_on,s.settlement_id",
                (account_id,),
            ).fetchall():
                settlements.append(
                    PendingSettlementView(
                        settlement_id=str(row["settlement_id"]),
                        account_id=account_id,
                        instrument_id=str(row["instrument_id"] or row["symbol"]),
                        quantity=Decimal(row["qty"]),
                        eligible_on=date.fromisoformat(str(row["eligible_on"])),
                        source_event_id=str(row["source_event_id"]),
                    )
                )
        return LaneSnapshot(
            lane=PortfolioLane.PAPER,
            account_ids=tuple(cash),
            positions=tuple(positions),
            open_orders=tuple(orders),
            pending_settlements=tuple(settlements),
            cash_by_account=cash,
            known_cash=sum((value for value in cash.values() if value is not None), Decimal(0)),
            frozen_cash=frozen_cash,
            unknown_cash=False,
            source_revision=revision,
            audit_status="DEGRADED" if warnings else "PASS",
            warnings=tuple(sorted(warnings)),
        )

    def material_events(
        self, revision: str, *, as_of: datetime, limit: int = 1000
    ) -> tuple[MaterialEventView, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT e.event_id,e.object_hash,t.market,t.symbol FROM continuous_monitor_event e "
                "JOIN continuous_monitor_target t ON t.target_id=e.target_id "
                "WHERE e.acknowledged_at IS NULL AND julianday(e.available_at)<=julianday(?) "
                "ORDER BY e.available_at DESC,e.event_id LIMIT ?",
                (as_of.isoformat(), limit + 1),
            ).fetchall()
        if len(rows) > limit:
            raise ValueError("unresolved monitor events exceed bounded preflight coverage")
        severity: dict[str, Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]] = {
            "INFO": "LOW",
            "WATCH": "MEDIUM",
            "MATERIAL": "HIGH",
            "CRITICAL": "CRITICAL",
        }
        events = []
        for row in rows:
            event = MonitorEvent.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            if event.event_id != row["event_id"]:
                raise ValueError("monitor event index does not match the immutable object")
            if event.available_at > as_of:
                continue
            if canonical_content_hash(event.payload) != event.payload_hash:
                raise ValueError("monitor event payload integrity failed")
            events.append(
                MaterialEventView(
                    event_id=event.event_id,
                    instrument_id=f"{row['market']}:{row['symbol']}",
                    severity=severity[event.severity.value],
                    available_at=event.available_at,
                    event_type=event.event_type.value,
                    summary=str(
                        event.payload.get("summary")
                        or event.payload.get("title")
                        or event.event_type.value
                    ),
                    source_revision=revision,
                )
            )
        return tuple(events)

    def next_visibility_change(self, as_of: datetime) -> datetime | None:
        fields = (
            ("external_account", "created_at"),
            ("external_account_event", "available_to_system_at"),
            ("continuous_monitor_event", "available_at"),
            ("research_subject_events", "available_at"),
            ("market_regime_snapshots_v2", "valid_from"),
            ("market_regime_snapshots_v2", "expires_at"),
        )
        boundaries: list[datetime] = []
        with self.store.connect() as connection:
            for table, column in fields:
                rows = connection.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE julianday("{column}")>=julianday(?)',
                    (as_of.isoformat(),),
                ).fetchall()
                boundaries.extend(
                    value for row in rows if (value := aware_time(str(row[0]))) > as_of
                )
        return min(boundaries) if boundaries else None
