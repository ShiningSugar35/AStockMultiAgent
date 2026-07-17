"""Strict inputs and artifacts for deterministic research diagnostics."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import Frequency, QualityStatus
from astock.schemas.research import BASE_CASE_SECTIONS, BaseCaseSection, SpecialistCoverageStatus


class DiagnosticStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class DiagnosticDirection(StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    MIXED = "MIXED"


class IndustryDiagnosticRules(AStockModel):
    max_substitutability_ratio: Decimal = Field(ge=0, le=1)


class ValuationDiagnosticRules(AStockModel):
    neutral_growth_gap: Decimal = Field(ge=0, le=1)


class TrendDiagnosticRules(AStockModel):
    frequency: Frequency
    minimum_bars: int = Field(ge=1)
    drawdown_alert: Decimal = Field(ge=-1, le=0)
    volatility_alert: Decimal | None = Field(default=None, ge=0, le=10)
    positive_min_score: int
    negative_max_score: int

    @model_validator(mode="after")
    def validate_score_bounds(self) -> TrendDiagnosticRules:
        if self.negative_max_score >= self.positive_min_score:
            raise ValueError("negative trend score bound must be below positive bound")
        return self


class ResearchDiagnosticConfig(AStockModel):
    diagnostics_version: str = Field(min_length=1)
    industry: IndustryDiagnosticRules
    valuation: ValuationDiagnosticRules
    daily: TrendDiagnosticRules
    hourly: TrendDiagnosticRules

    @model_validator(mode="after")
    def validate_independent_frequencies(self) -> ResearchDiagnosticConfig:
        if self.daily.frequency is not Frequency.D1:
            raise ValueError("daily diagnostic rules must use 1d")
        if self.hourly.frequency is not Frequency.H1:
            raise ValueError("hourly diagnostic rules must use 60m")
        if self.daily == self.hourly:
            raise ValueError("daily and hourly diagnostic rules must be independent")
        return self


class _DiagnosticRequestBase(AStockModel):
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)


class IndustryBottleneckDiagnosticRequest(_DiagnosticRequestBase):
    skill_id: Literal["IndustryBottleneckSkill"] = "IndustryBottleneckSkill"
    skill_version: Literal["industry-bottleneck-v1"] = "industry-bottleneck-v1"
    system_change_verified: bool
    system_change_evidence_ids: list[str]
    necessary_link_verified: bool
    necessary_link_evidence_ids: list[str]
    scarcity_verified: bool
    scarcity_evidence_ids: list[str]
    substitutability_ratio: Decimal = Field(ge=0, le=1)
    substitutability_evidence_ids: list[str]
    value_capture_verified: bool
    value_capture_evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_industry_evidence(self) -> IndustryBottleneckDiagnosticRequest:
        evidence_sets = (
            (self.system_change_verified, self.system_change_evidence_ids),
            (self.necessary_link_verified, self.necessary_link_evidence_ids),
            (self.scarcity_verified, self.scarcity_evidence_ids),
            (True, self.substitutability_evidence_ids),
            (self.value_capture_verified, self.value_capture_evidence_ids),
        )
        for verified, evidence_ids in evidence_sets:
            _require_unique(evidence_ids, "industry diagnostic evidence")
            if verified and not evidence_ids:
                raise ValueError("verified industry chain layers require evidence")
        return self


class EventToAlphaDiagnosticRequest(_DiagnosticRequestBase):
    skill_id: Literal["EventToAlphaSkill"] = "EventToAlphaSkill"
    skill_version: Literal["event-to-alpha-v1"] = "event-to-alpha-v1"
    event_verified: bool
    headline_only: bool = False
    event_evidence_ids: list[str]
    operating_metric: str | None = None
    operating_direction: DiagnosticDirection | None = None
    financial_metric: str | None = None
    financial_direction: DiagnosticDirection | None = None
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    transmission_evidence_ids: list[str]
    falsifier: str | None = None
    falsifier_evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_event_shape(self) -> EventToAlphaDiagnosticRequest:
        for evidence_ids in (
            self.event_evidence_ids,
            self.transmission_evidence_ids,
            self.falsifier_evidence_ids,
        ):
            _require_unique(evidence_ids, "event diagnostic evidence")
        if self.event_verified and not self.event_evidence_ids:
            raise ValueError("verified events require evidence")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("event diagnostic windows require both endpoints")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end < self.window_start
        ):
            raise ValueError("event diagnostic window end cannot precede start")
        return self


class GrowthScenario(AStockModel):
    scenario_id: str = Field(min_length=1)
    probability: Decimal = Field(ge=0, le=1)
    annual_growth_rate: Decimal = Field(ge=-1, le=10)
    duration_years: int = Field(ge=1, le=10)
    driver: str = Field(min_length=1)
    failure_condition: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario_evidence(self) -> GrowthScenario:
        _require_unique(self.evidence_ids, "growth scenario evidence")
        return self


class GrowthProbabilityDiagnosticRequest(_DiagnosticRequestBase):
    skill_id: Literal["GrowthProbabilitySkill"] = "GrowthProbabilitySkill"
    skill_version: Literal["growth-probability-v1"] = "growth-probability-v1"
    scenarios: list[GrowthScenario] = Field(min_length=2, max_length=5)
    consensus_available: bool
    consensus_growth_rate: Decimal | None = Field(default=None, ge=-1, le=10)
    consensus_evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_probability_conservation(self) -> GrowthProbabilityDiagnosticRequest:
        scenario_ids = [item.scenario_id for item in self.scenarios]
        _require_unique(scenario_ids, "growth scenario ids")
        if sum((item.probability for item in self.scenarios), Decimal("0")) != Decimal(
            "1"
        ):
            raise ValueError("growth scenario probabilities must sum exactly to one")
        _require_unique(self.consensus_evidence_ids, "growth consensus evidence")
        if self.consensus_available and (
            self.consensus_growth_rate is None or not self.consensus_evidence_ids
        ):
            raise ValueError("available growth consensus requires a value and evidence")
        if not self.consensus_available and (
            self.consensus_growth_rate is not None or self.consensus_evidence_ids
        ):
            raise ValueError("unavailable growth consensus cannot carry a value or evidence")
        return self


class GrowthValuationDiagnosticRequest(_DiagnosticRequestBase):
    skill_id: Literal["GrowthValuationLens"] = "GrowthValuationLens"
    skill_version: Literal["growth-valuation-v1"] = "growth-valuation-v1"
    market_implied_growth_rate: Decimal = Field(ge=-1, le=10)
    research_growth_rate: Decimal = Field(ge=-1, le=10)
    dilution_rate: Decimal = Field(ge=-1, le=1)
    reinvestment_rate: Decimal = Field(ge=0, le=2)
    valuation_evidence_ids: list[str] = Field(min_length=1)
    consensus_available: bool
    consensus_growth_rate: Decimal | None = Field(default=None, ge=-1, le=10)
    consensus_evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_valuation_evidence(self) -> GrowthValuationDiagnosticRequest:
        _require_unique(self.valuation_evidence_ids, "valuation evidence")
        _require_unique(self.consensus_evidence_ids, "valuation consensus evidence")
        if self.consensus_available and (
            self.consensus_growth_rate is None or not self.consensus_evidence_ids
        ):
            raise ValueError("available valuation consensus requires a value and evidence")
        if not self.consensus_available and (
            self.consensus_growth_rate is not None or self.consensus_evidence_ids
        ):
            raise ValueError("unavailable valuation consensus cannot carry a value or evidence")
        return self


class DailyTrendDiagnosticRequest(_DiagnosticRequestBase):
    skill_id: Literal["DailyTrendHealthSkill"] = "DailyTrendHealthSkill"
    skill_version: Literal["daily-trend-health-v1"] = "daily-trend-health-v1"
    frequency: Literal[Frequency.D1] = Frequency.D1
    quality_report_id: str = Field(min_length=1)
    quality_status: QualityStatus
    bar_count: int = Field(ge=0)
    close_vs_ma20: Decimal = Field(ge=-1, le=10)
    ma20_slope: Decimal = Field(ge=-1, le=10)
    ma60_slope: Decimal = Field(ge=-1, le=10)
    drawdown_from_60d_high: Decimal = Field(ge=-1, le=0)
    volume_ratio_20d: Decimal = Field(ge=0, le=100)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_daily_evidence(self) -> DailyTrendDiagnosticRequest:
        _require_unique(self.evidence_ids, "daily trend evidence")
        return self


class HourlySwingDiagnosticRequest(_DiagnosticRequestBase):
    skill_id: Literal["HourlySwingSkill"] = "HourlySwingSkill"
    skill_version: Literal["hourly-swing-v1"] = "hourly-swing-v1"
    frequency: Literal[Frequency.H1] = Frequency.H1
    quality_report_id: str = Field(min_length=1)
    quality_status: QualityStatus
    bar_count: int = Field(ge=0)
    close_vs_vwap_20h: Decimal = Field(ge=-1, le=10)
    ema12_slope: Decimal = Field(ge=-1, le=10)
    realized_volatility_20h: Decimal = Field(ge=0, le=10)
    drawdown_10h: Decimal = Field(ge=-1, le=0)
    volume_ratio_20h: Decimal = Field(ge=0, le=100)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hourly_evidence(self) -> HourlySwingDiagnosticRequest:
        _require_unique(self.evidence_ids, "hourly swing evidence")
        return self


SpecialistDiagnosticRequest = Annotated[
    IndustryBottleneckDiagnosticRequest
    | EventToAlphaDiagnosticRequest
    | GrowthProbabilityDiagnosticRequest
    | GrowthValuationDiagnosticRequest
    | DailyTrendDiagnosticRequest
    | HourlySwingDiagnosticRequest,
    Field(discriminator="skill_id"),
]


class SpecialistDiagnosticReport(AStockModel):
    diagnostic_id: str = Field(min_length=1)
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    delta_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    diagnostics_version: str = Field(min_length=1)
    status: DiagnosticStatus
    signal_codes: list[str] = Field(min_length=1)
    degradation_codes: list[str]
    metric_names: list[str]
    evidence_request_codes: list[str]
    evidence_ids: list[str]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_sets(self) -> SpecialistDiagnosticReport:
        for label, values in (
            ("signal code", self.signal_codes),
            ("degradation code", self.degradation_codes),
            ("metric name", self.metric_names),
            ("evidence request code", self.evidence_request_codes),
            ("evidence", self.evidence_ids),
        ):
            _require_unique(values, f"diagnostic {label}")
        return self


class ResearchMemoComposeRequest(AStockModel):
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    delta_ids: list[str]

    @model_validator(mode="after")
    def validate_delta_ids(self) -> ResearchMemoComposeRequest:
        _require_unique(self.delta_ids, "memo delta ids")
        return self


class ResearchMemoSectionReference(AStockModel):
    section: BaseCaseSection
    base_finding_ids: list[str]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_section_references(self) -> ResearchMemoSectionReference:
        _require_unique(self.base_finding_ids, "memo base finding ids")
        _require_unique(self.evidence_ids, "memo section evidence ids")
        return self


class ResearchMemoDeltaReference(AStockModel):
    delta_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    incremental_finding_ids: list[str]
    correction_ids: list[str]
    metric_ids: list[str]
    adjustment_ids: list[str]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_delta_references(self) -> ResearchMemoDeltaReference:
        for label, values in (
            ("incremental finding", self.incremental_finding_ids),
            ("correction", self.correction_ids),
            ("metric", self.metric_ids),
            ("adjustment", self.adjustment_ids),
            ("evidence", self.evidence_ids),
        ):
            _require_unique(values, f"memo delta {label} ids")
        return self


class ResearchMemoArtifact(AStockModel):
    memo_id: str = Field(min_length=1)
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    registry_version: str = Field(min_length=1)
    base_sections: list[ResearchMemoSectionReference]
    delta_references: list[ResearchMemoDeltaReference]
    missing_selected_skill_ids: list[str]
    open_gap_codes: list[str]
    coverage_status: SpecialistCoverageStatus
    confidence_cap: float = Field(ge=0, le=1)
    degradation_codes: list[str]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_memo_conservation(self) -> ResearchMemoArtifact:
        sections = [item.section for item in self.base_sections]
        if len(sections) != len(set(sections)) or set(sections) != set(BASE_CASE_SECTIONS):
            raise ValueError("research memo must reference every BaseCase section exactly once")
        delta_ids = [item.delta_id for item in self.delta_references]
        _require_unique(delta_ids, "memo delta references")
        for label, values in (
            ("missing selected Skill", self.missing_selected_skill_ids),
            ("open gap code", self.open_gap_codes),
            ("degradation code", self.degradation_codes),
            ("evidence", self.evidence_ids),
        ):
            _require_unique(values, f"memo {label}")
        expected_evidence = sorted(
            {
                evidence_id
                for item in (*self.base_sections, *self.delta_references)
                for evidence_id in item.evidence_ids
            }
        )
        if self.evidence_ids != expected_evidence:
            raise ValueError("research memo evidence ids must equal the reference union")
        return self


def _require_unique(values: Sequence[Hashable], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


__all__ = [
    "DailyTrendDiagnosticRequest",
    "DiagnosticDirection",
    "DiagnosticStatus",
    "EventToAlphaDiagnosticRequest",
    "GrowthProbabilityDiagnosticRequest",
    "GrowthScenario",
    "GrowthValuationDiagnosticRequest",
    "HourlySwingDiagnosticRequest",
    "IndustryBottleneckDiagnosticRequest",
    "IndustryDiagnosticRules",
    "ResearchDiagnosticConfig",
    "ResearchMemoArtifact",
    "ResearchMemoComposeRequest",
    "ResearchMemoDeltaReference",
    "ResearchMemoSectionReference",
    "SpecialistDiagnosticReport",
    "SpecialistDiagnosticRequest",
    "TrendDiagnosticRules",
    "ValuationDiagnosticRules",
]
