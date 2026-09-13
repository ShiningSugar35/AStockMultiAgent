from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )


class PortfolioLane(StrEnum):
    ACTUAL = "ACTUAL"
    PAPER = "PAPER"
    WATCHLIST = "WATCHLIST"
    RESEARCH = "RESEARCH"


class DatePrecision(StrEnum):
    EXACT = "EXACT"
    DATE_ONLY = "DATE_ONLY"
    MONTH_ONLY = "MONTH_ONLY"
    UNKNOWN = "UNKNOWN"


class RequestIntent(StrEnum):
    RESEARCH = "RESEARCH"
    FULL_RESEARCH_RECOMMENDATION = "FULL_RESEARCH_RECOMMENDATION"
    ACCOUNT_FACT_WRITE = "ACCOUNT_FACT_WRITE"
    PAPER_PREPARE = "PAPER_PREPARE"
    PAPER_CONFIRM = "PAPER_CONFIRM"
    PAPER_STATUS = "PAPER_STATUS"
    MONITOR = "MONITOR"


class SideEffectClass(StrEnum):
    READ = "READ"
    META = "META"
    EA_WRITE = "EA_WRITE"
    EA_PROVISIONAL = "EA_PROVISIONAL"
    PT_PREPARE = "PT_PREPARE"
    PT_CONFIRM = "PT_CONFIRM"
    PT_REPLAY = "PT_REPLAY"
    NONE = "NONE"


_FULL_RESEARCH_DECISION_PATTERNS = (
    re.compile(
        r"推荐|买什么|买哪些|怎么买|怎么配置|值得买|能不能买|能买吗|可以买吗|"
        r"买入价|卖出价|买入区间|仓位|投资组合|止损|加仓|减仓|清仓|目标价"
    ),
    re.compile(r"(?:选|挑).{0,12}(?:\d+|[一二三四五六七八九十]+).{0,6}(?:只|支|个)?股票"),
    re.compile(
        r"(?:持仓|持有|已经买|已买|买过|成本|浮亏|浮盈).{0,24}"
        r"(?:怎么办|怎么处理|要不要|是否继续|继续持有|卖出|加仓|减仓|清仓|止损|持有多久)"
    ),
    re.compile(
        r"(?:要不要|是否|继续|卖出|减仓|加仓|清仓|止损).{0,24}"
        r"(?:持仓|我.{0,8}持有|已买|买过|成本|浮亏|浮盈)"
    ),
    re.compile(r"股票代码.{0,24}(?:买入|价格|仓位)|(?:买入|价格|仓位).{0,24}股票代码"),
    re.compile(r"(?:买|买入|配置|下单).{0,12}多少股|多少股.{0,12}(?:买|买入|配置|下单)"),
    re.compile(r"排序.{0,24}(?:选择|买|股票)|(?:选择|买).{0,24}排序"),
    re.compile(
        r"\b(?:recommend|what to buy|stock picks?|portfolio allocation|buy price|sell price|"
        r"position sizing|stop loss|add to (?:the )?position)\b",
        re.IGNORECASE,
    ),
)


_LEGACY_FULL_RESEARCH_INPUT_ALIASES = frozenset(
    {"BUY_DECISION", "HOLDING_DECISION", "RECOMMENDATION", "PORTFOLIO_DECISION"}
)

_HOLDING_CONTEXT_PATTERNS = (
    re.compile(r"持仓|持有|我有.{0,8}(?:股|股票)|已经买|已买|买过|成本价|浮亏|浮盈|减仓|加仓|清仓"),
    re.compile(
        r"\b(?:my (?:holding|position)|existing (?:holding|position)|already (?:own|bought)|"
        r"trim (?:my )?position|add to (?:my )?position|exit (?:my )?position)\b",
        re.IGNORECASE,
    ),
)


def _intent_value(intent: RequestIntent | str) -> str:
    return intent.value if isinstance(intent, RequestIntent) else str(intent)


def requires_full_research_recommendation(
    raw_text: str,
    intent: RequestIntent | str,
    side_effect: SideEffectClass | str,
) -> bool:
    """Return whether a request may directly drive an investment decision.

    Historical decision names are accepted only as input-migration aliases. They are
    normalized before validation and do not own a planner, executor or publication path.
    """

    intent_value = _intent_value(intent)
    if intent_value in _LEGACY_FULL_RESEARCH_INPUT_ALIASES:
        return True
    normalized_intent = RequestIntent(intent_value)
    SideEffectClass(side_effect)
    if normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION:
        return True
    if normalized_intent is not RequestIntent.RESEARCH:
        return False
    text = raw_text.strip()
    return any(pattern.search(text) is not None for pattern in _FULL_RESEARCH_DECISION_PATTERNS)


