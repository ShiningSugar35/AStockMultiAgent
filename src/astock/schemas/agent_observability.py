"""Durable Agent routing, execution, and data-alignment observability schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel


class AgentTaskStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NEEDS_INFO = "NEEDS_INFO"
    FAILED = "FAILED"


class AgentTaskObservationRequest(AStockModel):
    schema_version: str = "agent-task-observation-request-v1"
    task_id: str = Field(min_length=1)
    task_status: AgentTaskStatus
    eligible_skill_ids: list[str] = Field(default_factory=list)
    selected_skill_ids: list[str] = Field(default_factory=list)
    completed_skill_ids: list[str] = Field(default_factory=list)
    expected_skill_ids: list[str] = Field(default_factory=list)
    duration_ms: int = Field(ge=0)
    finding_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_observation(self) -> AgentTaskObservationRequest:
        _validate_skill_lists(self)
        return self


class AgentTaskObservation(AgentTaskObservationRequest):
    schema_version: str = "agent-task-observation-v1"
    observation_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_observation_record(self) -> AgentTaskObservation:
        _validate_skill_lists(self)
        return self


def _validate_skill_lists(observation: AgentTaskObservationRequest) -> None:
    for values in (
        observation.eligible_skill_ids,
        observation.selected_skill_ids,
        observation.completed_skill_ids,
        observation.expected_skill_ids,
        observation.finding_codes,
    ):
        if values != sorted(set(values)):
            raise ValueError("Agent observation lists must be sorted and unique")
    eligible = set(observation.eligible_skill_ids)
    selected = set(observation.selected_skill_ids)
    completed = set(observation.completed_skill_ids)
    if not selected <= eligible:
        raise ValueError("selected Agent Skills must be eligible for the task")
    if not completed <= selected:
        raise ValueError("completed Agent Skills must be selected for the task")


class AgentSkillRoutingSummary(AStockModel):
    skill_id: str = Field(min_length=1)
    eligible_task_count: int = Field(ge=0)
    selected_task_count: int = Field(ge=0)
    completed_task_count: int = Field(ge=0)
    expected_task_count: int = Field(ge=0)
    selection_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    execution_hit_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    labeled_precision: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    labeled_recall: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class AgentTaskPerformanceSummary(AStockModel):
    observed_task_count: int = Field(ge=0)
    completed_task_count: int = Field(ge=0)
    needs_info_task_count: int = Field(ge=0)
    failed_task_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    mean_duration_ms: int = Field(ge=0)
    p50_duration_ms: int = Field(ge=0)
    p95_duration_ms: int = Field(ge=0)
    research_run_count: int = Field(ge=0)
    research_run_complete_count: int = Field(ge=0)
    research_run_mean_wall_time_ms: int = Field(ge=0)
    research_run_p95_wall_time_ms: int = Field(ge=0)
    research_run_mean_provider_calls: float = Field(ge=0, allow_inf_nan=False)
    research_run_cache_hit_rate: float = Field(ge=0, le=1, allow_inf_nan=False)


class AgentDataAlignmentSummary(AStockModel):
    canonical_manifest_count: int = Field(ge=0)
    dual_source_evaluable_count: int = Field(ge=0)
    dual_source_pass_count: int = Field(ge=0)
    data_alignment_pass_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    mean_timestamp_coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    mean_close_relative_p95: float = Field(ge=0, allow_inf_nan=False)
    mean_ohlc_relative_p95: float = Field(ge=0, allow_inf_nan=False)
    mean_volume_relative_p95: float = Field(ge=0, allow_inf_nan=False)
    worst_close_relative_p95: float = Field(ge=0, allow_inf_nan=False)
    current_snapshot_only: bool = True


class AgentObservabilityReport(AStockModel):
    schema_version: str = "agent-observability-report-v1"
    report_id: str = Field(min_length=1)
    lookback_days: int = Field(ge=0)
    task_window_start: str | None = None
    task_window_end: str
    routing_labeled_task_count: int = Field(ge=0)
    routing_micro_precision: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    routing_micro_recall: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    selected_skill_slot_count: int = Field(ge=0)
    completed_skill_slot_count: int = Field(ge=0)
    skill_execution_hit_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    production_skill_usage_event_count: int = Field(ge=0)
    production_skill_useful_event_count: int = Field(ge=0)
    production_skill_useful_hit_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    skill_summaries: list[AgentSkillRoutingSummary]
    task_performance: AgentTaskPerformanceSummary
    data_alignment: AgentDataAlignmentSummary
    finding_codes: list[str] = Field(default_factory=list)
    automatic_skill_modification_allowed: Literal[False] = False


__all__ = [
    "AgentDataAlignmentSummary",
    "AgentObservabilityReport",
    "AgentSkillRoutingSummary",
    "AgentTaskObservation",
    "AgentTaskObservationRequest",
    "AgentTaskPerformanceSummary",
    "AgentTaskStatus",
]
