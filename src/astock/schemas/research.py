"""Frozen-evidence and common BaseCase research contracts."""

from __future__ import annotations

from typing import Literal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.evidence import EvidenceGrade
from astock.schemas.pit import PointInTimeStatus
from astock.schemas.serenity_v2 import SerenityMethodContractV2

_SERENITY_V2_SKILL_VERSIONS = {
    "IndustryBottleneckSkill": "industry-bottleneck-v2",
    "EventToAlphaSkill": "event-to-alpha-v2",
    "GrowthProbabilitySkill": "growth-probability-v2",
    "GrowthValuationLens": "growth-valuation-v2",
    "DailyTrendHealthSkill": "daily-trend-health-v2",
}


class ResearchCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class ResearchFindingType(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"
    MANAGEMENT_CLAIM = "MANAGEMENT_CLAIM"
    THIRD_PARTY_ESTIMATE = "THIRD_PARTY_ESTIMATE"
    COMMUNITY_LEAD = "COMMUNITY_LEAD"
    ANALYST_INFERENCE = "ANALYST_INFERENCE"


class ResearchGapSeverity(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    MATERIAL = "MATERIAL"
    BLOCKING = "BLOCKING"


class ResearchRequestModule(StrEnum):
    FINANCIAL = "financial"
    EVIDENCE = "evidence"
    RESEARCH = "research"


class ResearchRequest(AStockModel):
    company: str = Field(min_length=1)
    ticker: str = Field(pattern=r"^\d{6}$")
    market: Literal["CN"] = "CN"
    requested_modules: list[ResearchRequestModule] = Field(
        default_factory=lambda: [
            ResearchRequestModule.FINANCIAL,
            ResearchRequestModule.EVIDENCE,
            ResearchRequestModule.RESEARCH,
        ]
    )

    @model_validator(mode="after")
    def validate_modules(self) -> "ResearchRequest":
        if not self.requested_modules:
            raise ValueError("requested_modules must not be empty")
        requested = set(self.requested_modules)
        object.__setattr__(
            self,
            "requested_modules",
            [module for module in ResearchRequestModule if module in requested],
        )
        return self


class EvidenceCollectionTask(AStockModel):
    request_artifact_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    ticker: str = Field(pattern=r"^\d{6}$")
    required_sources: list[str] = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_task(self) -> "EvidenceCollectionTask":
        if not self.required_sources:
            raise ValueError("evidence collection task requires at least one source")
        object.__setattr__(self, "required_sources", sorted(set(self.required_sources)))
        return self


class EvidenceCollectionRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_INFO = "NEEDS_INFO"
    FAILED = "FAILED"


class EvidenceCollectionRun(AStockModel):
    task_artifact_id: str = Field(min_length=1)
    status: EvidenceCollectionRunStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    collected_items: list[str] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_run_window(self) -> "EvidenceCollectionRun":
        if self.completed_at < self.started_at:
            raise ValueError("evidence collection run completed_at must be after started_at")
        return self


class EvidencePack(AStockModel):
    """Lightweight analysis input artifact for downstream research modules.

    EvidencePack is intended for analysis consumption and is distinct from
    FrozenEvidencePack, which remains the audit-oriented evidence freeze output.
    """

    run_artifact_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    ticker: str = Field(pattern=r"^\d{6}$")
    evidence_items: list[str] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def normalize_items(self) -> "EvidencePack":
        object.__setattr__(self, "evidence_items", sorted(set(self.evidence_items)))
        object.__setattr__(self, "missing_items", sorted(set(self.missing_items)))
        return self


class ResearchPreparationStatus(StrEnum):
    READY_FOR_BASE_CASE = "READY_FOR_BASE_CASE"
    NEEDS_INFO = "NEEDS_INFO"


class ResearchPreparationRequest(AStockModel):
    research_request_artifact_id: str = Field(min_length=1)
    evidence_pack_artifact_id: str = Field(min_length=1)
    financial_audit_run_id: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)
    as_of: AwareDatetime
    formal_historical: bool = True
    allow_approximated: bool = False

    @model_validator(mode="after")
    def normalize_request(self) -> ResearchPreparationRequest:
        normalized_claim_ids = sorted(set(self.claim_ids))
        if not normalized_claim_ids:
            raise ValueError("research preparation requires at least one claim")
        object.__setattr__(self, "claim_ids", normalized_claim_ids)
        if self.allow_approximated and not self.formal_historical:
            raise ValueError("allow_approximated only applies to formal historical mode")
        return self


class ResearchPreparationManifest(AStockModel):
    research_request_artifact_id: str = Field(min_length=1)
    evidence_pack_artifact_id: str = Field(min_length=1)
    financial_audit_run_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    ticker: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    status: ResearchPreparationStatus
    claim_ids: list[str] = Field(min_length=1)
    blocking_codes: list[str] = Field(default_factory=list)
    required_action_codes: list[str] = Field(default_factory=list)
    financial_manual_task_ids: list[str] = Field(default_factory=list)
    frozen_evidence_pack_id: str | None = None
    frozen_evidence_pack_artifact_id: str | None = None
    input_object_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_manifest(self) -> ResearchPreparationManifest:
        for field_name in (
            "claim_ids",
            "blocking_codes",
            "required_action_codes",
            "financial_manual_task_ids",
            "input_object_hashes",
        ):
            values = getattr(self, field_name)
            object.__setattr__(self, field_name, sorted(set(values)))
        invalid_hashes = [
            item
            for item in self.input_object_hashes
            if len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
        ]
        if invalid_hashes:
            raise ValueError("research preparation input object hashes must be SHA-256")
        frozen_ids = (
            self.frozen_evidence_pack_id,
            self.frozen_evidence_pack_artifact_id,
        )
        if self.status is ResearchPreparationStatus.READY_FOR_BASE_CASE:
            if any(item is None for item in frozen_ids):
                raise ValueError("ready research preparation requires a frozen evidence pack")
            if self.blocking_codes or self.required_action_codes:
                raise ValueError("ready research preparation cannot contain blocking actions")
        elif any(item is not None for item in frozen_ids):
            raise ValueError("NEEDS_INFO research preparation cannot reference a frozen pack")
        return self


class BaseCaseSection(StrEnum):
    BUSINESS_MODEL = "BUSINESS_MODEL"
    REVENUE_DRIVERS = "REVENUE_DRIVERS"
    PROFIT_DRIVERS = "PROFIT_DRIVERS"
    CASH_FLOW_QUALITY = "CASH_FLOW_QUALITY"
    CAPITAL_RETURNS = "CAPITAL_RETURNS"
    REINVESTMENT = "REINVESTMENT"
    MANAGEMENT_GOVERNANCE = "MANAGEMENT_GOVERNANCE"
    COMPETITIVE_POSITION = "COMPETITIVE_POSITION"
    INDUSTRY_SUPPLY_DEMAND = "INDUSTRY_SUPPLY_DEMAND"
    VALUATION_EXPECTATIONS = "VALUATION_EXPECTATIONS"
    PRICE_TREND_CONTEXT = "PRICE_TREND_CONTEXT"
    KNOWN_RISKS = "KNOWN_RISKS"


BASE_CASE_SECTIONS = tuple(BaseCaseSection)


class ResearchCoreConfig(AStockModel):
    kernel_version: str = Field(min_length=1)
    confidence_caps: dict[ResearchCoverageStatus, float]
    required_sections: list[BaseCaseSection]

    @model_validator(mode="after")
    def validate_complete_config(self) -> ResearchCoreConfig:
        if set(self.confidence_caps) != set(ResearchCoverageStatus):
            raise ValueError("research confidence caps must cover every coverage status")
        if any(value < 0 or value > 1 for value in self.confidence_caps.values()):
            raise ValueError("research confidence caps must be within 0..1")
        if set(self.required_sections) != set(BASE_CASE_SECTIONS):
            raise ValueError("research core config must list every BaseCase section")
        if len(self.required_sections) != len(set(self.required_sections)):
            raise ValueError("BaseCase config sections must be unique")
        return self


class EvidenceFreezeRequest(AStockModel):
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    claim_ids: list[str] = Field(default_factory=list)
    formal_historical: bool = True
    allow_approximated: bool = False

    @model_validator(mode="after")
    def validate_claim_scope(self) -> EvidenceFreezeRequest:
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise ValueError("evidence freeze claim ids must be unique")
        if self.allow_approximated and not self.formal_historical:
            raise ValueError("allow_approximated only applies to formal historical mode")
        return self


class FrozenEvidencePack(AStockModel):
    pack_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    formal_historical: bool
    allow_approximated: bool
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    conflict_ids: list[str]
    open_conflict_ids: list[str]
    evidence_grade_by_id: dict[str, EvidenceGrade]
    pit_id_by_evidence_id: dict[str, str | None]
    pit_status_by_evidence_id: dict[str, PointInTimeStatus | None]
    missing_pit_evidence_ids: list[str]
    coverage_status: ResearchCoverageStatus
    degradation_codes: list[str]
    frozen_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def validate_frozen_scope(self) -> FrozenEvidencePack:
        for label, values in (
            ("claim", self.claim_ids),
            ("evidence", self.evidence_ids),
            ("conflict", self.conflict_ids),
            ("open conflict", self.open_conflict_ids),
            ("missing PIT evidence", self.missing_pit_evidence_ids),
            ("degradation code", self.degradation_codes),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"frozen {label} values must be unique")
        evidence_set = set(self.evidence_ids)
        if set(self.evidence_grade_by_id) != evidence_set:
            raise ValueError("every frozen evidence requires one evidence grade")
        if set(self.pit_id_by_evidence_id) != evidence_set:
            raise ValueError("every frozen evidence requires a PIT id entry")
        if set(self.pit_status_by_evidence_id) != evidence_set:
            raise ValueError("every frozen evidence requires a PIT status entry")
        if set(self.missing_pit_evidence_ids) != {
            evidence_id
            for evidence_id, pit_id in self.pit_id_by_evidence_id.items()
            if pit_id is None
        }:
            raise ValueError("missing PIT evidence list must match PIT id entries")
        if not set(self.open_conflict_ids).issubset(self.conflict_ids):
            raise ValueError("open conflicts must be included in all conflicts")
        return self


class ResearchFindingInput(AStockModel):
    statement: str = Field(min_length=1)
    finding_type: ResearchFindingType
    confidence: float = Field(ge=0, le=1)
    critical: bool = True
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> ResearchFindingInput:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("research finding evidence ids must be unique")
        return self


class CitedResearchFinding(AStockModel):
    finding_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    finding_type: ResearchFindingType
    confidence: float = Field(ge=0, le=1)
    critical: bool = True
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> CitedResearchFinding:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("cited research finding evidence ids must be unique")
        return self


class ResearchGapInput(AStockModel):
    gap_code: str = Field(min_length=1)
    severity: ResearchGapSeverity
    decision_impact: str = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_required_evidence(self) -> ResearchGapInput:
        if len(self.required_evidence) != len(set(self.required_evidence)):
            raise ValueError("research gap evidence requirements must be unique")
        return self


class ResearchGap(AStockModel):
    gap_id: str = Field(min_length=1)
    gap_code: str = Field(min_length=1)
    severity: ResearchGapSeverity
    decision_impact: str = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)


