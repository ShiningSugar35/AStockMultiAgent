"""Strict portfolio analysis and construction contracts."""

from __future__ import annotations

from enum import StrEnum
from math import isfinite
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import Market


class PortfolioAnalysisStatus(StrEnum):
    READY = "READY"
    NEEDS_INFO = "NEEDS_INFO"
    EMPTY = "EMPTY"


class PortfolioAllocationMethod(StrEnum):
    EQUAL_WEIGHT_CONSTRAINED = "EQUAL_WEIGHT_CONSTRAINED"
    INVERSE_VOLATILITY = "INVERSE_VOLATILITY"
    HIERARCHICAL_RISK = "HIERARCHICAL_RISK"
    SHRINKAGE_MIN_VARIANCE = "SHRINKAGE_MIN_VARIANCE"


class PortfolioHoldingInput(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    weight: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    industry_tag: str | None = None


class PortfolioAnalysisRequest(AStockModel):
    schema_version: str = "portfolio-analysis-request-v1"
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    account_id: str | None = None
    holdings: list[PortfolioHoldingInput] = Field(default_factory=list)
    benchmark_symbol: str = Field(default="000300", pattern=r"^\d{6}$")
    benchmark_market: Market = Market.INDEX
    lookback_sessions: int = Field(default=120, ge=60, le=504)
    minimum_common_sessions: int = Field(default=60, ge=40, le=252)
    live: bool = False

    @model_validator(mode="after")
    def validate_source(self) -> PortfolioAnalysisRequest:
        if bool(self.account_id) == bool(self.holdings):
            raise ValueError("portfolio analysis requires exactly one account_id or holdings list")
        if self.holdings:
            ids = [item.company_id for item in self.holdings]
            if len(ids) != len(set(ids)):
                raise ValueError("portfolio holdings must be unique by company")
            if any(item.weight is None for item in self.holdings):
                raise ValueError("explicit portfolio holdings require weights")
            if sum(float(item.weight or 0) for item in self.holdings) > 1.0000001:
                raise ValueError("explicit portfolio weights cannot exceed 100%")
        if self.minimum_common_sessions > self.lookback_sessions:
            raise ValueError("minimum common sessions cannot exceed the lookback")
        return self


class PortfolioAssetRisk(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    market: Market
    weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    latest_close_fen: int = Field(gt=0)
    observation_count: int = Field(ge=2)
    annualized_volatility: float = Field(ge=0, allow_inf_nan=False)
    beta_to_benchmark: float = Field(allow_inf_nan=False)
    risk_contribution_fraction: float = Field(allow_inf_nan=False)
    max_abs_pair_correlation: float = Field(ge=0, le=1, allow_inf_nan=False)
    industry_tag: str | None = None
    daily_release_id: str = Field(min_length=1)
    daily_release_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PortfolioRiskMetrics(AStockModel):
    invested_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    cash_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    constant_weight_historical_annualized_return: float = Field(allow_inf_nan=False)
    annualized_volatility: float = Field(ge=0, allow_inf_nan=False)
    annualized_downside_deviation: float = Field(ge=0, allow_inf_nan=False)
    beta_to_benchmark: float = Field(allow_inf_nan=False)
    annualized_tracking_error: float = Field(ge=0, allow_inf_nan=False)
    max_drawdown: float = Field(ge=0, le=1, allow_inf_nan=False)
    historical_var_95: float = Field(ge=0, allow_inf_nan=False)
    historical_cvar_95: float = Field(ge=0, allow_inf_nan=False)
    historical_cdar_95: float = Field(ge=0, le=1, allow_inf_nan=False)
    concentration_hhi: float = Field(ge=0, le=1, allow_inf_nan=False)
    effective_number_of_positions: float = Field(ge=0, allow_inf_nan=False)
    max_abs_pair_correlation: float = Field(ge=0, le=1, allow_inf_nan=False)
    top_risk_contribution_fraction: float = Field(ge=0, allow_inf_nan=False)
    industry_exposures: dict[str, float]


class PortfolioAnalysisReport(AStockModel):
    schema_version: str = "portfolio-analysis-report-v1"
    report_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    data_cutoff_at: AwareDatetime
    status: PortfolioAnalysisStatus
    common_session_count: int = Field(ge=0)
    assets: list[PortfolioAssetRisk]
    metrics: PortfolioRiskMetrics | None = None
    benchmark_release_id: str | None = None
    benchmark_release_object_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warning_codes: list[str]
    hard_breach_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    paper_ledger_write_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> PortfolioAnalysisReport:
        for label, values in (
            ("warnings", self.warning_codes),
            ("hard breaches", self.hard_breach_codes),
            ("source artifacts", self.source_artifact_ids),
            ("source hashes", self.source_object_hashes),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"portfolio report {label} must be sorted and unique")
        if self.status is PortfolioAnalysisStatus.READY:
            if self.metrics is None or not self.assets or self.common_session_count < 2:
                raise ValueError("READY portfolio analysis requires metrics and assets")
        elif self.status is PortfolioAnalysisStatus.EMPTY:
            if self.assets or self.metrics is not None:
                raise ValueError("EMPTY portfolio analysis cannot carry assets or metrics")
        return self


class PortfolioCandidateInput(AStockModel):
    company_id: str = Field(pattern=r"^\d{6}$")
    classified_protocol_artifact_id: str = Field(min_length=1)
    risk_group: str = Field(min_length=1)



class PortfolioAllocationConstraints(AStockModel):
    """Explicit research assumptions; these are not a verified account projection."""
    current_weights: dict[str, float] = Field(default_factory=dict)
    can_buy: dict[str, bool] = Field(default_factory=dict)
    can_sell: dict[str, bool] = Field(default_factory=dict)
    one_way_costs: dict[str, float] = Field(default_factory=dict)
    additional_groups: dict[str, list[str]] = Field(default_factory=dict)
    group_caps: dict[str, float] = Field(default_factory=dict)
    min_cash: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    max_turnover: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_constraints(self) -> PortfolioAllocationConstraints:
        for mapping in (self.current_weights, self.one_way_costs, self.group_caps):
            if any(not key.strip() or not isfinite(value) or not 0 <= value <= 1
                   for key, value in mapping.items()):
                raise ValueError("allocation assumptions require finite fractions")
        if sum(self.current_weights.values()) > 1.00000001:
            raise ValueError("current allocation exceeds NAV")
        for groups in self.additional_groups.values():
            if (len(set(groups)) != len(groups)
                    or any(group not in self.group_caps for group in groups)):
                raise ValueError("additional groups require unique names and explicit caps")
        return self


class PortfolioConstructionRequest(AStockModel):
    schema_version: str = "portfolio-construction-request-v1"
    allocation_constraints: PortfolioAllocationConstraints | None = None
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    candidates: list[PortfolioCandidateInput] = Field(min_length=2, max_length=20)
    benchmark_symbol: str = Field(default="000300", pattern=r"^\d{6}$")
    benchmark_market: Market = Market.INDEX
    lookback_sessions: int = Field(default=120, ge=60, le=504)
    minimum_common_sessions: int = Field(default=60, ge=40, le=252)
    target_total_exposure: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    live: bool = False

    @model_validator(mode="after")
    def validate_candidates(self) -> PortfolioConstructionRequest:
        ids = [item.company_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("portfolio construction candidates must be unique")
        if self.minimum_common_sessions > self.lookback_sessions:
            raise ValueError("minimum common sessions cannot exceed the lookback")
        return self


class PortfolioAllocationProposal(AStockModel):
    method: PortfolioAllocationMethod
    weights: dict[str, float]
    cash_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    ex_ante_annualized_volatility: float = Field(ge=0, allow_inf_nan=False)
    concentration_hhi: float = Field(ge=0, le=1, allow_inf_nan=False)
    max_single_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    max_group_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    binding_constraint_codes: list[str]
    model_risk_codes: list[str]
    allocator_contract_version: str = "legacy-score-v1"
    solver_status: str = "LEGACY_SCORE_PROJECTION"
    optimality_claim: bool = False
    constraint_tolerance: float = Field(default=1e-8, gt=0, allow_inf_nan=False)
    one_off_cost_fraction: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    turnover_sum_abs_weights: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    target_exposure_deviation: float | None = Field(default=None, allow_inf_nan=False)
    return_basis: str = "UNSPECIFIED"

    @model_validator(mode="after")
    def validate_proposal(self) -> PortfolioAllocationProposal:
        if set(self.weights) == {""} or any(not key for key in self.weights):
            raise ValueError("portfolio proposal symbols cannot be blank")
        if any(not isfinite(value) or value < 0 or value > 1 for value in self.weights.values()):
            raise ValueError("portfolio proposal weights must be within 0..1")
        if sum(self.weights.values()) + self.cash_weight + self.one_off_cost_fraction > 1.0000001:
            raise ValueError("portfolio proposal total allocation cannot exceed 100%")
        if self.optimality_claim and self.solver_status != "OPTIMAL_RESEARCH":
            raise ValueError("postprocessed or heuristic weights cannot retain optimality")
        for values in (self.binding_constraint_codes, self.model_risk_codes):
            if values != sorted(set(values)):
                raise ValueError("portfolio proposal codes must be sorted and unique")
        return self


class PortfolioConstructionReport(AStockModel):
    schema_version: str = "portfolio-construction-report-v1"
    report_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    as_of: AwareDatetime
    data_cutoff_at: AwareDatetime
    status: PortfolioAnalysisStatus
    default_method: PortfolioAllocationMethod = PortfolioAllocationMethod.EQUAL_WEIGHT_CONSTRAINED
    proposals: list[PortfolioAllocationProposal]
    admitted_company_ids: list[str]
    rejected_company_ids: list[str]
    common_session_count: int = Field(ge=0)
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]
    requires_user_confirmation: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_construction(self) -> PortfolioConstructionReport:
        if self.admitted_company_ids != sorted(set(self.admitted_company_ids)):
            raise ValueError("admitted companies must be sorted and unique")
        if self.rejected_company_ids != sorted(set(self.rejected_company_ids)):
            raise ValueError("rejected companies must be sorted and unique")
        if set(self.admitted_company_ids) & set(self.rejected_company_ids):
            raise ValueError("admitted and rejected portfolio companies cannot overlap")
        for values in (
            self.warning_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("portfolio construction lists must be sorted and unique")
        if self.status is PortfolioAnalysisStatus.READY and len(self.proposals) != len(
            PortfolioAllocationMethod
        ):
            raise ValueError("READY construction requires every portfolio method proposal")
        return self


__all__ = [
    "PortfolioAllocationConstraints",
    "PortfolioAllocationMethod",
    "PortfolioAllocationProposal",
    "PortfolioAnalysisReport",
    "PortfolioAnalysisRequest",
    "PortfolioAnalysisStatus",
    "PortfolioAssetRisk",
    "PortfolioCandidateInput",
    "PortfolioConstructionReport",
    "PortfolioConstructionRequest",
    "PortfolioHoldingInput",
    "PortfolioRiskMetrics",
]
