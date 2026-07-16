"""Strict contracts for point-in-time financial-integrity audits."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.runs import RunStatus


class FinancialStatementType(StrEnum):
    BALANCE_SHEET = "BALANCE_SHEET"
    INCOME_STATEMENT = "INCOME_STATEMENT"
    CASH_FLOW_STATEMENT = "CASH_FLOW_STATEMENT"
    NOTES = "NOTES"


class FinancialPeriodType(StrEnum):
    ANNUAL = "ANNUAL"
    SEMIANNUAL = "SEMIANNUAL"
    QUARTERLY = "QUARTERLY"


class FinancialIndustryProfile(StrEnum):
    GENERAL_INDUSTRIAL = "GENERAL_INDUSTRIAL"
    BANK = "BANK"
    INSURANCE = "INSURANCE"
    SECURITIES = "SECURITIES"
    REAL_ESTATE = "REAL_ESTATE"
    EARLY_BIOTECH = "EARLY_BIOTECH"
    OTHER = "OTHER"


class FinancialUnit(StrEnum):
    CNY = "CNY"
    THOUSAND_CNY = "THOUSAND_CNY"
    TEN_THOUSAND_CNY = "TEN_THOUSAND_CNY"
    MILLION_CNY = "MILLION_CNY"
    HUNDRED_MILLION_CNY = "HUNDRED_MILLION_CNY"
    RATIO = "RATIO"
    PERCENT = "PERCENT"
    SHARES = "SHARES"
    CNY_PER_SHARE = "CNY_PER_SHARE"


class FinancialFieldCode(StrEnum):
    TOTAL_ASSETS = "TOTAL_ASSETS"
    TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
    TOTAL_EQUITY = "TOTAL_EQUITY"
    CASH_BEGINNING = "CASH_BEGINNING"
    CASH_ENDING = "CASH_ENDING"
    NET_CASH_OPERATING = "NET_CASH_OPERATING"
    NET_CASH_INVESTING = "NET_CASH_INVESTING"
    NET_CASH_FINANCING = "NET_CASH_FINANCING"
    EXCHANGE_EFFECT = "EXCHANGE_EFFECT"
    NET_PROFIT_INCOME = "NET_PROFIT_INCOME"
    NET_PROFIT_CASH_FLOW = "NET_PROFIT_CASH_FLOW"
    REVENUE = "REVENUE"
    OPERATING_COST = "OPERATING_COST"
    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE"
    INVENTORY = "INVENTORY"
    PREPAYMENTS = "PREPAYMENTS"
    OTHER_RECEIVABLES = "OTHER_RECEIVABLES"
    CURRENT_ASSETS = "CURRENT_ASSETS"
    CURRENT_LIABILITIES = "CURRENT_LIABILITIES"
    TOTAL_DEBT = "TOTAL_DEBT"
    RETAINED_EARNINGS = "RETAINED_EARNINGS"
    EBIT = "EBIT"


class FinancialSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FinancialRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FinancialCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class FinancialFindingStatus(StrEnum):
    PASS = "PASS"
    FLAG = "FLAG"
    CALCULATED = "CALCULATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    CONFLICTED = "CONFLICTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FinancialGapType(StrEnum):
    MISSING_FACT = "MISSING_FACT"
    MISSING_SNAPSHOT_REFERENCE = "MISSING_SNAPSHOT_REFERENCE"
    UNKNOWN_SNAPSHOT = "UNKNOWN_SNAPSHOT"
    SNAPSHOT_NOT_AVAILABLE = "SNAPSHOT_NOT_AVAILABLE"
    SNAPSHOT_OBJECT_MISSING = "SNAPSHOT_OBJECT_MISSING"
    SNAPSHOT_FETCH_INCOMPLETE = "SNAPSHOT_FETCH_INCOMPLETE"
    MISSING_PIT_REFERENCE = "MISSING_PIT_REFERENCE"
    UNKNOWN_PIT = "UNKNOWN_PIT"
    PIT_NOT_USABLE = "PIT_NOT_USABLE"
    LINEAGE_MISMATCH = "LINEAGE_MISMATCH"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    EVIDENCE_NOT_USABLE = "EVIDENCE_NOT_USABLE"
    UNSUITABLE_EVIDENCE_GRADE = "UNSUITABLE_EVIDENCE_GRADE"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    STATEMENT_TYPE_MISMATCH = "STATEMENT_TYPE_MISMATCH"
    CONFLICTING_VALUES = "CONFLICTING_VALUES"
    CAPABILITY_DISABLED = "CAPABILITY_DISABLED"


class FinancialRuleOutputType(StrEnum):
    IDENTITY = "IDENTITY"
    METRIC_ONLY = "METRIC_ONLY"
    SCORE = "SCORE"


class FinancialCalibrationStatus(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    NOT_CALIBRATED = "NOT_CALIBRATED"
    DEFERRED = "DEFERRED"


class FinancialImplementationStatus(StrEnum):
    IMPLEMENTED_M3_1 = "IMPLEMENTED_M3_1"
    DEFERRED_M3_2 = "DEFERRED_M3_2"
    DEFERRED_M3_3 = "DEFERRED_M3_3"


class FinancialFact(AStockModel):
    """One reported number with immutable source, PIT, and evidence lineage."""

    fact_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    period_start: date | None = None
    period_end: date
    period_type: FinancialPeriodType
    statement_type: FinancialStatementType
    field_code: FinancialFieldCode
    reported_value: Decimal = Field(allow_inf_nan=False)
    unit: FinancialUnit
    source_snapshot_id: str | None = None
    pit_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_period_and_evidence(self) -> FinancialFact:
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start must not follow period_end")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique within a fact")
        return self


class FinancialAuditRequest(AStockModel):
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    industry_profile: FinancialIndustryProfile
    facts: list[FinancialFact] = Field(default_factory=list)
    requested_rule_ids: list[str] = Field(default_factory=list)
    formal_historical: bool = True
    allow_approximated_pit: bool = False

    @model_validator(mode="after")
    def validate_request_identity(self) -> FinancialAuditRequest:
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_id values must be unique")
        mismatched = sorted(
            fact.fact_id for fact in self.facts if fact.company_id != self.company_id
        )
        if mismatched:
            raise ValueError(f"facts belong to another company: {', '.join(mismatched)}")
        if len(self.requested_rule_ids) != len(set(self.requested_rule_ids)):
            raise ValueError("requested_rule_ids must be unique")
        if self.allow_approximated_pit and not self.formal_historical:
            raise ValueError("allow_approximated_pit only applies to formal historical audits")
        return self


class FinancialRuleDefinition(AStockModel):
    rule_id: str
    formula_version: str
    source_reference: str
    applicable_industries: list[FinancialIndustryProfile]
    excluded_industries: list[FinancialIndustryProfile] = Field(default_factory=list)
    required_fields: list[FinancialFieldCode]
    minimum_periods: int = Field(default=1, ge=1)
    threshold_source: str
    calibration_status: FinancialCalibrationStatus
    false_positive_modes: list[str] = Field(default_factory=list)
    severity: FinancialSeverity
    output_type: FinancialRuleOutputType
    tests: list[str] = Field(min_length=1)
    calculator_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    implementation_status: FinancialImplementationStatus
    default_enabled: bool = False

    @model_validator(mode="after")
    def validate_industry_sets(self) -> FinancialRuleDefinition:
        overlap = set(self.applicable_industries) & set(self.excluded_industries)
        if overlap:
            raise ValueError(f"industry cannot be both applicable and excluded: {sorted(overlap)}")
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("required_fields must be unique")
        return self


class FinancialRuleRegistry(AStockModel):
    registry_version: str
    compatible_engine_version: str
    rules: list[FinancialRuleDefinition]

    @model_validator(mode="after")
    def validate_rule_ids(self) -> FinancialRuleRegistry:
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("financial rule ids must be unique")
        return self


class FinancialIndustryProfileDefinition(AStockModel):
    profile_id: FinancialIndustryProfile
    profile_version: str
    description: str
    excluded_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class FinancialIndustryProfileRegistry(AStockModel):
    registry_version: str
    profiles: list[FinancialIndustryProfileDefinition]

    @model_validator(mode="after")
    def validate_profile_ids(self) -> FinancialIndustryProfileRegistry:
        ids = [profile.profile_id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("financial industry profile ids must be unique")
        return self


class VerifiedFinancialNumber(AStockModel):
    field_code: FinancialFieldCode
    statement_type: FinancialStatementType
    period_start: date | None = None
    period_end: date
    period_type: FinancialPeriodType
    value_cny: Decimal
    reporting_quantum_cny: Decimal = Field(gt=0)
    fact_ids: list[str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    pit_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class RecalculatedFinancialMetric(AStockModel):
    metric_id: str
    rule_id: str
    period_end: date
    value: Decimal
    unit: FinancialUnit
    formula: str
    formula_version: str
    input_field_codes: list[FinancialFieldCode]
    input_fact_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class FinancialEvidenceGap(AStockModel):
    gap_id: str
    gap_type: FinancialGapType
    detail_code: str
    period_end: date | None = None
    field_codes: list[FinancialFieldCode] = Field(default_factory=list)
    related_fact_ids: list[str] = Field(default_factory=list)
    related_rule_ids: list[str] = Field(default_factory=list)
    safe_evidence_ids: list[str] = Field(default_factory=list)


class FinancialManualTask(AStockModel):
    task_id: str
    audit_run_id: str
    status: Literal["OPEN"] = "OPEN"
    reason_code: str
    required_action_code: str
    related_gap_ids: list[str] = Field(min_length=1)


class FinancialDocumentConflict(AStockModel):
    conflict_id: str
    period_end: date
    period_type: FinancialPeriodType
    field_code: FinancialFieldCode
    fact_ids: list[str] = Field(min_length=2)
    normalized_values_cny: list[Decimal] = Field(min_length=2)
    evidence_ids: list[str] = Field(min_length=1)
    resolution_status: Literal["OPEN"] = "OPEN"


class FinancialRuleFinding(AStockModel):
    finding_id: str
    rule_id: str
    formula_version: str
    period_end: date | None = None
    status: FinancialFindingStatus
    severity: FinancialSeverity
    message_code: str
    actual_value: Decimal | None = None
    threshold_value: Decimal | None = None
    unit: FinancialUnit | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_gap_ids: list[str] = Field(default_factory=list)
    applicability_reason_code: str | None = None

    @model_validator(mode="after")
    def validate_support(self) -> FinancialRuleFinding:
        if "FRAUD" in self.message_code.upper() or "造假" in self.message_code:
            raise ValueError("financial findings cannot assert fraud")
        if self.status is FinancialFindingStatus.NOT_APPLICABLE:
            if self.applicability_reason_code is None:
                raise ValueError("NOT_APPLICABLE requires an applicability reason")
        elif not self.evidence_ids and not self.evidence_gap_ids:
            raise ValueError("a financial finding requires evidence or an explicit evidence gap")
        return self


class FinancialAnomaly(AStockModel):
    anomaly_id: str
    model_id: str
    model_version: str
    severity: FinancialSeverity
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_gap_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_support(self) -> FinancialAnomaly:
        if not self.evidence_ids and not self.evidence_gap_ids:
            raise ValueError("an anomaly requires evidence or an explicit evidence gap")
        return self


class FinancialBenignExplanation(AStockModel):
    explanation_id: str
    explanation_code: str
    related_finding_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class FinancialIntegrityEvidencePack(AStockModel):
    """Advisory-only output; it cannot mutate trading or risk hard blocks."""

    audit_run_id: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RunStatus
    coverage_status: FinancialCoverageStatus
    company_id: str
    as_of: AwareDatetime
    industry_profile: FinancialIndustryProfile
    periods: list[date]
    input_fact_ids: list[str]
    source_snapshot_ids: list[str]
    pit_ids: list[str]
    verified_numbers: list[VerifiedFinancialNumber]
    recalculated_metrics: list[RecalculatedFinancialMetric]
    rule_findings: list[FinancialRuleFinding]
    time_series_anomalies: list[FinancialAnomaly] = Field(default_factory=list)
    peer_anomalies: list[FinancialAnomaly] = Field(default_factory=list)
    document_conflicts: list[FinancialDocumentConflict] = Field(default_factory=list)
    governance_findings: list[FinancialRuleFinding] = Field(default_factory=list)
    benign_explanations: list[FinancialBenignExplanation] = Field(default_factory=list)
    evidence_gaps: list[FinancialEvidenceGap] = Field(default_factory=list)
    manual_tasks: list[FinancialManualTask] = Field(default_factory=list)
    risk_level: FinancialRiskLevel
    hard_blocks: list[str] = Field(default_factory=list)
    advisory_only: Literal[True] = True
    rule_versions: dict[str, str]
    model_versions: dict[str, str]
    capability_status: dict[str, str]

    @model_validator(mode="after")
    def validate_pack_integrity(self) -> FinancialIntegrityEvidencePack:
        if self.status not in {RunStatus.SUCCEEDED, RunStatus.NEEDS_INFO}:
            raise ValueError("financial evidence packs must be terminal")
        if self.hard_blocks:
            raise ValueError("financial-integrity audits cannot create risk hard blocks")
        if self.periods != sorted(set(self.periods)):
            raise ValueError("periods must be sorted and unique")
        gap_ids = {gap.gap_id for gap in self.evidence_gaps}
        for finding in [*self.rule_findings, *self.governance_findings]:
            unknown = set(finding.evidence_gap_ids) - gap_ids
            if unknown:
                raise ValueError(f"finding references unknown evidence gaps: {sorted(unknown)}")
        for anomaly in [*self.time_series_anomalies, *self.peer_anomalies]:
            unknown = set(anomaly.evidence_gap_ids) - gap_ids
            if unknown:
                raise ValueError(f"anomaly references unknown evidence gaps: {sorted(unknown)}")
        for task in self.manual_tasks:
            unknown = set(task.related_gap_ids) - gap_ids
            if unknown:
                raise ValueError(f"manual task references unknown evidence gaps: {sorted(unknown)}")
        if self.status is RunStatus.SUCCEEDED and self.evidence_gaps:
            raise ValueError("SUCCEEDED financial audits cannot contain unresolved evidence gaps")
        if self.status is RunStatus.NEEDS_INFO and not self.evidence_gaps:
            raise ValueError("NEEDS_INFO financial audits require at least one evidence gap")
        return self
