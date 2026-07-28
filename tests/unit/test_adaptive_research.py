from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from astock.adaptive import AdaptiveResearchStatusService
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    AdaptiveResearchCapabilityStatus,
    AdaptiveResearchNextStage,
    AdaptiveResearchStatusReport,
    MarketRegime,
    Phase8AdmissionReport,
    Phase8AdmissionStatus,
    ShadowComparisonResult,
    ShadowEvaluationReport,
    ShadowFoldResult,
)
from astock.shadow import ShadowEvaluationService, load_shadow_evaluation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2028, 7, 18, tzinfo=UTC)
REPORT_OBJECT_HASH = "1" * 64
ADMISSION_OBJECT_HASH = "2" * 64
POLICY = load_shadow_evaluation_policy(
    PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
)


class _FakeObjectStore:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid

    def verify(self, object_hash: str) -> bool:
        return self.valid and len(object_hash) == 64


class _FakeRepository:
    def __init__(
        self,
        *,
        report: ShadowEvaluationReport | None,
        admission: Phase8AdmissionReport | None,
        has_study: bool = True,
    ) -> None:
        self.report = report
        self.admission = admission
        self.has_study = has_study

    def study_summary(self, study_id: str) -> dict[str, object] | None:
        return {"study_id": study_id} if self.has_study else None

    def latest_study_summary(self) -> dict[str, object] | None:
        return {"study_id": "study:ready"} if self.has_study else None

    def latest_report_summary(self, study_id: str) -> dict[str, object] | None:
        if self.report is None:
            return None
        return {
            "study_id": study_id,
            "report_id": self.report.report_id,
            "object_hash": REPORT_OBJECT_HASH,
        }

    def get_report(self, report_id: str) -> ShadowEvaluationReport | None:
        if self.report is None or self.report.report_id != report_id:
            return None
        return self.report

    def latest_admission_summary(self, study_id: str) -> dict[str, object] | None:
        if self.admission is None:
            return None
        return {
            "study_id": study_id,
            "admission_id": self.admission.admission_id,
            "object_hash": ADMISSION_OBJECT_HASH,
        }

    def get_admission(self, admission_id: str) -> Phase8AdmissionReport | None:
        if self.admission is None or self.admission.admission_id != admission_id:
            return None
        return self.admission


def _report() -> ShadowEvaluationReport:
    folds = [
        ShadowFoldResult.model_construct(independent_decision_count=20)
        for _ in range(5)
    ]
    comparison = ShadowComparisonResult.model_construct(folds=folds)
    return ShadowEvaluationReport.model_construct(
        report_id="report:ready",
        study_id="study:ready",
        report_sha256="a" * 64,
        policy_version=POLICY.policy_version,
        required_phase8_observation_months=12,
        required_independent_decisions=100,
        required_regime_count=3,
        required_walk_forward_folds=5,
        observation_months=Decimal("12.25"),
        independent_decision_count=100,
        mature_observation_count=600,
        required_decisions_per_fold=20,
        required_decisions_per_regime=30,
        comparisons=[comparison],
        market_regime_counts={
            MarketRegime.HIGH_VOL_BULL: 30,
            MarketRegime.PANIC: 30,
            MarketRegime.RANGE: 40,
            MarketRegime.UNCLASSIFIED: 100,
        },
    )


def _admission(
    status: Phase8AdmissionStatus = (
        Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
    ),
) -> Phase8AdmissionReport:
    eligible = status is Phase8AdmissionStatus.ELIGIBLE_RULE_STATE_MACHINE_RESEARCH
    return Phase8AdmissionReport.model_construct(
        admission_id="admission:ready",
        study_id="study:ready",
        shadow_report_id="report:ready",
        shadow_report_sha256="a" * 64,
        status=status,
        gate_results={"MINIMUM_SAMPLE": eligible},
        reason_codes=(
            ["PHASE8_RULE_STATE_MACHINE_RESEARCH_ELIGIBLE"]
            if eligible
            else ["PHASE8_INDEPENDENT_SAMPLE_INSUFFICIENT"]
        ),
    )


def _service(
    *,
    report: ShadowEvaluationReport | None,
    admission: Phase8AdmissionReport | None,
    audit_status: str = "PASS",
    audit_findings: list[str] | None = None,
    objects_valid: bool = True,
) -> AdaptiveResearchStatusService:
    repository = _FakeRepository(report=report, admission=admission)
    shadow = SimpleNamespace(
        repository=repository,
        object_store=_FakeObjectStore(valid=objects_valid),
        configured_policy=POLICY,
        audit=lambda _study_id: {
            "status": audit_status,
            "finding_codes": audit_findings or [],
        },
    )
    return AdaptiveResearchStatusService(
        cast(ShadowEvaluationService, shadow),
        clock=lambda: NOW,
    )


def _table_counts(state: StateStore) -> dict[str, int]:
    with closing(state.connect()) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in names
        }


