"""Git-ignored Markdown portfolio state for interactive Agent workflows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from astock.core.atomic import atomic_write_text
from astock.core.hashing import content_hash
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
from astock.schemas.portfolio_decision import ValidatedExternalTradeImport

_PORTFOLIO_SCHEMA = "astock-local-portfolio-v1"
_TRADES_SCHEMA = "astock-local-trades-v1"
_ORDERS_SCHEMA = "astock-local-orders-v1"
_ALLOWED_SIDES = {"BUY", "SELL"}
_ALLOWED_SOURCES = {"IMPORT", "PAPER_FILL"}
_ALLOWED_ACTIONS = {"HOLD", "ADD", "TRIM", "EXIT"}


class LocalPortfolioService:
    """Git-ignored compatibility projections; external account authority stays in SQLite."""

    def __init__(self, project_root: Path, state: StateStore | None = None) -> None:
        self.root = project_root.resolve() / "user_state"
        self.portfolio_path = self.root / "portfolio.md"
        self.orders_path = self.root / "orders.md"
        self.trades_path = self.root / "trades.md"
        self.state = state

    def initialize(self) -> dict[str, object]:
        self.root.mkdir(parents=True, exist_ok=True)
        created: list[str] = []
        if not self.trades_path.exists():
            self._write_trades([])
            created.append(str(self.trades_path))
        if not self.orders_path.exists():
            self._write_orders([])
            created.append(str(self.orders_path))
        if not self.portfolio_path.exists():
            self._write_portfolio([], settings=self._default_settings())
            created.append(str(self.portfolio_path))
        return {
            "status": "READY",
            "created": created,
            "portfolio_file": str(self.portfolio_path),
            "orders_file": str(self.orders_path),
            "trades_file": str(self.trades_path),
        }

    def status(self) -> dict[str, object]:
        self.initialize()
        portfolio = self._load_portfolio()
        orders = self._load_orders()
        trades = self._load_trades()
        replayed = self._replay(trades)
        stored = self._normalized_positions(portfolio.get("positions", []))
        stored_economic = [self._economic_projection(item) for item in stored]
        replayed_economic = [self._economic_projection(item) for item in replayed]
        return {
            "status": "READY" if stored_economic == replayed_economic else "NEEDS_REBUILD",
            "portfolio_file": str(self.portfolio_path),
            "orders_file": str(self.orders_path),
            "trades_file": str(self.trades_path),
            "settings": portfolio.get("settings", self._default_settings()),
            "positions": stored,
            "position_count": len(stored),
            "open_orders": orders,
            "open_order_count": len(orders),
            "trade_count": len(trades),
            "last_trade_at": trades[-1]["occurred_at"] if trades else None,
        }

    def trade_facts(self) -> list[dict[str, Any]]:
        """Return a copy of legacy local trade facts for one-time compatibility migration."""

        self.initialize()
        return [dict(item) for item in self._load_trades()]

    def record_trade(
        self,
        *,
        market: str,
        symbol: str,
        side: str,
        quantity: int,
        price_cny: Decimal | str | float,
        source: str,
        note: str = "",
        occurred_at: datetime | None = None,
    ) -> dict[str, object]:
        self.initialize()
        normalized_side = side.strip().upper()
        normalized_source = source.strip().upper()
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip()
        if normalized_side not in _ALLOWED_SIDES:
            raise ValueError(f"side must be one of {sorted(_ALLOWED_SIDES)}")
        if normalized_source not in _ALLOWED_SOURCES:
            raise ValueError(f"source must be one of {sorted(_ALLOWED_SOURCES)}")
        if not normalized_market or not normalized_symbol:
            raise ValueError("market and symbol are required")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        price = self._decimal(price_cny)
        if price <= 0:
            raise ValueError("price_cny must be positive")
        timestamp = (occurred_at or datetime.now(UTC)).astimezone(UTC)
        trades = self._load_trades()
        identity = {
            "occurred_at": timestamp.isoformat(),
            "market": normalized_market,
            "symbol": normalized_symbol,
            "side": normalized_side,
            "quantity": quantity,
            "price_cny": self._decimal_text(price),
            "source": normalized_source,
            "note": note.strip(),
            "ordinal": len(trades) + 1,
        }
        trade = {"trade_id": f"local-trade:{content_hash(identity)}", **identity}
        if any(item["trade_id"] == trade["trade_id"] for item in trades):
            raise ValueError("duplicate local trade")
        proposed = [*trades, trade]
        replayed = self._replay(proposed)
        current = self._load_portfolio()
        replayed = self._merge_review_state(replayed, current.get("positions", []))
        # Within this legacy mirror, trades.md replays portfolio.md; external authority is SQLite.
        self._write_trades(proposed)
        self._write_portfolio(replayed, settings=current.get("settings", self._default_settings()))
        return {"status": "RECORDED", "trade": trade, "positions": replayed}

    def inspect_validated_external_trade(
        self,
        trade: ValidatedExternalTradeImport,
    ) -> dict[str, object] | None:
        """Check legacy local facts without writing, for canonical-event preflight."""

        self.initialize()
        economic_key = self._external_trade_key(
            market=trade.market.value,
            symbol=trade.symbol,
            side=trade.side,
            quantity=trade.quantity,
            price_cny=trade.price_cny,
            occurred_at=trade.occurred_at,
        )
        for existing in self._load_trades():
            existing_key = self._external_trade_key(
                market=str(existing["market"]),
                symbol=str(existing["symbol"]),
                side=str(existing["side"]),
                quantity=int(existing["quantity"]),
                price_cny=existing["price_cny"],
                occurred_at=datetime.fromisoformat(str(existing["occurred_at"])),
            )
            if existing_key != economic_key:
                continue
            if str(existing["source"]).upper() == "PAPER_FILL":
                raise ValueError("external trade conflicts with an existing paper fill")
            return {
                "status": "DUPLICATE",
                "trade": existing,
                "positions": self.status()["positions"],
            }
        return None

    def record_validated_external_trade(
        self,
        trade: ValidatedExternalTradeImport,
    ) -> dict[str, object]:
        """Exactly-once compatibility projection of one validated external trade."""

        existing = self.inspect_validated_external_trade(trade)
        if existing is not None:
            return existing
        result = self.record_trade(
            market=trade.market.value,
            symbol=trade.symbol,
            side=trade.side,
            quantity=trade.quantity,
            price_cny=trade.price_cny,
            source="IMPORT",
            note=f"user-declared:{trade.capture_artifact_id}",
            occurred_at=trade.occurred_at,
        )
        return result

    @staticmethod
    def _external_trade_key(
        *,
        market: str,
        symbol: str,
        side: str,
        quantity: int,
        price_cny: object,
        occurred_at: datetime,
    ) -> tuple[str, str, str, int, str, str]:
        return (
            market.strip().upper(),
            symbol.strip(),
            side.strip().upper(),
            int(quantity),
            LocalPortfolioService._decimal_text(LocalPortfolioService._decimal(price_cny)),
            occurred_at.astimezone(UTC).isoformat(),
        )

    def record_review(
        self,
        *,
        market: str,
        symbol: str,
        action: str,
        thesis_status: str,
        note: str,
        reviewed_at: datetime | None = None,
    ) -> dict[str, object]:
        self.initialize()
        normalized_action = action.strip().upper()
        if normalized_action not in _ALLOWED_ACTIONS:
            raise ValueError(f"action must be one of {sorted(_ALLOWED_ACTIONS)}")
        portfolio = self._load_portfolio()
        positions = self._normalized_positions(portfolio.get("positions", []))
        key = (market.strip().upper(), symbol.strip())
        found = False
        timestamp = (reviewed_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        for position in positions:
            if (position["market"], position["symbol"]) != key:
                continue
            position["last_review_at"] = timestamp
            position["last_action"] = normalized_action
            position["thesis_status"] = thesis_status.strip() or "UNCHANGED"
            position["review_note"] = note.strip()
            found = True
            break
        if not found:
            raise ValueError("cannot review a symbol that is not currently held")
        self._write_portfolio(
            positions, settings=portfolio.get("settings", self._default_settings())
        )
        return {
            "status": "REVIEW_RECORDED",
            "market": key[0],
            "symbol": key[1],
            "action": normalized_action,
        }

    def audit(self) -> dict[str, object]:
        self.initialize()
        portfolio = self._load_portfolio()
        trades = self._load_trades()
        stored = self._normalized_positions(portfolio.get("positions", []))
        replayed = self._replay(trades)
        stored_economic = [self._economic_projection(item) for item in stored]
        replayed_economic = [self._economic_projection(item) for item in replayed]
        findings: list[str] = []
        if stored_economic != replayed_economic:
            findings.append("PORTFOLIO_DOES_NOT_RECONCILE_WITH_TRADES")
        return {
            "status": "PASS" if not findings else "FAIL",
            "finding_codes": findings,
            "position_count": len(stored),
            "trade_count": len(trades),
        }

    def rebuild(self) -> dict[str, object]:
        self.initialize()
        portfolio = self._load_portfolio()
        replayed = self._replay(self._load_trades())
        replayed = self._merge_review_state(replayed, portfolio.get("positions", []))
        self._write_portfolio(
            replayed, settings=portfolio.get("settings", self._default_settings())
        )
        return {"status": "REBUILT", "positions": replayed}

    def sync_from_paper(self, account_id: str | None = None) -> dict[str, object]:
        """Mirror confirmed simulated fills/positions into Git-ignored Markdown state."""
        if self.state is None:
            raise ValueError("paper sync requires StateStore")
        self.initialize()
        current = self._load_portfolio()
        with self.state.connect() as connection:
            accounts = [
                str(row[0])
                for row in connection.execute(
                    "SELECT account_id FROM paper_account ORDER BY account_id"
                ).fetchall()
            ]
            resolved_account_id = account_id
            if resolved_account_id is None:
                if "default" in accounts:
                    resolved_account_id = "default"
                elif len(accounts) == 1:
                    resolved_account_id = accounts[0]
                elif not accounts:
                    raise ValueError("paper account does not exist")
                else:
                    raise ValueError("multiple paper accounts exist; account_id is required")
            if resolved_account_id not in accounts:
                raise ValueError("paper account does not exist")
            account_id = resolved_account_id
            fill_rows = connection.execute(
                "SELECT f.fill_id,f.qty,f.price_fen,f.price_milli_yuan,f.occurred_at,"
                "o.side,o.symbol,b.market FROM fill f "
                "JOIN order_record o ON o.order_id=f.order_id "
                "LEFT JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
                "WHERE o.account_id=? ORDER BY f.occurred_at,f.fill_id",
                (account_id,),
            ).fetchall()
            if any(row[7] is None for row in fill_rows):
                raise ValueError("paper fill is missing a formal market binding")
            position_rows = connection.execute(
                "SELECT p.symbol,p.qty_total,p.avg_cost_fen,i.market,i.instrument_id,"
                "c.total_cost_fen FROM position p "
                "LEFT JOIN paper_position_identity i "
                "ON i.account_id=p.account_id AND i.symbol=p.symbol "
                "LEFT JOIN paper_position_cost c "
                "ON c.account_id=p.account_id AND c.symbol=p.symbol "
                "WHERE p.account_id=? AND p.qty_total>0 ORDER BY p.symbol",
                (account_id,),
            ).fetchall()
            order_rows = connection.execute(
                "SELECT o.order_id,o.submitted_at,o.symbol,o.side,o.qty,o.filled_qty,"
                "o.limit_price_fen,o.limit_price_milli_yuan,o.status,b.market "
                "FROM order_record o "
                "JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
                "WHERE o.account_id=? AND o.status IN ('ACCEPTED','PARTIALLY_FILLED') "
                "ORDER BY o.submitted_at,o.order_id",
                (account_id,),
            ).fetchall()
            markets_by_symbol: dict[str, set[str]] = {}
            for symbol, market in connection.execute(
                "SELECT o.symbol,b.market FROM order_record o "
                "JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
                "WHERE o.account_id=? ORDER BY o.submitted_at",
                (account_id,),
            ).fetchall():
                markets_by_symbol.setdefault(str(symbol), set()).add(str(market))

        orders: list[dict[str, Any]] = []
        for row in order_rows:
            limit_price_milli_yuan = int(row[7]) if row[7] is not None else int(row[6]) * 10
            orders.append(
                {
                    "order_id": str(row[0]),
                    "submitted_at": str(row[1]),
                    "symbol": str(row[2]),
                    "side": str(row[3]).upper(),
                    "quantity": int(row[4]),
                    "filled_quantity": int(row[5]),
                    "limit_price_cny": self._decimal_text(
                        Decimal(limit_price_milli_yuan) / 1000
                    ),
                    "status": str(row[8]),
                    "market": str(row[9]),
                }
            )
        trades: list[dict[str, Any]] = []
        times_by_identity: dict[tuple[str, str], list[str]] = {}
        for row in fill_rows:
            symbol = str(row[6])
            market = str(row[7])
            occurred_at = str(row[4])
            price_milli_yuan = int(row[3]) if row[3] is not None else int(row[2]) * 10
            times_by_identity.setdefault((market, symbol), []).append(occurred_at)
            trades.append(
                {
                    "trade_id": f"paper-fill:{row[0]}",
                    "occurred_at": occurred_at,
                    "market": market,
                    "symbol": symbol,
                    "side": str(row[5]).upper(),
                    "quantity": int(row[1]),
                    "price_cny": self._decimal_text(Decimal(price_milli_yuan) / 1000),
                    "source": "PAPER_FILL",
                    "note": "账户模拟成交回放",
                    "ordinal": len(trades) + 1,
                }
            )
        replayed_positions = {
            (str(position["market"]), str(position["symbol"])): position
            for position in self._replay(trades)
        }
        positions: list[dict[str, Any]] = []
        for row in position_rows:
            symbol = str(row[0])
            quantity = int(row[1])
            identity_market = str(row[3]) if row[3] is not None else None
            identity_instrument_id = str(row[4]) if row[4] is not None else None
            if identity_market is None:
                legacy_markets = markets_by_symbol.get(symbol, set())
                if len(legacy_markets) != 1:
                    raise ValueError(
                        f"open paper position lacks unambiguous market binding: {symbol}"
                    )
                market = next(iter(legacy_markets))
            else:
                market = identity_market
                if identity_instrument_id != f"{market}:{symbol}":
                    raise ValueError(
                        f"open paper position has invalid instrument identity: {symbol}"
                    )
            times = times_by_identity.get((market, symbol), [])
            replayed_position = replayed_positions.get((market, symbol))
            total_cost_fen = int(row[5]) if row[5] is not None else None
            if (
                replayed_position is not None
                and int(replayed_position["quantity"]) == quantity
            ):
                average_cost = Decimal(str(replayed_position["average_cost_cny"]))
            elif total_cost_fen is not None:
                average_cost = Decimal(total_cost_fen) / 100 / quantity
            else:
                average_cost = Decimal(int(row[2])) / 100
            positions.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "quantity": quantity,
                    "average_cost_cny": self._decimal_text(average_cost),
                    "opened_at": times[0] if times else datetime.now(UTC).isoformat(),
                    "last_trade_at": times[-1] if times else datetime.now(UTC).isoformat(),
                    "last_review_at": None,
                    "last_action": "HOLD",
                    "thesis_status": "UNREVIEWED",
                    "review_note": "",
                }
            )
        positions = self._merge_review_state(positions, current.get("positions", []))
        self._write_orders(orders)
        self._write_trades(trades)
        self._write_portfolio(positions, settings=current.get("settings", self._default_settings()))
        return {
            "status": "SYNCED_FROM_PAPER",
            "account_id": account_id,
            "open_order_count": len(orders),
            "fill_count": len(trades),
            "position_count": len(positions),
            "positions": positions,
        }

    def _load_portfolio(self) -> dict[str, Any]:
        payload = self._frontmatter(self.portfolio_path)
        if payload.get("schema_version") != _PORTFOLIO_SCHEMA:
            raise ValueError("unsupported local portfolio schema")
        if not isinstance(payload.get("positions"), list):
            raise ValueError("local portfolio positions must be a list")
        return payload

    def _load_orders(self) -> list[dict[str, Any]]:
        payload = self._frontmatter(self.orders_path)
        if payload.get("schema_version") != _ORDERS_SCHEMA:
            raise ValueError("unsupported local orders schema")
        values = payload.get("orders")
        if not isinstance(values, list):
            raise ValueError("local orders must be a list")
        result: list[dict[str, Any]] = []
        for raw in values:
            if not isinstance(raw, dict):
                raise ValueError("local order must be an object")
            result.append({str(key): value for key, value in raw.items()})
        return result

    def _load_trades(self) -> list[dict[str, Any]]:
        payload = self._frontmatter(self.trades_path)
        if payload.get("schema_version") != _TRADES_SCHEMA:
            raise ValueError("unsupported local trades schema")
        values = payload.get("trades")
        if not isinstance(values, list):
            raise ValueError("local trades must be a list")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in values:
            if not isinstance(raw, dict):
                raise ValueError("local trade must be an object")
            trade = {str(key): value for key, value in raw.items()}
            trade_id = str(trade.get("trade_id", ""))
            if not trade_id or trade_id in seen:
                raise ValueError("local trade ids must be non-empty and unique")
            seen.add(trade_id)
            side = str(trade.get("side", "")).upper()
            source = str(trade.get("source", "")).upper()
            if side not in _ALLOWED_SIDES or source not in _ALLOWED_SOURCES:
                raise ValueError("local trade side/source is invalid")
            trade["side"] = side
            trade["source"] = source
            trade["market"] = str(trade.get("market", "")).upper()
            trade["symbol"] = str(trade.get("symbol", ""))
            trade["quantity"] = int(trade.get("quantity", 0))
            trade["price_cny"] = self._decimal_text(self._decimal(trade.get("price_cny")))
            if trade["quantity"] <= 0:
                raise ValueError("local trade quantity must be positive")
            datetime.fromisoformat(str(trade.get("occurred_at")))
            result.append(trade)
        return result

    def _replay(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        state: dict[tuple[str, str], dict[str, Any]] = {}
        for trade in trades:
            key = (str(trade["market"]), str(trade["symbol"]))
            quantity = int(trade["quantity"])
            price = self._decimal(trade["price_cny"])
            current = state.get(key)
            if trade["side"] == "BUY":
                if current is None:
                    state[key] = {
                        "market": key[0],
                        "symbol": key[1],
                        "quantity": quantity,
                        "average_cost_cny": self._decimal_text(price),
                        "opened_at": str(trade["occurred_at"]),
                        "last_trade_at": str(trade["occurred_at"]),
                        "last_review_at": None,
                        "last_action": "HOLD",
                        "thesis_status": "UNREVIEWED",
                        "review_note": "",
                    }
                else:
                    old_quantity = int(current["quantity"])
                    old_cost = self._decimal(current["average_cost_cny"])
                    new_quantity = old_quantity + quantity
                    average = ((old_cost * old_quantity) + (price * quantity)) / new_quantity
                    current["quantity"] = new_quantity
                    current["average_cost_cny"] = self._decimal_text(average)
                    current["last_trade_at"] = str(trade["occurred_at"])
            else:
                if current is None or int(current["quantity"]) < quantity:
                    raise ValueError(f"sell exceeds current holding for {key[0]}:{key[1]}")
                current["quantity"] = int(current["quantity"]) - quantity
                current["last_trade_at"] = str(trade["occurred_at"])
                if current["quantity"] == 0:
                    del state[key]
        return [state[key] for key in sorted(state)]

    @staticmethod
    def _merge_review_state(replayed: list[dict[str, Any]], stored: object) -> list[dict[str, Any]]:
        if not isinstance(stored, list):
            return replayed
        old = {
            (str(item.get("market", "")).upper(), str(item.get("symbol", ""))): item
            for item in stored
            if isinstance(item, dict)
        }
        for position in replayed:
            previous = old.get((position["market"], position["symbol"]))
            if not isinstance(previous, dict):
                continue
            for key in ("last_review_at", "last_action", "thesis_status", "review_note"):
                if key in previous:
                    position[key] = previous[key]
        return replayed

    def _write_orders(self, orders: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = {"schema_version": _ORDERS_SCHEMA, "updated_at": now, "orders": orders}
        lines = [
            "# 本地未完成订单",
            "",
            "此文件仅保存在本机，不进入 Git。订单成交与否以模拟账户账本为准。",
            "",
        ]
        if orders:
            lines.extend(
                [
                    "| 时间 | 市场 | 标的 | 方向 | 数量 | 已成交 | 限价(CNY) | 状态 |",
                    "|---|---|---|---|---:|---:|---:|---|",
                ]
            )
            for item in orders:
                lines.append(
                    f"| {item['submitted_at']} | {item['market']} | "
                    f"{item['symbol']} | {item['side']} | {item['quantity']} | "
                    f"{item['filled_quantity']} | {item['limit_price_cny']} | "
                    f"{item['status']} |"
                )
        else:
            lines.append("当前无未完成订单。")
        atomic_write_text(self.orders_path, self._render(payload, lines))

    def _write_trades(self, trades: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = {"schema_version": _TRADES_SCHEMA, "updated_at": now, "trades": trades}
        lines = [
            "# 本地交易记录",
            "",
            "此文件仅保存在本机，不进入 Git。`trades` front matter 是事实源。",
            "",
        ]
        if trades:
            lines.extend(
                [
                    "| 时间 | 市场 | 标的 | 方向 | 数量 | 价格(CNY) | 来源 | 备注 |",
                    "|---|---|---|---:|---:|---:|---|---|",
                ]
            )
            for item in trades:
                note = str(item.get("note", "")).replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {item['occurred_at']} | {item['market']} | "
                    f"{item['symbol']} | {item['side']} | {item['quantity']} | "
                    f"{item['price_cny']} | {item['source']} | {note} |"
                )
        else:
            lines.append("暂无交易记录。")
        atomic_write_text(self.trades_path, self._render(payload, lines))

    def _write_portfolio(self, positions: list[dict[str, Any]], *, settings: object) -> None:
        now = datetime.now(UTC).isoformat()
        resolved_settings = settings if isinstance(settings, dict) else self._default_settings()
        payload = {
            "schema_version": _PORTFOLIO_SCHEMA,
            "updated_at": now,
            "settings": resolved_settings,
            "positions": self._normalized_positions(positions),
        }
        lines = [
            "# 本地持仓",
            "",
            "此文件仅保存在本机，不进入 Git。持仓由 `trades.md` 确定性回放得到。",
            "",
        ]
        if positions:
            lines.extend(
                [
                    "| 市场 | 标的 | 数量 | 平均成本(CNY) | 最近动作 | 逻辑状态 | 最近复核 |",
                    "|---|---|---:|---:|---|---|---|",
                ]
            )
            for item in self._normalized_positions(positions):
                lines.append(
                    f"| {item['market']} | {item['symbol']} | {item['quantity']} | "
                    f"{item['average_cost_cny']} | {item.get('last_action', 'HOLD')} | "
                    f"{item.get('thesis_status', 'UNREVIEWED')} | "
                    f"{item.get('last_review_at') or '-'} |"
                )
        else:
            lines.append("当前无持仓。")
        atomic_write_text(self.portfolio_path, self._render(payload, lines))

    @staticmethod
    def _default_settings() -> dict[str, object]:
        return {
            "manual_trade_overrides_research_opinion": True,
            "unsized_manual_buy": "MINIMUM_VALID_BOARD_LOT",
            "auto_ai_paper_order_on_approved_entry": True,
            "paper_order_requires_account_confirmation": True,
            "auto_review_on_investor_task": True,
        }

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"missing YAML front matter: {path}")
        try:
            end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration as exc:
            raise ValueError(f"unterminated YAML front matter: {path}") from exc
        payload = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(payload, dict):
            raise ValueError(f"invalid YAML front matter: {path}")
        return {str(key): value for key, value in payload.items()}

    @staticmethod
    def _render(payload: dict[str, Any], body_lines: list[str]) -> str:
        front = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()
        return f"---\n{front}\n---\n\n" + "\n".join(body_lines).rstrip() + "\n"

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal value: {value}") from exc

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        normalized = value.quantize(Decimal("0.0001"))
        return format(normalized, "f")

    @staticmethod
    def _economic_projection(position: dict[str, Any]) -> dict[str, object]:
        return {
            "market": position["market"],
            "symbol": position["symbol"],
            "quantity": int(position["quantity"]),
            "average_cost_cny": str(position["average_cost_cny"]),
            "opened_at": str(position["opened_at"]),
            "last_trade_at": str(position["last_trade_at"]),
        }

    @staticmethod
    def _normalized_positions(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("positions must be a list")
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError("position must be an object")
            item = {str(key): child for key, child in raw.items()}
            item["market"] = str(item.get("market", "")).upper()
            item["symbol"] = str(item.get("symbol", ""))
            item["quantity"] = int(item.get("quantity", 0))
            item["average_cost_cny"] = LocalPortfolioService._decimal_text(
                LocalPortfolioService._decimal(item.get("average_cost_cny"))
            )
            if not item["market"] or not item["symbol"] or item["quantity"] <= 0:
                raise ValueError("invalid open position")
            key = (item["market"], item["symbol"])
            if key in seen:
                raise ValueError("duplicate open position")
            seen.add(key)
            result.append(item)
        return sorted(result, key=lambda item: (item["market"], item["symbol"]))


def register_local_portfolio_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, StateStore, Any]],
) -> None:
    def service() -> LocalPortfolioService:
        paths, state, _ = services()
        return LocalPortfolioService(paths.root, state)

    @app.command("local-portfolio-init")
    def local_portfolio_init() -> None:
        typer.echo(yaml.safe_dump(service().initialize(), allow_unicode=True, sort_keys=False))

    @app.command("local-portfolio-status")
    def local_portfolio_status() -> None:
        typer.echo(yaml.safe_dump(service().status(), allow_unicode=True, sort_keys=False))

    @app.command("local-portfolio-sync-paper")
    def local_portfolio_sync_paper(
        account_id: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        typer.echo(
            yaml.safe_dump(
                service().sync_from_paper(account_id),
                allow_unicode=True,
                sort_keys=False,
            )
        )

    @app.command("local-portfolio-import-trade")
    def local_portfolio_import_trade(
        side: Annotated[str, typer.Argument()],
        market: Annotated[str, typer.Argument()],
        symbol: Annotated[str, typer.Argument()],
        quantity: Annotated[int, typer.Argument(min=1)],
        price_cny: Annotated[str, typer.Argument()],
        note: Annotated[str, typer.Option()] = "",
        occurred_at: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        timestamp = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise typer.BadParameter("occurred_at must include a timezone")
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip()
        normalized_side = side.strip().upper()
        price = LocalPortfolioService._decimal(price_cny)
        identity = {
            "market": normalized_market,
            "symbol": normalized_symbol,
            "side": normalized_side,
            "quantity": quantity,
            "price_cny": LocalPortfolioService._decimal_text(price),
            "occurred_at": timestamp.astimezone(UTC).isoformat(),
        }
        identity_hash = content_hash(identity)
        paths, state, objects = services()
        local = LocalPortfolioService(paths.root, state)
        repository = ExternalAccountRepository(state, objects)
        repository.create_account(
            account_id="default",
            display_name="默认外部账户",
            created_at=timestamp,
        )
        draft = ExternalAccountEventDraft.model_validate(
            {
                "account_id": "default",
                "event_type": ExternalAccountEventType.TRADE.value,
                "occurred_at": timestamp,
                "available_to_system_at": timestamp,
                "market": normalized_market,
                "symbol": normalized_symbol,
                "side": normalized_side,
                "quantity": quantity,
                "price_cny": price,
                "idempotency_key": f"local-portfolio-cli:{identity_hash}",
                "note": note.strip(),
                "created_at": timestamp,
            }
        )
        inserted, duplicates = repository.append_drafts([draft])
        validated = ValidatedExternalTradeImport.model_validate(
            {
                "capture_artifact_id": f"local-portfolio-cli:{identity_hash}",
                "instrument_id": f"{normalized_market}:{normalized_symbol}",
                "market": normalized_market,
                "symbol": normalized_symbol,
                "side": normalized_side,
                "quantity": quantity,
                "price_cny": price,
                "occurred_at": timestamp,
                "raw_statement": note.strip() or "local-portfolio-import-trade",
                "created_at": timestamp,
            }
        )
        reason_codes: list[str] = []
        try:
            compatibility = local.record_validated_external_trade(validated)
        except (OSError, ValueError):
            compatibility = {"status": "PROJECTION_FAILED"}
            reason_codes.append("LEGACY_LOCAL_PROJECTION_REFRESH_FAILED")
        try:
            projection = repository.write_markdown_projection(paths.root, as_of=timestamp)
        except (OSError, ValueError):
            projection = {"status": "PROJECTION_FAILED"}
            reason_codes.append("EXTERNAL_ACCOUNT_MARKDOWN_PROJECTION_REFRESH_FAILED")
        event_ids = inserted or duplicates
        typer.echo(
            yaml.safe_dump(
                {
                    "status": "RECORDED" if inserted else "DUPLICATE",
                    "event_id": event_ids[0],
                    "reason_codes": sorted(set(reason_codes)),
                    "compatibility_projection": compatibility,
                    "external_projection": projection,
                    "paper_ledger_write_allowed": False,
                    "broker_execution_allowed": False,
                },
                allow_unicode=True,
                sort_keys=False,
            )
        )

    @app.command("local-portfolio-review")
    def local_portfolio_review(
        market: Annotated[str, typer.Argument()],
        symbol: Annotated[str, typer.Argument()],
        action: Annotated[str, typer.Option()] = "HOLD",
        thesis_status: Annotated[str, typer.Option()] = "UNCHANGED",
        note: Annotated[str, typer.Option()] = "",
    ) -> None:
        typer.echo(
            yaml.safe_dump(
                service().record_review(
                    market=market,
                    symbol=symbol,
                    action=action,
                    thesis_status=thesis_status,
                    note=note,
                ),
                allow_unicode=True,
                sort_keys=False,
            )
        )

    @app.command("local-portfolio-audit")
    def local_portfolio_audit() -> None:
        result = service().audit()
        typer.echo(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
        if result["status"] != "PASS":
            raise typer.Exit(code=3)

    @app.command("local-portfolio-rebuild")
    def local_portfolio_rebuild() -> None:
        typer.echo(yaml.safe_dump(service().rebuild(), allow_unicode=True, sort_keys=False))


__all__ = ["LocalPortfolioService", "register_local_portfolio_commands"]