class BaseCaseDraft(AStockModel):
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    findings_by_section: dict[BaseCaseSection, list[ResearchFindingInput]]
    evidence_gaps: list[ResearchGapInput]
    specialist_tags: list[str]
    requested_base_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_sections(self) -> BaseCaseDraft:
        if set(self.findings_by_section) != set(BASE_CASE_SECTIONS):
            raise ValueError("BaseCase draft must include every section")
        if len(self.specialist_tags) != len(set(self.specialist_tags)):
            raise ValueError("BaseCase specialist tags must be unique")
        gap_codes = [gap.gap_code for gap in self.evidence_gaps]
        if len(gap_codes) != len(set(gap_codes)):
            raise ValueError("BaseCase gap codes must be unique")
        return self


class BaseCaseBuildRequest(AStockModel):
    evidence_pack_id: str = Field(min_length=1)
    draft: BaseCaseDraft


class BaseCasePack(AStockModel):
    base_case_id: str = Field(min_length=1)
    evidence_pack_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    kernel_version: str = Field(min_length=1)
    findings_by_section: dict[BaseCaseSection, list[CitedResearchFinding]]
    evidence_gaps: list[ResearchGap]
    specialist_tags: list[str]
    requested_base_confidence: float = Field(ge=0, le=1)
    base_confidence: float = Field(ge=0, le=1)
    confidence_cap: float = Field(ge=0, le=1)
    coverage_by_section: dict[BaseCaseSection, float]
    coverage_status: ResearchCoverageStatus
    degradation_codes: list[str]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_pack_conservation(self) -> BaseCasePack:
        if set(self.findings_by_section) != set(BASE_CASE_SECTIONS):
            raise ValueError("BaseCase pack must include every finding section")
        if set(self.coverage_by_section) != set(BASE_CASE_SECTIONS):
            raise ValueError("BaseCase pack must include every section coverage")
        if any(value < 0 or value > 1 for value in self.coverage_by_section.values()):
            raise ValueError("BaseCase section coverage must be within 0..1")
        findings = [
            finding
            for section in BASE_CASE_SECTIONS
            for finding in self.findings_by_section[section]
        ]
        finding_ids = [finding.finding_id for finding in findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("BaseCase finding ids must be unique")
        expected_evidence = sorted(
            {
                evidence_id
                for finding in findings
                for evidence_id in finding.evidence_ids
            }
        )
        if self.evidence_ids != expected_evidence:
            raise ValueError("BaseCase evidence ids must equal the cited finding union")
        if self.base_confidence > self.confidence_cap:
            raise ValueError("BaseCase confidence cannot exceed its coverage cap")
        if self.base_confidence > self.requested_base_confidence:
            raise ValueError("BaseCase confidence cannot exceed the requested confidence")
        for label, values in (
            ("specialist tag", self.specialist_tags),
            ("degradation code", self.degradation_codes),
            ("evidence", self.evidence_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"BaseCase {label} values must be unique")
        return self


class ResearchSkillKind(StrEnum):
    INDUSTRY_BOTTLENECK = "INDUSTRY_BOTTLENECK"
    EVENT_TO_ALPHA = "EVENT_TO_ALPHA"
    GROWTH_PROBABILITY = "GROWTH_PROBABILITY"
    GROWTH_VALUATION = "GROWTH_VALUATION"
    DAILY_TREND_HEALTH = "DAILY_TREND_HEALTH"
    HOURLY_SWING = "HOURLY_SWING"
    RESEARCH_MEMO = "RESEARCH_MEMO"


class ResearchSkillStatus(StrEnum):
    ENABLED_CONTRACT = "ENABLED_CONTRACT"
    PENDING = "PENDING"
    DISABLED = "DISABLED"


class ResearchCostClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"


class SpecialistCoverageStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class SpecialistEligibility(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class AdjustmentDirection(StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    NEUTRAL = "NEUTRAL"


class ResearchSkillManifest(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    kind: ResearchSkillKind
    source_references: list[str] = Field(min_length=1)
    trigger_tags: list[str]
    industry_tags: list[str]
    event_tags: list[str]
    horizons: list[str]
    required_inputs: list[str]
    optional_input_degradation_codes: dict[str, str]
    required_frequencies: list[str]
    required_evidence_grades: list[EvidenceGrade]
    reasoning_steps: list[str] = Field(min_length=1)
    positive_signals: list[str] = Field(min_length=1)
    negative_signals: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    known_failure_modes: list[str] = Field(min_length=1)
    output_schema: str = Field(min_length=1)
    cost_class: ResearchCostClass
    dependencies: list[str]
    incompatible_skills: list[str]
    counts_as_specialist: bool
    status: ResearchSkillStatus

    @model_validator(mode="after")
    def validate_manifest_sets(self) -> ResearchSkillManifest:
        list_fields = {
            "source references": self.source_references,
            "trigger tags": self.trigger_tags,
            "industry tags": self.industry_tags,
            "event tags": self.event_tags,
            "horizons": self.horizons,
            "required inputs": self.required_inputs,
            "required frequencies": self.required_frequencies,
            "required evidence grades": self.required_evidence_grades,
            "dependencies": self.dependencies,
            "incompatible skills": self.incompatible_skills,
        }
        for label, values in list_fields.items():
            if len(values) != len(set(values)):
                raise ValueError(f"research Skill {label} must be unique")
        overlap = set(self.required_inputs) & set(self.optional_input_degradation_codes)
        if overlap:
            raise ValueError("required and optional Skill inputs cannot overlap")
        if self.kind is ResearchSkillKind.RESEARCH_MEMO and self.counts_as_specialist:
            raise ValueError("ResearchMemoComposer cannot consume a specialist slot")
        if self.kind is not ResearchSkillKind.RESEARCH_MEMO and not self.counts_as_specialist:
            raise ValueError("analysis Skills must consume a specialist slot")
        return self


class ResearchSkillRegistry(AStockModel):
    registry_version: str = Field(min_length=1)
    open_source_audit_manifest_files: list[str] = Field(default_factory=list)
    max_specialists: int = Field(ge=1, le=3)
    coverage_confidence_caps: dict[SpecialistCoverageStatus, float]
    skills: list[ResearchSkillManifest] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> ResearchSkillRegistry:
        if len(self.open_source_audit_manifest_files) != len(
            set(self.open_source_audit_manifest_files)
        ):
            raise ValueError("open-source audit manifest files must be unique")
        skill_ids = [skill.skill_id for skill in self.skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("research Skill ids must be unique")
        identities = [(skill.skill_id, skill.skill_version) for skill in self.skills]
        if len(identities) != len(set(identities)):
            raise ValueError("research Skill identities must be unique")
        if set(self.coverage_confidence_caps) != set(SpecialistCoverageStatus):
            raise ValueError("specialist confidence caps must cover every coverage status")
        if any(value < 0 or value > 1 for value in self.coverage_confidence_caps.values()):
            raise ValueError("specialist confidence caps must be within 0..1")
        memo_count = sum(
            skill.kind is ResearchSkillKind.RESEARCH_MEMO for skill in self.skills
        )
        if memo_count != 1:
            raise ValueError("research Skill registry requires one ResearchMemoComposer")
        known = set(skill_ids)
        for skill in self.skills:
            unknown = (set(skill.dependencies) | set(skill.incompatible_skills)) - known
            if unknown:
                raise ValueError(
                    f"Skill {skill.skill_id} references unknown Skills: "
                    + ", ".join(sorted(unknown))
                )
        return self


class SpecialistRouteRequest(AStockModel):
    base_case_id: str = Field(min_length=1)
    thesis_tags: list[str]
    industry_tags: list[str]
    event_tags: list[str]
    horizon: str = Field(min_length=1)
    available_inputs: list[str]
    available_frequencies: list[str]
    explicit_skill_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_route_scope(self) -> SpecialistRouteRequest:
        for label, values in (
            ("thesis tags", self.thesis_tags),
            ("industry tags", self.industry_tags),
            ("event tags", self.event_tags),
            ("available inputs", self.available_inputs),
            ("available frequencies", self.available_frequencies),
            ("explicit Skill ids", self.explicit_skill_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"specialist route {label} must be unique")
        return self


class SpecialistRouteMatch(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    score: int = Field(ge=0)
    eligibility: SpecialistEligibility
    reason_codes: list[str] = Field(min_length=1)
    missing_required_inputs: list[str]
    missing_required_frequencies: list[str]
    degradation_codes: list[str]

    @model_validator(mode="after")
    def validate_route_match(self) -> SpecialistRouteMatch:
        for values in (
            self.reason_codes,
            self.missing_required_inputs,
            self.missing_required_frequencies,
            self.degradation_codes,
        ):
            if len(values) != len(set(values)):
                raise ValueError("specialist route match values must be unique")
        if self.eligibility is SpecialistEligibility.UNAVAILABLE and not (
            self.missing_required_inputs or self.missing_required_frequencies
        ):
            raise ValueError("unavailable specialist matches require a missing hard input")
        return self


class SpecialistRoutePlan(AStockModel):
    route_plan_id: str = Field(min_length=1)
    base_case_id: str = Field(min_length=1)
    evidence_pack_id: str = Field(min_length=1)
    registry_version: str = Field(min_length=1)
    selected: list[SpecialistRouteMatch]
    unavailable: list[SpecialistRouteMatch]
    excluded_skill_reasons: dict[str, list[str]]
    coverage_status: SpecialistCoverageStatus
    confidence_cap: float = Field(ge=0, le=1)
    max_specialists: int = Field(ge=1, le=3)
    degradation_codes: list[str]

    @model_validator(mode="after")
    def validate_route_plan(self) -> SpecialistRoutePlan:
        if len(self.selected) > self.max_specialists:
            raise ValueError("specialist route exceeds its declared maximum")
        selected_ids = [item.skill_id for item in self.selected]
        unavailable_ids = [item.skill_id for item in self.unavailable]
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("selected specialist Skills must be unique")
        if len(unavailable_ids) != len(set(unavailable_ids)):
            raise ValueError("unavailable specialist Skills must be unique")
        if set(selected_ids) & set(unavailable_ids):
            raise ValueError("a Skill cannot be both selected and unavailable")
        if any(item.eligibility is SpecialistEligibility.UNAVAILABLE for item in self.selected):
            raise ValueError("unavailable Skills cannot be selected")
        if any(
            item.eligibility is not SpecialistEligibility.UNAVAILABLE
            for item in self.unavailable
        ):
            raise ValueError("unavailable list can only contain unavailable matches")
        if len(self.degradation_codes) != len(set(self.degradation_codes)):
            raise ValueError("specialist route degradation codes must be unique")
        return self


class SpecialistMetricInput(AStockModel):
    metric_name: str = Field(min_length=1)
    value: float | str
    unit: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SpecialistMetric(AStockModel):
    metric_id: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    value: float | str
    unit: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SpecialistAdjustmentInput(AStockModel):
    dimension: str = Field(min_length=1)
    direction: AdjustmentDirection
    magnitude: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SpecialistAdjustment(AStockModel):
    adjustment_id: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    direction: AdjustmentDirection
    magnitude: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SpecialistEvidenceRequest(AStockModel):
    request_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    required_evidence: list[str] = Field(min_length=1)
    blocking: bool


class SpecialistDeltaBuildRequest(AStockModel):
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    incremental_findings: list[ResearchFindingInput]
    base_case_corrections: list[ResearchFindingInput]
    industry_specific_metrics: list[SpecialistMetricInput]
    additional_evidence_requests: list[SpecialistEvidenceRequest]
    failure_modes: list[str]
    confidence_delta: float = Field(ge=-0.25, le=0.25)
    valuation_adjustments: list[SpecialistAdjustmentInput]
    risk_adjustments: list[SpecialistAdjustmentInput]
    coverage_delta: dict[BaseCaseSection, float]
    method_contract: SerenityMethodContractV2 | None = None

    @model_validator(mode="after")
    def validate_delta_request(self) -> SpecialistDeltaBuildRequest:
        if any(value < -1 or value > 1 for value in self.coverage_delta.values()):
            raise ValueError("specialist coverage deltas must be within -1..1")
        request_codes = [item.request_code for item in self.additional_evidence_requests]
        if len(request_codes) != len(set(request_codes)):
            raise ValueError("specialist evidence request codes must be unique")
        if len(self.failure_modes) != len(set(self.failure_modes)):
            raise ValueError("specialist failure modes must be unique")
        expected_v2 = _SERENITY_V2_SKILL_VERSIONS.get(self.skill_id)
        if self.skill_version == expected_v2:
            if self.method_contract is None:
                raise ValueError("v2 SpecialistDelta requires its method contract")
            if self.method_contract.contract_version != self.skill_version:
                raise ValueError("method contract version must match the selected Skill")
        elif self.method_contract is not None:
            raise ValueError("v1 SpecialistDelta cannot carry a v2 method contract")
        return self


class SpecialistDelta(AStockModel):
    delta_id: str = Field(min_length=1)
    base_case_id: str = Field(min_length=1)
    evidence_pack_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    incremental_findings: list[CitedResearchFinding]
    base_case_corrections: list[CitedResearchFinding]
    industry_specific_metrics: list[SpecialistMetric]
    additional_evidence_requests: list[SpecialistEvidenceRequest]
    failure_modes: list[str]
    confidence_delta: float = Field(ge=-0.25, le=0.25)
    valuation_adjustments: list[SpecialistAdjustment]
    risk_adjustments: list[SpecialistAdjustment]
    coverage_delta: dict[BaseCaseSection, float]
    evidence_ids: list[str]
    method_contract: SerenityMethodContractV2 | None = None

    @model_validator(mode="after")
    def validate_delta_conservation(self) -> SpecialistDelta:
        if any(value < -1 or value > 1 for value in self.coverage_delta.values()):
            raise ValueError("specialist coverage deltas must be within -1..1")
        cited = {
            evidence_id
            for item in (
                *self.incremental_findings,
                *self.base_case_corrections,
                *self.industry_specific_metrics,
                *self.valuation_adjustments,
                *self.risk_adjustments,
            )
            for evidence_id in item.evidence_ids
        }
        if self.method_contract is not None:
            cited.update(self.method_contract.evidence_ids)
        if self.evidence_ids != sorted(cited):
            raise ValueError("SpecialistDelta evidence ids must equal the cited union")
        if len(self.failure_modes) != len(set(self.failure_modes)):
            raise ValueError("SpecialistDelta failure modes must be unique")
        expected_v2 = _SERENITY_V2_SKILL_VERSIONS.get(self.skill_id)
        if self.skill_version == expected_v2:
            if self.method_contract is None:
                raise ValueError("v2 SpecialistDelta requires its method contract")
            if self.method_contract.contract_version != self.skill_version:
                raise ValueError("method contract version must match the selected Skill")
        elif self.method_contract is not None:
            raise ValueError("v1 SpecialistDelta cannot carry a v2 method contract")
        return self


__all__ = [
    "BASE_CASE_SECTIONS",
    "ResearchRequest",
    "EvidenceCollectionTask",
    "EvidenceCollectionRunStatus",
    "EvidenceCollectionRun",
    "EvidencePack",
    "ResearchPreparationManifest",
    "ResearchPreparationRequest",
    "ResearchPreparationStatus",
    "ResearchRequestModule",
    "BaseCaseBuildRequest",
    "BaseCaseDraft",
    "BaseCasePack",
    "BaseCaseSection",
    "AdjustmentDirection",
    "CitedResearchFinding",
    "EvidenceFreezeRequest",
    "FrozenEvidencePack",
    "ResearchCoreConfig",
    "ResearchCoverageStatus",
    "ResearchFindingInput",
    "ResearchFindingType",
    "ResearchGap",
    "ResearchGapInput",
    "ResearchGapSeverity",
    "ResearchCostClass",
    "ResearchSkillKind",
    "ResearchSkillManifest",
    "ResearchSkillRegistry",
    "ResearchSkillStatus",
    "SpecialistAdjustment",
    "SpecialistAdjustmentInput",
    "SpecialistCoverageStatus",
    "SpecialistDelta",
    "SpecialistDeltaBuildRequest",
    "SpecialistEligibility",
    "SpecialistEvidenceRequest",
    "SpecialistMetric",
    "SpecialistMetricInput",
    "SpecialistRouteMatch",
    "SpecialistRoutePlan",
    "SpecialistRouteRequest",
]
