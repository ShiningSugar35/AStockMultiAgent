"""Typed contracts for portfolio transition, hedging, and user-declared holdings."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.knowledge import PositionAction
from astock.schemas.market import InstrumentType, Market
from astock.schemas.portfolio import PortfolioAllocationMethod


class DeclaredTradeValidationStatus(StrEnum):
    READY = "READY"
    NEEDS_INFO = "NEEDS_INFO"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


class UserDeclaredTradeCapture(AStockModel):
    schema_version: str = "user-declared-trade-capture-v1"
    raw_statement: str = Field(min_length=1)
    declared_at: AwareDatetime
    market: Market | None = None
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    side: str | None = None
    quantity: int | None = Field(default=None, gt=0)
    price_cny: Decimal | None = Field(default=None, gt=0)
    occurred_at: AwareDatetime | None = None
    instrument_name: str | None = None

    @field_validator("side")
    @classmethod
    def normalize_side(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"BUY", "SELL"}:
            raise ValueError("declared trade side must be BUY or SELL")
        return normalized

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        for name in ("market", "symbol", "side", "quantity", "price_cny", "occurred_at"):
            if getattr(self, name) is None:
                missing.append(name)
        return missing


class ValidatedExternalTradeImport(AStockModel):
    schema_version: str = "validated-external-trade-import-v1"
    capture_artifact_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price_cny: Decimal = Field(gt=0)
    occurred_at: AwareDatetime
    raw_statement: str = Field(min_length=1)
    source: Literal["USER_DECLARED_EXTERNAL"] = "USER_DECLARED_EXTERNAL"

    @model_validator(mode="after")
    def validate_identity(self) -> ValidatedExternalTradeImport:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("external trade instrument_id must be market:symbol")
        return self


class ExternalTradeImportReceipt(AStockModel):
    schema_version: str = "external-trade-import-receipt-v1"
    receipt_id: str = Field(min_length=1)
    status: DeclaredTradeValidationStatus
    capture_artifact_id: str
    validation_artifact_id: str | None = None
    trade_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    deduplicated: bool = False
    position_projection: dict[str, object] | None = None
    requires_user_confirmation: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_receipt(self) -> ExternalTradeImportReceipt:
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("external trade receipt reason codes must be sorted and unique")
        if (
            self.status
            in {
                DeclaredTradeValidationStatus.READY,
                DeclaredTradeValidationStatus.DUPLICATE,
            }
            and not self.trade_id
        ):
            raise ValueError("recorded or duplicate trade receipt requires trade_id")
        return self


class UserPortfolioPosition(AStockModel):
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    quantity: int = Field(gt=0)
    average_cost_cny: Decimal = Field(gt=0)
    opened_at: AwareDatetime
    last_trade_at: AwareDatetime
    last_review_at: AwareDatetime | None = None
    last_action: str = "HOLD"
    thesis_status: str = "UNREVIEWED"
    review_note: str = ""


class UserPortfolioSnapshot(AStockModel):
    schema_version: str = "user-portfolio-snapshot-v1"
    snapshot_id: str = Field(min_length=1)
    as_of: AwareDatetime
    positions: list[UserPortfolioPosition]
    open_orders: list[dict[str, object]]
    trade_count: int = Field(ge=0)
    cash_cny: Decimal | None = Field(default=None, ge=0)
    cash_known: bool = False
    source: Literal["LOCAL_USER_STATE", "EXTERNAL_ACCOUNT_DEFAULT"] = "LOCAL_USER_STATE"
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_snapshot(self) -> UserPortfolioSnapshot:
        keys = [(item.market.value, item.symbol) for item in self.positions]
        if keys != sorted(set(keys)):
            raise ValueError("user portfolio positions must be sorted and unique")
        if self.cash_known != (self.cash_cny is not None):
            raise ValueError("cash_known must match cash_cny availability")
        return self


class PortfolioRiskObjective(StrEnum):
    DIVERSIFY = "DIVERSIFY"
    REDUCE_MARKET_BETA = "REDUCE_MARKET_BETA"
    REDUCE_VOLATILITY = "REDUCE_VOLATILITY"
    REDUCE_CONCENTRATION = "REDUCE_CONCENTRATION"
    REDUCE_INDUSTRY_EXPOSURE = "REDUCE_INDUSTRY_EXPOSURE"
    PROTECT_SCENARIO = "PROTECT_SCENARIO"


class HedgeClassification(StrEnum):
    DIVERSIFICATION = "DIVERSIFICATION"
    NATURAL_HEDGE = "NATURAL_HEDGE"
    EXPLICIT_HEDGE = "EXPLICIT_HEDGE"
    UNPROVEN = "UNPROVEN"


class ETFCategory(StrEnum):
    EQUITY = "EQUITY"
    BOND = "BOND"
    GOLD = "GOLD"
    CROSS_BORDER = "CROSS_BORDER"
    MONEY_MARKET = "MONEY_MARKET"
    OTHER = "OTHER"


class SettlementCycle(StrEnum):
    T0 = "T0"
    T1 = "T1"


class ProductDataQuality(StrEnum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class ProductCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class PortfolioIntentProfile(AStockModel):
    schema_version: str = "portfolio-intent-profile-v1"
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    anchor_company_id: str | None = Field(default=None, pattern=r"^\d{6}$")
    risk_objectives: list[PortfolioRiskObjective] = Field(default_factory=list)
    max_total_exposure: float | None = Field(default=None, gt=0, le=1)
    max_single_position: float | None = Field(default=None, gt=0, le=1)
    max_industry_exposure: float | None = Field(default=None, gt=0, le=1)
    max_abs_correlation: float | None = Field(default=None, ge=0, le=1)
    max_drawdown: float | None = Field(default=None, ge=0, le=1)
    max_market_beta: float | None = Field(default=None, ge=0)
    target_annualized_volatility: float | None = Field(default=None, gt=0)
    minimum_cash_weight: float = Field(default=0, ge=0, le=1)
    maximum_turnover_weight: float | None = Field(default=None, ge=0, le=2)
    user_locked_company_ids: list[str] = Field(default_factory=list)
    allowed_instrument_types: list[InstrumentType] = Field(
        default_factory=lambda: [InstrumentType.STOCK]
    )
    constraints_complete: bool = False

    @model_validator(mode="after")
    def validate_intent(self) -> PortfolioIntentProfile:
        if self.user_locked_company_ids != sorted(set(self.user_locked_company_ids)):
            raise ValueError("locked company ids must be sorted and unique")
        if len(self.allowed_instrument_types) != len(set(self.allowed_instrument_types)):
            raise ValueError("allowed instrument types must be unique")
        if self.minimum_cash_weight >= 1:
            raise ValueError("minimum cash weight must leave room for invested assets")
        return self


class PortfolioImplementationCostInput(AStockModel):
    instrument_id: str = Field(min_length=1)
    estimated_round_trip_cost_bps: float = Field(ge=0, le=5000)
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)
    verified: bool = False

    @model_validator(mode="after")
    def validate_cost_provenance(self) -> PortfolioImplementationCostInput:
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("implementation cost artifact/hash counts must match")
        for values in (self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("implementation cost provenance must be sorted and unique")
        if self.verified and (not self.source_artifact_ids or not self.source_object_hashes):
            raise ValueError("verified implementation cost requires frozen provenance")
        return self


class InstrumentTradingUnitRule(AStockModel):
    schema_version: str = "instrument-trading-unit-rule-v1"
    instrument_id: str = Field(min_length=1)
    instrument_type: InstrumentType
    buy_lot_size: int = Field(gt=0)
    sell_lot_size: int = Field(gt=0)
    allow_odd_lot_full_exit: bool = True
    tick_size_cny: Decimal = Field(gt=0)
    settlement_cycle: SettlementCycle
    effective_from: date
    source_urls: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rule(self) -> InstrumentTradingUnitRule:
        if self.source_urls != sorted(set(self.source_urls)):
            raise ValueError("trading rule source urls must be sorted and unique")
        return self


class ETFProductProfile(AStockModel):
    schema_version: str = "etf-product-profile-v1"
    profile_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    instrument_type: InstrumentType = InstrumentType.ETF
    category: ETFCategory
    tracking_target: str = Field(min_length=1)
    tracking_benchmark_market: Market | None = None
    tracking_benchmark_symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    management_fee_bps: float | None = Field(default=None, ge=0, le=500)
    custody_fee_bps: float | None = Field(default=None, ge=0, le=500)
    total_expense_ratio_bps: float | None = Field(default=None, ge=0, le=1000)
    total_net_asset_cny: Decimal | None = Field(default=None, ge=0)
    total_outstanding_shares: int | None = Field(default=None, ge=0)
    nav_currency: Literal["CNY"] = "CNY"
    trading_rule: InstrumentTradingUnitRule
    secondary_market_tradable: bool = True
    paper_replay_supported: bool = False
    facts_as_of: AwareDatetime | None = None
    quality_status: ProductDataQuality = ProductDataQuality.PARTIAL
    quality_warning_codes: list[str] = Field(default_factory=list)
    official_source_artifact_ids: list[str] = Field(min_length=1)
    official_source_object_hashes: list[str] = Field(min_length=1)
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def validate_profile(self) -> ETFProductProfile:
        if self.instrument_type is not InstrumentType.ETF:
            raise ValueError("ETF profile requires InstrumentType.ETF")
        if self.market is Market.INDEX:
            raise ValueError("ETF profile requires an exchange market, not INDEX")
        if (self.tracking_benchmark_market is None) != (self.tracking_benchmark_symbol is None):
            raise ValueError("ETF tracking benchmark market/symbol must be supplied together")
        if (
            self.total_expense_ratio_bps is not None
            and self.management_fee_bps is not None
            and self.custody_fee_bps is not None
            and self.total_expense_ratio_bps + 1e-12
            < self.management_fee_bps + self.custody_fee_bps
        ):
            raise ValueError("ETF total expense ratio cannot be below management+custody fees")
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("ETF instrument_id must be market:symbol")
        if self.trading_rule.instrument_id != self.instrument_id:
            raise ValueError("ETF trading rule must match product instrument_id")
        if self.trading_rule.instrument_type is not InstrumentType.ETF:
            raise ValueError("ETF trading rule requires InstrumentType.ETF")
        if len(self.official_source_artifact_ids) != len(self.official_source_object_hashes):
            raise ValueError("ETF official artifact/hash counts must match")
        for values in (
            self.quality_warning_codes,
            self.official_source_artifact_ids,
            self.official_source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("ETF profile lists must be sorted and unique")
        if self.facts_as_of is not None and self.facts_as_of > self.available_to_system_at:
            raise ValueError("ETF facts_as_of cannot follow product availability")
        if self.quality_status is ProductDataQuality.VERIFIED:
            required = (
                self.facts_as_of,
                self.tracking_benchmark_market,
                self.tracking_benchmark_symbol,
                self.management_fee_bps,
                self.custody_fee_bps,
                self.total_expense_ratio_bps,
                self.total_net_asset_cny,
                self.total_outstanding_shares,
            )
            if any(item is None for item in required) or self.quality_warning_codes:
                raise ValueError(
                    "VERIFIED ETF profile requires complete benchmark/fee/size/share facts"
                )
        return self


class FundProductProfile(AStockModel):
    """Frozen official identity and low-frequency facts for a non-ETF fund product."""

    schema_version: str = "fund-product-profile-v1"
    profile_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    fund_code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    instrument_type: InstrumentType = InstrumentType.FUND
    fund_category: str = Field(min_length=1)
    manager_name: str = Field(min_length=1)
    tracking_target: str | None = None
    tracking_benchmark_market: Market | None = None
    tracking_benchmark_symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    management_fee_bps: float | None = Field(default=None, ge=0, le=500)
    custody_fee_bps: float | None = Field(default=None, ge=0, le=500)
    total_expense_ratio_bps: float | None = Field(default=None, ge=0, le=1000)
    total_net_asset_cny: Decimal | None = Field(default=None, ge=0)
    total_outstanding_shares: int | None = Field(default=None, ge=0)
    nav_currency: Literal["CNY"] = "CNY"
    facts_as_of: AwareDatetime
    available_to_system_at: AwareDatetime
    quality_status: ProductDataQuality = ProductDataQuality.PARTIAL
    quality_warning_codes: list[str] = Field(default_factory=list)
    official_source_artifact_ids: list[str] = Field(min_length=1)
    official_source_object_hashes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profile(self) -> FundProductProfile:
        if self.instrument_type is not InstrumentType.FUND:
            raise ValueError("fund profile requires InstrumentType.FUND")
        if self.instrument_id != f"FUND:{self.fund_code}":
            raise ValueError("fund instrument_id must be FUND:fund_code")
        if (self.tracking_benchmark_market is None) != (self.tracking_benchmark_symbol is None):
            raise ValueError("fund tracking benchmark market/symbol must be supplied together")
        if self.facts_as_of > self.available_to_system_at:
            raise ValueError("fund facts_as_of cannot follow product availability")
        if len(self.official_source_artifact_ids) != len(self.official_source_object_hashes):
            raise ValueError("fund official artifact/hash counts must match")
        for values in (
            self.quality_warning_codes,
            self.official_source_artifact_ids,
            self.official_source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("fund profile lists must be sorted and unique")
        if self.quality_status is ProductDataQuality.VERIFIED:
            required = (
                self.management_fee_bps,
                self.custody_fee_bps,
                self.total_expense_ratio_bps,
                self.total_net_asset_cny,
                self.total_outstanding_shares,
            )
            if any(item is None for item in required) or self.quality_warning_codes:
                raise ValueError("VERIFIED fund profile requires complete fee/size/share facts")
        return self


class IndexProductProfile(AStockModel):
    """Frozen official index identity and benchmark methodology identity."""

    schema_version: str = "index-product-profile-v1"
    profile_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Literal[Market.INDEX] = Market.INDEX
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    instrument_type: InstrumentType = InstrumentType.INDEX
    index_provider: str = Field(min_length=1)
    methodology_name: str = Field(min_length=1)
    base_date: date | None = None
    base_value: Decimal | None = Field(default=None, gt=0)
    currency: Literal["CNY"] = "CNY"
    facts_as_of: AwareDatetime
    available_to_system_at: AwareDatetime
    quality_status: ProductDataQuality = ProductDataQuality.PARTIAL
    quality_warning_codes: list[str] = Field(default_factory=list)
    official_source_artifact_ids: list[str] = Field(min_length=1)
    official_source_object_hashes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profile(self) -> IndexProductProfile:
        if self.instrument_type is not InstrumentType.INDEX:
            raise ValueError("index profile requires InstrumentType.INDEX")
        if self.instrument_id != f"INDEX:{self.symbol}":
            raise ValueError("index instrument_id must be INDEX:symbol")
        if self.facts_as_of > self.available_to_system_at:
            raise ValueError("index facts_as_of cannot follow product availability")
        if len(self.official_source_artifact_ids) != len(self.official_source_object_hashes):
            raise ValueError("index official artifact/hash counts must match")
        for values in (
            self.quality_warning_codes,
            self.official_source_artifact_ids,
            self.official_source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("index profile lists must be sorted and unique")
        if self.quality_status is ProductDataQuality.VERIFIED and (
            self.base_date is None or self.base_value is None or self.quality_warning_codes
        ):
            raise ValueError("VERIFIED index profile requires complete base facts")
        return self


class ProductConstituent(AStockModel):
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str | None = None
    weight: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_identity(self) -> ProductConstituent:
        if self.market is Market.INDEX:
            expected = f"INDEX:{self.symbol}"
        else:
            expected = f"{self.market.value}:{self.symbol}"
        if self.instrument_id != expected:
            raise ValueError("constituent instrument identity mismatch")
        return self


class ProductConstituentSnapshot(AStockModel):
    """Official point-in-time composition for an ETF, fund, or index."""

    schema_version: str = "product-constituent-snapshot-v1"
    snapshot_id: str = Field(min_length=1)
    product_artifact_id: str = Field(min_length=1)
    product_instrument_id: str = Field(min_length=1)
    product_type: Literal["ETF", "FUND", "INDEX"]
    as_of: AwareDatetime
    available_to_system_at: AwareDatetime
    coverage_status: ProductCoverageStatus
    constituents: list[ProductConstituent] = Field(min_length=1)
    official_source_artifact_ids: list[str] = Field(min_length=1)
    official_source_object_hashes: list[str] = Field(min_length=1)
    warning_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshot(self) -> ProductConstituentSnapshot:
        identities = [item.instrument_id for item in self.constituents]
        if identities != sorted(set(identities)):
            raise ValueError("product constituents must be sorted and unique")
        total_weight = sum((item.weight for item in self.constituents), Decimal("0"))
        if total_weight > Decimal("1.02"):
            raise ValueError("product constituent weights exceed a valid normalized total")
        if self.coverage_status is ProductCoverageStatus.COMPLETE and not (
            Decimal("0.98") <= total_weight <= Decimal("1.02")
        ):
            raise ValueError("complete constituent snapshot must cover approximately 100% weight")
        if len(self.official_source_artifact_ids) != len(self.official_source_object_hashes):
            raise ValueError("constituent artifact/hash counts must match")
        for values in (
            self.warning_codes,
            self.official_source_artifact_ids,
            self.official_source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("constituent snapshot lists must be sorted and unique")
        return self


class ETFResearchMetricsRequest(AStockModel):
    schema_version: str = "etf-research-metrics-request-v1"
    profile_artifact_id: str = Field(min_length=1)
    as_of: AwareDatetime
    lookback_sessions: int = Field(default=60, ge=20, le=500)
    minimum_sessions: int = Field(default=40, ge=10, le=400)

    @model_validator(mode="after")
    def validate_window(self) -> ETFResearchMetricsRequest:
        if self.minimum_sessions >= self.lookback_sessions:
            raise ValueError("ETF metrics minimum_sessions must be below lookback_sessions")
        return self


class ETFResearchMetrics(AStockModel):
    schema_version: str = "etf-research-metrics-v1"
    metrics_id: str = Field(min_length=1)
    profile_artifact_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    observation_count: int = Field(ge=1)
    average_daily_amount_cny: float | None = Field(default=None, ge=0)
    annualized_volatility: float = Field(ge=0)
    tracking_benchmark_instrument_id: str | None = None
    tracking_error_annualized: float | None = Field(default=None, ge=0)
    premium_discount_rate: float | None = None
    management_fee_bps: float | None = Field(default=None, ge=0, le=500)
    custody_fee_bps: float | None = Field(default=None, ge=0, le=500)
    total_expense_ratio_bps: float | None = Field(default=None, ge=0, le=1000)
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    recommendation_allowed: Literal[False] = False
    portfolio_weight_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_metrics(self) -> ETFResearchMetrics:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("ETF metrics instrument_id must be market:symbol")
        for values in (
            self.warning_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("ETF metrics lists must be sorted and unique")
        return self


class ETFNavSighting(AStockModel):
    """A frozen per-share NAV or iNAV sighting with complete point-in-time semantics."""

    schema_version: str = "etf-nav-sighting-v1"
    sighting_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    sighting_type: Literal["NAV", "INAV"]
    value_cny: Decimal = Field(gt=0)
    as_of: AwareDatetime
    available_to_system_at: AwareDatetime
    official_source_artifact_ids: list[str] = Field(min_length=1)
    official_source_object_hashes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sighting(self) -> ETFNavSighting:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("ETF nav sighting instrument_id must be market:symbol")
        if self.available_to_system_at < self.as_of:
            raise ValueError("ETF NAV/iNAV availability cannot precede its valuation timestamp")
        if len(self.official_source_artifact_ids) != len(self.official_source_object_hashes):
            raise ValueError("ETF nav sighting official artifact/hash counts must match")
        for values in (
            self.official_source_artifact_ids,
            self.official_source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("ETF nav sighting provenance must be sorted and unique")
        return self


class ETFMarketPriceSighting(AStockModel):
    """Frozen market-price observation used only for research valuation."""

    schema_version: str = "etf-market-price-sighting-v1"
    sighting_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    price_cny: Decimal = Field(gt=0)
    as_of: AwareDatetime
    available_to_system_at: AwareDatetime
    source_artifact_ids: list[str] = Field(min_length=1)
    source_object_hashes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sighting(self) -> ETFMarketPriceSighting:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("ETF market price instrument_id must be market:symbol")
        if self.available_to_system_at < self.as_of:
            raise ValueError("ETF market-price availability cannot precede its price timestamp")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("ETF market-price artifact/hash counts must match")
        for values in (self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("ETF market-price provenance must be sorted and unique")
        return self


class ETFPremiumDiscountRequest(AStockModel):
    schema_version: str = "etf-premium-discount-request-v2"
    profile_artifact_id: str = Field(min_length=1)
    as_of: AwareDatetime
    nav_sighting_artifact_id: str = Field(min_length=1)
    inav_sighting_artifact_id: str = Field(min_length=1)
    market_price_sighting_artifact_id: str = Field(min_length=1)


class ETFPremiumDiscountValuation(AStockModel):
    """Frozen market/iNAV premium-discount with NAV bound as an independent control."""

    schema_version: str = "etf-premium-discount-valuation-v2"
    valuation_id: str = Field(min_length=1)
    profile_artifact_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    nav_sighting_artifact_id: str = Field(min_length=1)
    inav_sighting_artifact_id: str = Field(min_length=1)
    market_price_sighting_artifact_id: str = Field(min_length=1)
    nav_value_cny: Decimal = Field(gt=0)
    inav_value_cny: Decimal = Field(gt=0)
    market_price_cny: Decimal = Field(gt=0)
    premium_discount_basis: Literal["INAV"] = "INAV"
    premium_discount_rate: float
    market_to_nav_rate: float
    source_artifact_ids: list[str]
    source_object_hashes: list[str]

    @model_validator(mode="after")
    def validate_valuation(self) -> ETFPremiumDiscountValuation:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("ETF premium discount instrument_id must be market:symbol")
        for values in (self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("ETF premium discount lists must be sorted and unique")
        return self


class HedgeEffectivenessRequest(AStockModel):
    schema_version: str = "hedge-effectiveness-request-v1"
    current_analysis_artifact_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    instrument_type: InstrumentType
    hedge_weight: float = Field(gt=0, le=0.5)
    targeted_risk: PortfolioRiskObjective
    etf_profile_artifact_id: str | None = None
    etf_metrics_artifact_id: str | None = None
    mechanism_source_artifact_ids: list[str] = Field(default_factory=list)
    mechanism_source_object_hashes: list[str] = Field(default_factory=list)
    implementation_cost: PortfolioImplementationCostInput | None = None

    @model_validator(mode="after")
    def validate_hedge_request(self) -> HedgeEffectivenessRequest:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("hedge request instrument_id must be market:symbol")
        if len(self.mechanism_source_artifact_ids) != len(self.mechanism_source_object_hashes):
            raise ValueError("hedge mechanism artifact/hash counts must match")
        for values in (
            self.mechanism_source_artifact_ids,
            self.mechanism_source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("hedge mechanism provenance must be sorted and unique")
        if self.instrument_type is InstrumentType.ETF and (
            not self.etf_profile_artifact_id or not self.etf_metrics_artifact_id
        ):
            raise ValueError("ETF hedge evaluation requires exact product profile and metrics")
        if self.instrument_type is not InstrumentType.ETF and (
            self.etf_profile_artifact_id or self.etf_metrics_artifact_id
        ):
            raise ValueError("non-ETF hedge evaluation cannot bind ETF profile/metrics")
        return self


class HedgeEffectivenessReport(AStockModel):
    schema_version: str = "hedge-effectiveness-report-v1"
    report_id: str = Field(min_length=1)
    current_analysis_artifact_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    targeted_risk: PortfolioRiskObjective
    hedge_weight: float = Field(gt=0, le=0.5)
    classification: HedgeClassification
    baseline_risk_value: float = Field(ge=0)
    hedged_risk_value: float = Field(ge=0)
    gross_risk_reduction_fraction: float
    estimated_round_trip_cost_bps: float | None = Field(default=None, ge=0, le=5000)
    cost_verified: bool = False
    cost_acceptable: bool = False
    normal_correlation: float = Field(ge=-1, le=1)
    stress_correlation: float = Field(ge=-1, le=1)
    common_session_count: int = Field(ge=2)
    basis_risk_codes: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_hedge_report(self) -> HedgeEffectivenessReport:
        for values in (
            self.basis_risk_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("hedge effectiveness report lists must be sorted and unique")
        if self.classification in {
            HedgeClassification.NATURAL_HEDGE,
            HedgeClassification.EXPLICIT_HEDGE,
        } and (
            self.gross_risk_reduction_fraction <= 0
            or not self.cost_verified
            or not self.cost_acceptable
        ):
            raise ValueError(
                "formal hedge report requires positive risk reduction and acceptable verified cost"
            )
        return self


class HedgeInstrumentCandidate(AStockModel):
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    instrument_type: InstrumentType
    classification: HedgeClassification
    targeted_risk_codes: list[str] = Field(default_factory=list)
    expected_risk_reduction_fraction: float | None = Field(default=None, ge=0, le=1)
    normal_correlation: float | None = Field(default=None, ge=-1, le=1)
    stress_correlation: float | None = Field(default=None, ge=-1, le=1)
    estimated_cost_bps: float | None = Field(default=None, ge=0, le=5000)
    basis_risk_codes: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hedge_claim(self) -> HedgeInstrumentCandidate:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("hedge candidate instrument_id must be market:symbol")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("hedge candidate artifact/hash counts must match")
        for values in (
            self.targeted_risk_codes,
            self.basis_risk_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("hedge candidate lists must be sorted and unique")
        if self.classification in {
            HedgeClassification.NATURAL_HEDGE,
            HedgeClassification.EXPLICIT_HEDGE,
        }:
            if not self.targeted_risk_codes:
                raise ValueError("formal hedge classification requires targeted risk codes")
            if (
                self.expected_risk_reduction_fraction is None
                or self.expected_risk_reduction_fraction <= 0
            ):
                raise ValueError("formal hedge classification requires positive risk reduction")
            if not self.source_artifact_ids or not self.source_object_hashes:
                raise ValueError("formal hedge classification requires frozen provenance")
        return self


class SupplementalPortfolioAsset(AStockModel):
    instrument_id: str = Field(min_length=1)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    instrument_type: InstrumentType
    target_weight: float = Field(gt=0, le=1)
    etf_profile_artifact_id: str | None = None
    etf_metrics_artifact_id: str | None = None

    @model_validator(mode="after")
    def validate_supplemental_asset(self) -> SupplementalPortfolioAsset:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("supplemental instrument_id must be market:symbol")
        if self.instrument_type is not InstrumentType.ETF:
            raise ValueError("supplemental portfolio assets are reserved for ETF overlays")
        if not self.etf_profile_artifact_id or not self.etf_metrics_artifact_id:
            raise ValueError("ETF overlay requires exact registered product profile and metrics")
        return self


class PortfolioComplementCandidate(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    name: str = Field(min_length=1)
    prefilter_score: float = Field(ge=0, le=1)
    portfolio_correlation: float = Field(ge=-1, le=1)
    beta_to_benchmark: float
    annualized_volatility: float = Field(ge=0)
    market_liquidity_score: float | None = Field(default=None, ge=0, le=1)
    research_priority_score: float = Field(ge=0, le=1)
    requires_deep_research: Literal[True] = True
    recommendation_allowed: Literal[False] = False
    portfolio_weight_allowed: Literal[False] = False


class PortfolioComplementScreenRequest(AStockModel):
    schema_version: str = "portfolio-complement-screen-request-v1"
    current_analysis_artifact_id: str = Field(min_length=1)
    research_seed_report_artifact_id: str = Field(min_length=1)
    objective: PortfolioRiskObjective
    max_candidates: int = Field(default=8, ge=2, le=12)


class PortfolioComplementScreenReport(AStockModel):
    schema_version: str = "portfolio-complement-screen-report-v1"
    report_id: str = Field(min_length=1)
    as_of: AwareDatetime
    objective: PortfolioRiskObjective
    candidates: list[PortfolioComplementCandidate]
    universe_coverage_complete: bool
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    recommendation_allowed: Literal[False] = False
    portfolio_weight_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_screen(self) -> PortfolioComplementScreenReport:
        ids = [item.company_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("portfolio complement candidates must be unique")
        for values in (self.warning_codes, self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("portfolio complement report lists must be sorted and unique")
        return self


class PortfolioRiskGap(AStockModel):
    gap_code: str = Field(min_length=1)
    current_value: float | None = None
    target_or_limit: float | None = None
    severity: Literal["INFO", "MATERIAL", "HARD"] = "MATERIAL"
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)


class PositionTargetBand(AStockModel):
    instrument_id: str = Field(min_length=1)
    current_weight: float = Field(ge=0, le=1)
    target_weight_lower: float = Field(ge=0, le=1)
    target_weight_mid: float = Field(ge=0, le=1)
    target_weight_upper: float = Field(ge=0, le=1)
    action: PositionAction
    current_quantity: int | None = Field(default=None, ge=0)
    target_quantity_min: int | None = Field(default=None, ge=0)
    target_quantity_max: int | None = Field(default=None, ge=0)
    estimated_trade_quantity_min: int | None = None
    estimated_trade_quantity_max: int | None = None

    @model_validator(mode="after")
    def validate_band(self) -> PositionTargetBand:
        if not (self.target_weight_lower <= self.target_weight_mid <= self.target_weight_upper):
            raise ValueError("position target weights must satisfy lower<=mid<=upper")
        if (
            self.target_quantity_min is not None
            and self.target_quantity_max is not None
            and self.target_quantity_min > self.target_quantity_max
        ):
            raise ValueError("target quantity minimum cannot exceed maximum")
        if (
            self.estimated_trade_quantity_min is not None
            and self.estimated_trade_quantity_max is not None
            and self.estimated_trade_quantity_min > self.estimated_trade_quantity_max
        ):
            raise ValueError("estimated trade quantity minimum cannot exceed maximum")
        return self


class PortfolioVariantMetrics(AStockModel):
    variant: Literal["CURRENT", "ANCHOR_ONLY", "TARGET"]
    weights: dict[str, float]
    cash_weight: float = Field(ge=0, le=1)
    annualized_volatility: float = Field(ge=0)
    beta_to_benchmark: float
    max_drawdown: float = Field(ge=0, le=1)
    historical_cvar_95: float = Field(ge=0)
    concentration_hhi: float = Field(ge=0, le=1)
    max_abs_pair_correlation: float = Field(ge=0, le=1)


class PortfolioTransitionRequest(AStockModel):
    schema_version: str = "portfolio-transition-request-v1"
    current_analysis_artifact_id: str = Field(min_length=1)
    target_construction_artifact_id: str = Field(min_length=1)
    intent: PortfolioIntentProfile
    selected_method: PortfolioAllocationMethod = PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED
    portfolio_nav_fen: int | None = Field(default=None, gt=0)
    current_quantities: dict[str, int] = Field(default_factory=dict)
    implementation_costs: list[PortfolioImplementationCostInput] = Field(default_factory=list)
    trading_rules: list[InstrumentTradingUnitRule] = Field(default_factory=list)
    supplemental_assets: list[SupplementalPortfolioAsset] = Field(default_factory=list)
    hedge_candidates: list[HedgeInstrumentCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_request(self) -> PortfolioTransitionRequest:
        if any(value < 0 for value in self.current_quantities.values()):
            raise ValueError("current quantities cannot be negative")
        instrument_ids = [item.instrument_id for item in self.trading_rules]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("transition trading rules must be unique by instrument")
        supplemental_ids = [item.instrument_id for item in self.supplemental_assets]
        if len(supplemental_ids) != len(set(supplemental_ids)):
            raise ValueError("supplemental assets must be unique")
        return self


class PortfolioTransitionReport(AStockModel):
    schema_version: str = "portfolio-transition-report-v1"
    report_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    current: PortfolioVariantMetrics
    anchor_only: PortfolioVariantMetrics | None = None
    target: PortfolioVariantMetrics
    risk_gaps: list[PortfolioRiskGap]
    target_bands: list[PositionTargetBand]
    hedge_candidates: list[HedgeInstrumentCandidate]
    estimated_turnover_weight: float = Field(ge=0, le=2)
    estimated_implementation_cost_fen: int | None = Field(default=None, ge=0)
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    requires_user_confirmation: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> PortfolioTransitionReport:
        for values in (
            self.warning_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("transition report lists must be sorted and unique")
        return self


class RebalanceBandPolicy(AStockModel):
    schema_version: str = "portfolio-rebalance-band-policy-v1"
    minimum_weight_band: float = Field(gt=0, le=0.25)
    maximum_weight_band: float = Field(gt=0, le=0.5)
    unverified_cost_weight_band: float = Field(gt=0, le=0.5)
    cost_to_band_multiplier: float = Field(gt=0, le=100)
    volatility_to_band_multiplier: float = Field(ge=0, le=1)
    material_weight_change: float = Field(gt=0, le=0.25)

    @model_validator(mode="after")
    def validate_bands(self) -> RebalanceBandPolicy:
        if self.minimum_weight_band > self.maximum_weight_band:
            raise ValueError("minimum rebalance band cannot exceed maximum band")
        if self.unverified_cost_weight_band < self.minimum_weight_band:
            raise ValueError("unverified-cost band cannot be narrower than minimum band")
        return self


__all__ = [
    "DeclaredTradeValidationStatus",
    "ETFCategory",
    "ETFMarketPriceSighting",
    "ETFNavSighting",
    "ETFPremiumDiscountRequest",
    "ETFPremiumDiscountValuation",
    "ETFProductProfile",
    "ETFResearchMetrics",
    "ETFResearchMetricsRequest",
    "ExternalTradeImportReceipt",
    "FundProductProfile",
    "HedgeClassification",
    "HedgeEffectivenessReport",
    "HedgeEffectivenessRequest",
    "HedgeInstrumentCandidate",
    "IndexProductProfile",
    "InstrumentTradingUnitRule",
    "ProductConstituent",
    "ProductConstituentSnapshot",
    "ProductCoverageStatus",
    "ProductDataQuality",
    "PortfolioComplementCandidate",
    "PortfolioComplementScreenReport",
    "PortfolioComplementScreenRequest",
    "PortfolioImplementationCostInput",
    "PortfolioIntentProfile",
    "PortfolioRiskGap",
    "PortfolioRiskObjective",
    "PortfolioTransitionReport",
    "PortfolioTransitionRequest",
    "PortfolioVariantMetrics",
    "PositionTargetBand",
    "RebalanceBandPolicy",
    "SettlementCycle",
    "SupplementalPortfolioAsset",
    "UserDeclaredTradeCapture",
    "UserPortfolioPosition",
    "UserPortfolioSnapshot",
    "ValidatedExternalTradeImport",
]
