"""Institutional fundamental-research contracts for the vNext research core."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.evidence import ClaimType, EvidenceGrade, FactStatus
from astock.schemas.pit import PointInTimeStatus

_SHA256 = r"^[0-9a-f]{64}$"


class InstitutionalArtifactStatus(StrEnum):
    READY = "READY"
    NEEDS_INFO = "NEEDS_INFO"


class InstitutionalClaimType(StrEnum):
    OBSERVED_FACT = "OBSERVED_FACT"
    DERIVED_FACT = "DERIVED_FACT"
    MANAGEMENT_ASSERTION = "MANAGEMENT_ASSERTION"
    INDUSTRY_ESTIMATE = "INDUSTRY_ESTIMATE"
    INTERPRETATION = "INTERPRETATION"
    CAUSAL_CLAIM = "CAUSAL_CLAIM"
    FORECAST = "FORECAST"
    VALUATION_ASSUMPTION = "VALUATION_ASSUMPTION"


class EvidenceAuthorityTier(StrEnum):
    A_STATUTORY_PRIMARY = "A_STATUTORY_PRIMARY"
    B_OFFICIAL_ADMIN_MACRO = "B_OFFICIAL_ADMIN_MACRO"
    C_MARKET_INFRASTRUCTURE = "C_MARKET_INFRASTRUCTURE"
    D_ISSUER_INTERPRETIVE = "D_ISSUER_INTERPRETIVE"
    E_SECONDARY_PROFESSIONAL = "E_SECONDARY_PROFESSIONAL"
    F_ALTERNATIVE_COMMUNITY = "F_ALTERNATIVE_COMMUNITY"


class EvidenceDirectness(StrEnum):
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    ASSERTION = "ASSERTION"
    INTERPRETIVE = "INTERPRETIVE"


class EvidenceFreshness(StrEnum):
    CURRENT = "CURRENT"
    AGING = "AGING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class EvidenceScopeMatch(StrEnum):
    EXACT = "EXACT"
    PARTIAL = "PARTIAL"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class EvidenceExtractionConfidence(StrEnum):
    VERIFIED = "VERIFIED"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceSufficiencyState(StrEnum):
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    CONFLICTED = "CONFLICTED"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SourceEpistemicMetadata(AStockModel):
    snapshot_id: str = Field(min_length=1)
    authority_tier: EvidenceAuthorityTier
    source_independence_group: str = Field(min_length=1)
    published_at: AwareDatetime | None = None
    first_publicly_available_at: AwareDatetime | None = None
    vendor_received_at: AwareDatetime | None = None
    system_ingested_at: AwareDatetime
    parsed_at: AwareDatetime | None = None
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None
    revision_id: str | None = None
    supersedes_id: str | None = None
    restatement_flag: bool = False
    survivorship_valid_from: AwareDatetime | None = None
    survivorship_valid_to: AwareDatetime | None = None
    metadata_basis: Literal["EXPLICIT", "CONSERVATIVE_DERIVED"] = "EXPLICIT"

    @model_validator(mode="after")
    def validate_timeline(self) -> SourceEpistemicMetadata:
        ordered = [
            value
            for value in (
                self.first_publicly_available_at,
                self.vendor_received_at,
                self.system_ingested_at,
                self.parsed_at,
            )
            if value is not None
        ]
        if ordered != sorted(ordered):
            raise ValueError("epistemic source timeline must be monotonic")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("source effective_to cannot precede effective_from")
        if (
            self.survivorship_valid_from
            and self.survivorship_valid_to
            and self.survivorship_valid_to < self.survivorship_valid_from
        ):
            raise ValueError("survivorship validity interval is reversed")
        if self.supersedes_id == self.revision_id and self.revision_id is not None:
            raise ValueError("a revision cannot supersede itself")
        return self


class EvidenceQualityVector(AStockModel):
    evidence_id: str = Field(min_length=1)
    evidence_grade: EvidenceGrade
    fact_status: FactStatus
    pit_status: PointInTimeStatus | None
    authority_tier: EvidenceAuthorityTier
    directness: EvidenceDirectness
    independence_group: str = Field(min_length=1)
    freshness: EvidenceFreshness
    scope_match: EvidenceScopeMatch
    extraction_confidence: EvidenceExtractionConfidence
    snapshot_id: str = Field(min_length=1)
    metadata_basis: Literal["EXPLICIT", "CONSERVATIVE_DERIVED"]


class ClaimDependencyEdge(AStockModel):
    prerequisite_claim_id: str = Field(min_length=1)
    dependent_claim_id: str = Field(min_length=1)
    relation: Literal["REQUIRES"] = "REQUIRES"

    @model_validator(mode="after")
    def validate_edge(self) -> ClaimDependencyEdge:
        if self.prerequisite_claim_id == self.dependent_claim_id:
            raise ValueError("claim dependency cannot self-reference")
        return self


class EvidenceSufficiencyRequest(AStockModel):
    schema_version: str = "evidence-sufficiency-request-v1"
    frozen_evidence_pack_artifact_id: str = Field(min_length=1)
    material_claim_ids: list[str] = Field(min_length=1)
    claim_type_overrides: dict[str, InstitutionalClaimType] = Field(default_factory=dict)
    dependencies: list[ClaimDependencyEdge] = Field(default_factory=list)
    not_applicable_claim_ids: list[str] = Field(default_factory=list)
    source_metadata: list[SourceEpistemicMetadata] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_request(self) -> EvidenceSufficiencyRequest:
        if self.material_claim_ids != sorted(set(self.material_claim_ids)):
            raise ValueError("material claim ids must be sorted and unique")
        if self.not_applicable_claim_ids != sorted(set(self.not_applicable_claim_ids)):
            raise ValueError("not-applicable claim ids must be sorted and unique")
        if not set(self.not_applicable_claim_ids).issubset(self.material_claim_ids):
            raise ValueError("not-applicable claims must be material claims")
        if not set(self.claim_type_overrides).issubset(self.material_claim_ids):
            raise ValueError("claim type overrides must target material claims")
        metadata_ids = [item.snapshot_id for item in self.source_metadata]
        if len(metadata_ids) != len(set(metadata_ids)):
            raise ValueError("source epistemic metadata must be unique by snapshot")
        edge_ids = [
            (item.prerequisite_claim_id, item.dependent_claim_id) for item in self.dependencies
        ]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("claim dependency edges must be unique")
        dependency_claim_ids = {
            claim_id
            for edge in self.dependencies
            for claim_id in (edge.prerequisite_claim_id, edge.dependent_claim_id)
        }
        if not dependency_claim_ids.issubset(set(self.material_claim_ids)):
            raise ValueError("claim dependency edges must remain inside the material claim set")
        return self


class ClaimSufficiencyAssessment(AStockModel):
    claim_id: str = Field(min_length=1)
    legacy_claim_type: ClaimType
    institutional_claim_type: InstitutionalClaimType
    state: EvidenceSufficiencyState
    support_evidence_ids: list[str]
    refute_evidence_ids: list[str]
    context_evidence_ids: list[str]
    support_independence_groups: list[str]
    refute_independence_groups: list[str]
    unresolved_conflict_ids: list[str]
    missing_dependency_claim_ids: list[str]
    quality_vectors: list[EvidenceQualityVector]
    reason_codes: list[str]

    @model_validator(mode="after")
    def validate_assessment(self) -> ClaimSufficiencyAssessment:
        for label, values in (
            ("support evidence", self.support_evidence_ids),
            ("refute evidence", self.refute_evidence_ids),
            ("context evidence", self.context_evidence_ids),
            ("support independence", self.support_independence_groups),
            ("refute independence", self.refute_independence_groups),
            ("conflict", self.unresolved_conflict_ids),
            ("missing dependency", self.missing_dependency_claim_ids),
            ("reason code", self.reason_codes),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"claim sufficiency {label} values must be sorted and unique")
        quality_ids = [item.evidence_id for item in self.quality_vectors]
        if quality_ids != sorted(set(quality_ids)):
            raise ValueError("claim quality vectors must be sorted and unique by evidence")
        linked = sorted(
            set(self.support_evidence_ids)
            | set(self.refute_evidence_ids)
            | set(self.context_evidence_ids)
        )
        if quality_ids != linked:
            raise ValueError("quality vectors must cover every linked frozen evidence item")
        return self


class EvidenceSufficiencyReport(AStockModel):
    schema_version: str = "evidence-sufficiency-report-v1"
    report_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    frozen_evidence_pack_artifact_id: str = Field(min_length=1)
    frozen_evidence_pack_object_hash: str = Field(pattern=_SHA256)
    status: InstitutionalArtifactStatus
    material_claim_ids: list[str] = Field(min_length=1)
    assessments: list[ClaimSufficiencyAssessment] = Field(min_length=1)
    dependencies: list[ClaimDependencyEdge]
    blocking_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> EvidenceSufficiencyReport:
        if self.material_claim_ids != sorted(set(self.material_claim_ids)):
            raise ValueError("sufficiency material claims must be sorted and unique")
        assessment_ids = [item.claim_id for item in self.assessments]
        if assessment_ids != self.material_claim_ids:
            raise ValueError("sufficiency assessments must match the sorted material claim set")
        for values in (self.blocking_codes, self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("sufficiency report lists must be sorted and unique")
        blocking_states = {
            EvidenceSufficiencyState.CONFLICTED,
            EvidenceSufficiencyState.INSUFFICIENT,
        }
        has_block = any(item.state in blocking_states for item in self.assessments)
        if (self.status is InstitutionalArtifactStatus.READY) == has_block:
            raise ValueError("sufficiency status must match claim-level blocking states")
        return self


class TaxonomyStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    PROVISIONAL = "PROVISIONAL"
    UNKNOWN = "UNKNOWN"


class CompanyArchetype(StrEnum):
    STABLE_OPERATING = "STABLE_OPERATING"
    MULTI_SEGMENT = "MULTI_SEGMENT"
    CYCLICAL = "CYCLICAL"
    FINANCIAL_INSTITUTION = "FINANCIAL_INSTITUTION"
    ASSET_HEAVY_SPECIAL = "ASSET_HEAVY_SPECIAL"
    HIGH_GROWTH = "HIGH_GROWTH"
    PRE_PROFIT_OPTION_LIKE = "PRE_PROFIT_OPTION_LIKE"


class EvidenceBoundStatement(AStockModel):
    statement: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lineage(self) -> EvidenceBoundStatement:
        if not self.claim_ids and not self.evidence_ids:
            raise ValueError("evidence-bound statement requires claim or evidence lineage")
        if self.claim_ids != sorted(set(self.claim_ids)):
            raise ValueError("statement claim ids must be sorted and unique")
        if self.evidence_ids != sorted(set(self.evidence_ids)):
            raise ValueError("statement evidence ids must be sorted and unique")
        return self


class EvidenceBoundMetric(AStockModel):
    metric: str = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1)
    period_end: date | None = None
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lineage(self) -> EvidenceBoundMetric:
        if not self.claim_ids and not self.evidence_ids:
            raise ValueError("evidence-bound metric requires claim or evidence lineage")
        if self.claim_ids != sorted(set(self.claim_ids)):
            raise ValueError("metric claim ids must be sorted and unique")
        if self.evidence_ids != sorted(set(self.evidence_ids)):
            raise ValueError("metric evidence ids must be sorted and unique")
        return self


class IndustryProfileDraft(AStockModel):
    industry_id: str = Field(min_length=1)
    industry_name: str = Field(min_length=1)
    taxonomy_status: TaxonomyStatus
    taxonomy_artifact_id: str | None = None
    definition: EvidenceBoundStatement | None = None
    market_size_growth: EvidenceBoundStatement | None = None
    industry_profitability: EvidenceBoundStatement | None = None
    market_share_structure: EvidenceBoundStatement | None = None
    supply_capacity_demand: EvidenceBoundStatement | None = None
    pricing_mechanism: EvidenceBoundStatement | None = None
    competitive_dynamics: EvidenceBoundStatement | None = None
    regulation_external_drivers: EvidenceBoundStatement | None = None
    cycle_technology_roadmap: EvidenceBoundStatement | None = None
    metrics: list[EvidenceBoundMetric] = Field(default_factory=list)


class IndustryProfile(AStockModel):
    schema_version: str = "industry-profile-v1"
    profile_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    status: InstitutionalArtifactStatus
    draft: IndustryProfileDraft
    missing_codes: list[str]
    warning_codes: list[str]
    claim_ids: list[str]
    evidence_ids: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]

    @model_validator(mode="after")
    def validate_profile(self) -> IndustryProfile:
        for values in (
            self.missing_codes,
            self.warning_codes,
            self.claim_ids,
            self.evidence_ids,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("industry profile lists must be sorted and unique")
        if self.status is InstitutionalArtifactStatus.READY and self.missing_codes:
            raise ValueError("READY industry profile cannot carry missing codes")
        if self.status is InstitutionalArtifactStatus.NEEDS_INFO and not self.missing_codes:
            raise ValueError("NEEDS_INFO industry profile requires missing codes")
        return self


class CompanySegmentEconomics(AStockModel):
    segment_id: str = Field(min_length=1)
    segment_name: str = Field(min_length=1)
    business_model: EvidenceBoundStatement
    revenue_driver: EvidenceBoundStatement
    pricing_driver: EvidenceBoundStatement
    margin_driver: EvidenceBoundStatement
    capital_intensity: EvidenceBoundStatement | None = None
    metrics: list[EvidenceBoundMetric] = Field(default_factory=list)


class CompanyEconomicsDraft(AStockModel):
    archetype: CompanyArchetype
    archetype_basis: EvidenceBoundStatement
    segments: list[CompanySegmentEconomics] = Field(default_factory=list)
    pricing_power: EvidenceBoundStatement | None = None
    customer_concentration: EvidenceBoundStatement | None = None
    supplier_dependency: EvidenceBoundStatement | None = None
    competitive_position: EvidenceBoundStatement | None = None
    management_governance: EvidenceBoundStatement | None = None
    capital_allocation: EvidenceBoundStatement | None = None
    reinvestment_roic: EvidenceBoundStatement | None = None
    funding_dilution: EvidenceBoundStatement | None = None
    industry_specific_economics: list[EvidenceBoundStatement] = Field(default_factory=list)


class CompanyEconomicsProfile(AStockModel):
    schema_version: str = "company-economics-profile-v1"
    profile_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    status: InstitutionalArtifactStatus
    draft: CompanyEconomicsDraft
    missing_codes: list[str]
    warning_codes: list[str]
    claim_ids: list[str]
    evidence_ids: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]

    @model_validator(mode="after")
    def validate_profile(self) -> CompanyEconomicsProfile:
        for values in (
            self.missing_codes,
            self.warning_codes,
            self.claim_ids,
            self.evidence_ids,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("company economics profile lists must be sorted and unique")
        if self.status is InstitutionalArtifactStatus.READY and self.missing_codes:
            raise ValueError("READY company economics profile cannot carry missing codes")
        if self.status is InstitutionalArtifactStatus.NEEDS_INFO and not self.missing_codes:
            raise ValueError("NEEDS_INFO company economics profile requires missing codes")
        return self


class DriverOperation(StrEnum):
    INPUT = "INPUT"
    ADD = "ADD"
    SUBTRACT = "SUBTRACT"
    MULTIPLY = "MULTIPLY"
    DIVIDE = "DIVIDE"


class ForecastScenario(StrEnum):
    BULL = "BULL"
    BASE = "BASE"
    BEAR = "BEAR"


class ForecastTemplate(StrEnum):
    OPERATING_FCFF = "OPERATING_FCFF"
    FINANCIAL_RESIDUAL_INCOME = "FINANCIAL_RESIDUAL_INCOME"
    ASSET_NAV = "ASSET_NAV"
    PRE_PROFIT_SCENARIO = "PRE_PROFIT_SCENARIO"


class DriverAssumptionProvenance(StrEnum):
    EVIDENCE = "EVIDENCE"
    BASE_RATE = "BASE_RATE"
    MANUAL = "MANUAL"


class DriverNode(AStockModel):
    node_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    operation: DriverOperation
    input_node_ids: list[str] = Field(default_factory=list)
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_node(self) -> DriverNode:
        if len(self.input_node_ids) != len(set(self.input_node_ids)):
            raise ValueError("driver input node ids must be unique")
        required_arity = {
            DriverOperation.INPUT: 0,
            DriverOperation.ADD: 2,
            DriverOperation.SUBTRACT: 2,
            DriverOperation.MULTIPLY: 2,
            DriverOperation.DIVIDE: 2,
        }[self.operation]
        if len(self.input_node_ids) != required_arity:
            raise ValueError("driver operation has invalid arity")
        if self.node_id in self.input_node_ids:
            raise ValueError("driver node cannot reference itself")
        return self


class DriverHistoricalPoint(AStockModel):
    node_id: str = Field(min_length=1)
    period_end: date
    value: Decimal
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class DriverTreeDraft(AStockModel):
    forecast_template: ForecastTemplate
    nodes: list[DriverNode] = Field(min_length=1)
    output_bindings: dict[str, str] = Field(min_length=1)
    historical_points: list[DriverHistoricalPoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_tree_shape(self) -> DriverTreeDraft:
        ids = [item.node_id for item in self.nodes]
        if ids != sorted(set(ids)):
            raise ValueError("driver nodes must be sorted and unique by node id")
        node_ids = set(ids)
        if not set(self.output_bindings.values()).issubset(node_ids):
            raise ValueError("driver output bindings must reference existing nodes")
        if any(not set(item.input_node_ids).issubset(node_ids) for item in self.nodes):
            raise ValueError("driver inputs must reference existing nodes")
        required_by_template = {
            ForecastTemplate.OPERATING_FCFF: {
                "REVENUE",
                "OPERATING_MARGIN",
                "TAX_RATE",
                "D_AND_A",
                "CAPEX",
                "CHANGE_WORKING_CAPITAL",
            },
            ForecastTemplate.FINANCIAL_RESIDUAL_INCOME: {
                "BOOK_VALUE",
                "NET_INCOME",
                "ROE",
            },
            ForecastTemplate.ASSET_NAV: {
                "ASSET_VALUE",
                "NET_DEBT",
            },
            ForecastTemplate.PRE_PROFIT_SCENARIO: {
                "REVENUE",
                "GROSS_MARGIN",
                "CASH_BURN",
                "CASH_BALANCE",
                "SHARES_OUTSTANDING",
            },
        }
        if not required_by_template[self.forecast_template].issubset(self.output_bindings):
            raise ValueError("driver tree lacks required output bindings for its forecast template")
        return self


class DriverTree(AStockModel):
    schema_version: str = "driver-tree-v1"
    tree_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    draft: DriverTreeDraft
    evaluation_order: list[str] = Field(min_length=1)
    claim_ids: list[str]
    evidence_ids: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]

    @model_validator(mode="after")
    def validate_tree(self) -> DriverTree:
        node_ids = [item.node_id for item in self.draft.nodes]
        if set(self.evaluation_order) != set(node_ids) or len(self.evaluation_order) != len(
            node_ids
        ):
            raise ValueError("driver evaluation order must contain every node exactly once")
        for values in (
            self.claim_ids,
            self.evidence_ids,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("driver tree lists must be sorted and unique")
        return self


class DriverInputValue(AStockModel):
    node_id: str = Field(min_length=1)
    period_end: date
    value: Decimal
    provenance: DriverAssumptionProvenance
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    provenance_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> DriverInputValue:
        if self.claim_ids != sorted(set(self.claim_ids)):
            raise ValueError("driver assumption claim ids must be sorted and unique")
        if self.evidence_ids != sorted(set(self.evidence_ids)):
            raise ValueError("driver assumption evidence ids must be sorted and unique")
        if self.provenance is DriverAssumptionProvenance.EVIDENCE and not (
            self.claim_ids or self.evidence_ids
        ):
            raise ValueError("evidence-based driver assumption requires evidence lineage")
        return self


class ForecastScenarioInput(AStockModel):
    scenario: ForecastScenario
    input_values: list[DriverInputValue] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inputs(self) -> ForecastScenarioInput:
        keys = [(item.node_id, item.period_end) for item in self.input_values]
        if keys != sorted(set(keys)):
            raise ValueError("forecast scenario driver inputs must be sorted and unique")
        return self


class ForecastPeriod(AStockModel):
    period_end: date
    template: ForecastTemplate
    metrics: dict[str, Decimal] = Field(min_length=1)
    revenue: Decimal | None = None
    operating_margin: Decimal | None = None
    ebit: Decimal | None = None
    tax_rate: Decimal | None = None
    nopat: Decimal | None = None
    d_and_a: Decimal | None = None
    capex: Decimal | None = None
    change_working_capital: Decimal | None = None
    fcff: Decimal | None = None
    evaluated_nodes: dict[str, Decimal]

    @model_validator(mode="after")
    def validate_template(self) -> ForecastPeriod:
        bridge = (
            self.revenue,
            self.operating_margin,
            self.ebit,
            self.tax_rate,
            self.nopat,
            self.d_and_a,
            self.capex,
            self.change_working_capital,
            self.fcff,
        )
        if self.template is ForecastTemplate.OPERATING_FCFF and any(
            value is None for value in bridge
        ):
            raise ValueError("OPERATING_FCFF forecast period requires the complete FCFF bridge")
        if self.template is not ForecastTemplate.OPERATING_FCFF and any(
            value is not None for value in bridge
        ):
            raise ValueError("non-FCFF forecast period cannot carry an FCFF bridge")
        return self


class ForecastScenarioPack(AStockModel):
    scenario: ForecastScenario
    periods: list[ForecastPeriod] = Field(min_length=1)
    assumption_claim_ids: list[str]
    assumption_evidence_ids: list[str]
    assumption_notes: list[str]


class ForecastBuildRequest(AStockModel):
    schema_version: str = "forecast-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    driver_tree_artifact_id: str = Field(min_length=1)
    evidence_sufficiency_artifact_id: str = Field(min_length=1)
    scenarios: list[ForecastScenarioInput] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_scenarios(self) -> ForecastBuildRequest:
        if {item.scenario for item in self.scenarios} != set(ForecastScenario):
            raise ValueError("forecast request requires Bull, Base and Bear exactly once")
        return self


class ForecastPack(AStockModel):
    schema_version: str = "forecast-pack-v1"
    forecast_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    driver_tree_artifact_id: str = Field(min_length=1)
    driver_tree_object_hash: str = Field(pattern=_SHA256)
    forecast_template: ForecastTemplate
    status: InstitutionalArtifactStatus
    scenarios: list[ForecastScenarioPack] = Field(min_length=3, max_length=3)
    blocking_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]

    @model_validator(mode="after")
    def validate_pack(self) -> ForecastPack:
        if {item.scenario for item in self.scenarios} != set(ForecastScenario):
            raise ValueError("forecast pack requires Bull, Base and Bear exactly once")
        for values in (self.blocking_codes, self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("forecast pack lists must be sorted and unique")
        if self.status is InstitutionalArtifactStatus.READY and self.blocking_codes:
            raise ValueError("READY forecast cannot carry blocking codes")
        return self


class ValuationMethod(StrEnum):
    DCF_FCFF = "DCF_FCFF"
    SOTP = "SOTP"
    MID_CYCLE_NORMALIZED = "MID_CYCLE_NORMALIZED"
    RESIDUAL_INCOME = "RESIDUAL_INCOME"
    NAV_REPLACEMENT = "NAV_REPLACEMENT"
    SCENARIO_VALUE = "SCENARIO_VALUE"


class ValuationScenarioAssumption(AStockModel):
    scenario: ForecastScenario
    method: ValuationMethod
    discount_rate: Decimal | None = None
    terminal_growth: Decimal | None = None
    net_debt: Decimal = Decimal("0")
    shares_outstanding: Decimal = Field(gt=0)
    normalized_metric: Decimal | None = None
    valuation_multiple: Decimal | None = None
    explicit_equity_value: Decimal | None = None
    assumption_claim_ids: list[str] = Field(default_factory=list)
    assumption_evidence_ids: list[str] = Field(default_factory=list)
    assumption_note: str = Field(min_length=1)


class ValuationScenarioResult(AStockModel):
    scenario: ForecastScenario
    method: ValuationMethod
    enterprise_value: Decimal | None = None
    equity_value: Decimal
    per_share_value: Decimal
    expected_return: Decimal | None = None


class ValuationSensitivityPoint(AStockModel):
    discount_rate: Decimal
    terminal_growth: Decimal
    per_share_value: Decimal


class MarketImpliedExpectation(AStockModel):
    expectation: Literal["IMPLIED_TERMINAL_GROWTH", "IMPLIED_FCFF_SCALE"]
    scenario: ForecastScenario
    implied_value: Decimal | None = None
    status: InstitutionalArtifactStatus
    reason_codes: list[str]


class MarketPriceAnchor(AStockModel):
    price: Decimal = Field(gt=0)
    observed_at: AwareDatetime
    available_to_system_at: AwareDatetime
    source_artifact_id: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_anchor(self) -> MarketPriceAnchor:
        if self.available_to_system_at < self.observed_at:
            raise ValueError("market price anchor cannot be available before observation")
        return self


class ValuationBuildRequest(AStockModel):
    schema_version: str = "valuation-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    archetype: CompanyArchetype
    forecast_pack_artifact_id: str = Field(min_length=1)
    evidence_sufficiency_artifact_id: str = Field(min_length=1)
    company_economics_artifact_id: str = Field(min_length=1)
    market_price_anchor: MarketPriceAnchor | None = None
    scenario_assumptions: list[ValuationScenarioAssumption] = Field(min_length=3, max_length=3)
    invalidation_conditions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenarios(self) -> ValuationBuildRequest:
        if {item.scenario for item in self.scenario_assumptions} != set(ForecastScenario):
            raise ValueError("valuation request requires Bull, Base and Bear exactly once")
        if self.invalidation_conditions != sorted(set(self.invalidation_conditions)):
            raise ValueError("valuation invalidation conditions must be sorted and unique")
        if self.market_price_anchor is not None and (
            self.market_price_anchor.observed_at > self.as_of
            or self.market_price_anchor.available_to_system_at > self.as_of
        ):
            raise ValueError("market price anchor cannot be future relative to valuation as_of")
        return self


class ValuationPack(AStockModel):
    schema_version: str = "valuation-pack-v1"
    valuation_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    archetype: CompanyArchetype
    forecast_pack_artifact_id: str = Field(min_length=1)
    forecast_pack_object_hash: str = Field(pattern=_SHA256)
    status: InstitutionalArtifactStatus
    results: list[ValuationScenarioResult] = Field(min_length=3, max_length=3)
    value_range_low: Decimal
    value_range_high: Decimal
    market_implied_expectations: list[MarketImpliedExpectation]
    sensitivity_table: list[ValuationSensitivityPoint]
    market_price_anchor: MarketPriceAnchor | None = None
    assumption_claim_ids: list[str]
    assumption_evidence_ids: list[str]
    invalidation_conditions: list[str]
    blocking_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    scenario_prices_are_targets: Literal[False] = False

    @model_validator(mode="after")
    def validate_pack(self) -> ValuationPack:
        if {item.scenario for item in self.results} != set(ForecastScenario):
            raise ValueError("valuation pack requires Bull, Base and Bear exactly once")
        if self.value_range_high < self.value_range_low:
            raise ValueError("valuation range is reversed")
        for values in (
            self.assumption_claim_ids,
            self.assumption_evidence_ids,
            self.invalidation_conditions,
            self.blocking_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("valuation pack lists must be sorted and unique")
        if self.status is InstitutionalArtifactStatus.READY and self.blocking_codes:
            raise ValueError("READY valuation cannot carry blocking codes")
        return self


class IndustryProfileBuildRequest(AStockModel):
    schema_version: str = "industry-profile-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    evidence_sufficiency_artifact_id: str = Field(min_length=1)
    draft: IndustryProfileDraft


class CompanyEconomicsBuildRequest(AStockModel):
    schema_version: str = "company-economics-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    evidence_sufficiency_artifact_id: str = Field(min_length=1)
    draft: CompanyEconomicsDraft


class DriverTreeBuildRequest(AStockModel):
    schema_version: str = "driver-tree-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    industry_profile_artifact_id: str = Field(min_length=1)
    company_economics_artifact_id: str = Field(min_length=1)
    draft: DriverTreeDraft


class FundamentalModelBundleBuildRequest(AStockModel):
    schema_version: str = "fundamental-model-bundle-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    evidence_sufficiency_artifact_id: str = Field(min_length=1)
    industry_profile_artifact_id: str = Field(min_length=1)
    company_economics_artifact_id: str = Field(min_length=1)
    driver_tree_artifact_id: str = Field(min_length=1)
    forecast_pack_artifact_id: str = Field(min_length=1)
    valuation_pack_artifact_id: str = Field(min_length=1)


class InstitutionalResearchFinalizeRequest(AStockModel):
    schema_version: str = "institutional-research-finalize-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    evidence_sufficiency: EvidenceSufficiencyRequest
    industry_profile: IndustryProfileDraft
    company_economics: CompanyEconomicsDraft
    driver_tree: DriverTreeDraft
    forecast_scenarios: list[ForecastScenarioInput] = Field(min_length=3, max_length=3)
    valuation_archetype: CompanyArchetype
    market_price_anchor: MarketPriceAnchor | None = None
    valuation_scenarios: list[ValuationScenarioAssumption] = Field(min_length=3, max_length=3)
    valuation_invalidation_conditions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_finalize(self) -> InstitutionalResearchFinalizeRequest:
        if {item.scenario for item in self.forecast_scenarios} != set(ForecastScenario):
            raise ValueError("institutional finalize requires all forecast scenarios")
        if {item.scenario for item in self.valuation_scenarios} != set(ForecastScenario):
            raise ValueError("institutional finalize requires all valuation scenarios")
        if self.valuation_invalidation_conditions != sorted(
            set(self.valuation_invalidation_conditions)
        ):
            raise ValueError("finalize valuation invalidations must be sorted and unique")
        return self


class InstitutionalDecisionContextDraft(AStockModel):
    decision_question: str = Field(min_length=1)
    decision_horizon_end: date
    investment_thesis: EvidenceBoundStatement
    variant_perception: EvidenceBoundStatement
    key_driver_ids: list[str] = Field(min_length=3, max_length=5)
    competing_hypotheses: list[EvidenceBoundStatement] = Field(min_length=1)
    portfolio_context: str | None = None

    @model_validator(mode="after")
    def validate_context(self) -> InstitutionalDecisionContextDraft:
        if self.key_driver_ids != sorted(set(self.key_driver_ids)):
            raise ValueError("institutional decision key drivers must be sorted and unique")
        return self


class InstitutionalDecisionContextBuildRequest(AStockModel):
    schema_version: str = "institutional-decision-context-build-request-v1"
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    fundamental_model_bundle_artifact_id: str = Field(min_length=1)
    draft: InstitutionalDecisionContextDraft

    @model_validator(mode="after")
    def validate_horizon(self) -> InstitutionalDecisionContextBuildRequest:
        if self.draft.decision_horizon_end <= self.as_of.date():
            raise ValueError("institutional decision horizon must be after as_of")
        return self


class InstitutionalDecisionContext(AStockModel):
    schema_version: str = "institutional-decision-context-v1"
    context_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    fundamental_model_bundle_artifact_id: str = Field(min_length=1)
    fundamental_model_bundle_object_hash: str = Field(pattern=_SHA256)
    draft: InstitutionalDecisionContextDraft
    claim_ids: list[str]
    evidence_ids: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_context(self) -> InstitutionalDecisionContext:
        for values in (
            self.claim_ids,
            self.evidence_ids,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("institutional decision context lists must be sorted and unique")
        if self.fundamental_model_bundle_artifact_id not in self.source_artifact_ids:
            raise ValueError("institutional decision context must bind its model bundle artifact")
        if self.fundamental_model_bundle_object_hash not in self.source_object_hashes:
            raise ValueError("institutional decision context must bind its model bundle hash")
        return self


class FundamentalModelBundle(AStockModel):
    schema_version: str = "fundamental-model-bundle-v1"
    bundle_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    status: InstitutionalArtifactStatus
    evidence_sufficiency_artifact_id: str = Field(min_length=1)
    industry_profile_artifact_id: str = Field(min_length=1)
    company_economics_artifact_id: str = Field(min_length=1)
    driver_tree_artifact_id: str = Field(min_length=1)
    forecast_pack_artifact_id: str = Field(min_length=1)
    valuation_pack_artifact_id: str = Field(min_length=1)
    artifact_object_hashes: dict[str, str] = Field(min_length=6)
    blocking_codes: list[str]
    warning_codes: list[str]
    evidence_ids: list[str]
    claim_ids: list[str]
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_bundle(self) -> FundamentalModelBundle:
        required = {
            self.evidence_sufficiency_artifact_id,
            self.industry_profile_artifact_id,
            self.company_economics_artifact_id,
            self.driver_tree_artifact_id,
            self.forecast_pack_artifact_id,
            self.valuation_pack_artifact_id,
        }
        if set(self.artifact_object_hashes) != required:
            raise ValueError("fundamental bundle hashes must cover every component artifact")
        if any(not value or len(value) != 64 for value in self.artifact_object_hashes.values()):
            raise ValueError("fundamental bundle component hashes must be SHA-256 values")
        for values in (
            self.blocking_codes,
            self.warning_codes,
            self.evidence_ids,
            self.claim_ids,
        ):
            if values != sorted(set(values)):
                raise ValueError("fundamental bundle lists must be sorted and unique")
        if self.status is InstitutionalArtifactStatus.READY and self.blocking_codes:
            raise ValueError("READY fundamental model bundle cannot carry blocking codes")
        return self


__all__ = [
    "ClaimDependencyEdge",
    "ClaimSufficiencyAssessment",
    "CompanyArchetype",
    "CompanyEconomicsBuildRequest",
    "CompanyEconomicsDraft",
    "CompanyEconomicsProfile",
    "CompanySegmentEconomics",
    "DriverAssumptionProvenance",
    "DriverHistoricalPoint",
    "DriverInputValue",
    "DriverNode",
    "DriverOperation",
    "DriverTree",
    "DriverTreeBuildRequest",
    "DriverTreeDraft",
    "EvidenceAuthorityTier",
    "EvidenceBoundMetric",
    "EvidenceBoundStatement",
    "EvidenceDirectness",
    "EvidenceExtractionConfidence",
    "EvidenceFreshness",
    "EvidenceQualityVector",
    "EvidenceScopeMatch",
    "EvidenceSufficiencyReport",
    "EvidenceSufficiencyRequest",
    "EvidenceSufficiencyState",
    "ForecastBuildRequest",
    "ForecastPack",
    "ForecastPeriod",
    "ForecastScenario",
    "ForecastScenarioInput",
    "ForecastScenarioPack",
    "FundamentalModelBundle",
    "FundamentalModelBundleBuildRequest",
    "IndustryProfile",
    "IndustryProfileBuildRequest",
    "IndustryProfileDraft",
    "InstitutionalArtifactStatus",
    "InstitutionalClaimType",
    "InstitutionalDecisionContext",
    "InstitutionalDecisionContextBuildRequest",
    "InstitutionalDecisionContextDraft",
    "InstitutionalResearchFinalizeRequest",
    "MarketImpliedExpectation",
    "MarketPriceAnchor",
    "SourceEpistemicMetadata",
    "TaxonomyStatus",
    "ValuationBuildRequest",
    "ValuationMethod",
    "ValuationPack",
    "ValuationScenarioAssumption",
    "ValuationScenarioResult",
    "ValuationSensitivityPoint",
]
