"""Contracts for bounded current-research execution and incremental target state."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class CanonicalMarketSnapshot(AStockModel):
    schema_version: str = "canonical-market-snapshot-v1"
    provider_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    price: Decimal | None = None
    pe_ttm: Decimal | None = None
    pb_mrq: Decimal | None = None
    market_cap_cny: Decimal | None = None
    turnover_cny: Decimal | None = None
    source_fields: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_market_snapshot(self) -> CanonicalMarketSnapshot:
        if self.missing_fields != sorted(set(self.missing_fields)):
            raise ValueError("canonical market missing fields must be sorted and unique")
        if any(
            value is not None and value < 0 for value in (self.market_cap_cny, self.turnover_cny)
        ):
            raise ValueError("market-cap and turnover cannot be negative")
        if self.price is not None and self.price <= 0:
            raise ValueError("market price must be positive")
        return self


class StandardizedFinancialMetrics(AStockModel):
    schema_version: str = "standardized-financial-metrics-v1"
    company_id: str = Field(min_length=1)
    industry_profile: str = Field(min_length=1)
    metrics: dict[str, Decimal] = Field(default_factory=dict)
    missing_critical_metrics: list[str] = Field(default_factory=list)
    ignored_non_numeric_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_financial_metrics(self) -> StandardizedFinancialMetrics:
        if self.missing_critical_metrics != sorted(set(self.missing_critical_metrics)):
            raise ValueError("missing critical metrics must be sorted and unique")
        if self.ignored_non_numeric_fields != sorted(set(self.ignored_non_numeric_fields)):
            raise ValueError("ignored financial fields must be sorted and unique")
        return self


class ResearchTargetState(StrEnum):
    POTENTIAL = "POTENTIAL"
    RESEARCHED = "RESEARCHED"
    WAITING = "WAITING"
    REVIEW_DUE = "REVIEW_DUE"
    SUSPENDED = "SUSPENDED"
    EXITED = "EXITED"


class ResearchTargetRecord(AStockModel):
    schema_version: str = "research-target-record-v1"
    target_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    state: ResearchTargetState
    source_reason: str = Field(min_length=1)
    source_artifact_id: str | None = None
    latest_result_artifact_id: str | None = None
    dependency_fingerprint: str = Field(min_length=1)
    module_versions: dict[str, str] = Field(default_factory=dict)
    triggers: list[str] = Field(default_factory=list)
    priority: int = 0
    invalidation_reason: str | None = None
    last_review_at: AwareDatetime | None = None
    next_review_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_target(self) -> ResearchTargetRecord:
        if self.updated_at < self.created_at:
            raise ValueError("research target cannot be updated before creation")
        if self.triggers != sorted(set(self.triggers)):
            raise ValueError("research target triggers must be sorted and unique")
        if any(not key.strip() or not value.strip() for key, value in self.module_versions.items()):
            raise ValueError("research target module versions cannot contain blank keys or values")
        return self


class IncrementalRefreshPlan(AStockModel):
    schema_version: str = "incremental-refresh-plan-v1"
    target_id: str = Field(min_length=1)
    changed_modules: list[str] = Field(default_factory=list)
    rerun_modules: list[str] = Field(default_factory=list)
    unchanged_modules: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_refresh(self) -> IncrementalRefreshPlan:
        for values in (
            self.changed_modules,
            self.rerun_modules,
            self.unchanged_modules,
            self.reason_codes,
        ):
            if values != sorted(set(values)):
                raise ValueError("incremental refresh lists must be sorted and unique")
        if not set(self.changed_modules) <= set(self.rerun_modules):
            raise ValueError("changed modules must be part of the rerun closure")
        return self


class ResearchValidationProof(AStockModel):
    """Cached proof that one immutable artifact passed one exact validator context."""

    schema_version: str = "research-validation-proof-v1"
    proof_id: str = Field(min_length=1)
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_scope: str = Field(min_length=1)
    fact_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = "PASS"

    @model_validator(mode="after")
    def validate_proof(self) -> ResearchValidationProof:
        if self.status != "PASS":
            raise ValueError("validation proof can persist only successful validation")
        return self


class ResearchTaskCategory(StrEnum):
    NETWORK = "NETWORK"
    CPU = "CPU"
    LLM = "LLM"
    WRITE = "WRITE"


class ResearchSchedulerRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ResearchSchedulerTaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ResearchSchedulerRun(AStockModel):
    schema_version: str = "research-scheduler-run-v1"
    run_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    status: ResearchSchedulerRunStatus
    generation: int = Field(ge=1)
    deadline_at: AwareDatetime
    overall_deadline_enforced: bool = True
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_run(self) -> ResearchSchedulerRun:
        if self.deadline_at <= self.started_at:
            raise ValueError("research scheduler deadline must follow its start time")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("research scheduler cannot finish before it starts")
        return self


class ResearchSchedulerTask(AStockModel):
    schema_version: str = "research-scheduler-task-v1"
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    candidate_id: str | None = None
    category: ResearchTaskCategory
    status: ResearchSchedulerTaskStatus
    dependencies: list[str] = Field(default_factory=list)
    input_fingerprint: str = Field(min_length=1)
    generation: int = Field(ge=1)
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    result_artifact_id: str | None = None
    error_code: str | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_task(self) -> ResearchSchedulerTask:
        if self.dependencies != sorted(set(self.dependencies)):
            raise ValueError("research scheduler dependencies must be sorted and unique")
        if self.finished_at is not None and self.started_at is None:
            raise ValueError("a finished research task must have a start time")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("research scheduler task cannot finish before it starts")
        if self.status is ResearchSchedulerTaskStatus.COMPLETED and not self.result_artifact_id:
            raise ValueError("completed research tasks require a result artifact")
        return self


class LlmTakeoverResultStatus(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class LlmTakeoverPacket(AStockModel):
    schema_version: str = "llm-takeover-packet-v1"
    packet_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    candidate_id: str | None = None
    failure_code: str = Field(min_length=1)
    trusted_artifact_ids: list[str] = Field(default_factory=list)
    attempted_actions: list[str] = Field(default_factory=list)
    missing_requirement: str = Field(min_length=1)
    allowed_write_paths: list[str] = Field(default_factory=list)
    expected_output_schema: str = Field(min_length=1)
    dependency_fingerprint: str = Field(min_length=1)
    generation: int = Field(ge=1)
    round_index: int = Field(ge=1, le=2)
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def validate_packet(self) -> LlmTakeoverPacket:
        for values in (
            self.trusted_artifact_ids,
            self.attempted_actions,
            self.allowed_write_paths,
        ):
            if values != sorted(set(values)):
                raise ValueError("LLM takeover lists must be sorted and unique")
        if self.deadline_at <= self.created_at:
            raise ValueError("LLM takeover requires remaining request budget")
        return self


class LlmTakeoverResult(AStockModel):
    schema_version: str = "llm-takeover-result-v1"
    packet_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    dependency_fingerprint: str = Field(min_length=1)
    status: LlmTakeoverResultStatus
    result_artifact_id: str | None = None
    validation_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> LlmTakeoverResult:
        if self.validation_codes != sorted(set(self.validation_codes)):
            raise ValueError("LLM takeover validation codes must be sorted and unique")
        if self.status is LlmTakeoverResultStatus.RESOLVED and not self.result_artifact_id:
            raise ValueError("resolved LLM takeover requires a result artifact")
        if self.status is LlmTakeoverResultStatus.UNRESOLVED and self.result_artifact_id:
            raise ValueError("unresolved LLM takeover cannot carry a result artifact")
        return self


__all__ = [
    "CanonicalMarketSnapshot",
    "IncrementalRefreshPlan",
    "LlmTakeoverPacket",
    "LlmTakeoverResult",
    "LlmTakeoverResultStatus",
    "ResearchSchedulerRun",
    "ResearchSchedulerRunStatus",
    "ResearchSchedulerTask",
    "ResearchSchedulerTaskStatus",
    "ResearchTargetRecord",
    "ResearchTargetState",
    "ResearchValidationProof",
    "StandardizedFinancialMetrics",
    "ResearchTaskCategory",
]
