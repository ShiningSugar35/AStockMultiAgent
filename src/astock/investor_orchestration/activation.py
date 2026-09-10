from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from astock.investor_orchestration.activation_evidence import ActivationEvidenceReader
from astock.investor_orchestration.models import (
    ActivationAssessment,
    ActivationGatePolicy,
    AdmissionStatus,
    ControlledLiveCheck,
    FeatureActivationReceipt,
    ShadowObservation,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


class ActivationGateService:
    """Fail-closed activation and rollback for orchestration features.

    The service can complete software delivery while keeping features disabled or
    shadow-only until legitimate prospective, controlled-live and owner evidence
    exists. It never writes an actual-account or paper-ledger table.
    """

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        *,
        config_path: str | Path = "configs/investor_orchestration_activation_v1.yaml",
    ) -> None:
        self.store = store
        self.config_path = Path(config_path)
        self.config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw_policy = dict(self.config["activation_policy"])
        raw_policy["version"] = str(self.config["version"])
        raw_policy["policy_hash"] = content_hash(raw_policy)
        self.policy = ActivationGatePolicy.model_validate(raw_policy)
        self.evidence = ActivationEvidenceReader(store, self.config)

    def status(self, feature_id: str) -> AdmissionStatus:
        latest = self.store.latest_activation_receipt(feature_id)
        if latest is not None:
            return latest.new_status
        configured = self.config["feature_status"].get(feature_id)
        if configured is None:
            return AdmissionStatus.NOT_ADMITTED
        return AdmissionStatus(str(configured))

    def record_shadow(self, observation: ShadowObservation) -> ShadowObservation:
        if observation.economic_write_count != 0:
            raise ValueError("shadow observation cannot include economic writes")
        self.store.save_shadow_observation(observation)
        return observation

    def record_controlled_live(self, check: ControlledLiveCheck) -> ControlledLiveCheck:
        self.store.save_controlled_live_check(check)
        return check

    def assess(
        self,
        feature_id: str,
        *,
        requested_status: AdmissionStatus = AdmissionStatus.ACTIVE,
        recorded_scenario_count: int,
        owner_approval_id: str | None = None,
        assessed_at: datetime | None = None,
    ) -> ActivationAssessment:
        now = assessed_at or utc_now()
        if now.tzinfo is None or now.utcoffset() is None or now > utc_now():
            raise ValueError("activation assessment time must be aware and not in the future")
        if feature_id not in self.config["feature_status"]:
            raise ValueError("unknown activation feature")
        if recorded_scenario_count < 0:
            raise ValueError("scenario count cannot be negative")
        # Compatibility argument is diagnostic only; accepted artifact evidence owns the count.
        recorded_scenario_count = self.evidence.scenario_count(
            feature_id, self.policy.policy_hash, now
        )
        accepted_ids = self.evidence.accepted_shadow_ids(feature_id)
        observations = tuple(
            item
            for item in self.store.shadow_observations(feature_id)
            if item.observation_id in accepted_ids and self._qualified_shadow(item, now)
        )
        live_checks = self.store.latest_controlled_live_checks()
        shadow_days = len({item.observed_at.date() for item in observations})
        material_errors = sum(item.material_error for item in observations)
        material_error_rate = None if not observations else material_errors / len(observations)
        minimum_coverage = min(
            (item.required_capability_coverage for item in observations),
            default=0.0,
        )
        prohibited_calls = sum(item.prohibited_call_count for item in observations)
        economic_writes = sum(item.economic_write_count for item in observations)
        label_only_actions = sum(item.label_change_only_action for item in observations)
        macro_pass = self._check_pass(live_checks, "CURRENT_MACRO", feature_id)
        schedule_pass = self._check_pass(live_checks, "SCHEDULED_RESEARCH", feature_id)
        gates = {
            "recorded_scenarios": recorded_scenario_count
            >= self.policy.required_recorded_scenarios,
            "shadow_observations": len(observations) >= self.policy.minimum_shadow_observations,
            "shadow_days": shadow_days >= self.policy.minimum_shadow_days,
            "material_error_rate": material_error_rate is not None
            and material_error_rate <= self.policy.maximum_material_error_rate,
            "required_capability_coverage": minimum_coverage
            >= self.policy.minimum_required_capability_coverage,
            "prohibited_calls": prohibited_calls <= self.policy.maximum_prohibited_calls,
            "economic_writes": economic_writes <= self.policy.maximum_economic_writes,
            "label_change_only_actions": label_only_actions == 0,
            "current_macro_controlled_live": (
                macro_pass if self.policy.require_current_macro_controlled_live else True
            ),
            "scheduled_research_controlled_live": (
                schedule_pass if self.policy.require_schedule_controlled_live else True
            ),
            "owner_approval": (
                self.evidence.owner_approved(
                    feature_id, self.policy.policy_hash, owner_approval_id, now
                )
            ),
        }
        blockers = tuple(name for name, passed in gates.items() if not passed)
        current_status = self.status(feature_id)
        body = {
            "feature_id": feature_id,
            "assessed_at": now,
            "current_status": current_status,
            "requested_status": requested_status,
            "eligible": not blockers,
            "gate_results": gates,
            "blockers": blockers,
            "recorded_scenario_count": recorded_scenario_count,
            "shadow_observation_count": len(observations),
            "shadow_day_count": shadow_days,
            "material_error_rate": material_error_rate,
            "label_change_only_action_count": label_only_actions,
            "economic_write_count": economic_writes,
            "owner_approval_id": owner_approval_id,
            "policy_hash": self.policy.policy_hash,
        }
        assessment_hash = content_hash(body)
        return ActivationAssessment(
            assessment_id=(
                f"activation-assessment-{uuid.uuid5(uuid.NAMESPACE_URL, assessment_hash)}"
            ),
            assessment_hash=assessment_hash,
            **body,
        )

    def activate(
        self,
        assessment: ActivationAssessment,
        *,
        feature_flags: Mapping[str, bool],
    ) -> FeatureActivationReceipt:
        assessment = ActivationAssessment.model_validate(assessment.model_dump())
        if not self.evidence.authentic_assessment(assessment):
            raise ValueError("activation assessment payload was altered")
        if assessment.policy_hash != self.policy.policy_hash:
            raise ValueError("activation assessment belongs to another policy")
        if yaml.safe_load(self.config_path.read_text(encoding="utf-8")) != self.config:
            raise ValueError("activation configuration changed; reassessment is required")
        fresh = self.assess(
            assessment.feature_id,
            recorded_scenario_count=assessment.recorded_scenario_count,
            owner_approval_id=assessment.owner_approval_id,
        )
        if (
            not assessment.eligible
            or not fresh.eligible
            or fresh.gate_results != assessment.gate_results
        ):
            raise ValueError("activation gates are not satisfied by current accepted evidence")
        if assessment.requested_status is not AdmissionStatus.ACTIVE:
            raise ValueError("activation assessment must request ACTIVE status")
        if not assessment.owner_approval_id:
            raise ValueError("owner approval is required")
        previous = self.status(assessment.feature_id)
        flags = dict(feature_flags)
        if (
            not flags
            or not set(flags) <= set(self.config["feature_flags"])
            or any(type(value) is not bool or not value for value in flags.values())
        ):
            raise ValueError("activation requires explicit true feature flags")
        body = {
            "feature_id": assessment.feature_id,
            "previous_status": previous,
            "new_status": AdmissionStatus.ACTIVE,
            "changed_at": utc_now(),
            "reason": "ALL_ACTIVATION_GATES_PASSED",
            "assessment_id": assessment.assessment_id,
            "owner_approval_id": assessment.owner_approval_id,
            "feature_flags": flags,
            "ledger_write_count": 0,
        }
        receipt_hash = content_hash(body)
        receipt = FeatureActivationReceipt(
            receipt_id=f"activation-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_hash)}",
            receipt_hash=receipt_hash,
            **body,
        )
        self.store.save_activation_receipt(receipt)
        return receipt

    def rollback(
        self,
        feature_id: str,
        *,
        reason: str,
    ) -> FeatureActivationReceipt:
        previous = self.status(feature_id)
        disabled_flags = {str(flag): False for flag in self.config["rollback"]["disables"]}
        body = {
            "feature_id": feature_id,
            "previous_status": previous,
            "new_status": AdmissionStatus.ROLLED_BACK,
            "changed_at": utc_now(),
            "reason": reason,
            "assessment_id": None,
            "owner_approval_id": None,
            "feature_flags": disabled_flags,
            "ledger_write_count": 0,
        }
        receipt_hash = content_hash(body)
        receipt = FeatureActivationReceipt(
            receipt_id=f"rollback-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_hash)}",
            receipt_hash=receipt_hash,
            **body,
        )
        self.store.save_activation_receipt(receipt)
        return receipt

    def _qualified_shadow(self, observation: ShadowObservation, now: datetime) -> bool:
        if (
            observation.observed_at.tzinfo is None
            or observation.observed_at > now
            or not 0 <= observation.required_capability_coverage <= 1
            or observation.prohibited_call_count < 0
            or observation.economic_write_count != 0
            or observation.baseline_artifact_id is None
            or observation.candidate_artifact_id is None
            or observation.baseline_artifact_id == observation.candidate_artifact_id
        ):
            return False
        for artifact_id in (observation.baseline_artifact_id, observation.candidate_artifact_id):
            record = self.evidence.verifier.state.artifact_record(artifact_id)
            if record is None:
                return False
            created = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None or created > observation.observed_at:
                return False
            self.evidence.verifier.objects.get_bytes(str(record["object_hash"]))
        return True

    def _check_pass(
        self,
        checks: Mapping[str, ControlledLiveCheck],
        check_type: str,
        feature_id: str,
    ) -> bool:
        check = checks.get(check_type)
        accepted = self.evidence.pins.get(feature_id, {}).get("controlled_live_check_ids", ())
        if (
            check is None
            or check.status != "PASS"
            or not check.evidence_ids
            or check.check_id not in accepted
            or check.checked_at.tzinfo is None
            or check.checked_at > utc_now()
        ):
            return False
        for artifact_id in check.evidence_ids:
            record = self.evidence.verifier.state.artifact_record(artifact_id)
            if record is None:
                return False
            self.evidence.verifier.objects.get_bytes(str(record["object_hash"]))
        return True


