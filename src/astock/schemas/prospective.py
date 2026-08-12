"""Phase 11 prospective all-trials and statistical-governance contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.shadow import ShadowArtifactReference


class ProspectiveFunnelStage(StrEnum):
    SEED = "SEED"
    PROMOTION = "PROMOTION"
    CANDIDATE = "CANDIDATE"
    COMMITTEE = "COMMITTEE"
    FORMAL_ASSIGNMENT = "FORMAL_ASSIGNMENT"


class ProspectiveFunnelOutcome(StrEnum):
    SEED_REJECTED = "SEED_REJECTED"
    PROMOTION_BLOCKED = "PROMOTION_BLOCKED"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    COMMITTEE_REJECT = "COMMITTEE_REJECT"
    COMMITTEE_NEEDS_INFO = "COMMITTEE_NEEDS_INFO"
    COMMITTEE_WATCH = "COMMITTEE_WATCH"
    COMMITTEE_APPROVE_SIMULATION = "COMMITTEE_APPROVE_SIMULATION"
    FORMAL_ASSIGNMENT_REGISTERED = "FORMAL_ASSIGNMENT_REGISTERED"


class TrialClusterType(StrEnum):
    STOCK = "STOCK"
    INDUSTRY = "INDUSTRY"
    THEME = "THEME"
    DECISION_DATE = "DECISION_DATE"
    SHARED_CATALYST = "SHARED_CATALYST"


class EndpointRole(StrEnum):
    PRIMARY = "PRIMARY"
    DIAGNOSTIC = "DIAGNOSTIC"


class ProspectiveEndpointDefinition(AStockModel):
    endpoint_id: str = Field(min_length=1)
    role: EndpointRole
    horizon_days: int | None = Field(default=None, ge=1, le=252)
    adjustment: str = Field(min_length=1)
    higher_is_better: bool


class ProspectiveGovernanceConfig(AStockModel):
    schema_version: str = "prospective-governance-config-v1"
    config_id: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    effective_from: AwareDatetime
    independence_contract_version: str = Field(min_length=1)
    market_regime_rule_version: str = Field(min_length=1)
    statistics_version: str = Field(min_length=1)
    endpoints: list[ProspectiveEndpointDefinition] = Field(min_length=6)
    walk_forward_folds: int = Field(default=5, ge=2, le=20)
    minimum_independence_units: int = Field(default=100, ge=1)
    purge_horizon_sessions: int = Field(default=60, ge=1, le=252)
    embargo_sessions: int = Field(default=5, ge=0, le=60)
    cluster_bootstrap_replicates: int = Field(default=2000, ge=100, le=10000)
    hard_cluster_types: list[TrialClusterType] = Field(
        default_factory=lambda: [
            TrialClusterType.DECISION_DATE,
            TrialClusterType.SHARED_CATALYST,
            TrialClusterType.STOCK,
        ]
    )
    dsr_pbo_diagnostic_only: Literal[True] = True
    automatic_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_config(self) -> ProspectiveGovernanceConfig:
        endpoint_ids = [item.endpoint_id for item in self.endpoints]
        if endpoint_ids != sorted(set(endpoint_ids)):
            raise ValueError("prospective endpoints must be sorted and unique")
        required = {
            "20D_SECTOR_ADJUSTED_RETURN": EndpointRole.PRIMARY,
            "60D_BENCHMARK_ADJUSTED_RETURN": EndpointRole.PRIMARY,
            "60D_MAE": EndpointRole.PRIMARY,
            "PROCESS_GROUNDING": EndpointRole.PRIMARY,
            "DECISION_CALIBRATION": EndpointRole.PRIMARY,
            "5D_RETURN": EndpointRole.DIAGNOSTIC,
        }
        actual = {item.endpoint_id: item.role for item in self.endpoints}
        if any(actual.get(key) is not role for key, role in required.items()):
            raise ValueError("prospective config is missing a preregistered endpoint or role")
        if self.hard_cluster_types != sorted(
            set(self.hard_cluster_types), key=lambda item: item.value
        ):
            raise ValueError("hard cluster types must be sorted and unique")
        return self


class ProspectiveTrialRecordRequest(AStockModel):
    schema_version: str = "prospective-trial-record-request-v1"
    study_id: str = Field(min_length=1)
    governance_config_artifact_id: str = Field(min_length=1)
    governance_config_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_trial_id: str = Field(min_length=1)
    funnel_event_id: str = Field(min_length=1)
    company_id: str | None = Field(default=None, min_length=1)
    decision_time: AwareDatetime
    stage: ProspectiveFunnelStage
    outcome: ProspectiveFunnelOutcome
    independence_unit_id: str = Field(min_length=1)
    cluster_ids: dict[TrialClusterType, list[str]]
    frozen_inputs: list[ShadowArtifactReference] = Field(default_factory=list)
    market_regime_id: str | None = None
    market_regime_rule_version: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    formal_assignment_id: str | None = None
    formal_trade_event: Literal[False] = False

    @model_validator(mode="after")
    def validate_trial(self) -> ProspectiveTrialRecordRequest:
        if self.created_at > self.decision_time:
            raise ValueError("prospective trial metadata must be frozen by the decision time")
        for cluster_type in TrialClusterType:
            values = self.cluster_ids.get(cluster_type, [])
            if values != sorted(set(values)):
                raise ValueError("prospective trial cluster ids must be sorted and unique")
        if not self.cluster_ids.get(TrialClusterType.DECISION_DATE):
            raise ValueError("prospective trials require a decision-date cluster")
        if self.company_id and not self.cluster_ids.get(TrialClusterType.STOCK):
            raise ValueError("company trials require a stock cluster")
        artifact_ids = [item.artifact_id for item in self.frozen_inputs]
        if artifact_ids != sorted(set(artifact_ids)):
            raise ValueError("prospective frozen inputs must be sorted and unique")
        if any(item.available_at > self.decision_time for item in self.frozen_inputs):
            raise ValueError("prospective trials cannot freeze future-visible inputs")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("prospective trial reason codes must be sorted and unique")
        regime_fields = (self.market_regime_id, self.market_regime_rule_version)
        if (regime_fields[0] is None) != (regime_fields[1] is None):
            raise ValueError("market regime id and rule version must be frozen together")
        if self.stage is ProspectiveFunnelStage.FORMAL_ASSIGNMENT:
            if self.outcome is not ProspectiveFunnelOutcome.FORMAL_ASSIGNMENT_REGISTERED:
                raise ValueError("formal assignment stage requires its registered outcome")
            if not self.formal_assignment_id:
                raise ValueError("formal assignment funnel record requires assignment id")
        elif self.formal_assignment_id is not None:
            raise ValueError("non-assignment funnel events cannot claim formal assignment identity")
        return self


class ProspectiveTrialRecord(ProspectiveTrialRecordRequest):
    schema_version: str = "prospective-trial-record-v1"
    trial_event_id: str = Field(min_length=1)
    frozen_input_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial_event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_at: AwareDatetime


class ProspectiveAllTrialsReport(AStockModel):
    schema_version: str = "prospective-all-trials-report-v1"
    report_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    governance_config_artifact_id: str = Field(min_length=1)
    event_count: int = Field(ge=0)
    research_trial_count: int = Field(ge=0)
    independence_unit_count: int = Field(ge=0)
    stage_counts: dict[ProspectiveFunnelStage, int]
    outcome_counts: dict[ProspectiveFunnelOutcome, int]
    cluster_counts: dict[TrialClusterType, int]
    formal_assignment_link_count: int = Field(ge=0)
    formal_trade_event_count: Literal[0] = 0
    finding_codes: list[str]
    input_trial_event_sha256s: list[str]
    prospective_forward_count_mutated: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> ProspectiveAllTrialsReport:
        if self.finding_codes != sorted(set(self.finding_codes)):
            raise ValueError("all-trials finding codes must be sorted and unique")
        if self.input_trial_event_sha256s != sorted(set(self.input_trial_event_sha256s)):
            raise ValueError("all-trials input hashes must be sorted and unique")
        return self


class PurgedFoldDefinition(AStockModel):
    fold_number: int = Field(ge=1)
    test_independence_unit_ids: list[str]
    train_independence_unit_ids: list[str]
    purged_independence_unit_ids: list[str]
    embargoed_independence_unit_ids: list[str]

    @model_validator(mode="after")
    def validate_fold(self) -> PurgedFoldDefinition:
        groups = (
            self.test_independence_unit_ids,
            self.train_independence_unit_ids,
            self.purged_independence_unit_ids,
            self.embargoed_independence_unit_ids,
        )
        for values in groups:
            if values != sorted(set(values)):
                raise ValueError("purged fold ids must be sorted and unique")
        if set(self.test_independence_unit_ids) & set(self.train_independence_unit_ids):
            raise ValueError("test and train units cannot overlap")
        return self


class ProspectiveStatisticsPlan(AStockModel):
    schema_version: str = "prospective-statistics-plan-v1"
    plan_id: str = Field(min_length=1)
    study_id: str = Field(min_length=1)
    governance_config_artifact_id: str = Field(min_length=1)
    primary_endpoint_ids: list[str]
    diagnostic_endpoint_ids: list[str]
    folds: list[PurgedFoldDefinition]
    independence_unit_count: int = Field(ge=0)
    cluster_counts: dict[TrialClusterType, int]
    paired_comparison_required: Literal[True] = True
    cluster_bootstrap_required: Literal[True] = True
    dsr_pbo_diagnostic_only: Literal[True] = True
    independence_sample_floor_reached: bool
    finding_codes: list[str]


class SelectionBiasDiagnosticStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"
    COMPUTED = "COMPUTED"


class SelectionBiasDiagnostic(AStockModel):
    schema_version: str = "selection-bias-diagnostic-v1"
    diagnostic_id: str = Field(min_length=1)
    selection_candidate_count: int = Field(ge=1)
    observation_count: int = Field(ge=0)
    status: SelectionBiasDiagnosticStatus
    deflated_sharpe_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    probability_of_backtest_overfitting: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    finding_codes: list[str]
    pass_badge_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_diagnostic(self) -> SelectionBiasDiagnostic:
        if (
            self.selection_candidate_count == 1
            and self.status is not SelectionBiasDiagnosticStatus.NOT_APPLICABLE
        ):
            raise ValueError("DSR/PBO must be not-applicable without repeated selection")
        if self.status is SelectionBiasDiagnosticStatus.COMPUTED and (
            self.deflated_sharpe_ratio is None or self.probability_of_backtest_overfitting is None
        ):
            raise ValueError("computed selection-bias diagnostic requires DSR and PBO")
        if self.finding_codes != sorted(set(self.finding_codes)):
            raise ValueError("selection-bias finding codes must be sorted and unique")
        return self


__all__ = [
    "EndpointRole",
    "ProspectiveAllTrialsReport",
    "ProspectiveEndpointDefinition",
    "ProspectiveFunnelOutcome",
    "ProspectiveFunnelStage",
    "ProspectiveGovernanceConfig",
    "ProspectiveStatisticsPlan",
    "ProspectiveTrialRecord",
    "ProspectiveTrialRecordRequest",
    "PurgedFoldDefinition",
    "SelectionBiasDiagnostic",
    "SelectionBiasDiagnosticStatus",
    "TrialClusterType",
]
