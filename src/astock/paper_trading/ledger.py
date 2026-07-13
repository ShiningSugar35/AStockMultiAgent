"""Append-only double-entry paper account with idempotent order and fill services."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from astock.core.errors import FailureClass, PolicyError
from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas import (
    AccountType,
    CorporateActionEvent,
    Fill,
    NormalBalance,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioNAV,
    ReplayCheckpoint,
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


_ACCOUNT_SPECS: dict[str, tuple[AccountType, NormalBalance]] = {
    "CASH": (AccountType.ASSET, NormalBalance.DEBIT),
    "FROZEN_CASH": (AccountType.ASSET, NormalBalance.DEBIT),
    "SECURITIES": (AccountType.ASSET, NormalBalance.DEBIT),
    "FEES": (AccountType.EXPENSE, NormalBalance.DEBIT),
    "CAPITAL": (AccountType.EQUITY, NormalBalance.CREDIT),
    "REALIZED_PNL": (AccountType.INCOME, NormalBalance.CREDIT),
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class LedgerService:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def initialize_account(self, account_id: str, initial_cash_fen: int) -> PostResult:
        if initial_cash_fen < 0:
            raise ValueError("initial_cash_fen cannot be negative")
        idempotency_key = f"account-init:{account_id}"
        with self.state.transaction() as connection:
            existing = self._existing_event(connection, idempotency_key)
            if existing is not None:
                return PostResult(existing["event_id"], existing["seq"], False)
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
            lines = []
            if initial_cash_fen:
                lines = [
                    LedgerLine("CASH", debit_fen=initial_cash_fen),
                    LedgerLine("CAPITAL", credit_fen=initial_cash_fen),
                ]
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
        with self.state.transaction() as connection:
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
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM order_record WHERE account_id=? AND client_request_id=?",
                (account_id, client_request_id),
            ).fetchone()
            if existing is not None:
                return self._order_from_row(existing)
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

    def open_orders(self, account_id: str, symbol: str | None = None) -> list[Order]:
        query = "SELECT * FROM order_record WHERE account_id=? AND status IN (?,?)"
        parameters: list[object] = [
            account_id,
            OrderStatus.ACCEPTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        ]
        if symbol is not None:
            query += " AND symbol=?"
            parameters.append(symbol)
        query += " ORDER BY submitted_at,order_id"
        with closing(self.state.connect()) as connection:
            self._require_account(connection, account_id)
            rows = connection.execute(query, parameters).fetchall()
        return [self._order_from_row(row) for row in rows]

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
        with self.state.transaction() as connection:
            existing_fill = connection.execute(
                "SELECT * FROM fill WHERE fill_id=?", (fill_id,)
            ).fetchone()
            if existing_fill is not None:
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
                old_cost = int(position["avg_cost_fen"]) if position else 0
                new_qty = old_qty + qty
                new_avg = ((old_qty * old_cost) + gross) // new_qty
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
                realized_net = gross - cost_basis - fees
                connection.execute(
                    "UPDATE position SET qty_total=?,avg_cost_fen=?,realized_pnl_fen="
                    "realized_pnl_fen+? WHERE account_id=? AND symbol=?",
                    (
                        new_qty,
                        int(position["avg_cost_fen"]) if new_qty else 0,
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
            if order["side"] == OrderSide.BUY.value:
                trade_date = occurred_at.astimezone(_SHANGHAI).date()
                connection.execute(
                    "INSERT INTO position_settlement(settlement_id,account_id,symbol,qty,"
                    "trade_date,eligible_on,source_event_id,status) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        order["account_id"],
                        order["symbol"],
                        qty,
                        trade_date.isoformat(),
                        (trade_date + timedelta(days=1)).isoformat(),
                        post.event_id,
                        "PENDING_CALENDAR_CONFIRMATION",
                    ),
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
        with self.state.transaction() as connection:
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
        with self.state.transaction() as connection:
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

    def replay_checkpoint(self, account_id: str, symbol: str) -> ReplayCheckpoint | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM replay_checkpoint WHERE account_id=? AND symbol=?",
                (account_id, symbol),
            ).fetchone()
        if row is None:
            return None
        return ReplayCheckpoint(
            account_id=row["account_id"],
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
        with self.state.transaction() as connection:
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

    def register_corporate_action(self, event: CorporateActionEvent) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO corporate_action_event(event_id,symbol,event_type,ex_date,"
                "payload_json,source_id,rule_version) VALUES(?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.symbol,
                    event.event_type.value,
                    event.ex_date,
                    canonical_json_bytes(event.model_dump(mode="json")).decode("utf-8"),
                    event.source_id,
                    event.rule_version,
                ),
            )

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
        self, account_id: str, market_prices_fen: dict[str, int] | None = None
    ) -> PortfolioNAV:
        market_prices_fen = market_prices_fen or {}
        status = self.status(account_id)
        market_value = 0
        data_quality = "MARK_TO_COST"
        for position in status["positions"]:
            symbol = str(position["symbol"])
            price = market_prices_fen.get(symbol)
            if price is None:
                price = int(position["avg_cost_fen"])
            else:
                data_quality = "MARKET_PRICE_INPUT"
            market_value += int(position["qty_total"]) * int(price)
        cash = int(status["balances_fen"]["CASH"])
        frozen = int(status["balances_fen"]["FROZEN_CASH"])
        return PortfolioNAV(
            account_id=account_id,
            as_of=datetime.now(UTC),
            cash_fen=cash,
            frozen_cash_fen=frozen,
            market_value_fen=market_value,
            nav_fen=cash + frozen + market_value,
            data_quality=data_quality,
        )

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
            "SELECT event_id,seq FROM journal WHERE idempotency_key=?", (idempotency_key,)
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
