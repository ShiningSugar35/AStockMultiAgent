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


class FinancialDurationSemantics(StrEnum):
    """How a reported duration value relates to its fiscal period."""

    INSTANT = "INSTANT"
    STANDALONE_PERIOD = "STANDALONE_PERIOD"
    YEAR_TO_DATE = "YEAR_TO_DATE"
    REPORTED_PERIOD = "REPORTED_PERIOD"


class FinancialDerivationType(StrEnum):
    TTM = "TTM"
    YEAR_OVER_YEAR = "YEAR_OVER_YEAR"
    QUARTER_OVER_QUARTER = "QUARTER_OVER_QUARTER"
    PER_SHARE = "PER_SHARE"


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
    SCORE = "SCORE"


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
    DEPRECIATION_AMORTIZATION = "DEPRECIATION_AMORTIZATION"
    SELLING_GENERAL_ADMIN_EXPENSE = "SELLING_GENERAL_ADMIN_EXPENSE"
    PROPERTY_PLANT_EQUIPMENT = "PROPERTY_PLANT_EQUIPMENT"
    LONG_TERM_DEBT = "LONG_TERM_DEBT"
    MARKET_CAP = "MARKET_CAP"
    SHARES_OUTSTANDING = "SHARES_OUTSTANDING"


class FinancialSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FinancialRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FinancialAnomalyModelType(StrEnum):
    ROBUST_Z_SCORE = "ROBUST_Z_SCORE"
    ISOLATION_FOREST = "ISOLATION_FOREST"
    PYOD_ECOD = "PYOD_ECOD"


class FinancialAnomalyScope(StrEnum):
    TIME_SERIES = "TIME_SERIES"
    PEER = "PEER"


class FinancialBenignContext(StrEnum):
    HIGH_GROWTH = "HIGH_GROWTH"
    ACQUISITION_RESTRUCTURING = "ACQUISITION_RESTRUCTURING"
    SEASONALITY = "SEASONALITY"
    REAL_ESTATE_PROJECT_ACCOUNTING = "REAL_ESTATE_PROJECT_ACCOUNTING"
    EARLY_BIOTECH_RND_PHASE = "EARLY_BIOTECH_RND_PHASE"
    ACCOUNTING_POLICY_CHANGE = "ACCOUNTING_POLICY_CHANGE"


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
    AMBIGUOUS_PERIOD_SEMANTICS = "AMBIGUOUS_PERIOD_SEMANTICS"
    INSUFFICIENT_PERIODS = "INSUFFICIENT_PERIODS"
    NON_CONTIGUOUS_PERIODS = "NON_CONTIGUOUS_PERIODS"
    PEER_COHORT_MISMATCH = "PEER_COHORT_MISMATCH"
    INSUFFICIENT_PEER_SAMPLE = "INSUFFICIENT_PEER_SAMPLE"
    PEER_LINEAGE_INVALID = "PEER_LINEAGE_INVALID"
    MODEL_INPUT_INVALID = "MODEL_INPUT_INVALID"
    MODEL_SAMPLE_INSUFFICIENT = "MODEL_SAMPLE_INSUFFICIENT"


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
    IMPLEMENTED_M3_2 = "IMPLEMENTED_M3_2"
    IMPLEMENTED_M3_3 = "IMPLEMENTED_M3_3"
    DEFERRED_M3_2 = "DEFERRED_M3_2"
    DEFERRED_M3_3 = "DEFERRED_M3_3"


class FinancialFact(AStockModel):
    """One reported number with immutable source, PIT, and evidence lineage."""

    fact_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    period_start: date | None = None
    period_end: date
    period_type: FinancialPeriodType
    duration_semantics: FinancialDurationSemantics = FinancialDurationSemantics.REPORTED_PERIOD
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
        if self.statement_type is FinancialStatementType.BALANCE_SHEET and (
            self.duration_semantics
            in {
                FinancialDurationSemantics.STANDALONE_PERIOD,
                FinancialDurationSemantics.YEAR_TO_DATE,
            }
        ):
            raise ValueError("balance-sheet facts cannot use duration-period semantics")
        if self.statement_type is not FinancialStatementType.BALANCE_SHEET and (
            self.duration_semantics is FinancialDurationSemantics.INSTANT
        ):
            raise ValueError("duration-statement facts cannot use INSTANT semantics")
        return self


