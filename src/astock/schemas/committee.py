"""Frozen-input committee, counter-case, decision, and trade-protocol contracts."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.runs import CommitteeAccessPolicy, ContextBudgetReport


class CommitteeDecisionScope(StrEnum):
    NEW_CANDIDATE = "NEW_CANDIDATE"
    PAPER_POSITION = "PAPER_POSITION"
    MONITORING_POSITION = "MONITORING_POSITION"


class CommitteeVerdict(StrEnum):
    REJECT = "REJECT"
    NEEDS_INFO = "NEEDS_INFO"
    WATCH = "WATCH"
    PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
    PAPER_HOLD = "PAPER_HOLD"
    PAPER_EXIT = "PAPER_EXIT"


class CommitteeInputRole(StrEnum):
    PRIMARY = "PRIMARY"
    SPECIALIST = "SPECIALIST"
    FINANCIAL = "FINANCIAL"
    LIFECYCLE = "LIFECYCLE"
    STATE = "STATE"
    COUNTER_CASE = "COUNTER_CASE"


class CommitteeMemberRole(StrEnum):
    BASE_CASE = "BASE_CASE"
    SERENITY_DELTA = "SERENITY_DELTA"
    ZHIHU_EXPERT_DELTA = "ZHIHU_EXPERT_DELTA"
    FINANCIAL_INTEGRITY = "FINANCIAL_INTEGRITY"


class CommitteeProtocolStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class TradeProtocolOutcome(StrEnum):
    WATCH = "WATCH"
    REJECT = "REJECT"
    NEEDS_INFO = "NEEDS_INFO"
    APPROVE_SIMULATION = "APPROVE_SIMULATION"


class CommitteeEntryOrderType(StrEnum):
    NONE = "NONE"
    PAPER_MARKET = "PAPER_MARKET"
    PAPER_LIMIT = "PAPER_LIMIT"


class CommitteeTaskStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class CommitteeNarrativeMode(StrEnum):
    DISABLED = "DISABLED"
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    CODEX_FROZEN_INPUT_ONLY = "CODEX_FROZEN_INPUT_ONLY"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PROVIDER_COST_EXCEEDED = "PROVIDER_COST_EXCEEDED"


class CounterCaseTriggerCode(StrEnum):
    HIGH_PLANNED_POSITION = "HIGH_PLANNED_POSITION"
    BASE_SPECIALIST_CONFLICT = "BASE_SPECIALIST_CONFLICT"
    FINANCIAL_ANOMALY = "FINANCIAL_ANOMALY"
    HIGH_RETURN_LOW_COVERAGE = "HIGH_RETURN_LOW_COVERAGE"
    LOW_COVERAGE_DOMAIN = "LOW_COVERAGE_DOMAIN"
    SPECIALIST_DISAGREEMENT = "SPECIALIST_DISAGREEMENT"
    MATERIAL_NEW_DISCLOSURE = "MATERIAL_NEW_DISCLOSURE"
    INVALIDATION_NEAR = "INVALIDATION_NEAR"
    PORTFOLIO_RISK_CHANGE = "PORTFOLIO_RISK_CHANGE"


class CommitteeArtifactReference(AStockModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: CommitteeInputRole


class CommitteeMemberBinding(AStockModel):
    role: CommitteeMemberRole
    artifact_id: str = Field(min_length=1)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CommitteeRatioRange(AStockModel):
    lower: Decimal = Field(ge=Decimal("-1"), le=Decimal("10"), allow_inf_nan=False)
    upper: Decimal = Field(ge=Decimal("-1"), le=Decimal("10"), allow_inf_nan=False)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> CommitteeRatioRange:
        if self.lower > self.upper:
            raise ValueError("committee range lower bound cannot exceed upper bound")
        _require_sorted_unique(self.evidence_ids, "committee range evidence ids")
        return self


class CommitteeCoverageMetrics(AStockModel):
    data_coverage: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_coverage: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    specialist_coverage: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    pit_coverage: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    liquidity_score: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> CommitteeCoverageMetrics:
        _require_sorted_unique(self.evidence_ids, "committee coverage evidence ids")
        return self


class CommitteePortfolioRiskState(AStockModel):
    current_total_exposure: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    post_decision_total_exposure: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    current_industry_exposure: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    post_decision_industry_exposure: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    max_abs_correlation: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    portfolio_drawdown: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    consecutive_loss_count: int = Field(ge=0)
    material_announcement_freeze: bool = False
    data_anomaly_freeze: bool = False
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_portfolio_risk(self) -> CommitteePortfolioRiskState:
        _require_sorted_unique(
            self.evidence_ids,
            "committee portfolio-risk evidence ids",
        )
        return self


class CommitteeProtocolDraft(AStockModel):
    strategy_id: str = Field(min_length=1)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    earliest_executable_time: AwareDatetime
    entry_rule: str = Field(min_length=1)
    entry_order_type: CommitteeEntryOrderType
    position_size_rule: str = Field(min_length=1)
    price_stop_rule: str = Field(min_length=1)
    volatility_stop_rule: str = Field(min_length=1)
    trailing_stop_rule: str = Field(min_length=1)
    time_stop_rule: str = Field(min_length=1)
    thesis_invalidation_rule: str = Field(min_length=1)
    take_profit_rule: str = Field(min_length=1)
    review_events: list[str] = Field(min_length=1)
    max_holding_period_days: int = Field(ge=1)
    cost_model_version: str = Field(min_length=1)
    fill_model_version: str = Field(min_length=1)
    evidence_snapshot_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    requires_user_confirmation: Literal[True] = True
    broker_commands: Literal[None] = None

    @model_validator(mode="after")
    def validate_protocol_draft(self) -> CommitteeProtocolDraft:
        _require_sorted_unique(self.review_events, "protocol review events")
        _require_sorted_unique(self.evidence_ids, "protocol evidence ids")
        if any(not key or not value for key, value in self.skill_versions.items()):
            raise ValueError("protocol Skill versions require non-empty ids and versions")
        return self


class CommitteeAssessment(AStockModel):
    company_id: str = Field(min_length=1)
    scope: CommitteeDecisionScope
    as_of: AwareDatetime
    expected_return_range: CommitteeRatioRange
    downside_range: CommitteeRatioRange
    confidence: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    coverage: CommitteeCoverageMetrics
    portfolio_risk: CommitteePortfolioRiskState
    tradable: bool
    market_data_quality_pass: bool
    key_fact_community_only: bool = False
    qualified_or_adverse_audit_risk: bool = False
    explicitly_prohibited: bool = False
    manual_emergency_stop: bool = False
    leverage_requested: bool = False
    thesis_invalidated: bool = False
    base_specialist_conflict: bool = False
    multiple_specialist_disagreement: bool = False
    material_new_disclosure: bool = False
    invalidation_near_trigger: bool = False
    portfolio_risk_changed: bool = False
    current_position: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    requested_position: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    holding_horizon_days: int = Field(ge=1)
    review_at: AwareDatetime
    support_evidence_ids: list[str] = Field(min_length=1)
    signal_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    optional_narrative_requested: bool = False
    estimated_provider_cost_cny: Decimal = Field(default=Decimal("0"), ge=0)
    protocol: CommitteeProtocolDraft

    @model_validator(mode="after")
    def validate_assessment(self) -> CommitteeAssessment:
        if self.downside_range.upper > 0:
            raise ValueError("downside range cannot contain a positive upper bound")
        if self.review_at <= self.as_of:
            raise ValueError("committee review time must follow as_of")
        if self.protocol.earliest_executable_time < self.as_of:
            raise ValueError("earliest executable time cannot precede the signal time")
        if self.scope is CommitteeDecisionScope.NEW_CANDIDATE and self.current_position != 0:
            raise ValueError("new candidates cannot already have a position")
        if self.scope is not CommitteeDecisionScope.NEW_CANDIDATE and self.current_position <= 0:
            raise ValueError("position decisions require a positive current position")
        if self.portfolio_risk.current_total_exposure < self.current_position:
            raise ValueError("total exposure cannot be below the current company position")
        if self.portfolio_risk.current_industry_exposure < self.current_position:
            raise ValueError("industry exposure cannot be below the current company position")
        if self.portfolio_risk.post_decision_total_exposure < self.requested_position:
            raise ValueError("post-decision total exposure cannot be below the requested position")
        if self.portfolio_risk.post_decision_industry_exposure < self.requested_position:
            raise ValueError(
                "post-decision industry exposure cannot be below the requested position"
            )
        _require_sorted_unique(
            self.support_evidence_ids,
            "committee assessment support evidence ids",
        )
        required_signal_evidence = {
            name
            for name in (
                "key_fact_community_only",
                "qualified_or_adverse_audit_risk",
                "explicitly_prohibited",
                "manual_emergency_stop",
                "leverage_requested",
                "thesis_invalidated",
                "base_specialist_conflict",
                "multiple_specialist_disagreement",
                "material_new_disclosure",
                "invalidation_near_trigger",
                "portfolio_risk_changed",
            )
            if bool(getattr(self, name))
        }
        if not self.tradable:
            required_signal_evidence.add("tradable")
        if not self.market_data_quality_pass:
            required_signal_evidence.add("market_data_quality_pass")
        for key, values in self.signal_evidence_ids.items():
            if not key:
                raise ValueError("committee signal evidence keys cannot be empty")
            _require_sorted_unique(values, f"committee signal {key} evidence ids")
        missing = required_signal_evidence - {
            key for key, values in self.signal_evidence_ids.items() if values
        }
        if missing:
            raise ValueError(f"material committee signals require evidence ids: {sorted(missing)}")
        return self


class CommitteeAssessmentSnapshot(CommitteeAssessment):
    assessment_id: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CounterCaseDraft(AStockModel):
    challenged_claim_ids: list[str] = Field(min_length=1)
    alternative_explanations: list[str] = Field(min_length=1)
    downside_paths: list[str] = Field(min_length=1)
    missing_evidence_codes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    estimated_tokens: int = Field(ge=0)
    estimated_minutes: int = Field(ge=0)
    estimated_cost_cny: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_counter_case(self) -> CounterCaseDraft:
        for label, values in (
            ("challenged claim", self.challenged_claim_ids),
            ("alternative explanation", self.alternative_explanations),
            ("downside path", self.downside_paths),
            ("missing evidence code", self.missing_evidence_codes),
            ("evidence", self.evidence_ids),
        ):
            _require_sorted_unique(values, f"counter-case {label} values")
        return self


class CounterCasePack(CounterCaseDraft):
    counter_case_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    scope: CommitteeDecisionScope
    as_of: AwareDatetime
    trigger_codes: list[CounterCaseTriggerCode] = Field(min_length=1)
    frozen_input_hashes: list[str] = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_pack(self) -> CounterCasePack:
        _require_sorted_unique(self.trigger_codes, "counter-case trigger codes")
        _require_sorted_unique(
            self.frozen_input_hashes,
            "counter-case frozen input hashes",
        )
        return self


class CommitteeRuleConfig(AStockModel):
    rule_set_id: str = Field(min_length=1)
    rules_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    effective_from: AwareDatetime
    min_data_coverage: Decimal = Field(ge=0, le=1)
    min_evidence_coverage: Decimal = Field(ge=0, le=1)
    min_specialist_coverage: Decimal = Field(ge=0, le=1)
    min_pit_coverage: Decimal = Field(ge=0, le=1)
    min_liquidity_score: Decimal = Field(ge=0, le=1)
    max_single_position: Decimal = Field(gt=0, le=1)
    high_position_threshold: Decimal = Field(gt=0, le=1)
    min_expected_return_lower: Decimal = Field(ge=Decimal("-1"), le=Decimal("10"))
    high_potential_return_lower: Decimal = Field(ge=Decimal("-1"), le=Decimal("10"))
    max_downside_absolute: Decimal = Field(ge=0, le=1)
    low_coverage_margin: Decimal = Field(ge=0, le=1)
    max_total_exposure: Decimal = Field(gt=0, le=1)
    max_industry_exposure: Decimal = Field(gt=0, le=1)
    max_abs_correlation: Decimal = Field(ge=0, le=1)
    max_portfolio_drawdown: Decimal = Field(gt=0, le=1)
    max_consecutive_losses: int = Field(ge=1)
    financial_integrity_required: bool = True
    financial_reject_rule_ids: list[str]
    max_context_bytes: int = Field(ge=1)
    max_estimated_text_tokens: int = Field(ge=1)
    provider_enabled: bool = False
    provider_cost_ceiling_cny: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_rules(self) -> CommitteeRuleConfig:
        if self.high_position_threshold > self.max_single_position:
            raise ValueError("high-position threshold cannot exceed the position cap")
        if self.high_potential_return_lower < self.min_expected_return_lower:
            raise ValueError("high-return threshold cannot be below the eligibility threshold")
        if self.max_industry_exposure > self.max_total_exposure:
            raise ValueError("industry exposure cap cannot exceed total exposure cap")
        _require_sorted_unique(
            self.financial_reject_rule_ids,
            "committee financial reject rule ids",
        )
        return self


class CommitteeDecisionRequest(AStockModel):
    artifact_references: list[CommitteeArtifactReference] = Field(min_length=1)
    member_bindings: list[CommitteeMemberBinding] = Field(default_factory=list)
    assessment: CommitteeAssessment
    counter_case: CounterCaseDraft | None = None
    access_policy: CommitteeAccessPolicy

    @model_validator(mode="after")
    def validate_request(self) -> CommitteeDecisionRequest:
        artifact_ids = [item.artifact_id for item in self.artifact_references]
        hashes = [item.object_sha256 for item in self.artifact_references]
        if artifact_ids != sorted(set(artifact_ids)):
            raise ValueError("committee artifact references must be sorted and unique")
        if len(hashes) != len(set(hashes)):
            raise ValueError("committee artifact object hashes must be unique")
        if self.access_policy.frozen_artifact_hashes != sorted(hashes):
            raise ValueError("committee access policy must exactly bind the frozen inputs")
        if self.member_bindings:
            roles = [item.role for item in self.member_bindings]
            if roles != sorted(set(roles), key=lambda item: item.value):
                raise ValueError("committee member bindings must be sorted and unique by role")
            if set(roles) != set(CommitteeMemberRole):
                raise ValueError("investment committee requires every mandatory member role")
            reference_by_id = {
                item.artifact_id: item.object_sha256 for item in self.artifact_references
            }
            if any(
                reference_by_id.get(item.artifact_id) != item.object_sha256
                for item in self.member_bindings
            ):
                raise ValueError("committee member bindings must reference exact frozen inputs")
        return self


class CommitteeInputBundle(AStockModel):
    bundle_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    scope: CommitteeDecisionScope
    as_of: AwareDatetime
    artifact_references: list[CommitteeArtifactReference] = Field(min_length=2)
    member_bindings: list[CommitteeMemberBinding] = Field(default_factory=list)
    access_policy: CommitteeAccessPolicy
    rules_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    skill_versions: dict[str, str]
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bundle(self) -> CommitteeInputBundle:
        ids = [item.artifact_id for item in self.artifact_references]
        hashes = [item.object_sha256 for item in self.artifact_references]
        if ids != sorted(set(ids)):
            raise ValueError("committee bundle artifacts must be sorted and unique")
        if len(hashes) != len(set(hashes)):
            raise ValueError("committee bundle object hashes must be unique")
        if self.access_policy.frozen_artifact_hashes != sorted(hashes):
            raise ValueError("committee bundle policy must bind every frozen artifact")
        if self.member_bindings:
            roles = [item.role for item in self.member_bindings]
            if roles != sorted(set(roles), key=lambda item: item.value):
                raise ValueError("committee bundle member roles must be sorted and unique")
            reference_by_id = {
                item.artifact_id: item.object_sha256 for item in self.artifact_references
            }
            if any(
                reference_by_id.get(item.artifact_id) != item.object_sha256
                for item in self.member_bindings
            ):
                raise ValueError("committee bundle members must bind exact frozen inputs")
        return self


class CommitteeBudgetReport(AStockModel):
    context: ContextBudgetReport
    within_limit: bool
    narrative_mode: CommitteeNarrativeMode
    provider_estimated_cost_cny: Decimal = Field(ge=0)
    provider_cost_ceiling_cny: Decimal = Field(ge=0)
    degradation_codes: list[str]

    @model_validator(mode="after")
    def validate_offline_budget(self) -> CommitteeBudgetReport:
        if (
            self.context.full_documents_to_open
            or self.context.expected_browser_steps
            or self.context.expected_mcp_calls
            or self.context.expected_api_calls
        ):
            raise ValueError("committee context budget cannot include external access")
        _require_sorted_unique(
            self.degradation_codes,
            "committee budget degradation codes",
        )
        return self


class CommitteeInvestigationTask(AStockModel):
    task_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    importance: Literal["BLOCKING"] = "BLOCKING"
    decision_impact: Literal["VERDICT_BLOCKED"] = "VERDICT_BLOCKED"
    priority: Literal["HIGH"] = "HIGH"
    expected_minutes: int = Field(ge=1)
    sources: list[str] = Field(min_length=1)
    search_terms: list[str] = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    required_materials: list[str] = Field(min_length=1)
    support_signal: str = Field(min_length=1)
    refute_signal: str = Field(min_length=1)
    stop_condition: str = Field(min_length=1)
    fallback_evidence: list[str] = Field(default_factory=list)
    status: CommitteeTaskStatus = CommitteeTaskStatus.OPEN
    resolution_artifact_id: str | None = None
    resolution_object_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_task(self) -> CommitteeInvestigationTask:
        for label, values in (
            ("source", self.sources),
            ("search term", self.search_terms),
            ("step", self.steps),
            ("required material", self.required_materials),
            ("fallback evidence", self.fallback_evidence),
        ):
            _require_sorted_unique(values, f"committee task {label} values")
        if self.status is CommitteeTaskStatus.RESOLVED:
            if not self.resolution_artifact_id or not self.resolution_object_sha256:
                raise ValueError("resolved committee tasks require a frozen resolution artifact")
        elif self.resolution_artifact_id or self.resolution_object_sha256:
            raise ValueError("open/cancelled committee tasks cannot claim a resolution artifact")
        return self


class DecisionPack(AStockModel):
    decision_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    scope: CommitteeDecisionScope
    as_of: AwareDatetime
    rules_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    frozen_input_hashes: list[str] = Field(min_length=2)
    verdict: CommitteeVerdict
    expected_return_range: CommitteeRatioRange
    downside_range: CommitteeRatioRange
    confidence: Decimal = Field(ge=0, le=1)
    hard_blocks: list[str]
    needs_info_task_ids: list[str]
    counter_case_trigger_codes: list[CounterCaseTriggerCode]
    counter_case_id: str | None = None
    current_position: Decimal = Field(ge=0, le=1)
    max_position: Decimal = Field(ge=0, le=1)
    review_at: AwareDatetime
    rationale_codes: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    context_budget: CommitteeBudgetReport
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    narrative_can_override: Literal[False] = False
    requires_user_confirmation: Literal[True] = True

    @model_validator(mode="after")
    def validate_decision(self) -> DecisionPack:
        for label, values in (
            ("frozen input hash", self.frozen_input_hashes),
            ("hard block", self.hard_blocks),
            ("needs-info task", self.needs_info_task_ids),
            ("counter-case trigger", self.counter_case_trigger_codes),
            ("rationale", self.rationale_codes),
            ("evidence", self.evidence_ids),
        ):
            _require_sorted_unique(values, f"decision {label} values")
        if self.verdict is CommitteeVerdict.NEEDS_INFO and not self.needs_info_task_ids:
            raise ValueError("NEEDS_INFO decisions require investigation tasks")
        if self.verdict is not CommitteeVerdict.NEEDS_INFO and self.needs_info_task_ids:
            raise ValueError("only NEEDS_INFO decisions may contain open task ids")
        if self.counter_case_id and not self.counter_case_trigger_codes:
            raise ValueError("counter-case ids require trigger codes")
        return self


class TradeProtocol(AStockModel):
    protocol_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: str = Field(min_length=1)
    verdict: CommitteeVerdict
    protocol_status: CommitteeProtocolStatus
    blocking_codes: list[str]
    strategy_id: str = Field(min_length=1)
    skill_versions: dict[str, str]
    signal_time: AwareDatetime
    earliest_executable_time: AwareDatetime
    holding_horizon_days: int = Field(ge=1)
    entry_rule: str = Field(min_length=1)
    entry_order_type: CommitteeEntryOrderType
    position_size_rule: str = Field(min_length=1)
    price_stop_rule: str = Field(min_length=1)
    volatility_stop_rule: str = Field(min_length=1)
    trailing_stop_rule: str = Field(min_length=1)
    time_stop_rule: str = Field(min_length=1)
    thesis_invalidation_rule: str = Field(min_length=1)
    take_profit_rule: str = Field(min_length=1)
    review_events: list[str] = Field(min_length=1)
    max_holding_period_days: int = Field(ge=1)
    cost_model_version: str = Field(min_length=1)
    fill_model_version: str = Field(min_length=1)
    evidence_snapshot_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    effective_from: AwareDatetime
    requires_user_confirmation: Literal[True] = True
    broker_execution_allowed: Literal[False] = False
    paper_simulation_allowed: bool = False
    ledger_write_allowed: bool = False

    @property
    def outcome(self) -> TradeProtocolOutcome:
        if self.verdict is CommitteeVerdict.REJECT:
            return TradeProtocolOutcome.REJECT
        if self.verdict is CommitteeVerdict.NEEDS_INFO:
            return TradeProtocolOutcome.NEEDS_INFO
        if self.verdict in {
            CommitteeVerdict.PAPER_ELIGIBLE,
            CommitteeVerdict.PAPER_EXIT,
        }:
            return TradeProtocolOutcome.APPROVE_SIMULATION
        return TradeProtocolOutcome.WATCH

    @model_validator(mode="after")
    def validate_protocol(self) -> TradeProtocol:
        active_verdicts = {
            CommitteeVerdict.PAPER_ELIGIBLE,
            CommitteeVerdict.PAPER_EXIT,
        }
        expected_status = (
            CommitteeProtocolStatus.ACTIVE
            if self.verdict in active_verdicts
            else CommitteeProtocolStatus.BLOCKED
        )
        if self.protocol_status is not expected_status:
            raise ValueError("trade protocol status does not match its committee verdict")
        if self.protocol_status is CommitteeProtocolStatus.BLOCKED and not self.blocking_codes:
            raise ValueError("blocked trade protocols require blocking codes")
        execution_verdict = self.verdict in {
            CommitteeVerdict.PAPER_ELIGIBLE,
            CommitteeVerdict.PAPER_EXIT,
        }
        if self.paper_simulation_allowed != execution_verdict:
            raise ValueError("paper simulation gate does not match the committee verdict")
        if self.ledger_write_allowed != self.paper_simulation_allowed:
            raise ValueError("paper ledger gate must equal the simulation gate")
        if self.earliest_executable_time < self.signal_time:
            raise ValueError("trade protocol cannot execute before its signal")
        _require_sorted_unique(self.blocking_codes, "trade protocol blocking codes")
        _require_sorted_unique(self.review_events, "trade protocol review events")
        _require_sorted_unique(self.evidence_ids, "trade protocol evidence ids")
        return self


class CommitteePlanReport(AStockModel):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prospective_bundle_id: str = Field(min_length=1)
    prospective_decision_id: str = Field(min_length=1)
    verdict: CommitteeVerdict
    hard_blocks: list[str]
    counter_case_trigger_codes: list[CounterCaseTriggerCode]
    missing_counter_case: bool
    investigation_reason_codes: list[str]
    context_budget: CommitteeBudgetReport
    persistent_writes: Literal[False] = False

    @model_validator(mode="after")
    def validate_plan(self) -> CommitteePlanReport:
        for label, values in (
            ("hard block", self.hard_blocks),
            ("counter-case trigger", self.counter_case_trigger_codes),
            ("investigation reason", self.investigation_reason_codes),
        ):
            _require_sorted_unique(values, f"committee plan {label} values")
        return self


def _require_sorted_unique(values: Sequence[object], label: str) -> None:
    rendered = [item.value if isinstance(item, StrEnum) else str(item) for item in values]
    if rendered != sorted(set(rendered)):
        raise ValueError(f"{label} must be sorted and unique")


__all__ = [
    "CommitteeAccessPolicy",
    "CommitteeArtifactReference",
    "CommitteeAssessment",
    "CommitteeAssessmentSnapshot",
    "CommitteeBudgetReport",
    "CommitteeCoverageMetrics",
    "CommitteeDecisionRequest",
    "CommitteeDecisionScope",
    "CommitteeEntryOrderType",
    "CommitteeInputBundle",
    "CommitteeInputRole",
    "CommitteeMemberBinding",
    "CommitteeMemberRole",
    "CommitteeInvestigationTask",
    "CommitteeNarrativeMode",
    "CommitteePlanReport",
    "CommitteePortfolioRiskState",
    "CommitteeProtocolDraft",
    "CommitteeProtocolStatus",
    "CommitteeRatioRange",
    "CommitteeRuleConfig",
    "CommitteeTaskStatus",
    "CommitteeVerdict",
    "CounterCaseDraft",
    "CounterCasePack",
    "CounterCaseTriggerCode",
    "DecisionPack",
    "TradeProtocol",
    "TradeProtocolOutcome",
]