def test_no_study_status_is_stable_fail_closed_and_has_no_database_writes(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    shadow = ShadowEvaluationService(
        state,
        ObjectStore(tmp_path / "objects"),
        load_shadow_evaluation_policy(
            PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
        ),
    )
    before = _table_counts(state)
    first = AdaptiveResearchStatusService(shadow, clock=lambda: NOW).status(
        "study:not-run"
    )
    second = AdaptiveResearchStatusService(
        shadow,
        clock=lambda: NOW + timedelta(days=1),
    ).status("study:not-run")
    after = _table_counts(state)

    assert before == after
    assert first.implementation_status == "IMPLEMENTED_DISABLED_BOUNDARY"
    assert (
        first.capability_status
        is AdaptiveResearchCapabilityStatus.NOT_ENTERED_BY_DESIGN
    )
    assert first.phase7_audit_status == "NOT_RUN"
    assert first.reason_codes == ["PHASE7_STUDY_NOT_RUN"]
    assert first.shadow_policy_version == "shadow-evaluation-policy-v2"
    assert first.observation_month_gap == Decimal("12")
    assert first.independent_decision_gap == 100
    assert first.qualifying_walk_forward_fold_gap == 5
    assert first.qualifying_market_regime_gap == 3
    assert (
        first.next_permitted_stage
        is AdaptiveResearchNextStage.PHASE7_FORWARD_EVIDENCE_COLLECTION
    )
    assert not first.adaptive_weights_enabled
    assert not first.online_learning_allowed
    assert not first.main_paper_ledger_write_allowed
    assert not first.broker_execution_allowed
    assert first.status_sha256 == second.status_sha256
    assert first.status_sha256 == content_hash(
        first.model_dump(
            mode="python",
            exclude={"schema_version", "created_at", "status_sha256"},
        )
    )


def test_missing_report_and_admission_explain_the_forward_evidence_gap() -> None:
    no_report = _service(report=None, admission=None).status("study:ready")
    assert no_report.reason_codes == [
        "PHASE7_EVALUATION_NOT_RUN",
        "PHASE8_ADMISSION_NOT_AVAILABLE",
    ]

    report_without_admission = _service(report=_report(), admission=None).status(
        "study:ready"
    )
    assert report_without_admission.reason_codes == [
        "PHASE8_ADMISSION_NOT_AVAILABLE"
    ]
    assert report_without_admission.observation_months == Decimal("12.25")
    assert report_without_admission.qualifying_walk_forward_fold_count == 5
    assert report_without_admission.qualifying_market_regime_count == 3


def test_noneligible_admission_preserves_deterministic_gate_reasons() -> None:
    result = _service(
        report=_report(),
        admission=_admission(
            Phase8AdmissionStatus.NOT_ELIGIBLE_INSUFFICIENT_SAMPLE
        ),
    ).status("study:ready")

    assert (
        result.capability_status
        is AdaptiveResearchCapabilityStatus.NOT_ENTERED_BY_DESIGN
    )
    assert result.phase8_admission_status is (
        Phase8AdmissionStatus.NOT_ELIGIBLE_INSUFFICIENT_SAMPLE
    )
    assert "PHASE8_ADMISSION_NOT_ELIGIBLE" in result.reason_codes
    assert "PHASE8_INDEPENDENT_SAMPLE_INSUFFICIENT" in result.reason_codes
    assert "PHASE8_GATE_FAILED__MINIMUM_SAMPLE" in result.reason_codes


def test_eligible_admission_still_requires_explicit_versioned_approval() -> None:
    result = _service(report=_report(), admission=_admission()).status("study:ready")

    assert result.capability_status is (
        AdaptiveResearchCapabilityStatus.AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL
    )
    assert (
        result.next_permitted_stage
        is AdaptiveResearchNextStage.EXPLICIT_RULE_RESEARCH_APPROVAL
    )
    assert result.reason_codes == ["EXPLICIT_RULE_RESEARCH_APPROVAL_REQUIRED"]
    assert result.shadow_report_id == "report:ready"
    assert result.phase8_admission_id == "admission:ready"
    assert result.observation_months == Decimal("12.25")
    assert result.independent_decision_count == 100
    assert result.mature_observation_count == 600
    assert result.qualifying_walk_forward_fold_count == 5
    assert result.qualifying_market_regime_count == 3
    assert result.observation_month_gap == 0
    assert result.independent_decision_gap == 0
    assert result.qualifying_walk_forward_fold_gap == 0
    assert result.qualifying_market_regime_gap == 0
    assert not result.adaptive_weights_enabled
    assert not result.online_learning_allowed
    assert not result.main_paper_ledger_write_allowed
    assert not result.broker_execution_allowed


def test_audit_or_object_failure_blocks_an_otherwise_eligible_admission() -> None:
    audit_blocked = _service(
        report=_report(),
        admission=_admission(),
        audit_status="PARTIAL",
        audit_findings=["SHADOW_REPORT_RECALCULATION_MISMATCH"],
    ).status("study:ready")
    assert audit_blocked.capability_status is (
        AdaptiveResearchCapabilityStatus.NOT_ENTERED_BY_DESIGN
    )
    assert "BLOCKED_BY_INTEGRITY" in audit_blocked.reason_codes
    assert (
        "PHASE7_AUDIT__SHADOW_REPORT_RECALCULATION_MISMATCH"
        in audit_blocked.reason_codes
    )

    object_blocked = _service(
        report=_report(),
        admission=_admission(),
        objects_valid=False,
    ).status("study:ready")
    assert object_blocked.phase7_audit_status == "PARTIAL"
    assert object_blocked.reason_codes == [
        "BLOCKED_BY_INTEGRITY",
        "PHASE7_REPORT_OBJECT_INVALID",
    ]


def test_disabled_boundary_schema_rejects_active_research_or_enabled_flags() -> None:
    baseline = _service(report=None, admission=None).status("study:ready")
    payload = baseline.model_dump(mode="python")
    with pytest.raises(ValidationError, match="cannot report active adaptive research"):
        AdaptiveResearchStatusReport.model_validate(
            {
                **payload,
                "capability_status": (
                    AdaptiveResearchCapabilityStatus.OFFLINE_DYNAMIC_WEIGHT_RESEARCH
                ),
            }
        )
    with pytest.raises(ValidationError):
        AdaptiveResearchStatusReport.model_validate(
            {**payload, "adaptive_weights_enabled": True}
        )
