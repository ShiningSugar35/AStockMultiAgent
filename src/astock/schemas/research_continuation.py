"""Durable same-request continuation contracts for current company research."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from astock.schemas.base import AStockModel
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import (
    AcquisitionCapability,
    ExternalAuthority,
    ManualResearchAction,
)


class CurrentResearchContinuationStatus(StrEnum):
    AUTO_RESOLUTION_REQUIRED = "AUTO_RESOLUTION_REQUIRED"
    TEAM_RESEARCH_REQUIRED = "TEAM_RESEARCH_REQUIRED"
    READY_FOR_INVESTOR_VIEW = "READY_FOR_INVESTOR_VIEW"
    OBSERVATION_ONLY_FOR_INVESTOR_VIEW = "OBSERVATION_ONLY_FOR_INVESTOR_VIEW"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    PUBLIC_DATA_UNAVAILABLE = "PUBLIC_DATA_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FAILED = "FAILED"
    EXTERNAL_COMPLETION_UNKNOWN = "EXTERNAL_COMPLETION_UNKNOWN"


class ExternalResearchTaskStatus(StrEnum):
    PENDING = "PENDING"
    EVIDENCE_BOUND = "EVIDENCE_BOUND"
    RESOLVED = "RESOLVED"


class CurrentResearchContinuationRequest(AStockModel):
    schema_version: str = "current-research-continuation-request-v1"
    request_id: str = Field(min_length=1, max_length=200)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    lookback_days: int | None = Field(default=None, ge=30, le=730)
    planner_plan_artifact_id: str | None = Field(default=None, min_length=1)
    automatic_resolution_budget_seconds: int = Field(default=1800, ge=60, le=7200)
    max_automatic_rounds: int = Field(default=3, ge=1, le=5)
    max_transport_attempts: int = Field(default=64, ge=1, le=512)
    paper_ledger_write_allowed: Literal[False] = False


class CurrentResearchExternalTask(AStockModel):
    schema_version: str = "current-research-external-task-v1"
    task_id: str = Field(min_length=1)
    capability: AcquisitionCapability
    research_question: str = Field(min_length=1)
    preferred_authorities: list[ExternalAuthority] = Field(min_length=1)
    status: ExternalResearchTaskStatus = ExternalResearchTaskStatus.PENDING
    capture_artifact_ids: list[str] = Field(default_factory=list)
    automatic_rounds_attempted: int = Field(default=1, ge=1, le=5)
    last_failure_code: str | None = None
    private_material_required: bool = False
    external_operation_id: str | None = Field(default=None, min_length=1)
    dispatch_id: str | None = Field(default=None, min_length=1)
    scheduler_run_id: str | None = Field(default=None, min_length=1)
    resolution_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    web_search_allowed: Literal[True] = True
    manual_user_action_required: Literal[False] = False

    @field_validator("preferred_authorities", "capture_artifact_ids")
    @classmethod
    def validate_sorted_unique(cls, value: list[object]) -> list[object]:
        if value != sorted(set(value), key=str):
            raise ValueError("continuation task authority/artifact lists must be sorted and unique")
        return value


class CurrentResearchAutomaticResolution(AStockModel):
    """One Agent-owned automatic evidence-resolution result for a continuation task."""

    schema_version: str = "current-research-automatic-resolution-v1"
    continuation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    external_operation_id: str | None = Field(default=None, min_length=1)
    dispatch_id: str | None = Field(default=None, min_length=1)
    capture_artifact_ids: list[str] = Field(default_factory=list)
    failure_code: str | None = Field(default=None, min_length=1)
    private_material_required: bool = False

    @field_validator("capture_artifact_ids")
    @classmethod
    def validate_capture_artifacts(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("automatic-resolution capture artifacts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> CurrentResearchAutomaticResolution:
        if bool(self.capture_artifact_ids) == bool(self.failure_code):
            raise ValueError(
                "automatic resolution requires either frozen captures or one failure code"
            )
        if self.private_material_required and self.capture_artifact_ids:
            raise ValueError("private-material escalation cannot also bind public captures")
        return self


class PublicRecoveryRoute(BaseModel):
    """Actual bounded responses, not a model's assertion that the public web was exhausted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    capability: AcquisitionCapability
    route_id: str = Field(min_length=1, max_length=300)
    route_kind: Literal["PROVIDER", "WEB_QUERY"]
    classification: Literal[
        "NO_NEW_EVIDENCE", "EVIDENCE_RETURNED", "FAILED_OR_BLOCKED",
        "COMPLETION_UNKNOWN", "NOT_ATTEMPTED", "EVIDENCE_UNVERIFIABLE",
    ]
    attempt_count: int = Field(ge=0, le=1024)
    observed_record_count: int = Field(default=0, ge=0)
    new_source_object_count: int = Field(default=0, ge=0)
    response_codes: tuple[str, ...] = Field(default=(), max_length=128)
    evidence_artifact_ids: tuple[str, ...] = Field(default=(), max_length=128)
    source_object_hashes: tuple[str, ...] = Field(default=(), max_length=512)
    observed_at: AwareDatetime | None = None
    uncovered_impact: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def verified_empty_requires_evidence(self) -> PublicRecoveryRoute:
        if self.classification == "NO_NEW_EVIDENCE" and (
            not self.attempt_count or not self.evidence_artifact_ids
            or not self.source_object_hashes or self.observed_record_count != 0
        ):
            raise ValueError("empty route requires a verified actual zero-result response")
        return self


