"""Versioned contracts for the recoverable single-stock research runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.committee import CommitteeAssessment, CounterCaseDraft
from astock.schemas.knowledge_completion import KnowledgeSkillQuery, KnowledgeSkillSummary
from astock.schemas.market import Market
from astock.schemas.paper import PaperTradingClassification
from astock.schemas.research import (
    BaseCaseDraft,
    BaseCaseSection,
    ResearchFindingInput,
    SpecialistAdjustmentInput,
    SpecialistEvidenceRequest,
    SpecialistMetricInput,
)
from astock.schemas.serenity_v2 import SerenityMethodContractV2

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ResearchRunMode(StrEnum):
    RECORDED_INPUT = "RECORDED_INPUT"
    LIVE = "LIVE"


class ResearchRunStage(StrEnum):
    EVIDENCE = "EVIDENCE"
    FINANCIAL_INTEGRITY = "FINANCIAL_INTEGRITY"
    BASE_CASE = "BASE_CASE"
    SERENITY_DELTA = "SERENITY_DELTA"
    KNOWLEDGE_SKILL_DELTA = "KNOWLEDGE_SKILL_DELTA"
    COMMITTEE = "COMMITTEE"
    TRADING_CLASSIFICATION = "TRADING_CLASSIFICATION"
    TRADE_PROTOCOL = "TRADE_PROTOCOL"
    COMPLETE = "COMPLETE"


class ResearchRunStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    NEEDS_INFO = "NEEDS_INFO"
    COMPLETE = "COMPLETE"


class TradingClassificationStatus(StrEnum):
    READY = "READY"
    NEEDS_INFO = "NEEDS_INFO"


class RuntimeArtifactReference(AStockModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)


class TradingClassificationDraft(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    effective_from: AwareDatetime
    valid_until: AwareDatetime
    classification: PaperTradingClassification
    special_no_price_limit: bool
    corporate_action_baseline_artifact_id: str | None = None
    source_artifact_ids: list[str] = Field(min_length=1)
    status: TradingClassificationStatus = TradingClassificationStatus.READY
    reason_codes: list[str] = Field(default_factory=list)
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_draft(self) -> TradingClassificationDraft:
        if self.effective_from > self.as_of or self.as_of > self.valid_until:
            raise ValueError("trading classification as_of is outside its validity interval")
        if self.classification.instrument_id != f"{self.market.value}:{self.symbol}":
            raise ValueError("trading classification instrument identity mismatch")
        if self.source_artifact_ids != sorted(set(self.source_artifact_ids)):
            raise ValueError("trading classification source artifacts must be sorted and unique")
        if self.status is TradingClassificationStatus.READY:
            if self.reason_codes:
                raise ValueError("ready trading classification cannot carry reason codes")
            if not self.classification.suspension_status_verified:
                raise ValueError("ready trading classification requires verified suspension status")
        elif not self.reason_codes:
            raise ValueError("NEEDS_INFO trading classification requires reason codes")
        return self


class TradingClassificationRelease(AStockModel):
    schema_version: str = "trading-classification-release-v1"
    release_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    effective_from: AwareDatetime
    valid_until: AwareDatetime
    classification: PaperTradingClassification
    special_no_price_limit: bool
    corporate_action_baseline_artifact_id: str | None = None
    source_artifact_ids: list[str] = Field(min_length=1)
    source_object_hashes: list[str] = Field(min_length=1)
    status: TradingClassificationStatus
    reason_codes: list[str]
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_release(self) -> TradingClassificationRelease:
        if self.source_artifact_ids != sorted(set(self.source_artifact_ids)):
            raise ValueError("classification release source artifacts must be sorted and unique")
        if self.source_object_hashes != sorted(set(self.source_object_hashes)):
            raise ValueError("classification release source hashes must be sorted and unique")
        if self.effective_from > self.as_of or self.as_of > self.valid_until:
            raise ValueError("classification release as_of is outside its validity interval")
        if self.status is TradingClassificationStatus.READY and self.reason_codes:
            raise ValueError("ready classification release cannot carry reason codes")
        if self.status is TradingClassificationStatus.NEEDS_INFO and not self.reason_codes:
            raise ValueError("NEEDS_INFO classification release requires reason codes")
        return self


class TradingClassificationRecord(AStockModel):
    release: TradingClassificationRelease
    artifact_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    idempotent_replay: bool


class ResearchRunRouteDraft(AStockModel):
    thesis_tags: list[str] = Field(default_factory=list)
    industry_tags: list[str] = Field(default_factory=list)
    event_tags: list[str] = Field(default_factory=list)
    horizon: str = Field(default="long", min_length=1)
    available_inputs: list[str] = Field(default_factory=list)
    available_frequencies: list[str] = Field(default_factory=list)
    explicit_skill_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_route(self) -> ResearchRunRouteDraft:
        for values in (
            self.thesis_tags,
            self.industry_tags,
            self.event_tags,
            self.available_inputs,
            self.available_frequencies,
            self.explicit_skill_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("research runtime route values must be unique")
        return self


class ResearchRunSpecialistDeltaDraft(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    incremental_findings: list[ResearchFindingInput] = Field(default_factory=list)
    base_case_corrections: list[ResearchFindingInput] = Field(default_factory=list)
    industry_specific_metrics: list[SpecialistMetricInput] = Field(default_factory=list)
    additional_evidence_requests: list[SpecialistEvidenceRequest] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    confidence_delta: float = Field(default=0.0, ge=-0.25, le=0.25)
    valuation_adjustments: list[SpecialistAdjustmentInput] = Field(default_factory=list)
    risk_adjustments: list[SpecialistAdjustmentInput] = Field(default_factory=list)
    coverage_delta: dict[BaseCaseSection, float] = Field(default_factory=dict)
    method_contract: SerenityMethodContractV2 | None = None


class ResearchRunFrozenInputs(AStockModel):
    frozen_evidence_pack_artifact_id: str | None = None
    base_case_artifact_id: str | None = None
    specialist_route_artifact_id: str | None = None
    serenity_delta_artifact_id: str | None = None
    zhihu_delta_artifact_id: str | None = None
    research_memo_artifact_id: str | None = None
    financial_integrity_artifact_id: str | None = None


class ResearchRunRequest(AStockModel):
    schema_version: str = "research-run-request-v1"
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    mode: ResearchRunMode = ResearchRunMode.LIVE
    evidence_pack_artifact_id: str | None = None
    financial_audit_run_id: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    formal_historical: bool = False
    allow_approximated: bool = False
    base_case_draft: BaseCaseDraft | None = None
    route_draft: ResearchRunRouteDraft | None = None
    specialist_delta_drafts: list[ResearchRunSpecialistDeltaDraft] = Field(default_factory=list)
    frozen_inputs: ResearchRunFrozenInputs | None = None
    knowledge_run_id: str | None = None
    knowledge_query: KnowledgeSkillQuery | None = None
    committee_assessment: CommitteeAssessment | None = None
    counter_case: CounterCaseDraft | None = None
    trading_classification_artifact_id: str | None = None
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_request(self) -> ResearchRunRequest:
        if self.claim_ids != sorted(set(self.claim_ids)):
            raise ValueError("research run claim ids must be sorted and unique")
        skill_ids = [item.skill_id for item in self.specialist_delta_drafts]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("research run specialist drafts must have unique Skill ids")
        if (self.knowledge_run_id is None) != (self.knowledge_query is None):
            raise ValueError("knowledge run id and query must be provided together")
        return self


class KnowledgeSkillDelta(AStockModel):
    schema_version: str = "knowledge-skill-delta-v1"
    delta_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    knowledge_run_id: str = Field(min_length=1)
    registry_release_id: str = Field(min_length=1)
    registry_artifact_id: str = Field(min_length=1)
    registry_object_hash: str = Field(pattern=_SHA256_PATTERN)
    selection_result_hash: str = Field(pattern=_SHA256_PATTERN)
    selected_skills: list[KnowledgeSkillSummary]
    context_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    provider_latency_ms: int = Field(ge=0)
    provider_cache_hit: bool
    formal_committee_weight_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_delta(self) -> KnowledgeSkillDelta:
        ids = [item.final_skill_id for item in self.selected_skills]
        if len(ids) != len(set(ids)):
            raise ValueError("knowledge delta selected Skills must be unique")
        return self


class ResearchRunCheckpoint(AStockModel):
    stage: ResearchRunStage
    status: ResearchRunStatus
    artifact_ids: list[str] = Field(default_factory=list)
    duration_ms: int = Field(ge=0)
    cache_hit: bool
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> ResearchRunCheckpoint:
        if self.artifact_ids != sorted(set(self.artifact_ids)):
            raise ValueError("checkpoint artifact ids must be sorted and unique")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("checkpoint reason codes must be sorted and unique")
        return self


class ResearchRunPerformanceSummary(AStockModel):
    wall_time_ms: int = Field(ge=0)
    knowledge_top_k_latency_ms: int = Field(ge=0)
    context_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    estimated_token_limit: int = Field(ge=0)
    cache_hit_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)


class ResearchRunPlan(AStockModel):
    schema_version: str = "research-run-plan-v1"
    run_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    next_stage: ResearchRunStage
    missing_codes: list[str]
    reusable_artifact_ids: list[str]
    ledger_write_planned: Literal[False] = False
    broker_execution_planned: Literal[False] = False


class ResearchRunReport(AStockModel):
    schema_version: str = "research-run-report-v1"
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    mode: ResearchRunMode
    request_artifact_id: str = Field(min_length=1)
    request_object_hash: str = Field(pattern=_SHA256_PATTERN)
    previous_report_artifact_id: str | None = None
    status: ResearchRunStatus
    current_stage: ResearchRunStage
    checkpoints: list[ResearchRunCheckpoint]
    output_artifacts: dict[str, RuntimeArtifactReference]
    needs_info_codes: list[str]
    trade_protocol_outcome: str | None = None
    performance: ResearchRunPerformanceSummary
    paper_ledger_write_count: Literal[0] = 0
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> ResearchRunReport:
        if self.needs_info_codes != sorted(set(self.needs_info_codes)):
            raise ValueError("research run needs-info codes must be sorted and unique")
        if self.status is ResearchRunStatus.COMPLETE and self.needs_info_codes:
            raise ValueError("complete research run cannot carry NEEDS_INFO codes")
        if self.status is ResearchRunStatus.NEEDS_INFO and not self.needs_info_codes:
            raise ValueError("NEEDS_INFO research run requires reason codes")
        return self


class ResearchRunAudit(AStockModel):
    schema_version: str = "research-run-audit-v1"
    run_id: str = Field(min_length=1)
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    latest_report_artifact_id: str | None = None
    finding_codes: list[str]
    paper_ledger_write_count: Literal[0] = 0
    broker_execution_allowed: Literal[False] = False


class ResearchRunBenchmark(AStockModel):
    schema_version: str = "research-run-benchmark-v1"
    run_id: str = Field(min_length=1)
    cold_wall_time_ms: int = Field(ge=0)
    warm_wall_time_ms: int = Field(ge=0)
    cold_report_artifact_id: str = Field(min_length=1)
    warm_report_artifact_id: str = Field(min_length=1)
    warm_cache_hit_count: int = Field(ge=0)
    knowledge_top_k_latency_ms: int = Field(ge=0)
    context_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    estimated_token_limit: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)


__all__ = [
    "KnowledgeSkillDelta",
    "ResearchRunAudit",
    "ResearchRunBenchmark",
    "ResearchRunCheckpoint",
    "ResearchRunFrozenInputs",
    "ResearchRunMode",
    "ResearchRunPerformanceSummary",
    "ResearchRunPlan",
    "ResearchRunRequest",
    "ResearchRunRouteDraft",
    "ResearchRunSpecialistDeltaDraft",
    "ResearchRunStage",
    "ResearchRunStatus",
    "ResearchRunReport",
    "RuntimeArtifactReference",
    "TradingClassificationDraft",
    "TradingClassificationRecord",
    "TradingClassificationRelease",
    "TradingClassificationStatus",
]
