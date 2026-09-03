"""Read-only Phase 8 capability status derived from frozen Phase 7 evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, TypedDict

from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.schemas import (
    AdaptiveResearchCapabilityStatus,
    AdaptiveResearchNextStage,
    AdaptiveResearchStatusReport,
    MarketRegime,
    Phase8AdmissionStatus,
    ShadowEvaluationPolicy,
    ShadowEvaluationReport,
)
from astock.shadow import ShadowEvaluationService

Phase7AuditStatus = Literal["NOT_RUN", "PASS", "PARTIAL"]


class _AdaptiveCounts(TypedDict):
    observation_months: Decimal
    independent_decision_count: int
    mature_observation_count: int
    qualifying_walk_forward_fold_count: int
    qualifying_market_regime_count: int


class _AdaptiveThresholds(TypedDict):
    shadow_policy_version: str
    required_observation_months: int
    required_independent_decision_count: int
    required_walk_forward_fold_count: int
    required_decisions_per_fold: int
    required_market_regime_count: int
    required_decisions_per_regime: int


class AdaptiveResearchStatusService:
    """Expose a fail-closed Phase 8 boundary without creating research state."""

    BOUNDARY_VERSION = "adaptive-research-boundary-v1"
    ENGINE_VERSION = "adaptive-research-status-engine-v1"

    def __init__(
        self,
        shadow_service: ShadowEvaluationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.shadow_service = shadow_service
        self.repository = shadow_service.repository
        self.object_store = shadow_service.object_store
        self._default_thresholds = self._policy_thresholds(shadow_service.configured_policy)
        self._clock = clock or (lambda: datetime.now(UTC))

    def status(self, study_id: str | None = None) -> AdaptiveResearchStatusReport:
        try:
            return self._status(study_id)
        except (
            AStockError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            sqlite3.Error,
        ):
            return self._build(
                study_id=study_id,
                phase7_audit_status="PARTIAL",
                reason_codes=[
                    "BLOCKED_BY_INTEGRITY",
                    "PHASE7_INPUT_UNAVAILABLE_OR_INVALID",
                ],
            )

    def _status(self, study_id: str | None) -> AdaptiveResearchStatusReport:
        study_summary = (
            self.repository.study_summary(study_id)
            if study_id is not None
            else self.repository.latest_study_summary(
                policy_version=self.shadow_service.configured_policy.policy_version
            )
        )
        if study_summary is None:
            return self._build(
                study_id=study_id,
                phase7_audit_status="NOT_RUN",
                reason_codes=["PHASE7_STUDY_NOT_RUN"],
            )
        resolved_study_id = str(study_summary["study_id"])
        thresholds = self._study_thresholds(study_summary)
        audit = self.shadow_service.audit(resolved_study_id)
        audit_status = self._audit_status(audit.get("status"))
        reasons: set[str] = set()
        if audit_status != "PASS":
            reasons.add("BLOCKED_BY_INTEGRITY")
            reasons.add("PHASE7_AUDIT_NOT_PASS")
            audit_findings = audit.get("finding_codes", [])
            if isinstance(audit_findings, list):
                reasons.update(f"PHASE7_AUDIT__{item}" for item in audit_findings)

        report_summary = self.repository.latest_report_summary(resolved_study_id)
        if report_summary is None:
            reasons.add("PHASE7_EVALUATION_NOT_RUN")
            reasons.add("PHASE8_ADMISSION_NOT_AVAILABLE")
            return self._build(
                study_id=resolved_study_id,
                phase7_audit_status=audit_status,
                reason_codes=sorted(reasons),
                thresholds=thresholds,
            )
        report_id = str(report_summary["report_id"])
        report_object_hash = str(report_summary["object_hash"])
        if not self.object_store.verify(report_object_hash):
            reasons.add("PHASE7_REPORT_OBJECT_INVALID")
            reasons.add("BLOCKED_BY_INTEGRITY")
            return self._build(
                study_id=resolved_study_id,
                phase7_audit_status="PARTIAL",
                reason_codes=sorted(reasons),
                thresholds=thresholds,
            )
        report = self.repository.get_report(report_id)
        if report is None or report.report_id != report_id or report.study_id != resolved_study_id:
            reasons.add("BLOCKED_BY_INTEGRITY")
            reasons.add("PHASE7_REPORT_LINEAGE_INVALID")
            return self._build(
                study_id=resolved_study_id,
                phase7_audit_status="PARTIAL",
                reason_codes=sorted(reasons),
                thresholds=thresholds,
            )

        counts = self._report_counts(report)
        thresholds = self._report_thresholds(report)
        admission_summary = self.repository.latest_admission_summary(resolved_study_id)
        if admission_summary is None:
            reasons.add("PHASE8_ADMISSION_NOT_AVAILABLE")
            return self._build(
                study_id=resolved_study_id,
                shadow_report_id=report_id,
                shadow_report_object_sha256=report_object_hash,
                phase7_audit_status=audit_status,
                reason_codes=sorted(reasons),
                thresholds=thresholds,
                **counts,
            )
        admission_id = str(admission_summary["admission_id"])
        admission_object_hash = str(admission_summary["object_hash"])
        if not self.object_store.verify(admission_object_hash):
            reasons.add("PHASE8_ADMISSION_OBJECT_INVALID")
            reasons.add("BLOCKED_BY_INTEGRITY")
            return self._build(
                study_id=resolved_study_id,
                shadow_report_id=report_id,
                shadow_report_object_sha256=report_object_hash,
                phase7_audit_status="PARTIAL",
                reason_codes=sorted(reasons),
                thresholds=thresholds,
                **counts,
            )
        admission = self.repository.get_admission(admission_id)
        if admission is None:
            reasons.add("BLOCKED_BY_INTEGRITY")
            reasons.add("PHASE8_ADMISSION_OBJECT_INVALID")
            return self._build(
                study_id=resolved_study_id,
                shadow_report_id=report_id,
                shadow_report_object_sha256=report_object_hash,
                phase7_audit_status="PARTIAL",
                reason_codes=sorted(reasons),
                thresholds=thresholds,
                **counts,
            )
        if (
            admission.study_id != resolved_study_id
            or admission.shadow_report_id != report.report_id
            or admission.shadow_report_sha256 != report.report_sha256
        ):
            reasons.add("BLOCKED_BY_INTEGRITY")
            reasons.add("PHASE8_ADMISSION_LINEAGE_INVALID")
            audit_status = "PARTIAL"

        eligible = (
            admission.status is Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
            and audit_status == "PASS"
        )
        if eligible:
            reasons.add("EXPLICIT_RULE_RESEARCH_APPROVAL_REQUIRED")
        else:
            reasons.add("PHASE8_ADMISSION_NOT_ELIGIBLE")
            reasons.update(admission.reason_codes)
            reasons.update(
                f"PHASE8_GATE_FAILED__{key}"
                for key, passed in admission.gate_results.items()
                if not passed
            )
        return self._build(
            study_id=resolved_study_id,
            shadow_report_id=report_id,
            shadow_report_object_sha256=report_object_hash,
            phase8_admission_id=admission_id,
            phase8_admission_object_sha256=admission_object_hash,
            phase8_admission_status=admission.status,
            phase7_audit_status=audit_status,
            capability_status=(
                AdaptiveResearchCapabilityStatus.AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL
                if eligible
                else AdaptiveResearchCapabilityStatus.NOT_ENTERED_BY_DESIGN
            ),
            next_permitted_stage=(
                AdaptiveResearchNextStage.EXPLICIT_RULE_RESEARCH_APPROVAL
                if eligible
                else AdaptiveResearchNextStage.PHASE7_FORWARD_EVIDENCE_COLLECTION
            ),
            reason_codes=sorted(reasons),
            thresholds=thresholds,
            **counts,
        )

    @staticmethod
    def _report_counts(report: ShadowEvaluationReport) -> _AdaptiveCounts:
        qualifying_folds = max(
            (
                sum(
                    fold.independent_decision_count >= report.required_decisions_per_fold
                    for fold in comparison.folds
                )
                for comparison in report.comparisons
            ),
            default=0,
        )
        qualifying_regimes = sum(
            regime is not MarketRegime.UNCLASSIFIED
            and count >= report.required_decisions_per_regime
            for regime, count in report.market_regime_counts.items()
        )
        return {
            "observation_months": report.observation_months,
            "independent_decision_count": report.independent_decision_count,
            "mature_observation_count": report.mature_observation_count,
            "qualifying_walk_forward_fold_count": qualifying_folds,
            "qualifying_market_regime_count": qualifying_regimes,
        }

    @staticmethod
    def _policy_thresholds(policy: ShadowEvaluationPolicy) -> _AdaptiveThresholds:
        return {
            "shadow_policy_version": policy.policy_version,
            "required_observation_months": policy.phase8_observation_months,
            "required_independent_decision_count": (policy.minimum_independent_decisions),
            "required_walk_forward_fold_count": policy.minimum_walk_forward_folds,
            "required_decisions_per_fold": policy.minimum_decisions_per_fold,
            "required_market_regime_count": policy.minimum_regime_count,
            "required_decisions_per_regime": policy.minimum_decisions_per_regime,
        }

    @staticmethod
    def _report_thresholds(report: ShadowEvaluationReport) -> _AdaptiveThresholds:
        return {
            "shadow_policy_version": report.policy_version,
            "required_observation_months": (report.required_phase8_observation_months),
            "required_independent_decision_count": (report.required_independent_decisions),
            "required_walk_forward_fold_count": (report.required_walk_forward_folds),
            "required_decisions_per_fold": report.required_decisions_per_fold,
            "required_market_regime_count": report.required_regime_count,
            "required_decisions_per_regime": report.required_decisions_per_regime,
        }

    def _study_thresholds(
        self,
        study_summary: dict[str, object],
    ) -> _AdaptiveThresholds:
        policy_version = study_summary.get("policy_version")
        if policy_version is None:
            return self._default_thresholds
        if not isinstance(policy_version, str) or not policy_version:
            raise ValueError("shadow study policy version is invalid")
        if policy_version == self._default_thresholds["shadow_policy_version"]:
            return self._default_thresholds
        policy_summary = self.repository.policy_summary(policy_version)
        if policy_summary is None:
            raise ValueError("shadow study policy is unavailable")
        object_hash = str(policy_summary["object_hash"])
        if not self.object_store.verify(object_hash):
            raise ValueError("shadow study policy object is invalid")
        policy = self.repository.get_policy(policy_version)
        if policy is None or policy.policy_version != policy_version:
            raise ValueError("shadow study policy lineage is invalid")
        return self._policy_thresholds(policy)

    @staticmethod
    def _audit_status(value: object) -> Phase7AuditStatus:
        if value == "NOT_RUN":
            return "NOT_RUN"
        if value == "PASS":
            return "PASS"
        return "PARTIAL"

    def _build(
        self,
        *,
        study_id: str | None,
        phase7_audit_status: Phase7AuditStatus,
        reason_codes: list[str],
        shadow_report_id: str | None = None,
        shadow_report_object_sha256: str | None = None,
        phase8_admission_id: str | None = None,
        phase8_admission_object_sha256: str | None = None,
        phase8_admission_status: Phase8AdmissionStatus | None = None,
        capability_status: AdaptiveResearchCapabilityStatus = (
            AdaptiveResearchCapabilityStatus.NOT_ENTERED_BY_DESIGN
        ),
        next_permitted_stage: AdaptiveResearchNextStage = (
            AdaptiveResearchNextStage.PHASE7_FORWARD_EVIDENCE_COLLECTION
        ),
        observation_months: Decimal = Decimal("0"),
        independent_decision_count: int = 0,
        mature_observation_count: int = 0,
        qualifying_walk_forward_fold_count: int = 0,
        qualifying_market_regime_count: int = 0,
        thresholds: _AdaptiveThresholds | None = None,
    ) -> AdaptiveResearchStatusReport:
        resolved_thresholds = thresholds or self._default_thresholds
        observation_month_gap = max(
            Decimal(resolved_thresholds["required_observation_months"]) - observation_months,
            Decimal("0"),
        )
        independent_decision_gap = max(
            resolved_thresholds["required_independent_decision_count"] - independent_decision_count,
            0,
        )
        qualifying_walk_forward_fold_gap = max(
            resolved_thresholds["required_walk_forward_fold_count"]
            - qualifying_walk_forward_fold_count,
            0,
        )
        qualifying_market_regime_gap = max(
            resolved_thresholds["required_market_regime_count"] - qualifying_market_regime_count,
            0,
        )
        identity = {
            "boundary_version": self.BOUNDARY_VERSION,
            "engine_version": self.ENGINE_VERSION,
            "implementation_status": "IMPLEMENTED_DISABLED_BOUNDARY",
            **resolved_thresholds,
            "study_id": study_id,
            "shadow_report_id": shadow_report_id,
            "shadow_report_object_sha256": shadow_report_object_sha256,
            "phase8_admission_id": phase8_admission_id,
            "phase8_admission_object_sha256": phase8_admission_object_sha256,
            "phase8_admission_status": phase8_admission_status,
            "phase7_audit_status": phase7_audit_status,
            "user_admission_status": "NOT_ADMITTED",
            "capability_status": capability_status,
            "observation_months": observation_months,
            "independent_decision_count": independent_decision_count,
            "mature_observation_count": mature_observation_count,
            "qualifying_walk_forward_fold_count": (qualifying_walk_forward_fold_count),
            "qualifying_market_regime_count": qualifying_market_regime_count,
            "observation_month_gap": observation_month_gap,
            "independent_decision_gap": independent_decision_gap,
            "qualifying_walk_forward_fold_gap": (qualifying_walk_forward_fold_gap),
            "qualifying_market_regime_gap": qualifying_market_regime_gap,
            "reason_codes": sorted(set(reason_codes)),
            "adaptive_weights_enabled": False,
            "online_learning_allowed": False,
            "main_paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
            "next_permitted_stage": next_permitted_stage,
        }
        return AdaptiveResearchStatusReport(
            boundary_version=self.BOUNDARY_VERSION,
            engine_version=self.ENGINE_VERSION,
            implementation_status="IMPLEMENTED_DISABLED_BOUNDARY",
            shadow_policy_version=resolved_thresholds["shadow_policy_version"],
            study_id=study_id,
            shadow_report_id=shadow_report_id,
            shadow_report_object_sha256=shadow_report_object_sha256,
            phase8_admission_id=phase8_admission_id,
            phase8_admission_object_sha256=phase8_admission_object_sha256,
            phase8_admission_status=phase8_admission_status,
            phase7_audit_status=phase7_audit_status,
            capability_status=capability_status,
            observation_months=observation_months,
            independent_decision_count=independent_decision_count,
            mature_observation_count=mature_observation_count,
            qualifying_walk_forward_fold_count=(qualifying_walk_forward_fold_count),
            qualifying_market_regime_count=qualifying_market_regime_count,
            required_observation_months=resolved_thresholds["required_observation_months"],
            required_independent_decision_count=resolved_thresholds[
                "required_independent_decision_count"
            ],
            required_walk_forward_fold_count=resolved_thresholds[
                "required_walk_forward_fold_count"
            ],
            required_decisions_per_fold=resolved_thresholds["required_decisions_per_fold"],
            required_market_regime_count=resolved_thresholds["required_market_regime_count"],
            required_decisions_per_regime=resolved_thresholds["required_decisions_per_regime"],
            observation_month_gap=observation_month_gap,
            independent_decision_gap=independent_decision_gap,
            qualifying_walk_forward_fold_gap=(qualifying_walk_forward_fold_gap),
            qualifying_market_regime_gap=qualifying_market_regime_gap,
            reason_codes=sorted(set(reason_codes)),
            adaptive_weights_enabled=False,
            online_learning_allowed=False,
            main_paper_ledger_write_allowed=False,
            broker_execution_allowed=False,
            next_permitted_stage=next_permitted_stage,
            status_sha256=content_hash(identity),
            created_at=self._now(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("adaptive research clock must be timezone-aware")
        return value.astimezone(UTC)


__all__ = ["AdaptiveResearchStatusService"]
