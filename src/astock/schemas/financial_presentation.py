"""Advisory financial-presentation semantics. Never a financial-certification authority."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.financial import (
    FinancialDurationSemantics,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
)
from astock.schemas.financial_sources import FinancialStatementScope


class PresentationFramework(StrEnum):
    PRC_ASBE = "PRC_ASBE"
    IFRS = "IFRS"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class AdoptionClaim(StrEnum):
    CONFIRMED = "CONFIRMED"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    CONFLICTED = "CONFLICTED"


class PresentationSourceMode(StrEnum):
    CERTIFIED_RELEASE = "CERTIFIED_RELEASE"
    OFFICIAL_CAPTURE_ONLY = "OFFICIAL_CAPTURE_ONLY"


class PresentationReviewStatus(StrEnum):
    EVIDENCE_BOUND = "EVIDENCE_BOUND"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"


class FinancialPageAnchor(AStockModel):
    """Exact character interval in a verified, immutable official page text."""

    page_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _interval(self) -> FinancialPageAnchor:
        if self.end <= self.start or len(self.quote) != self.end - self.start:
            raise ValueError("anchor quote length/offset mismatch")
        return self


class FinancialLineSuggestion(AStockModel):
    """Suggested mapping, never a replacement for FinancialPdfCertifier."""

    field_code: FinancialFieldCode | None = None
    statement_type: FinancialStatementType
    statement_scope: FinancialStatementScope | None = None
    original_label: str = Field(min_length=1, max_length=180)
    numeric_text: str = Field(min_length=1, max_length=110)
    unit: FinancialUnit
    evidence: FinancialPageAnchor
    alternative_labels: list[str] = Field(default_factory=list, max_length=6)
    ambiguity_reason: str | None = Field(default=None, max_length=800)


class FinancialPresentationProposal(AStockModel):
    """LLM-authored hypothesis bound to one certified release or official capture."""

    schema_version: str = "financial-presentation-proposal-v1"
    company_id: str = Field(pattern=r"^\d{6}$")
    instrument_id: str = Field(pattern=r"^(XSHG|XSHE|BJSE):\d{6}$")
    source_mode: PresentationSourceMode = PresentationSourceMode.CERTIFIED_RELEASE
    release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    official_document_id: str = Field(min_length=1)
    official_snapshot_id: str = Field(min_length=1)
    official_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    period_end: date
    period_type: FinancialPeriodType
    as_of: AwareDatetime
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    framework: PresentationFramework = PresentationFramework.UNKNOWN
    standard_version_candidate: str | None = Field(default=None, max_length=100)
    adoption: AdoptionClaim = AdoptionClaim.NOT_CONFIRMED
    adoption_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    adoption_effective_date: date | None = None
    industry_profile_candidate: FinancialIndustryProfile | None = None
    industry_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    statement_scope: FinancialStatementScope | None = None
    scope_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    duration_semantics: FinancialDurationSemantics | None = None
    duration_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    comparative_period_end: date | None = None
    comparative_period_evidence: list[FinancialPageAnchor] = Field(
        default_factory=list, max_length=8
    )
    declared_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    currency_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    declared_unit: FinancialUnit | None = None
    unit_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    restatement_claim: str | None = Field(default=None, max_length=700)
    restatement_evidence: list[FinancialPageAnchor] = Field(default_factory=list, max_length=8)
    proposed_rule_ids: list[str] = Field(default_factory=list, max_length=30)
    lines: list[FinancialLineSuggestion] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _semantic_constraints(self) -> FinancialPresentationProposal:
        if self.schema_version != "financial-presentation-proposal-v1":
            raise ValueError("unsupported financial presentation proposal schema")
        if self.source_mode is PresentationSourceMode.CERTIFIED_RELEASE and self.release_id is None:
            raise ValueError("certified proposal requires a real financial release ID")
        if (
            self.source_mode is PresentationSourceMode.OFFICIAL_CAPTURE_ONLY
            and self.release_id is not None
        ):
            raise ValueError("uncertified capture cannot claim a certified financial release ID")
        if self.instrument_id.split(":", 1)[1] != self.company_id:
            raise ValueError("company and instrument identity mismatch")
        if self.adoption is AdoptionClaim.CONFIRMED and (
            self.framework is PresentationFramework.UNKNOWN
            or not self.standard_version_candidate
            or not self.adoption_evidence
        ):
            raise ValueError("confirmed standard adoption requires cited version and disclosure")
        if self.standard_version_candidate and self.framework is PresentationFramework.UNKNOWN:
            raise ValueError("unknown framework cannot claim a standard version")
        if self.adoption_effective_date is not None and not self.adoption_evidence:
            raise ValueError("adoption effective date requires issuer disclosure evidence")
        if self.comparative_period_end and self.comparative_period_end > self.period_end:
            raise ValueError("comparative period must precede or equal current period")
        for value, evidence, name in (
            (self.comparative_period_end, self.comparative_period_evidence, "comparative period"),
            (self.declared_currency, self.currency_evidence, "currency"),
            (self.declared_unit, self.unit_evidence, "unit"),
            (self.industry_profile_candidate, self.industry_evidence, "industry"),
            (self.statement_scope, self.scope_evidence, "scope"),
            (self.duration_semantics, self.duration_evidence, "duration"),
            (self.restatement_claim, self.restatement_evidence, "restatement"),
        ):
            if value is not None and not evidence:
                raise ValueError(f"{name} interpretation needs source anchors")
        if len(self.proposed_rule_ids) != len(set(self.proposed_rule_ids)):
            raise ValueError("rule suggestions must be unique")
        return self


class FinancialLineReview(AStockModel):
    field_code: FinancialFieldCode | None
    status: PresentationReviewStatus
    reason_codes: list[str] = Field(default_factory=list)
    exact_official_match: bool = False


class FinancialPresentationReview(AStockModel):
    schema_version: str = "financial-presentation-review-v1"
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: PresentationReviewStatus
    reason_codes: list[str] = Field(default_factory=list)
    anchored_claim_count: int = Field(ge=0)
    checked_line_count: int = Field(ge=0)
    line_reviews: list[FinancialLineReview] = Field(default_factory=list)
    proposed_industry_profile: FinancialIndustryProfile | None = None
    rule_suggestion_ids: list[str] = Field(default_factory=list)
    formal_financial_authority: bool = False
    active_parser_changed: bool = False
    published_recommendation: bool = False

    @model_validator(mode="after")
    def _no_promotion(self) -> FinancialPresentationReview:
        if (
            self.formal_financial_authority
            or self.active_parser_changed
            or self.published_recommendation
        ):
            raise ValueError("presentation shadow review cannot claim production authority")
        return self


class FinancialPresentationRulePreview(AStockModel):
    """Explanatory candidate rule selection, never a production audit decision."""

    schema_version: str = "financial-presentation-rule-preview-v1"
    release_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_version: str = Field(min_length=1)
    framework_candidate: PresentationFramework
    standard_version_candidate: str | None = None
    industry_profile_candidate: FinancialIndustryProfile | None = None
    core_rule_ids_preserved: list[str] = Field(default_factory=list)
    noncore_rule_candidates: list[str] = Field(default_factory=list)
    potentially_inapplicable_noncore_rules: list[str] = Field(default_factory=list)
    unresolved_reasons: list[str] = Field(default_factory=list)
    approval_status: str = "SHADOW_ONLY_NOT_ADMITTED"
    formal_financial_authority: bool = False

    @model_validator(mode="after")
    def _must_remain_shadow(self) -> FinancialPresentationRulePreview:
        if self.approval_status != "SHADOW_ONLY_NOT_ADMITTED" or self.formal_financial_authority:
            raise ValueError("semantic preview cannot grant rule authority")
        return self


class FinancialStandardPolicy(AStockModel):
    """Versioned production policy. Not a source of issuer adoption claims."""

    standard_id: str = Field(min_length=3, pattern=r"^[A-Z0-9_]+$")
    framework: PresentationFramework
    standard_version: str = Field(min_length=3)
    first_possible_adoption: date
    eligible_for_certified_a_share: bool
    official_reference: str = Field(pattern=r"^https://")
    accounting_core_rule_ids: list[str] = Field(min_length=2)
    noncore_rule_ids: list[str] = Field(default_factory=list)
    admission: str

    @model_validator(mode="after")
    def _unique_rules(self) -> FinancialStandardPolicy:
        codes = [*self.accounting_core_rule_ids, *self.noncore_rule_ids]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate accounting rules in standard policy")
        if self.framework is PresentationFramework.UNKNOWN:
            raise ValueError("unknown framework cannot be a production policy")
        return self


class FinancialStandardPolicyRegistry(AStockModel):
    schema_version: str = "financial-presentation-standards-v1"
    registry_version: str = Field(min_length=1)
    production_enabled: bool
    profiles: list[FinancialStandardPolicy] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_profiles(self) -> FinancialStandardPolicyRegistry:
        if self.schema_version != "financial-presentation-standards-v1":
            raise ValueError("unknown financial presentation standard registry schema")
        codes = [p.standard_id for p in self.profiles]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate financial standard policies")
        return self


class FinancialStandardAdmission(AStockModel):
    """Explicit independent approval allowing context-aware deterministic auditing.

    The operator's approval does not certify a new monetary number, extend the
    actual financial rule registry, or permit Portfolio / Publication.
    """

    schema_version: str = "financial-standard-admission-v1"
    policy_id: str = Field(min_length=3)
    policy_registry_version: str = Field(min_length=1)
    rule_registry_version: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_artifact_id: str = Field(
        pattern=r"^financial-presentation-review:[0-9a-f]{64}$"
    )
    company_id: str = Field(pattern=r"^\d{6}$")
    instrument_id: str = Field(pattern=r"^(XSHG|XSHE|BJSE):\d{6}$")
    financial_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_document_id: str = Field(min_length=1)
    official_snapshot_id: str = Field(min_length=1)
    official_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    period_end: date
    period_type: FinancialPeriodType
    accounting_standard_version: str = Field(min_length=3)
    framework: PresentationFramework
    industry_profile: FinancialIndustryProfile
    adoption_effective_date: date
    independently_reviewed_by: str = Field(min_length=3)
    independent_review_note: str = Field(min_length=12, max_length=2000)
    admitted_at: AwareDatetime
    status: str = "ACTIVE_EVIDENCE_GATED"
    formal_financial_certification_granted: bool = False
    portfolio_permission_granted: bool = False

    @model_validator(mode="after")
    def _bounds(self) -> FinancialStandardAdmission:
        if self.schema_version != "financial-standard-admission-v1":
            raise ValueError("unsupported financial standard admission version")
        if self.instrument_id.split(":", 1)[1] != self.company_id:
            raise ValueError("admission issuer mismatch")
        if self.adoption_effective_date > self.period_end:
            raise ValueError("future adoption cannot govern an earlier report")
        if self.status != "ACTIVE_EVIDENCE_GATED":
            raise ValueError("admission is not a supported production selection")
        if self.formal_financial_certification_granted or self.portfolio_permission_granted:
            raise ValueError("standard selection cannot grant facts or portfolio authority")
        return self
