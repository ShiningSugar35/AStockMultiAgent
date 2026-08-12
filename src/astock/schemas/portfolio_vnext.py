"""Versioned Phase 10 portfolio risk, implementation, stress, and attribution contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel


class CompactRiskFactor(StrEnum):
    MARKET = "MARKET"
    SIZE = "SIZE"
    VALUE = "VALUE"
    MOMENTUM = "MOMENTUM"
    QUALITY_PROFITABILITY = "QUALITY_PROFITABILITY"
    VOLATILITY = "VOLATILITY"
    LIQUIDITY = "LIQUIDITY"
    INDUSTRY = "INDUSTRY"


class RiskExposureProvenance(StrEnum):
    MARKET_DERIVED = "MARKET_DERIVED"
    FROZEN_INPUT = "FROZEN_INPUT"
    CALLER_SUPPLIED_UNVERIFIED = "CALLER_SUPPLIED_UNVERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


class PortfolioFundamentalFactorInput(AStockModel):
    schema_version: str = "portfolio-fundamental-factor-input-v1"
    company_id: str = Field(pattern=r"^\d{6}$")
    size_exposure: float | None = Field(default=None, ge=-10, le=10, allow_inf_nan=False)
    value_exposure: float | None = Field(default=None, ge=-10, le=10, allow_inf_nan=False)
    quality_profitability_exposure: float | None = Field(
        default=None,
        ge=-10,
        le=10,
        allow_inf_nan=False,
    )
    source_artifact_id: str | None = None
    source_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    methodology_version: str | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> PortfolioFundamentalFactorInput:
        supplied = any(
            value is not None
            for value in (
                self.size_exposure,
                self.value_exposure,
                self.quality_profitability_exposure,
            )
        )
        frozen = self.source_artifact_id is not None or self.source_object_hash is not None
        if frozen and not (
            self.source_artifact_id and self.source_object_hash and self.methodology_version
        ):
            raise ValueError("frozen factor inputs require artifact, hash, and methodology version")
        if not supplied and frozen:
            raise ValueError("empty factor input cannot claim frozen provenance")
        return self


class PortfolioRiskExplanationRequest(AStockModel):
    schema_version: str = "portfolio-risk-explanation-request-v1"
    portfolio_analysis_artifact_id: str = Field(min_length=1)
    as_of: AwareDatetime
    position_notional_fen: dict[str, int] = Field(default_factory=dict)
    fundamental_factors: list[PortfolioFundamentalFactorInput] = Field(default_factory=list)
    liquidity_lookback_sessions: int = Field(default=60, ge=20, le=252)
    participation_cap: float = Field(default=0.10, gt=0, le=0.30, allow_inf_nan=False)
    round_trip: bool = True

    @model_validator(mode="after")
    def validate_request(self) -> PortfolioRiskExplanationRequest:
        factor_ids = [item.company_id for item in self.fundamental_factors]
        if factor_ids != sorted(set(factor_ids)):
            raise ValueError("fundamental factor inputs must be sorted and unique by company")
        if any(not key.isdigit() or len(key) != 6 for key in self.position_notional_fen):
            raise ValueError("position notional keys must be six-digit company ids")
        if any(value <= 0 for value in self.position_notional_fen.values()):
            raise ValueError("position notionals must be positive")
        return self


class CompactFactorExposure(AStockModel):
    factor: CompactRiskFactor
    exposure: float | None = Field(default=None, ge=-20, le=20, allow_inf_nan=False)
    provenance: RiskExposureProvenance
    methodology: str = Field(min_length=1)
    source_artifact_id: str | None = None
    source_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_exposure(self) -> CompactFactorExposure:
        if self.provenance is RiskExposureProvenance.UNAVAILABLE:
            if self.exposure is not None:
                raise ValueError("unavailable factor exposure cannot carry a value")
        elif self.exposure is None:
            raise ValueError("available factor exposure requires a value")
        if self.provenance is RiskExposureProvenance.FROZEN_INPUT and not (
            self.source_artifact_id and self.source_object_hash
        ):
            raise ValueError("frozen factor exposure requires artifact provenance")
        return self


class LiquidityImplementationEstimate(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    average_daily_volume_shares: float = Field(ge=0, allow_inf_nan=False)
    average_daily_amount_fen: int | None = Field(default=None, ge=0)
    median_daily_range_fraction: float = Field(ge=0, allow_inf_nan=False)
    position_notional_fen: int | None = Field(default=None, gt=0)
    participation_cap: float = Field(gt=0, le=0.30, allow_inf_nan=False)
    days_to_liquidate: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_slippage_bps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_round_trip_cost_fen: int | None = Field(default=None, ge=0)
    model_version: str = "daily-range-participation-v1"
    warning_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_codes(self) -> LiquidityImplementationEstimate:
        if self.warning_codes != sorted(set(self.warning_codes)):
            raise ValueError("liquidity warning codes must be sorted and unique")
        if self.position_notional_fen is None and any(
            value is not None
            for value in (
                self.days_to_liquidate,
                self.estimated_round_trip_cost_fen,
            )
        ):
            raise ValueError("notional-dependent estimates require a position notional")
        return self


class PortfolioAssetRiskExplanation(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    industry_tag: str | None = None
    factors: list[CompactFactorExposure] = Field(min_length=8, max_length=8)
    liquidity: LiquidityImplementationEstimate

    @model_validator(mode="after")
    def validate_factors(self) -> PortfolioAssetRiskExplanation:
        identities = [item.factor for item in self.factors]
        if identities != list(CompactRiskFactor):
            raise ValueError("asset risk explanation must contain every compact factor in order")
        return self


class PortfolioRiskExplanationReport(AStockModel):
    schema_version: str = "portfolio-risk-explanation-report-v1"
    report_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    assets: list[PortfolioAssetRiskExplanation]
    portfolio_factor_exposures: dict[CompactRiskFactor, float | None]
    estimated_one_way_turnover_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    estimated_round_trip_implementation_cost_fen: int | None = Field(default=None, ge=0)
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    alpha_signal_allowed: Literal[False] = False
    allocation_override_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> PortfolioRiskExplanationReport:
        expected = set(CompactRiskFactor)
        if set(self.portfolio_factor_exposures) != expected:
            raise ValueError("portfolio factor exposures must cover the compact factor set")
        for values in (self.warning_codes, self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("portfolio risk report lists must be sorted and unique")
        return self


class EconomicStressScenario(StrEnum):
    RATES = "RATES"
    RMB_FX = "RMB_FX"
    CREDIT_TIGHTENING = "CREDIT_TIGHTENING"
    COMMODITY = "COMMODITY"
    DOMESTIC_DEMAND = "DOMESTIC_DEMAND"
    SECTOR_MULTIPLE_COMPRESSION = "SECTOR_MULTIPLE_COMPRESSION"
    LIQUIDITY_SHOCK = "LIQUIDITY_SHOCK"
    COMPANY_THESIS_BREAK = "COMPANY_THESIS_BREAK"


class AssetStressShock(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    shock_return: float = Field(ge=-1, le=1, allow_inf_nan=False)
    rationale_code: str = Field(min_length=1)
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shock(self) -> AssetStressShock:
        for values in (self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("stress shock provenance must be sorted and unique")
        if self.source_object_hashes and any(
            len(value) != 64 for value in self.source_object_hashes
        ):
            raise ValueError("stress source hashes must be sha256 values")
        return self


class PortfolioStressScenarioInput(AStockModel):
    scenario: EconomicStressScenario
    scenario_version: str = Field(min_length=1)
    asset_shocks: list[AssetStressShock] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_assets(self) -> PortfolioStressScenarioInput:
        ids = [item.company_id for item in self.asset_shocks]
        if ids != sorted(set(ids)):
            raise ValueError("stress scenario asset shocks must be sorted and unique")
        return self


class PortfolioStressRequest(AStockModel):
    schema_version: str = "portfolio-stress-request-v1"
    portfolio_analysis_artifact_id: str = Field(min_length=1)
    as_of: AwareDatetime
    scenarios: list[PortfolioStressScenarioInput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenarios(self) -> PortfolioStressRequest:
        identities = [item.scenario for item in self.scenarios]
        if identities != sorted(set(identities), key=lambda item: item.value):
            raise ValueError("stress scenarios must be sorted and unique")
        return self


class PortfolioStressResult(AStockModel):
    scenario: EconomicStressScenario
    weighted_return_shock: float = Field(ge=-1, le=1, allow_inf_nan=False)
    stressed_nav_fraction: float = Field(ge=0, le=2, allow_inf_nan=False)
    largest_asset_contribution: float = Field(allow_inf_nan=False)
    uncovered_company_ids: list[str]
    rationale_codes: list[str]


class PortfolioStressReport(AStockModel):
    schema_version: str = "portfolio-stress-report-v1"
    report_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    results: list[PortfolioStressResult]
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    scenario_probabilities_assigned: Literal[False] = False
    alpha_signal_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


class AttributionComponent(StrEnum):
    STOCK_SELECTION = "STOCK_SELECTION"
    SECTOR = "SECTOR"
    COMPACT_FACTOR = "COMPACT_FACTOR"
    TIMING = "TIMING"
    IMPLEMENTATION_COST = "IMPLEMENTATION_COST"


class AttributionResearchLink(AStockModel):
    research_memo_id: str | None = None
    skill_ids: list[str] = Field(default_factory=list)
    specialist_delta_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_links(self) -> AttributionResearchLink:
        for values in (self.skill_ids, self.specialist_delta_ids):
            if values != sorted(set(values)):
                raise ValueError("attribution research links must be sorted and unique")
        return self


class AssetAttributionInput(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    beginning_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    realized_return: float = Field(ge=-1, le=10, allow_inf_nan=False)
    benchmark_return: float = Field(ge=-1, le=10, allow_inf_nan=False)
    sector_contribution: float = Field(allow_inf_nan=False)
    compact_factor_contribution: float = Field(allow_inf_nan=False)
    timing_contribution: float = Field(allow_inf_nan=False)
    implementation_cost_return: float = Field(ge=0, le=1, allow_inf_nan=False)
    research_links: AttributionResearchLink = Field(default_factory=AttributionResearchLink)


class PortfolioAttributionRequest(AStockModel):
    schema_version: str = "portfolio-attribution-request-v1"
    portfolio_id: str = Field(min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime
    assets: list[AssetAttributionInput] = Field(min_length=1)
    source_artifact_ids: list[str] = Field(default_factory=list)
    source_object_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_request(self) -> PortfolioAttributionRequest:
        if self.period_end <= self.period_start:
            raise ValueError("attribution period end must follow its start")
        ids = [item.company_id for item in self.assets]
        if ids != sorted(set(ids)):
            raise ValueError("attribution assets must be sorted and unique")
        if sum(item.beginning_weight for item in self.assets) > 1.0000001:
            raise ValueError("attribution beginning weights cannot exceed 100%")
        for values in (self.source_artifact_ids, self.source_object_hashes):
            if values != sorted(set(values)):
                raise ValueError("attribution provenance must be sorted and unique")
        return self


class AssetAttributionResult(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    realized_excess_contribution: float = Field(allow_inf_nan=False)
    components: dict[AttributionComponent, float]
    residual: float = Field(allow_inf_nan=False)
    research_links: AttributionResearchLink

    @model_validator(mode="after")
    def validate_components(self) -> AssetAttributionResult:
        if set(self.components) != set(AttributionComponent):
            raise ValueError("attribution result must contain every component")
        return self


class ResearchAttributionSummary(AStockModel):
    research_memo_contributions: dict[str, float]
    skill_contributions: dict[str, float]
    specialist_delta_contributions: dict[str, float]


class PortfolioAttributionReport(AStockModel):
    schema_version: str = "portfolio-attribution-report-v1"
    report_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime
    assets: list[AssetAttributionResult]
    component_totals: dict[AttributionComponent, float]
    realized_excess_return: float = Field(allow_inf_nan=False)
    total_residual: float = Field(allow_inf_nan=False)
    research_attribution: ResearchAttributionSummary
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    causal_credit_claimed: Literal[False] = False
    automatic_skill_modification_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "AssetAttributionInput",
    "AssetAttributionResult",
    "AssetStressShock",
    "AttributionComponent",
    "AttributionResearchLink",
    "CompactFactorExposure",
    "CompactRiskFactor",
    "EconomicStressScenario",
    "LiquidityImplementationEstimate",
    "PortfolioAssetRiskExplanation",
    "PortfolioAttributionReport",
    "PortfolioAttributionRequest",
    "PortfolioFundamentalFactorInput",
    "PortfolioRiskExplanationReport",
    "PortfolioRiskExplanationRequest",
    "PortfolioStressReport",
    "PortfolioStressRequest",
    "PortfolioStressResult",
    "PortfolioStressScenarioInput",
    "ResearchAttributionSummary",
    "RiskExposureProvenance",
]
