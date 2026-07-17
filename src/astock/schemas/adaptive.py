"""Admission-gated Phase 8 adaptive-research boundary contracts."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.shadow import Phase8AdmissionStatus


class AdaptiveResearchCapabilityStatus(StrEnum):
    NOT_ENTERED_BY_DESIGN = "NOT_ENTERED_BY_DESIGN"
    AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL = (
        "AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL"
    )
    RULE_STATE_MACHINE_SHADOW_RESEARCH = "RULE_STATE_MACHINE_SHADOW_RESEARCH"
    OFFLINE_DYNAMIC_WEIGHT_RESEARCH = "OFFLINE_DYNAMIC_WEIGHT_RESEARCH"
    CONTEXTUAL_BANDIT_SHADOW_RESEARCH = "CONTEXTUAL_BANDIT_SHADOW_RESEARCH"
    RL_SANDBOX_RESEARCH = "RL_SANDBOX_RESEARCH"


class AdaptiveResearchNextStage(StrEnum):
    PHASE7_FORWARD_EVIDENCE_COLLECTION = "PHASE7_FORWARD_EVIDENCE_COLLECTION"
    EXPLICIT_RULE_RESEARCH_APPROVAL = "EXPLICIT_RULE_RESEARCH_APPROVAL"


class AdaptiveResearchStatusReport(AStockModel):
    boundary_version: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    implementation_status: Literal["IMPLEMENTED_DISABLED_BOUNDARY"] = (
        "IMPLEMENTED_DISABLED_BOUNDARY"
    )
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
                self.required_independent_decision_count
                - self.independent_decision_count,
                0,
            ),
            max(
                self.required_walk_forward_fold_count
                - self.qualifying_walk_forward_fold_count,
                0,
            ),
            max(
                self.required_market_regime_count
                - self.qualifying_market_regime_count,
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
            raise ValueError("the P8.0 boundary cannot report active adaptive research")
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
        if bool(self.phase8_admission_id) != bool(
            self.phase8_admission_object_sha256
        ):
            raise ValueError("adaptive admission identity and object hash must appear together")
        if bool(self.shadow_report_id) != bool(self.shadow_report_object_sha256):
            raise ValueError("adaptive report identity and object hash must appear together")
        return self


__all__ = [
    "AdaptiveResearchCapabilityStatus",
    "AdaptiveResearchNextStage",
    "AdaptiveResearchStatusReport",
]
