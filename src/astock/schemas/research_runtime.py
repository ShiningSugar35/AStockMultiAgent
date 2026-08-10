"""Versioned contracts for the recoverable single-stock research runtime."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.committee import CommitteeAssessment, CounterCaseDraft, TradeProtocolOutcome
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
from astock.schemas.runs import ContextBudgetReport
from astock.schemas.serenity_v2 import SerenityMethodContractV2

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ResearchRunMode(StrEnum):
    RECORDED_INPUT = "RECORDED_INPUT"
    LIVE = "LIVE"


class ResearchRunStage(StrEnum):
    INPUT_RESOLUTION = "INPUT_RESOLUTION"
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


class TradingSpecialRegime(StrEnum):
    ORDINARY = "ORDINARY"
    IPO_INITIAL_NO_FIXED_PRICE_LIMIT = "IPO_INITIAL_NO_FIXED_PRICE_LIMIT"
    SUSPENDED = "SUSPENDED"
    SPECIAL_UNVERIFIED = "SPECIAL_UNVERIFIED"


class TradingPriceLimitRegime(StrEnum):
    FIXED = "FIXED"
    NO_FIXED = "NO_FIXED"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


class RuntimeArtifactReference(AStockModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)


class TradingClassificationCorporateActionBaseline(AStockModel):
    schema_version: str = "trading-classification-corporate-action-baseline-v1"
    baseline_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    window_start: str = Field(min_length=10)
    window_end: str = Field(min_length=10)
    reference_status: str = Field(min_length=1)
    release_id: str | None = None
    manifest_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    raw_snapshot_ids: list[str]
    observed_record_count: int = Field(ge=0)
    official_query_snapshot_ids: list[str] = Field(default_factory=list)
    candidate_announcement_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str]
    absence_is_officially_certified: bool = False
    ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_baseline(self) -> TradingClassificationCorporateActionBaseline:
        for label, values in (
            ("raw snapshot", self.raw_snapshot_ids),
            ("official query snapshot", self.official_query_snapshot_ids),
            ("candidate announcement", self.candidate_announcement_ids),
            ("reason code", self.reason_codes),
        ):
            if values != sorted(set(values)):
                raise ValueError(
                    f"corporate-action baseline {label} values must be sorted and unique"
                )
        if self.absence_is_officially_certified:
            if not self.official_query_snapshot_ids or self.candidate_announcement_ids:
                raise ValueError(
                    "certified corporate-action absence requires official query snapshots "
                    "and no candidates"
                )
        return self


class TradingClassificationDraft(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    effective_from: AwareDatetime
    valid_until: AwareDatetime
    classification: PaperTradingClassification
    special_no_price_limit: bool
    special_regime: TradingSpecialRegime = TradingSpecialRegime.ORDINARY
    price_limit_regime: TradingPriceLimitRegime = TradingPriceLimitRegime.FIXED
    price_limit_rate_bps: int | None = Field(default=None, ge=1)
    rulebook_artifact_id: str | None = None
    instrument_release_id: str | None = None
    calendar_release_id: str | None = None
    daily_release_id: str | None = None
    resolver_version: str | None = None
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
        if self.resolver_version is not None:
            if not all(
                (
                    self.rulebook_artifact_id,
                    self.instrument_release_id,
                    self.calendar_release_id,
                    self.daily_release_id,
                    self.corporate_action_baseline_artifact_id,
                )
            ):
                raise ValueError("resolved classification requires complete reference lineage")
            if self.special_no_price_limit != (
                self.price_limit_regime is TradingPriceLimitRegime.NO_FIXED
            ):
                raise ValueError("special_no_price_limit must match the price-limit regime")
            if self.price_limit_regime is TradingPriceLimitRegime.FIXED:
                if (
                    not self.classification.fixed_price_limit_eligible
                    or self.price_limit_rate_bps is None
                ):
                    raise ValueError("fixed price-limit classification requires a frozen rate")
            elif self.price_limit_rate_bps is not None:
                raise ValueError("non-fixed price-limit regimes cannot carry a fixed rate")
            if self.special_regime is TradingSpecialRegime.SUSPENDED:
                if not self.classification.suspended:
                    raise ValueError("suspended regime requires suspended classification")
            elif self.classification.suspended:
                raise ValueError("suspended classification requires suspended regime")
        return self


class TradingClassificationRelease(AStockModel):
    schema_version: str = "trading-classification-release-v2"
    release_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    effective_from: AwareDatetime
    valid_until: AwareDatetime
    classification: PaperTradingClassification
    special_no_price_limit: bool
    special_regime: TradingSpecialRegime = TradingSpecialRegime.ORDINARY
    price_limit_regime: TradingPriceLimitRegime = TradingPriceLimitRegime.FIXED
    price_limit_rate_bps: int | None = Field(default=None, ge=1)
    rulebook_artifact_id: str | None = None
    instrument_release_id: str | None = None
    calendar_release_id: str | None = None
    daily_release_id: str | None = None
    resolver_version: str | None = None
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
        if self.resolver_version is not None:
            required_lineage = (
                self.rulebook_artifact_id,
                self.instrument_release_id,
                self.calendar_release_id,
                self.daily_release_id,
                self.corporate_action_baseline_artifact_id,
            )
            if not all(required_lineage):
                raise ValueError(
                    "resolved classification release requires complete reference lineage"
                )
            if self.special_no_price_limit != (
                self.price_limit_regime is TradingPriceLimitRegime.NO_FIXED
            ):
                raise ValueError("classification release price-limit regime drift")
            if self.price_limit_regime is TradingPriceLimitRegime.FIXED:
                if (
                    not self.classification.fixed_price_limit_eligible
                    or self.price_limit_rate_bps is None
                ):
                    raise ValueError("fixed classification release requires one frozen rate")
            elif self.price_limit_rate_bps is not None:
                raise ValueError("non-fixed classification release cannot carry a fixed rate")
        return self


class TradingClassificationRecord(AStockModel):
    release: TradingClassificationRelease
    artifact_id: str = Field(min_length=1)
    object_hash: str = Field(pattern=_SHA256_PATTERN)
    idempotent_replay: bool


class TradingClassificationResolution(AStockModel):
    schema_version: str = "trading-classification-resolution-v1"
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    status: TradingClassificationStatus
    artifact_id: str | None = None
    object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_artifact_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    live_sync_attempted: bool = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_resolution(self) -> TradingClassificationResolution:
        if self.source_artifact_ids != sorted(set(self.source_artifact_ids)):
            raise ValueError("classification resolution source artifacts must be sorted and unique")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("classification resolution reasons must be sorted and unique")
        if self.status is TradingClassificationStatus.READY:
            if not self.artifact_id or not self.object_hash or self.reason_codes:
                raise ValueError(
                    "READY classification resolution requires one release and no reasons"
                )
        elif not self.reason_codes:
            raise ValueError("NEEDS_INFO classification resolution requires reason codes")
        return self


class ClassifiedTradeProtocol(AStockModel):
    schema_version: str = "classified-trade-protocol-v1"
    protocol_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    decision_pack_artifact_id: str = Field(min_length=1)
    decision_pack_object_hash: str = Field(pattern=_SHA256_PATTERN)
    committee_protocol_artifact_id: str = Field(min_length=1)
    committee_protocol_object_hash: str = Field(pattern=_SHA256_PATTERN)
    trading_classification_artifact_id: str = Field(min_length=1)
    trading_classification_object_hash: str = Field(pattern=_SHA256_PATTERN)
    committee_outcome: TradeProtocolOutcome
    final_outcome: TradeProtocolOutcome
    board: str = Field(min_length=1)
    risk_status: str = Field(min_length=1)
    special_regime: TradingSpecialRegime
    price_limit_regime: TradingPriceLimitRegime
    price_limit_rate_bps: int | None = Field(default=None, ge=1)
    blocking_codes: list[str]
    frozen_input_hashes: list[str] = Field(min_length=3)
    requires_user_confirmation: Literal[True] = True
    paper_simulation_allowed: bool = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_protocol(self) -> ClassifiedTradeProtocol:
        if self.blocking_codes != sorted(set(self.blocking_codes)):
            raise ValueError("classified protocol blocking codes must be sorted and unique")
        if self.frozen_input_hashes != sorted(set(self.frozen_input_hashes)):
            raise ValueError("classified protocol input hashes must be sorted and unique")
        required = {
            self.decision_pack_object_hash,
            self.committee_protocol_object_hash,
            self.trading_classification_object_hash,
        }
        if not required.issubset(self.frozen_input_hashes):
            raise ValueError(
                "classified protocol must bind decision, committee protocol, and classification"
            )
        if self.paper_simulation_allowed != (
            self.final_outcome is TradeProtocolOutcome.APPROVE_SIMULATION
        ):
            raise ValueError("classified protocol paper gate must match its final outcome")
        if self.paper_simulation_allowed and self.blocking_codes:
            raise ValueError("paper-eligible classified protocol cannot carry blocking codes")
        return self


class ResearchPaperDecision(AStockModel):
    schema_version: str = "research-paper-decision-v1"
    decision_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    classified_protocol_artifact_id: str = Field(min_length=1)
    classified_protocol_object_hash: str = Field(pattern=_SHA256_PATTERN)
    outcome: TradeProtocolOutcome
    paper_simulation_eligible: bool
    requires_user_confirmation: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_decision(self) -> ResearchPaperDecision:
        if self.paper_simulation_eligible != (
            self.outcome is TradeProtocolOutcome.APPROVE_SIMULATION
        ):
            raise ValueError("paper decision eligibility must match the classified outcome")
        return self


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
    decision_pack_artifact_id: str | None = None
    committee_protocol_artifact_id: str | None = None

    @model_validator(mode="after")
    def validate_committee_pair(self) -> ResearchRunFrozenInputs:
        if (self.decision_pack_artifact_id is None) != (
            self.committee_protocol_artifact_id is None
        ):
            raise ValueError("frozen committee decision and protocol must be provided together")
        return self


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
    auto_resolve_inputs: bool = True
    sync_reference_inputs: bool = True
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


class ResearchRunInputManifest(AStockModel):
    schema_version: str = "research-run-input-manifest-v1"
    run_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    resolved_artifact_ids: dict[str, str] = Field(default_factory=dict)
    unresolved_codes: list[str] = Field(default_factory=list)
    knowledge_run_id: str | None = None
    knowledge_query: KnowledgeSkillQuery | None = None
    auto_resolution_enabled: bool
    reference_sync_enabled: bool
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_manifest(self) -> ResearchRunInputManifest:
        if self.unresolved_codes != sorted(set(self.unresolved_codes)):
            raise ValueError("runtime input manifest unresolved codes must be sorted and unique")
        if (self.knowledge_run_id is None) != (self.knowledge_query is None):
            raise ValueError("runtime input manifest knowledge identity/query must be paired")
        if any(not key or not value for key, value in self.resolved_artifact_ids.items()):
            raise ValueError("runtime input manifest artifact bindings cannot be blank")
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
    artifact_object_hashes: dict[str, str] = Field(default_factory=dict)
    duration_ms: int = Field(ge=0)
    cache_hit: bool
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> ResearchRunCheckpoint:
        if self.artifact_ids != sorted(set(self.artifact_ids)):
            raise ValueError("checkpoint artifact ids must be sorted and unique")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("checkpoint reason codes must be sorted and unique")
        if self.artifact_object_hashes and set(self.artifact_object_hashes) != set(
            self.artifact_ids
        ):
            raise ValueError("checkpoint artifact hash keys must match artifact ids")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.artifact_object_hashes.values()
        ):
            raise ValueError("checkpoint artifact hashes must be SHA-256 values")
        return self


class ResearchRunPerformanceSummary(AStockModel):
    wall_time_ms: int = Field(ge=0)
    knowledge_top_k_latency_ms: int = Field(ge=0)
    context_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    estimated_token_limit: int = Field(ge=0)
    cache_hit_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    stage_wall_time_ms: dict[str, int] = Field(default_factory=dict)
    stage_cache_hits: dict[str, bool] = Field(default_factory=dict)
    context_budget: ContextBudgetReport = Field(default_factory=ContextBudgetReport)


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
