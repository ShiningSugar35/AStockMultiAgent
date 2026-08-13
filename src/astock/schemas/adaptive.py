"""Admission-gated Phase 8 adaptive-research contracts."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.shadow import MarketRegime, Phase8AdmissionStatus, ShadowMetricInterval


class AdaptiveResearchCapabilityStatus(StrEnum):
    NOT_ENTERED_BY_DESIGN = "NOT_ENTERED_BY_DESIGN"
    AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL = "AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL"
    RULE_STATE_MACHINE_SHADOW_RESEARCH = "RULE_STATE_MACHINE_SHADOW_RESEARCH"
    OFFLINE_DYNAMIC_WEIGHT_RESEARCH = "OFFLINE_DYNAMIC_WEIGHT_RESEARCH"
    CONTEXTUAL_BANDIT_SHADOW_RESEARCH = "CONTEXTUAL_BANDIT_SHADOW_RESEARCH"
    RL_SANDBOX_RESEARCH = "RL_SANDBOX_RESEARCH"


class AdaptiveResearchNextStage(StrEnum):
    PHASE7_FORWARD_EVIDENCE_COLLECTION = "PHASE7_FORWARD_EVIDENCE_COLLECTION"
    EXPLICIT_RULE_RESEARCH_APPROVAL = "EXPLICIT_RULE_RESEARCH_APPROVAL"


class AdaptiveResearchPhaseStatus(StrEnum):
    NOT_ENTERED = "NOT_ENTERED"
    ENTERED_SHADOW_RESEARCH = "ENTERED_SHADOW_RESEARCH"


class AdaptiveResearchWorkflowStage(StrEnum):
    NOT_ENTERED = "NOT_ENTERED"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"


class AdaptiveSkillStabilityStatus(StrEnum):
    STABLE = "STABLE"
    UNSTABLE = "UNSTABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class AdaptiveBiasKind(StrEnum):
    LOOK_AHEAD = "LOOK_AHEAD"
    INPUT_LINEAGE = "INPUT_LINEAGE"
    UNPAIRED_SELECTION = "UNPAIRED_SELECTION"
    SINGLE_CASE_CONCENTRATION = "SINGLE_CASE_CONCENTRATION"
    MARKET_STATE_CONCENTRATION = "MARKET_STATE_CONCENTRATION"


class AdaptiveBiasStatus(StrEnum):
    CLEAR = "CLEAR"
    RISK = "RISK"
    BLOCKED = "BLOCKED"


class AdaptiveFailureCategory(StrEnum):
    INPUT_LINEAGE_MISSING = "INPUT_LINEAGE_MISSING"
    NEGATIVE_FOLD = "NEGATIVE_FOLD"
    HARMFUL_MARKET_STATE = "HARMFUL_MARKET_STATE"
    UNPAIRED_DECISIONS = "UNPAIRED_DECISIONS"
    DRAWDOWN_WORSENING = "DRAWDOWN_WORSENING"
    CONCENTRATED_INCREMENT = "CONCENTRATED_INCREMENT"


class AdaptiveAdjustmentRecommendation(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"
    KEEP_CURRENT_SKILL_CONTRACT = "KEEP_CURRENT_SKILL_CONTRACT"
    REVIEW_IN_NEW_SHADOW_EXPERIMENT = "REVIEW_IN_NEW_SHADOW_EXPERIMENT"


class AdaptiveResearchPrerequisiteCheck(AStockModel):
    """The exact Phase 8 entry gate frozen by TASK-PHASE8-FINAL-CLOSE."""

    real_forward_study: bool
    observation_months: Decimal = Field(ge=0)
    independent_decision_count: int = Field(ge=0)
    qualifying_market_state_count: int = Field(ge=0)
    qualifying_walk_forward_fold_count: int = Field(ge=0)
    required_observation_months: int = Field(ge=1)
    required_independent_decision_count: int = Field(ge=1)
    required_market_state_count: int = Field(ge=1)
    required_walk_forward_fold_count: int = Field(ge=1)
    gate_results: dict[str, bool] = Field(min_length=4, max_length=4)
    all_prerequisites_met: bool

    @model_validator(mode="after")
    def validate_gate_results(self) -> AdaptiveResearchPrerequisiteCheck:
        expected = {
            "MINIMUM_MARKET_STATES": (
                self.qualifying_market_state_count >= self.required_market_state_count
            ),
            "MINIMUM_INDEPENDENT_DECISIONS": (
                self.independent_decision_count >= self.required_independent_decision_count
            ),
            "MINIMUM_QUALIFYING_WALK_FORWARD_FOLDS": (
                self.qualifying_walk_forward_fold_count >= self.required_walk_forward_fold_count
            ),
            "MINIMUM_REAL_FORWARD_MONTHS": (
                self.real_forward_study
                and self.observation_months >= self.required_observation_months
            ),
        }
        if self.gate_results != expected:
            raise ValueError("adaptive research prerequisite results must be recalculated")
        if self.all_prerequisites_met != all(expected.values()):
            raise ValueError("adaptive research prerequisite summary is inconsistent")
        return self


class AdaptiveShadowResearchAuthorizationRequest(AStockModel):
    """Manual authorization to run Phase 8 research in an isolated shadow only."""

    study_id: str = Field(min_length=1)
    phase8_admission_id: str = Field(min_length=1)
    phase8_admission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1)
    approved_at: AwareDatetime
    approval_scope: Literal["RULE_STATE_MACHINE_SHADOW_RESEARCH"] = (
        "RULE_STATE_MACHINE_SHADOW_RESEARCH"
    )
    confirmation: Literal["I_APPROVE_PHASE8_SHADOW_RESEARCH_ONLY"]
    rationale: str = Field(min_length=1)
    production_weight_change_allowed: Literal[False] = False
    online_learning_allowed: Literal[False] = False
    main_paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


class AdaptiveShadowResearchAuthorization(AdaptiveShadowResearchAuthorizationRequest):
    authorization_id: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdaptiveResearchInputSummary(AStockModel):
    research_memo_artifact_ids: list[str] = Field(default_factory=list)
    research_memo_object_sha256s: list[str] = Field(default_factory=list)
    committee_decision_artifact_ids: list[str] = Field(default_factory=list)
    committee_decision_object_sha256s: list[str] = Field(default_factory=list)
    future_outcome_observation_ids: list[str] = Field(default_factory=list)
    future_outcome_object_sha256s: list[str] = Field(default_factory=list)
    lineage_complete: bool
    missing_input_reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_inputs(self) -> AdaptiveResearchInputSummary:
        for label, values in (
            ("research memo artifact ids", self.research_memo_artifact_ids),
            ("research memo hashes", self.research_memo_object_sha256s),
            ("committee decision artifact ids", self.committee_decision_artifact_ids),
            ("committee decision hashes", self.committee_decision_object_sha256s),
            ("future outcome ids", self.future_outcome_observation_ids),
            ("future outcome hashes", self.future_outcome_object_sha256s),
            ("missing input reason codes", self.missing_input_reason_codes),
        ):
            _require_sorted_unique(values, label)
        for values in (
            self.research_memo_object_sha256s,
            self.committee_decision_object_sha256s,
            self.future_outcome_object_sha256s,
        ):
            if any(
                len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
                for value in values
            ):
                raise ValueError("adaptive research input hashes must be lowercase SHA-256")
        if self.lineage_complete == bool(self.missing_input_reason_codes):
            raise ValueError("adaptive research input completeness disagrees with its reasons")
        if self.lineage_complete and not all(
            (
                self.research_memo_artifact_ids,
                self.committee_decision_artifact_ids,
                self.future_outcome_observation_ids,
            )
        ):
            raise ValueError("complete adaptive research inputs require all three input classes")
        return self


class AdaptiveSkillStability(AStockModel):
    paired_decision_count: int = Field(ge=0)
    qualifying_walk_forward_fold_count: int = Field(ge=0)
    positive_walk_forward_fold_count: int = Field(ge=0)
    positive_fold_ratio: Decimal = Field(ge=0, le=1)
    qualifying_market_state_count: int = Field(ge=0)
    harmful_market_states: list[MarketRegime]
    paired_net_return_delta: ShadowMetricInterval
    holm_adjusted_p_value: Decimal | None = Field(default=None, ge=0, le=1)
    required_independent_decision_count: int = Field(ge=1)
    required_walk_forward_fold_count: int = Field(ge=1)
    required_market_state_count: int = Field(ge=1)
    minimum_positive_fold_ratio: Decimal = Field(ge=0, le=1)
    stability_status: AdaptiveSkillStabilityStatus
    reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_stability(self) -> AdaptiveSkillStability:
        _require_sorted_unique(self.harmful_market_states, "harmful market states")
        _require_sorted_unique(self.reason_codes, "skill stability reason codes")
        if self.positive_walk_forward_fold_count > self.qualifying_walk_forward_fold_count:
            raise ValueError("positive folds cannot exceed qualifying folds")
        expected_ratio = (
            Decimal(self.positive_walk_forward_fold_count)
            / Decimal(self.qualifying_walk_forward_fold_count)
            if self.qualifying_walk_forward_fold_count
            else Decimal("0")
        )
        if self.positive_fold_ratio != expected_ratio:
            raise ValueError("positive fold ratio must be recalculated")
        interval_positive = (
            self.paired_net_return_delta.lower is not None
            and self.paired_net_return_delta.lower > 0
        )
        significant = (
            self.holm_adjusted_p_value is not None and self.holm_adjusted_p_value <= Decimal("0.05")
        )
        stable = (
            self.paired_decision_count >= self.required_independent_decision_count
            and self.qualifying_walk_forward_fold_count >= self.required_walk_forward_fold_count
            and self.positive_fold_ratio >= self.minimum_positive_fold_ratio
            and self.qualifying_market_state_count >= self.required_market_state_count
            and not self.harmful_market_states
            and interval_positive
            and significant
        )
        if self.stability_status is AdaptiveSkillStabilityStatus.STABLE and not stable:
            raise ValueError("stable Skill status requires every frozen stability gate")
        if self.stability_status is AdaptiveSkillStabilityStatus.UNSTABLE and stable:
            raise ValueError("an unstable Skill cannot pass every frozen stability gate")
        return self


class AdaptiveBiasFinding(AStockModel):
    bias_kind: AdaptiveBiasKind
    status: AdaptiveBiasStatus
    observed_value: Decimal | None = None
    threshold: Decimal | None = None
    reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reasons(self) -> AdaptiveBiasFinding:
        _require_sorted_unique(self.reason_codes, "adaptive bias reason codes")
        return self


class AdaptiveFailureCase(AStockModel):
    failure_case_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    category: AdaptiveFailureCategory
    fold_number: int | None = Field(default=None, ge=1)
    market_state: MarketRegime | None = None
    sample_count: int = Field(ge=0)
    effect_estimate: Decimal | None = None
    reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_context(self) -> AdaptiveFailureCase:
        _require_sorted_unique(self.reason_codes, "adaptive failure reason codes")
        if self.category is AdaptiveFailureCategory.NEGATIVE_FOLD:
            if self.fold_number is None or self.market_state is not None:
                raise ValueError("negative-fold failures require only a fold number")
        if self.category is AdaptiveFailureCategory.HARMFUL_MARKET_STATE:
            if self.market_state is None or self.fold_number is not None:
                raise ValueError("market-state failures require only a market state")
        return self


class AdaptiveSkillEvaluation(AStockModel):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    experimental_arm_id: str = Field(min_length=1)
    stability: AdaptiveSkillStability
    bias_findings: list[AdaptiveBiasFinding]
    failure_cases: list[AdaptiveFailureCase]
    adjustment_recommendation: AdaptiveAdjustmentRecommendation
    adjustment_recommended: bool
    recommendation_reason_codes: list[str] = Field(min_length=1)
    direct_production_change_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_evaluation(self) -> AdaptiveSkillEvaluation:
        bias_keys = [item.bias_kind for item in self.bias_findings]
        if bias_keys != sorted(set(bias_keys)):
            raise ValueError("Skill bias findings must be sorted and unique")
        failure_ids = [item.failure_case_id for item in self.failure_cases]
        _require_sorted_unique(failure_ids, "Skill failure case ids")
        _require_sorted_unique(
            self.recommendation_reason_codes,
            "Skill recommendation reason codes",
        )
        expected_adjustment = (
            self.adjustment_recommendation
            is AdaptiveAdjustmentRecommendation.REVIEW_IN_NEW_SHADOW_EXPERIMENT
        )
        if self.adjustment_recommended != expected_adjustment:
            raise ValueError("Skill adjustment boolean disagrees with its recommendation")
        if self.adjustment_recommendation is AdaptiveAdjustmentRecommendation.NOT_EVALUATED:
            raise ValueError("persisted Skill evaluations cannot be NOT_EVALUATED")
        return self


class AdaptiveResearchReport(AStockModel):
    report_id: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    phase8_status: AdaptiveResearchPhaseStatus
    workflow_stage: AdaptiveResearchWorkflowStage
    study_id: str | None = None
    shadow_report_id: str | None = None
    phase8_admission_id: str | None = None
    shadow_authorization_id: str | None = None
    phase7_audit_status: Literal["NOT_RUN", "PASS", "PARTIAL"]
    prerequisites: AdaptiveResearchPrerequisiteCheck
    inputs: AdaptiveResearchInputSummary
    skill_evaluations: list[AdaptiveSkillEvaluation]
    bias_findings: list[AdaptiveBiasFinding]
    failure_cases: list[AdaptiveFailureCase]
    adjustment_recommendation: AdaptiveAdjustmentRecommendation
    adjustment_recommended: bool
    recommendation_reason_codes: list[str] = Field(min_length=1)
    human_approval_required: Literal[True] = True
    human_approval_status: Literal["NOT_APPLICABLE", "PENDING"]
    direct_production_weight_change_allowed: Literal[False] = False
    online_learning_allowed: Literal[False] = False
    main_paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_boundary(self) -> AdaptiveResearchReport:
        skill_ids = [item.skill_id for item in self.skill_evaluations]
        _require_sorted_unique(skill_ids, "adaptive report Skill ids")
        bias_keys = [item.bias_kind for item in self.bias_findings]
        if bias_keys != sorted(set(bias_keys)):
            raise ValueError("adaptive report bias findings must be sorted and unique")
        failure_ids = [item.failure_case_id for item in self.failure_cases]
        _require_sorted_unique(failure_ids, "adaptive report failure case ids")
        _require_sorted_unique(
            self.recommendation_reason_codes,
            "adaptive report recommendation reason codes",
        )
        expected_adjustment = (
            self.adjustment_recommendation
            is AdaptiveAdjustmentRecommendation.REVIEW_IN_NEW_SHADOW_EXPERIMENT
        )
        if self.adjustment_recommended != expected_adjustment:
            raise ValueError("adaptive report adjustment boolean is inconsistent")
        entered = self.phase8_status is AdaptiveResearchPhaseStatus.ENTERED_SHADOW_RESEARCH
        if entered:
            if (
                not self.prerequisites.all_prerequisites_met
                or self.phase7_audit_status != "PASS"
                or not self.study_id
                or not self.shadow_report_id
                or not self.phase8_admission_id
                or not self.shadow_authorization_id
                or not self.inputs.lineage_complete
                or not self.skill_evaluations
            ):
                raise ValueError("entered Phase 8 reports require complete audited lineage")
            if (
                self.workflow_stage is not AdaptiveResearchWorkflowStage.AWAITING_HUMAN_APPROVAL
                or self.human_approval_status != "PENDING"
                or self.adjustment_recommendation is AdaptiveAdjustmentRecommendation.NOT_EVALUATED
            ):
                raise ValueError("evaluated Phase 8 reports must await human approval")
        else:
            if (
                self.workflow_stage is not AdaptiveResearchWorkflowStage.NOT_ENTERED
                or self.shadow_authorization_id
                or self.skill_evaluations
                or self.bias_findings
                or self.failure_cases
                or self.adjustment_recommendation
                is not AdaptiveAdjustmentRecommendation.NOT_EVALUATED
                or self.adjustment_recommended
                or self.human_approval_status != "NOT_APPLICABLE"
            ):
                raise ValueError("NOT_ENTERED reports cannot claim adaptive evaluation")
        return self


class AdaptiveResearchStatusReport(AStockModel):
    boundary_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    implementation_status: Literal[
        "IMPLEMENTED_DISABLED_BOUNDARY",
        "IMPLEMENTED_ADMISSION_GATED_SHADOW_FEEDBACK",
    ] = "IMPLEMENTED_ADMISSION_GATED_SHADOW_FEEDBACK"
    shadow_policy_version: str = Field(min_length=1)
    study_id: str | None = None
    shadow_report_id: str | None = None
    shadow_report_object_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    phase8_admission_id: str | None = None
    phase8_admission_object_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    phase8_admission_status: Phase8AdmissionStatus | None = None
    phase7_audit_status: Literal["NOT_RUN", "PASS", "PARTIAL"]
    user_admission_status: Literal["NOT_ADMITTED"] = "NOT_ADMITTED"
    capability_status: AdaptiveResearchCapabilityStatus
    observation_months: Decimal = Field(ge=0)
    independent_decision_count: int = Field(ge=0)
    mature_observation_count: int = Field(ge=0)
    qualifying_walk_forward_fold_count: int = Field(ge=0)
    qualifying_market_regime_count: int = Field(ge=0)
    required_observation_months: int = Field(ge=1)
    required_independent_decision_count: int = Field(ge=1)
    required_walk_forward_fold_count: int = Field(ge=1)
    required_decisions_per_fold: int = Field(ge=1)
    required_market_regime_count: int = Field(ge=1)
    required_decisions_per_regime: int = Field(ge=1)
    observation_month_gap: Decimal = Field(ge=0)
    independent_decision_gap: int = Field(ge=0)
    qualifying_walk_forward_fold_gap: int = Field(ge=0)
    qualifying_market_regime_gap: int = Field(ge=0)
    reason_codes: list[str] = Field(min_length=1)
    adaptive_weights_enabled: Literal[False] = False
    online_learning_allowed: Literal[False] = False
    main_paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False
    next_permitted_stage: AdaptiveResearchNextStage
    status_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_disabled_boundary(self) -> AdaptiveResearchStatusReport:
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("adaptive research reason codes must be sorted and unique")
        expected_gaps = (
            max(
                Decimal(self.required_observation_months) - self.observation_months,
                Decimal("0"),
            ),
            max(
                self.required_independent_decision_count - self.independent_decision_count,
                0,
            ),
            max(
                self.required_walk_forward_fold_count - self.qualifying_walk_forward_fold_count,
                0,
            ),
            max(
                self.required_market_regime_count - self.qualifying_market_regime_count,
                0,
            ),
        )
        actual_gaps = (
            self.observation_month_gap,
            self.independent_decision_gap,
            self.qualifying_walk_forward_fold_gap,
            self.qualifying_market_regime_gap,
        )
        if actual_gaps != expected_gaps:
            raise ValueError("adaptive research gaps must match frozen Phase 7 thresholds")
        active_statuses = {
            AdaptiveResearchCapabilityStatus.RULE_STATE_MACHINE_SHADOW_RESEARCH,
            AdaptiveResearchCapabilityStatus.OFFLINE_DYNAMIC_WEIGHT_RESEARCH,
            AdaptiveResearchCapabilityStatus.CONTEXTUAL_BANDIT_SHADOW_RESEARCH,
            AdaptiveResearchCapabilityStatus.RL_SANDBOX_RESEARCH,
        }
        if self.capability_status in active_statuses:
            raise ValueError("the status boundary cannot report active adaptive research")
        ready_for_approval = (
            self.phase8_admission_status
            is Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
            and self.phase7_audit_status == "PASS"
        )
        waiting = (
            self.capability_status
            is AdaptiveResearchCapabilityStatus.AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL
        )
        if ready_for_approval != waiting:
            raise ValueError(
                "adaptive rule research approval requires eligible audited Phase 7 evidence"
            )
        if waiting and any(actual_gaps):
            raise ValueError("adaptive rule research approval requires zero sample gaps")
        expected_next = (
            AdaptiveResearchNextStage.EXPLICIT_RULE_RESEARCH_APPROVAL
            if ready_for_approval
            else AdaptiveResearchNextStage.PHASE7_FORWARD_EVIDENCE_COLLECTION
        )
        if self.next_permitted_stage is not expected_next:
            raise ValueError("adaptive research next stage disagrees with its admission gate")
        if bool(self.phase8_admission_id) != bool(self.phase8_admission_object_sha256):
            raise ValueError("adaptive admission identity and object hash must appear together")
        if bool(self.shadow_report_id) != bool(self.shadow_report_object_sha256):
            raise ValueError("adaptive report identity and object hash must appear together")
        return self


def _require_sorted_unique(values: Sequence[object], label: str) -> None:
    rendered = [item.value if isinstance(item, StrEnum) else str(item) for item in values]
    if rendered != sorted(set(rendered)):
        raise ValueError(f"{label} must be sorted and unique")


__all__ = [
    "AdaptiveAdjustmentRecommendation",
    "AdaptiveBiasFinding",
    "AdaptiveBiasKind",
    "AdaptiveBiasStatus",
    "AdaptiveFailureCase",
    "AdaptiveFailureCategory",
    "AdaptiveResearchCapabilityStatus",
    "AdaptiveResearchInputSummary",
    "AdaptiveResearchNextStage",
    "AdaptiveResearchPhaseStatus",
    "AdaptiveResearchPrerequisiteCheck",
    "AdaptiveResearchReport",
    "AdaptiveResearchStatusReport",
    "AdaptiveResearchWorkflowStage",
    "AdaptiveShadowResearchAuthorization",
    "AdaptiveShadowResearchAuthorizationRequest",
    "AdaptiveSkillEvaluation",
    "AdaptiveSkillStability",
    "AdaptiveSkillStabilityStatus",
]
