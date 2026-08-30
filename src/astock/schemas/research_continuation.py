"""Durable same-request continuation contracts for current company research."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

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
    FAILED = "FAILED"


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
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


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
    capture_artifact_ids: list[str] = Field(default_factory=list)
    failure_code: str | None = Field(default=None, min_length=1)
    private_material_required: bool = False
    broker_execution_allowed: Literal[False] = False

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
    automatic_rounds_completed: int = Field(ge=1, le=5)
    acquisition_report_artifact_ids: list[str] = Field(min_length=1)
    current_acquisition_report_artifact_id: str = Field(min_length=1)
    automatic_resolution_artifact_ids: list[str] = Field(default_factory=list)
    external_tasks: list[CurrentResearchExternalTask] = Field(default_factory=list)
    team_plan_id: str | None = None
    readiness_report_artifact_id: str | None = None
    manual_actions: list[ManualResearchAction] = Field(default_factory=list)
    automatic_budget_exhausted: bool = False
    private_material_required: bool = False
    investor_view_allowed: bool = False
    formal_recommendation_allowed: bool = False
    same_request_continuation_required: Literal[True] = True
    manual_escalation_after_automatic_exhaustion: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

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
        if self.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED:
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
                or not self.formal_recommendation_allowed
            ):
                raise ValueError("READY_FOR_INVESTOR_VIEW requires a complete readiness lineage")
        elif self.status is CurrentResearchContinuationStatus.OBSERVATION_ONLY_FOR_INVESTOR_VIEW:
            if (
                unresolved
                or self.team_plan_id is None
                or self.readiness_report_artifact_id is None
                or self.manual_actions
                or not self.investor_view_allowed
                or self.formal_recommendation_allowed
            ):
                raise ValueError(
                    "OBSERVATION_ONLY_FOR_INVESTOR_VIEW requires complete non-formal lineage"
                )
        elif self.investor_view_allowed or self.formal_recommendation_allowed:
            raise ValueError("only investor-view terminal states may allow an investor answer")
        if self.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT:
            if not self.manual_actions or not (
                self.automatic_budget_exhausted or self.private_material_required
            ):
                raise ValueError(
                    "NEEDS_USER_INPUT requires exhausted automatic channels or private material"
                )
        elif self.automatic_budget_exhausted or self.private_material_required:
            raise ValueError("manual-escalation flags are valid only for NEEDS_USER_INPUT")
        return self


class CurrentResearchEvidenceBinding(AStockModel):
    schema_version: str = "current-research-evidence-binding-v1"
    continuation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    capture_artifact_id: str = Field(min_length=1)
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "CurrentResearchAutomaticResolution",
    "CurrentResearchContinuation",
    "CurrentResearchContinuationRequest",
    "CurrentResearchContinuationStatus",
    "CurrentResearchEvidenceBinding",
    "CurrentResearchExternalTask",
    "ExternalResearchTaskStatus",
]
