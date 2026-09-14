"""Read-only trade episodes for private documents, derived from canonical events/fills.

This view never places orders or changes cost accounting. Returns are transaction
cash-flow returns, with distributions excluded explicitly. Transfers or incomplete
histories retain unknown performance rather than inventing a sale or cost basis.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from astock.core.hashing import content_hash
from astock.external_accounts import replay_external_account_events
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.ledger import _gross_fen
from astock.schemas.external_accounts import ExternalAccountEvent, ExternalAccountEventType


@dataclass(frozen=True)
class DocumentTrade:
    event_id: str
    key: str
    occurred_at: datetime
    sequence: int
    quantity: int
    action: str
    price: Decimal | None
    gross: Decimal | None
    fees: Decimal | None


def trade_episodes(trades: list[DocumentTrade]) -> dict[str, dict[str, Any]]:
    """O(n log n) bounded projection with stable identities across closed/reopened trades."""
    grouped: dict[str, list[DocumentTrade]] = defaultdict(list)
    for item in trades:
        if item.occurred_at.tzinfo is None or item.occurred_at.utcoffset() is None:
            raise ValueError("trade history requires an aware event time")
        if item.quantity <= 0 or item.action not in {"BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT"}:
            raise ValueError("trade history event has invalid quantity/action")
        for value in (item.price, item.gross, item.fees):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("trade history money must be finite and nonnegative")
        grouped[item.key].append(item)
    result: dict[str, dict[str, Any]] = {}
    for key, rows in grouped.items():
        ordered = sorted(rows, key=lambda item: (item.occurred_at, item.sequence, item.event_id))
        if len({item.event_id for item in ordered}) != len(ordered):
            raise ValueError("duplicate trade history event")
        episodes: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        quantity = 0
        buys = sells = fees = buy_fees = Decimal("0")
        known_fees = True
        trade_only = True
        complete = True
        for item in ordered:
            incoming = item.action in {"BUY", "TRANSFER_IN"}
            if quantity == 0:
                if not incoming:
                    complete = False
                    break
                current = {
                    "episode_id": content_hash({"key": key, "entry_event_id": item.event_id}),
                    "key": key,
                    "opened_at": item.occurred_at.isoformat(),
                    "closed_at": None,
                    "entry_price": str(item.price) if item.price is not None else None,
                    "entry_time_kind": "TRADE" if item.action == "BUY" else "TRANSFER",
                    "event_ids": [],
                }
                buys = sells = fees = buy_fees = Decimal("0")
                known_fees = True
                trade_only = True
            assert current is not None
            if not incoming and item.quantity > quantity:
                # E.g. an unprojected stock distribution. Do not force the fill path
                # to look like an ordinary complete buy/sell cycle.
                complete = False
                break
            quantity += item.quantity if incoming else -item.quantity
            current["event_ids"].append(item.event_id)
            if item.action not in {"BUY", "SELL"} or item.gross is None:
                trade_only = False
            elif item.action == "BUY":
                buys += item.gross
            else:
                sells += item.gross
            if item.fees is None:
                known_fees = False
            else:
                fees += item.fees
                if item.action == "BUY":
                    buy_fees += item.fees
            current["quantity"] = quantity
            current["last_event_at"] = item.occurred_at.isoformat()
            if quantity == 0:
                current["closed_at"] = item.occurred_at.isoformat()
                gross_pnl = sells - buys if trade_only and buys > 0 else None
                net_pnl = gross_pnl - fees if gross_pnl is not None and known_fees else None
                current.update(
                    {
                        "buy_amount": str(buys) if trade_only else None,
                        "sell_amount": str(sells) if trade_only else None,
                        "fees": str(fees) if known_fees else None,
                        "gross_pnl": str(gross_pnl) if gross_pnl is not None else None,
                        "net_pnl": str(net_pnl) if net_pnl is not None else None,
                        "gross_return": str(gross_pnl / buys) if gross_pnl is not None else None,
                        "net_return": str(net_pnl / (buys + buy_fees))
                        if net_pnl is not None
                        else None,
                        "return_basis": "交易现金流收益，未含分红及其他非交易现金流",
                        "performance_known": net_pnl is not None,
                    }
                )
                episodes.append(current)
                current = None
        result[key] = {
            "complete": complete,
            "active": current if complete else None,
            "closed": episodes,
            "quantity": quantity if complete else None,
            "source_revision": content_hash([item.event_id for item in ordered]),
        }
    return result


def _active_external_events(events: list[ExternalAccountEvent]) -> list[ExternalAccountEvent]:
    """Use the canonical correction semantics; canonical replay cross-checks resulting holdings."""
    ordered = sorted(
        events,
        key=lambda item: (
            item.occurred_at,
            item.sequence_no,
            item.available_to_system_at,
            item.event_id,
        ),
    )
    correction = {
        target: item.event_id
        for item in ordered
        for target in (item.reverses_event_id, item.replaces_event_id)
        if target is not None
    }
    effective: dict[str, bool] = {}
    for item in ordered:
        chain: list[str] = []
        visiting: set[str] = set()
        cursor = item.event_id
        while cursor not in effective:
            if cursor in visiting:
                raise ValueError("cyclic external correction history")
            visiting.add(cursor)
            chain.append(cursor)
            successor = correction.get(cursor)
            if successor is None:
                effective[cursor] = True
                break
            cursor = successor
        for event_id in reversed(chain):
            successor = correction.get(event_id)
            effective[event_id] = True if successor is None else not effective[successor]
    return [
        item
        for item in ordered
        if item.event_type is not ExternalAccountEventType.REVERSAL and effective[item.event_id]
    ]


def position_trade_history(
    store: InvestorOrchestrationStore,
    *,
    as_of: datetime,
    max_events: int = 10000,
) -> dict[str, dict[str, Any]]:
    """Load existing canonical rows once; an over-budget lane is unavailable, never empty-proof."""
    if as_of.tzinfo is None or as_of.utcoffset() is None or max_events < 1:
        raise ValueError("invalid trade history read boundary")
    cutoff = as_of.astimezone(UTC)
    with store.connect() as connection:
        connection.execute("BEGIN")
        external_rows = connection.execute(
            "SELECT payload_json FROM external_account_event LIMIT ?",
            (max_events + 1,),
        ).fetchall()
        paper_rows = connection.execute(
            "SELECT f.*,o.account_id,o.symbol,o.side,COALESCE(b.market,i.market) AS market,"
            "j.seq AS event_sequence FROM fill f JOIN order_record o ON o.order_id=f.order_id "
            "LEFT JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
            "LEFT JOIN paper_position_identity i "
            "ON i.account_id=o.account_id AND i.symbol=o.symbol "
            "LEFT JOIN journal j ON j.idempotency_key=('fill:' || f.fill_id) LIMIT ?",
            (max_events + 1,),
        ).fetchall()
        paper_quantities = {
            (row["account_id"], row["symbol"]): int(row["qty_total"])
            for row in connection.execute("SELECT account_id,symbol,qty_total FROM position")
        }
    trades: list[DocumentTrade] = []
    expected_quantities: dict[str, int] = {}
    if len(external_rows) <= max_events:
        by_account: dict[str, list[ExternalAccountEvent]] = defaultdict(list)
        for row in external_rows:
            event = ExternalAccountEvent.model_validate_json(row["payload_json"])
            if event.available_to_system_at <= cutoff:
                by_account[event.account_id].append(event)
        for account_id, events in by_account.items():
            canonical = replay_external_account_events(account_id, events, as_of=cutoff)
            for position in canonical.positions:
                key = f"ACTUAL:{account_id}:{position.market.value}:{position.symbol}"
                expected_quantities[key] = position.quantity
            for sequence, event in enumerate(_active_external_events(events)):
                if event.market is None or event.symbol is None or event.quantity is None:
                    continue
                action = (
                    event.side
                    if event.event_type is ExternalAccountEventType.TRADE
                    else {
                        ExternalAccountEventType.SECURITY_TRANSFER_IN: "TRANSFER_IN",
                        ExternalAccountEventType.SECURITY_TRANSFER_OUT: "TRANSFER_OUT",
                    }.get(event.event_type)
                )
                if action is None:
                    continue
                trades.append(
                    DocumentTrade(
                        event.event_id,
                        f"ACTUAL:{account_id}:{event.market.value}:{event.symbol}",
                        event.occurred_at,
                        sequence,
                        event.quantity,
                        action,
                        event.price_cny,
                        event.price_cny * event.quantity if event.price_cny is not None else None,
                        None,
                    )
                )
    if len(paper_rows) <= max_events:
        for row in paper_rows:
            at = datetime.fromisoformat(row["occurred_at"])
            if at > cutoff or row["market"] is None or row["event_sequence"] is None:
                continue
            key = f"PAPER:{row['account_id']}:{row['market']}:{row['symbol']}"
            expected_quantities[key] = paper_quantities.get((row["account_id"], row["symbol"]), 0)
            milli = row["price_milli_yuan"]
            price = Decimal(milli) / 1000 if milli is not None else Decimal(row["price_fen"]) / 100
            gross = Decimal(_gross_fen(row["qty"], row["price_fen"], milli)) / 100
            fee = Decimal(row["commission_fen"] + row["tax_fen"] + row["transfer_fee_fen"]) / 100
            trades.append(
                DocumentTrade(
                    row["fill_id"],
                    f"PAPER:{row['account_id']}:{row['market']}:{row['symbol']}",
                    at,
                    row["event_sequence"],
                    row["qty"],
                    row["side"],
                    price,
                    gross,
                    fee,
                )
            )
    histories = trade_episodes(trades)
    for key, history in histories.items():
        if history["quantity"] != expected_quantities.get(key, 0):
            history["complete"] = False
            history["active"] = None
    return histories


def apply_trade_history(
    active: dict[str, Any],
    previous: dict[str, Any],
    histories: dict[str, dict[str, Any]],
    *,
    as_of: datetime,
    checked_accounts: set[tuple[str, str]],
    entry_snapshot: Callable[[str, str | None, datetime, str], dict[str, object]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enrich a document generation without changing canonical position membership."""
    result = deepcopy(active)
    closed = deepcopy(previous.get("closed", {}))
    prior_active = previous.get("active", {})
    for key, item in result.items():
        for field in (
            "trade_episode_id",
            "entry_trade_time",
            "entry_trade_price",
            "entry_time_kind",
        ):
            if field in prior_active.get(key, {}):
                item.setdefault(field, prior_active[key][field])
    for key, history in histories.items():
        if not history["complete"] or not any(
            key.startswith(f"{lane}:{account}:") for lane, account in checked_accounts
        ):
            continue
        current = result.get(key)
        episode = history["active"]
        prior = prior_active.get(key, {})
        if current is not None and current.get("coverage") == "CHECKED" and episode is not None:
            if Decimal(current["quantity"]) != Decimal(history["quantity"]):
                continue
            prior_id = prior.get("trade_episode_id")
            newly_opened = prior_id is not None and prior_id != episode["episode_id"]
            if prior_id is None and isinstance(prior.get("first_seen_at"), str):
                newly_opened = datetime.fromisoformat(
                    episode["opened_at"]
                ) > datetime.fromisoformat(prior["first_seen_at"])
            newly_verified = prior_id is None
            if newly_opened:
                current["first_seen_at"] = as_of.isoformat()
            if newly_opened or newly_verified:
                opened_at = datetime.fromisoformat(episode["opened_at"])
                entry_kind = str(episode["entry_time_kind"])
                entry_price = episode["entry_price"] if entry_kind == "TRADE" else None
                current["entry_snapshot"] = entry_snapshot(
                    current["instrument_id"], entry_price, opened_at, entry_kind
                )
                current["entry_price"] = current.get("average_cost")
            current["trade_episode_id"] = episode["episode_id"]
            current["entry_trade_time"] = episode["opened_at"]
            current["entry_trade_price"] = episode["entry_price"]
            current["entry_time_kind"] = episode["entry_time_kind"]
        confirmed_ids = {item["episode_id"] for item in history["closed"]}
        for old in closed.values():
            if (
                old.get("trade_history_key") == key
                and old.get("trade_episode_id") not in confirmed_ids
            ):
                old["superseded_by_correction"] = True
                old["realized_return"] = "成交记录已更正，原清仓收益不再作为有效结果"
        for finished in history["closed"]:
            archive_id = key + "@" + finished["episode_id"]
            if (
                archive_id in closed
                and closed[archive_id].get("trade_history") == finished
                and not closed[archive_id].get("superseded_by_correction")
            ):
                continue
            lane, account = next(
                (lane, account)
                for lane, account in checked_accounts
                if key.startswith(f"{lane}:{account}:")
            )
            instrument = key.removeprefix(f"{lane}:{account}:")
            original = (
                prior if prior.get("trade_episode_id") in {None, finished["episode_id"]} else {}
            )
            net = finished["net_pnl"]
            gross = finished["gross_pnl"]
            if net is not None:
                percent = Decimal(finished["net_return"]) * 100
                performance = f"扣除交易费用后盈亏 {net} 元，交易现金流收益率 {percent:.2f}%"
            elif gross is not None:
                percent = Decimal(finished["gross_return"]) * 100
                performance = f"扣费前盈亏 {gross} 元，收益率 {percent:.2f}%；费用未齐，净收益未知"
            else:
                performance = "包含非交易转移或资料未齐，收益暂不估算"
            closed[archive_id] = {
                **original,
                "lane": lane,
                "account_id": account,
                "instrument_id": instrument,
                "closed_at": as_of.isoformat(),
                "exit_trade_time": finished["closed_at"],
                "entry_trade_time": finished["opened_at"],
                "entry_trade_price": finished["entry_price"],
                "entry_time_kind": finished["entry_time_kind"],
                "realized_return": performance + "；" + finished["return_basis"],
                "trade_history": finished,
                "trade_history_key": key,
                "trade_episode_id": finished["episode_id"],
                "superseded_by_correction": False,
            }
    for key, item in prior_active.items():
        if key not in result and not any(
            row.get("trade_history_key") == key and not row.get("superseded_by_correction")
            for row in closed.values()
        ):
            archive_id = key + "@" + str(item.get("first_seen_at", "unknown"))
            closed.setdefault(
                archive_id,
                {
                    **item,
                    "closed_at": as_of.isoformat(),
                    "exit_trade_time": "尚缺可核实的成交记录",
                    "realized_return": "尚缺可核实的成交及费用记录，暂不估算收益",
                },
            )
    return result, closed
