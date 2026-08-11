"""Strict, report-only Serenity method contracts for the active v2 research registry."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import AdjustmentMode, Frequency


class SerenityContractKind(StrEnum):
    INDUSTRY_BOTTLENECK = "INDUSTRY_BOTTLENECK"
    EVENT_TO_ALPHA = "EVENT_TO_ALPHA"
    GROWTH_PROBABILITY = "GROWTH_PROBABILITY"
    GROWTH_VALUATION = "GROWTH_VALUATION"
    DAILY_TREND_HEALTH = "DAILY_TREND_HEALTH"
    JUGLAR_CYCLE_STAGE = "JUGLAR_CYCLE_STAGE"


class JuglarStage(StrEnum):
    RECOVERY = "STAGE_1_RECOVERY"
    EXPANSION = "STAGE_2_EXPANSION"
    OVERHEATING = "STAGE_3_OVERHEATING"
    DOWNTURN = "STAGE_4_DOWNTURN"
    CLEARING = "STAGE_5_CLEARING"


class JuglarCycleDimension(StrEnum):
    DEMAND = "DEMAND"
    ASP = "ASP"
    MARGIN = "MARGIN"
    CAPEX = "CAPEX"
    INVENTORY = "INVENTORY"
    CAPACITY_RELEASE = "CAPACITY_RELEASE"
    CUSTOMER_BEHAVIOR = "CUSTOMER_BEHAVIOR"
    CAPITAL_MARKET_REACTION = "CAPITAL_MARKET_REACTION"


class GrowthHypothesisId(StrEnum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    H4 = "H4"
    H5 = "H5"


class MetricDirection(StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    MIXED = "MIXED"


class FinancialMetricV2(StrEnum):
    REVENUE = "revenue"
    GROSS_PROFIT = "gross_profit"
    OPERATING_PROFIT = "operating_profit"
    NET_INCOME = "net_income"
    EPS = "eps"
    FREE_CASH_FLOW = "free_cash_flow"


class Comparator(StrEnum):
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"
    GTE = "GTE"
    GT = "GT"


class MemoScenarioCase(StrEnum):
    BULL = "BULL"
    BASE = "BASE"
    BEAR = "BEAR"


class QualityCalibrationStatus(StrEnum):
    REPORT_ONLY_UNCALIBRATED = "REPORT_ONLY_UNCALIBRATED"
    A_SHARE_CALIBRATED = "A_SHARE_CALIBRATED"


class ValuationApplicability(StrEnum):
    APPLICABLE = "APPLICABLE"
    REPORT_ONLY = "REPORT_ONLY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CurrencyScale(StrEnum):
    ONES = "ONES"
    THOUSANDS = "THOUSANDS"
    MILLIONS = "MILLIONS"
    BILLIONS = "BILLIONS"


class _EvidenceNode(AStockModel):
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> _EvidenceNode:
        _unique(self.evidence_ids, "method node evidence")
        return self


class SystemChangeV2(_EvidenceNode):
    node_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    effective_at: AwareDatetime


class IndustryChainNodeV2(_EvidenceNode):
    node_id: str = Field(min_length=1)
    parent_node_id: str | None = None
    level: int = Field(ge=0)
    name: str = Field(min_length=1)


class CandidateUniverseV2(_EvidenceNode):
    universe_id: str = Field(min_length=1)
    inclusion_rule: str = Field(min_length=1)
    member_company_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_members(self) -> CandidateUniverseV2:
        _unique(self.member_company_ids, "candidate universe members")
        return self


class NecessaryLinkV2(_EvidenceNode):
    node_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class ScarcityMetricV2(_EvidenceNode):
    metric: str = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1)
    measurement_at: AwareDatetime


class SubstitutionAlternativeV2(_EvidenceNode):
    alternative_id: str = Field(min_length=1)
    feasibility: bool
    time_to_substitute_months: int | None = Field(default=None, ge=0)
    cost_ratio: Decimal | None = Field(default=None, ge=0)


class ValueCaptureV2(_EvidenceNode):
    company_id: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    metric: str | None = None
    value: Decimal | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def validate_optional_metric(self) -> ValueCaptureV2:
        supplied = (self.metric is not None, self.value is not None, self.unit is not None)
        if any(supplied) and not all(supplied):
            raise ValueError("value capture metric, value and unit must appear together")
        return self


class ObservableInvalidationV2(_EvidenceNode):
    invalidation_id: str = Field(min_length=1)
    observable: str = Field(min_length=1)
    comparator: Comparator
    threshold: Decimal
    unit: str = Field(min_length=1)
    deadline: AwareDatetime


class IndustryBottleneckContractV2(AStockModel):
    contract_kind: Literal[SerenityContractKind.INDUSTRY_BOTTLENECK] = (
        SerenityContractKind.INDUSTRY_BOTTLENECK
    )
    contract_version: Literal["industry-bottleneck-v2"] = "industry-bottleneck-v2"
    target_company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    system_change: SystemChangeV2
    chain_nodes: list[IndustryChainNodeV2] = Field(min_length=1)
    candidate_universe: CandidateUniverseV2
    necessary_link: NecessaryLinkV2
    scarcity: list[ScarcityMetricV2] = Field(min_length=1)
    substitutions: list[SubstitutionAlternativeV2] = Field(min_length=1)
    aggregate_substitutability_ratio: Decimal = Field(ge=0, le=1)
    value_capture: list[ValueCaptureV2] = Field(min_length=1)
    invalidation_conditions: list[ObservableInvalidationV2] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_industry_contract(self) -> IndustryBottleneckContractV2:
        node_ids = [item.node_id for item in self.chain_nodes]
        _unique(node_ids, "industry chain node ids")
        known = set(node_ids)
        roots = [item for item in self.chain_nodes if item.parent_node_id is None]
        if len(roots) != 1:
            raise ValueError("industry chain requires exactly one root")
        if roots[0].level != 0:
            raise ValueError("industry chain root must be level zero")
        if any(
            item.parent_node_id is not None and item.parent_node_id not in known
            for item in self.chain_nodes
        ):
            raise ValueError("industry chain parent must resolve")
        by_id = {item.node_id: item for item in self.chain_nodes}
        if any(
            item.parent_node_id is not None and item.level != by_id[item.parent_node_id].level + 1
            for item in self.chain_nodes
        ):
            raise ValueError("industry chain child level must equal parent level plus one")
        for node in self.chain_nodes:
            visited: set[str] = set()
            current = node
            while current.parent_node_id is not None:
                if current.node_id in visited:
                    raise ValueError("industry chain cannot contain a cycle")
                visited.add(current.node_id)
                current = by_id[current.parent_node_id]
        if self.necessary_link.node_id not in known:
            raise ValueError("necessary link must reference a chain node")
        if self.target_company_id not in self.candidate_universe.member_company_ids:
            raise ValueError("target company must belong to the frozen universe")
        if self.target_company_id not in {item.company_id for item in self.value_capture}:
            raise ValueError("value capture must cover the target company")
        if self.system_change.effective_at > self.as_of:
            raise ValueError("industry system change cannot be future relative to as_of")
        if any(item.measurement_at > self.as_of for item in self.scarcity):
            raise ValueError("industry scarcity measurement cannot be future relative to as_of")
        _unique(
            [item.invalidation_id for item in self.invalidation_conditions],
            "industry invalidation conditions",
        )
        _evidence_union_matches(self)
        return self


class EventFactV2(_EvidenceNode):
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    announced_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    demand_metric: str = Field(min_length=1)
    direction: MetricDirection


class BusinessPurityV2(_EvidenceNode):
    metric: str = Field(min_length=1)
    value: Decimal = Field(ge=0, le=1)
    unit: Literal["ratio"] = "ratio"
    period: str = Field(min_length=1)


class TransmissionStepV2(_EvidenceNode):
    step_no: int = Field(ge=1)
    from_metric: str = Field(min_length=1)
    to_metric: str = Field(min_length=1)
    direction: MetricDirection
    lag_quarters: int = Field(ge=1, le=4)


class ScaleElasticityV2(_EvidenceNode):
    input_metric: str = Field(min_length=1)
    output_metric: str = Field(min_length=1)
    value: Decimal | None = None
    unit: Literal["output_ratio_per_input_ratio"] = "output_ratio_per_input_ratio"


class MarketMisclassificationV2(_EvidenceNode):
    market_implied_metric: str = Field(min_length=1)
    market_implied_value: Decimal
    research_metric: str = Field(min_length=1)
    research_value: Decimal
    unit: str = Field(min_length=1)
    observed_at: AwareDatetime


class ValidationCheckpointV2(_EvidenceNode):
    quarter_offset: int = Field(ge=1, le=4)
    observable: str = Field(min_length=1)
    comparator: Comparator
    threshold: Decimal
    unit: str = Field(min_length=1)


class FalsifierV2(_EvidenceNode):
    observable: str = Field(min_length=1)
    comparator: Comparator
    threshold: Decimal
    unit: str = Field(min_length=1)
    deadline: AwareDatetime


class EventToAlphaContractV2(AStockModel):
    contract_kind: Literal[SerenityContractKind.EVENT_TO_ALPHA] = (
        SerenityContractKind.EVENT_TO_ALPHA
    )
    contract_version: Literal["event-to-alpha-v2"] = "event-to-alpha-v2"
    target_company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    event: EventFactV2
    business_purity: BusinessPurityV2
    transmission_steps: list[TransmissionStepV2] = Field(min_length=1)
    financial_endpoint: FinancialMetricV2
    scale_elasticity: ScaleElasticityV2
    market_misclassification: MarketMisclassificationV2 | None = None
    validation_checkpoints: list[ValidationCheckpointV2] = Field(min_length=1)
    falsifier: FalsifierV2
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_event_contract(self) -> EventToAlphaContractV2:
        steps = sorted(self.transmission_steps, key=lambda item: item.step_no)
        if [item.step_no for item in steps] != list(range(1, len(steps) + 1)):
            raise ValueError("event transmission step numbers must be continuous")
        if steps[0].from_metric != self.event.demand_metric:
            raise ValueError("event transmission must start from the demand metric")
        if any(
            left.to_metric != right.from_metric
            for left, right in zip(steps, steps[1:], strict=False)
        ):
            raise ValueError("event transmission steps must form one chain")
        if steps[-1].to_metric != self.financial_endpoint.value:
            raise ValueError("event transmission must end at the declared financial metric")
        if (
            self.scale_elasticity.input_metric != steps[0].from_metric
            or self.scale_elasticity.output_metric != steps[-1].to_metric
        ):
            raise ValueError("event elasticity must bind the transmission chain endpoints")
        offsets = [item.quarter_offset for item in self.validation_checkpoints]
        _unique(offsets, "event validation quarter offsets")
        if self.event.announced_at > self.as_of:
            raise ValueError("event announcement cannot be future relative to as_of")
        if (
            self.market_misclassification is not None
            and self.market_misclassification.observed_at > self.as_of
        ):
            raise ValueError("market observation cannot be future relative to as_of")
        _evidence_union_matches(self)
        return self


class GrowthHypothesisV2(_EvidenceNode):
    hypothesis_id: GrowthHypothesisId
    definition: str = Field(min_length=1)
    growth_lower: Decimal
    growth_upper: Decimal
    duration_years: int = Field(ge=1, le=10)
    drivers: list[str] = Field(min_length=1)
    failure_conditions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> GrowthHypothesisV2:
        if self.growth_upper <= self.growth_lower:
            raise ValueError("growth hypothesis upper bound must exceed lower bound")
        _unique(self.drivers, "growth drivers")
        _unique(self.failure_conditions, "growth failure conditions")
        return self


class GrowthPriorBasisV2(_EvidenceNode):
    population: str = Field(min_length=1)
    window: str = Field(min_length=1)


class GrowthLikelihoodUpdateV2(_EvidenceNode):
    update_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    correlation_group: str = Field(min_length=1)
    likelihood_by_hypothesis: dict[GrowthHypothesisId, Decimal]

    @model_validator(mode="after")
    def validate_likelihoods(self) -> GrowthLikelihoodUpdateV2:
        _probability_vector(self.likelihood_by_hypothesis, require_sum=False)
        return self


class GrowthConsensusV2(_EvidenceNode):
    growth_rate: Decimal
    duration_years: int = Field(ge=1, le=10)
    available_at: AwareDatetime


class GrowthProbabilityInputV2(AStockModel):
    target_company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    hypotheses: list[GrowthHypothesisV2] = Field(min_length=6, max_length=6)
    prior_by_hypothesis: dict[GrowthHypothesisId, Decimal]
    prior_basis: GrowthPriorBasisV2
    likelihood_updates: list[GrowthLikelihoodUpdateV2] = Field(min_length=1)
    consensus: GrowthConsensusV2 | None = None
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_growth_input(self) -> GrowthProbabilityInputV2:
        if {item.hypothesis_id for item in self.hypotheses} != set(GrowthHypothesisId):
            raise ValueError("growth hypotheses must cover H0 through H5")
        ordered = sorted(self.hypotheses, key=lambda item: item.hypothesis_id.value)
        if any(
            left.growth_upper != right.growth_lower
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            raise ValueError("growth hypothesis ranges must be continuous and non-overlapping")
        _probability_vector(self.prior_by_hypothesis, require_sum=True)
        sequences = [item.sequence for item in self.likelihood_updates]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("growth likelihood update sequence must be continuous")
        _unique(
            [item.correlation_group for item in self.likelihood_updates],
            "growth likelihood correlation groups",
        )
        evidence_signatures = [tuple(sorted(item.evidence_ids)) for item in self.likelihood_updates]
        _unique(evidence_signatures, "growth likelihood update evidence sets")
        seen_update_evidence: set[str] = set()
        for update in self.likelihood_updates:
            if seen_update_evidence.intersection(update.evidence_ids):
                raise ValueError(
                    "growth likelihood evidence cannot be counted in more than one update"
                )
            seen_update_evidence.update(update.evidence_ids)
        if self.consensus is not None and self.consensus.available_at > self.as_of:
            raise ValueError("growth consensus cannot be future relative to as_of")
        _evidence_union_matches(self)
        return self


class GrowthPosteriorStepV2(AStockModel):
    sequence: int = Field(ge=1)
    update_id: str = Field(min_length=1)
    prior: dict[GrowthHypothesisId, Decimal]
    likelihood: dict[GrowthHypothesisId, Decimal]
    posterior: dict[GrowthHypothesisId, Decimal]

    @model_validator(mode="after")
    def validate_step(self) -> GrowthPosteriorStepV2:
        _probability_vector(self.prior, require_sum=True)
        _probability_vector(self.likelihood, require_sum=False)
        _probability_vector(self.posterior, require_sum=True)
        return self


class GrowthProbabilityContractV2(AStockModel):
    contract_kind: Literal[SerenityContractKind.GROWTH_PROBABILITY] = (
        SerenityContractKind.GROWTH_PROBABILITY
    )
    contract_version: Literal["growth-probability-v2"] = "growth-probability-v2"
    input: GrowthProbabilityInputV2
    update_trajectory: list[GrowthPosteriorStepV2] = Field(min_length=1)
    final_posterior: dict[GrowthHypothesisId, Decimal]
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_growth_output(self) -> GrowthProbabilityContractV2:
        updates = self.input.likelihood_updates
        if len(self.update_trajectory) != len(updates):
            raise ValueError("growth trajectory must contain exactly one step per input update")
        expected_prior = self.input.prior_by_hypothesis
        for step, update in zip(self.update_trajectory, updates, strict=True):
            if step.sequence != update.sequence or step.update_id != update.update_id:
                raise ValueError("growth trajectory must preserve input update identity and order")
            if step.prior != expected_prior:
                raise ValueError(
                    "growth trajectory prior must continue from the previous posterior"
                )
            if step.likelihood != update.likelihood_by_hypothesis:
                raise ValueError("growth trajectory likelihood must equal its input update")
            expected_posterior = _normalized_bayesian_update(step.prior, step.likelihood)
            if step.posterior != expected_posterior:
                raise ValueError(
                    "growth trajectory posterior must equal normalized prior times likelihood"
                )
            expected_prior = step.posterior
        if self.update_trajectory[-1].posterior != self.final_posterior:
            raise ValueError("final posterior must equal the last trajectory step")
        _probability_vector(self.final_posterior, require_sum=True)
        if self.evidence_ids != self.input.evidence_ids:
            raise ValueError("growth output evidence must equal input evidence")
        return self


class TamRunwayV2(_EvidenceNode):
    tam_value: Decimal = Field(gt=0)
    current_revenue: Decimal = Field(ge=0)
    addressable_share: Decimal = Field(gt=0, le=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    scale: CurrencyScale
    measurement_at: AwareDatetime
    target_year: int = Field(ge=1900, le=2200)


class QualityFactorV2(_EvidenceNode):
    factor_id: Literal[
        "durability",
        "cash_conversion",
        "concentration",
        "capital_intensity",
        "dilution",
    ]
    raw_value: Decimal
    unit: str = Field(min_length=1)
    direction: MetricDirection
    calibration_status: QualityCalibrationStatus


class PegInputV2(_EvidenceNode):
    pe_multiple: Decimal
    earnings_basis: str = Field(min_length=1)
    earnings_period: str = Field(min_length=1)
    growth_value: Decimal
    growth_period: str = Field(min_length=1)
    growth_unit: Literal["percentage_points_per_year"] = "percentage_points_per_year"
    peg_unit: Literal["x_per_percentage_point"] = "x_per_percentage_point"

    @model_validator(mode="after")
    def validate_periods(self) -> PegInputV2:
        if self.earnings_period != self.growth_period:
            raise ValueError("PEG earnings and growth periods must match")
        return self


class GrowthValuationContractV2(AStockModel):
    contract_kind: Literal[SerenityContractKind.GROWTH_VALUATION] = (
        SerenityContractKind.GROWTH_VALUATION
    )
    contract_version: Literal["growth-valuation-v2"] = "growth-valuation-v2"
    target_company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    market_implied_growth_rate: Decimal
    research_growth_rate: Decimal
    dilution_rate: Decimal = Field(ge=-1, le=1)
    reinvestment_rate: Decimal = Field(ge=0, le=2)
    tam_runway: TamRunwayV2 | None = None
    quality_factors: list[QualityFactorV2]
    peg: PegInputV2 | None = None
    applicability: ValuationApplicability
    applicability_reasons: list[str] = Field(min_length=1)
    consensus: GrowthConsensusV2 | None = None
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_valuation_contract(self) -> GrowthValuationContractV2:
        factor_ids = [item.factor_id for item in self.quality_factors]
        _unique(factor_ids, "valuation quality factor ids")
        _unique(self.applicability_reasons, "valuation applicability reasons")
        if self.tam_runway is not None and self.tam_runway.measurement_at > self.as_of:
            raise ValueError("TAM measurement cannot be future relative to as_of")
        if self.consensus is not None and self.consensus.available_at > self.as_of:
            raise ValueError("valuation consensus cannot be future relative to as_of")
        if self.applicability is not ValuationApplicability.NOT_APPLICABLE and self.peg is None:
            raise ValueError("applicable or report-only valuation requires PEG inputs")
        _evidence_union_matches(self)
        return self


class DailySeriesV2(_EvidenceNode):
    symbol: str = Field(min_length=1)
    as_of: AwareDatetime
    frequency: Literal[Frequency.D1] = Frequency.D1
    quality_report_id: str = Field(min_length=1)
    bar_count: int = Field(ge=0)
    adjustment_mode: AdjustmentMode
    dataset_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class MovingAverageV2(_EvidenceNode):
    window: Literal[20, 50, 100, 200]
    value: Decimal
    close: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    calculated_at: AwareDatetime
    bars_used: int = Field(ge=20)
    dataset_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    calculation_status: Literal["CALLER_SUPPLIED_REPORT_ONLY"] = "CALLER_SUPPLIED_REPORT_ONLY"

    @model_validator(mode="after")
    def validate_window(self) -> MovingAverageV2:
        if self.bars_used < self.window:
            raise ValueError("moving average bars_used cannot be below its window")
        return self


class FundamentalGrowthV2(_EvidenceNode):
    metric: Literal["REVENUE", "EARNINGS"]
    current: Decimal
    prior: Decimal
    unit: str = Field(min_length=1)
    current_period: str = Field(min_length=1)
    prior_period: str = Field(min_length=1)


class EstimateRevisionV2(_EvidenceNode):
    metric: str = Field(min_length=1)
    forecast_period: str = Field(min_length=1)
    prior_estimate: Decimal
    current_estimate: Decimal
    unit: str = Field(min_length=1)
    prior_available_at: AwareDatetime
    current_available_at: AwareDatetime

    @model_validator(mode="after")
    def validate_revision_time(self) -> EstimateRevisionV2:
        if self.current_available_at <= self.prior_available_at:
            raise ValueError("estimate revision availability must advance")
        return self


class DailyTrendHealthContractV2(AStockModel):
    contract_kind: Literal[SerenityContractKind.DAILY_TREND_HEALTH] = (
        SerenityContractKind.DAILY_TREND_HEALTH
    )
    contract_version: Literal["daily-trend-health-v2"] = "daily-trend-health-v2"
    target_company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    daily_series: DailySeriesV2
    moving_averages: list[MovingAverageV2] = Field(min_length=1)
    fundamental_growth: list[FundamentalGrowthV2]
    estimate_revisions: list[EstimateRevisionV2]
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_daily_contract(self) -> DailyTrendHealthContractV2:
        _unique([item.window for item in self.moving_averages], "daily moving-average windows")
        if self.daily_series.as_of != self.as_of:
            raise ValueError("daily series must share contract as_of")
        if any(
            item.calculated_at != self.daily_series.as_of or item.calculated_at != self.as_of
            for item in self.moving_averages
        ):
            raise ValueError("daily series and moving averages must share as_of")
        if any(item.bars_used > self.daily_series.bar_count for item in self.moving_averages):
            raise ValueError("moving average bars_used cannot exceed the frozen daily series")
        if any(
            item.dataset_version != self.daily_series.dataset_version
            for item in self.moving_averages
        ):
            raise ValueError("moving averages must bind the frozen daily dataset version")
        _unique([item.metric for item in self.fundamental_growth], "fundamental metrics")
        if any(
            item.prior_available_at > self.as_of or item.current_available_at > self.as_of
            for item in self.estimate_revisions
        ):
            raise ValueError("estimate revisions cannot be future relative to as_of")
        _evidence_union_matches(self)
        return self


class JuglarDimensionScoreV1(_EvidenceNode):
    dimension: JuglarCycleDimension
    score: int = Field(ge=-2, le=2)
    explanation: str = Field(min_length=1)


class JuglarStageProbabilityV1(AStockModel):
    stage: JuglarStage
    probability: Decimal = Field(ge=0, le=1)


class JuglarMigrationSignalV1(_EvidenceNode):
    signal_id: str = Field(min_length=1)
    target_stage: JuglarStage
    observable: str = Field(min_length=1)
    interpretation: str = Field(min_length=1)


class JuglarCounterEvidenceV1(_EvidenceNode):
    counterevidence_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class JuglarCycleStageContractV1(AStockModel):
    contract_kind: Literal[SerenityContractKind.JUGLAR_CYCLE_STAGE] = (
        SerenityContractKind.JUGLAR_CYCLE_STAGE
    )
    contract_version: Literal["juglar-cycle-stage-v1"] = "juglar-cycle-stage-v1"
    target_company_id: str = Field(min_length=1)
    as_of: AwareDatetime
    core_industry: str = Field(min_length=1)
    dimension_scores: list[JuglarDimensionScoreV1] = Field(min_length=8, max_length=8)
    stage_probabilities: list[JuglarStageProbabilityV1] = Field(min_length=5, max_length=5)
    industry_stage: JuglarStage
    company_operating_stage: JuglarStage
    stock_pricing_stage: JuglarStage
    counterevidence: list[JuglarCounterEvidenceV1] = Field(min_length=1)
    migration_signals: list[JuglarMigrationSignalV1] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_juglar_contract(self) -> JuglarCycleStageContractV1:
        dimensions = [item.dimension for item in self.dimension_scores]
        if set(dimensions) != set(JuglarCycleDimension) or len(dimensions) != len(set(dimensions)):
            raise ValueError("Juglar cycle contract requires each of the eight dimensions once")
        stages = [item.stage for item in self.stage_probabilities]
        if set(stages) != set(JuglarStage) or len(stages) != len(set(stages)):
            raise ValueError("Juglar cycle contract requires all five stage probabilities once")
        probability_sum = sum((item.probability for item in self.stage_probabilities), Decimal("0"))
        if probability_sum != Decimal("1"):
            raise ValueError("Juglar cycle stage probabilities must sum exactly to one")
        _unique(
            [item.counterevidence_id for item in self.counterevidence],
            "Juglar counter-evidence ids",
        )
        _unique(
            [item.signal_id for item in self.migration_signals],
            "Juglar migration signal ids",
        )
        _evidence_union_matches(self)
        return self


SerenityMethodContractV2 = Annotated[
    IndustryBottleneckContractV2
    | EventToAlphaContractV2
    | GrowthProbabilityContractV2
    | GrowthValuationContractV2
    | DailyTrendHealthContractV2
    | JuglarCycleStageContractV1,
    Field(discriminator="contract_kind"),
]


class _DiagnosticRequestV2Base(AStockModel):
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)


class IndustryBottleneckDiagnosticRequestV2(_DiagnosticRequestV2Base):
    skill_id: Literal["IndustryBottleneckSkill"] = "IndustryBottleneckSkill"
    skill_version: Literal["industry-bottleneck-v2"] = "industry-bottleneck-v2"
    method_contract: IndustryBottleneckContractV2


class EventToAlphaDiagnosticRequestV2(_DiagnosticRequestV2Base):
    skill_id: Literal["EventToAlphaSkill"] = "EventToAlphaSkill"
    skill_version: Literal["event-to-alpha-v2"] = "event-to-alpha-v2"
    method_contract: EventToAlphaContractV2
    headline_only: bool = False


class GrowthProbabilityDiagnosticRequestV2(_DiagnosticRequestV2Base):
    skill_id: Literal["GrowthProbabilitySkill"] = "GrowthProbabilitySkill"
    skill_version: Literal["growth-probability-v2"] = "growth-probability-v2"
    method_input: GrowthProbabilityInputV2


class GrowthValuationDiagnosticRequestV2(_DiagnosticRequestV2Base):
    skill_id: Literal["GrowthValuationLens"] = "GrowthValuationLens"
    skill_version: Literal["growth-valuation-v2"] = "growth-valuation-v2"
    method_contract: GrowthValuationContractV2


class DailyTrendDiagnosticRequestV2(_DiagnosticRequestV2Base):
    skill_id: Literal["DailyTrendHealthSkill"] = "DailyTrendHealthSkill"
    skill_version: Literal["daily-trend-health-v2"] = "daily-trend-health-v2"
    method_contract: DailyTrendHealthContractV2


class JuglarCycleDiagnosticRequestV2(_DiagnosticRequestV2Base):
    skill_id: Literal["JuglarCycleStageSkill"] = "JuglarCycleStageSkill"
    skill_version: Literal["juglar-cycle-stage-v1"] = "juglar-cycle-stage-v1"
    method_contract: JuglarCycleStageContractV1


SpecialistDiagnosticRequestV2 = Annotated[
    IndustryBottleneckDiagnosticRequestV2
    | EventToAlphaDiagnosticRequestV2
    | GrowthProbabilityDiagnosticRequestV2
    | GrowthValuationDiagnosticRequestV2
    | DailyTrendDiagnosticRequestV2
    | JuglarCycleDiagnosticRequestV2,
    Field(discriminator="skill_id"),
]


class MemoControversyV2(AStockModel):
    controversy_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    supporting_source_refs: list[str] = Field(min_length=1)
    opposing_source_refs: list[str] = Field(min_length=1)
    open_gap_codes: list[str]


class MemoScenarioV2(AStockModel):
    case: MemoScenarioCase
    thesis: str = Field(min_length=1)
    assumption_source_refs: list[str] = Field(min_length=1)
    growth_hypothesis_refs: list[str]
    probability_ref: str | None = None


class MemoCatalystV2(AStockModel):
    catalyst_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    expected_start: AwareDatetime
    expected_end: AwareDatetime
    observable: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_window(self) -> MemoCatalystV2:
        if self.expected_end < self.expected_start:
            raise ValueError("memo catalyst end cannot precede start")
        return self


class MemoInvalidationV2(AStockModel):
    invalidation_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    observable: str = Field(min_length=1)
    comparator: Comparator
    threshold: Decimal
    unit: str = Field(min_length=1)
    deadline: AwareDatetime
    source_refs: list[str] = Field(min_length=1)


class MemoMonitoringItemV2(AStockModel):
    item_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    cadence: str = Field(min_length=1)
    next_review_at: AwareDatetime


class StructuredResearchMemoV2(AStockModel):
    composer_version: Literal["research-memo-composer-v2"] = "research-memo-composer-v2"
    controversies: list[MemoControversyV2] = Field(min_length=1)
    scenarios: list[MemoScenarioV2] = Field(min_length=3, max_length=3)
    catalysts: list[MemoCatalystV2]
    invalidations: list[MemoInvalidationV2] = Field(min_length=1)
    monitoring_items: list[MemoMonitoringItemV2]

    @model_validator(mode="after")
    def validate_memo(self) -> StructuredResearchMemoV2:
        if {item.case for item in self.scenarios} != set(MemoScenarioCase):
            raise ValueError("structured memo requires Bull, Base and Bear exactly once")
        _unique([item.controversy_id for item in self.controversies], "memo controversy ids")
        _unique([item.catalyst_id for item in self.catalysts], "memo catalyst ids")
        _unique([item.invalidation_id for item in self.invalidations], "memo invalidation ids")
        _unique([item.item_id for item in self.monitoring_items], "memo monitoring ids")
        return self


class ResearchMemoComposeRequestV2(AStockModel):
    base_case_id: str = Field(min_length=1)
    route_plan_id: str = Field(min_length=1)
    delta_ids: list[str]
    structured_memo: StructuredResearchMemoV2

    @model_validator(mode="after")
    def validate_delta_ids(self) -> ResearchMemoComposeRequestV2:
        _unique(self.delta_ids, "memo v2 delta ids")
        return self


def _unique(values: Sequence[Hashable], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _probability_vector(
    values: dict[GrowthHypothesisId, Decimal],
    *,
    require_sum: bool,
) -> None:
    if set(values) != set(GrowthHypothesisId):
        raise ValueError("probability vectors must cover H0 through H5")
    if any(value < 0 or value > 1 for value in values.values()):
        raise ValueError("probability vector values must be within zero and one")
    if require_sum and sum(values.values(), Decimal("0")) != Decimal("1"):
        raise ValueError("probability vectors must sum exactly to one")


def _normalized_bayesian_update(
    prior: dict[GrowthHypothesisId, Decimal],
    likelihood: dict[GrowthHypothesisId, Decimal],
) -> dict[GrowthHypothesisId, Decimal]:
    weighted = {
        hypothesis_id: prior[hypothesis_id] * likelihood[hypothesis_id]
        for hypothesis_id in GrowthHypothesisId
    }
    denominator = sum(weighted.values(), Decimal("0"))
    if denominator <= 0:
        raise ValueError("Bayesian likelihood update has zero evidence probability")
    quantum = Decimal("0.000000000001")
    result: dict[GrowthHypothesisId, Decimal] = {}
    ordered = list(GrowthHypothesisId)
    for hypothesis_id in ordered[:-1]:
        result[hypothesis_id] = (weighted[hypothesis_id] / denominator).quantize(quantum)
    result[ordered[-1]] = Decimal("1") - sum(result.values(), Decimal("0"))
    if result[ordered[-1]] < 0 or result[ordered[-1]] > 1:
        raise ValueError("Bayesian residual normalization escaped probability bounds")
    return result


def _evidence_union_matches(model: AStockModel) -> None:
    payload = model.model_dump(mode="python")
    evidence_ids = payload.pop("evidence_ids")
    nested: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_ids" and isinstance(item, list):
                    nested.update(str(evidence_id) for evidence_id in item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    if evidence_ids != sorted(nested):
        raise ValueError("method contract evidence_ids must equal the nested reference union")


__all__ = [
    "DailyTrendDiagnosticRequestV2",
    "DailyTrendHealthContractV2",
    "EventToAlphaContractV2",
    "EventToAlphaDiagnosticRequestV2",
    "FinancialMetricV2",
    "GrowthHypothesisId",
    "GrowthProbabilityContractV2",
    "GrowthProbabilityDiagnosticRequestV2",
    "GrowthProbabilityInputV2",
    "GrowthPosteriorStepV2",
    "GrowthValuationContractV2",
    "GrowthValuationDiagnosticRequestV2",
    "IndustryBottleneckContractV2",
    "IndustryBottleneckDiagnosticRequestV2",
    "JuglarCounterEvidenceV1",
    "JuglarCycleDiagnosticRequestV2",
    "JuglarCycleDimension",
    "JuglarCycleStageContractV1",
    "JuglarDimensionScoreV1",
    "JuglarMigrationSignalV1",
    "JuglarStage",
    "JuglarStageProbabilityV1",
    "MemoScenarioCase",
    "ObservableInvalidationV2",
    "ResearchMemoComposeRequestV2",
    "SerenityContractKind",
    "SerenityMethodContractV2",
    "SpecialistDiagnosticRequestV2",
    "StructuredResearchMemoV2",
]
