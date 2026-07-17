"""Strict contracts for versioned position monitoring and incremental reviews."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.knowledge import PositionAction
from astock.schemas.research import SpecialistCoverageStatus


class DecisionReferenceStatus(StrEnum):
    REGISTERED_ARTIFACT = "REGISTERED_ARTIFACT"
    USER_DECLARED_EXTERNAL = "USER_DECLARED_EXTERNAL"


class LifecycleSourceType(StrEnum):
    PRICE = "PRICE"
    FUNDAMENTAL = "FUNDAMENTAL"
    EVENT = "EVENT"
    MANUAL = "MANUAL"


class LifecycleCondition(AStockModel):
    rule_id: str = Field(min_length=1)
    signal_code: str = Field(min_length=1)
    action: PositionAction
    source_type: LifecycleSourceType
    description: str = Field(min_length=1)
    requires_new_evidence: bool = True
    hard_block: bool = False

    @model_validator(mode="after")
    def validate_action_safety(self) -> LifecycleCondition:
        if self.action is PositionAction.HOLD:
            raise ValueError("HOLD is the default and cannot be a trigger condition")
        if self.action is PositionAction.ADD and not self.requires_new_evidence:
            raise ValueError("ADD conditions must require new evidence")
        if self.hard_block and self.action not in {
            PositionAction.EXIT,
            PositionAction.REVIEW,
        }:
            raise ValueError("hard blocks can only trigger EXIT or REVIEW")
        return self


class LifecycleMetricDefinition(AStockModel):
    metric_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_metric_evidence(self) -> LifecycleMetricDefinition:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("lifecycle metric evidence ids must be unique")
        return self


class PositionLifecycleConfig(AStockModel):
    rules_version: str = Field(min_length=1)
    action_priority: list[PositionAction]
    base_action_confidence: dict[PositionAction, float]
    coverage_confidence_caps: dict[SpecialistCoverageStatus, float]
    conflict_hard_block_code: str = Field(min_length=1)
    invalidated_evidence_hard_block_code: str = Field(min_length=1)
    add_support_missing_code: str = Field(min_length=1)
    requires_user_confirmation: Literal[True] = True
    add_requires_new_evidence: Literal[True] = True

    @model_validator(mode="after")
    def validate_rule_completeness(self) -> PositionLifecycleConfig:
        expected = [
            PositionAction.EXIT,
            PositionAction.REVIEW,
            PositionAction.TRIM,
            PositionAction.ADD,
            PositionAction.HOLD,
        ]
        if self.action_priority != expected:
            raise ValueError("lifecycle action priority must be EXIT/REVIEW/TRIM/ADD/HOLD")
        if set(self.base_action_confidence) != set(PositionAction):
            raise ValueError("lifecycle confidence must cover every action")
        if set(self.coverage_confidence_caps) != set(SpecialistCoverageStatus):
            raise ValueError("lifecycle coverage caps must cover every status")
        if any(
            value < 0 or value > 1
            for value in (
                *self.base_action_confidence.values(),
                *self.coverage_confidence_caps.values(),
            )
        ):
            raise ValueError("lifecycle confidence values must be within 0..1")
        return self


class PositionPlanCreateRequest(AStockModel):
    position_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    decision_reference_status: DecisionReferenceStatus
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    memo_id: str = Field(min_length=1)
    as_of: AwareDatetime
    thesis_summary: str = Field(min_length=1)
    entry_assumptions: list[str] = Field(min_length=1)
    holding_horizon: str = Field(min_length=1)
    key_value_drivers: list[str] = Field(min_length=1)
    validation_metrics: list[LifecycleMetricDefinition] = Field(min_length=1)
    monitoring_sources: list[str] = Field(min_length=1)
    monitoring_cadence: dict[str, str]
    conditions: list[LifecycleCondition] = Field(min_length=1)
    manual_information_needs: list[str]
    next_review_at: AwareDatetime

    @model_validator(mode="after")
    def validate_monitoring_plan_request(self) -> PositionPlanCreateRequest:
        for label, values in (
            ("entry assumption", self.entry_assumptions),
            ("value driver", self.key_value_drivers),
            ("monitoring source", self.monitoring_sources),
            ("manual information need", self.manual_information_needs),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"position plan {label} values must be unique")
        metric_ids = [item.metric_id for item in self.validation_metrics]
        rule_ids = [item.rule_id for item in self.conditions]
        signal_codes = [item.signal_code for item in self.conditions]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("position plan metric ids must be unique")
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("position plan rule ids must be unique")
        if len(signal_codes) != len(set(signal_codes)):
            raise ValueError("position plan signal codes must be unique")
        if not any(item.action is PositionAction.EXIT for item in self.conditions):
            raise ValueError("position plans require at least one EXIT condition")
        if set(self.monitoring_cadence) != set(self.monitoring_sources):
            raise ValueError("every monitoring source requires exactly one cadence")
        if self.next_review_at <= self.as_of:
            raise ValueError("next review must follow the position plan as_of")
        return self


class HoldingRuleSignal(AStockModel):
    rule_id: str = Field(min_length=1)
    observed_value: str = Field(min_length=1)
    occurred_at: AwareDatetime
    evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_signal_evidence(self) -> HoldingRuleSignal:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("holding rule signal evidence ids must be unique")
        return self


class HoldingReviewRequest(AStockModel):
    plan_id: str = Field(min_length=1)
    from_as_of: AwareDatetime
    to_as_of: AwareDatetime
    added_evidence_ids: list[str]
    changed_claim_ids: list[str]
    invalidated_evidence_ids: list[str]
    unresolved_conflict_ids: list[str]
    signals: list[HoldingRuleSignal]

    @model_validator(mode="after")
    def validate_review_window_and_sets(self) -> HoldingReviewRequest:
        if self.to_as_of <= self.from_as_of:
            raise ValueError("holding review window must move forward")
        for label, values in (
            ("added evidence", self.added_evidence_ids),
            ("changed claim", self.changed_claim_ids),
            ("invalidated evidence", self.invalidated_evidence_ids),
            ("unresolved conflict", self.unresolved_conflict_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"holding review {label} ids must be unique")
        signal_rules = [item.rule_id for item in self.signals]
        if len(signal_rules) != len(set(signal_rules)):
            raise ValueError("holding review can trigger each rule at most once")
        return self


__all__ = [
    "DecisionReferenceStatus",
    "HoldingReviewRequest",
    "HoldingRuleSignal",
    "LifecycleCondition",
    "LifecycleMetricDefinition",
    "LifecycleSourceType",
    "PositionLifecycleConfig",
    "PositionPlanCreateRequest",
]
