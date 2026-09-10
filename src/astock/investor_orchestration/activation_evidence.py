"""Read independently accepted release evidence from the canonical registry.

There is deliberately no report/approval creation API here. The versioned
activation config must pin the independently reviewed artifact IDs and hashes;
a caller's count, boolean or arbitrary approval name is not release evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from astock.investor_orchestration.models import StrictModel
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash

_VARIANTS = frozenset(
    {
        "positive",
        "missing_or_conflict",
        "stale_or_pit",
        "actual_paper",
        "empty_holding",
        "side_effect_idempotency",
        "missing_or_prohibited",
        "public_answer",
    }
)


class AcceptedScenarioEvidence(StrictModel):
    scenario_id: int
    variants: dict[str, str]
    status: Literal["PASS"]


class InvestorReleaseAcceptance(StrictModel):
    schema_version: Literal["investor-release-acceptance-v1"]
    feature_id: str
    policy_hash: str
    code_revision: str = Field(min_length=40)
    verified_at: datetime
    source_tree_stable: Literal[True]
    verification_level: Literal["DOMAIN_E2E_NOT_HANDLER_STUBS"]
    scenarios: tuple[AcceptedScenarioEvidence, ...]
    quality_evidence: dict[str, str]
    ledger_economic_write_count: Literal[0]
    broker_execution_allowed: Literal[False]


class InvestorOwnerApproval(StrictModel):
    schema_version: Literal["investor-owner-approval-v1"]
    feature_id: str
    policy_hash: str
    release_acceptance_hash: str
    approved_at: datetime
    expires_at: datetime
    approved: Literal[True]
    authorization_scope: Literal["LOCAL_RESEARCH_FEATURE_ONLY"]
    broker_execution_allowed: Literal[False]


class ActivationEvidenceReader:
    def __init__(self, store: InvestorOrchestrationStore, config: Mapping[str, Any]) -> None:
        self.verifier = RegisteredOutputVerifier(store)
        self.pins = config.get("accepted_evidence", {})

    def _pinned(
        self,
        feature_id: str,
        key: str,
        model: type[StrictModel],
    ) -> StrictModel | None:
        feature = self.pins.get(feature_id, {})
        pin = feature.get(key)
        if not isinstance(pin, dict) or set(pin) != {"artifact_id", "object_hash"}:
            return None
        record = self.verifier.state.artifact_record(str(pin["artifact_id"]))
        if record is None or record["object_hash"] != pin["object_hash"]:
            return None
        return self.verifier.load(str(pin["artifact_id"]), model)  # type: ignore[return-value]

    def release(
        self,
        feature_id: str,
        policy_hash: str,
        now: datetime,
    ) -> InvestorReleaseAcceptance | None:
        value = self._pinned(feature_id, "release", InvestorReleaseAcceptance)
        if not isinstance(value, InvestorReleaseAcceptance):
            return None
        if (
            value.feature_id != feature_id
            or value.policy_hash != policy_hash
            or value.verified_at.tzinfo is None
            or value.verified_at > now
        ):
            return None
        required_quality = {"pytest", "ruff", "pyright", "performance", "rollback", "safety_audit"}
        if set(value.quality_evidence) != required_quality:
            return None
        for artifact_id in value.quality_evidence.values():
            record = self.verifier.state.artifact_record(artifact_id)
            if record is None:
                return None
            self.verifier.objects.get_bytes(str(record["object_hash"]))
        seen: set[int] = set()
        for scenario in value.scenarios:
            if scenario.scenario_id in seen or set(scenario.variants) != _VARIANTS:
                return None
            seen.add(scenario.scenario_id)
            for artifact_id in scenario.variants.values():
                record = self.verifier.state.artifact_record(artifact_id)
                if record is None:
                    return None
                self.verifier.objects.get_bytes(str(record["object_hash"]))
        return value

    def scenario_count(self, feature_id: str, policy_hash: str, now: datetime) -> int:
        release = self.release(feature_id, policy_hash, now)
        if release is None:
            return 0
        expected = {
            1,
            2,
            3,
            *range(11, 15),
            *range(22, 30),
            *range(31, 39),
            *range(41, 49),
            *range(51, 58),
            *range(61, 68),
            *range(71, 78),
            *range(81, 91),
            *range(91, 97),
        }
        return len(expected) if {case.scenario_id for case in release.scenarios} == expected else 0

    def owner_approved(
        self,
        feature_id: str,
        policy_hash: str,
        approval_id: str | None,
        now: datetime,
    ) -> bool:
        pin = self.pins.get(feature_id, {}).get("owner_approval", {})
        if not approval_id or pin.get("artifact_id") != approval_id:
            return False
        approval = self._pinned(feature_id, "owner_approval", InvestorOwnerApproval)
        release_pin = self.pins.get(feature_id, {}).get("release", {})
        return bool(
            isinstance(approval, InvestorOwnerApproval)
            and approval.feature_id == feature_id
            and approval.policy_hash == policy_hash
            and approval.release_acceptance_hash == release_pin.get("object_hash")
            and approval.approved_at.tzinfo is not None
            and approval.expires_at.tzinfo is not None
            and approval.approved_at <= now < approval.expires_at
            and self.release(feature_id, policy_hash, now) is not None
        )

    def accepted_shadow_ids(self, feature_id: str) -> frozenset[str]:
        values = self.pins.get(feature_id, {}).get("shadow_observation_ids", ())
        return frozenset(values) if isinstance(values, (list, tuple)) else frozenset()

    def valid_coverage(self, request_id: str, receipt_id: str | None) -> bool:
        if receipt_id is None:
            return False
        if receipt_id.startswith("ScheduledCapabilityCoverageReceipt:"):
            from astock.investor_orchestration.scheduled_input_coverage import (
                ScheduledInputCoverageService,
                load_input_audit_policy,
            )

            try:
                receipt = ScheduledInputCoverageService(
                    self.verifier.store,
                    load_input_audit_policy(),
                    self.verifier.objects,
                ).verify_registered_run_coverage(receipt_id)
            except (ValueError, OSError):
                return False
            return bool(
                request_id == f"scheduled:{receipt.run_id}"
                and receipt.all_families_checked
                and receipt.expected_subject_count > 0
                and receipt.verified_subject_count == receipt.expected_subject_count
                and receipt.semantic_coverage_complete
                and receipt.semantic_capability_receipt_id is not None
                and receipt.prohibited_call_count == 0
                and receipt.economic_write_count == 0
            )
        with self.verifier.store.connect() as connection:
            row = connection.execute(
                "SELECT receipt_hash FROM capability_coverage_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        return row is not None and self.verifier.authenticated_coverage(
            request_id, receipt_id, row[0]
        )

    @staticmethod
    def authentic_assessment(assessment: StrictModel) -> bool:
        payload = assessment.model_dump(exclude={"assessment_id", "assessment_hash"})
        return content_hash(payload) == getattr(assessment, "assessment_hash", None)
