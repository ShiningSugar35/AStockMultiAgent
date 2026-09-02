"""Independent, fail-closed ETF paper-execution policy.

ETF paper rules are intentionally not inferred from A-share stock defaults. Only
an exact instrument rule frozen in this policy can be admitted to the ETF order
and replay paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from astock.schemas import Market, ReplayFeeSchedule
from astock.schemas.portfolio_decision import SettlementCycle


@dataclass(frozen=True, slots=True)
class ETFInstrumentExecutionRule:
    instrument_id: str
    market: Market
    symbol: str
    effective_from: date
    price_limit_rate_bps: int
    buy_lot_size: int
    sell_lot_size: int
    allow_odd_lot_full_exit: bool
    tick_size_milli_yuan: int
    settlement_cycle: SettlementCycle
    source_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ETFExecutionPolicy:
    schema_version: str
    rule_version: str
    execution_enabled: bool
    instrument_rules: tuple[ETFInstrumentExecutionRule, ...]
    fee_schedule: ReplayFeeSchedule

    def rule_for(
        self,
        instrument_id: str,
        *,
        market: Market,
        symbol: str,
        trade_date: date,
    ) -> ETFInstrumentExecutionRule:
        candidates = [
            rule
            for rule in self.instrument_rules
            if rule.instrument_id == instrument_id and rule.effective_from <= trade_date
        ]
        if not candidates:
            raise ValueError(
                f"ETF execution policy has no effective exact-instrument rule for {instrument_id}"
            )
        rule = max(candidates, key=lambda item: item.effective_from)
        if rule.market is not market or rule.symbol != symbol:
            raise ValueError("ETF execution policy instrument identity mismatch")
        return rule

    def as_frozen_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rule_version": self.rule_version,
            "execution_enabled": self.execution_enabled,
            "instrument_rules": [
                {
                    "instrument_id": rule.instrument_id,
                    "market": rule.market.value,
                    "symbol": rule.symbol,
                    "effective_from": rule.effective_from.isoformat(),
                    "price_limit_rate_bps": rule.price_limit_rate_bps,
                    "buy_lot_size": rule.buy_lot_size,
                    "sell_lot_size": rule.sell_lot_size,
                    "allow_odd_lot_full_exit": rule.allow_odd_lot_full_exit,
                    "tick_size_milli_yuan": rule.tick_size_milli_yuan,
                    "settlement_cycle": rule.settlement_cycle.value,
                    "source_urls": list(rule.source_urls),
                }
                for rule in self.instrument_rules
            ],
            "fee_schedule": self.fee_schedule.model_dump(mode="json", exclude={"created_at"}),
        }


def load_etf_execution_policy(path: Path) -> ETFExecutionPolicy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("schema_version") != "etf-paper-trading-rules-v1":
        raise ValueError(f"Unsupported ETF paper trading rules: {path}")
    fee = raw.get("fee_schedule") or {}
    rule_version = str(raw["rule_version"])
    fee_schedule = ReplayFeeSchedule.model_validate(
        {
            **fee,
            "rule_version": str(fee["rule_version"]),
            "applicable_markets": [Market(str(item)) for item in fee.get("applicable_markets", [])],
            "source_urls": sorted({str(item) for item in fee.get("source_urls", [])}),
        }
    )
    if fee_schedule.rule_version != rule_version:
        raise ValueError("ETF policy and fee schedule rule versions must match")
    if not fee_schedule.source_urls:
        raise ValueError("ETF fee schedule requires frozen source URLs")
    if fee_schedule.stamp_tax_sell_rate != 0 or fee_schedule.transfer_fee_rate != 0:
        raise ValueError("ETF policy must not silently inherit stock stamp/transfer taxes")
    if not fee_schedule.requires_broker_confirmation:
        raise ValueError("ETF paper fees must remain explicitly broker-confirmed")

    rules: list[ETFInstrumentExecutionRule] = []
    seen: set[tuple[str, date]] = set()
    for item in raw.get("instruments", []):
        market = Market(str(item["market"]))
        symbol = str(item["symbol"])
        instrument_id = str(item["instrument_id"])
        if market is Market.INDEX or instrument_id != f"{market.value}:{symbol}":
            raise ValueError("ETF execution rule requires exact exchange market:symbol identity")
        effective_from = date.fromisoformat(str(item["effective_from"]))
        identity = (instrument_id, effective_from)
        if identity in seen:
            raise ValueError("duplicate ETF instrument/effective-date rule")
        seen.add(identity)
        source_urls = tuple(sorted({str(url) for url in item.get("source_urls", [])}))
        if not source_urls:
            raise ValueError("ETF execution rule requires formal source URLs")
        buy_lot_size = int(item["buy_lot_size"])
        sell_lot_size = int(item["sell_lot_size"])
        price_limit_rate_bps = int(item["price_limit_rate_bps"])
        tick_size_milli_yuan = _tick_milli_yuan(item["tick_size_cny"])
        if min(buy_lot_size, sell_lot_size, price_limit_rate_bps, tick_size_milli_yuan) <= 0:
            raise ValueError("ETF execution units and price limit must be positive")
        rules.append(
            ETFInstrumentExecutionRule(
                instrument_id=instrument_id,
                market=market,
                symbol=symbol,
                effective_from=effective_from,
                price_limit_rate_bps=price_limit_rate_bps,
                buy_lot_size=buy_lot_size,
                sell_lot_size=sell_lot_size,
                allow_odd_lot_full_exit=bool(item.get("allow_odd_lot_full_exit", True)),
                tick_size_milli_yuan=tick_size_milli_yuan,
                settlement_cycle=SettlementCycle(str(item["settlement_cycle"])),
                source_urls=source_urls,
            )
        )
    rules.sort(key=lambda item: (item.instrument_id, item.effective_from))
    execution_enabled = bool(raw.get("execution_enabled", False))
    if execution_enabled and not rules:
        raise ValueError("Enabled ETF execution policy requires at least one exact instrument rule")
    for rule in rules:
        if rule.market not in fee_schedule.applicable_markets:
            raise ValueError("ETF instrument rule market is not covered by its frozen fee schedule")
    return ETFExecutionPolicy(
        schema_version="etf-paper-trading-rules-v1",
        rule_version=rule_version,
        execution_enabled=execution_enabled,
        instrument_rules=tuple(rules),
        fee_schedule=fee_schedule,
    )


def _tick_milli_yuan(value: object) -> int:
    scaled = Decimal(str(value)) * Decimal(1000)
    if scaled != scaled.to_integral_value():
        raise ValueError("ETF paper tick_size_cny must be exactly representable to 0.001 CNY")
    return int(scaled)


__all__ = [
    "ETFExecutionPolicy",
    "ETFInstrumentExecutionRule",
    "load_etf_execution_policy",
]
