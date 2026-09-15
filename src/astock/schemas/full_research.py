"""Canonical contracts for the fail-closed full-research recommendation gate.

These models are projections over existing canonical research/evidence artifacts. They do
not create a second market, financial, evidence, portfolio, paper or account truth source.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.entry_quality import EntryQualityState

_SHA256 = r"^[0-9a-f]{64}$"


class FullResearchNodeStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    DEGRADED = "DEGRADED"


class FullResearchComponentType(StrEnum):
    MACRO = "MACRO"
    INDUSTRY = "INDUSTRY"
    FUNDAMENTAL = "FUNDAMENTAL"
    FINANCIAL_QUALITY = "FINANCIAL_QUALITY"
    GOVERNANCE = "GOVERNANCE"
    EVENT_RESEARCH = "EVENT_RESEARCH"
    QUANT_FACTOR = "QUANT_FACTOR"
    CHALLENGER = "CHALLENGER"


class FullResearchNode(StrEnum):
    REQUEST_CONTRACT = "REQUEST_CONTRACT"
    POINT_IN_TIME_SNAPSHOT = "POINT_IN_TIME_SNAPSHOT"
    MACRO = "MACRO"
    MARKET_REGIME = "MARKET_REGIME"
    INDUSTRY = "INDUSTRY"
    UNIVERSE_SCREENING = "UNIVERSE_SCREENING"
    COMPANY_FUNDAMENTAL = "COMPANY_FUNDAMENTAL"
    FINANCIAL_QUALITY_AUDIT = "FINANCIAL_QUALITY_AUDIT"
    VALUATION = "VALUATION"
    NEWS_EVENT = "NEWS_EVENT"
    GOVERNANCE = "GOVERNANCE"
    QUANT_FACTOR = "QUANT_FACTOR"
    BEAR_CASE_CHALLENGER = "BEAR_CASE_CHALLENGER"
    CANDIDATE_RANKING = "CANDIDATE_RANKING"
    PORTFOLIO_CONSTRUCTION = "PORTFOLIO_CONSTRUCTION"
    EXECUTION_PLANNING = "EXECUTION_PLANNING"
    RISK_AUDIT = "RISK_AUDIT"
    EVIDENCE_AUDIT = "EVIDENCE_AUDIT"
    PUBLICATION_GATE = "PUBLICATION_GATE"
    TRACKING_REEVALUATION = "TRACKING_REEVALUATION"


class SourceFamily(StrEnum):
    MARKET_PRICE = "MARKET_PRICE"
    FINANCIAL_REPORT = "FINANCIAL_REPORT"
    OFFICIAL_FILING = "OFFICIAL_FILING"
    MACRO_RELEASE = "MACRO_RELEASE"
    NEWS_EVENT = "NEWS_EVENT"
    INDUSTRY = "INDUSTRY"
    GOVERNANCE = "GOVERNANCE"
    QUANT = "QUANT"
    OTHER = "OTHER"


class SourceAuthority(StrEnum):
    PRIMARY_OFFICIAL = "PRIMARY_OFFICIAL"
    ISSUER_OFFICIAL = "ISSUER_OFFICIAL"
    SECONDARY_STRUCTURED = "SECONDARY_STRUCTURED"
    CONFIRMED_MEDIA = "CONFIRMED_MEDIA"
    ANALYST_INTERPRETATION = "ANALYST_INTERPRETATION"
    MARKET_RUMOR = "MARKET_RUMOR"


class EventEvidenceClass(StrEnum):
    OFFICIAL_FILING = "OFFICIAL_FILING"
    SECONDARY_STRUCTURED = "SECONDARY_STRUCTURED"
    CONFIRMED_NEWS = "CONFIRMED_NEWS"
    ANALYST_INTERPRETATION = "ANALYST_INTERPRETATION"
    MARKET_RUMOR = "MARKET_RUMOR"


class PortfolioAssumptionSource(StrEnum):
    USER = "USER"
    MODEL_PORTFOLIO = "MODEL_PORTFOLIO"


class InvestmentExpectationValueSource(StrEnum):
    CURRENT_USER = "CURRENT_USER"
    RECENT_HISTORY = "RECENT_HISTORY"
    DEFAULT = "DEFAULT"


class ModelPortfolioAssumptions(AStockModel):
    schema_version: str = "model-portfolio-assumptions-v1"
    source: PortfolioAssumptionSource
    capital_rmb: Decimal = Field(gt=0)
    target_annual_return: Decimal | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    capital_source: InvestmentExpectationValueSource | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    target_annual_return_source: InvestmentExpectationValueSource | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    expectation_history_references: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    expectation_notes: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    risk_profile: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    horizon_min_months: int = Field(ge=1)
    horizon_max_months: int = Field(ge=1)
    long_only: bool = True
    margin_allowed: bool = False
    short_allowed: bool = False
    cash_allowed: bool = True
    target_position_min: int = Field(ge=0)
    target_position_max: int = Field(ge=0)
    user_constraints: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_assumptions(self) -> ModelPortfolioAssumptions:
        if self.horizon_max_months < self.horizon_min_months:
            raise ValueError("portfolio horizon range is reversed")
        if self.target_position_max < self.target_position_min:
            raise ValueError("portfolio target position range is reversed")
        if not self.long_only or self.margin_allowed or self.short_allowed:
            raise ValueError(
                "v1 recommendation portfolio must be long-only without margin or shorting"
            )
        return self


class FullResearchRequestContract(AStockModel):
    schema_version: str = "full-research-request-contract-v1"
    request_id: str = Field(min_length=1)
    as_of_timestamp: AwareDatetime
    raw_text_hash: str = Field(pattern=_SHA256)
    account_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    intent: Literal["FULL_RESEARCH_RECOMMENDATION"] = "FULL_RESEARCH_RECOMMENDATION"
    portfolio_assumptions: ModelPortfolioAssumptions
    requested_instruments: tuple[str, ...] = ()
    requested_count: int | None = Field(default=None, ge=1)
    current_recommendation: bool = True
    decision_context: Literal["NEW_ALLOCATION", "EXISTING_HOLDING"] = "NEW_ALLOCATION"
    assumptions_explicitly_disclosed: bool = True
    broker_execution_allowed: Literal[False] = False


class SourceLineageEntry(AStockModel):
    schema_version: str = "recommendation-source-lineage-v1"
    source_id: str = Field(min_length=1)
    family: SourceFamily
    authority: SourceAuthority
    provider: str = Field(min_length=1)
    source_identity: str = Field(min_length=1)
    source_url: str | None = None
    artifact_id: str | None = None
    evidence_id: str | None = None
    object_hash: str = Field(pattern=_SHA256)
    observed_at: AwareDatetime | None = None
    release_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    captured_at: AwareDatetime
    available_to_system_at: AwareDatetime
    ingestion_version: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage_clock(self) -> SourceLineageEntry:
        source_times = tuple(
            value
            for value in (self.observed_at, self.release_at, self.published_at)
            if value is not None
        )
        if not source_times:
            raise ValueError("source lineage needs an observed/release/published timestamp")
        if self.available_to_system_at < max(source_times):
            raise ValueError("source cannot be available before its source timestamp")
        if self.available_to_system_at < self.captured_at:
            raise ValueError("source cannot be available before capture")
        return self


class EvidenceConflict(AStockModel):
    schema_version: str = "recommendation-evidence-conflict-v1"
    conflict_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=2)
    status: Literal["OPEN", "RESOLVED"] = "OPEN"
    resolution: str | None = None
    primary_source_id: str | None = None

    @model_validator(mode="after")
    def validate_conflict(self) -> EvidenceConflict:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("conflict source identities must be unique")
        if self.status == "RESOLVED" and (not self.resolution or not self.primary_source_id):
            raise ValueError("resolved conflict requires rationale and selected primary source")
        if self.primary_source_id is not None and self.primary_source_id not in self.source_ids:
            raise ValueError("conflict primary source must belong to the conflict")
        return self


class PointInTimeSnapshot(AStockModel):
    schema_version: str = "recommendation-pit-snapshot-v1"
    snapshot_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    as_of_timestamp: AwareDatetime
    sources: tuple[SourceLineageEntry, ...]
    conflicts: tuple[EvidenceConflict, ...] = ()
    critical_source_families: tuple[SourceFamily, ...]
    lineage_coverage: Decimal = Field(ge=0, le=1)
    point_in_time_leakage_count: int = Field(ge=0, default=0)
    status: FullResearchNodeStatus

    @model_validator(mode="after")
    def validate_point_in_time(self) -> PointInTimeSnapshot:
        ids = [item.source_id for item in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("PIT source identities must be unique")
        if any(item.available_to_system_at > self.as_of_timestamp for item in self.sources):
            raise ValueError("PIT snapshot includes future-visible source data")
        available_families = {item.family for item in self.sources}
        complete = set(self.critical_source_families) <= available_families
        open_conflict = any(item.status == "OPEN" for item in self.conflicts)
        if self.status == FullResearchNodeStatus.PASS and (
            self.point_in_time_leakage_count != 0
            or not complete
            or open_conflict
            or self.lineage_coverage != Decimal("1")
        ):
            raise ValueError(
                "PASS PIT snapshot requires complete, conflict-free, non-leaking lineage"
            )
        return self


class MacroResearchOutcome(AStockModel):
    schema_version: str = "macro-research-outcome-v1"
    macro_regime: str = Field(min_length=1)
    liquidity_regime: str = Field(min_length=1)
    risk_appetite: str = Field(min_length=1)
    policy_bias: str = Field(min_length=1)
    dimensions: dict[str, str]
    macro_sector_implications: tuple[str, ...]
    macro_risk_events: tuple[str, ...]
    source_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_macro_dimensions(self) -> MacroResearchOutcome:
        required = {
            "domestic_growth",
            "inflation",
            "credit",
            "liquidity",
            "interest_rates",
            "foreign_exchange",
            "fiscal_policy",
            "industrial_policy",
            "real_estate",
            "exports",
            "global_risk_assets",
        }
        if not required <= set(self.dimensions):
            raise ValueError("macro research is missing mandatory regime dimensions")
        if any(not str(self.dimensions[key]).strip() for key in required):
            raise ValueError("macro research dimensions cannot be blank")
        if not self.source_ids and not self.evidence_ids:
            raise ValueError("macro research requires source or evidence lineage")
        return self


class IndustryResearchOutcome(AStockModel):
    schema_version: str = "industry-research-outcome-v1"
    industry_id: str = Field(min_length=1)
    industry_score: Decimal
    cycle_phase: str = Field(min_length=1)
    competitive_intensity: str = Field(min_length=1)
    dimensions: dict[str, str | Decimal]
    industry_catalysts: tuple[str, ...]
    industry_risks: tuple[str, ...]
    peer_set: tuple[str, ...]
    peer_set_exception_reason: str | None = None
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_peer_set(self) -> IndustryResearchOutcome:
        required_dimensions = {
            "demand",
            "supply_capacity",
            "inventory",
            "pricing",
            "capital_expenditure",
            "competition",
            "policy",
            "technology",
            "cycle_position",
            "value_chain_bargaining_power",
            "valuation_percentile",
        }
        if not required_dimensions <= set(self.dimensions):
            raise ValueError("industry research is missing mandatory operating dimensions")
        if len(set(self.peer_set)) != len(self.peer_set):
            raise ValueError("industry peer set must be unique")
        if len(self.peer_set) < 5 and not self.peer_set_exception_reason:
            raise ValueError(
                "industry research needs five peers or an explicit structural exception"
            )
        return self

    def recommendation_peer_coverage_complete(self) -> bool:
        """Require real peers or a typed industry-structure exception for formal use."""

        return len(self.peer_set) >= 5 or bool(
            self.peer_set_exception_reason
            and self.peer_set_exception_reason.startswith("STRUCTURAL_NOT_APPLICABLE:")
        )


class CompanyFundamentalSnapshot(AStockModel):
    schema_version: str = "company-fundamental-snapshot-v1"
    instrument_id: str = Field(min_length=1)
    available_complete_years: int = Field(ge=0)
    analyzed_complete_years: int = Field(ge=0)
    available_quarters: int = Field(ge=0)
    analyzed_quarters: int = Field(ge=0)
    ttm_reconstructed: bool
    metrics: dict[str, Decimal | None]
    growth_decomposition: dict[str, Decimal | None]
    source_artifact_ids: tuple[str, ...] = Field(min_length=1)
    source_object_hashes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_fundamental_coverage(self) -> CompanyFundamentalSnapshot:
        required_metrics = {
            "revenue",
            "parent_net_profit",
            "adjusted_net_profit",
            "gross_margin",
            "operating_margin",
            "roe",
            "roic",
            "operating_cash_flow",
            "free_cash_flow",
            "capital_expenditure",
            "receivables",
            "inventory",
            "contract_liabilities",
            "net_debt",
            "financing_cost",
            "shares_outstanding",
            "dividends",
            "buybacks",
            "earnings_revision",
        }
        required_growth_drivers = {
            "price",
            "volume",
            "consolidation",
            "foreign_exchange",
            "non_recurring",
            "subsidy",
            "asset_disposal",
            "fair_value_change",
            "core_business",
        }
        if not required_metrics <= set(self.metrics):
            raise ValueError(
                "fundamental snapshot is missing mandatory operating/financial metrics"
            )
        if not required_growth_drivers <= set(self.growth_decomposition):
            raise ValueError("fundamental growth decomposition is incomplete")
        required_years = min(5, self.available_complete_years)
        required_quarters = min(12, self.available_quarters)
        if self.analyzed_complete_years < required_years:
            raise ValueError("fundamental research did not analyze all required complete years")
        if self.analyzed_quarters < required_quarters:
            raise ValueError("fundamental research did not analyze all required available quarters")
        if not self.ttm_reconstructed:
            raise ValueError("fundamental research must reconstruct TTM")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("fundamental source artifact/hash lineage must be one-to-one")
        return self

    def recommendation_history_complete(self) -> bool:
        return (
            self.available_complete_years >= 5
            and self.analyzed_complete_years >= 5
            and self.available_quarters >= 12
            and self.analyzed_quarters >= 12
            and self.ttm_reconstructed
        )

    def recommendation_metrics_complete(self) -> bool:
        return all(value is not None for value in self.metrics.values())

    def recommendation_growth_decomposition_complete(self) -> bool:
        return all(value is not None for value in self.growth_decomposition.values())

    def recommendation_ready(self) -> bool:
        return (
            self.recommendation_history_complete()
            and self.recommendation_metrics_complete()
            and self.recommendation_growth_decomposition_complete()
        )


class FinancialQualityAssessment(AStockModel):
    schema_version: str = "financial-quality-assessment-v1"
    instrument_id: str = Field(min_length=1)
    accounting_quality_score: Decimal = Field(ge=0, le=100)
    checks: dict[str, bool | str | Decimal]
    red_flags: tuple[str, ...] = ()
    critical_veto: bool = False
    critical_veto_reasons: tuple[str, ...] = ()
    audit_opinion: str = Field(min_length=1)
    cash_conversion_quality: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_accounting_checks(self) -> FinancialQualityAssessment:
        required = {
            "audit_opinion",
            "non_standard_opinion",
            "revenue_cashflow_divergence",
            "receivables_anomaly",
            "inventory_anomaly",
            "gross_margin_anomaly",
            "capitalized_r_and_d",
            "goodwill_impairment",
            "asset_disposal",
            "government_subsidy",
            "related_party_transactions",
            "controlling_shareholder_fund_occupation",
            "share_pledge",
            "guarantees",
            "short_debt_long_investment",
            "cash_and_interest_bearing_debt",
            "non_recurring_items",
            "minority_interest",
            "cash_conversion_quality",
        }
        if not required <= set(self.checks):
            raise ValueError("financial quality audit is missing mandatory checks")
        if self.critical_veto != bool(self.critical_veto_reasons):
            raise ValueError("financial critical veto must carry explicit reasons")
        return self


class GovernanceAssessment(AStockModel):
    schema_version: str = "governance-assessment-v1"
    instrument_id: str = Field(min_length=1)
    governance_score: Decimal = Field(ge=0, le=100)
    checks: dict[str, bool | str | Decimal]
    red_flags: tuple[str, ...] = ()
    critical_veto: bool = False
    critical_veto_reasons: tuple[str, ...] = ()
    controller: str | None = None
    management_stability: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_governance_checks(self) -> GovernanceAssessment:
        required = {
            "controlling_shareholder",
            "actual_controller",
            "management_stability",
            "regulatory_penalties",
            "formal_investigations",
            "director_executive_changes",
            "insider_reductions",
            "share_pledges",
            "related_party_transactions",
            "fund_occupation",
            "illegal_guarantees",
            "auditor_changes",
            "material_litigation",
        }
        if not required <= set(self.checks):
            raise ValueError("governance assessment is missing mandatory checks")
        if self.critical_veto != bool(self.critical_veto_reasons):
            raise ValueError("governance critical veto must carry explicit reasons")
        return self


class ValuationScenario(AStockModel):
    schema_version: str = "recommendation-valuation-scenario-v1"
    scenario: Literal["BEAR", "BASE", "BULL"]
    per_share_value: Decimal = Field(gt=0)
    probability: Decimal = Field(ge=0, le=1)
    expected_return: Decimal


class RecommendationValuationSnapshot(AStockModel):
    schema_version: str = "recommendation-valuation-snapshot-v1"
    instrument_id: str = Field(min_length=1)
    method_family: str = Field(min_length=1)
    methods: tuple[str, ...] = Field(min_length=2)
    current_price: Decimal = Field(gt=0)
    scenarios: tuple[ValuationScenario, ...] = Field(min_length=3, max_length=3)
    return_horizon_months: Decimal | None = Field(
        default=None, gt=0, exclude_if=lambda v: v is None
    )
    expected_return_mean: Decimal
    expected_return_downside: Decimal
    margin_of_safety: Decimal
    historical_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    peer_relative_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    entry_quality_state: EntryQualityState | None = None
    entry_quality_score: Decimal | None = Field(default=None, ge=0, le=1)
    entry_quality_reason_codes: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = Field(min_length=1)
    source_object_hashes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_valuation(self) -> RecommendationValuationSnapshot:
        normalized_methods = {item.strip().upper() for item in self.methods if item.strip()}
        if len(normalized_methods) < 2:
            raise ValueError("valuation must use at least two distinct methods or lenses")
        if normalized_methods <= {"PE", "P/E"}:
            raise ValueError("PE-only valuation is not allowed")
        by_scenario = {item.scenario: item for item in self.scenarios}
        if set(by_scenario) != {"BEAR", "BASE", "BULL"}:
            raise ValueError("valuation requires Bear/Base/Bull exactly once")
        if not (
            by_scenario["BEAR"].per_share_value
            <= by_scenario["BASE"].per_share_value
            <= by_scenario["BULL"].per_share_value
        ):
            raise ValueError("valuation scenario values are not ordered Bear <= Base <= Bull")
        if sum((item.probability for item in self.scenarios), Decimal("0")) != Decimal("1"):
            raise ValueError("valuation scenario probabilities must sum to one")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("valuation source artifact/hash lineage must be one-to-one")
        return self


class NewsEvent(AStockModel):
    schema_version: str = "recommendation-news-event-v1"
    event_id: str = Field(min_length=1)
    instrument_id: str | None = None
    event_timestamp: AwareDatetime
    source_id: str = Field(min_length=1)
    category: str = "uncategorized"
    related_entity_ids: tuple[str, ...] = ()
    person_names: tuple[str, ...] = ()
    relation_scope: Literal[
        "COMPANY",
        "CONTROLLER",
        "SUBSIDIARY",
        "PARTNER",
        "UPSTREAM",
        "DOWNSTREAM",
        "PUBLIC_OFFICE",
        "INDUSTRY",
        "POLICY",
    ] = "COMPANY"
    evidence_class: EventEvidenceClass
    confidence: Decimal = Field(ge=0, le=1)
    expected_direction: Literal["POSITIVE", "NEGATIVE", "MIXED", "NEUTRAL", "UNKNOWN"]
    impact_horizon: str = Field(min_length=1)
    already_priced_probability: Decimal = Field(ge=0, le=1)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_event_relations(self) -> NewsEvent:
        if not self.category.strip():
            raise ValueError("news event category must be non-empty")
        for values in (self.related_entity_ids, self.person_names):
            if values != tuple(sorted(set(values))):
                raise ValueError("news event relation values must be sorted and unique")
        return self


class NewsEventCoverage(AStockModel):
    schema_version: str = "recommendation-news-event-coverage-v1"
    as_of: AwareDatetime
    covered_windows_days: tuple[int, ...]
    covered_categories: tuple[str, ...]
    source_classes_seen: tuple[EventEvidenceClass, ...]
    event_ids: tuple[str, ...]
    conflicts_resolved: bool

    @model_validator(mode="after")
    def validate_news_coverage(self) -> NewsEventCoverage:
        if set(self.covered_windows_days) != {7, 30, 90}:
            raise ValueError("news research must cover 7/30/90 day windows")
        required_categories = {
            "earnings",
            "orders",
            "m_and_a",
            "restructuring",
            "buyback",
            "insider_reduction",
            "financing",
            "major_contract",
            "product",
            "policy",
            "litigation",
            "investigation",
            "safety_incident",
            "management",
            "industry_pricing",
            "supply_chain",
            "overseas_sanctions_trade",
            "domestic_upstream_downstream_news",
            "global_upstream_downstream_news",
            "domestic_upstream_downstream_policy",
        }
        if not required_categories <= set(self.covered_categories):
            raise ValueError("news research is missing mandatory event categories")
        return self


class FactorSnapshot(AStockModel):
    schema_version: str = "recommendation-factor-snapshot-v1"
    instrument_id: str = Field(min_length=1)
    value: Decimal | None = None
    quality: Decimal | None = None
    growth: Decimal | None = None
    momentum: Decimal | None = None
    low_volatility: Decimal | None = None
    liquidity: Decimal | None = None
    size: Decimal | None = None
    earnings_revision: Decimal | None = None
    profitability: Decimal | None = None
    crowding: Decimal | None = None
    dominant_exposures: tuple[str, ...] = ()

    def missing_dimensions(self) -> tuple[str, ...]:
        dimensions = (
            "value",
            "quality",
            "growth",
            "momentum",
            "low_volatility",
            "liquidity",
            "size",
            "earnings_revision",
            "profitability",
            "crowding",
        )
        return tuple(name for name in dimensions if getattr(self, name) is None)

    def recommendation_ready(self) -> bool:
        return not self.missing_dimensions()


class ChallengerAssessment(AStockModel):
    schema_version: str = "recommendation-challenger-v1"
    instrument_id: str = Field(min_length=1)
    independent_context_id: str = Field(min_length=1)
    raw_fact_artifact_ids: tuple[str, ...] = Field(min_length=1)
    primary_final_label_visible: Literal[False] = False
    market_may_be_right_because: str = Field(min_length=1)
    overlooked_bad_news: tuple[str, ...]
    valuation_fully_priced_risk: str = Field(min_length=1)
    thesis_invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    maximum_reasonable_downside: Decimal = Field(ge=0)
    better_alternatives: tuple[str, ...] = ()
    material_conflict_with_primary: bool = False
    confidence_adjustment: Decimal = Decimal("0")

    @model_validator(mode="after")
    def validate_challenge_independence(self) -> ChallengerAssessment:
        if self.material_conflict_with_primary and self.confidence_adjustment >= 0:
            raise ValueError("material challenger conflict must reduce recommendation confidence")
        return self


class NewsEventResearchPack(AStockModel):
    schema_version: str = "full-research-news-event-pack-v1"
    instrument_id: str = Field(min_length=1)
    as_of: AwareDatetime
    events: tuple[NewsEvent, ...]
    coverage: NewsEventCoverage
    evidence_ids: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = ()
    source_object_hashes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_event_pack(self) -> NewsEventResearchPack:
        if self.coverage.as_of != self.as_of:
            raise ValueError("event pack coverage as_of must match the pack")
        if set(self.coverage.event_ids) != {item.event_id for item in self.events}:
            raise ValueError("event pack coverage ids must match the frozen event set")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("event pack source artifact/hash lineage must be one-to-one")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("event pack Evidence ids must be sorted and unique")
        return self

    def recommendation_ready(
        self,
        *,
        required_windows: tuple[int, ...] = (7, 30, 90),
        required_categories: tuple[str, ...] = (),
    ) -> bool:
        return (
            self.coverage.covered_windows_days == required_windows
            and set(required_categories) <= set(self.coverage.covered_categories)
            and self.coverage.conflicts_resolved
            and bool(self.evidence_ids or self.source_artifact_ids)
        )


class QuantFactorResearchPack(AStockModel):
    schema_version: str = "full-research-quant-factor-pack-v1"
    as_of: AwareDatetime
    factors: tuple[FactorSnapshot, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = ()
    source_object_hashes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_factor_pack(self) -> QuantFactorResearchPack:
        ids = [item.instrument_id for item in self.factors]
        if len(ids) != len(set(ids)):
            raise ValueError("quant factor pack instruments must be unique")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("quant factor source artifact/hash lineage must be one-to-one")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("quant factor Evidence ids must be sorted and unique")
        return self

    def recommendation_ready(self) -> bool:
        return all(item.recommendation_ready() for item in self.factors) and bool(
            self.evidence_ids or self.source_artifact_ids
        )


class CandidateDecisionNarrative(AStockModel):
    schema_version: str = "recommendation-candidate-narrative-v1"
    instrument_id: str = Field(min_length=1)
    investment_thesis: str = Field(min_length=1)
    why_now: str = Field(min_length=1)
    catalysts: tuple[str, ...] = Field(min_length=1)
    primary_risks: tuple[str, ...] = Field(min_length=1)
    thesis_invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class CandidateRankingEntry(AStockModel):
    schema_version: str = "recommendation-candidate-ranking-v1"
    instrument_id: str = Field(min_length=1)
    industry_id: str = Field(min_length=1)
    expected_return: Decimal
    downside: Decimal = Field(ge=0)
    quality: Decimal
    valuation: Decimal
    catalyst: Decimal
    macro_fit: Decimal
    industry_fit: Decimal
    entry_quality_state: EntryQualityState | None = None
    entry_quality_score: Decimal | None = Field(default=None, ge=0, le=1)
    entry_timing_risk: bool = False
    momentum: Decimal | None
    liquidity: Decimal | None
    accounting_risk: Decimal = Field(ge=0)
    governance_risk: Decimal = Field(ge=0)
    evidence_confidence: Decimal = Field(ge=0, le=1)
    eligible: bool
    rejection_reasons: tuple[str, ...] = ()
    accounting_critical_veto: bool = False
    governance_critical_veto: bool = False
    factor_snapshot: FactorSnapshot

    @model_validator(mode="after")
    def validate_candidate(self) -> CandidateRankingEntry:
        if (self.accounting_critical_veto or self.governance_critical_veto) and self.eligible:
            raise ValueError("critical financial/governance veto cannot remain eligible")
        if self.eligible and not self.factor_snapshot.recommendation_ready():
            raise ValueError("eligible candidate requires complete ten-factor coverage")
        if not self.eligible and not self.rejection_reasons:
            raise ValueError("rejected candidate requires an auditable reason")
        return self


class PortfolioPositionPlan(AStockModel):
    schema_version: str = "recommendation-portfolio-position-v1"
    instrument_id: str = Field(min_length=1)
    industry_id: str = Field(min_length=1)
    target_weight: Decimal = Field(gt=0, le=1)
    target_amount: Decimal = Field(ge=0)
    reference_price: Decimal = Field(gt=0)
    target_shares: int = Field(ge=0)
    lot_size: int = Field(gt=0)
    estimated_cost: Decimal = Field(ge=0)
    estimated_slippage: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_lot(self) -> PortfolioPositionPlan:
        if self.target_shares % self.lot_size != 0:
            raise ValueError("portfolio position shares must obey the configured lot size")
        return self


class PortfolioConstructionSnapshot(AStockModel):
    schema_version: str = "recommendation-portfolio-construction-v1"
    capital: Decimal = Field(gt=0)
    target_annual_return: Decimal | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    annual_profit_target: Decimal | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    target_horizon_months: Decimal | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )
    target_horizon_return: Decimal | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    target_horizon_profit: Decimal | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    expected_research_return: Decimal | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    expected_research_profit: Decimal | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    modeled_downside_loss: Decimal | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    return_objective_gap: Decimal | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    objective_status: Literal[
        "MEETS_HORIZON_TARGET", "BELOW_HORIZON_TARGET",
        "NO_ELIGIBLE_POSITIONS", "HORIZON_NOT_COMPARABLE"
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    positions: tuple[PortfolioPositionPlan, ...]
    cash: Decimal = Field(ge=0)
    cash_weight: Decimal = Field(ge=0, le=1)
    industry_weights: dict[str, Decimal]
    factor_exposures: dict[str, Decimal]
    portfolio_beta: Decimal | None = None
    expected_volatility: Decimal | None = Field(default=None, ge=0)
    expected_shortfall: Decimal | None = Field(default=None, ge=0)
    max_drawdown_proxy: Decimal | None = Field(default=None, ge=0)
    turnover: Decimal | None = Field(default=None, ge=0)
    estimated_transaction_cost: Decimal = Field(ge=0)
    estimated_slippage: Decimal = Field(ge=0)
    constraint_violations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_portfolio(self) -> PortfolioConstructionSnapshot:
        invested = sum((item.target_amount for item in self.positions), Decimal("0"))
        if abs((invested + self.cash) - self.capital) > Decimal("0.02"):
            raise ValueError("portfolio amount plus cash must reconcile to capital")
        weights = sum((item.target_weight for item in self.positions), Decimal("0"))
        if abs((weights + self.cash_weight) - Decimal("1")) > Decimal("0.0001"):
            raise ValueError("portfolio weights plus cash must sum to one")
        if len({item.instrument_id for item in self.positions}) != len(self.positions):
            raise ValueError("portfolio positions must be unique")
        base_objective_values = (
            self.target_annual_return,
            self.annual_profit_target,
            self.expected_research_return,
            self.expected_research_profit,
            self.modeled_downside_loss,
            self.objective_status,
        )
        horizon_objective_values = (
            self.target_horizon_months,
            self.target_horizon_return,
            self.target_horizon_profit,
            self.return_objective_gap,
        )
        if all(value is None for value in (*base_objective_values, *horizon_objective_values)):
            return self
        if any(value is None for value in base_objective_values):
            raise ValueError(
                "investment expectation base objective fields must be complete when present"
            )
        assert self.target_annual_return is not None
        assert self.annual_profit_target is not None
        assert self.expected_research_return is not None
        assert self.expected_research_profit is not None
        assert self.objective_status is not None
        if (
            abs(self.annual_profit_target - self.capital * self.target_annual_return)
            > Decimal("0.02")
        ):
            raise ValueError("annual profit target must reconcile to capital and annual target")
        if (
            abs(self.expected_research_profit - self.capital * self.expected_research_return)
            > Decimal("0.02")
        ):
            raise ValueError(
                "expected research profit must reconcile to capital and expected return"
            )
        if self.objective_status in {"HORIZON_NOT_COMPARABLE", "NO_ELIGIBLE_POSITIONS"}:
            if any(value is not None for value in horizon_objective_values):
                raise ValueError(
                    "non-comparable or empty portfolios must not claim a horizon target gap"
                )
            if self.objective_status == "NO_ELIGIBLE_POSITIONS" and self.positions:
                raise ValueError("NO_ELIGIBLE_POSITIONS requires an empty portfolio")
            if self.objective_status == "HORIZON_NOT_COMPARABLE" and not self.positions:
                raise ValueError("HORIZON_NOT_COMPARABLE requires at least one position")
            return self
        if any(value is None for value in horizon_objective_values):
            raise ValueError("comparable objective status requires complete horizon fields")
        assert self.target_horizon_return is not None
        assert self.target_horizon_profit is not None
        assert self.return_objective_gap is not None
        if (
            abs(self.target_horizon_profit - self.capital * self.target_horizon_return)
            > Decimal("0.02")
        ):
            raise ValueError("horizon profit target must reconcile to capital and horizon target")
        if abs(
            self.return_objective_gap
            - (self.target_horizon_return - self.expected_research_return)
        ) > Decimal("0.000001"):
            raise ValueError("return objective gap must reconcile to target and expected return")
        expected_status = (
            "MEETS_HORIZON_TARGET"
            if self.return_objective_gap <= 0
            else "BELOW_HORIZON_TARGET"
        )
        if self.objective_status != expected_status:
            raise ValueError("return objective status conflicts with the portfolio objective gap")
        return self


class PortfolioRiskAudit(AStockModel):
    schema_version: str = "recommendation-portfolio-risk-audit-v1"
    status: FullResearchNodeStatus
    portfolio_beta: Decimal
    expected_volatility: Decimal = Field(ge=0)
    expected_shortfall: Decimal = Field(ge=0)
    max_drawdown_proxy: Decimal = Field(ge=0)
    liquidity_capacity_pass: bool
    max_order_adv_fraction: Decimal = Field(ge=0, le=1)
    turnover: Decimal = Field(ge=0)
    transaction_cost: Decimal = Field(ge=0)
    slippage: Decimal = Field(ge=0)
    correlation_constraint_pass: bool
    industry_constraint_pass: bool
    factor_constraint_pass: bool
    single_name_constraint_pass: bool
    violations: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = Field(min_length=1)
    source_object_hashes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_risk_audit(self) -> PortfolioRiskAudit:
        mechanical_pass = all(
            (
                self.liquidity_capacity_pass,
                self.correlation_constraint_pass,
                self.industry_constraint_pass,
                self.factor_constraint_pass,
                self.single_name_constraint_pass,
            )
        )
        if self.status == FullResearchNodeStatus.PASS and (not mechanical_pass or self.violations):
            raise ValueError("PASS portfolio risk audit cannot carry constraint failures")
        if self.status != FullResearchNodeStatus.PASS and not self.violations:
            raise ValueError("non-PASS portfolio risk audit requires explicit violations")
        if len(self.source_artifact_ids) != len(self.source_object_hashes):
            raise ValueError("risk-audit source artifact/hash lineage must be one-to-one")
        return self


class ExecutionInstruction(AStockModel):
    schema_version: str = "recommendation-execution-instruction-v1"
    instrument_id: str = Field(min_length=1)
    quote_observed_at: AwareDatetime
    quote_available_to_system_at: AwareDatetime
    reference_price: Decimal = Field(gt=0)
    quote_age_seconds: int = Field(ge=0)
    quote_freshness_sla_seconds: int = Field(gt=0)
    quote_fresh: bool
    initial_shares: int | None = Field(default=None, ge=0)
    target_shares: int | None = Field(default=None, ge=0)
    buy_range_low: Decimal | None = Field(default=None, gt=0)
    buy_range_high: Decimal | None = Field(default=None, gt=0)
    maximum_acceptable_price: Decimal | None = Field(default=None, gt=0)
    goal_entry_price_ceiling: Decimal | None = Field(
        default=None, gt=0, exclude_if=lambda v: v is None
    )
    scenario_profit_rmb: Decimal | None = Field(default=None, exclude_if=lambda v: v is None)
    downside_loss_rmb: Decimal | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    add_conditions: tuple[str, ...]
    reduce_exit_conditions: tuple[str, ...]
    thesis_invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    time_stop_condition: str = Field(min_length=1)
    valuation_exit_condition: str = Field(min_length=1)
    event_exit_conditions: tuple[str, ...]
    instant_quantity_allowed: bool

    @model_validator(mode="after")
    def validate_execution(self) -> ExecutionInstruction:
        if self.quote_available_to_system_at < self.quote_observed_at:
            raise ValueError("quote cannot be available before observation")
        if self.quote_fresh != (self.quote_age_seconds <= self.quote_freshness_sla_seconds):
            raise ValueError("quote freshness flag conflicts with age/SLA")
        if not self.quote_fresh and (
            self.instant_quantity_allowed
            or self.initial_shares is not None
            or self.target_shares is not None
        ):
            raise ValueError("stale quote cannot authorize immediate share quantities")
        if self.buy_range_low is not None and self.buy_range_high is not None:
            if self.buy_range_high < self.buy_range_low:
                raise ValueError("buy range is reversed")
        if self.instant_quantity_allowed and (
            self.initial_shares is None or self.target_shares is None
        ):
            raise ValueError(
                "instant execution permission requires explicit initial and target shares"
            )
        return self


class FullResearchNodeExecution(AStockModel):
    schema_version: str = "full-research-node-execution-v1"
    node: FullResearchNode
    status: FullResearchNodeStatus
    mandatory: bool = True
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    reason: str | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_node(self) -> FullResearchNodeExecution:
        if self.completed_at < self.started_at:
            raise ValueError("node completion cannot precede start")
        if self.status == FullResearchNodeStatus.PASS and not (
            self.artifact_ids or self.evidence_ids
        ):
            raise ValueError("PASS mandatory research node requires auditable evidence identity")
        if self.status != FullResearchNodeStatus.PASS and not self.reason:
            raise ValueError("non-PASS node requires a reason")
        return self


class FullResearchDAGReceipt(AStockModel):
    schema_version: str = "full-research-dag-receipt-v1"
    dag_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    as_of_timestamp: AwareDatetime
    executions: tuple[FullResearchNodeExecution, ...]
    degraded_allowed: tuple[FullResearchNode, ...] = ()
    mandatory_coverage: Decimal = Field(ge=0, le=1)
    publication_ready: bool

    @model_validator(mode="after")
    def validate_dag(self) -> FullResearchDAGReceipt:
        nodes = [item.node for item in self.executions]
        if len(nodes) != len(set(nodes)):
            raise ValueError("full research DAG cannot contain duplicate nodes")
        mandatory = [item for item in self.executions if item.mandatory]
        accepted = [
            item
            for item in mandatory
            if item.status == FullResearchNodeStatus.PASS
            or (
                item.status == FullResearchNodeStatus.DEGRADED
                and item.node in self.degraded_allowed
            )
        ]
        coverage = (
            Decimal("1") if not mandatory else Decimal(len(accepted)) / Decimal(len(mandatory))
        )
        if abs(coverage - self.mandatory_coverage) > Decimal("0.000001"):
            raise ValueError("full research mandatory coverage is inconsistent")
        ready = bool(mandatory) and len(accepted) == len(mandatory)
        if self.publication_ready != ready:
            raise ValueError(
                "full research publication readiness conflicts with mandatory node states"
            )
        return self


class HoldingDecisionSnapshot(AStockModel):
    schema_version: str = "holding-decision-snapshot-v1"
    instrument_id: str = Field(min_length=1)
    position_id: str = Field(min_length=1)
    portfolio_context_revision: str = Field(min_length=1)
    recommended_action: Literal["HOLD", "ADD", "TRIM", "EXIT", "REVIEW"]
    action_confidence: Decimal = Field(ge=0, le=1)
    thesis_strength_change: str = Field(min_length=1)
    risk_change: str = Field(min_length=1)
    current_quantity: Decimal | None = Field(default=None, ge=0)
    current_weight: Decimal | None = Field(default=None, ge=0, le=1)
    target_weight_lower: Decimal | None = Field(default=None, ge=0, le=1)
    target_weight_mid: Decimal | None = Field(default=None, ge=0, le=1)
    target_weight_upper: Decimal | None = Field(default=None, ge=0, le=1)
    target_quantity_min: int | None = Field(default=None, ge=0)
    target_quantity_max: int | None = Field(default=None, ge=0)
    preconditions: tuple[str, ...] = ()
    reversal_conditions: tuple[str, ...] = ()
    next_review_conditions: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_target_band(self) -> HoldingDecisionSnapshot:
        weights = (self.target_weight_lower, self.target_weight_mid, self.target_weight_upper)
        if any(value is not None for value in weights):
            if any(value is None for value in weights):
                raise ValueError("holding target weights must be complete")
            assert self.target_weight_lower is not None
            assert self.target_weight_mid is not None
            assert self.target_weight_upper is not None
            if not self.target_weight_lower <= self.target_weight_mid <= self.target_weight_upper:
                raise ValueError("holding target weights must satisfy lower<=mid<=upper")
        if (
            self.target_quantity_min is not None
            and self.target_quantity_max is not None
            and self.target_quantity_min > self.target_quantity_max
        ):
            raise ValueError("holding target quantity range is reversed")
        return self


class PublicationDecision(AStockModel):
    schema_version: str = "recommendation-publication-decision-v1"
    status: Literal["PUBLISH", "CONDITIONAL_ONLY", "BLOCKED"]
    formal_recommendation_allowed: bool
    instant_trade_parameters_allowed: bool
    reasons: tuple[str, ...] = ()
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_publication(self) -> PublicationDecision:
        if self.status == "BLOCKED" and self.formal_recommendation_allowed:
            raise ValueError("blocked publication cannot allow a formal recommendation")
        if self.instant_trade_parameters_allowed and not self.formal_recommendation_allowed:
            raise ValueError("instant trade parameters require a formal recommendation")
        if self.status == "CONDITIONAL_ONLY" and self.instant_trade_parameters_allowed:
            raise ValueError(
                "conditional-only publication cannot contain immediate trade quantities"
            )
        return self


class RecommendationResearchReceipt(AStockModel):
    schema_version: str = "recommendation-research-receipt-v1"
    receipt_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    as_of: AwareDatetime
    request_contract: FullResearchRequestContract
    holding_reviews: tuple[HoldingDecisionSnapshot, ...] = ()
    pit_snapshot: PointInTimeSnapshot
    dag: FullResearchDAGReceipt
    source_manifest: tuple[SourceLineageEntry, ...]
    skill_executions: dict[str, FullResearchNodeStatus]
    candidate_universe: tuple[str, ...]
    candidate_rankings: tuple[CandidateRankingEntry, ...]
    candidate_narratives: tuple[CandidateDecisionNarrative, ...]
    rejected_candidates: dict[str, tuple[str, ...]]
    fundamentals: tuple[CompanyFundamentalSnapshot, ...]
    financial_quality: tuple[FinancialQualityAssessment, ...]
    governance: tuple[GovernanceAssessment, ...]
    valuations: tuple[RecommendationValuationSnapshot, ...]
    news_events: tuple[NewsEvent, ...]
    news_coverage: NewsEventCoverage
    challengers: tuple[ChallengerAssessment, ...]
    factor_scores: tuple[FactorSnapshot, ...]
    macro: MacroResearchOutcome
    industries: tuple[IndustryResearchOutcome, ...]
    portfolio: PortfolioConstructionSnapshot
    risk_audit: PortfolioRiskAudit
    execution_plans: tuple[ExecutionInstruction, ...]
    optimizer_inputs: dict[str, Decimal | int | str | bool]
    optimizer_outputs: dict[str, Decimal | int | str | bool]
    publication: PublicationDecision
    model_versions: dict[str, str]
    code_version: str = Field(min_length=1)
    config_versions: dict[str, str]
    input_artifact_hashes: dict[str, str]
    receipt_hash: str = Field(pattern=_SHA256)
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_receipt(self) -> RecommendationResearchReceipt:
        if (
            self.request_contract.request_id != self.request_id
            or self.dag.request_id != self.request_id
        ):
            raise ValueError("recommendation receipt request identities differ")
        if (
            self.request_contract.as_of_timestamp != self.as_of
            or self.pit_snapshot.as_of_timestamp != self.as_of
        ):
            raise ValueError("recommendation receipt must use one as-of timestamp")
        if (
            self.dag.as_of_timestamp != self.as_of
            or self.pit_snapshot.request_id != self.request_id
        ):
            raise ValueError("recommendation DAG/PIT identity differs from the receipt")
        source_ids = {item.source_id for item in self.source_manifest}
        if source_ids != {item.source_id for item in self.pit_snapshot.sources}:
            raise ValueError("recommendation source manifest must equal PIT source lineage")
        rankings = {item.instrument_id: item for item in self.candidate_rankings}
        if set(rankings) - set(self.candidate_universe):
            raise ValueError("ranked candidate is absent from the frozen candidate universe")
        holding_ids = [item.instrument_id for item in self.holding_reviews]
        if len(holding_ids) != len(set(holding_ids)):
            raise ValueError("holding decision snapshots must use unique instruments")
        if (
            self.request_contract.decision_context == "EXISTING_HOLDING"
            and not self.holding_reviews
        ):
            raise ValueError("existing-holding Full Research requires a frozen holding review")
        if self.holding_reviews and self.request_contract.decision_context != "EXISTING_HOLDING":
            raise ValueError("holding reviews require EXISTING_HOLDING request context")
        if set(holding_ids) - set(rankings):
            raise ValueError(
                "holding review instrument is absent from the ranked candidate universe"
            )
        for instrument, reasons in self.rejected_candidates.items():
            if instrument not in rankings or rankings[instrument].eligible:
                raise ValueError("rejected candidate must match an ineligible ranking entry")
            if tuple(rankings[instrument].rejection_reasons) != tuple(reasons):
                raise ValueError("rejected candidate reasons differ from ranking evidence")
        eligible = {key for key, item in rankings.items() if item.eligible}
        if any(
            review.recommended_action == "ADD" and review.instrument_id not in eligible
            for review in self.holding_reviews
        ):
            raise ValueError("holding ADD cannot bypass candidate admission or CriticalVeto")
        narratives = {item.instrument_id: item for item in self.candidate_narratives}
        if set(narratives) != set(rankings):
            raise ValueError("candidate narratives must exactly cover the ranked candidate set")
        if any(position.instrument_id not in eligible for position in self.portfolio.positions):
            raise ValueError("portfolio contains a candidate that did not pass admission")
        dag_statuses = {item.node.value: item.status for item in self.dag.executions}
        if self.skill_executions != dag_statuses:
            raise ValueError("skill execution status map must match the Mandatory Research DAG")
        ranked_ids = set(rankings)
        fundamental_ids = {item.instrument_id for item in self.fundamentals}
        financial_ids = {item.instrument_id for item in self.financial_quality}
        governance_ids = {item.instrument_id for item in self.governance}
        valuation_ids = {item.instrument_id for item in self.valuations}
        challenger_ids = {item.instrument_id for item in self.challengers}
        factor_ids = {item.instrument_id for item in self.factor_scores}
        if not eligible <= fundamental_ids:
            raise ValueError("eligible candidates lack mandatory fundamental coverage")
        if not eligible <= financial_ids or not eligible <= governance_ids:
            raise ValueError("eligible candidates lack financial/governance gate coverage")
        if not eligible <= valuation_ids or not eligible <= challenger_ids:
            raise ValueError("eligible candidates lack valuation/challenger coverage")
        if ranked_ids != factor_ids:
            raise ValueError("candidate ranking and factor coverage must use the same set")
        factor_by_id = {item.instrument_id: item for item in self.factor_scores}
        if any(
            item.factor_snapshot != factor_by_id[item.instrument_id] for item in rankings.values()
        ):
            raise ValueError("ranking factor snapshots differ from the frozen factor score set")
        industry_ids = {item.industry_id for item in self.industries}
        if not {item.industry_id for item in rankings.values()} <= industry_ids:
            raise ValueError("ranked candidates lack industry context")
        fundamental_by_id = {item.instrument_id: item for item in self.fundamentals}
        industry_by_id = {item.industry_id: item for item in self.industries}
        for instrument_id in eligible:
            if not fundamental_by_id[instrument_id].recommendation_ready():
                raise ValueError(
                    "eligible candidate lacks complete five-year/twelve-quarter fundamentals"
                )
            industry_id = rankings[instrument_id].industry_id
            if not industry_by_id[industry_id].recommendation_peer_coverage_complete():
                raise ValueError("eligible candidate lacks five-peer industry coverage")
            if not rankings[instrument_id].factor_snapshot.recommendation_ready():
                raise ValueError("eligible candidate lacks complete ten-factor coverage")
        event_ids = {item.event_id for item in self.news_events}
        if set(self.news_coverage.event_ids) != event_ids or self.news_coverage.as_of != self.as_of:
            raise ValueError("news/event coverage differs from the frozen event set")
        if not self.news_coverage.conflicts_resolved:
            raise ValueError("unresolved news conflicts cannot enter a recommendation receipt")
        portfolio_ids = {item.instrument_id for item in self.portfolio.positions}
        if {item.instrument_id for item in self.execution_plans} != portfolio_ids:
            raise ValueError("execution plans must exactly cover portfolio positions")
        if self.publication.formal_recommendation_allowed and not self.dag.publication_ready:
            raise ValueError("publication cannot outrun the mandatory research DAG")
        if (
            self.publication.formal_recommendation_allowed
            and self.risk_audit.status != FullResearchNodeStatus.PASS
        ):
            raise ValueError("formal recommendation requires a PASS portfolio risk audit")
        if any(
            item.eligible and (item.accounting_critical_veto or item.governance_critical_veto)
            for item in rankings.values()
        ):
            raise ValueError("critical veto cannot leak into an eligible candidate")
        if self.publication.instant_trade_parameters_allowed and (
            not self.execution_plans
            or not all(item.instant_quantity_allowed for item in self.execution_plans)
        ):
            raise ValueError("publication execution permission exceeds the execution plan evidence")
        if (
            self.publication.formal_recommendation_allowed
            and self.pit_snapshot.status != FullResearchNodeStatus.PASS
        ):
            raise ValueError("formal recommendation requires PASS point-in-time evidence")
        return self


class RecommendationReevaluationRecord(AStockModel):
    schema_version: str = "recommendation-reevaluation-v1"
    revision_id: str = Field(min_length=1)
    original_receipt_id: str = Field(min_length=1)
    original_receipt_hash: str = Field(pattern=_SHA256)
    instrument_id: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    observed_at: AwareDatetime
    previous_view: str = Field(min_length=1)
    new_view: str = Field(min_length=1)
    change_reason: str = Field(min_length=1)
    recommendation_change: str = Field(min_length=1)
    realized_return: Decimal | None = None
    maximum_favorable_excursion: Decimal | None = None
    maximum_adverse_excursion: Decimal | None = None
    attribution: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = ()
    revision_hash: str = Field(pattern=_SHA256)


def latest_visible_timestamp(source: SourceLineageEntry) -> datetime:
    """Return the latest timestamp that controls point-in-time availability."""

    return max(
        value
        for value in (
            source.observed_at,
            source.release_at,
            source.published_at,
            source.captured_at,
            source.available_to_system_at,
        )
        if value is not None
    )