class FinancialSeriesRequest(AStockModel):
    request_id: str = Field(min_length=1)
    derivation_type: FinancialDerivationType
    field_code: FinancialFieldCode
    period_end: date | None = None
    shares_field_code: FinancialFieldCode = FinancialFieldCode.SHARES_OUTSTANDING

    @model_validator(mode="after")
    def validate_series_request(self) -> FinancialSeriesRequest:
        if (
            self.derivation_type is FinancialDerivationType.PER_SHARE
            and self.field_code is FinancialFieldCode.SHARES_OUTSTANDING
        ):
            raise ValueError("shares outstanding cannot be divided by itself as a per-share metric")
        return self


class FinancialPeerObservation(AStockModel):
    observation_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    industry_profile: FinancialIndustryProfile
    metric_id: str = Field(min_length=1)
    formula_version: str = Field(min_length=1)
    period_end: date
    available_at: AwareDatetime
    value: Decimal = Field(allow_inf_nan=False)
    unit: FinancialUnit
    source_snapshot_ids: list[str] = Field(min_length=1)
    pit_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class FinancialPeerCohort(AStockModel):
    cohort_id: str = Field(min_length=1)
    industry_profile: FinancialIndustryProfile
    metric_id: str = Field(min_length=1)
    formula_version: str = Field(min_length=1)
    as_of: AwareDatetime
    minimum_sample_size: int = Field(default=8, ge=3)
    observations: list[FinancialPeerObservation] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cohort(self) -> FinancialPeerCohort:
        companies = [item.company_id for item in self.observations]
        if len(companies) != len(set(companies)):
            raise ValueError("peer cohort company_id values must be unique")
        for item in self.observations:
            if item.industry_profile is not self.industry_profile:
                raise ValueError("peer observation industry must match cohort")
            if item.metric_id != self.metric_id or item.formula_version != self.formula_version:
                raise ValueError("peer observation metric identity must match cohort")
            if item.available_at > self.as_of:
                raise ValueError("peer observation cannot be available after cohort as_of")
        return self