def requires_existing_holding_context(raw_text: str, intent: RequestIntent | str) -> bool:
    intent_value = _intent_value(intent)
    if intent_value == "HOLDING_DECISION":
        return True
    text = raw_text.strip()
    return any(pattern.search(text) is not None for pattern in _HOLDING_CONTEXT_PATTERNS)


class CapabilityRequirement(StrEnum):
    REQUIRED = "REQUIRED"
    CONDITIONAL = "CONDITIONAL"
    OPTIONAL = "OPTIONAL"
    PROHIBITED = "PROHIBITED"


class CapabilityRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REUSED = "REUSED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    PROHIBITED = "PROHIBITED"


class SubjectEventKind(StrEnum):
    MENTIONED = "MENTIONED"
    RESEARCHED = "RESEARCHED"
    RECOMMENDED = "RECOMMENDED"
    WATCHLIST_ADDED = "WATCHLIST_ADDED"
    WATCHLIST_UPDATED = "WATCHLIST_UPDATED"
    WATCHLIST_REMOVED = "WATCHLIST_REMOVED"
    HELD_ACTUAL = "HELD_ACTUAL"
    HELD_PAPER = "HELD_PAPER"
    EXITED = "EXITED"
    REJECTED = "REJECTED"
    MONITOR_ENROLLED = "MONITOR_ENROLLED"
    MONITOR_REMOVED = "MONITOR_REMOVED"


class RegimeState(StrEnum):
    HEALTHY_BULL = "HEALTHY_BULL"
    SPECULATIVE_BULL = "SPECULATIVE_BULL"
    NEUTRAL_RANGE = "NEUTRAL_RANGE"
    RISK_OFF_RANGE = "RISK_OFF_RANGE"
    TREND_BEAR = "TREND_BEAR"
    PANIC = "PANIC"
    TRANSITION = "TRANSITION"
    UNCLASSIFIED = "UNCLASSIFIED"


class ScheduledDomain(StrEnum):
    WATCHLIST = "WATCHLIST"
    PAPER_HOLDING = "PAPER_HOLDING"
    ACTUAL_HOLDING = "ACTUAL_HOLDING"