def shadow_observation(
    *,
    feature_id: str,
    request_or_run_id: str,
    required_capability_coverage: float,
    observed_at: datetime | None = None,
    baseline_artifact_id: str | None = None,
    candidate_artifact_id: str | None = None,
    baseline_action: str | None = None,
    candidate_action: str | None = None,
    label_change_only_action: bool = False,
    prohibited_call_count: int = 0,
    economic_write_count: int = 0,
    latency_ms: int | None = None,
    material_error: bool = False,
    notes: tuple[str, ...] = (),
) -> ShadowObservation:
    at = observed_at or utc_now()
    body: dict[str, Any] = {
        "feature_id": feature_id,
        "observed_at": at,
        "request_or_run_id": request_or_run_id,
        "baseline_artifact_id": baseline_artifact_id,
        "candidate_artifact_id": candidate_artifact_id,
        "baseline_action": baseline_action,
        "candidate_action": candidate_action,
        "label_change_only_action": label_change_only_action,
        "required_capability_coverage": required_capability_coverage,
        "prohibited_call_count": prohibited_call_count,
        "economic_write_count": economic_write_count,
        "latency_ms": latency_ms,
        "material_error": material_error,
        "notes": notes,
    }
    return ShadowObservation(
        observation_id=f"shadow-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}",
        **body,
    )
