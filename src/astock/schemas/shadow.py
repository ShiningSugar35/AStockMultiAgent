"""Frozen-weight shadow evaluation and Phase 8 admission contracts."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.market import Market, ReplayQuality
from astock.schemas.pit import PointInTimeStatus
from astock.schemas.research import ResearchSkillStatus


class ShadowStudyMode(StrEnum):
    FORWARD_FORMAL = "FORWARD_FORMAL"
    EXPLORATORY_RETROSPECTIVE = "EXPLORATORY_RETROSPECTIVE"


class ShadowEvidenceStatus(StrEnum):
    COLLECTING = "COLLECTING"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    PROVISIONAL = "PROVISIONAL"
    EVIDENCE_READY = "EVIDENCE_READY"
    FAILED_INTEGRITY = "FAILED_INTEGRITY"
    CLOSED = "CLOSED"


class ShadowArmType(StrEnum):
    RULE_BASELINE = "RULE_BASELINE"
    BASE_CASE_ONLY = "BASE_CASE_ONLY"
    BASE_CASE_PLUS_SPECIALIST = "BASE_CASE_PLUS_SPECIALIST"
    FULL_COMMITTEE = "FULL_COMMITTEE"
    APPROVED_SKILL = "APPROVED_SKILL"
    CSI300_BENCHMARK = "CSI300_BENCHMARK"
    CHINA_ALL_BENCHMARK = "CHINA_ALL_BENCHMARK"
    EQUAL_WEIGHT_CANDIDATE = "EQUAL_WEIGHT_CANDIDATE"


class ShadowArmResearchStatus(StrEnum):
    PRODUCTION_CONTRACT = "PRODUCTION_CONTRACT"
    RESEARCH_ISOLATED = "RESEARCH_ISOLATED"
    BENCHMARK = "BENCHMARK"


class ShadowAction(StrEnum):
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    NO_ACTION = "NO_ACTION"


class ShadowObservationStatus(StrEnum):
    PENDING_MATURITY = "PENDING_MATURITY"
    MATURE = "MATURE"
    EXCLUDED = "EXCLUDED"


class MarketRegime(StrEnum):
    PANIC = "PANIC"
    HIGH_VOL_BULL = "HIGH_VOL_BULL"
    TREND_BULL = "TREND_BULL"
    TREND_BEAR = "TREND_BEAR"
    RANGE = "RANGE"
    UNCLASSIFIED = "UNCLASSIFIED"


class Phase8AdmissionStatus(StrEnum):
    ELIGIBLE_RULE_STATE_MACHINE_RESEARCH = "ELIGIBLE_RULE_STATE_MACHINE_RESEARCH"
    NOT_ELIGIBLE_INSUFFICIENT_SAMPLE = "NOT_ELIGIBLE_INSUFFICIENT_SAMPLE"
    NOT_ELIGIBLE_INTEGRITY = "NOT_ELIGIBLE_INTEGRITY"
    NOT_ELIGIBLE_NO_INCREMENT = "NOT_ELIGIBLE_NO_INCREMENT"


class ShadowEvaluationPolicy(AStockModel):
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    effective_from: AwareDatetime
    regime_rule_version: str = Field(min_length=1)
    independence_rule_version: str = Field(min_length=1)
    statistics_version: str = Field(min_length=1)
    required_horizons: list[int] = Field(min_length=3)
    final_horizon_days: int = Field(ge=1)
    minimum_independent_decisions: int = Field(ge=1)
    minimum_regime_count: int = Field(ge=1)
    minimum_decisions_per_regime: int = Field(ge=1)
    minimum_walk_forward_folds: int = Field(ge=1)
    minimum_decisions_per_fold: int = Field(ge=1)
    provisional_observation_months: int = Field(ge=1)
    phase8_observation_months: int = Field(ge=1)
    bootstrap_replicates: int = Field(ge=100)
    bootstrap_block_length: int = Field(ge=1)
    confidence_level: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)
    holm_family_alpha: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)
    minimum_positive_fold_ratio: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    maximum_drawdown: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    maximum_drawdown_worsening: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    maximum_path_uncertainty_rate: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    minimum_dual_source_rate: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    maximum_single_profit_contribution: Decimal = Field(
        gt=0, le=1, allow_inf_nan=False
    )
    maximum_regime_profit_contribution: Decimal = Field(
        gt=0, le=1, allow_inf_nan=False
    )
    formal_pit_statuses: list[PointInTimeStatus] = Field(min_length=1)
    panic_drawdown_threshold: Decimal = Field(ge=Decimal("-1"), le=0)
    panic_volatility_percentile: Decimal = Field(ge=0, le=1)
    panic_breadth_threshold: Decimal = Field(ge=0, le=1)
    bull_daily_trend_threshold: Decimal = Field(ge=0, le=1)
    bear_daily_trend_threshold: Decimal = Field(ge=Decimal("-1"), le=0)
    bull_breadth_threshold: Decimal = Field(ge=0, le=1)
    bear_breadth_threshold: Decimal = Field(ge=0, le=1)
    high_volatility_percentile: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_policy(self) -> ShadowEvaluationPolicy:
        if self.required_horizons != [5, 20, 60]:
            raise ValueError("shadow evaluation horizons must be exactly [5, 20, 60]")
        if self.final_horizon_days != self.required_horizons[-1]:
            raise ValueError("final shadow horizon must be the longest required horizon")
        if self.phase8_observation_months < self.provisional_observation_months:
            raise ValueError("Phase 8 observation period cannot be shorter than provisional")
        if (
            self.minimum_walk_forward_folds * self.minimum_decisions_per_fold
            > self.minimum_independent_decisions
        ):
            raise ValueError("walk-forward fold minimums cannot exceed the total minimum")
        if self.bear_breadth_threshold > self.bull_breadth_threshold:
            raise ValueError("bear breadth threshold cannot exceed bull breadth threshold")
        _require_sorted_unique(self.formal_pit_statuses, "formal PIT statuses")
        if set(self.formal_pit_statuses) != {
            PointInTimeStatus.CERTIFIED,
            PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
        }:
            raise ValueError("formal shadow PIT statuses must be certified or reconstructed")
        return self


class FrozenWeightProfile(AStockModel):
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    component_weights: dict[str, Decimal] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_weights(self) -> FrozenWeightProfile:
        if any(not key for key in self.component_weights):
            raise ValueError("weight component ids cannot be empty")
        if any(value < 0 or value > 1 for value in self.component_weights.values()):
            raise ValueError("weight components must be between zero and one")
        if sum(self.component_weights.values(), Decimal("0")) != Decimal("1"):
            raise ValueError("frozen component weights must sum exactly to one")
        return self


class ShadowArmDraft(AStockModel):
    arm_key: str = Field(min_length=1)
    arm_type: ShadowArmType
    weight_profile: FrozenWeightProfile
    research_status: ShadowArmResearchStatus
    protocol_family_version: str = Field(min_length=1)
    cost_model_version: str = Field(min_length=1)
    fill_model_version: str = Field(min_length=1)
    corporate_action_version: str = Field(min_length=1)
    specialist_skill_id: str | None = None
    specialist_skill_version: str | None = None
    specialist_skill_status: ResearchSkillStatus | None = None
    benchmark_symbol: str | None = None

    @model_validator(mode="after")
    def validate_arm(self) -> ShadowArmDraft:
        specialist = self.arm_type in {
            ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
            ShadowArmType.APPROVED_SKILL,
        }
        specialist_fields = (
            self.specialist_skill_id,
            self.specialist_skill_version,
            self.specialist_skill_status,
        )
        if specialist and any(value is None for value in specialist_fields):
            raise ValueError("specialist shadow arms require a frozen Skill identity")
        if not specialist and any(value is not None for value in specialist_fields):
            raise ValueError("non-specialist shadow arms cannot claim a Skill identity")
        benchmark = self.arm_type in {
            ShadowArmType.CSI300_BENCHMARK,
            ShadowArmType.CHINA_ALL_BENCHMARK,
        }
        if benchmark != bool(self.benchmark_symbol):
            raise ValueError("only benchmark shadow arms require a benchmark symbol")
        if benchmark and self.research_status is not ShadowArmResearchStatus.BENCHMARK:
            raise ValueError("benchmark arms require BENCHMARK research status")
        if (
            self.arm_type is ShadowArmType.APPROVED_SKILL
            and self.specialist_skill_status is not ResearchSkillStatus.ENABLED_CONTRACT
        ):
            raise ValueError("approved Skill arms require an enabled production contract")
        if (
            self.research_status is ShadowArmResearchStatus.RESEARCH_ISOLATED
            and self.arm_type is not ShadowArmType.APPROVED_SKILL
        ):
            raise ValueError("only Skill arms may be research-isolated")
        return self


class ShadowStudyCreateRequest(AStockModel):
    study_name: str = Field(min_length=1)
    mode: ShadowStudyMode
    effective_from: AwareDatetime
    observation_end: AwareDatetime | None = None
    candidate_policy_id: str = Field(min_length=1)
    candidate_policy_version: str = Field(min_length=1)
    candidate_set_id: str = Field(min_length=1)
    initial_capital_fen: int = Field(gt=0)
    fixed_notional_fen: int = Field(gt=0)
    arms: list[ShadowArmDraft] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_study_request(self) -> ShadowStudyCreateRequest:
        if self.fixed_notional_fen > self.initial_capital_fen:
            raise ValueError("fixed shadow notional cannot exceed initial capital")
        keys = [arm.arm_key for arm in self.arms]
        if keys != sorted(set(keys)):
            raise ValueError("shadow arm keys must be sorted and unique")
        if self.mode is ShadowStudyMode.FORWARD_FORMAL:
            if self.created_at > self.effective_from:
                raise ValueError(
                    "formal shadow studies must be frozen before they become effective"
                )
            if self.observation_end is not None:
                raise ValueError("forward shadow studies cannot predeclare a retrospective end")
            required = {
                ShadowArmType.RULE_BASELINE,
                ShadowArmType.BASE_CASE_ONLY,
                ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
                ShadowArmType.FULL_COMMITTEE,
                ShadowArmType.EQUAL_WEIGHT_CANDIDATE,
            }
            types = {arm.arm_type for arm in self.arms}
            if missing := required - types:
                raise ValueError(f"formal shadow study is missing required arms: {sorted(missing)}")
            if not types & {
                ShadowArmType.CSI300_BENCHMARK,
                ShadowArmType.CHINA_ALL_BENCHMARK,
            }:
                raise ValueError("formal shadow studies require an index benchmark arm")
        elif self.observation_end is None:
            raise ValueError("retrospective shadow studies require an observation end")
        if self.observation_end is not None and self.observation_end < self.effective_from:
            raise ValueError("shadow observation end cannot precede effective_from")
        return self


class ShadowArmDefinition(ShadowArmDraft):
    arm_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    arm_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShadowStudyManifest(AStockModel):
    study_id: str = Field(min_length=1)
    study_name: str = Field(min_length=1)
    mode: ShadowStudyMode
    effective_from: AwareDatetime
    observation_end: AwareDatetime | None = None
    candidate_policy_id: str = Field(min_length=1)
    candidate_policy_version: str = Field(min_length=1)
    candidate_set_id: str = Field(min_length=1)
    initial_capital_fen: int = Field(gt=0)
    fixed_notional_fen: int = Field(gt=0)
    policy_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_ids: list[str] = Field(min_length=1)
    evidence_status: ShadowEvidenceStatus = ShadowEvidenceStatus.COLLECTING
    study_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> ShadowStudyManifest:
        _require_sorted_unique(self.arm_ids, "shadow study arm ids")
        if self.evidence_status is ShadowEvidenceStatus.EVIDENCE_READY:
            raise ValueError("new shadow studies cannot self-declare evidence readiness")
        return self


class ShadowStudyPlan(AStockModel):
    prospective_study_id: str = Field(min_length=1)
    prospective_arm_ids: list[str] = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    mode: ShadowStudyMode
    initial_evidence_status: Literal[ShadowEvidenceStatus.COLLECTING] = (
        ShadowEvidenceStatus.COLLECTING
    )
    minimum_independent_decisions: int = Field(ge=1)
    minimum_observation_months: int = Field(ge=1)
    persistent_writes: Literal[False] = False

    @model_validator(mode="after")
    def validate_plan(self) -> ShadowStudyPlan:
        _require_sorted_unique(self.prospective_arm_ids, "prospective shadow arm ids")
        return self


class ShadowArtifactReference(AStockModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_at: AwareDatetime


class ShadowArmSignal(AStockModel):
    arm_id: str = Field(min_length=1)
    action: ShadowAction
    comparable: bool = True
    input_artifact_ids: list[str]
    reason_codes: list[str]

    @model_validator(mode="after")
    def validate_signal(self) -> ShadowArmSignal:
        _require_sorted_unique(self.input_artifact_ids, "shadow signal artifact ids")
        _require_sorted_unique(self.reason_codes, "shadow signal reason codes")
        if not self.comparable and not self.reason_codes:
            raise ValueError("non-comparable shadow signals require a reason code")
        return self


class ShadowDecisionAssignmentRequest(AStockModel):
    study_id: str = Field(min_length=1)
    candidate_set_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    market: Market
    signal_time: AwareDatetime
    independence_key: str = Field(min_length=1)
    thesis_version: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    trade_protocol_id: str = Field(min_length=1)
    artifact_references: list[ShadowArtifactReference] = Field(min_length=1)
    arm_signals: list[ShadowArmSignal] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_assignment_request(self) -> ShadowDecisionAssignmentRequest:
        artifact_ids = [item.artifact_id for item in self.artifact_references]
        if artifact_ids != sorted(set(artifact_ids)):
            raise ValueError("shadow assignment artifacts must be sorted and unique")
        if any(item.available_at > self.signal_time for item in self.artifact_references):
            raise ValueError("shadow assignment cannot include future artifacts")
        signal_arm_ids = [item.arm_id for item in self.arm_signals]
        if signal_arm_ids != sorted(set(signal_arm_ids)):
            raise ValueError("shadow arm signals must be sorted and unique")
        for signal in self.arm_signals:
            if not set(signal.input_artifact_ids).issubset(artifact_ids):
                raise ValueError("shadow signal references an unfrozen artifact")
        return self


class ShadowDecisionAssignment(ShadowDecisionAssignmentRequest):
    assignment_id: str = Field(min_length=1)
    assignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MarketRegimeFeatures(AStockModel):
    feature_snapshot_id: str = Field(min_length=1)
    feature_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: AwareDatetime
    daily_trend_score: Decimal | None = Field(default=None, ge=Decimal("-1"), le=1)
    hourly_trend_score: Decimal | None = Field(default=None, ge=Decimal("-1"), le=1)
    market_breadth: Decimal | None = Field(default=None, ge=0, le=1)
    new_high_low_balance: Decimal | None = Field(
        default=None, ge=Decimal("-1"), le=1
    )
    turnover_ratio: Decimal | None = Field(default=None, ge=0)
    industry_diffusion: Decimal | None = Field(default=None, ge=0, le=1)
    volatility_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    index_drawdown: Decimal | None = Field(default=None, ge=Decimal("-1"), le=0)
    style_relative_performance: Decimal | None = Field(default=None, ge=Decimal("-10"), le=10)
    strategy_performance: Decimal | None = Field(default=None, ge=Decimal("-10"), le=10)
    evidence_ids: list[str] = Field(min_length=1)
    pit_statuses: list[PointInTimeStatus] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_features(self) -> MarketRegimeFeatures:
        _require_sorted_unique(self.evidence_ids, "market-regime evidence ids")
        _require_sorted_unique(self.pit_statuses, "market-regime PIT statuses")
        return self


class MarketRegimeSnapshot(AStockModel):
    regime_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    regime_rule_version: str = Field(min_length=1)
    features: MarketRegimeFeatures
    regime: MarketRegime
    rationale_codes: list[str] = Field(min_length=1)
    regime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_regime(self) -> MarketRegimeSnapshot:
        _require_sorted_unique(self.rationale_codes, "market-regime rationale codes")
        return self


class ShadowExecutionObservationDraft(AStockModel):
    study_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    arm_id: str = Field(min_length=1)
    independence_key: str = Field(min_length=1)
    regime_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    market: Market
    horizon_days: int = Field(ge=1)
    trading_days_elapsed: int = Field(ge=0)
    action: ShadowAction
    signal_time: AwareDatetime
    entry_time: AwareDatetime | None = None
    valuation_time: AwareDatetime | None = None
    quantity: int = Field(ge=0)
    entry_price_fen: int | None = Field(default=None, gt=0)
    valuation_price_fen: int | None = Field(default=None, gt=0)
    corporate_action_cash_fen: int = 0
    gross_pnl_fen: int
    commission_fen: int = Field(ge=0)
    tax_fen: int = Field(ge=0)
    transfer_fee_fen: int = Field(ge=0)
    slippage_fen: int = Field(ge=0)
    net_pnl_fen: int
    capital_at_risk_fen: int = Field(ge=0)
    net_return: Decimal = Field(ge=Decimal("-10"), le=Decimal("10"))
    nav_before_fen: int = Field(gt=0)
    nav_after_fen: int = Field(gt=0)
    mfe: Decimal = Field(ge=Decimal("-10"), le=Decimal("10"))
    mae: Decimal = Field(ge=Decimal("-10"), le=Decimal("10"))
    turnover_fen: int = Field(ge=0)
    liquidity_score: Decimal = Field(ge=0, le=1)
    participation_rate: Decimal = Field(ge=0, le=1)
    replay_quality: ReplayQuality
    market_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_observation_ids: list[str] = Field(min_length=1)
    pit_statuses: list[PointInTimeStatus] = Field(min_length=1)
    ambiguous_intrabar_path: bool = False
    optimistic_net_pnl_fen: int
    exclusion_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_execution(self) -> ShadowExecutionObservationDraft:
        if self.horizon_days not in {5, 20, 60}:
            raise ValueError("shadow observations only support 5/20/60 trading-day horizons")
        _require_sorted_unique(self.market_observation_ids, "shadow market observation ids")
        _require_sorted_unique(self.pit_statuses, "shadow observation PIT statuses")
        _require_sorted_unique(self.exclusion_codes, "shadow exclusion codes")
        no_action = self.action is ShadowAction.NO_ACTION
        if no_action:
            if any(
                value != 0
                for value in (
                    self.quantity,
                    self.gross_pnl_fen,
                    self.commission_fen,
                    self.tax_fen,
                    self.transfer_fee_fen,
                    self.slippage_fen,
                    self.net_pnl_fen,
                    self.capital_at_risk_fen,
                    self.turnover_fen,
                    self.optimistic_net_pnl_fen,
                )
            ):
                raise ValueError("no-action shadow observations must have zero execution values")
            if self.entry_time or self.entry_price_fen or self.valuation_price_fen:
                raise ValueError("no-action shadow observations cannot claim an execution")
            if self.net_return != 0 or self.nav_after_fen != self.nav_before_fen:
                raise ValueError("no-action shadow observations cannot change NAV")
        else:
            if not self.entry_time or not self.entry_price_fen or not self.valuation_price_fen:
                raise ValueError("executed shadow observations require entry and valuation facts")
            if self.quantity <= 0 or self.capital_at_risk_fen <= 0:
                raise ValueError(
                    "executed shadow observations require positive quantity and capital"
                )
            if self.market is not Market.INDEX and self.quantity % 100:
                raise ValueError("A-share shadow quantities must use 100-share lots")
            if self.entry_time < self.signal_time:
                raise ValueError("shadow execution cannot precede its signal")
            if self.valuation_time is None or self.valuation_time < self.entry_time:
                raise ValueError("shadow valuation cannot precede entry")
            expected_gross = (
                (self.valuation_price_fen - self.entry_price_fen) * self.quantity
                + self.corporate_action_cash_fen
            )
            if self.gross_pnl_fen != expected_gross:
                raise ValueError("shadow gross PnL does not reconcile to execution facts")
        costs = (
            self.commission_fen
            + self.tax_fen
            + self.transfer_fee_fen
            + self.slippage_fen
        )
        if self.net_pnl_fen != self.gross_pnl_fen - costs:
            raise ValueError("shadow net PnL does not reconcile after costs")
        if self.nav_after_fen != self.nav_before_fen + self.net_pnl_fen:
            raise ValueError("shadow NAV does not reconcile to net PnL")
        expected_return = (
            Decimal("0")
            if self.capital_at_risk_fen == 0
            else Decimal(self.net_pnl_fen) / Decimal(self.capital_at_risk_fen)
        )
        if self.net_return != expected_return:
            raise ValueError("shadow net return must be recalculated from net PnL")
        if self.optimistic_net_pnl_fen < self.net_pnl_fen:
            raise ValueError("optimistic path cannot be worse than the conservative main result")
        if not self.ambiguous_intrabar_path and self.optimistic_net_pnl_fen != self.net_pnl_fen:
            raise ValueError("path sensitivity requires an ambiguous intrabar path")
        return self


class ShadowExecutionObservation(ShadowExecutionObservationDraft):
    observation_id: str = Field(min_length=1)
    status: ShadowObservationStatus
    formal_eligible: bool
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_observation_status(self) -> ShadowExecutionObservation:
        mature = self.trading_days_elapsed >= self.horizon_days
        if self.status is ShadowObservationStatus.MATURE and not mature:
            raise ValueError("mature shadow observations require a completed horizon")
        if self.status is ShadowObservationStatus.PENDING_MATURITY and mature:
            raise ValueError("completed horizons cannot remain pending maturity")
        if self.status is ShadowObservationStatus.EXCLUDED and not self.exclusion_codes:
            raise ValueError("excluded shadow observations require exclusion codes")
        if self.formal_eligible and self.status is not ShadowObservationStatus.MATURE:
            raise ValueError("only mature shadow observations can be formally eligible")
        if self.formal_eligible and set(self.pit_statuses) - {
            PointInTimeStatus.CERTIFIED,
            PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
        }:
            raise ValueError("formal shadow observations require PIT-safe inputs")
        return self


class ShadowMetricInterval(AStockModel):
    metric: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    estimate: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> ShadowMetricInterval:
        if self.sample_count == 0 and any(
            value is not None for value in (self.estimate, self.lower, self.upper)
        ):
            raise ValueError("empty metric intervals cannot claim estimates")
        if self.sample_count > 0 and self.estimate is None:
            raise ValueError("non-empty metric intervals require an estimate")
        if (self.lower is None) != (self.upper is None):
            raise ValueError("metric confidence bounds must be both present or both absent")
        if self.lower is not None:
            assert self.upper is not None
            assert self.estimate is not None
            if not self.lower <= self.estimate <= self.upper:
                raise ValueError("metric estimate must lie within its confidence interval")
        return self


class ShadowFoldResult(AStockModel):
    fold_number: int = Field(ge=1)
    start_at: AwareDatetime
    end_at: AwareDatetime
    independent_decision_count: int = Field(ge=0)
    paired_net_return_delta: ShadowMetricInterval
    positive_point_estimate: bool

    @model_validator(mode="after")
    def validate_fold(self) -> ShadowFoldResult:
        if self.end_at < self.start_at:
            raise ValueError("shadow fold end cannot precede start")
        if self.paired_net_return_delta.sample_count != self.independent_decision_count:
            raise ValueError("shadow fold metric count must match independent decisions")
        return self


class ShadowRegimeResult(AStockModel):
    regime: MarketRegime
    independent_decision_count: int = Field(ge=0)
    paired_net_return_delta: ShadowMetricInterval
    clearly_harmful: bool


class ShadowArmMetrics(AStockModel):
    arm_id: str = Field(min_length=1)
    independent_decision_count: int = Field(ge=0)
    mature_observation_count: int = Field(ge=0)
    total_net_pnl_fen: int
    mean_net_return: ShadowMetricInterval
    win_rate: ShadowMetricInterval
    maximum_drawdown: Decimal = Field(ge=0, le=1)
    mean_mfe: Decimal | None = None
    mean_mae: Decimal | None = None
    payoff_ratio: Decimal | None = None
    turnover_fen: int = Field(ge=0)
    path_uncertainty_rate: Decimal = Field(ge=0, le=1)
    dual_source_rate: Decimal = Field(ge=0, le=1)


class ShadowComparisonResult(AStockModel):
    baseline_arm_id: str = Field(min_length=1)
    experimental_arm_id: str = Field(min_length=1)
    specialist_skill_id: str | None = None
    paired_decision_count: int = Field(ge=0)
    paired_net_return_delta: ShadowMetricInterval
    raw_one_sided_p_value: Decimal | None = Field(default=None, ge=0, le=1)
    holm_adjusted_p_value: Decimal | None = Field(default=None, ge=0, le=1)
    folds: list[ShadowFoldResult]
    regimes: list[ShadowRegimeResult]
    maximum_drawdown_delta: Decimal
    single_profit_contribution: Decimal = Field(ge=0, le=1)
    regime_profit_contribution: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_comparison(self) -> ShadowComparisonResult:
        fold_numbers = [item.fold_number for item in self.folds]
        if fold_numbers != sorted(set(fold_numbers)):
            raise ValueError("shadow comparison folds must be sorted and unique")
        regimes = [item.regime for item in self.regimes]
        _require_sorted_unique(regimes, "shadow comparison regimes")
        if self.paired_net_return_delta.sample_count != self.paired_decision_count:
            raise ValueError("shadow comparison metric count must match paired decisions")
        return self


class ShadowEvaluationReport(AStockModel):
    report_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    statistics_version: str = Field(min_length=1)
    as_of: AwareDatetime
    evidence_status: ShadowEvidenceStatus
    observation_months: Decimal = Field(ge=0)
    assignment_count: int = Field(ge=0)
    mature_observation_count: int = Field(ge=0)
    independent_decision_count: int = Field(ge=0)
    market_regime_counts: dict[MarketRegime, int]
    pit_status_counts: dict[PointInTimeStatus, int]
    exclusion_counts: dict[str, int]
    arm_metrics: list[ShadowArmMetrics]
    comparisons: list[ShadowComparisonResult]
    finding_codes: list[str]
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> ShadowEvaluationReport:
        arm_ids = [item.arm_id for item in self.arm_metrics]
        if arm_ids != sorted(set(arm_ids)):
            raise ValueError("shadow report arm metrics must be sorted and unique")
        comparison_ids = [
            (item.baseline_arm_id, item.experimental_arm_id) for item in self.comparisons
        ]
        if comparison_ids != sorted(set(comparison_ids)):
            raise ValueError("shadow report comparisons must be sorted and unique")
        _require_sorted_unique(self.finding_codes, "shadow report finding codes")
        if self.evidence_status is ShadowEvidenceStatus.EVIDENCE_READY:
            if self.assignment_count == 0 or not self.comparisons:
                raise ValueError("evidence-ready shadow reports require real comparisons")
        return self


class Phase8AdmissionReport(AStockModel):
    admission_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    shadow_report_id: str = Field(min_length=1)
    shadow_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Phase8AdmissionStatus
    gate_results: dict[str, bool] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    only_rule_state_machine_research_allowed: Literal[True] = True
    online_weight_changes_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False
    admission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_admission(self) -> Phase8AdmissionReport:
        _require_sorted_unique(self.reason_codes, "Phase 8 admission reason codes")
        eligible = (
            self.status is Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
        )
        if eligible != all(self.gate_results.values()):
            raise ValueError("Phase 8 eligibility must match all deterministic gates")
        return self


class ShadowStatusReport(AStockModel):
    study_id: str | None = None
    status: str = Field(min_length=1)
    arm_count: int = Field(ge=0)
    assignment_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    mature_observation_count: int = Field(ge=0)
    independent_decision_count: int = Field(ge=0)
    report_id: str | None = None
    admission_status: Phase8AdmissionStatus | None = None


def _require_sorted_unique(values: Sequence[object], label: str) -> None:
    rendered = [item.value if isinstance(item, StrEnum) else str(item) for item in values]
    if rendered != sorted(set(rendered)):
        raise ValueError(f"{label} must be sorted and unique")


__all__ = [
    "FrozenWeightProfile",
    "MarketRegime",
    "MarketRegimeFeatures",
    "MarketRegimeSnapshot",
    "Phase8AdmissionReport",
    "Phase8AdmissionStatus",
    "ShadowAction",
    "ShadowArmDefinition",
    "ShadowArmDraft",
    "ShadowArmMetrics",
    "ShadowArmResearchStatus",
    "ShadowArmSignal",
    "ShadowArmType",
    "ShadowArtifactReference",
    "ShadowComparisonResult",
    "ShadowDecisionAssignment",
    "ShadowDecisionAssignmentRequest",
    "ShadowEvaluationPolicy",
    "ShadowEvaluationReport",
    "ShadowEvidenceStatus",
    "ShadowExecutionObservation",
    "ShadowExecutionObservationDraft",
    "ShadowFoldResult",
    "ShadowMetricInterval",
    "ShadowObservationStatus",
    "ShadowRegimeResult",
    "ShadowStatusReport",
    "ShadowStudyCreateRequest",
    "ShadowStudyManifest",
    "ShadowStudyMode",
    "ShadowStudyPlan",
]
