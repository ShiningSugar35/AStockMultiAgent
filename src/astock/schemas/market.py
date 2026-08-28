"""Market-provider, routing, bar, and quality contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class Market(StrEnum):
    XSHG = "XSHG"
    XSHE = "XSHE"
    BJSE = "BJSE"
    INDEX = "INDEX"


class InstrumentType(StrEnum):
    STOCK = "STOCK"
    INDEX = "INDEX"


class Frequency(StrEnum):
    M1 = "1m"
    M5 = "5m"
    H1 = "60m"
    D1 = "1d"


class AdjustmentMode(StrEnum):
    NONE = "NONE"
    QFQ = "QFQ"
    HFQ = "HFQ"
    LOCAL_VERSIONED = "LOCAL_VERSIONED"


class TimestampSemantics(StrEnum):
    BAR_START = "BAR_START"
    BAR_END = "BAR_END"
    DATE_ONLY = "DATE_ONLY"
    UNKNOWN = "UNKNOWN"


class VolumeUnit(StrEnum):
    SHARE = "SHARE"
    LOT_100_SHARES = "LOT_100_SHARES"
    UNKNOWN = "UNKNOWN"


class AmountUnit(StrEnum):
    CNY = "CNY"
    UNKNOWN = "UNKNOWN"


class ProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class QualityStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


class ReplayQuality(StrEnum):
    DUAL_SOURCE_5M_VERIFIED = "DUAL_SOURCE_5M_VERIFIED"
    SINGLE_SOURCE_5M = "SINGLE_SOURCE_5M"
    PROVIDER_1H_APPROX = "PROVIDER_1H_APPROX"
    DAILY_OPEN_MODEL = "DAILY_OPEN_MODEL"
    DAILY_CLOSE_MODEL = "DAILY_CLOSE_MODEL"
    DAILY_CONSERVATIVE = "DAILY_CONSERVATIVE"
    UNREPLAYABLE = "UNREPLAYABLE"


class AccessTransport(StrEnum):
    LOCAL = "LOCAL"
    API = "API"
    MCP = "MCP"
    BROWSER = "BROWSER"
    SEARCH = "SEARCH"
    MANUAL = "MANUAL"


class SourceClass(StrEnum):
    LOCAL_IMMUTABLE = "LOCAL_IMMUTABLE"
    PRIMARY_OFFICIAL_WEB = "PRIMARY_OFFICIAL_WEB"
    SECONDARY_STRUCTURED = "SECONDARY_STRUCTURED"
    REPUTABLE_WEB_SEARCH = "REPUTABLE_WEB_SEARCH"
    MANUAL = "MANUAL"
    UNKNOWN = "UNKNOWN"


class CompletenessSemantics(StrEnum):
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    EXACT_ITEM = "EXACT_ITEM"
    WINDOW_EXHAUSTIVE = "WINDOW_EXHAUSTIVE"
    FULL_UNIVERSE = "FULL_UNIVERSE"
    CONTINUOUS_SERIES = "CONTINUOUS_SERIES"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RateLimitState(StrEnum):
    UNKNOWN = "UNKNOWN"
    OK = "OK"
    THROTTLED = "THROTTLED"
    PAUSED = "PAUSED"


class BarRequest(AStockModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: Market
    exchange: Market | None = None
    instrument_type: InstrumentType = InstrumentType.STOCK
    frequency: Frequency = Frequency.M5
    requested_start: AwareDatetime
    requested_end: AwareDatetime
    adjustment_mode: AdjustmentMode = AdjustmentMode.NONE
    limit: int = Field(default=2048, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_range(self) -> BarRequest:
        if self.requested_end < self.requested_start:
            raise ValueError("requested_end must not precede requested_start")
        if self.market is Market.INDEX:
            if self.instrument_type is not InstrumentType.INDEX:
                raise ValueError("INDEX market requests require INDEX instrument_type")
            if self.exchange not in {None, Market.XSHG, Market.XSHE}:
                raise ValueError("index exchange must be XSHG/XSHE when explicitly known")
        elif self.exchange is not None and self.exchange is not self.market:
            raise ValueError("stock request exchange must match market")
        return self


class DataProviderCapability(AStockModel):
    provider_id: str
    markets: list[Market]
    instrument_types: list[InstrumentType]
    frequencies: list[Frequency]
    adjustment_modes: list[AdjustmentMode]
    amount_supported: bool
    timestamp_semantics: TimestampSemantics
    session_rules: str
    volume_unit: VolumeUnit
    rate_limit: str | None = None
    auth_dependency: str | None = None
    requested_range: tuple[AwareDatetime, AwareDatetime] | None = None
    actual_range: tuple[AwareDatetime, AwareDatetime] | None = None
    last_probe_at: AwareDatetime
    quality_score: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    status: ProviderStatus
    limitations: list[str] = Field(default_factory=list)


class TransportCapability(AStockModel):
    source_id: str
    transport: AccessTransport
    requested_capabilities: list[str]
    available: bool
    reason: str
    officiality: str = "UNKNOWN"
    source_class: SourceClass = SourceClass.UNKNOWN
    formal_eligible: bool = True
    completeness_semantics: CompletenessSemantics = CompletenessSemantics.NOT_APPLICABLE
    completeness_score: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    local_availability_score: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    independence_score: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    independence_group: str | None = None
    health_status: str = "UNKNOWN"
    freshness_score: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    latency_ms: int = Field(default=0, ge=0)
    cost_efficiency_score: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    auth_ease_score: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    retryable_failure: bool = False


class SourceAccessRequest(AStockModel):
    source_id: str | None = None
    requested_capability: str
    formal_use: bool = False
    require_complete: bool = False


class SourceAccessDecision(AStockModel):
    decision_id: str
    source_id: str
    selected_source_id: str | None = None
    requested_capability: str
    selected_transport: AccessTransport
    selection_reason: str
    fallback_chain: list[AccessTransport]
    fallback_source_chain: list[str] = Field(default_factory=list)
    request_started_at: AwareDatetime
    request_finished_at: AwareDatetime | None = None
    result_hash: str | None = None
    failure_class: str | None = None
    rate_limit_state: RateLimitState = RateLimitState.UNKNOWN


class MarketBar(AStockModel):
    observation_id: str
    provider_id: str
    symbol: str
    market: Market
    frequency: Frequency
    timestamp: AwareDatetime
    timestamp_semantics: TimestampSemantics
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    volume: Decimal = Field(ge=0)
    volume_unit: VolumeUnit
    amount: Decimal | None = Field(default=None, ge=0)
    amount_unit: AmountUnit = AmountUnit.CNY
    adjustment_mode: AdjustmentMode = AdjustmentMode.NONE


class MarketDataBatch(AStockModel):
    batch_id: str
    provider_id: str
    request: BarRequest
    requested_start: AwareDatetime
    requested_end: AwareDatetime
    actual_start: AwareDatetime | None = None
    actual_end: AwareDatetime | None = None
    bar_count: int = Field(ge=0)
    bars: list[MarketBar]
    raw_snapshot_id: str
    cursor: str | None = None
    provider_latency_ms: int = Field(ge=0)
    provider_status: ProviderStatus

    @model_validator(mode="after")
    def validate_batch(self) -> MarketDataBatch:
        if self.bar_count != len(self.bars):
            raise ValueError("bar_count must equal len(bars)")
        if self.bars:
            minimum = min(bar.timestamp for bar in self.bars)
            maximum = max(bar.timestamp for bar in self.bars)
            if self.actual_start != minimum or self.actual_end != maximum:
                raise ValueError("actual range must match contained bars")
        elif self.actual_start is not None or self.actual_end is not None:
            raise ValueError("empty batches cannot declare an actual range")
        return self


class DataQualityReport(AStockModel):
    report_id: str
    batch_ids: list[str]
    symbol: str
    frequency: Frequency
    requested_start: AwareDatetime
    requested_end: AwareDatetime
    actual_start: AwareDatetime | None = None
    actual_end: AwareDatetime | None = None
    bar_count: int = Field(ge=0)
    missing_sessions: list[str] = Field(default_factory=list)
    duplicate_bars: int = Field(default=0, ge=0)
    ohlc_errors: int = Field(default=0, ge=0)
    volume_unit: VolumeUnit
    adjustment_mode: AdjustmentMode
    timestamp_semantics: TimestampSemantics
    provider_latency_ms: int = Field(default=0, ge=0)
    provider_status: ProviderStatus
    cross_source_diffs: dict[str, int | float | str] = Field(default_factory=dict)
    quality_status: QualityStatus
    replay_quality: ReplayQuality
    reasons: list[str] = Field(default_factory=list)


def shanghai_time(value: datetime) -> datetime:
    """Identity helper retained as a clear extension point for time normalization."""

    return value
