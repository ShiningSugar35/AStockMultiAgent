"""Temporal non-interference and knowledge-cutoff diagnostic contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class TemporalOperationKind(StrEnum):
    SOURCE = "SOURCE"
    WINDOW = "WINDOW"
    RESAMPLE = "RESAMPLE"
    ASOF_JOIN = "ASOF_JOIN"
    RETRIEVAL = "RETRIEVAL"
    TRANSFORM = "TRANSFORM"
    DECISION = "DECISION"


class TemporalAuditStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class KnowledgeCutoffDiagnosticStatus(StrEnum):
    EVALUABLE = "EVALUABLE"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class TemporalPipelineNode(AStockModel):
    node_id: str = Field(min_length=1)
    operation_kind: TemporalOperationKind
    dependency_ids: list[str] = Field(default_factory=list)
    reference_time: AwareDatetime
    available_at: AwareDatetime
    value_independent_availability: bool = True

    @model_validator(mode="after")
    def validate_node(self) -> TemporalPipelineNode:
        if self.dependency_ids != sorted(set(self.dependency_ids)):
            raise ValueError("temporal node dependency ids must be sorted and unique")
        if self.node_id in self.dependency_ids:
            raise ValueError("temporal node cannot depend on itself")
        return self


class TemporalNonInterferenceRequest(AStockModel):
    schema_version: str = "temporal-non-interference-request-v1"
    pipeline_id: str = Field(min_length=1)
    decision_time: AwareDatetime
    nodes: list[TemporalPipelineNode] = Field(min_length=1)
    output_node_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pipeline(self) -> TemporalNonInterferenceRequest:
        node_ids = [item.node_id for item in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("temporal pipeline node ids must be unique")
        if self.output_node_ids != sorted(set(self.output_node_ids)):
            raise ValueError("temporal output node ids must be sorted and unique")
        if not set(self.output_node_ids) <= set(node_ids):
            raise ValueError("temporal output nodes must exist in the pipeline")
        return self


class TemporalNodeAudit(AStockModel):
    node_id: str = Field(min_length=1)
    operation_kind: TemporalOperationKind
    reference_time: AwareDatetime
    declared_available_at: AwareDatetime
    effective_available_at: AwareDatetime
    dependency_count: int = Field(ge=0)
    finding_codes: list[str] = Field(default_factory=list)


class TemporalNonInterferenceReport(AStockModel):
    schema_version: str = "temporal-non-interference-report-v1"
    report_id: str = Field(min_length=1)
    pipeline_id: str = Field(min_length=1)
    decision_time: AwareDatetime
    status: TemporalAuditStatus
    node_count: int = Field(ge=1)
    edge_count: int = Field(ge=0)
    checked_value_independent_fragment: bool
    linear_time_contract: Literal[True] = True
    node_audits: list[TemporalNodeAudit]
    finding_codes: list[str]
    production_admission_allowed: Literal[False] = False
    automatic_skill_modification_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


class KnowledgeCutoffAlphaPeriod(AStockModel):
    period_id: str = Field(min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime
    alpha: float = Field(allow_inf_nan=False)
    independent_decision_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_period(self) -> KnowledgeCutoffAlphaPeriod:
        if self.period_end < self.period_start:
            raise ValueError("knowledge-cutoff period end cannot precede its start")
        return self


class KnowledgeCutoffDiagnosticRequest(AStockModel):
    schema_version: str = "knowledge-cutoff-diagnostic-request-v1"
    method_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime
    periods: list[KnowledgeCutoffAlphaPeriod] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_periods(self) -> KnowledgeCutoffDiagnosticRequest:
        period_ids = [item.period_id for item in self.periods]
        if period_ids != sorted(set(period_ids)):
            raise ValueError("knowledge-cutoff period ids must be sorted and unique")
        return self


class KnowledgeCutoffDiagnosticReport(AStockModel):
    schema_version: str = "knowledge-cutoff-diagnostic-report-v1"
    report_id: str = Field(min_length=1)
    method_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime
    status: KnowledgeCutoffDiagnosticStatus
    pre_cutoff_period_count: int = Field(ge=0)
    post_cutoff_period_count: int = Field(ge=0)
    crossing_cutoff_period_count: int = Field(ge=0)
    pre_cutoff_decision_count: int = Field(ge=0)
    post_cutoff_decision_count: int = Field(ge=0)
    pre_cutoff_weighted_alpha: float | None = Field(default=None, allow_inf_nan=False)
    post_cutoff_weighted_alpha: float | None = Field(default=None, allow_inf_nan=False)
    alpha_decay_pre_minus_post: float | None = Field(default=None, allow_inf_nan=False)
    alpha_retention_ratio: float | None = Field(default=None, allow_inf_nan=False)
    finding_codes: list[str]
    deployment_claim_allowed: Literal[False] = False
    production_admission_allowed: Literal[False] = False
    automatic_skill_modification_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


class TruncationInvarianceResult(AStockModel):
    schema_version: str = "truncation-invariance-result-v1"
    checked_cutoff_count: int = Field(ge=0)
    exhaustive: bool
    drift_cutoffs: list[int] = Field(default_factory=list)
    invariant: bool


__all__ = [
    "KnowledgeCutoffAlphaPeriod",
    "KnowledgeCutoffDiagnosticReport",
    "KnowledgeCutoffDiagnosticRequest",
    "KnowledgeCutoffDiagnosticStatus",
    "TemporalAuditStatus",
    "TemporalNodeAudit",
    "TemporalNonInterferenceReport",
    "TemporalNonInterferenceRequest",
    "TemporalOperationKind",
    "TemporalPipelineNode",
    "TruncationInvarianceResult",
]
