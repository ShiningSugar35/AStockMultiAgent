"""Lease-backed handoff between deterministic scheduled facts and semantic workers.

This module never calls a model. It exposes one bounded, privacy-minimized work item,
then accepts only registered capability artifacts and typed per-subject results. The
canonical lease prevents two semantic workers from owning the same scheduled bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from astock.core.state import StateStore
from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    MaterialChangeDigest,
    RequestIntent,
    ScheduledDomain,
    ScheduledResearchPolicy,
    ScheduledRunRequest,
    ScheduledSemanticSubjectResult,
    ScheduledTaskBinding,
    SideEffectClass,
    StrictModel,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService
from astock.investor_orchestration.scheduled_context import (
    ScheduledAnalysisContext,
    minimal_analysis_context,
)
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputCoverageService,
    load_input_audit_policy,
)
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


class ScheduledSemanticSubjectWork(StrictModel):
    domain: ScheduledDomain
    instrument_id: str
    source_report_id: str
    context: ScheduledAnalysisContext


class ScheduledSemanticWorkItem(StrictModel):
    owner_id: str
    lease_until: datetime
    binding_id: str
    schedule_bucket: str
    scheduled_request: ScheduledRunRequest
    investor_request: InvestorRequestEnvelope
    subjects: tuple[ScheduledSemanticSubjectWork, ...]
    source_report_ids: tuple[str, ...]
    actual_execution_allowed: Literal[False] = False


class ScheduledSemanticSubmission(StrictModel):
    owner_id: str = Field(min_length=1, max_length=200)
    capability_artifacts: dict[str, tuple[str, ...]]
    results: tuple[ScheduledSemanticSubjectResult, ...]
    digests: tuple[MaterialChangeDigest, ...] = ()

    @model_validator(mode="after")
    def bounded_unique_submission(self) -> ScheduledSemanticSubmission:
        if not self.capability_artifacts or len(self.capability_artifacts) > 64:
            raise ValueError("scheduled semantic submission needs bounded capability artifacts")
        if any(
            not capability.strip()
            or not artifacts
            or len(artifacts) > 256
            or len(set(artifacts)) != len(artifacts)
            or any(not artifact.strip() for artifact in artifacts)
            for capability, artifacts in self.capability_artifacts.items()
        ):
            raise ValueError("scheduled semantic capability artifacts are malformed")
        if not self.results or len(self.results) > 256:
            raise ValueError("scheduled semantic submission needs bounded subject results")
        if len({item.result_id for item in self.results}) != len(self.results):
            raise ValueError("scheduled semantic subject results must be unique")
        if len({item.digest_id for item in self.digests}) != len(self.digests):
            raise ValueError("scheduled semantic digests must be unique")
        return self


class ScheduledSemanticWorkInProgress(RuntimeError):
    pass


class ScheduledSemanticWorkService:
    def __init__(self, store: InvestorOrchestrationStore) -> None:
        self.store = store
        self.state = StateStore(store.path)
        self.preflight = InvestorSessionPreflightService(store)
        self.audit = ScheduledInputCoverageService(store, load_input_audit_policy())
        self.scheduler = ScheduledResearchService(
            store,
            self.preflight,
            source_audit_policy=self.audit.policy,
        )

    def claim(
        self,
        binding_id: str,
        schedule_bucket: str,
        *,
        owner_id: str,
        lease_seconds: int = 1800,
    ) -> ScheduledSemanticWorkItem:
        if not owner_id.strip() or len(owner_id) > 200:
            raise ValueError("scheduled semantic owner identity is invalid")
        if not 60 <= lease_seconds <= 7200:
            raise ValueError("scheduled semantic lease must be within [60, 7200] seconds")
        checkpoint = self.store.get_scheduled_checkpoint(binding_id, schedule_bucket)
        if checkpoint is None:
            raise ValueError("scheduled semantic work requires an existing prepared bucket")
        request = ScheduledRunRequest.model_validate(checkpoint["request"])
        if not request.source_coverage_artifact_ids:
            raise ValueError("scheduled semantic work requires verified staged source reports")
        if request.semantic_capability_receipt_id or request.semantic_result_artifact_ids:
            raise ValueError("scheduled semantic work is already staged for this bucket")
        binding, policy = self._binding_policy(request)
        if binding.binding_id != binding_id:
            raise ValueError("scheduled semantic work binding identity changed")
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)
        lock_key = self._lock_key(binding_id, schedule_bucket)
        if not self.state.acquire_lock(lock_key, owner_id, lease_until):
            raise ScheduledSemanticWorkInProgress("scheduled semantic bucket is already claimed")
        try:
            report_map = self._report_map(request)
            entity_ids = tuple(sorted({instrument for _, instrument in report_map}))
            investor_request = self._investor_request(request, policy, entity_ids)
            preflight = self.preflight.build(investor_request)
            current_subjects = self.scheduler._subjects_by_domain(preflight)
            selected_subjects = {
                domain: current_subjects.get(domain, ()) for domain in request.domains
            }
            coverage = self.audit.bind_run(
                run_id=request.run_id,
                window=request.window,
                requested_at=request.requested_at,
                subjects=selected_subjects,
                report_ids=request.source_coverage_artifact_ids,
                interval_start=request.source_interval_start,
                interval_end=request.source_interval_end,
            )
            if not coverage.all_families_checked or coverage.verified_subject_count != len(
                report_map
            ):
                raise ValueError("scheduled source coverage is not complete at semantic claim time")
            expected = {
                (domain, normalized_instrument(instrument))
                for domain, instruments in selected_subjects.items()
                for instrument in instruments
            }
            if expected != set(report_map):
                raise ValueError(
                    "scheduled semantic subject set differs from frozen source coverage"
                )
            subjects = tuple(
                ScheduledSemanticSubjectWork(
                    domain=domain,
                    instrument_id=instrument,
                    source_report_id=report_map[(domain, instrument)],
                    context=minimal_analysis_context(
                        preflight,
                        policy,
                        domain=domain,
                        instrument_id=instrument,
                        window=request.window,
                    ),
                )
                for domain, instrument in sorted(
                    expected, key=lambda item: (item[0].value, item[1])
                )
            )
            return ScheduledSemanticWorkItem(
                owner_id=owner_id,
                lease_until=lease_until,
                binding_id=binding_id,
                schedule_bucket=schedule_bucket,
                scheduled_request=request,
                investor_request=investor_request,
                subjects=subjects,
                source_report_ids=request.source_coverage_artifact_ids,
                actual_execution_allowed=False,
            )
        except Exception:
            self.state.release_lock(lock_key, owner_id)
            raise

    def submit(
        self,
        binding_id: str,
        schedule_bucket: str,
        submission: ScheduledSemanticSubmission,
    ) -> ScheduledRunRequest:
        submission = ScheduledSemanticSubmission.model_validate(submission)
        checkpoint = self.store.get_scheduled_checkpoint(binding_id, schedule_bucket)
        if checkpoint is None:
            raise ValueError("scheduled semantic submission requires a prepared bucket")
        request = ScheduledRunRequest.model_validate(checkpoint["request"])
        binding, policy = self._binding_policy(request)
        if binding.binding_id != binding_id:
            raise ValueError("scheduled semantic submission binding identity changed")
        already_staged = bool(
            request.semantic_capability_receipt_id and request.semantic_result_artifact_ids
        )
        if not already_staged:
            self._require_current_lease(binding_id, schedule_bucket, submission.owner_id)
        report_map = self._report_map(request)
        entity_ids = tuple(sorted({instrument for _, instrument in report_map}))
        investor_request = self._investor_request(request, policy, entity_ids)
        _preflight, _plan, coverage = InvestorOrchestrationService(
            self.store
        ).execute_registered_inputs(
            investor_request,
            submission.capability_artifacts,
        )
        if (
            not coverage.coverage_complete
            or coverage.required_capability_coverage != 1.0
            or coverage.prohibited_call_count != 0
        ):
            raise ValueError("scheduled semantic capability execution is incomplete")
        results = {
            (item.domain, normalized_instrument(item.instrument_id)): item
            for item in submission.results
        }
        if len(results) != len(submission.results) or set(results) != set(report_map):
            raise ValueError(
                "scheduled semantic submission must cover each frozen subject exactly once"
            )
        digests_by_artifact = {
            f"MaterialChangeDigest:{self.scheduler._digest_hash(digest)}": digest
            for digest in submission.digests
        }
        registered: list[str] = []
        for key in sorted(results, key=lambda item: (item[0].value, item[1])):
            result = results[key]
            source_report_id = report_map[key]
            if (
                result.run_id != request.run_id
                or result.as_of != request.requested_at
                or result.evidence_artifact_ids != (source_report_id,)
            ):
                raise ValueError(
                    "scheduled semantic result differs from its frozen subject evidence"
                )
            digest = (
                None
                if result.digest_artifact_id is None
                else digests_by_artifact.get(result.digest_artifact_id)
            )
            if result.material_change and digest is None:
                raise ValueError("scheduled material result is missing its submitted typed digest")
            registered.append(self.scheduler.register_semantic_result(result, digest=digest))
        if set(digests_by_artifact) != {
            item.digest_artifact_id
            for item in submission.results
            if item.digest_artifact_id is not None
        }:
            raise ValueError("scheduled semantic submission contains an unused or missing digest")
        staged = self.scheduler.stage(
            request.model_copy(
                update={
                    "semantic_capability_receipt_id": coverage.receipt_id,
                    "semantic_result_artifact_ids": tuple(sorted(registered)),
                }
            )
        )
        if not already_staged:
            if not self.state.release_lock(
                self._lock_key(binding_id, schedule_bucket), submission.owner_id
            ):
                raise RuntimeError("scheduled semantic lease disappeared before successful staging")
        return staged

    def release(self, binding_id: str, schedule_bucket: str, *, owner_id: str) -> bool:
        return self.state.release_lock(self._lock_key(binding_id, schedule_bucket), owner_id)

    def _report_map(
        self,
        request: ScheduledRunRequest,
    ) -> dict[tuple[ScheduledDomain, str], str]:
        result: dict[tuple[ScheduledDomain, str], str] = {}
        for artifact_id in request.source_coverage_artifact_ids:
            report = self.audit.verify_registered(artifact_id)
            audit = report.request
            key = (audit.domain, normalized_instrument(audit.scope.instrument_id))
            if (
                not report.all_families_checked
                or key in result
                or audit.run_id != request.run_id
                or audit.window is not request.window
                or audit.as_of != request.requested_at
                or audit.interval_start != request.source_interval_start
                or audit.interval_end != request.source_interval_end
            ):
                raise ValueError("scheduled semantic source report set is inconsistent")
            result[key] = artifact_id
        if not result:
            raise ValueError("scheduled semantic work has no verified source reports")
        return result

    def _binding_policy(
        self,
        request: ScheduledRunRequest,
    ) -> tuple[ScheduledTaskBinding, ScheduledResearchPolicy]:
        binding = self.store.get_binding(request.binding_id)
        if (
            binding is None
            or not binding.active
            or binding.confirmed_at is None
            or not binding.consent_hash
        ):
            raise ValueError("scheduled semantic work requires an active consented binding")
        if binding.execution_surface not in {"LOCAL_DAEMON", "DESKTOP_LOCAL"}:
            raise ValueError("scheduled semantic work requires a local execution surface")
        policy = self.store.get_scheduled_policy(binding.policy_id)
        if (
            policy is None
            or policy.policy_hash != request.policy_hash
            or binding.policy_hash != request.policy_hash
        ):
            raise ValueError("scheduled semantic work policy binding changed")
        return binding, policy

    @staticmethod
    def _investor_request(
        request: ScheduledRunRequest,
        policy: ScheduledResearchPolicy,
        entity_ids: tuple[str, ...],
    ) -> InvestorRequestEnvelope:
        return InvestorRequestEnvelope(
            request_id=f"scheduled:{request.run_id}",
            question_time=request.requested_at,
            user_timezone=policy.market_timezone,
            market_timezone=policy.market_timezone,
            raw_text=f"scheduled research {request.window.value}",
            normalized_intent=RequestIntent.MONITOR,
            side_effect=SideEffectClass.META,
            entity_ids=entity_ids,
            idempotency_key=request.idempotency_key,
            metadata={
                "binding_id": request.binding_id,
                "schedule_bucket": request.schedule_bucket,
                "disclosure_level": policy.disclosure_level.value,
            },
        )

    def _require_current_lease(self, binding_id: str, schedule_bucket: str, owner_id: str) -> None:
        lock_key = self._lock_key(binding_id, schedule_bucket)
        now = utc_now().astimezone(UTC)
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT owner_run_id,lease_until FROM lease_lock WHERE lock_key=?",
                (lock_key,),
            ).fetchone()
        lease_until = (
            None
            if row is None
            else datetime.fromisoformat(str(row["lease_until"]).replace("Z", "+00:00"))
        )
        if (
            row is None
            or str(row["owner_run_id"]) != owner_id
            or lease_until is None
            or lease_until.tzinfo is None
            or lease_until.utcoffset() is None
            or lease_until.astimezone(UTC) <= now
        ):
            raise ScheduledSemanticWorkInProgress(
                "scheduled semantic submission does not own a current lease"
            )

    @staticmethod
    def _lock_key(binding_id: str, schedule_bucket: str) -> str:
        return "scheduled-semantic:" + content_hash(
            {"binding_id": binding_id, "schedule_bucket": schedule_bucket}
        )


__all__ = [
    "ScheduledSemanticSubmission",
    "ScheduledSemanticSubjectWork",
    "ScheduledSemanticWorkInProgress",
    "ScheduledSemanticWorkItem",
    "ScheduledSemanticWorkService",
]
