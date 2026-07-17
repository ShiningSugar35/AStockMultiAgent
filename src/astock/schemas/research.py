"""Frozen-evidence and common BaseCase research contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.evidence import EvidenceGrade
from astock.schemas.pit import PointInTimeStatus


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


__all__ = [
    "BASE_CASE_SECTIONS",
    "BaseCaseBuildRequest",
    "BaseCaseDraft",
    "BaseCasePack",
    "BaseCaseSection",
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
]