class FinancialAnomalySample(AStockModel):
    sample_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    period_end: date
    available_at: AwareDatetime
    producer: Literal["FINANCIAL_M3_2"] = "FINANCIAL_M3_2"
    feature_values: dict[str, Decimal] = Field(min_length=1)
    feature_formula_versions: dict[str, str] = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    pit_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    expected_anomaly: bool | None = None
    benign_contexts: list[FinancialBenignContext] = Field(default_factory=list)
    benign_context_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_feature_and_context_lineage(self) -> FinancialAnomalySample:
        if set(self.feature_values) != set(self.feature_formula_versions):
            raise ValueError("every anomaly feature requires exactly one formula version")
        if any(not value.is_finite() for value in self.feature_values.values()):
            raise ValueError("anomaly features must contain only finite decimal values")
        for values, label in (
            (self.source_snapshot_ids, "source_snapshot_ids"),
            (self.pit_ids, "pit_ids"),
            (self.evidence_ids, "evidence_ids"),
            (self.benign_context_evidence_ids, "benign_context_evidence_ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.benign_contexts and not self.benign_context_evidence_ids:
            raise ValueError("benign contexts require separate supporting evidence")
        return self


class FinancialAnomalyDataset(AStockModel):
    dataset_id: str = Field(min_length=1)
    as_of: AwareDatetime
    industry_profile: FinancialIndustryProfile
    peer_cohort_id: str = Field(min_length=1)
    feature_names: list[str] = Field(min_length=1)
    samples: list[FinancialAnomalySample] = Field(min_length=1)
    training_sample_ids: list[str] = Field(min_length=1)
    target_sample_id: str = Field(min_length=1)
    evaluation_sample_ids: list[str] = Field(default_factory=list)
    frozen: Literal[True] = True

    @model_validator(mode="after")
    def validate_frozen_dataset(self) -> FinancialAnomalyDataset:
        if len(self.feature_names) != len(set(self.feature_names)):
            raise ValueError("anomaly dataset feature names must be unique")
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("anomaly dataset sample ids must be unique")
        entity_periods = [
            (sample.company_id, sample.period_end) for sample in self.samples
        ]
        if len(entity_periods) != len(set(entity_periods)):
            raise ValueError("anomaly dataset company-period observations must be unique")
        known = set(sample_ids)
        training = set(self.training_sample_ids)
        evaluation = set(self.evaluation_sample_ids)
        if len(training) != len(self.training_sample_ids):
            raise ValueError("training sample ids must be unique")
        if len(evaluation) != len(self.evaluation_sample_ids):
            raise ValueError("evaluation sample ids must be unique")
        if self.target_sample_id not in known:
            raise ValueError("target sample must exist in the frozen dataset")
        if self.target_sample_id in training:
            raise ValueError("target sample cannot be part of anomaly-model training")
        if training & evaluation:
            raise ValueError("training and evaluation samples must not overlap")
        if not training <= known or not evaluation <= known:
            raise ValueError("training and evaluation ids must reference known samples")
        expected_features = set(self.feature_names)
        by_id = {sample.sample_id: sample for sample in self.samples}
        for sample in self.samples:
            if set(sample.feature_values) != expected_features:
                raise ValueError("every sample must match the frozen feature schema")
            if sample.available_at > self.as_of:
                raise ValueError("sample cannot be available after dataset as_of")
            if sample.period_end > self.as_of.date():
                raise ValueError("sample period cannot end after dataset as_of")
        if any(by_id[sample_id].expected_anomaly is None for sample_id in evaluation):
            raise ValueError("evaluation samples require frozen expected labels")
        return self


class FinancialAnomalyModelSpec(AStockModel):
    model_id: str = Field(min_length=1)
    model_type: FinancialAnomalyModelType
    model_version: str = Field(min_length=1)
    scope: FinancialAnomalyScope
    feature_names: list[str] = Field(min_length=1)
    minimum_training_samples: int = Field(default=20, ge=8)
    contamination: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.50"))
    random_state: int = 20260717
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_model_features(self) -> FinancialAnomalyModelSpec:
        if len(self.feature_names) != len(set(self.feature_names)):
            raise ValueError("anomaly model feature names must be unique")
        return self


class FinancialAnomalyModelRegistry(AStockModel):
    registry_version: str = Field(min_length=1)
    compatible_engine_version: str = Field(min_length=1)
    models: list[FinancialAnomalyModelSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_model_ids(self) -> FinancialAnomalyModelRegistry:
        model_ids = [model.model_id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("anomaly model registry ids must be unique")
        return self


class FinancialAuditRequest(AStockModel):
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    industry_profile: FinancialIndustryProfile
    facts: list[FinancialFact] = Field(default_factory=list)
    requested_rule_ids: list[str] = Field(default_factory=list)
    series_requests: list[FinancialSeriesRequest] = Field(default_factory=list)
    peer_cohorts: list[FinancialPeerCohort] = Field(default_factory=list)
    anomaly_dataset: FinancialAnomalyDataset | None = None
    anomaly_model_specs: list[FinancialAnomalyModelSpec] = Field(default_factory=list)
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
        series_ids = [item.request_id for item in self.series_requests]
        if len(series_ids) != len(set(series_ids)):
            raise ValueError("series request ids must be unique")
        cohort_ids = [item.cohort_id for item in self.peer_cohorts]
        if len(cohort_ids) != len(set(cohort_ids)):
            raise ValueError("peer cohort ids must be unique")
        for cohort in self.peer_cohorts:
            if cohort.as_of > self.as_of:
                raise ValueError("peer cohort cannot use an as_of after the audit")
            if cohort.industry_profile is not self.industry_profile:
                raise ValueError("peer cohort industry must match the audited company")
        model_ids = [spec.model_id for spec in self.anomaly_model_specs]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("anomaly model ids must be unique")
        if (self.anomaly_dataset is None) != (not self.anomaly_model_specs):
            raise ValueError("anomaly dataset and model specs must be supplied together")
        if self.anomaly_dataset is not None:
            if self.anomaly_dataset.as_of > self.as_of:
                raise ValueError("anomaly dataset cannot use an as_of after the audit")
            if self.anomaly_dataset.industry_profile is not self.industry_profile:
                raise ValueError("anomaly dataset industry must match the audited company")
            target = next(
                sample
                for sample in self.anomaly_dataset.samples
                if sample.sample_id == self.anomaly_dataset.target_sample_id
            )
            if target.company_id != self.company_id:
                raise ValueError("anomaly target sample must belong to the audited company")
            for spec in self.anomaly_model_specs:
                if spec.feature_names != self.anomaly_dataset.feature_names:
                    raise ValueError("model features must exactly match the frozen dataset")
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
    duration_semantics: FinancialDurationSemantics = FinancialDurationSemantics.REPORTED_PERIOD
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
    input_period_ends: list[date] = Field(default_factory=list)
    component_values: dict[str, Decimal] = Field(default_factory=dict)


class FinancialDerivedMetric(AStockModel):
    derived_metric_id: str
    request_id: str
    metric_key: str
    derivation_type: FinancialDerivationType
    field_code: FinancialFieldCode
    period_end: date
    comparison_period_ends: list[date]
    value: Decimal = Field(allow_inf_nan=False)
    unit: FinancialUnit
    formula: str
    formula_version: str = "m3.2-series-v1"
    input_fact_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class FinancialPeerPercentile(AStockModel):
    percentile_id: str
    cohort_id: str
    metric_id: str
    formula_version: str
    period_end: date
    company_value: Decimal = Field(allow_inf_nan=False)
    unit: FinancialUnit
    percentile: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    sample_size: int = Field(ge=3)
    peer_company_ids: list[str] = Field(min_length=3)
    source_snapshot_ids: list[str] = Field(min_length=1)
    pit_ids: list[str] = Field(min_length=1)
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
    model_artifact_id: str
    dataset_id: str
    sample_id: str
    scope: FinancialAnomalyScope
    score: Decimal = Field(allow_inf_nan=False)
    threshold: Decimal = Field(allow_inf_nan=False)
    is_anomaly: bool
    triggered_features: list[str] = Field(default_factory=list)
    limitation_codes: list[str] = Field(default_factory=list)
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
    related_finding_ids: list[str] = Field(default_factory=list)
    related_anomaly_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_related_signal(self) -> FinancialBenignExplanation:
        if not self.related_finding_ids and not self.related_anomaly_ids:
            raise ValueError("benign explanation requires a related finding or anomaly")
        return self


class FinancialAnomalyModelArtifact(AStockModel):
    model_artifact_id: str
    model_id: str
    model_type: FinancialAnomalyModelType
    model_version: str
    scope: FinancialAnomalyScope
    dataset_id: str
    feature_names: list[str]
    training_sample_ids: list[str]
    parameters: dict[str, Any]
    library_versions: dict[str, str]
    dataset_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    serialized_model_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinancialAnomalyEvaluationReport(AStockModel):
    evaluation_id: str
    model_artifact_id: str
    dataset_id: str
    scope: FinancialAnomalyScope
    sample_count: int = Field(ge=0)
    true_positive: int = Field(ge=0)
    true_negative: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    precision: Decimal | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    recall: Decimal | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


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
    derived_metrics: list[FinancialDerivedMetric] = Field(default_factory=list)
    peer_percentiles: list[FinancialPeerPercentile] = Field(default_factory=list)
    rule_findings: list[FinancialRuleFinding]
    time_series_anomalies: list[FinancialAnomaly] = Field(default_factory=list)
    peer_anomalies: list[FinancialAnomaly] = Field(default_factory=list)
    anomaly_model_artifacts: list[FinancialAnomalyModelArtifact] = Field(default_factory=list)
    anomaly_evaluations: list[FinancialAnomalyEvaluationReport] = Field(default_factory=list)
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
