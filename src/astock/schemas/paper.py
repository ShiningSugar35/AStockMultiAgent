"""Paper order, ledger, position, NAV, and replay contracts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import Market, ReplayQuality


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class NormalBalance(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class Order(AStockModel):
    order_id: str
    account_id: str
    client_request_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: int = Field(gt=0)
    filled_qty: int = Field(default=0, ge=0)
    limit_price_fen: int | None = Field(default=None, gt=0)
    reserved_fen: int = Field(default=0, ge=0)
    reserved_qty: int = Field(default=0, ge=0)
    status: OrderStatus
    submitted_at: AwareDatetime
    effective_rule_version: str

    @model_validator(mode="after")
    def validate_quantities(self) -> Order:
        if self.filled_qty > self.qty:
            raise ValueError("filled_qty cannot exceed qty")
        if self.order_type == OrderType.LIMIT and self.limit_price_fen is None:
            raise ValueError("limit orders require limit_price_fen")
        return self


class Fill(AStockModel):
    fill_id: str
    order_id: str
    qty: int = Field(gt=0)
    price_fen: int = Field(gt=0)
    commission_fen: int = Field(default=0, ge=0)
    tax_fen: int = Field(default=0, ge=0)
    transfer_fee_fen: int = Field(default=0, ge=0)
    occurred_at: AwareDatetime
    replay_quality: ReplayQuality


class LedgerAccount(AStockModel):
    account_id: str
    paper_account_id: str
    account_type: AccountType
    currency: str = "CNY"
    normal_balance: NormalBalance
    status: str = "OPEN"


class LedgerEntry(AStockModel):
    entry_id: str
    event_id: str
    account_id: str
    debit_fen: int = Field(default=0, ge=0)
    credit_fen: int = Field(default=0, ge=0)
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def exactly_one_side(self) -> LedgerEntry:
        if (self.debit_fen > 0) == (self.credit_fen > 0):
            raise ValueError("ledger entry must have exactly one positive side")
        return self


class Position(AStockModel):
    account_id: str
    symbol: str
    qty_total: int = Field(ge=0)
    qty_available: int = Field(ge=0)
    avg_cost_fen: int = Field(ge=0)
    realized_pnl_fen: int = 0
    unrealized_pnl_fen: int = 0
    as_of_event_seq: int = Field(ge=0)


class PortfolioNAV(AStockModel):
    account_id: str
    as_of: AwareDatetime
    cash_fen: int
    frozen_cash_fen: int = 0
    market_value_fen: int = 0
    receivable_fen: int = 0
    payable_fen: int = 0
    nav_fen: int
    data_quality: str


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    DELIST = "DELIST"


class CorporateActionEvent(AStockModel):
    event_id: str
    symbol: str
    event_type: CorporateActionType
    ex_date: str
    record_date: str | None = None
    pay_date: str | None = None
    ratio: Decimal | None = None
    cash_fen: int | None = None
    source_id: str
    rule_version: str


class ReplayCheckpoint(AStockModel):
    account_id: str
    symbol: str
    requested_resolution: str = "5m"
    actual_resolution: str
    replay_quality: ReplayQuality
    provider_id: str | None = None
    coverage_start: AwareDatetime | None = None
    coverage_end: AwareDatetime | None = None
    missing_bars: int = Field(default=0, ge=0)
    fallback_reason: str | None = None
    last_event_seq: int = Field(default=0, ge=0)
    market_cursor: str | None = None


class ReplayFeeSchedule(AStockModel):
    rule_version: str
    effective_from: date
    applicable_markets: list[Market]
    commission_rate: Decimal = Field(ge=0, le=Decimal("0.003"))
    minimum_commission_fen: int = Field(ge=0)
    stamp_tax_sell_rate: Decimal = Field(ge=0, le=Decimal("0.01"))
    transfer_fee_rate: Decimal = Field(ge=0, le=Decimal("0.01"))
    commission_includes_exchange_regulatory_fees: bool = True
    requires_broker_confirmation: bool = True
    source_urls: list[str] = Field(default_factory=list)


class ReplayExecutionReport(AStockModel):
    account_id: str
    market: Market
    symbol: str
    requested_cursor: AwareDatetime
    previous_cursor: AwareDatetime | None = None
    processed_bars: int = Field(ge=0)
    matched_orders: int = Field(ge=0)
    fill_ids: list[str] = Field(default_factory=list)
    replay_quality: ReplayQuality
    fee_rule_version: str
    fee_assumptions_require_broker_confirmation: bool
    maximum_participation_rate: Decimal = Field(gt=0, le=1)
    checkpoint: ReplayCheckpoint | None = None
