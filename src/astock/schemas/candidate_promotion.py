"""ResearchSeed-to-Candidate promotion contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class SeedPromotionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    NEEDS_INFO = "NEEDS_INFO"


class SeedPromotionCompanyStatus(StrEnum):
    PROMOTED = "PROMOTED"
    NEEDS_INFO = "NEEDS_INFO"
    REUSED_EXISTING_CANDIDATE = "REUSED_EXISTING_CANDIDATE"


class SeedPromotionTask(AStockModel):
    task_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    task_code: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    source_artifact_ids: list[str] = Field(default_factory=list)
    retryable: bool = True

    @field_validator("reason_codes", "source_artifact_ids")
    @classmethod
    def validate_sorted_sets(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("promotion task list values must be sorted and unique")
        return value


class SeedPromotionCompanyResult(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    seed_id: str = Field(min_length=1)
    status: SeedPromotionCompanyStatus
    source_artifact_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    financial_audit_run_id: str | None = None
    candidate_version_id: str | None = None

    @field_validator("source_artifact_ids", "reason_codes")
    @classmethod
    def validate_sorted_sets(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("promotion result list values must be sorted and unique")
        return value


class SeedPromotionRequest(AStockModel):
    schema_version: str = "seed-promotion-request-v1"
    seed_report_artifact_id: str = Field(min_length=1)
    max_seeds: int = Field(default=20, ge=1, le=60)
    reference_lookback_days: int = Field(default=140, ge=40, le=500)
    announcement_lookback_days: int = Field(default=90, ge=30, le=365)
    max_announcement_documents_per_company: int = Field(default=8, ge=1, le=20)
    live: bool = False


class SeedPromotionReport(AStockModel):
    schema_version: str = "seed-promotion-report-v1"
    promotion_id: str = Field(min_length=1)
    seed_report_artifact_id: str = Field(min_length=1)
    seed_report_object_hash: str = Field(pattern=_SHA256_PATTERN)
    as_of: AwareDatetime
    live: bool
    status: SeedPromotionStatus
    selected_seed_count: int = Field(ge=0)
    promoted_company_count: int = Field(ge=0)
    blocked_company_count: int = Field(ge=0)
    reused_candidate_count: int = Field(ge=0)
    company_results: list[SeedPromotionCompanyResult]
    tasks: list[SeedPromotionTask]
    candidate_input_release_id: str | None = None
    candidate_input_release_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    candidate_scan_id: str | None = None
    candidate_scan_status: str | None = None
    source_artifact_ids: list[str] = Field(default_factory=list)
    recommendation_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @field_validator("source_artifact_ids")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("promotion source artifacts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> SeedPromotionReport:
        promoted = sum(
            item.status is SeedPromotionCompanyStatus.PROMOTED for item in self.company_results
        )
        blocked = sum(
            item.status is SeedPromotionCompanyStatus.NEEDS_INFO for item in self.company_results
        )
        reused = sum(
            item.status is SeedPromotionCompanyStatus.REUSED_EXISTING_CANDIDATE
            for item in self.company_results
        )
        if (promoted, blocked, reused) != (
            self.promoted_company_count,
            self.blocked_company_count,
            self.reused_candidate_count,
        ):
            raise ValueError("promotion company counts do not reconcile")
        if self.selected_seed_count != len(self.company_results):
            raise ValueError("promotion selected seed count does not reconcile")
        if self.promoted_company_count == 0 and self.candidate_input_release_id is not None:
            raise ValueError("promotion cannot publish an input release without promoted companies")
        if self.candidate_scan_id is not None and self.candidate_input_release_id is None:
            raise ValueError("candidate scan requires an input release")
        return self


__all__ = [
    "SeedPromotionCompanyResult",
    "SeedPromotionCompanyStatus",
    "SeedPromotionReport",
    "SeedPromotionRequest",
    "SeedPromotionStatus",
    "SeedPromotionTask",
]
