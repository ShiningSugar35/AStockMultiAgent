"""Point-in-time market-reference dataset contracts."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import AdjustmentMode, AmountUnit, InstrumentType, Market, VolumeUnit


class ReferenceDatasetKind(StrEnum):
    INSTRUMENT_MASTER = "INSTRUMENT_MASTER"
    TRADING_CALENDAR = "TRADING_CALENDAR"
    DAILY_UNADJUSTED = "DAILY_UNADJUSTED"
    CORPORATE_ACTION = "CORPORATE_ACTION"


class ReferenceCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    CONFLICTED = "CONFLICTED"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


class ReferencePitStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    RECONSTRUCTED = "RECONSTRUCTED"
    UNVERIFIED = "UNVERIFIED"


class CorporateActionStatus(StrEnum):
    DISCOVERED_STRUCTURED = "DISCOVERED_STRUCTURED"
    OFFICIAL_DOCUMENT_LINKED = "OFFICIAL_DOCUMENT_LINKED"
    TERMS_VERIFIED = "TERMS_VERIFIED"


class BaoStockRawEnvelopeV1(AStockModel):
    schema_version: str = "baostock-raw-envelope-v1"
    sdk_version: str = "0.8.9"
    capability: str
    request: dict[str, str]
    request_started_at: AwareDatetime
    request_finished_at: AwareDatetime
    login_error_code: str
    login_error_message: str
    result_error_code: str
    result_error_message: str
    fields: list[str]
    rows: list[list[str]]
    row_contexts: list[dict[str, str]] = Field(default_factory=list)
    complete: bool

    @model_validator(mode="after")
    def _raw_shape(self) -> BaoStockRawEnvelopeV1:
        if self.request_finished_at < self.request_started_at:
            raise ValueError("request_finished_at precedes request_started_at")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("BaoStock fields must be unique")
        if any(len(row) != len(self.fields) for row in self.rows):
            raise ValueError("BaoStock row width does not match fields")
        if self.row_contexts and len(self.row_contexts) != len(self.rows):
            raise ValueError("BaoStock row_contexts must align with rows")
        if self.complete and (self.login_error_code != "0" or self.result_error_code != "0"):
            raise ValueError("complete BaoStock envelope cannot carry an SDK error")
        return self


class ReferenceCoverage(AStockModel):
    requested_start: date | None = None
    requested_end: date | None = None
    actual_start: date | None = None
    actual_end: date | None = None
    record_count: int = Field(ge=0)
    status: ReferenceCoverageStatus
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_range(self) -> ReferenceCoverage:
        if (
            self.requested_start
            and self.requested_end
            and self.requested_end < self.requested_start
        ):
            raise ValueError("requested_end must not precede requested_start")
        if self.actual_start and self.actual_end and self.actual_end < self.actual_start:
            raise ValueError("actual_end must not precede actual_start")
        if self.status is ReferenceCoverageStatus.COMPLETE and self.record_count == 0:
            raise ValueError("COMPLETE coverage requires records")
        return self


class InstrumentRecord(AStockModel):
    record_type: str = "instrument"
    instrument_id: str
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str
    instrument_type: InstrumentType
    tradable: bool
    status_date: date
    is_st: bool
    listing_date: date | None = None
    delisting_date: date | None = None
    source_snapshot_id: str
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def _identity_and_tradeability(self) -> InstrumentRecord:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("instrument_id must be market:symbol")
        if self.instrument_type is InstrumentType.INDEX and self.tradable:
            raise ValueError("indices are not tradable instruments")
        return self


class TradingSession(AStockModel):
    record_type: str = "trading_session"
    exchange: Market
    session_date: date
    is_open: bool
    source_snapshot_id: str
    available_to_system_at: AwareDatetime

    @field_validator("exchange")
    @classmethod
    def _exchange_not_index(cls, value: Market) -> Market:
        if value is Market.INDEX:
            raise ValueError("INDEX is not an exchange calendar")
        return value


class DailyBarObservation(AStockModel):
    record_type: str = "daily_bar"
    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_id: str
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    session_date: date
    session_close_at: AwareDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    previous_close: Decimal | None = Field(default=None, gt=0)
    volume: Decimal = Field(ge=0)
    volume_unit: VolumeUnit = VolumeUnit.SHARE
    amount: Decimal | None = Field(default=None, ge=0)
    amount_unit: AmountUnit = AmountUnit.CNY
    adjustment_mode: AdjustmentMode = AdjustmentMode.NONE
    is_st: bool | None = None
    source_snapshot_id: str
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def _unadjusted_and_consistent(self) -> DailyBarObservation:
        if self.adjustment_mode is not AdjustmentMode.NONE:
            raise ValueError("daily reference facts must be unadjusted")
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("instrument_id must be market:symbol")
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close, self.high
        ):
            raise ValueError("invalid OHLC relationship")
        shanghai = ZoneInfo("Asia/Shanghai")
        expected_close = datetime.combine(self.session_date, time(15, 0), tzinfo=shanghai)
        if self.session_close_at.astimezone(shanghai) != expected_close:
            raise ValueError("session_close_at must be the Shanghai 15:00 close")
        if self.available_to_system_at < self.session_close_at:
            raise ValueError("daily bar cannot be available before the Shanghai session close")
        return self


class CorporateActionObservation(AStockModel):
    record_type: str = "corporate_action"
    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_id: str
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    action_type: str
    report_period: str | None = None
    announcement_date: date | None = None
    ex_date: date | None = None
    status: CorporateActionStatus
    structured_terms: dict[str, str] = Field(default_factory=dict)
    official_document_snapshot_id: str | None = None
    official_document_url: str | None = None
    official_announcement_id: str | None = None
    ledger_eligible: bool = False
    source_snapshot_id: str
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def _official_terms_gate(self) -> CorporateActionObservation:
        if self.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("instrument_id must be market:symbol")
        if self.status is CorporateActionStatus.DISCOVERED_STRUCTURED and (
            self.official_document_snapshot_id
            or self.official_document_url
            or self.official_announcement_id
        ):
            raise ValueError("structured-only hints cannot claim an official document")
        if self.status is not CorporateActionStatus.DISCOVERED_STRUCTURED and (
            not self.official_document_snapshot_id
            or not self.official_document_url
            or not self.official_announcement_id
        ):
            raise ValueError("linked or verified actions require an official document")
        if self.ledger_eligible and self.status is not CorporateActionStatus.TERMS_VERIFIED:
            raise ValueError("only TERMS_VERIFIED actions may be ledger eligible")
        return self


ReferenceRecord = (
    InstrumentRecord | TradingSession | DailyBarObservation | CorporateActionObservation
)


class ReferenceBatch(AStockModel):
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_kind: ReferenceDatasetKind
    scope_key: str
    provider_id: str
    raw_snapshot_ids: list[str] = Field(min_length=1)
    records: list[ReferenceRecord]
    coverage: ReferenceCoverage
    pit_status: ReferencePitStatus
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def _homogeneous_records(self) -> ReferenceBatch:
        expected = {
            ReferenceDatasetKind.INSTRUMENT_MASTER: InstrumentRecord,
            ReferenceDatasetKind.TRADING_CALENDAR: TradingSession,
            ReferenceDatasetKind.DAILY_UNADJUSTED: DailyBarObservation,
            ReferenceDatasetKind.CORPORATE_ACTION: CorporateActionObservation,
        }[self.dataset_kind]
        if any(not isinstance(item, expected) for item in self.records):
            raise ValueError("reference batch contains a record of the wrong dataset kind")
        if self.coverage.record_count != len(self.records):
            raise ValueError("coverage record_count must match records")
        if len(set(self.raw_snapshot_ids)) != len(self.raw_snapshot_ids):
            raise ValueError("raw_snapshot_ids must be unique")
        identities: list[str] = []
        for item in self.records:
            if isinstance(item, InstrumentRecord):
                identities.append(item.instrument_id)
            elif isinstance(item, TradingSession):
                identities.append(f"{item.exchange.value}:{item.session_date.isoformat()}")
            else:
                identities.append(item.observation_id)
        if len(set(identities)) != len(identities):
            raise ValueError("reference batch contains duplicate record identities")
        for item in self.records:
            if item.available_to_system_at > self.available_to_system_at:
                raise ValueError("record availability cannot exceed batch availability")
            if (
                isinstance(item, DailyBarObservation)
                and item.available_to_system_at < item.session_close_at
            ):
                raise ValueError("daily record is not point-in-time visible at batch release")
        return self


class ReferenceFileDescriptor(AStockModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(gt=0)
    logical_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DatasetReleaseManifest(AStockModel):
    schema_version: str = "market-reference-release-v2"
    release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_kind: ReferenceDatasetKind
    scope_key: str
    provider_id: str
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_snapshot_ids: list[str] = Field(min_length=1)
    observation_files: list[ReferenceFileDescriptor] = Field(min_length=1)
    canonical_files: list[ReferenceFileDescriptor] = Field(min_length=1)
    coverage: ReferenceCoverage
    pit_status: ReferencePitStatus
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def _manifest_integrity(self) -> DatasetReleaseManifest:
        files = [*self.observation_files, *self.canonical_files]
        paths = [item.path for item in files]
        if len(set(paths)) != len(paths):
            raise ValueError("release file paths must be unique")
        if any(item.logical_content_hash != self.content_hash for item in files):
            raise ValueError("release file logical hashes must match content_hash")
        return self


class ReferenceSyncReport(AStockModel):
    schema_version: str = "reference-sync-report-v1"
    command: str
    status: ReferenceCoverageStatus
    dataset_kind: ReferenceDatasetKind
    scope_key: str
    provider_id: str | None = None
    release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_snapshot_ids: list[str] = Field(default_factory=list)
    coverage: ReferenceCoverage
    pit_status: ReferencePitStatus
    reason_codes: list[str] = Field(default_factory=list)


__all__ = [
    "BaoStockRawEnvelopeV1",
    "CorporateActionObservation",
    "CorporateActionStatus",
    "DailyBarObservation",
    "DatasetReleaseManifest",
    "InstrumentRecord",
    "ReferenceBatch",
    "ReferenceCoverage",
    "ReferenceCoverageStatus",
    "ReferenceDatasetKind",
    "ReferencePitStatus",
    "ReferenceFileDescriptor",
    "ReferenceSyncReport",
    "TradingSession",
]
