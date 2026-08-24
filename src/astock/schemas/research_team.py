"""Contracts for full-market research-team orchestration and recommendation gating."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel


class ResearchExecutionBackend(StrEnum):
    CHAT_ORCHESTRATED = "CHAT_ORCHESTRATED"
    AGENT_RUNTIME = "AGENT_RUNTIME"


class ResearchTeamScope(StrEnum):
    FULL_MARKET = "FULL_MARKET"
    COMPANY = "COMPANY"


class ResearchTeamDepth(StrEnum):
    INSTITUTIONAL = "INSTITUTIONAL"
    RAPID_OBSERVATION = "RAPID_OBSERVATION"


class ResearchResourceClass(StrEnum):
    LOW_RESOURCE = "LOW_RESOURCE"
    STANDARD = "STANDARD"
    HIGH_RESOURCE = "HIGH_RESOURCE"


class ResearchTaskRole(StrEnum):
    CIO = "CIO"
    MACRO = "MACRO"
    POLICY = "POLICY"
    LIQUIDITY_RISK = "LIQUIDITY_RISK"
    UNIVERSE = "UNIVERSE"
    BLIND_CANDIDATE = "BLIND_CANDIDATE"
    INDUSTRY = "INDUSTRY"
    FUNDAMENTAL = "FUNDAMENTAL"
    FINANCIAL_INTEGRITY = "FINANCIAL_INTEGRITY"
    CATALYST = "CATALYST"
    MARKET_CONTEXT = "MARKET_CONTEXT"
    VALUATION = "VALUATION"
    BULL = "BULL"
    BEAR = "BEAR"
    REVIEWER = "REVIEWER"
    COMMITTEE = "COMMITTEE"
    PORTFOLIO = "PORTFOLIO"
    RECOMMENDATION_GATE = "RECOMMENDATION_GATE"


class ResearchTeamTaskState(StrEnum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


class RecommendationReadinessStatus(StrEnum):
    READY = "READY"
    OBSERVATION_ONLY = "OBSERVATION_ONLY"


class HardwareBudget(AStockModel):
    resource_class: ResearchResourceClass
    cpu_count: int = Field(ge=1)
    memory_gib: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    provider_workers: int = Field(ge=1, le=16)
    agent_workers: int = Field(ge=1, le=16)
    duckdb_threads: int = Field(ge=1, le=32)
    max_parallel_companies: int = Field(ge=1, le=16)
    max_deep_candidates: int = Field(ge=1, le=50)
    background_service_required: Literal[False] = False
    gpu_required: Literal[False] = False


class ResearchTeamTask(AStockModel):
    task_id: str = Field(min_length=1)
    role: ResearchTaskRole
    stage: int = Field(ge=0, le=100)
    dependencies: list[str] = Field(default_factory=list)
    fanout_key: str | None = None
    required_for_recommendation: bool = True
    independent_context_required: bool = True
    output_contract: str = Field(min_length=1)
    readiness_checks: list[str] = Field(default_factory=list)

    @field_validator("dependencies", "readiness_checks")
    @classmethod
    def validate_dependencies(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("task dependencies must be sorted and unique")
        return value


class ResearchTeamPlan(AStockModel):
    schema_version: str = "research-team-plan-v1"
    plan_id: str = Field(min_length=1)
    scope: ResearchTeamScope
    depth: ResearchTeamDepth
    backend: ResearchExecutionBackend
    as_of: AwareDatetime
    policy_version: str = Field(min_length=1)
    hardware_budget: HardwareBudget
    tasks: list[ResearchTeamTask] = Field(min_length=1)
    automatic_resolution_budget_seconds: int = Field(ge=1)
    on_demand_acquisition: Literal[True] = True
    background_service_required: Literal[False] = False
    no_manual_candidate_fallback: Literal[True] = True
    formal_recommendation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_dag(self) -> ResearchTeamPlan:
        task_ids = [item.task_id for item in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("research-team task ids must be unique")
        known = set(task_ids)
        for item in self.tasks:
            if item.task_id in item.dependencies:
                raise ValueError("task cannot depend on itself")
            if not set(item.dependencies).issubset(known):
                raise ValueError("task dependency references unknown task")
        return self


class ResearchRoleOutput(AStockModel):
    schema_version: str = "research-role-output-v1"
    plan_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    output_contract: str = Field(min_length=1)
    member_artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    readiness_check_results: dict[str, bool] = Field(default_factory=dict)
    summary: str = Field(min_length=1)

    @field_validator("member_artifact_ids", "evidence_ids")
    @classmethod
    def validate_lineage_lists(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("role-output lineage lists must be sorted and unique")
        return value


class ResearchRoleResult(AStockModel):
    schema_version: str = "research-role-result-v1"
    plan_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    state: ResearchTeamTaskState
    independent_context_id: str = Field(min_length=1)
    output_artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str | None = None

    @field_validator("output_artifact_ids", "evidence_ids")
    @classmethod
    def validate_sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("role-result artifact/evidence lists must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_completion(self) -> ResearchRoleResult:
        if self.state is ResearchTeamTaskState.COMPLETE and not self.output_artifact_ids:
            raise ValueError("COMPLETE role result requires at least one output artifact")
        return self


class RecommendationReadinessRequest(AStockModel):
    schema_version: str = "recommendation-readiness-request-v1"
    plan_id: str = Field(min_length=1)
    checks: dict[str, bool] = Field(default_factory=dict)


class RecommendationReadinessReport(AStockModel):
    schema_version: str = "recommendation-readiness-report-v1"
    report_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    status: RecommendationReadinessStatus
    required_checks: list[str]
    passed_checks: list[str]
    missing_or_failed_checks: list[str]
    formal_recommendation_allowed: bool
    manual_candidate_fallback_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @field_validator("required_checks", "passed_checks", "missing_or_failed_checks")
    @classmethod
    def validate_sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("readiness check lists must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_authority(self) -> RecommendationReadinessReport:
        ready = self.status is RecommendationReadinessStatus.READY
        if self.formal_recommendation_allowed != ready:
            raise ValueError("formal recommendation authority must match READY status")
        if ready and self.missing_or_failed_checks:
            raise ValueError("READY report cannot have missing checks")
        if not ready and not self.missing_or_failed_checks:
            raise ValueError("OBSERVATION_ONLY report requires a failed or missing check")
        return self


class ResearchCoverageScore(AStockModel):
    schema_version: str = "research-coverage-score-v1"
    universal_research_coverage: float = Field(ge=0, le=100, allow_inf_nan=False)
    industry_specialist_coverage: float = Field(ge=0, le=100, allow_inf_nan=False)
    private_skill_coverage: float = Field(ge=0, le=100, allow_inf_nan=False)
    evidence_coverage: float = Field(ge=0, le=100, allow_inf_nan=False)
    private_skill_is_edge_only: Literal[True] = True


class ResearchCoverageRequest(AStockModel):
    schema_version: str = "research-coverage-request-v1"
    company_id: str = Field(min_length=6, max_length=32)
    universal_required_ids: list[str] = Field(min_length=1)
    universal_completed_ids: list[str] = Field(default_factory=list)
    industry_required_ids: list[str] = Field(min_length=1)
    industry_completed_ids: list[str] = Field(default_factory=list)
    private_skill_available_ids: list[str] = Field(default_factory=list)
    private_skill_matched_ids: list[str] = Field(default_factory=list)
    evidence_required_ids: list[str] = Field(min_length=1)
    evidence_satisfied_ids: list[str] = Field(default_factory=list)

    @field_validator(
        "universal_required_ids",
        "universal_completed_ids",
        "industry_required_ids",
        "industry_completed_ids",
        "private_skill_available_ids",
        "private_skill_matched_ids",
        "evidence_required_ids",
        "evidence_satisfied_ids",
    )
    @classmethod
    def validate_coverage_lists(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("coverage ids must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_coverage_subsets(self) -> ResearchCoverageRequest:
        pairs = (
            (self.universal_completed_ids, self.universal_required_ids, "universal"),
            (self.industry_completed_ids, self.industry_required_ids, "industry"),
            (self.private_skill_matched_ids, self.private_skill_available_ids, "private_skill"),
            (self.evidence_satisfied_ids, self.evidence_required_ids, "evidence"),
        )
        for completed, required, label in pairs:
            if not set(completed).issubset(required):
                raise ValueError(
                    f"{label} completed ids must be a subset of required/available ids"
                )
        return self


class ResearchCoverageReport(AStockModel):
    schema_version: str = "research-coverage-report-v1"
    report_id: str = Field(min_length=1)
    company_id: str = Field(min_length=6, max_length=32)
    score: ResearchCoverageScore
    universal_minimum: float = Field(ge=0, le=100)
    industry_minimum: float = Field(ge=0, le=100)
    evidence_minimum: float = Field(ge=0, le=100)
    core_coverage_pass: bool
    private_skill_gates_recommendation: Literal[False] = False
    missing_universal_ids: list[str] = Field(default_factory=list)
    missing_industry_ids: list[str] = Field(default_factory=list)
    missing_evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("missing_universal_ids", "missing_industry_ids", "missing_evidence_ids")
    @classmethod
    def validate_missing_lists(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("coverage missing ids must be sorted and unique")
        return value


class IndustryResearchArchetype(AStockModel):
    schema_version: str = "industry-research-archetype-v1"
    archetype_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=1)
    key_metrics: list[str] = Field(min_length=1)
    valuation_methods: list[str] = Field(min_length=1)
    key_risks: list[str] = Field(min_length=1)
    taxonomy_kind: Literal["INTERNAL_RESEARCH_ARCHETYPE"] = "INTERNAL_RESEARCH_ARCHETYPE"
    certified_external_taxonomy: Literal[False] = False


class IndustryResearchMatch(AStockModel):
    schema_version: str = "industry-research-match-v1"
    query: str = Field(min_length=1)
    status: Literal["MATCHED", "UNCLASSIFIED"]
    archetype: IndustryResearchArchetype | None = None
    matched_alias: str | None = None
    private_skill_required_for_analysis: Literal[False] = False

    @model_validator(mode="after")
    def validate_match(self) -> IndustryResearchMatch:
        matched = self.status == "MATCHED"
        if matched != (self.archetype is not None and self.matched_alias is not None):
            raise ValueError("industry match status does not reconcile")
        return self


__all__ = [
    "HardwareBudget",
    "RecommendationReadinessReport",
    "RecommendationReadinessRequest",
    "RecommendationReadinessStatus",
    "ResearchCoverageReport",
    "ResearchCoverageRequest",
    "ResearchCoverageScore",
    "ResearchExecutionBackend",
    "IndustryResearchArchetype",
    "IndustryResearchMatch",
    "ResearchResourceClass",
    "ResearchRoleOutput",
    "ResearchRoleResult",
    "ResearchTaskRole",
    "ResearchTeamDepth",
    "ResearchTeamPlan",
    "ResearchTeamScope",
    "ResearchTeamTask",
    "ResearchTeamTaskState",
]
