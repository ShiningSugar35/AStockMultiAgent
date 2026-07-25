"""Append-only double-entry paper account with idempotent order and fill services."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo

from astock.core.errors import FailureClass, PolicyError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    AccountType,
    CorporateActionEvent,
    CorporateActionObservation,
    CorporateActionStatus,
    Fill,
    Market,
    NormalBalance,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioNAV,
    ReplayCheckpoint,
    ReplayFeeSchedule,
    ReplayQuality,
)


@dataclass(frozen=True, slots=True)
class PostResult:
    event_id: str
    event_seq: int
    created: bool


@dataclass(frozen=True, slots=True)
class LedgerLine:
    account_code: str
    debit_fen: int = 0
    credit_fen: int = 0


class ReplayFillPlan(TypedDict):
    fill_id: str
    order_id: str
    qty: int
    price_fen: int


_ACCOUNT_SPECS: dict[str, tuple[AccountType, NormalBalance]] = {
    "CASH": (AccountType.ASSET, NormalBalance.DEBIT),
    "FROZEN_CASH": (AccountType.ASSET, NormalBalance.DEBIT),
    "SECURITIES": (AccountType.ASSET, NormalBalance.DEBIT),
    "FEES": (AccountType.EXPENSE, NormalBalance.DEBIT),
    "CAPITAL": (AccountType.EQUITY, NormalBalance.CREDIT),
    "DIVIDEND_INCOME": (AccountType.INCOME, NormalBalance.CREDIT),
    "REALIZED_PNL": (AccountType.INCOME, NormalBalance.CREDIT),
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACTIVE_CONNECTION: ContextVar[sqlite3.Connection | None] = ContextVar(
    "paper_ledger_active_connection", default=None
)


class LedgerService:
    def __init__(self, state: StateStore, objects: ObjectStore | None = None) -> None:
        self.state = state
        self.objects = objects

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        active = _ACTIVE_CONNECTION.get()
        if active is not None:
            yield active
            return
        with self.state.transaction() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Join the active paper transaction or open one immediate writer transaction."""

        with self._transaction() as connection:
            yield connection

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Reuse one immediate transaction across a bounded paper-ledger operation."""

        if _ACTIVE_CONNECTION.get() is not None:
            yield
            return
        with self.state.transaction() as connection:
            token = _ACTIVE_CONNECTION.set(connection)
            try:
                yield
            finally:
                _ACTIVE_CONNECTION.reset(token)

    def initialize_account(self, account_id: str, initial_cash_fen: int) -> PostResult:
        if initial_cash_fen < 0:
            raise ValueError("initial_cash_fen cannot be negative")
        idempotency_key = f"account-init:{account_id}"
        lines = []
        if initial_cash_fen:
            lines = [
                LedgerLine("CASH", debit_fen=initial_cash_fen),
                LedgerLine("CAPITAL", credit_fen=initial_cash_fen),
            ]
        with self._transaction() as connection:
            existing = self._existing_event(connection, idempotency_key)
            if existing is not None:
                return self._post_event_in_transaction(
                    connection,
                    account_id=account_id,
                    event_type="ACCOUNT_INITIALIZED",
                    idempotency_key=idempotency_key,
                    payload={"initial_cash_fen": initial_cash_fen},
                    lines=lines,
                    occurred_at=datetime.now(UTC),
                )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO paper_account(account_id,status,created_at) VALUES(?,?,?)",
                (account_id, "OPEN", now),
            )
            for code, (account_type, normal_balance) in _ACCOUNT_SPECS.items():
                connection.execute(
                    "INSERT INTO ledger_account(account_id,paper_account_id,account_type,currency,"
                    "normal_balance,status) VALUES(?,?,?,?,?,?)",
                    (
                        self._ledger_account_id(account_id, code),
                        account_id,
                        account_type.value,
                        "CNY",
                        normal_balance.value,
                        "OPEN",
                    ),
                )
            return self._post_event_in_transaction(
                connection,
                account_id=account_id,
                event_type="ACCOUNT_INITIALIZED",
                idempotency_key=idempotency_key,
                payload={"initial_cash_fen": initial_cash_fen},
                lines=lines,
                occurred_at=datetime.now(UTC),
            )

    def post_event(
        self,
        *,
        account_id: str,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        lines: list[LedgerLine],
        occurred_at: datetime | None = None,
    ) -> PostResult:
        with self._transaction() as connection:
            return self._post_event_in_transaction(
                connection,
                account_id=account_id,
                event_type=event_type,
                idempotency_key=idempotency_key,
                payload=payload,
                lines=lines,
                occurred_at=occurred_at or datetime.now(UTC),
            )

    def place_order(
        self,
        *,
        account_id: str,
        client_request_id: str,
        symbol: str,
        side: OrderSide,
        qty: int,
        limit_price_fen: int,
        fee_reserve_fen: int = 0,
        effective_rule_version: str = "cn-a-m1",
        submitted_at: datetime | None = None,
    ) -> Order:
        if qty <= 0 or qty % 100 != 0:
            raise ValueError("M1 A-share orders require a positive 100-share lot multiple")
        if limit_price_fen <= 0 or fee_reserve_fen < 0:
            raise ValueError("limit price must be positive and fee reserve non-negative")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM order_record WHERE account_id=? AND client_request_id=?",
                (account_id, client_request_id),
            ).fetchone()
            if existing is not None:
                stored = self._order_from_row(existing)
                if (
                    stored.symbol != symbol
                    or stored.side is not side
                    or stored.qty != qty
                    or stored.limit_price_fen != limit_price_fen
                    or stored.effective_rule_version != effective_rule_version
                    or (submitted_at is not None and stored.submitted_at != submitted_at)
                ):
                    raise PolicyError(
                        "Order client-request identity collision",
                        failure_class=FailureClass.CONFLICT,
                    )
                return stored
            self._require_account(connection, account_id)
            order_id = uuid4().hex
            reserve_fen = 0
            reserve_qty = 0
            lines: list[LedgerLine] = []
            if side == OrderSide.BUY:
                reserve_fen = qty * limit_price_fen + fee_reserve_fen
                available = self._balance(connection, account_id, "CASH")
                if available < reserve_fen:
                    raise PolicyError(
                        "Insufficient paper cash",
                        failure_class=FailureClass.POLICY_REJECTED,
                        details={"available_fen": available, "required_fen": reserve_fen},
                    )
                lines = [
                    LedgerLine("FROZEN_CASH", debit_fen=reserve_fen),
                    LedgerLine("CASH", credit_fen=reserve_fen),
                ]
            else:
                position = connection.execute(
                    "SELECT * FROM position WHERE account_id=? AND symbol=?",
                    (account_id, symbol),
                ).fetchone()
                available_qty = int(position["qty_available"]) if position else 0
                if available_qty < qty:
                    raise PolicyError(
                        "Insufficient available paper position",
                        failure_class=FailureClass.POLICY_REJECTED,
                        details={"available_qty": available_qty, "required_qty": qty},
                    )
                reserve_qty = qty
                connection.execute(
                    "UPDATE position SET qty_available=qty_available-? "
                    "WHERE account_id=? AND symbol=?",
                    (qty, account_id, symbol),
                )
            submitted_at = submitted_at or datetime.now(UTC)
            connection.execute(
                "INSERT INTO order_record(order_id,account_id,client_request_id,symbol,side,"
                "order_type,qty,filled_qty,limit_price_fen,reserved_fen,reserved_qty,status,"
                "submitted_at,effective_rule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    account_id,
                    client_request_id,
                    symbol,
                    side.value,
                    OrderType.LIMIT.value,
                    qty,
                    0,
                    limit_price_fen,
                    reserve_fen,
                    reserve_qty,
                    OrderStatus.ACCEPTED.value,
                    submitted_at.isoformat(),
                    effective_rule_version,
                ),
            )
            self._post_event_in_transaction(
                connection,
                account_id=account_id,
                event_type="ORDER_ACCEPTED",
                idempotency_key=f"order:{account_id}:{client_request_id}",
                payload={"order_id": order_id, "symbol": symbol, "side": side.value, "qty": qty},
                lines=lines,
                occurred_at=submitted_at,
            )
            row = connection.execute(
                "SELECT * FROM order_record WHERE order_id=?", (order_id,)
            ).fetchone()
            return self._order_from_row(row)

    def open_orders(
        self,
        account_id: str,
        symbol: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> list[Order]:
        query = (
            "SELECT o.* FROM order_record o LEFT JOIN paper_order_rule_binding b "
            "ON b.order_id=o.order_id WHERE o.account_id=? AND o.status IN (?,?)"
        )
        parameters: list[object] = [
            account_id,
            OrderStatus.ACCEPTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        ]
        if symbol is not None:
            query += " AND o.symbol=?"
            parameters.append(symbol)
        if as_of is not None:
            query += " AND (b.order_id IS NULL OR b.validity='GTC' OR b.expires_at>=?)"
            parameters.append(as_of.isoformat())
        query += " ORDER BY o.submitted_at,o.client_request_id,o.order_id"
        with closing(self.state.connect()) as connection:
            self._require_account(connection, account_id)
            rows = connection.execute(query, parameters).fetchall()
        return [self._order_from_row(row) for row in rows]

    def get_order(self, order_id: str) -> Order:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM order_record WHERE order_id=?", (order_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown order: {order_id}")
        return self._order_from_row(row)

    def order_rule_binding(self, order_id: str) -> dict[str, object] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM paper_order_rule_binding WHERE order_id=?", (order_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def unbound_open_order_ids(self, account_id: str, symbol: str) -> list[str]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT o.order_id FROM order_record o LEFT JOIN paper_order_rule_binding b "
                "ON b.order_id=o.order_id WHERE o.account_id=? AND o.symbol=? "
                "AND o.status IN (?,?) AND b.order_id IS NULL ORDER BY o.order_id",
                (
                    account_id,
                    symbol,
                    OrderStatus.ACCEPTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                ),
            ).fetchall()
        return [str(row["order_id"]) for row in rows]

    def record_order_transition(
        self,
        order_id: str,
        from_status: OrderStatus,
        to_status: OrderStatus,
        *,
        occurred_at: datetime,
        source_operation_id: str | None = None,
        source_bar_commit_id: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            latest = connection.execute(
                "SELECT to_status FROM paper_order_transition WHERE order_id=? "
                "ORDER BY transition_seq DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if latest is not None and latest["to_status"] == to_status.value:
                return
            seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(transition_seq),0)+1 FROM paper_order_transition "
                    "WHERE order_id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO paper_order_transition(order_id,transition_seq,from_status,"
                "to_status,source_operation_id,source_bar_commit_id,occurred_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    order_id,
                    seq,
                    from_status.value,
                    to_status.value,
                    source_operation_id,
                    source_bar_commit_id,
                    occurred_at.isoformat(),
                ),
            )

    def record_fill(
        self,
        *,
        fill_id: str,
        order_id: str,
        qty: int,
        price_fen: int,
        commission_fen: int = 0,
        tax_fen: int = 0,
        transfer_fee_fen: int = 0,
        replay_quality: ReplayQuality = ReplayQuality.SINGLE_SOURCE_5M,
        occurred_at: datetime | None = None,
    ) -> Fill:
        if qty <= 0 or price_fen <= 0:
            raise ValueError("fill qty and price must be positive")
        if min(commission_fen, tax_fen, transfer_fee_fen) < 0:
            raise ValueError("fees cannot be negative")
        occurred_at = occurred_at or datetime.now(UTC)
        with self._transaction() as connection:
            existing_fill = connection.execute(
                "SELECT * FROM fill WHERE fill_id=?", (fill_id,)
            ).fetchone()
            if existing_fill is not None:
                existing = self._fill_from_row(existing_fill)
                expected = (
                    order_id,
                    qty,
                    price_fen,
                    commission_fen,
                    tax_fen,
                    transfer_fee_fen,
                    occurred_at,
                    replay_quality,
                )
                actual = (
                    existing.order_id,
                    existing.qty,
                    existing.price_fen,
                    existing.commission_fen,
                    existing.tax_fen,
                    existing.transfer_fee_fen,
                    existing.occurred_at,
                    existing.replay_quality,
                )
                if actual != expected:
                    raise PolicyError(
                        "Fill idempotency identity collision",
                        failure_class=FailureClass.CONFLICT,
                    )
                return self._fill_from_row(existing_fill)
            order = connection.execute(
                "SELECT * FROM order_record WHERE order_id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise ValueError(f"Unknown order: {order_id}")
            if order["status"] not in {
                OrderStatus.ACCEPTED.value,
                OrderStatus.PARTIALLY_FILLED.value,
            }:
                raise PolicyError(
                    f"Order is not fillable: {order['status']}",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            remaining = int(order["qty"]) - int(order["filled_qty"])
            if qty > remaining:
                raise ValueError("fill exceeds remaining order quantity")
            fees = commission_fen + tax_fen + transfer_fee_fen
            gross = qty * price_fen
            final_fill = qty == remaining
            lines: list[LedgerLine]
            position = connection.execute(
                "SELECT * FROM position WHERE account_id=? AND symbol=?",
                (order["account_id"], order["symbol"]),
            ).fetchone()
            binding = connection.execute(
                "SELECT market,instrument_id FROM paper_order_rule_binding WHERE order_id=?",
                (order_id,),
            ).fetchone()
            formal = binding is not None
            resulting_total_cost = 0
            if order["side"] == OrderSide.BUY.value:
                total = gross + fees
                reserved_fen = int(order["reserved_fen"])
                if total > reserved_fen:
                    raise PolicyError(
                        "Fill exceeds reserved paper cash",
                        failure_class=FailureClass.POLICY_REJECTED,
                        details={"reserved_fen": reserved_fen, "fill_total_fen": total},
                    )
                release = reserved_fen - total if final_fill else 0
                lines = [
                    LedgerLine("SECURITIES", debit_fen=gross),
                    LedgerLine("FROZEN_CASH", credit_fen=total),
                ]
                if fees:
                    lines.append(LedgerLine("FEES", debit_fen=fees))
                if release:
                    lines.extend(
                        [
                            LedgerLine("CASH", debit_fen=release),
                            LedgerLine("FROZEN_CASH", credit_fen=release),
                        ]
                    )
                old_qty = int(position["qty_total"]) if position else 0
                exact_cost = connection.execute(
                    "SELECT total_cost_fen FROM paper_position_cost "
                    "WHERE account_id=? AND symbol=?",
                    (order["account_id"], order["symbol"]),
                ).fetchone()
                if formal and old_qty and exact_cost is None:
                    raise PolicyError(
                        "Formal fill cannot backfill a legacy position cost",
                        failure_class=FailureClass.DATA_QUALITY,
                    )
                old_total_cost = (
                    int(exact_cost["total_cost_fen"])
                    if exact_cost is not None
                    else old_qty * (int(position["avg_cost_fen"]) if position else 0)
                )
                new_qty = old_qty + qty
                new_total_cost = old_total_cost + gross
                resulting_total_cost = new_total_cost
                new_avg = new_total_cost // new_qty
                if position:
                    connection.execute(
                        "UPDATE position SET qty_total=?,avg_cost_fen=? "
                        "WHERE account_id=? AND symbol=?",
                        (new_qty, new_avg, order["account_id"], order["symbol"]),
                    )
                else:
                    connection.execute(
                        "INSERT INTO position(account_id,symbol,qty_total,qty_available,"
                        "avg_cost_fen,"
                        "realized_pnl_fen,as_of_event_seq) VALUES(?,?,?,?,?,?,0)",
                        (order["account_id"], order["symbol"], qty, 0, new_avg, 0),
                    )
                new_reserved_fen = 0 if final_fill else reserved_fen - total
                new_reserved_qty = 0
            else:
                if position is None or int(position["qty_total"]) < qty:
                    raise PolicyError(
                        "Position disappeared before sell fill",
                        failure_class=FailureClass.CONFLICT,
                    )
                if formal:
                    lots = connection.execute(
                        "SELECT lot_id,remaining_qty,total_cost_fen FROM paper_position_lot "
                        "WHERE account_id=? AND symbol=? AND remaining_qty>0 "
                        "ORDER BY acquired_at,lot_id",
                        (order["account_id"], order["symbol"]),
                    ).fetchall()
                    if sum(int(lot["remaining_qty"]) for lot in lots) < qty:
                        raise PolicyError(
                            "Formal sell lacks an exact FIFO lot chain",
                            failure_class=FailureClass.DATA_QUALITY,
                        )
                    remaining_to_consume = qty
                    cost_basis = 0
                    for lot in lots:
                        if remaining_to_consume == 0:
                            break
                        lot_qty = int(lot["remaining_qty"])
                        lot_cost = int(lot["total_cost_fen"])
                        consume = min(lot_qty, remaining_to_consume)
                        allocated = (
                            lot_cost if consume == lot_qty else lot_cost * consume // lot_qty
                        )
                        connection.execute(
                            "UPDATE paper_position_lot SET remaining_qty=?,total_cost_fen=? "
                            "WHERE lot_id=?",
                            (lot_qty - consume, lot_cost - allocated, lot["lot_id"]),
                        )
                        cost_basis += allocated
                        remaining_to_consume -= consume
                else:
                    cost_basis = qty * int(position["avg_cost_fen"])
                cash_net = gross - fees
                if cash_net < 0:
                    raise ValueError("fees exceed sell proceeds")
                lines = [
                    LedgerLine("CASH", debit_fen=cash_net),
                    LedgerLine("SECURITIES", credit_fen=cost_basis),
                ]
                if fees:
                    lines.append(LedgerLine("FEES", debit_fen=fees))
                difference = gross - cost_basis
                if difference > 0:
                    lines.append(LedgerLine("REALIZED_PNL", credit_fen=difference))
                elif difference < 0:
                    lines.append(LedgerLine("REALIZED_PNL", debit_fen=-difference))
                new_qty = int(position["qty_total"]) - qty
                remaining_cost = (
                    int(
                        connection.execute(
                            "SELECT COALESCE(SUM(total_cost_fen),0) FROM paper_position_lot "
                            "WHERE account_id=? AND symbol=? AND remaining_qty>0",
                            (order["account_id"], order["symbol"]),
                        ).fetchone()[0]
                    )
                    if formal
                    else new_qty * int(position["avg_cost_fen"])
                )
                resulting_total_cost = remaining_cost
                realized_net = gross - cost_basis - fees
                connection.execute(
                    "UPDATE position SET qty_total=?,avg_cost_fen=?,realized_pnl_fen="
                    "realized_pnl_fen+? WHERE account_id=? AND symbol=?",
                    (
                        new_qty,
                        remaining_cost // new_qty if new_qty else 0,
                        realized_net,
                        order["account_id"],
                        order["symbol"],
                    ),
                )
                new_reserved_fen = 0
                new_reserved_qty = int(order["reserved_qty"]) - qty
            post = self._post_event_in_transaction(
                connection,
                account_id=order["account_id"],
                event_type="FILL_RECORDED",
                idempotency_key=f"fill:{fill_id}",
                payload={"fill_id": fill_id, "order_id": order_id, "qty": qty},
                lines=lines,
                occurred_at=occurred_at,
            )
            connection.execute(
                "INSERT INTO fill(fill_id,order_id,qty,price_fen,commission_fen,tax_fen,"
                "transfer_fee_fen,occurred_at,replay_quality) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    fill_id,
                    order_id,
                    qty,
                    price_fen,
                    commission_fen,
                    tax_fen,
                    transfer_fee_fen,
                    occurred_at.isoformat(),
                    replay_quality.value,
                ),
            )
            if formal:
                identity = connection.execute(
                    "SELECT market,instrument_id FROM paper_position_identity "
                    "WHERE account_id=? AND symbol=?",
                    (order["account_id"], order["symbol"]),
                ).fetchone()
                expected_identity = (binding["market"], binding["instrument_id"])
                if identity is not None and tuple(identity) != expected_identity:
                    raise PolicyError(
                        "Position instrument identity collision",
                        failure_class=FailureClass.CONFLICT,
                    )
                if identity is None:
                    connection.execute(
                        "INSERT INTO paper_position_identity(account_id,symbol,market,"
                        "instrument_id) VALUES(?,?,?,?)",
                        (
                            order["account_id"],
                            order["symbol"],
                            binding["market"],
                            binding["instrument_id"],
                        ),
                    )
                connection.execute(
                    "INSERT INTO paper_position_cost(account_id,symbol,total_cost_fen) "
                    "VALUES(?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET "
                    "total_cost_fen=excluded.total_cost_fen",
                    (order["account_id"], order["symbol"], resulting_total_cost),
                )
                if order["side"] == OrderSide.BUY.value:
                    connection.execute(
                        "INSERT INTO paper_position_lot(lot_id,account_id,symbol,acquired_at,"
                        "remaining_qty,total_cost_fen,source_fill_id,source_action_id) "
                        "VALUES(?,?,?,?,?,?,?,NULL)",
                        (
                            f"fill:{fill_id}",
                            order["account_id"],
                            order["symbol"],
                            occurred_at.isoformat(),
                            qty,
                            gross,
                            fill_id,
                        ),
                    )
            if order["side"] == OrderSide.BUY.value:
                trade_date = occurred_at.astimezone(_SHANGHAI).date()
                settlement_id = uuid4().hex
                connection.execute(
                    "INSERT INTO position_settlement(settlement_id,account_id,symbol,qty,"
                    "trade_date,eligible_on,source_event_id,status) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        settlement_id,
                        order["account_id"],
                        order["symbol"],
                        qty,
                        trade_date.isoformat(),
                        (trade_date + timedelta(days=1)).isoformat(),
                        post.event_id,
                        "PENDING_CALENDAR_CONFIRMATION",
                    ),
                )
                if formal:
                    connection.execute(
                        "INSERT INTO paper_settlement_identity(settlement_id,market,instrument_id) "
                        "VALUES(?,?,?)",
                        (settlement_id, binding["market"], binding["instrument_id"]),
                    )
            new_filled = int(order["filled_qty"]) + qty
            new_status = OrderStatus.FILLED if final_fill else OrderStatus.PARTIALLY_FILLED
            connection.execute(
                "UPDATE order_record SET filled_qty=?,reserved_fen=?,reserved_qty=?,status=? "
                "WHERE order_id=?",
                (new_filled, new_reserved_fen, new_reserved_qty, new_status.value, order_id),
            )
            connection.execute(
                "UPDATE position SET as_of_event_seq=? WHERE account_id=? AND symbol=?",
                (post.event_seq, order["account_id"], order["symbol"]),
            )
            row = connection.execute("SELECT * FROM fill WHERE fill_id=?", (fill_id,)).fetchone()
            return self._fill_from_row(row)

    def cancel_order(self, order_id: str) -> Order:
        with self._transaction() as connection:
            order = connection.execute(
                "SELECT * FROM order_record WHERE order_id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise ValueError(f"Unknown order: {order_id}")
            if order["status"] == OrderStatus.CANCELLED.value:
                return self._order_from_row(order)
            if order["status"] not in {
                OrderStatus.ACCEPTED.value,
                OrderStatus.PARTIALLY_FILLED.value,
            }:
                raise PolicyError(
                    f"Order cannot be cancelled: {order['status']}",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            lines: list[LedgerLine] = []
            if order["side"] == OrderSide.BUY.value and int(order["reserved_fen"]):
                remaining = int(order["reserved_fen"])
                lines = [
                    LedgerLine("CASH", debit_fen=remaining),
                    LedgerLine("FROZEN_CASH", credit_fen=remaining),
                ]
            elif order["side"] == OrderSide.SELL.value and int(order["reserved_qty"]):
                connection.execute(
                    "UPDATE position SET qty_available=qty_available+? "
                    "WHERE account_id=? AND symbol=?",
                    (int(order["reserved_qty"]), order["account_id"], order["symbol"]),
                )
            self._post_event_in_transaction(
                connection,
                account_id=order["account_id"],
                event_type="ORDER_CANCELLED",
                idempotency_key=f"cancel:{order_id}",
                payload={"order_id": order_id},
                lines=lines,
                occurred_at=datetime.now(UTC),
            )
            connection.execute(
                "UPDATE order_record SET reserved_fen=0,reserved_qty=0,status=? WHERE order_id=?",
                (OrderStatus.CANCELLED.value, order_id),
            )
            row = connection.execute(
                "SELECT * FROM order_record WHERE order_id=?", (order_id,)
            ).fetchone()
            return self._order_from_row(row)

    def save_replay_checkpoint(self, checkpoint: ReplayCheckpoint) -> None:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT market_cursor FROM replay_checkpoint WHERE account_id=? AND symbol=?",
                (checkpoint.account_id, checkpoint.symbol),
            ).fetchone()
            if existing and existing["market_cursor"] and checkpoint.market_cursor:
                old = datetime.fromisoformat(existing["market_cursor"])
                new = datetime.fromisoformat(checkpoint.market_cursor)
                if new < old:
                    raise PolicyError(
                        "Replay cursor cannot move backwards",
                        failure_class=FailureClass.POLICY_REJECTED,
                    )
            connection.execute(
                "INSERT INTO replay_checkpoint(account_id,symbol,requested_resolution,"
                "actual_resolution,replay_quality,provider_id,coverage_start,coverage_end,"
                "missing_bars,fallback_reason,last_event_seq,market_cursor,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET "
                "requested_resolution=excluded.requested_resolution,"
                "actual_resolution=excluded.actual_resolution,replay_quality=excluded.replay_quality,"
                "provider_id=excluded.provider_id,coverage_start=excluded.coverage_start,"
                "coverage_end=excluded.coverage_end,missing_bars=excluded.missing_bars,"
                "fallback_reason=excluded.fallback_reason,last_event_seq=excluded.last_event_seq,"
                "market_cursor=excluded.market_cursor,updated_at=excluded.updated_at",
                (
                    checkpoint.account_id,
                    checkpoint.symbol,
                    checkpoint.requested_resolution,
                    checkpoint.actual_resolution,
                    checkpoint.replay_quality.value,
                    checkpoint.provider_id,
                    checkpoint.coverage_start.isoformat() if checkpoint.coverage_start else None,
                    checkpoint.coverage_end.isoformat() if checkpoint.coverage_end else None,
                    checkpoint.missing_bars,
                    checkpoint.fallback_reason,
                    checkpoint.last_event_seq,
                    checkpoint.market_cursor,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def commit_replay_bar(
        self,
        *,
        account_id: str,
        symbol: str,
        bar_observation_id: str,
        input_hash: str,
        fill_plans: list[ReplayFillPlan],
        checkpoint: ReplayCheckpoint,
        fee_schedule: ReplayFeeSchedule,
        interrupt_after_fills: bool = False,
    ) -> tuple[list[Fill], ReplayCheckpoint]:
        """Commit every fill and the cursor for one bar in one SQLite transaction."""

        if self.objects is None:
            raise PolicyError(
                "Atomic replay requires an ObjectStore-bound ledger",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        if checkpoint.account_id != account_id or checkpoint.symbol != symbol:
            raise ValueError("Replay checkpoint does not match bar identity")
        if checkpoint.market is None or checkpoint.instrument_id is None:
            raise PolicyError(
                "Formal replay checkpoint requires market and instrument identity",
                failure_class=FailureClass.DATA_QUALITY,
            )
        if checkpoint.instrument_id != f"{checkpoint.market.value}:{symbol}":
            raise PolicyError(
                "Replay checkpoint instrument identity mismatch",
                failure_class=FailureClass.CONFLICT,
            )
        fee_schedule_hash = content_hash(
            fee_schedule.model_dump(mode="json", exclude={"created_at"})
        )
        commit_id = content_hash(
            {
                "account_id": account_id,
                "market": checkpoint.market.value,
                "instrument_id": checkpoint.instrument_id,
                "symbol": symbol,
                "bar_observation_id": bar_observation_id,
            }
        )
        with self.state.transaction() as connection:
            token = _ACTIVE_CONNECTION.set(connection)
            try:
                existing = connection.execute(
                    "SELECT input_hash,commit_object_hash,fill_ids_json,checkpoint_json "
                    "FROM paper_replay_bar_commit WHERE commit_id=?",
                    (commit_id,),
                ).fetchone()
                if existing is not None:
                    object_hash = str(existing["commit_object_hash"])
                    if existing["input_hash"] != input_hash or not self.objects.verify(
                        object_hash
                    ):
                        raise PolicyError(
                            "Replay bar identity collision",
                            failure_class=FailureClass.CONFLICT,
                        )
                    ids = json.loads(existing["fill_ids_json"])
                    committed = ReplayCheckpoint.model_validate_json(
                        existing["checkpoint_json"]
                    )
                    expected_payload = {
                        "commit_id": commit_id,
                        "account_id": account_id,
                        "market": checkpoint.market.value,
                        "instrument_id": checkpoint.instrument_id,
                        "symbol": symbol,
                        "bar_observation_id": bar_observation_id,
                        "input_hash": input_hash,
                        "fill_plans": fill_plans,
                        "fill_ids": ids,
                        "checkpoint": committed.model_dump(mode="json"),
                        "fee_schedule_hash": fee_schedule_hash,
                    }
                    if self.objects.get_bytes(object_hash) != canonical_json_bytes(
                        expected_payload
                    ):
                        raise PolicyError(
                            "Replay bar object binding mismatch",
                            failure_class=FailureClass.CONFLICT,
                        )
                    fills = [
                        self._fill_from_row(
                            connection.execute(
                                "SELECT * FROM fill WHERE fill_id=?", (fill_id,)
                            ).fetchone()
                        )
                        for fill_id in ids
                    ]
                    return fills, committed
                self._expire_day_orders(
                    account_id,
                    checkpoint.coverage_end or datetime.now(UTC),
                    source_bar_commit_id=commit_id,
                )
                fills: list[Fill] = []
                for plan in fill_plans:
                    order_id = str(plan["order_id"])
                    order_row = connection.execute(
                        "SELECT * FROM order_record WHERE order_id=?", (order_id,)
                    ).fetchone()
                    if order_row is None or order_row["account_id"] != account_id:
                        raise PolicyError(
                            "Replay fill references an unknown account order",
                            failure_class=FailureClass.CONFLICT,
                        )
                    binding = connection.execute(
                        "SELECT market,instrument_id,fee_rule_version,fee_schedule_hash "
                        "FROM paper_order_rule_binding WHERE order_id=?",
                        (order_id,),
                    ).fetchone()
                    if binding is None:
                        raise PolicyError(
                            "Formal replay refuses a legacy order without a verified binding",
                            failure_class=FailureClass.DATA_QUALITY,
                        )
                    if (
                        binding["market"] != checkpoint.market.value
                        or binding["instrument_id"] != checkpoint.instrument_id
                        or binding["fee_rule_version"] != fee_schedule.rule_version
                        or binding["fee_schedule_hash"] != fee_schedule_hash
                    ):
                        raise PolicyError(
                            "Order binding does not match replay market or frozen fee schedule",
                            failure_class=FailureClass.CONFLICT,
                        )
                    prior = connection.execute(
                        "SELECT gross_fen,commission_fen,tax_fen,transfer_fee_fen,"
                        "fee_rule_version,fee_schedule_hash FROM paper_fee_accrual "
                        "WHERE order_id=?",
                        (order_id,),
                    ).fetchone()
                    if prior is not None and (
                        prior["fee_rule_version"] != fee_schedule.rule_version
                        or prior["fee_schedule_hash"] != fee_schedule_hash
                    ):
                        raise PolicyError(
                            "An order cannot switch fee schedules between partial fills",
                            failure_class=FailureClass.CONFLICT,
                        )
                    if prior is None and int(order_row["filled_qty"]) > 0:
                        raise PolicyError(
                            "Partial fill history has no reliable frozen fee accrual",
                            failure_class=FailureClass.DATA_QUALITY,
                        )
                    gross_delta = int(plan["qty"]) * int(plan["price_fen"])
                    prior_gross = int(prior["gross_fen"]) if prior else 0
                    cumulative_gross = prior_gross + gross_delta
                    target_commission = _commission_for_gross(cumulative_gross, fee_schedule)
                    target_tax = (
                        _round_fee(
                            Decimal(cumulative_gross) * fee_schedule.stamp_tax_sell_rate
                        )
                        if order_row["side"] == OrderSide.SELL.value
                        else 0
                    )
                    target_transfer = _round_fee(
                        Decimal(cumulative_gross) * fee_schedule.transfer_fee_rate
                    )
                    commission = target_commission - (
                        int(prior["commission_fen"]) if prior else 0
                    )
                    tax = target_tax - (int(prior["tax_fen"]) if prior else 0)
                    transfer = target_transfer - (
                        int(prior["transfer_fee_fen"]) if prior else 0
                    )
                    before_status = OrderStatus(str(order_row["status"]))
                    fill = self.record_fill(
                        fill_id=str(plan["fill_id"]),
                        order_id=order_id,
                        qty=int(plan["qty"]),
                        price_fen=int(plan["price_fen"]),
                        commission_fen=commission,
                        tax_fen=tax,
                        transfer_fee_fen=transfer,
                        replay_quality=checkpoint.replay_quality,
                        occurred_at=checkpoint.coverage_end,
                    )
                    fills.append(fill)
                    connection.execute(
                        "INSERT INTO paper_fee_accrual(order_id,gross_fen,commission_fen,"
                        "tax_fen,transfer_fee_fen,fee_rule_version,fee_schedule_hash,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                        "gross_fen=excluded.gross_fen,commission_fen=excluded.commission_fen,"
                        "tax_fen=excluded.tax_fen,transfer_fee_fen=excluded.transfer_fee_fen,"
                        "updated_at=excluded.updated_at",
                        (
                            order_id,
                            cumulative_gross,
                            target_commission,
                            target_tax,
                            target_transfer,
                            fee_schedule.rule_version,
                            fee_schedule_hash,
                            (checkpoint.coverage_end or datetime.now(UTC)).isoformat(),
                        ),
                    )
                    after_row = connection.execute(
                        "SELECT status FROM order_record WHERE order_id=?", (order_id,)
                    ).fetchone()
                    after_status = OrderStatus(str(after_row["status"]))
                    self.record_order_transition(
                        order_id,
                        before_status,
                        after_status,
                        occurred_at=checkpoint.coverage_end or datetime.now(UTC),
                        source_bar_commit_id=commit_id,
                    )
                if interrupt_after_fills:
                    raise RuntimeError("simulated crash after replay fills")
                last_event_seq = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM journal WHERE paper_account_id=?",
                        (account_id,),
                    ).fetchone()[0]
                )
                committed_checkpoint = checkpoint.model_copy(
                    update={"last_event_seq": last_event_seq}
                )
                self.save_replay_checkpoint(committed_checkpoint)
                fill_ids = [fill.fill_id for fill in fills]
                commit_payload = {
                    "commit_id": commit_id,
                    "account_id": account_id,
                    "market": checkpoint.market.value,
                    "instrument_id": checkpoint.instrument_id,
                    "symbol": symbol,
                    "bar_observation_id": bar_observation_id,
                    "input_hash": input_hash,
                    "fill_plans": fill_plans,
                    "fill_ids": fill_ids,
                    "checkpoint": committed_checkpoint.model_dump(mode="json"),
                    "fee_schedule_hash": fee_schedule_hash,
                }
                commit_ref = self.objects.put_json(commit_payload)
                connection.execute(
                    "INSERT INTO paper_replay_bar_commit(commit_id,account_id,market,"
                    "instrument_id,symbol,"
                    "bar_observation_id,input_hash,commit_object_hash,fill_ids_json,"
                    "checkpoint_json,committed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        commit_id,
                        account_id,
                        checkpoint.market.value,
                        checkpoint.instrument_id,
                        symbol,
                        bar_observation_id,
                        input_hash,
                        commit_ref.sha256,
                        canonical_json_bytes(fill_ids).decode(),
                        canonical_json_bytes(
                            committed_checkpoint.model_dump(mode="json")
                        ).decode(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                return fills, committed_checkpoint
            finally:
                _ACTIVE_CONNECTION.reset(token)

    def replay_checkpoint(self, account_id: str, symbol: str) -> ReplayCheckpoint | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM replay_checkpoint WHERE account_id=? AND symbol=?",
                (account_id, symbol),
            ).fetchone()
            identity = connection.execute(
                "SELECT market,instrument_id FROM paper_replay_bar_commit "
                "WHERE account_id=? AND symbol=? ORDER BY committed_at DESC,commit_id DESC LIMIT 1",
                (account_id, symbol),
            ).fetchone()
        if row is None:
            return None
        return ReplayCheckpoint(
            account_id=row["account_id"],
            market=Market(str(identity["market"])) if identity is not None else None,
            instrument_id=str(identity["instrument_id"]) if identity is not None else None,
            symbol=row["symbol"],
            requested_resolution=row["requested_resolution"],
            actual_resolution=row["actual_resolution"],
            replay_quality=ReplayQuality(row["replay_quality"]),
            provider_id=row["provider_id"],
            coverage_start=(
                datetime.fromisoformat(row["coverage_start"]) if row["coverage_start"] else None
            ),
            coverage_end=(
                datetime.fromisoformat(row["coverage_end"]) if row["coverage_end"] else None
            ),
            missing_bars=row["missing_bars"],
            fallback_reason=row["fallback_reason"],
            last_event_seq=row["last_event_seq"],
            market_cursor=row["market_cursor"],
        )

    def settle_buys(
        self,
        account_id: str,
        *,
        as_of: datetime,
        trading_calendar_confirmed: bool,
    ) -> int:
        """Release T+1 quantities only after a caller confirms the trading calendar."""

        if not trading_calendar_confirmed:
            raise PolicyError(
                "T+1 settlement requires a confirmed trading calendar date",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        local_date = as_of.astimezone(_SHANGHAI).date().isoformat()
        settled_qty = 0
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM position_settlement WHERE account_id=? AND status=? "
                "AND eligible_on<=? ORDER BY eligible_on,settlement_id",
                (account_id, "PENDING_CALENDAR_CONFIRMATION", local_date),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE position SET qty_available=qty_available+? "
                    "WHERE account_id=? AND symbol=?",
                    (row["qty"], account_id, row["symbol"]),
                )
                connection.execute(
                    "UPDATE position_settlement SET status='SETTLED',settled_at=? "
                    "WHERE settlement_id=?",
                    (as_of.isoformat(), row["settlement_id"]),
                )
                settled_qty += int(row["qty"])
        return settled_qty

    def settle_buys_with_calendar(
        self,
        account_id: str,
        *,
        as_of: datetime,
        open_session_dates: list[date],
        calendar_release_id: str,
        market: Market | None = None,
    ) -> int:
        """Release T+1 lots only on the next open date in one verified calendar release."""

        ordered = sorted(set(open_session_dates))
        if not ordered:
            raise PolicyError(
                "T+1 settlement calendar has no open sessions",
                failure_class=FailureClass.DATA_QUALITY,
            )
        local_date = as_of.astimezone(_SHANGHAI).date()
        settled_qty = 0
        with self._transaction() as connection:
            if market is not None:
                legacy = connection.execute(
                    "SELECT s.settlement_id FROM position_settlement s "
                    "LEFT JOIN paper_settlement_identity i ON i.settlement_id=s.settlement_id "
                    "WHERE s.account_id=? AND s.status=? AND i.settlement_id IS NULL LIMIT 1",
                    (account_id, "PENDING_CALENDAR_CONFIRMATION"),
                ).fetchone()
                if legacy is not None:
                    raise PolicyError(
                        "Formal settlement refuses a legacy lot without instrument identity",
                        failure_class=FailureClass.DATA_QUALITY,
                    )
                rows = connection.execute(
                    "SELECT s.*,i.market,i.instrument_id FROM position_settlement s "
                    "JOIN paper_settlement_identity i ON i.settlement_id=s.settlement_id "
                    "WHERE s.account_id=? AND s.status=? AND i.market=? "
                    "ORDER BY s.trade_date,s.settlement_id",
                    (account_id, "PENDING_CALENDAR_CONFIRMATION", market.value),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM position_settlement WHERE account_id=? AND status=? "
                    "ORDER BY trade_date,settlement_id",
                    (account_id, "PENDING_CALENDAR_CONFIRMATION"),
                ).fetchall()
            for row in rows:
                if market is not None and row["instrument_id"] != (
                    f"{market.value}:{row['symbol']}"
                ):
                    raise PolicyError(
                        "Settlement instrument identity mismatch",
                        failure_class=FailureClass.CONFLICT,
                    )
                trade_date = date.fromisoformat(str(row["trade_date"]))
                eligible = next((item for item in ordered if item > trade_date), None)
                if eligible is None:
                    if trade_date <= local_date:
                        raise PolicyError(
                            "Verified calendar does not cover the next open settlement date",
                            failure_class=FailureClass.DATA_QUALITY,
                        )
                    continue
                connection.execute(
                    "UPDATE position_settlement SET eligible_on=? WHERE settlement_id=?",
                    (eligible.isoformat(), row["settlement_id"]),
                )
                if eligible > local_date:
                    continue
                connection.execute(
                    "UPDATE position SET qty_available=qty_available+? "
                    "WHERE account_id=? AND symbol=?",
                    (row["qty"], account_id, row["symbol"]),
                )
                post = self._post_event_in_transaction(
                    connection,
                    account_id=account_id,
                    event_type="POSITION_T1_SETTLED",
                    idempotency_key=(
                        f"settlement:{row['settlement_id']}:{calendar_release_id}"
                    ),
                    payload={
                        "settlement_id": row["settlement_id"],
                        "eligible_on": eligible.isoformat(),
                        "calendar_release_id": calendar_release_id,
                    },
                    lines=[],
                    occurred_at=as_of,
                )
                connection.execute(
                    "UPDATE position_settlement SET status='SETTLED',settled_at=? "
                    "WHERE settlement_id=?",
                    (as_of.isoformat(), row["settlement_id"]),
                )
                connection.execute(
                    "UPDATE position SET as_of_event_seq=? WHERE account_id=? AND symbol=?",
                    (post.event_seq, account_id, row["symbol"]),
                )
                settled_qty += int(row["qty"])
        return settled_qty

    def register_corporate_action(self, event: CorporateActionEvent) -> None:
        with self._transaction() as connection:
            payload = canonical_json_bytes(event.model_dump(mode="json")).decode("utf-8")
            values = (
                event.symbol,
                event.event_type.value,
                event.ex_date,
                payload,
                event.source_id,
                event.rule_version,
            )
            existing = connection.execute(
                "SELECT symbol,event_type,ex_date,payload_json,source_id,rule_version "
                "FROM corporate_action_event WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                raise PolicyError(
                    "Corporate-action event identity collision",
                    failure_class=FailureClass.CONFLICT,
                )
            if existing is None:
                connection.execute(
                    "INSERT INTO corporate_action_event(event_id,symbol,event_type,ex_date,"
                    "payload_json,source_id,rule_version) VALUES(?,?,?,?,?,?,?)",
                    (event.event_id, *values),
                )

    def register_verified_corporate_action(
        self, observation: CorporateActionObservation, release_id: str
    ) -> None:
        if (
            observation.status is not CorporateActionStatus.TERMS_VERIFIED
            or not observation.ledger_eligible
            or observation.ex_date is None
            or observation.official_document_snapshot_id is None
        ):
            raise PolicyError(
                "Only exact TERMS_VERIFIED corporate actions are ledger eligible",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        payload = canonical_json_bytes(observation.model_dump(mode="json")).decode("utf-8")
        values = (
            observation.symbol,
            observation.action_type,
            observation.ex_date.isoformat(),
            payload,
            observation.official_document_snapshot_id,
            release_id,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT symbol,event_type,ex_date,payload_json,source_id,rule_version "
                "FROM corporate_action_event WHERE event_id=?",
                (observation.observation_id,),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                raise PolicyError(
                    "Corporate-action observation identity collision",
                    failure_class=FailureClass.CONFLICT,
                )
            if existing is None:
                connection.execute(
                    "INSERT INTO corporate_action_event(event_id,symbol,event_type,ex_date,"
                    "payload_json,source_id,rule_version) VALUES(?,?,?,?,?,?,?)",
                    (observation.observation_id, *values),
                )

    def apply_verified_corporate_action(
        self,
        account_id: str,
        observation: CorporateActionObservation,
        release_id: str,
        *,
        as_of: datetime,
    ) -> bool:
        """Apply exact verified terms; ambiguous tax or entitlement semantics fail closed."""

        if self.objects is None:
            raise PolicyError(
                "Corporate-action application requires an ObjectStore-bound ledger",
                failure_class=FailureClass.POLICY_REJECTED,
            )
        self.register_verified_corporate_action(observation, release_id)
        terms = observation.structured_terms
        record_date_text = terms.get("dividRegistDate")
        if not record_date_text:
            raise PolicyError(
                "Corporate action lacks an exact record date",
                failure_class=FailureClass.DATA_QUALITY,
            )
        record_date = date.fromisoformat(record_date_text)
        assert observation.ex_date is not None
        effective_date = observation.ex_date
        pay_date_text = terms.get("dividPayDate")
        if pay_date_text:
            effective_date = max(effective_date, date.fromisoformat(pay_date_text))
        if as_of.astimezone(_SHANGHAI).date() < effective_date:
            return False
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT release_id,application_hash,application_object_hash,result_json "
                "FROM paper_corporate_action_application WHERE action_observation_id=? "
                "AND account_id=?",
                (observation.observation_id, account_id),
            ).fetchone()
            fills = connection.execute(
                "SELECT f.qty,f.occurred_at,o.side FROM fill f JOIN order_record o "
                "ON o.order_id=f.order_id WHERE o.account_id=? AND o.symbol=?",
                (account_id, observation.symbol),
            ).fetchall()
            entitlement_qty = 0
            for fill in fills:
                occurred = datetime.fromisoformat(str(fill["occurred_at"]))
                if occurred.astimezone(_SHANGHAI).date() <= record_date:
                    direction = 1 if fill["side"] == OrderSide.BUY.value else -1
                    entitlement_qty += direction * int(fill["qty"])
            prior_applications = connection.execute(
                "SELECT a.result_json FROM paper_corporate_action_application a "
                "JOIN corporate_action_event e ON e.event_id=a.action_observation_id "
                "WHERE a.account_id=? AND a.action_observation_id<>? AND e.symbol=?",
                (account_id, observation.observation_id, observation.symbol),
            ).fetchall()
            for application in prior_applications:
                try:
                    prior_result = json.loads(str(application["result_json"]))
                    prior_effective = date.fromisoformat(str(prior_result["effective_date"]))
                    prior_stock = int(prior_result["stock_qty"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise PolicyError(
                        "Prior corporate-action entitlement snapshot is invalid",
                        failure_class=FailureClass.DATA_QUALITY,
                    ) from exc
                if prior_effective <= record_date:
                    entitlement_qty += prior_stock
            if entitlement_qty < 0:
                raise PolicyError(
                    "Corporate-action entitlement reconstruction is negative",
                    failure_class=FailureClass.CONFLICT,
                )
            stock_ratio = Decimal(terms.get("dividStocksPs", "0")) + Decimal(
                terms.get("dividReserveToStockPs", "0")
            )
            if stock_ratio < 0:
                raise PolicyError(
                    "Corporate-action stock ratio cannot be negative",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            exact_stock_qty = Decimal(entitlement_qty) * stock_ratio
            if exact_stock_qty != exact_stock_qty.to_integral_value():
                raise PolicyError(
                    "Fractional corporate-action entitlement cannot be silently truncated",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"exact_stock_qty": str(exact_stock_qty)},
                )
            stock_qty = int(exact_stock_qty)
            cash_after_tax_fen_text = terms.get("cashFenPsAfterTax")
            cash_after_tax_yuan_text = terms.get("cashPsAfterTax")
            cash_before_tax = Decimal(terms.get("dividCashPsBeforeTax", "0"))
            if cash_before_tax > 0 and not (
                cash_after_tax_fen_text or cash_after_tax_yuan_text
            ):
                raise PolicyError(
                    "Cash dividend lacks exact after-tax cash terms",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            cash_per_share_fen = (
                Decimal(cash_after_tax_fen_text)
                if cash_after_tax_fen_text
                else Decimal(cash_after_tax_yuan_text) * 100
                if cash_after_tax_yuan_text
                else Decimal(0)
            )
            cash_fen = _round_fee(Decimal(entitlement_qty) * cash_per_share_fen)
            result = {
                "entitlement_qty": entitlement_qty,
                "stock_qty": stock_qty,
                "cash_fen": cash_fen,
                "record_date": record_date.isoformat(),
                "effective_date": effective_date.isoformat(),
                "release_id": release_id,
            }
            application_payload = {
                "account_id": account_id,
                "observation": observation.model_dump(mode="json"),
                "result": result,
            }
            application_hash = content_hash(application_payload)
            application_ref = self.objects.put_json(application_payload)
            result_json = canonical_json_bytes(result).decode()
            expected = (release_id, application_hash, application_ref.sha256, result_json)
            if existing is not None:
                if tuple(existing) != expected:
                    raise PolicyError(
                        "Corporate-action application identity collision",
                        failure_class=FailureClass.CONFLICT,
                    )
                return False
            position = connection.execute(
                "SELECT * FROM position WHERE account_id=? AND symbol=?",
                (account_id, observation.symbol),
            ).fetchone()
            current_qty = int(position["qty_total"]) if position is not None else 0
            if stock_qty and current_qty != entitlement_qty:
                raise PolicyError(
                    "Post-record trades prevent exact stock-entitlement cost allocation",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            lines: list[LedgerLine] = []
            if cash_fen:
                lines = [
                    LedgerLine("CASH", debit_fen=cash_fen),
                    LedgerLine("DIVIDEND_INCOME", credit_fen=cash_fen),
                ]
            post = self._post_event_in_transaction(
                connection,
                account_id=account_id,
                event_type="CORPORATE_ACTION_APPLIED",
                idempotency_key=(
                    f"corporate-action:{account_id}:{observation.observation_id}"
                ),
                payload={
                    "observation_id": observation.observation_id,
                    **result,
                },
                lines=lines,
                occurred_at=as_of,
            )
            if stock_qty:
                assert position is not None
                new_qty = current_qty + stock_qty
                exact_cost = connection.execute(
                    "SELECT total_cost_fen FROM paper_position_cost "
                    "WHERE account_id=? AND symbol=?",
                    (account_id, observation.symbol),
                ).fetchone()
                total_cost = (
                    int(exact_cost["total_cost_fen"])
                    if exact_cost is not None
                    else current_qty * int(position["avg_cost_fen"])
                )
                new_avg = total_cost // new_qty
                connection.execute(
                    "UPDATE position SET qty_total=?,qty_available=qty_available+?,"
                    "avg_cost_fen=?,as_of_event_seq=? WHERE account_id=? AND symbol=?",
                    (
                        new_qty,
                        stock_qty,
                        new_avg,
                        post.event_seq,
                        account_id,
                        observation.symbol,
                    ),
                )
                if exact_cost is not None:
                    connection.execute(
                        "UPDATE paper_position_cost SET total_cost_fen=? "
                        "WHERE account_id=? AND symbol=?",
                        (total_cost, account_id, observation.symbol),
                    )
                    connection.execute(
                        "INSERT INTO paper_position_lot(lot_id,account_id,symbol,acquired_at,"
                        "remaining_qty,total_cost_fen,source_fill_id,source_action_id) "
                        "VALUES(?,?,?,?,?,0,NULL,?)",
                        (
                            f"action:{observation.observation_id}",
                            account_id,
                            observation.symbol,
                            as_of.isoformat(),
                            stock_qty,
                            observation.observation_id,
                        ),
                    )
            connection.execute(
                "INSERT INTO paper_corporate_action_application(action_observation_id,"
                "account_id,release_id,source_event_id,application_hash,"
                "application_object_hash,result_json,applied_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    account_id,
                    release_id,
                    post.event_id,
                    application_hash,
                    application_ref.sha256,
                    result_json,
                    as_of.isoformat(),
                ),
            )
            return True

    def status(self, account_id: str) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            self._require_account(connection, account_id)
            balances = {
                code: self._balance(connection, account_id, code) for code in _ACCOUNT_SPECS
            }
            positions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM position WHERE account_id=? ORDER BY symbol", (account_id,)
                ).fetchall()
            ]
            open_orders = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM order_record WHERE account_id=? AND status IN (?,?) "
                    "ORDER BY submitted_at",
                    (
                        account_id,
                        OrderStatus.ACCEPTED.value,
                        OrderStatus.PARTIALLY_FILLED.value,
                    ),
                ).fetchall()
            ]
            pending_settlements = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM position_settlement WHERE account_id=? AND status<>? "
                    "ORDER BY eligible_on",
                    (account_id, "SETTLED"),
                ).fetchall()
            ]
            imbalanced = connection.execute(
                "SELECT COUNT(*) FROM (SELECT j.event_id FROM journal j LEFT JOIN ledger_entry e "
                "ON e.event_id=j.event_id WHERE j.paper_account_id=? GROUP BY j.event_id "
                "HAVING COALESCE(SUM(e.debit_fen),0)<>COALESCE(SUM(e.credit_fen),0))",
                (account_id,),
            ).fetchone()[0]
            last_seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0) FROM journal WHERE paper_account_id=?", (account_id,)
            ).fetchone()[0]
        return {
            "account_id": account_id,
            "balances_fen": balances,
            "positions": positions,
            "open_orders": open_orders,
            "pending_settlements": pending_settlements,
            "last_event_seq": last_seq,
            "imbalanced_events": imbalanced,
            "integrity": self.state.integrity_check(),
        }

    def portfolio_nav(
        self,
        account_id: str,
        market_prices_fen: dict[str, int] | None = None,
        *,
        as_of: datetime | None = None,
        require_all_prices: bool = False,
    ) -> PortfolioNAV:
        market_prices_fen = market_prices_fen or {}
        mark_time = as_of or datetime.now(UTC)
        status = self.status(account_id)
        market_value = 0
        data_quality = "MARK_TO_COST"
        for position in status["positions"]:
            symbol = str(position["symbol"])
            price = market_prices_fen.get(symbol)
            if price is None:
                if require_all_prices and int(position["qty_total"]) > 0:
                    raise PolicyError(
                        "Formal mark is missing a position price",
                        failure_class=FailureClass.DATA_QUALITY,
                        details={"missing_symbol": symbol},
                    )
                price = int(position["avg_cost_fen"])
            else:
                data_quality = "MARKET_PRICE_INPUT"
            market_value += int(position["qty_total"]) * int(price)
        cash = int(status["balances_fen"]["CASH"])
        frozen = int(status["balances_fen"]["FROZEN_CASH"])
        return PortfolioNAV(
            created_at=mark_time,
            account_id=account_id,
            as_of=mark_time,
            cash_fen=cash,
            frozen_cash_fen=frozen,
            market_value_fen=market_value,
            nav_fen=cash + frozen + market_value,
            data_quality=("PIT_RELEASE_COMPLETE" if require_all_prices else data_quality),
        )

    def recover(
        self,
        account_id: str,
        *,
        as_of: datetime,
        expire_day_orders: bool,
        source_operation_id: str | None = None,
    ) -> dict[str, object]:
        expired = 0
        issues: list[str] = []
        with closing(self.state.connect()) as connection:
            self._require_account(connection, account_id)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                issues.append("FOREIGN_KEY_VIOLATION")
            imbalanced = connection.execute(
                "SELECT COUNT(*) FROM (SELECT j.event_id FROM journal j "
                "LEFT JOIN ledger_entry e ON e.event_id=j.event_id "
                "WHERE j.paper_account_id=? GROUP BY j.event_id "
                "HAVING COALESCE(SUM(e.debit_fen),0)<>COALESCE(SUM(e.credit_fen),0))",
                (account_id,),
            ).fetchone()[0]
            if imbalanced:
                issues.append("UNBALANCED_JOURNAL")
            frozen = self._balance(connection, account_id, "FROZEN_CASH")
            reserved = int(
                connection.execute(
                    "SELECT COALESCE(SUM(reserved_fen),0) FROM order_record "
                    "WHERE account_id=? AND side=? AND status IN (?,?)",
                    (
                        account_id,
                        OrderSide.BUY.value,
                        OrderStatus.ACCEPTED.value,
                        OrderStatus.PARTIALLY_FILLED.value,
                    ),
                ).fetchone()[0]
            )
            if frozen != reserved:
                issues.append("FROZEN_CASH_MISMATCH")
            fill_mismatch = connection.execute(
                "SELECT 1 FROM order_record o LEFT JOIN (SELECT order_id,SUM(qty) qty FROM fill "
                "GROUP BY order_id) f ON f.order_id=o.order_id WHERE o.account_id=? "
                "AND o.filled_qty<>COALESCE(f.qty,0) LIMIT 1",
                (account_id,),
            ).fetchone()
            if fill_mismatch is not None:
                issues.append("FILL_ORDER_MISMATCH")
            invalid_position = connection.execute(
                "SELECT 1 FROM position p WHERE p.account_id=? AND (p.qty_available<0 "
                "OR p.qty_available>p.qty_total) LIMIT 1",
                (account_id,),
            ).fetchone()
            if invalid_position is not None:
                issues.append("POSITION_QUANTITY_INVALID")
            exact_cost_mismatch = connection.execute(
                "SELECT 1 FROM position p "
                "LEFT JOIN paper_position_identity i ON i.account_id=p.account_id "
                "AND i.symbol=p.symbol "
                "LEFT JOIN paper_position_cost c ON c.account_id=p.account_id "
                "AND c.symbol=p.symbol LEFT JOIN (SELECT account_id,symbol,"
                "SUM(remaining_qty) AS qty,SUM(total_cost_fen) AS cost FROM paper_position_lot "
                "GROUP BY account_id,symbol) l ON l.account_id=p.account_id AND l.symbol=p.symbol "
                "WHERE p.account_id=? AND (i.instrument_id IS NULL OR c.total_cost_fen IS NULL "
                "OR COALESCE(l.qty,0)<>p.qty_total "
                "OR COALESCE(l.cost,0)<>c.total_cost_fen) LIMIT 1",
                (account_id,),
            ).fetchone()
            if exact_cost_mismatch is not None:
                issues.append("POSITION_EXACT_COST_CHAIN_INVALID")
            invalid_settlement = connection.execute(
                "SELECT 1 FROM position_settlement WHERE account_id=? "
                "AND status NOT IN ('PENDING_CALENDAR_CONFIRMATION','SETTLED') LIMIT 1",
                (account_id,),
            ).fetchone()
            if invalid_settlement is not None:
                issues.append("SETTLEMENT_STATE_INVALID")
            if source_operation_id is not None:
                nonterminal = connection.execute(
                    "SELECT r.operation_id,e.status FROM paper_operation_request r "
                    "JOIN paper_operation_execution e ON e.operation_id=r.operation_id "
                    "WHERE r.account_id=? AND r.operation_id<>? AND e.status IN (?,?,?) "
                    "ORDER BY r.operation_id",
                    (
                        account_id,
                        source_operation_id,
                        "PLANNED",
                        "VALIDATED",
                        "COMMITTED",
                    ),
                ).fetchall()
                issues.extend(
                    f"NONTERMINAL_OPERATION:{row['operation_id']}:{row['status']}"
                    for row in nonterminal
                )
            commits = connection.execute(
                "SELECT c.* FROM paper_replay_bar_commit c WHERE c.account_id=? "
                "AND NOT EXISTS (SELECT 1 FROM paper_replay_bar_commit newer "
                "WHERE newer.account_id=c.account_id AND newer.symbol=c.symbol "
                "AND (newer.committed_at>c.committed_at OR "
                "(newer.committed_at=c.committed_at AND newer.commit_id>c.commit_id)))",
                (account_id,),
            ).fetchall()
            for commit in commits:
                try:
                    checkpoint = ReplayCheckpoint.model_validate_json(
                        commit["checkpoint_json"]
                    )
                    fill_ids = json.loads(str(commit["fill_ids_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    issues.append("BAR_COMMIT_CHECKPOINT_INVALID")
                    break
                if (
                    checkpoint.account_id != account_id
                    or checkpoint.market is None
                    or checkpoint.market.value != commit["market"]
                    or checkpoint.instrument_id != commit["instrument_id"]
                    or checkpoint.symbol != commit["symbol"]
                    or not isinstance(fill_ids, list)
                    or not all(isinstance(item, str) for item in fill_ids)
                ):
                    issues.append("BAR_COMMIT_IDENTITY_MISMATCH")
                    break
                row = connection.execute(
                    "SELECT * FROM replay_checkpoint WHERE account_id=? AND symbol=?",
                    (account_id, checkpoint.symbol),
                ).fetchone()
                if row is None:
                    issues.append("BAR_COMMIT_CHECKPOINT_MISSING")
                    break
                current_checkpoint = ReplayCheckpoint(
                    account_id=str(row["account_id"]),
                    market=Market(str(commit["market"])),
                    instrument_id=str(commit["instrument_id"]),
                    symbol=str(row["symbol"]),
                    requested_resolution=str(row["requested_resolution"]),
                    actual_resolution=str(row["actual_resolution"]),
                    replay_quality=ReplayQuality(str(row["replay_quality"])),
                    provider_id=row["provider_id"],
                    coverage_start=(
                        datetime.fromisoformat(str(row["coverage_start"]))
                        if row["coverage_start"]
                        else None
                    ),
                    coverage_end=(
                        datetime.fromisoformat(str(row["coverage_end"]))
                        if row["coverage_end"]
                        else None
                    ),
                    missing_bars=int(row["missing_bars"]),
                    fallback_reason=row["fallback_reason"],
                    last_event_seq=int(row["last_event_seq"]),
                    market_cursor=row["market_cursor"],
                )
                if current_checkpoint != checkpoint:
                    issues.append("BAR_COMMIT_CHECKPOINT_MISMATCH")
                    break
                object_hash = str(commit["commit_object_hash"])
                if self.objects is None or not self.objects.verify(object_hash):
                    issues.append("BAR_COMMIT_OBJECT_CORRUPT")
                    break
                try:
                    commit_object = json.loads(self.objects.get_bytes(object_hash))
                except (TypeError, ValueError, json.JSONDecodeError):
                    issues.append("BAR_COMMIT_OBJECT_INVALID")
                    break
                expected_object_fields = {
                    "commit_id": str(commit["commit_id"]),
                    "account_id": account_id,
                    "market": str(commit["market"]),
                    "instrument_id": str(commit["instrument_id"]),
                    "symbol": str(commit["symbol"]),
                    "bar_observation_id": str(commit["bar_observation_id"]),
                    "input_hash": str(commit["input_hash"]),
                    "fill_ids": fill_ids,
                    "checkpoint": checkpoint.model_dump(mode="json"),
                }
                if not isinstance(commit_object, dict) or any(
                    commit_object.get(key) != value
                    for key, value in expected_object_fields.items()
                ):
                    issues.append("BAR_COMMIT_OBJECT_BINDING_MISMATCH")
                    break
        if not issues and expire_day_orders:
            expired = self._expire_day_orders(
                account_id, as_of, source_operation_id=source_operation_id
            )
        return {
            "status": (
                "CORRUPT" if issues else "RECOVERED" if expired else "HEALTHY_NOOP"
            ),
            "expired_order_count": expired,
            "issues": issues,
            "integrity": self.state.integrity_check(),
        }

    def _expire_day_orders(
        self,
        account_id: str,
        as_of: datetime,
        *,
        source_operation_id: str | None = None,
        source_bar_commit_id: str | None = None,
    ) -> int:
        expired = 0
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT o.* FROM order_record o JOIN paper_order_rule_binding b "
                "ON b.order_id=o.order_id WHERE o.account_id=? AND b.validity='DAY' "
                "AND o.status IN (?,?) ORDER BY o.submitted_at,o.order_id",
                (
                    account_id,
                    OrderStatus.ACCEPTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                ),
            ).fetchall()
            for order in rows:
                expires_at = connection.execute(
                    "SELECT expires_at FROM paper_order_rule_binding WHERE order_id=?",
                    (order["order_id"],),
                ).fetchone()[0]
                if expires_at is None or datetime.fromisoformat(str(expires_at)).astimezone(
                    UTC
                ) >= as_of.astimezone(UTC):
                    continue
                lines: list[LedgerLine] = []
                if order["side"] == OrderSide.BUY.value and int(order["reserved_fen"]):
                    remaining = int(order["reserved_fen"])
                    lines = [
                        LedgerLine("CASH", debit_fen=remaining),
                        LedgerLine("FROZEN_CASH", credit_fen=remaining),
                    ]
                elif order["side"] == OrderSide.SELL.value and int(order["reserved_qty"]):
                    connection.execute(
                        "UPDATE position SET qty_available=qty_available+? "
                        "WHERE account_id=? AND symbol=?",
                        (order["reserved_qty"], account_id, order["symbol"]),
                    )
                self._post_event_in_transaction(
                    connection,
                    account_id=account_id,
                    event_type="ORDER_EXPIRED",
                    idempotency_key=f"expire:{order['order_id']}",
                    payload={"order_id": order["order_id"]},
                    lines=lines,
                    occurred_at=as_of,
                )
                connection.execute(
                    "UPDATE order_record SET reserved_fen=0,reserved_qty=0,status=? "
                    "WHERE order_id=?",
                    (OrderStatus.EXPIRED.value, order["order_id"]),
                )
                self.record_order_transition(
                    str(order["order_id"]),
                    OrderStatus(str(order["status"])),
                    OrderStatus.EXPIRED,
                    occurred_at=as_of,
                    source_operation_id=source_operation_id,
                    source_bar_commit_id=source_bar_commit_id,
                )
                expired += 1
        return expired

    def _post_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        lines: list[LedgerLine],
        occurred_at: datetime,
    ) -> PostResult:
        existing = self._existing_event(connection, idempotency_key)
        if existing is not None:
            expected_payload = canonical_json_bytes(payload).decode("utf-8")
            stored_lines = [
                (
                    str(row["account_id"]).removeprefix(f"{account_id}:"),
                    int(row["debit_fen"]),
                    int(row["credit_fen"]),
                )
                for row in connection.execute(
                    "SELECT e.account_id,e.debit_fen,e.credit_fen FROM ledger_entry e "
                    "WHERE e.event_id=? ORDER BY e.account_id,e.debit_fen,e.credit_fen",
                    (existing["event_id"],),
                ).fetchall()
            ]
            expected_lines = sorted(
                (line.account_code, line.debit_fen, line.credit_fen) for line in lines
            )
            if (
                existing["paper_account_id"] != account_id
                or existing["event_type"] != event_type
                or existing["payload_json"] != expected_payload
                or stored_lines != expected_lines
            ):
                raise PolicyError(
                    "Journal idempotency key collides with different payload or lines",
                    failure_class=FailureClass.POLICY_REJECTED,
                )
            return PostResult(existing["event_id"], existing["seq"], False)
        total_debit = sum(line.debit_fen for line in lines)
        total_credit = sum(line.credit_fen for line in lines)
        if total_debit != total_credit:
            raise ValueError(f"Unbalanced event: debit={total_debit}, credit={total_credit}")
        if any(
            line.debit_fen < 0
            or line.credit_fen < 0
            or (line.debit_fen > 0) == (line.credit_fen > 0)
            for line in lines
        ):
            raise ValueError("Each ledger line must have exactly one positive side")
        event_id = uuid4().hex
        connection.execute(
            "INSERT INTO journal(event_id,paper_account_id,event_type,occurred_at,idempotency_key,"
            "payload_json) VALUES(?,?,?,?,?,?)",
            (
                event_id,
                account_id,
                event_type,
                occurred_at.isoformat(),
                idempotency_key,
                canonical_json_bytes(payload).decode("utf-8"),
            ),
        )
        event_seq = int(
            connection.execute("SELECT seq FROM journal WHERE event_id=?", (event_id,)).fetchone()[
                0
            ]
        )
        for line in lines:
            account = self._ledger_account_id(account_id, line.account_code)
            connection.execute(
                "INSERT INTO ledger_entry(entry_id,event_id,account_id,debit_fen,credit_fen) "
                "VALUES(?,?,?,?,?)",
                (uuid4().hex, event_id, account, line.debit_fen, line.credit_fen),
            )
        return PostResult(event_id, event_seq, True)

    @staticmethod
    def _existing_event(connection: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT event_id,seq,paper_account_id,event_type,payload_json FROM journal "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _require_account(connection: sqlite3.Connection, account_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM paper_account WHERE account_id=?", (account_id,)
            ).fetchone()
            is None
        ):
            raise ValueError(f"Unknown paper account: {account_id}")

    @staticmethod
    def _ledger_account_id(account_id: str, code: str) -> str:
        if code not in _ACCOUNT_SPECS:
            raise ValueError(f"Unknown ledger account code: {code}")
        return f"{account_id}:{code}"

    def _balance(self, connection: sqlite3.Connection, account_id: str, code: str) -> int:
        account = self._ledger_account_id(account_id, code)
        row = connection.execute(
            "SELECT COALESCE(SUM(debit_fen),0) AS debit,COALESCE(SUM(credit_fen),0) AS credit "
            "FROM ledger_entry WHERE account_id=?",
            (account,),
        ).fetchone()
        normal = _ACCOUNT_SPECS[code][1]
        return int(
            row["debit"] - row["credit"]
            if normal == NormalBalance.DEBIT
            else row["credit"] - row["debit"]
        )

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> Order:
        return Order(
            order_id=row["order_id"],
            account_id=row["account_id"],
            client_request_id=row["client_request_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            order_type=OrderType(row["order_type"]),
            qty=row["qty"],
            filled_qty=row["filled_qty"],
            limit_price_fen=row["limit_price_fen"],
            reserved_fen=row["reserved_fen"],
            reserved_qty=row["reserved_qty"],
            status=OrderStatus(row["status"]),
            submitted_at=datetime.fromisoformat(row["submitted_at"]),
            effective_rule_version=row["effective_rule_version"],
        )

    @staticmethod
    def _fill_from_row(row: sqlite3.Row) -> Fill:
        return Fill(
            fill_id=row["fill_id"],
            order_id=row["order_id"],
            qty=row["qty"],
            price_fen=row["price_fen"],
            commission_fen=row["commission_fen"],
            tax_fen=row["tax_fen"],
            transfer_fee_fen=row["transfer_fee_fen"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            replay_quality=ReplayQuality(row["replay_quality"]),
        )


def _round_fee(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _commission_for_gross(gross_fen: int, schedule: ReplayFeeSchedule) -> int:
    commission = _round_fee(Decimal(gross_fen) * schedule.commission_rate)
    if schedule.commission_rate > 0:
        commission = max(commission, schedule.minimum_commission_fen)
    return commission
