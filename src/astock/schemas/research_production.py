"""Phase 12 research-production routing, efficiency, instrumentation, and catalyst contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.research import ResearchCostClass, ResearchSkillKind


class OrdinalResearchLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ResearchPriorityBucket(StrEnum):
    DEFER = "DEFER"
    STANDARD = "STANDARD"
    HIGH = "HIGH"
    URGENT = "URGENT"


class ProductionSkillRole(StrEnum):
    FUNDAMENTAL_SPECIALIST = "FUNDAMENTAL_SPECIALIST"
    SHARED_HYPOTHESIS = "SHARED_HYPOTHESIS"
    CANONICAL_VALUATION = "CANONICAL_VALUATION"
    MARKET_TRADE_CONTEXT = "MARKET_TRADE_CONTEXT"
    COMPOSER = "COMPOSER"


class ResearchModule(StrEnum):
    EVIDENCE = "EVIDENCE"
    INDUSTRY = "INDUSTRY"
    COMPANY_ECONOMICS = "COMPANY_ECONOMICS"
    DRIVER_TREE = "DRIVER_TREE"
    SHARED_HYPOTHESIS = "SHARED_HYPOTHESIS"
    FORECAST = "FORECAST"
    VALUATION = "VALUATION"
    MARKET_TRADE_CONTEXT = "MARKET_TRADE_CONTEXT"
    RESEARCH_MEMO = "RESEARCH_MEMO"
    COMMITTEE = "COMMITTEE"


class ResearchProductionPolicy(AStockModel):
    schema_version: str = "research-production-policy-v1"
    policy_id: str = "research-production-v1"
    policy_version: str = "research-production-v1"
    default_specialist_budget: int = Field(default=3, ge=2, le=3)
    minimum_specialist_budget: int = Field(default=2, ge=1, le=3)
    hard_max_specialists: int = Field(default=4, ge=3, le=4)
    role_by_kind: dict[ResearchSkillKind, ProductionSkillRole]
    automatic_skill_modification_allowed: Literal[False] = False
    online_weight_learning_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_policy(self) -> ResearchProductionPolicy:
        if set(self.role_by_kind) != set(ResearchSkillKind):
            raise ValueError("research production policy must classify every Skill kind")
        if (
            self.role_by_kind[ResearchSkillKind.GROWTH_PROBABILITY]
            is not ProductionSkillRole.SHARED_HYPOTHESIS
        ):
            raise ValueError("Growth Probability must route through the shared hypothesis layer")
        if (
            self.role_by_kind[ResearchSkillKind.GROWTH_VALUATION]
            is not ProductionSkillRole.CANONICAL_VALUATION
        ):
            raise ValueError("Growth Valuation must route through the canonical valuation engine")
        for kind in (ResearchSkillKind.DAILY_TREND_HEALTH, ResearchSkillKind.HOURLY_SWING):
            if self.role_by_kind[kind] is not ProductionSkillRole.MARKET_TRADE_CONTEXT:
                raise ValueError("daily/hourly technical Skills must be market/trade context")
        if self.role_by_kind[ResearchSkillKind.RESEARCH_MEMO] is not ProductionSkillRole.COMPOSER:
            raise ValueError("ResearchMemoComposer must remain a composer")
        return self


class ResearchNeedVector(AStockModel):
    schema_version: str = "research-need-vector-v1"
    need_id: str = Field(min_length=1)
    base_case_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    thesis_tags: list[str] = Field(default_factory=list)
    industry_tags: list[str] = Field(default_factory=list)
    event_tags: list[str] = Field(default_factory=list)
    ontology_terms: list[str] = Field(default_factory=list)
    horizon: str = Field(min_length=1)
    available_inputs: list[str] = Field(default_factory=list)
    available_frequencies: list[str] = Field(default_factory=list)
    materiality: OrdinalResearchLevel
    novelty: OrdinalResearchLevel
    uncertainty: OrdinalResearchLevel
    portfolio_relevance: OrdinalResearchLevel
    catalyst_urgency: OrdinalResearchLevel
    data_availability: OrdinalResearchLevel
    source_diversity: OrdinalResearchLevel
    estimated_research_cost: OrdinalResearchLevel
    embedding_recall_scores: dict[str, float] = Field(default_factory=dict)
    embedding_recall_artifact_id: str | None = None
    embedding_recall_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_need(self) -> ResearchNeedVector:
        for values in (
            self.thesis_tags,
            self.industry_tags,
            self.event_tags,
            self.ontology_terms,
            self.available_inputs,
            self.available_frequencies,
        ):
            if values != sorted(set(values)):
                raise ValueError("research need vector list fields must be sorted and unique")
        if any(value < 0 or value > 1 for value in self.embedding_recall_scores.values()):
            raise ValueError("embedding recall scores must be in 0..1")
        has_embedding_provenance = bool(
            self.embedding_recall_artifact_id or self.embedding_recall_object_hash
        )
        if has_embedding_provenance != bool(
            self.embedding_recall_artifact_id and self.embedding_recall_object_hash
        ):
            raise ValueError("embedding recall artifact id/hash must be supplied together")
        if self.embedding_recall_scores and not has_embedding_provenance:
            raise ValueError("embedding recall scores require frozen artifact provenance")
        return self


class SkillCapabilityVector(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    kind: ResearchSkillKind
    role: ProductionSkillRole
    ontology_terms: list[str]
    required_inputs: list[str]
    required_frequencies: list[str]
    incompatible_skills: list[str]
    cost_class: ResearchCostClass


class ResearchPriorityDecision(AStockModel):
    priority_bucket: ResearchPriorityBucket
    ordinal_score: int = Field(ge=0, le=21)
    positive_factor_codes: list[str]
    limiting_factor_codes: list[str]
    specialist_budget: int = Field(ge=2, le=4)
    fake_monetary_value_assigned: Literal[False] = False
    fake_probability_assigned: Literal[False] = False


class ResearchProductionRouteNeedsInfo(AStockModel):
    schema_version: str = "research-production-route-needs-info-v1"
    status: Literal["NEEDS_INFO"] = "NEEDS_INFO"
    need_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    requested_base_case_id: str = Field(min_length=1)
    requested_artifact_type: Literal["BaseCasePack"] = "BaseCasePack"
    requested_artifact_id: str = Field(min_length=1)
    requested_object_hash: None = None
    available_base_case_id: str | None = None
    available_base_case_object_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    finding_codes: list[str] = Field(min_length=1)
    required_action_codes: list[str] = Field(min_length=1)
    automatic_skill_modification_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_needs_info(self) -> ResearchProductionRouteNeedsInfo:
        if (self.available_base_case_id is None) != (self.available_base_case_object_hash is None):
            raise ValueError("available BaseCase id/hash must be supplied together")
        for values in (self.finding_codes, self.required_action_codes):
            if values != sorted(set(values)):
                raise ValueError("route NEEDS_INFO codes must be sorted and unique")
        return self


class ProductionRouteMatch(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    kind: ResearchSkillKind
    role: ProductionSkillRole
    hard_applicable: bool
    ontology_overlap_count: int = Field(ge=0)
    embedding_recall_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    materiality_points: int = Field(ge=0, le=3)
    uncertainty_points: int = Field(ge=0, le=3)
    applicability_points: int = Field(ge=0, le=4)
    incremental_evidence_points: int = Field(ge=0, le=3)
    marginal_cost_points: int = Field(ge=1, le=3)
    route_score: float = Field(ge=0, le=320, allow_inf_nan=False)
    reason_codes: list[str]


class ResearchProductionRoutePlan(AStockModel):
    schema_version: str = "research-production-route-plan-v1"
    route_plan_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    registry_version: str = Field(min_length=1)
    need_id: str = Field(min_length=1)
    base_case_id: str = Field(min_length=1)
    priority: ResearchPriorityDecision
    selected_fundamental_specialists: list[ProductionRouteMatch]
    shared_hypothesis_modules: list[ProductionRouteMatch]
    canonical_valuation_modules: list[ProductionRouteMatch]
    market_trade_context_modules: list[ProductionRouteMatch]
    composers: list[ProductionRouteMatch]
    excluded: dict[str, list[str]]
    hard_max_specialists: int = Field(ge=3, le=4)
    embedding_recall_used: bool
    finding_codes: list[str]
    automatic_skill_modification_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_route(self) -> ResearchProductionRoutePlan:
        if len(self.selected_fundamental_specialists) > self.priority.specialist_budget:
            raise ValueError("production route exceeds its dynamic specialist budget")
        if len(self.selected_fundamental_specialists) > self.hard_max_specialists:
            raise ValueError("production route exceeds the hard specialist maximum")
        if any(
            item.role is not ProductionSkillRole.FUNDAMENTAL_SPECIALIST
            for item in self.selected_fundamental_specialists
        ):
            raise ValueError("only fundamental specialists can consume specialist budget")
        for values in (self.finding_codes,):
            if values != sorted(set(values)):
                raise ValueError("production route finding codes must be sorted and unique")
        return self


class SkillUsageEvent(AStockModel):
    schema_version: str = "skill-usage-event-v1"
    usage_event_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    corrected_claim: bool = False
    found_gap: bool = False
    changed_driver: bool = False
    provided_falsifier: bool = False
    changed_investment_committee_state: bool = False
    prospective_lift: float | None = Field(default=None, ge=-10, le=10, allow_inf_nan=False)
    prospective_lift_artifact_id: str | None = None
    prospective_lift_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    token_cost: int = Field(ge=0)
    near_duplicate_skill_ids: list[str] = Field(default_factory=list)
    conflict_skill_ids: list[str] = Field(default_factory=list)
    automatic_skill_modification_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_usage(self) -> SkillUsageEvent:
        for values in (self.near_duplicate_skill_ids, self.conflict_skill_ids):
            if values != sorted(set(values)):
                raise ValueError("Skill usage relationship ids must be sorted and unique")
        has_lift_provenance = bool(
            self.prospective_lift_artifact_id or self.prospective_lift_object_hash
        )
        if self.prospective_lift is not None and not (
            self.prospective_lift_artifact_id and self.prospective_lift_object_hash
        ):
            raise ValueError("prospective lift requires frozen prospective-evaluation provenance")
        if self.prospective_lift is None and has_lift_provenance:
            raise ValueError("prospective lift provenance requires a lift value")
        return self


class SkillLifecycleRecommendation(StrEnum):
    KEEP = "KEEP"
    REVIEW_LOW_VALUE = "REVIEW_LOW_VALUE"
    REVIEW_DUPLICATE_OR_CONFLICT = "REVIEW_DUPLICATE_OR_CONFLICT"
    INSUFFICIENT_PROSPECTIVE_EVIDENCE = "INSUFFICIENT_PROSPECTIVE_EVIDENCE"


class SkillEfficiencySummary(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    usage_count: int = Field(ge=0)
    corrected_claim_count: int = Field(ge=0)
    found_gap_count: int = Field(ge=0)
    changed_driver_count: int = Field(ge=0)
    provided_falsifier_count: int = Field(ge=0)
    changed_ic_state_count: int = Field(ge=0)
    total_token_cost: int = Field(ge=0)
    mean_prospective_lift: float | None = Field(default=None, allow_inf_nan=False)
    near_duplicate_skill_ids: list[str]
    conflict_skill_ids: list[str]
    recommendation: SkillLifecycleRecommendation


class SkillEfficiencyReport(AStockModel):
    schema_version: str = "skill-efficiency-report-v1"
    report_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    summaries: list[SkillEfficiencySummary]
    prospective_evidence_required_for_retirement: Literal[True] = True
    automatic_retirement_allowed: Literal[False] = False
    automatic_skill_modification_allowed: Literal[False] = False


class CatalystStatus(StrEnum):
    EXPECTED = "EXPECTED"
    CONFIRMED = "CONFIRMED"
    MISSED = "MISSED"
    INVALIDATED = "INVALIDATED"
    CLOSED = "CLOSED"


class KPIComparison(StrEnum):
    GE = "GE"
    LE = "LE"
    GT = "GT"
    LT = "LT"
    EQ = "EQ"


class CatalystKPIRule(AStockModel):
    kpi_id: str = Field(min_length=1)
    comparison: KPIComparison
    threshold: float = Field(allow_inf_nan=False)


class CatalystRecordRequest(AStockModel):
    schema_version: str = "catalyst-record-request-v1"
    company_id: str = Field(min_length=1)
    thesis_id: str = Field(min_length=1)
    catalyst_type: str = Field(min_length=1)
    expected_from: AwareDatetime
    expected_to: AwareDatetime
    status: CatalystStatus = CatalystStatus.EXPECTED
    kpi_rules: list[CatalystKPIRule] = Field(default_factory=list)
    affected_modules: list[ResearchModule] = Field(min_length=1)
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_catalyst(self) -> CatalystRecordRequest:
        if self.expected_to < self.expected_from:
            raise ValueError("catalyst expected window is invalid")
        kpis = [item.kpi_id for item in self.kpi_rules]
        if kpis != sorted(set(kpis)):
            raise ValueError("catalyst KPI rules must be sorted and unique")
        if self.affected_modules != sorted(set(self.affected_modules), key=lambda item: item.value):
            raise ValueError("catalyst affected modules must be sorted and unique")
        for values in (self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("catalyst provenance must be sorted and unique")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("catalyst source artifact/hash lists must align")
        return self


class CatalystRecord(CatalystRecordRequest):
    schema_version: str = "catalyst-record-v1"
    catalyst_id: str = Field(min_length=1)
    catalyst_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ThesisKPIObservation(AStockModel):
    kpi_id: str = Field(min_length=1)
    value: float = Field(allow_inf_nan=False)
    observed_at: AwareDatetime
    source_artifact_id: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalystMonitorRequest(AStockModel):
    schema_version: str = "catalyst-monitor-request-v1"
    catalyst_id: str = Field(min_length=1)
    as_of: AwareDatetime
    observations: list[ThesisKPIObservation] = Field(default_factory=list)
    observed_status: CatalystStatus | None = None

    @model_validator(mode="after")
    def validate_observations(self) -> CatalystMonitorRequest:
        ids = [item.kpi_id for item in self.observations]
        if ids != sorted(set(ids)):
            raise ValueError("catalyst monitor observations must be sorted and unique")
        if any(item.observed_at > self.as_of for item in self.observations):
            raise ValueError("catalyst monitor cannot consume future observations")
        return self


class CatalystMonitorReport(AStockModel):
    schema_version: str = "catalyst-monitor-report-v1"
    monitor_id: str = Field(min_length=1)
    catalyst_id: str = Field(min_length=1)
    as_of: AwareDatetime
    prior_status: CatalystStatus
    evaluated_status: CatalystStatus
    triggered_kpi_ids: list[str]
    missing_kpi_ids: list[str]
    rerun_modules: list[ResearchModule]
    no_full_research_rerun: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "CatalystKPIRule",
    "CatalystMonitorReport",
    "CatalystMonitorRequest",
    "CatalystRecord",
    "CatalystRecordRequest",
    "CatalystStatus",
    "KPIComparison",
    "OrdinalResearchLevel",
    "ProductionRouteMatch",
    "ProductionSkillRole",
    "ResearchModule",
    "ResearchNeedVector",
    "ResearchPriorityBucket",
    "ResearchPriorityDecision",
    "ResearchProductionPolicy",
    "ResearchProductionRouteNeedsInfo",
    "ResearchProductionRoutePlan",
    "SkillCapabilityVector",
    "SkillEfficiencyReport",
    "SkillEfficiencySummary",
    "SkillLifecycleRecommendation",
    "SkillUsageEvent",
    "ThesisKPIObservation",
]
