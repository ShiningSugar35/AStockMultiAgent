"""Strict contracts for financial-source observation, certification, and releases."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.financial import (
    FinancialDurationSemantics,
    FinancialFieldCode,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
)
from astock.schemas.market import InstrumentType, Market


class FinancialStatementScope(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"
    PARENT_COMPANY = "PARENT_COMPANY"


class FinancialSourceReleaseStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    NEEDS_INFO = "NEEDS_INFO"
    FAILED = "FAILED"


class FinancialSourceObservation(AStockModel):
    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: str = Field(pattern=r"^\d{6}$")
    instrument_id: str = Field(pattern=r"^(XSHG|XSHE|BJSE):\d{6}$")
    market: Market
    instrument_type: InstrumentType
    instrument_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_manifest_artifact_id: str = Field(
        pattern=r"^market-reference:[0-9a-f]{64}$"
    )
    instrument_manifest_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    period_start: date | None = None
    period_end: date
    period_type: FinancialPeriodType
    duration_semantics: FinancialDurationSemantics
    statement_type: FinancialStatementType
    statement_scope: FinancialStatementScope
    field_code: FinancialFieldCode
    provider_field: str = Field(min_length=1)
    reported_value: Decimal | None = Field(default=None, allow_inf_nan=False)
    unit: FinancialUnit
    provider_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    source_request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_to_system_at: AwareDatetime

    @model_validator(mode="after")
    def _period_and_scope(self) -> FinancialSourceObservation:
        if self.market is Market.INDEX or self.instrument_type is not InstrumentType.STOCK:
            raise ValueError("financial observations require an exchange-listed stock")
        if self.instrument_id != f"{self.market.value}:{self.company_id}":
            raise ValueError("financial observation instrument identity is inconsistent")
        if self.statement_scope is not FinancialStatementScope.CONSOLIDATED:
            raise ValueError("financial observations require CONSOLIDATED scope")
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start must not follow period_end")
        if self.statement_type is FinancialStatementType.BALANCE_SHEET:
            if self.duration_semantics is not FinancialDurationSemantics.INSTANT:
                raise ValueError("balance-sheet observations require INSTANT semantics")
            if self.period_start is not None:
                raise ValueError("balance-sheet observations cannot declare period_start")
        elif self.duration_semantics is FinancialDurationSemantics.INSTANT:
            raise ValueError("duration statements cannot use INSTANT semantics")
        elif self.period_start != date(self.period_end.year, 1, 1):
            raise ValueError("duration observations require fiscal-year period_start")
        if self.period_type is FinancialPeriodType.ANNUAL:
            if (self.period_end.month, self.period_end.day) != (12, 31):
                raise ValueError("annual observations require a December 31 period end")
            if self.statement_type is not FinancialStatementType.BALANCE_SHEET and (
                self.duration_semantics is not FinancialDurationSemantics.REPORTED_PERIOD
            ):
                raise ValueError("annual duration observations require REPORTED_PERIOD")
        elif self.period_type is FinancialPeriodType.SEMIANNUAL:
            if (self.period_end.month, self.period_end.day) != (6, 30):
                raise ValueError("semiannual observations require a June 30 period end")
            if self.statement_type is not FinancialStatementType.BALANCE_SHEET and (
                self.duration_semantics is not FinancialDurationSemantics.YEAR_TO_DATE
            ):
                raise ValueError("semiannual duration observations require YEAR_TO_DATE")
        else:
            if (self.period_end.month, self.period_end.day) not in {(3, 31), (9, 30)}:
                raise ValueError("quarterly observations require March 31 or September 30")
            if self.statement_type is not FinancialStatementType.BALANCE_SHEET and (
                self.duration_semantics is not FinancialDurationSemantics.YEAR_TO_DATE
            ):
                raise ValueError("quarterly duration observations require YEAR_TO_DATE")
        if self.field_code is FinancialFieldCode.SHARES_OUTSTANDING:
            if self.unit is not FinancialUnit.SHARES:
                raise ValueError("share observations require SHARES")
        elif self.unit is FinancialUnit.SHARES:
            raise ValueError("non-share observations cannot use SHARES")
        return self


class FinancialSourceFileDescriptor(AStockModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(gt=0)
    logical_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinancialSourceCoverage(AStockModel):
    provider_ids: list[str] = Field(min_length=1)
    statements_requested: list[FinancialStatementType] = Field(min_length=3)
    statements_observed: list[FinancialStatementType] = Field(default_factory=list)
    source_observation_count: int = Field(ge=0)
    certified_fact_count: int = Field(ge=0)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_values(self) -> FinancialSourceCoverage:
        for values, label in (
            (self.provider_ids, "provider_ids"),
            (self.statements_requested, "statements_requested"),
            (self.statements_observed, "statements_observed"),
            (self.reason_codes, "reason_codes"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self


class FinancialSourceReleaseManifest(AStockModel):
    schema_version: str = "financial-source-release-v1"
    release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: str = Field(pattern=r"^\d{6}$")
    instrument_id: str = Field(pattern=r"^(XSHG|XSHE|BJSE):\d{6}$")
    market: Market
    instrument_type: InstrumentType
    instrument_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_manifest_artifact_id: str = Field(
        pattern=r"^market-reference:[0-9a-f]{64}$"
    )
    instrument_manifest_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_available_to_system_at: AwareDatetime
    period_end: date
    period_type: FinancialPeriodType
    previous_release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    supersedes_release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_ids: list[str] = Field(min_length=1)
    raw_snapshot_ids: list[str] = Field(min_length=1)
    official_document_id: str
    official_index_snapshot_id: str
    official_snapshot_id: str
    official_pit_id: str
    source_files: list[FinancialSourceFileDescriptor] = Field(min_length=1)
    certified_files: list[FinancialSourceFileDescriptor] = Field(min_length=1)
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    certified_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_to_system_at: AwareDatetime
    status: FinancialSourceReleaseStatus
    coverage: FinancialSourceCoverage

    @model_validator(mode="after")
    def _integrity(self) -> FinancialSourceReleaseManifest:
        if self.market is Market.INDEX or self.instrument_type is not InstrumentType.STOCK:
            raise ValueError("financial releases require an exchange-listed stock")
        if self.instrument_id != f"{self.market.value}:{self.company_id}":
            raise ValueError("financial release instrument identity is inconsistent")
        if self.instrument_available_to_system_at > self.available_to_system_at:
            raise ValueError("instrument release was unavailable at financial release time")
        for values, label in (
            (self.provider_ids, "provider_ids"),
            (self.raw_snapshot_ids, "raw_snapshot_ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        paths = [item.path for item in [*self.source_files, *self.certified_files]]
        if len(paths) != len(set(paths)):
            raise ValueError("release file paths must be unique")
        if any(
            item.logical_content_hash != self.source_content_hash
            for item in self.source_files
        ):
            raise ValueError("source descriptor logical hash mismatch")
        if any(
            item.logical_content_hash != self.certified_content_hash
            for item in self.certified_files
        ):
            raise ValueError("certified descriptor logical hash mismatch")
        if self.status is not FinancialSourceReleaseStatus.CERTIFIED:
            raise ValueError("only certified facts may be published as a release")
        if self.coverage.certified_fact_count == 0:
            raise ValueError("certified release requires facts")
        return self


class FinancialSourceSyncReport(AStockModel):
    schema_version: str = "financial-source-sync-report-v1"
    command: str = "sync-financial"
    company_id: str = Field(pattern=r"^\d{6}$")
    period_end: date
    period_type: FinancialPeriodType
    status: FinancialSourceReleaseStatus
    release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_ids: list[str] = Field(default_factory=list)
    raw_snapshot_ids: list[str] = Field(default_factory=list)
    official_snapshot_id: str | None = None
    coverage: FinancialSourceCoverage
    reason_codes: list[str] = Field(default_factory=list)


__all__ = [
    "FinancialSourceCoverage",
    "FinancialSourceFileDescriptor",
    "FinancialSourceObservation",
    "FinancialSourceReleaseManifest",
    "FinancialSourceReleaseStatus",
    "FinancialSourceSyncReport",
    "FinancialStatementScope",
]