class PublicRecoveryCoverage(BaseModel):
    """A projection over existing acquisition/Web receipts; no additional persistence ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    continuation_id: str
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    started_at: AwareDatetime
    acquisition_report_artifact_id: str
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    missing_capabilities: tuple[AcquisitionCapability, ...] = Field(max_length=5)
    routes: tuple[PublicRecoveryRoute, ...] = Field(max_length=200)
    source_object_hashes: tuple[str, ...] = Field(max_length=2048)
    truncated: bool = False
    whole_public_web_exhausted: Literal[False] = False
    conclusion_authority_allowed: Literal[False] = False

    @model_validator(mode="after")
    def unique_routes(self) -> PublicRecoveryCoverage:
        keys = [(route.capability, route.route_id) for route in self.routes]
        if len(keys) != len(set(keys)):
            raise ValueError("recovery route identities must be unique")
        if any(route.capability not in self.missing_capabilities for route in self.routes):
            raise ValueError("recovery route is outside the current evidence gaps")
        return self

    def exhausted_capabilities(self) -> frozenset[AcquisitionCapability]:
        if self.truncated:
            return frozenset()
        return frozenset(
            capability for capability in self.missing_capabilities
            if (routes := [route for route in self.routes if route.capability is capability])
            and all(route.classification == "NO_NEW_EVIDENCE" for route in routes)
        )

    def all_applicable_routes_completed_empty(self) -> bool:
        return bool(self.missing_capabilities) and (
            set(self.missing_capabilities) == self.exhausted_capabilities()
        )


class CurrentResearchContinuation(AStockModel):
    schema_version: str = "current-research-continuation-v1"
    continuation_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    lookback_days: int | None = Field(default=None, ge=30, le=730)
    planner_plan_artifact_id: str | None = Field(default=None, min_length=1)
    started_at: AwareDatetime
    deadline_at: AwareDatetime
    status: CurrentResearchContinuationStatus
    automatic_resolution_budget_seconds: int = Field(ge=60, le=7200)
    max_automatic_rounds: int = Field(ge=1, le=5)
    max_transport_attempts: int = Field(default=64, ge=1, le=512)
    automatic_rounds_completed: int = Field(ge=1, le=5)
    acquisition_report_artifact_ids: list[str] = Field(min_length=1)
    current_acquisition_report_artifact_id: str = Field(min_length=1)
    automatic_resolution_artifact_ids: list[str] = Field(default_factory=list)
    external_tasks: list[CurrentResearchExternalTask] = Field(default_factory=list)
    team_plan_id: str | None = None
    readiness_report_artifact_id: str | None = None
    manual_actions: list[ManualResearchAction] = Field(default_factory=list)
    automatic_budget_exhausted: bool = False
    recovery_stop_reason: Literal[
        "TIME_BUDGET_EXHAUSTED", "ATTEMPT_BUDGET_EXHAUSTED", "LEGACY_BUDGET_EXHAUSTED",
    ] | None = None
    public_recovery_coverage: PublicRecoveryCoverage | None = None
    private_material_required: bool = False
    investor_view_allowed: bool = False
    full_research_input_ready: bool = False
    same_request_continuation_required: Literal[True] = True
    manual_escalation_after_automatic_exhaustion: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False

    @field_validator("acquisition_report_artifact_ids", "automatic_resolution_artifact_ids")
    @classmethod
    def validate_artifact_history(cls, value: list[str]) -> list[str]:
        if value != list(dict.fromkeys(value)):
            raise ValueError("continuation artifact histories must be ordered and unique")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> CurrentResearchContinuation:
        if self.deadline_at <= self.started_at:
            raise ValueError("continuation deadline must be after start")
        if self.current_acquisition_report_artifact_id not in self.acquisition_report_artifact_ids:
            raise ValueError("current acquisition report must be present in history")
        task_ids = [item.task_id for item in self.external_tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("continuation external task ids must be unique")
        unresolved = [
            item
            for item in self.external_tasks
            if item.status is not ExternalResearchTaskStatus.RESOLVED
        ]
        if self.status in {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        }:
            if not unresolved or self.manual_actions or self.team_plan_id is not None:
                raise ValueError(
                    "AUTO_RESOLUTION_REQUIRED must retain only automatic evidence gaps"
                )
        if self.status is CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED:
            if unresolved or self.team_plan_id is None or self.manual_actions:
                raise ValueError("TEAM_RESEARCH_REQUIRED requires a gap-free company team plan")
        if self.status is CurrentResearchContinuationStatus.READY_FOR_INVESTOR_VIEW:
            if (
                unresolved
                or self.team_plan_id is None
                or self.readiness_report_artifact_id is None
                or self.manual_actions
                or not self.investor_view_allowed
                or not self.full_research_input_ready
            ):
                raise ValueError("READY_FOR_INVESTOR_VIEW requires a complete readiness lineage")
        elif self.status is CurrentResearchContinuationStatus.OBSERVATION_ONLY_FOR_INVESTOR_VIEW:
            if (
                unresolved
                or self.team_plan_id is None
                or self.readiness_report_artifact_id is None
                or self.manual_actions
                or not self.investor_view_allowed
                or self.full_research_input_ready
            ):
                raise ValueError(
                    "OBSERVATION_ONLY_FOR_INVESTOR_VIEW requires complete non-formal lineage"
                )
        elif self.investor_view_allowed or self.full_research_input_ready:
            raise ValueError("only investor-view terminal states may expose completed research")
        self._validate_recovery_state(bool(unresolved))
        return self

    def _validate_recovery_state(self, has_unresolved: bool) -> None:
        if self.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT:
            if not self.manual_actions or not (
                self.automatic_budget_exhausted or self.private_material_required
            ):
                raise ValueError(
                    "NEEDS_USER_INPUT requires exhausted automatic channels or private material"
                )
        elif self.status in {
            CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE,
            CurrentResearchContinuationStatus.BUDGET_EXHAUSTED,
        }:
            if (
                not has_unresolved
                or self.private_material_required
                or self.manual_actions
                or self.team_plan_id is not None
            ):
                raise ValueError("public recovery exhaustion cannot request private user material")
            if (
                self.status is CurrentResearchContinuationStatus.BUDGET_EXHAUSTED
                and (not self.automatic_budget_exhausted or self.recovery_stop_reason is None)
            ):
                raise ValueError("budget exhaustion requires its actual limit reason")
            if self.status is CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE and (
                self.automatic_budget_exhausted or self.public_recovery_coverage is None
                or not self.public_recovery_coverage.all_applicable_routes_completed_empty()
            ):
                raise ValueError("public unavailability requires completed bounded route evidence")
        elif self.automatic_budget_exhausted or self.private_material_required:
            raise ValueError("recovery flags require a corresponding terminal gap state")
        if self.public_recovery_coverage is not None and (
            self.public_recovery_coverage.request_id != self.request_id
            or self.public_recovery_coverage.continuation_id != self.continuation_id
            or self.public_recovery_coverage.company_id != self.company_id
            or self.public_recovery_coverage.market is not self.market
        ):
            raise ValueError("public recovery evidence belongs to another request or security")
        if self.recovery_stop_reason is not None and not self.automatic_budget_exhausted:
            raise ValueError("a budget stop reason requires an exhausted budget")


class CurrentResearchEvidenceBinding(AStockModel):
    schema_version: str = "current-research-evidence-binding-v1"
    continuation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    capture_artifact_id: str = Field(min_length=1)


__all__ = [
    "PublicRecoveryCoverage",
    "PublicRecoveryRoute",
    "CurrentResearchAutomaticResolution",
    "CurrentResearchContinuation",
    "CurrentResearchContinuationRequest",
    "CurrentResearchContinuationStatus",
    "CurrentResearchEvidenceBinding",
    "CurrentResearchExternalTask",
    "ExternalResearchTaskStatus",
]