class ScheduledWindow(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    INTRADAY = "INTRADAY"
    POST_CLOSE = "POST_CLOSE"


class ScheduledActionPolicy(StrEnum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    PROPOSE_ONLY = "PROPOSE_ONLY"
    REPLAY_CONFIRMED_RULES_ONLY = "REPLAY_CONFIRMED_RULES_ONLY"
    PUSH_ADVISORY_ONLY = "PUSH_ADVISORY_ONLY"


class ScheduledRunOutcome(StrEnum):
    MATERIAL_CHANGE = "MATERIAL_CHANGE"
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class DisclosureLevel(StrEnum):
    MINIMUM = "MINIMUM"
    ACTIONABLE = "ACTIONABLE"
    EXPLICIT_FULL = "EXPLICIT_FULL"


class AdmissionStatus(StrEnum):
    IMPLEMENTED_DISABLED = "IMPLEMENTED_DISABLED"
    SHADOW_ONLY = "SHADOW_ONLY"
    NOT_ADMITTED = "NOT_ADMITTED"
    ELIGIBLE = "ELIGIBLE"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"


class ResolvedDate(StrictModel):
    original_text: str
    user_timezone: str
    market_timezone: str = "Asia/Shanghai"
    civil_date: date | None = None
    exact_timestamp: datetime | None = None
    precision: DatePrecision = DatePrecision.UNKNOWN

    @field_validator("exact_timestamp")
    @classmethod
    def exact_timestamp_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("exact_timestamp must be timezone-aware")
        return value


class InvestorRequestEnvelope(StrictModel):
    request_id: str
    question_time: datetime
    research_mode: Literal["CURRENT", "HISTORICAL"] = "CURRENT"
    decision_time: datetime | None = None
    parent_request_id: str | None = None
    decision_freeze_artifact_id: str | None = None
    user_timezone: str
    market_timezone: str = "Asia/Shanghai"
    raw_text: str
    normalized_intent: RequestIntent
    side_effect: SideEffectClass
    entity_ids: tuple[str, ...] = ()
    account_id: str | None = None
    resolved_dates: tuple[ResolvedDate, ...] = ()
    requested_horizon: str | None = None
    risk_intent: str | None = None
    idempotency_key: str
    conversation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def route_material_decision_to_full_research(cls, raw: Any) -> Any:
        """Make the high-risk route part of request construction, not answer prompting."""

        if not isinstance(raw, dict):
            return raw
        intent = raw.get("normalized_intent", RequestIntent.RESEARCH)
        side_effect = raw.get("side_effect", SideEffectClass.READ)
        text = str(raw.get("raw_text", ""))
        if not requires_full_research_recommendation(text, intent, side_effect):
            return raw
        intent_value = _intent_value(intent)
        routed = dict(raw)
        routed["normalized_intent"] = RequestIntent.FULL_RESEARCH_RECOMMENDATION
        metadata = dict(routed.get("metadata") or {})
        if intent_value != RequestIntent.FULL_RESEARCH_RECOMMENDATION.value:
            metadata.setdefault("full_research_routed_from", intent_value)
        metadata["full_research_router_policy"] = "full-research-recommendation-v1"
        if requires_existing_holding_context(text, intent):
            metadata["full_research_holding_context"] = True
        routed["metadata"] = metadata
        return routed

    @field_validator("question_time")
    @classmethod
    def question_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("question_time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_decision_clock(self) -> InvestorRequestEnvelope:
        if self.decision_time is None:
            if self.parent_request_id is not None or self.decision_freeze_artifact_id is not None:
                raise ValueError("decision freeze identity requires a frozen decision time")
            return self
        if self.research_mode != "CURRENT":
            raise ValueError("historical requests cannot advance their frozen evidence cutoff")
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")
        if self.decision_time < self.question_time:
            raise ValueError("current decision time cannot precede its original question")
        if not self.parent_request_id or not self.decision_freeze_artifact_id:
            raise ValueError("current decision time requires its registered freeze lineage")
        if self.parent_request_id == self.request_id:
            raise ValueError("decision freeze cannot overwrite its parent request")
        return self

    @property
    def evidence_cutoff(self) -> datetime:
        return self.decision_time or self.question_time


class PositionView(StrictModel):
    account_id: str
    lane: PortfolioLane
    instrument_id: str
    quantity: Decimal
    available_quantity: Decimal | None = None
    average_cost: Decimal | None = None
    cost_status: Literal["EXACT", "ESTIMATED", "UNKNOWN"] = "UNKNOWN"
    market_value: Decimal | None = None
    currency: str = "CNY"
    source_revision: str


class OrderView(StrictModel):
    account_id: str
    lane: PortfolioLane = PortfolioLane.PAPER
    order_id: str
    instrument_id: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    limit_price: Decimal | None = None
    status: str
    confirmed: bool = False
    source_revision: str


class PendingSettlementView(StrictModel):
    settlement_id: str
    account_id: str
    instrument_id: str
    quantity: Decimal = Field(gt=0)
    eligible_on: date
    source_event_id: str


class LaneSnapshot(StrictModel):
    lane: PortfolioLane
    account_ids: tuple[str, ...] = ()
    positions: tuple[PositionView, ...] = ()
    open_orders: tuple[OrderView, ...] = ()
    pending_settlements: tuple[PendingSettlementView, ...] = ()
    cash_by_account: dict[str, Decimal | None] = Field(default_factory=dict)
    frozen_cash: Decimal | None = None
    known_cash: Decimal | None = None
    unknown_cash: bool = True
    source_revision: str
    audit_status: Literal["PASS", "DEGRADED", "FAILED", "NOT_APPLICABLE"]
    warnings: tuple[str, ...] = ()


class MaterialEventView(StrictModel):
    event_id: str
    instrument_id: str | None = None
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    available_at: datetime
    event_type: str
    summary: str
    source_revision: str

    @field_validator("available_at")
    @classmethod
    def available_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        return value


class UnifiedPortfolioContext(StrictModel):
    as_of: datetime
    actual: LaneSnapshot
    paper: LaneSnapshot
    material_events: tuple[MaterialEventView, ...] = ()
    researched_instruments: tuple[str, ...] = ()
    recommended_instruments: tuple[str, ...] = ()
    economic_duplicate_warnings: tuple[str, ...] = ()
    aggregate_revision: str

    @field_validator("as_of")
    @classmethod
    def as_of_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @property
    def empty_holdings(self) -> bool:
        return not self.actual.positions and not self.paper.positions


class RegimeAvailability(StrictModel):
    snapshot_id: str | None = None
    state: RegimeState | None = None
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    age_seconds: int | None = None
    available: bool = False
    reason: str | None = None


class InvestorSessionPreflightReceipt(StrictModel):
    receipt_id: str
    request_id: str
    normalized_intent: RequestIntent | None = None
    as_of: datetime
    source_revision_vector: dict[str, str]
    context: UnifiedPortfolioContext
    regime: RegimeAvailability
    freshness: Literal["FRESH", "STALE", "DEGRADED", "UNKNOWN"]
    coverage_warnings: tuple[str, ...] = ()
    material_holding_change_present: bool = False
    receipt_hash: str
    built_from_cache: bool = False


class ResearchSubjectEvent(StrictModel):
    event_id: str
    instrument_id: str
    event_type: SubjectEventKind
    lane: PortfolioLane = PortfolioLane.RESEARCH
    available_at: datetime
    request_id: str | None = None
    artifact_id: str | None = None
    reason: str | None = None
    idempotency_key: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProvisionalPositionAssertion(StrictModel):
    assertion_id: str
    account_id: str
    lane: PortfolioLane
    instrument_id: str
    quantity: Decimal
    asserted_at: datetime
    date_precision: DatePrecision
    source_text: str
    status: Literal["PROVISIONAL", "REPLACED", "REVERSED"] = "PROVISIONAL"
    idempotency_key: str


class EstimatedCostBasisRange(StrictModel):
    estimate_id: str
    assertion_id: str
    low: Decimal
    high: Decimal
    method: str
    currency: str = "CNY"
    as_of: datetime
    source_artifact_id: str
    status: Literal["ESTIMATED_NOT_LEDGER_TRUTH"] = "ESTIMATED_NOT_LEDGER_TRUTH"

    @model_validator(mode="after")
    def high_not_below_low(self) -> EstimatedCostBasisRange:
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        return self


class CapabilityNode(StrictModel):
    capability_id: str
    requirement: CapabilityRequirement
    dependencies: tuple[str, ...] = ()
    conditional_reason: str | None = None
    freshness_seconds: int | None = None
    side_effect: SideEffectClass = SideEffectClass.READ
    output_schema: str
    parallel_group: str | None = None
    budget_units: int = 1


class CapabilityExecutionPlan(StrictModel):
    plan_id: str
    request_id: str
    policy_version: str
    nodes: tuple[CapabilityNode, ...]
    planned_at: datetime
    plan_hash: str


class CapabilityRunRecord(StrictModel):
    capability_id: str
    status: CapabilityRunStatus
    artifact_ids: tuple[str, ...] = ()
    reason: str | None = None
    source_revision: str | None = None
    latency_ms: int | None = None


class CapabilityCoverageReceipt(StrictModel):
    receipt_id: str
    request_id: str
    plan_id: str
    policy_version: str
    records: tuple[CapabilityRunRecord, ...]
    required_capability_coverage: float
    prohibited_call_count: int
    coverage_complete: bool
    outputs_verified: bool = False
    preflight_receipt_id: str | None = None
    request_fingerprint: str | None = None
    unresolved_conflicts: tuple[str, ...] = ()
    receipt_hash: str


class MacroObservation(StrictModel):
    observation_id: str
    authority: Literal["NBS", "PBOC", "MOF", "NDRC", "OTHER_OFFICIAL"]
    release_family: str
    series_key: str
    observation_period: str
    value: Decimal | str
    unit: str | None = None
    published_at: datetime
    effective_at: datetime | None = None
    ingested_at: datetime
    available_to_system_at: datetime
    revision: str
    source_url: str
    source_hash: str
    first_release: bool = False
    first_observed: bool = False
    first_release_verified: bool = False


class MacroReleaseSnapshot(StrictModel):
    release_id: str
    authority: str
    release_family: str
    source_url: str
    source_hash: str
    captured_at: datetime
    published_at: datetime
    content_type: str
    raw_object_id: str
    capture_mode: Literal["LIVE", "RECORDED", "LEGACY_UNVERIFIED"] = "LEGACY_UNVERIFIED"
    final_source_url: str | None = None
    redirect_chain: tuple[str, ...] = ()
    capture_policy_hash: str | None = None
    observations: tuple[MacroObservation, ...] = ()
    parse_status: Literal["PASS", "PARTIAL", "FAILED"]
    warnings: tuple[str, ...] = ()


class MarketRegimeFeatureSnapshot(StrictModel):
    feature_snapshot_id: str
    as_of: datetime
    trend_score: float | None = None
    breadth_score: float | None = None
    tail_risk_score: float | None = None
    liquidity_score: float | None = None
    valuation_fragility_score: float | None = None
    earnings_diffusion_score: float | None = None
    macro_credit_score: float | None = None
    family_coverage: dict[str, float] = Field(default_factory=dict)
    source_revisions: dict[str, str] = Field(default_factory=dict)
    ood_score: float | None = None

    @field_validator(
        "trend_score",
        "breadth_score",
        "tail_risk_score",
        "liquidity_score",
        "valuation_fragility_score",
        "earnings_diffusion_score",
        "macro_credit_score",
    )
    @classmethod
    def normalized_feature_score(cls, value: float | None) -> float | None:
        if value is None:
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or not -1.0 <= numeric <= 1.0:
            raise ValueError("regime feature scores must be finite and within [-1, 1]")
        return numeric

    @field_validator("ood_score")
    @classmethod
    def normalized_ood_score(cls, value: float | None) -> float | None:
        if value is None:
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
            raise ValueError("ood_score must be finite and within [0, 1]")
        return numeric

    @field_validator("family_coverage")
    @classmethod
    def normalized_family_coverage(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for family, raw in value.items():
            numeric = float(raw)
            if not family.strip() or not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                raise ValueError("family coverage must use named finite values within [0, 1]")
            normalized[family] = numeric
        return normalized

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("regime feature as_of must be timezone-aware")
        return value


class MarketRegimeSnapshotV2(StrictModel):
    snapshot_id: str
    feature_snapshot_id: str
    as_of: datetime
    probabilities: dict[RegimeState, float]
    selected_state: RegimeState
    confidence: float
    baseline_state: RegimeState
    challenger_state: RegimeState | None = None
    disagreement: bool = False
    previous_state: RegimeState | None = None
    dwell_days: int = 0
    emergency_override: bool = False
    top_drivers: tuple[str, ...] = ()
    valid_from: datetime
    expires_at: datetime
    policy_version: str
    model_version: str
    coverage: float
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def probability_vector_is_valid(self) -> MarketRegimeSnapshotV2:
        total = sum(self.probabilities.values())
        if not self.probabilities or abs(total - 1.0) > 1e-6:
            raise ValueError("probabilities must sum to 1")
        if any(value < 0 or value > 1 for value in self.probabilities.values()):
            raise ValueError("probabilities must be between 0 and 1")
        return self


class RegimeDecisionOverlay(StrictModel):
    overlay_id: str
    regime_snapshot_id: str
    policy_version: str
    total_risk_multiplier: float
    new_position_multiplier: float
    single_name_multiplier: float
    recommendation_cap: int
    minimum_liquidity_percentile: float
    margin_of_safety_adjustment: float
    tranche_count: int
    no_trade_band_multiplier: float
    red_team_depth: int
    user_limit_binding: dict[str, float | int | str]
    label_change_only_action_allowed: Literal[False] = False
    feature_lineage_audit: dict[str, str] = Field(default_factory=dict)


class WatchlistAnalysisRevision(StrictModel):
    revision_id: str
    instrument_id: str
    as_of: datetime
    thesis_status: Literal["ATTRACTIVE_WAIT", "RESEARCHING", "READY", "DEGRADED", "REMOVE"]
    valuation_summary: str | None = None
    timing_summary: str | None = None
    material_changes: tuple[str, ...] = ()
    next_review_conditions: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = ()
    previous_revision_id: str | None = None
    revision_hash: str


class ScheduledResearchPolicy(StrictModel):
    policy_id: str
    version: str
    domains: tuple[ScheduledDomain, ...]
    windows: tuple[ScheduledWindow, ...]
    market_timezone: str = "Asia/Shanghai"
    intraday_min_interval_minutes: int = 60
    max_subjects_per_run: int = 50
    material_severities: tuple[str, ...] = ("HIGH", "CRITICAL")
    disclosure_level: DisclosureLevel = DisclosureLevel.MINIMUM
    field_allowlist: tuple[str, ...] = ()
    action_policy_by_domain: dict[ScheduledDomain, ScheduledActionPolicy]
    confirmed_paper_replay_allowed: bool = False
    source_audit_policy_hash: str | None = None
    economic_writes_allowed: Literal[False] = False
    policy_hash: str


class ScheduledTaskBinding(StrictModel):
    binding_id: str
    platform_task_id: str | None = None
    creation_mode: Literal["NATIVE_UI_BOUND", "OFFICIAL_API_VERIFIED", "LOCAL_ONLY"]
    execution_surface: Literal["DESKTOP_LOCAL", "WEB_CLOUD", "EXISTING_CHAT", "LOCAL_DAEMON"]
    schedule_expression: str
    timezone: str
    policy_id: str
    policy_hash: str
    active: bool = True
    confirmed_at: datetime | None = None
    consent_hash: str | None = None


class ScheduledRunRequest(StrictModel):
    run_id: str
    binding_id: str
    schedule_bucket: str
    window: ScheduledWindow
    domains: tuple[ScheduledDomain, ...]
    requested_at: datetime
    source_revision_set: dict[str, str]
    policy_hash: str
    idempotency_key: str
    source_coverage_artifact_ids: tuple[str, ...] = ()
    source_interval_start: datetime | None = None
    source_interval_end: datetime | None = None
    semantic_capability_receipt_id: str | None = None
    semantic_result_artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_source_interval(self) -> ScheduledRunRequest:
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("scheduled request time must be timezone-aware")
        values = (self.source_interval_start, self.source_interval_end)
        if any(value is not None for value in values) or self.source_coverage_artifact_ids:
            start, end = values
            if start is None or end is None:
                raise ValueError("source check interval must include start and end")
            if (
                start.tzinfo is None
                or start.utcoffset() is None
                or end.tzinfo is None
                or end.utcoffset() is None
                or not start < end <= self.requested_at
            ):
                raise ValueError("source check interval must be aware and end by the request time")
        if (
            len(self.source_coverage_artifact_ids) > 1000
            or len(set(self.source_coverage_artifact_ids)) != len(self.source_coverage_artifact_ids)
            or any(not item.strip() for item in self.source_coverage_artifact_ids)
        ):
            raise ValueError("source check reports must be bounded, nonempty identities and unique")
        if (
            self.semantic_capability_receipt_id is not None
            and not self.semantic_capability_receipt_id.strip()
        ):
            raise ValueError("semantic capability receipt identity must be nonempty")
        if (
            len(self.semantic_result_artifact_ids) > 1000
            or len(set(self.semantic_result_artifact_ids)) != len(self.semantic_result_artifact_ids)
            or any(not item.strip() for item in self.semantic_result_artifact_ids)
        ):
            raise ValueError("semantic result artifacts must be bounded, nonempty and unique")
        if self.semantic_result_artifact_ids and self.semantic_capability_receipt_id is None:
            raise ValueError(
                "semantic result artifacts require an authenticated capability receipt"
            )
        return self


class MaterialChangeDigest(StrictModel):
    digest_id: str
    run_id: str
    domain: ScheduledDomain
    instrument_id: str
    as_of: datetime
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    change_summary: str
    impact_summary: str
    action: Literal[
        "WAIT",
        "RESEARCH",
        "REVIEW",
        "HOLD",
        "ADD_REVIEW",
        "TRIM_REVIEW",
        "EXIT_REVIEW",
        "REMOVE",
    ]
    action_conditions: tuple[str, ...] = ()
    actual_execution_allowed: Literal[False] = False
    source_artifact_ids: tuple[str, ...] = ()


class ScheduledSemanticSubjectResult(StrictModel):
    schema_version: Literal["scheduled-semantic-subject-result-v1"] = (
        "scheduled-semantic-subject-result-v1"
    )
    result_id: str
    run_id: str
    domain: ScheduledDomain
    instrument_id: str
    as_of: datetime
    material_change: bool
    digest_artifact_id: str | None = None
    evidence_artifact_ids: tuple[str, ...] = ()
    actual_execution_allowed: Literal[False] = False
    result_hash: str

    @model_validator(mode="after")
    def validate_result(self) -> ScheduledSemanticSubjectResult:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("scheduled semantic result time must be timezone-aware")
        if self.material_change != bool(self.digest_artifact_id):
            raise ValueError("material-change flag must match the digest artifact identity")
        if (
            len(self.evidence_artifact_ids) > 256
            or len(set(self.evidence_artifact_ids)) != len(self.evidence_artifact_ids)
            or any(not item.strip() for item in self.evidence_artifact_ids)
        ):
            raise ValueError("scheduled semantic evidence identities must be bounded and unique")
        return self


class ScheduledRunReceipt(StrictModel):
    receipt_id: str
    run_id: str
    binding_id: str
    schedule_bucket: str
    request_fingerprint: str | None = None
    outcome: ScheduledRunOutcome
    preflight_receipt_id: str | None = None
    capability_receipt_id: str | None = None
    source_coverage_artifact_ids: tuple[str, ...] = ()
    source_coverage_complete: bool = False
    source_checked_through: datetime | None = None
    digest_ids: tuple[str, ...] = ()
    watchlist_revision_ids: tuple[str, ...] = ()
    paper_replay_ids: tuple[str, ...] = ()
    degradation_reasons: tuple[str, ...] = ()
    notification_required: bool = False
    notification_reason: str | None = None
    next_watermark: str | None = None
    economic_write_count: Literal[0] = 0
    completed_at: datetime
    receipt_hash: str


class InvestorAnswerDraft(StrictModel):
    request_id: str
    conclusion: str
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    actions: tuple[str, ...]
    change_conditions: tuple[str, ...]
    actual_holding_section: str | None = None
    paper_holding_section: str | None = None
    evidence_as_of: datetime
    internal_metadata: dict[str, Any] = Field(default_factory=dict)


class InvestorAnswer(StrictModel):
    request_id: str
    conclusion: str
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    actions: tuple[str, ...]
    change_conditions: tuple[str, ...]
    actual_holding_section: str | None = None
    paper_holding_section: str | None = None
    evidence_as_of: datetime
    degraded: bool = False
    degradation_reason: str | None = None


class ShadowObservation(StrictModel):
    observation_id: str
    feature_id: str
    observed_at: datetime
    request_or_run_id: str
    baseline_artifact_id: str | None = None
    candidate_artifact_id: str | None = None
    baseline_action: str | None = None
    candidate_action: str | None = None
    label_change_only_action: bool = False
    required_capability_coverage: float
    prohibited_call_count: int = 0
    economic_write_count: int = 0
    latency_ms: int | None = None
    material_error: bool = False
    notes: tuple[str, ...] = ()


class ControlledLiveCheck(StrictModel):
    check_id: str
    check_type: str
    checked_at: datetime
    status: Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN"]
    evidence_ids: tuple[str, ...] = ()
    details: str | None = None


class ActivationGatePolicy(StrictModel):
    policy_id: str
    version: str
    required_recorded_scenarios: int = 68
    minimum_shadow_observations: int = 100
    minimum_shadow_days: int = 20
    maximum_material_error_rate: float = 0.0
    minimum_required_capability_coverage: float = 1.0
    maximum_prohibited_calls: int = 0
    maximum_economic_writes: int = 0
    require_current_macro_controlled_live: bool = True
    require_schedule_controlled_live: bool = True
    require_owner_approval: bool = True
    policy_hash: str


class ActivationAssessment(StrictModel):
    assessment_id: str
    feature_id: str
    assessed_at: datetime
    current_status: AdmissionStatus
    requested_status: AdmissionStatus
    eligible: bool
    gate_results: dict[str, bool]
    blockers: tuple[str, ...]
    recorded_scenario_count: int
    shadow_observation_count: int
    shadow_day_count: int
    material_error_rate: float | None = None
    label_change_only_action_count: int = 0
    economic_write_count: int = 0
    owner_approval_id: str | None = None
    policy_hash: str
    assessment_hash: str


class FeatureActivationReceipt(StrictModel):
    receipt_id: str
    feature_id: str
    previous_status: AdmissionStatus
    new_status: AdmissionStatus
    changed_at: datetime
    reason: str
    assessment_id: str | None = None
    owner_approval_id: str | None = None
    feature_flags: dict[str, bool]
    ledger_write_count: Literal[0] = 0
    receipt_hash: str
