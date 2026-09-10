from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from astock.investor_orchestration.canonical_state import (
    CanonicalStateProjectionReader,
    normalized_instrument,
)
from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    DisclosureLevel,
    InvestorRequestEnvelope,
    MaterialChangeDigest,
    RequestIntent,
    ScheduledActionPolicy,
    ScheduledDomain,
    ScheduledResearchPolicy,
    ScheduledRunOutcome,
    ScheduledRunReceipt,
    ScheduledRunRequest,
    ScheduledSemanticSubjectResult,
    ScheduledTaskBinding,
    ScheduledWindow,
    SideEffectClass,
    WatchlistAnalysisRevision,
)
from astock.investor_orchestration.notification_delivery import DurableNotificationOutbox
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled_context import minimal_analysis_context
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputAuditPolicy,
    ScheduledInputCoverageService,
    ScheduledRunSourceCoverage,
    load_input_audit_policy,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


def build_scheduled_run_request(
    binding: ScheduledTaskBinding,
    *,
    window: ScheduledWindow,
    schedule_bucket: str,
    requested_at: datetime,
    domains: tuple[ScheduledDomain, ...],
    source_revision_set: Mapping[str, str] | None = None,
) -> ScheduledRunRequest:
    """Build the deterministic run identity; evidence is staged separately and monotonically."""
    key = content_hash(
        {
            "binding_id": binding.binding_id,
            "schedule_bucket": schedule_bucket,
            "window": window,
            "domains": tuple(sorted(domain.value for domain in domains)),
            "policy_hash": binding.policy_hash,
        }
    )
    return ScheduledRunRequest(
        run_id=f"scheduled-{uuid.uuid5(uuid.NAMESPACE_URL, key)}",
        binding_id=binding.binding_id,
        schedule_bucket=schedule_bucket,
        window=window,
        domains=domains,
        requested_at=requested_at,
        source_revision_set=dict(source_revision_set or {}),
        policy_hash=binding.policy_hash,
        idempotency_key=key,
    )


class NotificationSink(Protocol):
    def publish(
        self,
        *,
        notification_key: str,
        title: str,
        body: str,
        metadata: Mapping[str, Any],
    ) -> str: ...


class ConfirmedPaperReplayAdapter(Protocol):
    """Narrow adapter to the existing paper ledger/replay implementation."""

    def replay_confirmed_orders(
        self,
        *,
        run_id: str,
        requested_at: datetime,
        order_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...


class SQLiteNotificationOutbox(DurableNotificationOutbox):
    """Compatibility name for the transactional, recoverable notification outbox."""


class ScheduledAnalyzer(Protocol):
    def __call__(
        self,
        *,
        domain: ScheduledDomain,
        instrument_id: str,
        window: ScheduledWindow,
        preflight: Any,
        run_id: str,
    ) -> MaterialChangeDigest | None: ...


class DeterministicMaterialChangeAnalyzer:
    """Low-cost default analyzer over already captured material events.

    It does not fetch data or use a model. Semantic deep research can replace this
    analyzer, but receives only the policy allowlist and must return the same typed
    digest.
    """

    _severity_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

    def __call__(
        self,
        *,
        domain: ScheduledDomain,
        instrument_id: str,
        window: ScheduledWindow,
        preflight: Any,
        run_id: str,
    ) -> MaterialChangeDigest | None:
        events = [
            event
            for event in preflight.context.material_events
            if event.instrument_id is not None
            and normalized_instrument(event.instrument_id) == normalized_instrument(instrument_id)
            and event.severity in {"HIGH", "CRITICAL"}
        ]
        if not events:
            return None
        events.sort(
            key=lambda item: (self._severity_rank[item.severity], item.available_at),
            reverse=True,
        )
        top = events[0]
        selected_events = events[:5]
        combined_summary = "；".join(event.summary for event in selected_events)
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
        if domain is ScheduledDomain.WATCHLIST:
            action = "RESEARCH"
            impact = "观察名单结论需要按新增重大事件重新评估，暂不自动形成交易。"
        elif domain is ScheduledDomain.PAPER_HOLDING:
            action = "REVIEW"
            impact = "模拟持仓进入复核；只生成建议，不创建、修改或确认新订单。"
        else:
            action = "REVIEW"
            impact = "正式持仓需要用户及时复核；系统只推送建议，不写账户或执行交易。"
        body = {
            "run_id": run_id,
            "domain": domain,
            "instrument_id": instrument_id,
            "as_of": preflight.as_of,
            "severity": top.severity,
            "change_summary": combined_summary,
            "impact_summary": impact,
            "action": action,
            "action_conditions": (
                f"核实事件 {top.event_id} 的权威来源与实际影响",
                "复核原投资逻辑、估值和组合风险贡献",
            ),
            "actual_execution_allowed": False,
            "source_artifact_ids": tuple(event.event_id for event in selected_events),
        }
        return MaterialChangeDigest(
            digest_id=f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}",
            **body,
        )


class ScheduledResearchService:
    def __init__(
        self,
        store: InvestorOrchestrationStore,
        preflight_service: InvestorSessionPreflightService,
        *,
        analyzer: ScheduledAnalyzer | None = None,
        notification_sink: NotificationSink | None = None,
        paper_replay_adapter: ConfirmedPaperReplayAdapter | None = None,
        source_audit_policy: ScheduledInputAuditPolicy | None = None,
    ) -> None:
        self.store = store
        self.preflight_service = preflight_service
        self.analyzer = analyzer or DeterministicMaterialChangeAnalyzer()
        self.notification_sink = notification_sink
        self.outbox = DurableNotificationOutbox(store)
        self.paper_replay_adapter = paper_replay_adapter
        self.source_audit = ScheduledInputCoverageService(
            store, source_audit_policy or load_input_audit_policy()
        )

    def register_policy(self, policy: ScheduledResearchPolicy) -> ScheduledResearchPolicy:
        self._validate_policy(policy)
        self.store.save_scheduled_policy(policy)
        return policy

    def register_binding(self, binding: ScheduledTaskBinding) -> ScheduledTaskBinding:
        policy = self.store.get_scheduled_policy(binding.policy_id)
        if policy is None:
            raise ValueError(f"unknown active scheduled policy: {binding.policy_id}")
        if policy.policy_hash != binding.policy_hash:
            raise ValueError("binding policy hash does not match active policy")
        if binding.creation_mode == "OFFICIAL_API_VERIFIED" and binding.platform_task_id is None:
            raise ValueError("verified API binding requires platform_task_id")
        existing = self.store.get_binding(binding.binding_id)
        if existing is not None:
            if existing != binding:
                raise ValueError("binding_id already exists with different immutable fields")
            return existing
        self.store.save_binding(binding)
        return binding

    def block(self, request: ScheduledRunRequest, reason: str) -> ScheduledRunReceipt:
        """Persist a fail-closed receipt for a known binding and schedule bucket."""
        request = ScheduledRunRequest.model_validate(request)
        checkpoint = self.store.get_scheduled_checkpoint(
            request.binding_id, request.schedule_bucket
        )
        if checkpoint is not None:
            request = self._merge_checkpoint_request(checkpoint, request)
        return self._blocked_receipt(request, reason)

    @staticmethod
    def _stable_request_identity(request: ScheduledRunRequest) -> tuple[object, ...]:
        return (
            request.run_id,
            request.binding_id,
            request.schedule_bucket,
            request.window,
            tuple(sorted(domain.value for domain in request.domains)),
            request.policy_hash,
            request.idempotency_key,
        )

    @staticmethod
    def _has_prepared_inputs(request: ScheduledRunRequest) -> bool:
        return bool(
            request.source_coverage_artifact_ids
            or request.source_interval_start is not None
            or request.source_interval_end is not None
            or request.semantic_capability_receipt_id is not None
            or request.semantic_result_artifact_ids
        )

    def _merge_checkpoint_request(
        self,
        checkpoint: Mapping[str, Any],
        incoming: ScheduledRunRequest,
    ) -> ScheduledRunRequest:
        staged = ScheduledRunRequest.model_validate(checkpoint["request"])
        if self._stable_request_identity(staged) != self._stable_request_identity(incoming):
            raise ValueError("schedule bucket already has a different request identity")
        if not self._has_prepared_inputs(incoming):
            return staged
        staged_prepared = self._has_prepared_inputs(staged)
        if staged_prepared and incoming.requested_at != staged.requested_at:
            raise ValueError("prepared scheduled input cannot change an already-frozen cutoff")
        if not staged_prepared and incoming.requested_at < staged.requested_at:
            raise ValueError("prepared scheduled cutoff cannot precede the original wake time")
        if staged.source_revision_set and incoming.source_revision_set:
            if staged.source_revision_set != incoming.source_revision_set:
                raise ValueError("scheduled source revisions changed within one pending bucket")
        if staged.source_coverage_artifact_ids:
            if not set(staged.source_coverage_artifact_ids).issubset(
                incoming.source_coverage_artifact_ids
            ):
                raise ValueError("prepared scheduled source reports cannot remove staged evidence")
        staged_interval = (staged.source_interval_start, staged.source_interval_end)
        incoming_interval = (incoming.source_interval_start, incoming.source_interval_end)
        if (
            any(value is not None for value in staged_interval)
            and staged_interval != incoming_interval
        ):
            raise ValueError("prepared scheduled source interval changed within one pending bucket")
        if (
            staged.semantic_capability_receipt_id is not None
            and incoming.semantic_capability_receipt_id != staged.semantic_capability_receipt_id
        ):
            raise ValueError("scheduled semantic receipt changed within one pending bucket")
        if staged.semantic_result_artifact_ids and not set(
            staged.semantic_result_artifact_ids
        ).issubset(incoming.semantic_result_artifact_ids):
            raise ValueError("scheduled semantic results cannot remove staged evidence")
        merged = staged.model_copy(
            update={
                "requested_at": (
                    incoming.requested_at if not staged_prepared else staged.requested_at
                ),
                "source_revision_set": incoming.source_revision_set or staged.source_revision_set,
                "source_coverage_artifact_ids": tuple(
                    sorted(
                        set(staged.source_coverage_artifact_ids)
                        | set(incoming.source_coverage_artifact_ids)
                    )
                ),
                "source_interval_start": incoming.source_interval_start
                or staged.source_interval_start,
                "source_interval_end": incoming.source_interval_end or staged.source_interval_end,
                "semantic_capability_receipt_id": incoming.semantic_capability_receipt_id
                or staged.semantic_capability_receipt_id,
                "semantic_result_artifact_ids": tuple(
                    sorted(
                        set(staged.semantic_result_artifact_ids)
                        | set(incoming.semantic_result_artifact_ids)
                    )
                ),
            }
        )
        return ScheduledRunRequest.model_validate(merged)

    @staticmethod
    def _semantic_result_hash(result: ScheduledSemanticSubjectResult) -> str:
        return content_hash(
            result.model_dump(
                mode="python",
                exclude={"result_id", "result_hash"},
            )
        )

    @staticmethod
    def _digest_hash(digest: MaterialChangeDigest) -> str:
        return content_hash(digest.model_dump(mode="python", exclude={"digest_id"}))

    def register_semantic_result(
        self,
        result: ScheduledSemanticSubjectResult,
        *,
        digest: MaterialChangeDigest | None = None,
    ) -> str:
        """Register one typed semantic subject result without creating economic facts."""
        result = ScheduledSemanticSubjectResult.model_validate(result)
        result_hash = self._semantic_result_hash(result)
        expected_result_id = (
            f"scheduled-semantic-result-{uuid.uuid5(uuid.NAMESPACE_URL, result_hash)}"
        )
        if result.result_hash != result_hash or result.result_id != expected_result_id:
            raise ValueError("scheduled semantic result identity does not bind its typed payload")
        evidence_hashes: list[str] = []
        for artifact_id in result.evidence_artifact_ids:
            record = self.source_audit.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(
                    "scheduled semantic result cites an unregistered evidence artifact"
                )
            evidence_hashes.append(str(record["object_hash"]))
        if result.material_change:
            if digest is None or result.digest_artifact_id is None:
                raise ValueError("material semantic result requires its typed digest")
            digest = MaterialChangeDigest.model_validate(digest)
            digest_hash = self._digest_hash(digest)
            expected_digest_id = f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, digest_hash)}"
            artifact_id = f"MaterialChangeDigest:{digest_hash}"
            if digest.digest_id != expected_digest_id or result.digest_artifact_id != artifact_id:
                raise ValueError(
                    "scheduled semantic digest identity does not bind its typed payload"
                )
            if (
                digest.run_id != result.run_id
                or digest.domain is not result.domain
                or normalized_instrument(digest.instrument_id)
                != normalized_instrument(result.instrument_id)
                or digest.as_of != result.as_of
                or not set(digest.source_artifact_ids).issubset(result.evidence_artifact_ids)
            ):
                raise ValueError("scheduled semantic digest does not match its subject result")
            reference = self.source_audit.objects.put_json(digest.model_dump(mode="json"))
            self.source_audit.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="MaterialChangeDigest",
                schema_version="material-change-digest-v1",
                object_hash=reference.sha256,
                input_hashes=evidence_hashes,
            )
        elif digest is not None:
            raise ValueError("no-change semantic result cannot attach a digest")
        reference = self.source_audit.objects.put_json(result.model_dump(mode="json"))
        artifact_id = f"ScheduledSemanticSubjectResult:{result_hash}"
        self.source_audit.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ScheduledSemanticSubjectResult",
            schema_version=result.schema_version,
            object_hash=reference.sha256,
            input_hashes=evidence_hashes,
        )
        return artifact_id

    def stage(self, request: ScheduledRunRequest) -> ScheduledRunRequest:
        """Bind prepared source/semantic references to the one pending bucket identity."""
        from astock.investor_orchestration.run_ownership import schedule_run_ownership

        request = ScheduledRunRequest.model_validate(request)
        with schedule_run_ownership(self.store.path, request.binding_id, request.schedule_bucket):
            return self._stage_owned(request)

    def _stage_owned(self, request: ScheduledRunRequest) -> ScheduledRunRequest:
        if request.requested_at.tzinfo is None or request.requested_at > utc_now():
            raise ValueError("scheduled request time must be aware and not in the future")
        binding = self.store.get_binding(request.binding_id)
        if binding is None or not binding.active:
            raise ValueError("cannot stage an unknown or inactive scheduled binding")
        policy = self.store.get_scheduled_policy(binding.policy_id)
        if (
            policy is None
            or request.policy_hash != binding.policy_hash
            or policy.policy_hash != binding.policy_hash
        ):
            raise ValueError("cannot stage a request for a different scheduled policy")
        self._validate_policy(policy)
        if binding.confirmed_at is None or not binding.consent_hash:
            raise ValueError("explicit scheduled consent is required before staging")
        if binding.execution_surface not in {"DESKTOP_LOCAL", "LOCAL_DAEMON"}:
            raise ValueError("scheduled local state staging requires a local execution surface")
        if request.window not in policy.windows:
            raise ValueError("scheduled window is not allowed by the bound policy")
        if not request.domains or any(domain not in policy.domains for domain in request.domains):
            raise ValueError("scheduled domains are not allowed by the bound policy")
        final = self.store.get_scheduled_receipt(
            binding_id=request.binding_id,
            schedule_bucket=request.schedule_bucket,
        )
        checkpoint = self.store.get_scheduled_checkpoint(
            request.binding_id, request.schedule_bucket
        )
        if final is not None:
            if checkpoint is None:
                raise ValueError("scheduled bucket already has a final receipt")
            resolved = self._merge_checkpoint_request(checkpoint, request)
            if self._request_fingerprint(resolved) != final.request_fingerprint:
                raise ValueError(
                    "scheduled bucket already finalized with different prepared inputs"
                )
            return resolved
        resolved = (
            request if checkpoint is None else self._merge_checkpoint_request(checkpoint, request)
        )
        self.store.save_scheduled_checkpoint(
            resolved,
            status="READY" if self._has_prepared_inputs(resolved) else "PENDING_INPUTS",
        )
        return resolved

    @staticmethod
    def _request_fingerprint(request: ScheduledRunRequest) -> str:
        return content_hash(
            {
                "binding_id": request.binding_id,
                "schedule_bucket": request.schedule_bucket,
                "window": request.window,
                "domains": tuple(sorted(domain.value for domain in request.domains)),
                "policy_hash": request.policy_hash,
                "source_revision_set": request.source_revision_set,
                **(
                    {
                        "run_id": request.run_id,
                        "requested_at": request.requested_at,
                        "source_coverage_artifact_ids": tuple(
                            sorted(request.source_coverage_artifact_ids)
                        ),
                        "source_interval_start": request.source_interval_start,
                        "source_interval_end": request.source_interval_end,
                        "semantic_capability_receipt_id": request.semantic_capability_receipt_id,
                        "semantic_result_artifact_ids": tuple(
                            sorted(request.semantic_result_artifact_ids)
                        ),
                    }
                    if request.source_coverage_artifact_ids
                    or request.source_interval_start is not None
                    or request.semantic_capability_receipt_id is not None
                    or request.semantic_result_artifact_ids
                    else {}
                ),
            }
        )

    def _existing_receipt(
        self,
        request: ScheduledRunRequest,
        request_fingerprint: str,
    ) -> ScheduledRunReceipt | None:
        existing = self.store.get_scheduled_receipt(idempotency_key=request.idempotency_key)
        if existing is None:
            existing = self.store.get_scheduled_receipt(
                binding_id=request.binding_id,
                schedule_bucket=request.schedule_bucket,
            )
        if existing is None:
            return None
        if existing.request_fingerprint is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise ValueError("schedule bucket already completed with a different request")
        elif existing.run_id != request.run_id:
            raise ValueError("legacy schedule bucket already completed by a different run")
        return existing

    def run(self, request: ScheduledRunRequest) -> ScheduledRunReceipt:
        from astock.investor_orchestration.run_ownership import schedule_run_ownership

        request = ScheduledRunRequest.model_validate(request.model_dump())
        with schedule_run_ownership(self.store.path, request.binding_id, request.schedule_bucket):
            return self._run_owned(request)

    def _run_owned(self, request: ScheduledRunRequest) -> ScheduledRunReceipt:
        request = ScheduledRunRequest.model_validate(request)
        if request.requested_at.tzinfo is None or request.requested_at > utc_now():
            raise ValueError("scheduled request time must be aware and not in the future")
        binding = self.store.get_binding(request.binding_id)
        if binding is None or not binding.active:
            return self._blocked_receipt(request, "BINDING_MISSING_OR_INACTIVE")
        policy = self.store.get_scheduled_policy(binding.policy_id)
        if policy is None:
            return self._blocked_receipt(request, "POLICY_MISSING")
        if request.policy_hash != binding.policy_hash or policy.policy_hash != binding.policy_hash:
            raise ValueError("scheduled policy binding changed")
        self._validate_policy(policy)
        if binding.confirmed_at is None or not binding.consent_hash:
            return self._blocked_receipt(request, "EXPLICIT_CONSENT_REQUIRED")
        if binding.execution_surface not in {"DESKTOP_LOCAL", "LOCAL_DAEMON"}:
            return self._blocked_receipt(request, "LOCAL_STATE_EXECUTION_SURFACE_REQUIRED")
        if request.window not in policy.windows:
            return self._blocked_receipt(request, "WINDOW_NOT_ALLOWED")
        if not request.domains or any(domain not in policy.domains for domain in request.domains):
            return self._blocked_receipt(request, "DOMAIN_NOT_ALLOWED")
        checkpoint = self.store.get_scheduled_checkpoint(
            request.binding_id, request.schedule_bucket
        )
        if checkpoint is not None:
            request = self._merge_checkpoint_request(checkpoint, request)
        request_fingerprint = self._request_fingerprint(request)
        existing = self._existing_receipt(request, request_fingerprint)
        if existing is not None:
            self._resume_notification(request, existing, policy)
            return existing
        prepared_inputs = self._has_prepared_inputs(request)
        self.store.save_scheduled_checkpoint(
            request,
            status="READY" if prepared_inputs else "PENDING_INPUTS",
        )
        if not prepared_inputs:
            return self._pending_attempt(request, request_fingerprint)
        investor_request = InvestorRequestEnvelope(
            request_id=f"scheduled:{request.run_id}",
            question_time=request.requested_at,
            user_timezone=policy.market_timezone,
            market_timezone=policy.market_timezone,
            raw_text=f"scheduled research {request.window.value}",
            normalized_intent=RequestIntent.MONITOR,
            side_effect=SideEffectClass.META,
            entity_ids=(),
            idempotency_key=request.idempotency_key,
            metadata={
                "binding_id": request.binding_id,
                "schedule_bucket": request.schedule_bucket,
                "disclosure_level": policy.disclosure_level.value,
            },
        )
        preflight = self._preflight_for_run(investor_request, request)
        degradation_reasons: list[str] = []
        if preflight.freshness != "FRESH":
            degradation_reasons.append("PREFLIGHT_NOT_FRESH")
        paper_replay_ids: tuple[str, ...] = ()
        subjects = self._subjects_by_domain(preflight)
        if policy.source_audit_policy_hash != content_hash(self.source_audit.policy):
            if request.source_coverage_artifact_ids:
                raise ValueError(
                    "source-audit policy is not bound to the consented scheduled policy"
                )
            degradation_reasons.append("SOURCE_AUDIT_POLICY_UNBOUND")
        selected_subjects = {domain: subjects.get(domain, ()) for domain in request.domains}
        source_coverage = self.source_audit.bind_run(
            run_id=request.run_id,
            window=request.window,
            requested_at=request.requested_at,
            subjects=selected_subjects,
            report_ids=request.source_coverage_artifact_ids,
            interval_start=request.source_interval_start,
            interval_end=request.source_interval_end,
        )
        capability_receipt_id = None
        if source_coverage.expected_subject_count > 0:
            capability_coverage = self.source_audit.register_run_coverage(
                run_id=request.run_id,
                window=request.window,
                requested_at=request.requested_at,
                subjects=selected_subjects,
                report_ids=request.source_coverage_artifact_ids,
                interval_start=request.source_interval_start,
                interval_end=request.source_interval_end,
                semantic_capability_receipt_id=request.semantic_capability_receipt_id,
            )
            capability_receipt_id = capability_coverage.receipt_id
            if not capability_coverage.semantic_coverage_complete:
                degradation_reasons.append("SEMANTIC_CAPABILITY_COVERAGE_UNAVAILABLE")
        degradation_reasons.extend(source_coverage.degradation_reasons)
        semantic_digests = self._registered_semantic_digests(
            request,
            selected_subjects,
            degradation_reasons,
        )
        digests: list[MaterialChangeDigest] = []
        if semantic_digests is not None:
            for digest in semantic_digests:
                self._enforce_domain_action(policy, digest)
                digests.append(digest)
        else:
            for domain in request.domains:
                selected = subjects.get(domain, ())
                if len(selected) > policy.max_subjects_per_run:
                    degradation_reasons.append("SUBJECT_COVERAGE_TRUNCATED")
                for instrument_id in selected[: policy.max_subjects_per_run]:
                    disclosed = minimal_analysis_context(
                        preflight,
                        policy,
                        domain=domain,
                        instrument_id=instrument_id,
                        window=request.window,
                    )
                    digest = self.analyzer(
                        domain=domain,
                        instrument_id=instrument_id,
                        window=request.window,
                        preflight=disclosed,
                        run_id=request.run_id,
                    )
                    if digest is None:
                        degradation_reasons.append(
                            f"ANALYSIS_RESULT_UNAVAILABLE:{domain.value}:{instrument_id}"
                        )
                        continue
                    digest = MaterialChangeDigest.model_validate(digest)
                    if (
                        digest.run_id != request.run_id
                        or digest.domain != domain
                        or digest.instrument_id != instrument_id
                        or digest.as_of.tzinfo is None
                        or digest.as_of > preflight.as_of
                    ):
                        raise ValueError(
                            "scheduled analysis output does not match its subject/run/time"
                        )
                    self._enforce_domain_action(policy, digest)
                    digests.append(digest)
        if not degradation_reasons:
            paper_replay_ids = self._replay_confirmed_paper_orders(
                policy=policy,
                request=request,
                preflight=preflight,
                degradation_reasons=degradation_reasons,
            )
        receipt = self._persist_completed_run(
            request,
            request_fingerprint,
            policy,
            preflight,
            digests,
            paper_replay_ids,
            degradation_reasons,
            capability_receipt_id,
            source_coverage,
        )
        self._resume_notification(request, receipt, policy)
        return receipt

    def _preflight_for_run(
        self,
        investor_request: InvestorRequestEnvelope,
        request: ScheduledRunRequest,
    ) -> Any:
        semantic_receipt_id = request.semantic_capability_receipt_id
        if semantic_receipt_id is None:
            return self.preflight_service.build(investor_request)
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM capability_coverage_receipts "
                "WHERE receipt_id=? AND request_id=?",
                (semantic_receipt_id, investor_request.request_id),
            ).fetchone()
        if row is None:
            raise ValueError("scheduled semantic capability receipt is unavailable")
        coverage = CapabilityCoverageReceipt.model_validate_json(str(row["payload_json"]))
        preflight = self.store.get_preflight_for_request(investor_request.request_id)
        verifier = RegisteredOutputVerifier(self.store)
        if (
            preflight is None
            or coverage.preflight_receipt_id != preflight.receipt_id
            or not verifier.authenticated_preflight(preflight)
            or not verifier.authenticated_coverage(
                investor_request.request_id,
                semantic_receipt_id,
                coverage.receipt_hash,
            )
        ):
            raise ValueError("scheduled semantic preflight or coverage is not authentic")
        if preflight.as_of != request.requested_at:
            raise ValueError("scheduled semantic preflight time differs from this run")
        current_revisions = CanonicalStateProjectionReader(self.store).revision_vector(
            {
                "subjects": ("research_subject_events",),
                "regime": ("market_regime_snapshots_v2",),
            }
        )
        drift_scopes = tuple(
            scope
            for scope in ("actual", "paper", "monitor", "regime")
            if current_revisions.get(scope) != preflight.source_revision_vector.get(scope)
        )
        if drift_scopes:
            raise ValueError(
                "scheduled semantic preflight is stale after canonical state changed: "
                + ",".join(drift_scopes)
            )
        return preflight

    def _registered_semantic_digests(
        self,
        request: ScheduledRunRequest,
        subjects: Mapping[ScheduledDomain, tuple[str, ...]],
        degradation_reasons: list[str],
    ) -> tuple[MaterialChangeDigest, ...] | None:
        if request.semantic_capability_receipt_id is None:
            return None
        expected = {
            (domain, normalized_instrument(instrument_id))
            for domain in request.domains
            for instrument_id in subjects.get(domain, ())
        }
        if not request.semantic_result_artifact_ids:
            for domain, instrument_id in sorted(
                expected, key=lambda item: (item[0].value, item[1])
            ):
                degradation_reasons.append(
                    f"SEMANTIC_RESULT_UNAVAILABLE:{domain.value}:{instrument_id}"
                )
            return ()
        source_reports = set(request.source_coverage_artifact_ids)
        seen: set[tuple[ScheduledDomain, str]] = set()
        digests: list[MaterialChangeDigest] = []
        for artifact_id in sorted(request.semantic_result_artifact_ids):
            record = self.source_audit.state.artifact_record(artifact_id)
            if record is None or record["type"] != "ScheduledSemanticSubjectResult":
                raise ValueError("scheduled semantic result is not a registered typed artifact")
            result = ScheduledSemanticSubjectResult.model_validate_json(
                self.source_audit.objects.get_bytes(str(record["object_hash"]))
            )
            result_hash = self._semantic_result_hash(result)
            expected_result_id = (
                f"scheduled-semantic-result-{uuid.uuid5(uuid.NAMESPACE_URL, result_hash)}"
            )
            if (
                result.result_hash != result_hash
                or result.result_id != expected_result_id
                or artifact_id != f"ScheduledSemanticSubjectResult:{result_hash}"
                or result.run_id != request.run_id
                or result.as_of != request.requested_at
            ):
                raise ValueError(
                    "scheduled semantic result identity does not match the pending run"
                )
            key = (result.domain, normalized_instrument(result.instrument_id))
            if key not in expected or key in seen:
                raise ValueError("scheduled semantic result has an unexpected or duplicate subject")
            evidence = set(result.evidence_artifact_ids)
            if not evidence or not evidence.issubset(source_reports):
                raise ValueError(
                    "scheduled semantic result is not grounded in frozen source reports"
                )
            seen.add(key)
            if result.digest_artifact_id is None:
                continue
            digest_record = self.source_audit.state.artifact_record(result.digest_artifact_id)
            if digest_record is None or digest_record["type"] != "MaterialChangeDigest":
                raise ValueError("scheduled semantic digest is not a registered typed artifact")
            digest = MaterialChangeDigest.model_validate_json(
                self.source_audit.objects.get_bytes(str(digest_record["object_hash"]))
            )
            digest_hash = self._digest_hash(digest)
            if (
                result.digest_artifact_id != f"MaterialChangeDigest:{digest_hash}"
                or digest.digest_id != f"digest-{uuid.uuid5(uuid.NAMESPACE_URL, digest_hash)}"
                or digest.run_id != request.run_id
                or digest.domain is not result.domain
                or normalized_instrument(digest.instrument_id) != key[1]
                or digest.as_of != request.requested_at
                or not set(digest.source_artifact_ids).issubset(evidence)
            ):
                raise ValueError(
                    "scheduled semantic digest does not match its authenticated result"
                )
            digests.append(digest)
        for domain, instrument_id in sorted(
            expected - seen, key=lambda item: (item[0].value, item[1])
        ):
            degradation_reasons.append(
                f"SEMANTIC_RESULT_UNAVAILABLE:{domain.value}:{instrument_id}"
            )
        return tuple(digests)

    def _persist_completed_run(
        self,
        request: ScheduledRunRequest,
        request_fingerprint: str,
        policy: ScheduledResearchPolicy,
        preflight: Any,
        digests: Sequence[MaterialChangeDigest],
        paper_replay_ids: tuple[str, ...],
        degradation_reasons: list[str],
        capability_receipt_id: str | None,
        source_coverage: ScheduledRunSourceCoverage,
    ) -> ScheduledRunReceipt:
        if degradation_reasons:
            return self._persist_degraded_attempt(
                request=request,
                request_fingerprint=request_fingerprint,
                preflight=preflight,
                paper_replay_ids=paper_replay_ids,
                degradation_reasons=degradation_reasons,
                capability_receipt_id=capability_receipt_id,
                source_coverage=source_coverage,
            )
        # No external/model call occurs inside this short atomic write section.
        with self.store.transaction():
            existing = self._existing_receipt(request, request_fingerprint)
            if existing is not None:
                return existing
            revisions: list[WatchlistAnalysisRevision] = []
            for digest in digests:
                self.store.save_material_digest(digest)
                if digest.domain is ScheduledDomain.WATCHLIST:
                    revision = self._watchlist_revision(digest)
                    self.store.save_watchlist_revision(revision)
                    revisions.append(revision)
            notify = [digest for digest in digests if self._notification_required(policy, digest)]
            outcome = (
                ScheduledRunOutcome.MATERIAL_CHANGE
                if digests or paper_replay_ids
                else ScheduledRunOutcome.NO_MATERIAL_CHANGE
            )
            body = {
                "run_id": request.run_id,
                "binding_id": request.binding_id,
                "schedule_bucket": request.schedule_bucket,
                "request_fingerprint": request_fingerprint,
                "outcome": outcome,
                "preflight_receipt_id": preflight.receipt_id,
                "capability_receipt_id": capability_receipt_id,
                "source_coverage_artifact_ids": source_coverage.report_ids,
                "source_coverage_complete": source_coverage.all_families_checked,
                "source_checked_through": source_coverage.checked_through,
                "digest_ids": tuple(digest.digest_id for digest in digests),
                "watchlist_revision_ids": tuple(revision.revision_id for revision in revisions),
                "paper_replay_ids": paper_replay_ids,
                "degradation_reasons": (),
                "notification_required": bool(notify),
                "notification_reason": "MATERIAL_ACTUAL_OR_PAPER_HOLDING_CHANGE"
                if notify
                else None,
                "next_watermark": self._next_watermark(preflight),
                "economic_write_count": 0,
                "completed_at": utc_now(),
            }
            receipt_hash = content_hash(body)
            receipt = ScheduledRunReceipt(
                receipt_id=f"scheduled-receipt-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_hash)}",
                receipt_hash=receipt_hash,
                **body,
            )
            if not self.store.save_scheduled_receipt(
                receipt, idempotency_key=request.idempotency_key
            ):
                raise ValueError("scheduled receipt identity conflict")
            self.store.save_scheduled_checkpoint(
                request,
                status="SUCCEEDED",
                final_receipt_id=receipt.receipt_id,
                job_status="SUCCEEDED",
            )
            if notify:
                self._publish_notification(request, notify)
        return receipt

    def _pending_attempt(
        self,
        request: ScheduledRunRequest,
        request_fingerprint: str,
    ) -> ScheduledRunReceipt:
        reason = "RESEARCH_INPUTS_PENDING"
        attempt_identity = {
            "run_id": request.run_id,
            "binding_id": request.binding_id,
            "schedule_bucket": request.schedule_bucket,
            "request_fingerprint": request_fingerprint,
            "reason": reason,
        }
        attempt_hash = content_hash(attempt_identity)
        existing = self.store.get_scheduled_attempt(attempt_hash)
        if existing is not None:
            return existing
        body = {
            "run_id": request.run_id,
            "binding_id": request.binding_id,
            "schedule_bucket": request.schedule_bucket,
            "request_fingerprint": request_fingerprint,
            "outcome": ScheduledRunOutcome.DEGRADED,
            "preflight_receipt_id": None,
            "capability_receipt_id": None,
            "source_coverage_artifact_ids": (),
            "source_coverage_complete": False,
            "source_checked_through": None,
            "digest_ids": (),
            "watchlist_revision_ids": (),
            "paper_replay_ids": (),
            "degradation_reasons": (reason,),
            "notification_required": False,
            "notification_reason": reason,
            "next_watermark": None,
            "economic_write_count": 0,
            "completed_at": utc_now(),
        }
        receipt = ScheduledRunReceipt(
            receipt_id=f"scheduled-attempt-{uuid.uuid5(uuid.NAMESPACE_URL, attempt_hash)}",
            receipt_hash=attempt_hash,
            **body,
        )
        with self.store.transaction():
            self.store.save_scheduled_attempt(
                receipt,
                idempotency_key=request.idempotency_key,
                attempt_hash=attempt_hash,
            )
            self.store.save_scheduled_checkpoint(
                request,
                status="PENDING_INPUTS",
                degradation_reasons=(reason,),
            )
        return receipt

    def _persist_degraded_attempt(
        self,
        *,
        request: ScheduledRunRequest,
        request_fingerprint: str,
        preflight: Any,
        paper_replay_ids: tuple[str, ...],
        degradation_reasons: Sequence[str],
        capability_receipt_id: str | None,
        source_coverage: ScheduledRunSourceCoverage,
    ) -> ScheduledRunReceipt:
        reasons = tuple(dict.fromkeys(degradation_reasons))
        attempt_identity = {
            "run_id": request.run_id,
            "binding_id": request.binding_id,
            "schedule_bucket": request.schedule_bucket,
            "request_fingerprint": request_fingerprint,
            "preflight_receipt_id": preflight.receipt_id,
            "capability_receipt_id": capability_receipt_id,
            "source_coverage_artifact_ids": source_coverage.report_ids,
            "source_coverage_complete": source_coverage.all_families_checked,
            "source_checked_through": source_coverage.checked_through,
            "paper_replay_ids": paper_replay_ids,
            "degradation_reasons": reasons,
        }
        attempt_hash = content_hash(attempt_identity)
        existing_attempt = self.store.get_scheduled_attempt(attempt_hash)
        if existing_attempt is not None:
            self.store.save_scheduled_checkpoint(
                request,
                status="PENDING_INPUTS",
                degradation_reasons=reasons,
            )
            return existing_attempt
        body = {
            **attempt_identity,
            "outcome": ScheduledRunOutcome.DEGRADED,
            "digest_ids": (),
            "watchlist_revision_ids": (),
            "notification_required": False,
            "notification_reason": "RESEARCH_INPUTS_INCOMPLETE",
            "next_watermark": None,
            "economic_write_count": 0,
            "completed_at": utc_now(),
        }
        receipt = ScheduledRunReceipt(
            receipt_id=f"scheduled-attempt-{uuid.uuid5(uuid.NAMESPACE_URL, attempt_hash)}",
            receipt_hash=attempt_hash,
            **body,
        )
        with self.store.transaction():
            self.store.save_scheduled_attempt(
                receipt,
                idempotency_key=request.idempotency_key,
                attempt_hash=attempt_hash,
            )
            self.store.save_scheduled_checkpoint(
                request,
                status="PENDING_INPUTS",
                degradation_reasons=reasons,
            )
        return receipt

    def _resume_notification(
        self,
        request: ScheduledRunRequest,
        receipt: ScheduledRunReceipt,
        policy: ScheduledResearchPolicy,
    ) -> None:
        if not receipt.notification_required:
            return
        key = f"scheduled:{request.binding_id}:{request.schedule_bucket}"
        if self.outbox.get(key) is None:
            # Recover old receipts saved before the former non-atomic sink call.
            digests = []
            with self.store.connect() as connection:
                for digest_id in receipt.digest_ids:
                    row = connection.execute(
                        "SELECT payload_json FROM material_change_digests WHERE digest_id=?",
                        (digest_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            "undelivered notification is missing its persisted analysis"
                        )
                    digest = MaterialChangeDigest.model_validate_json(row[0])
                    if digest.run_id != receipt.run_id:
                        raise ValueError("notification analysis belongs to a different run")
                    if self._notification_required(policy, digest):
                        digests.append(digest)
            if not digests:
                raise ValueError("undelivered notification has no verified message content")
            self._publish_notification(request, digests)
        if self.notification_sink is not None:
            self.outbox.deliver(key, self.notification_sink)

    def _replay_confirmed_paper_orders(
        self,
        *,
        policy: ScheduledResearchPolicy,
        request: ScheduledRunRequest,
        preflight: Any,
        degradation_reasons: list[str],
    ) -> tuple[str, ...]:
        if ScheduledDomain.PAPER_HOLDING not in request.domains:
            return ()
        action_policy = policy.action_policy_by_domain[ScheduledDomain.PAPER_HOLDING]
        if action_policy is not ScheduledActionPolicy.REPLAY_CONFIRMED_RULES_ONLY:
            return ()
        confirmed_order_ids = tuple(
            sorted(
                order.order_id for order in preflight.context.paper.open_orders if order.confirmed
            )
        )
        if not confirmed_order_ids:
            return ()
        if not policy.confirmed_paper_replay_allowed:
            degradation_reasons.append("CONFIRMED_PAPER_REPLAY_NOT_ALLOWED")
            return ()
        if self.paper_replay_adapter is None:
            degradation_reasons.append("PAPER_REPLAY_ADAPTER_UNAVAILABLE")
            return ()
        try:
            replay_ids = tuple(
                self.paper_replay_adapter.replay_confirmed_orders(
                    run_id=request.run_id,
                    requested_at=request.requested_at,
                    order_ids=confirmed_order_ids,
                )
            )
        except Exception as exc:  # noqa: BLE001 - captured as a typed degradation
            degradation_reasons.append(f"PAPER_REPLAY_FAILED:{type(exc).__name__}")
            return ()
        if (
            not replay_ids
            or len(replay_ids) != len(set(replay_ids))
            or any(not replay_id.strip() for replay_id in replay_ids)
        ):
            degradation_reasons.append("PAPER_REPLAY_RESULT_INVALID")
            return ()
        return replay_ids

    def _subjects_by_domain(self, preflight: Any) -> dict[ScheduledDomain, tuple[str, ...]]:
        from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

        watchlist = sorted(
            event.instrument_id
            for event in ResearchSubjectRegistryService(self.store).current_watchlist(
                as_of=preflight.as_of
            )
        )
        paper = sorted({position.instrument_id for position in preflight.context.paper.positions})
        actual = sorted({position.instrument_id for position in preflight.context.actual.positions})
        return {
            ScheduledDomain.WATCHLIST: tuple(watchlist),
            ScheduledDomain.PAPER_HOLDING: tuple(paper),
            ScheduledDomain.ACTUAL_HOLDING: tuple(actual),
        }

    def _watchlist_revision(self, digest: MaterialChangeDigest) -> WatchlistAnalysisRevision:
        previous = self.store.latest_watchlist_revision(digest.instrument_id)
        status: Literal["ATTRACTIVE_WAIT", "RESEARCHING", "READY", "DEGRADED", "REMOVE"] = (
            "DEGRADED" if digest.severity == "CRITICAL" else "RESEARCHING"
        )
        body = {
            "instrument_id": digest.instrument_id,
            "as_of": digest.as_of,
            "thesis_status": status,
            "valuation_summary": None,
            "timing_summary": digest.impact_summary,
            "material_changes": (digest.change_summary,),
            "next_review_conditions": digest.action_conditions,
            "source_artifact_ids": digest.source_artifact_ids,
            "previous_revision_id": previous.revision_id if previous else None,
        }
        revision_hash = content_hash(body)
        return WatchlistAnalysisRevision(
            revision_id=f"watch-revision-{uuid.uuid5(uuid.NAMESPACE_URL, revision_hash)}",
            revision_hash=revision_hash,
            **body,
        )

    @staticmethod
    def _validate_policy(policy: ScheduledResearchPolicy) -> None:
        if policy.economic_writes_allowed:
            raise ValueError("scheduled research policy cannot allow economic writes")
        expected = {
            ScheduledDomain.WATCHLIST: ScheduledActionPolicy.ANALYSIS_ONLY,
            ScheduledDomain.ACTUAL_HOLDING: ScheduledActionPolicy.PUSH_ADVISORY_ONLY,
        }
        for domain, required_policy in expected.items():
            if (
                domain in policy.domains
                and policy.action_policy_by_domain.get(domain) != required_policy
            ):
                raise ValueError(f"unsafe action policy for {domain.value}")
        raw_paper_policy = policy.action_policy_by_domain.get(ScheduledDomain.PAPER_HOLDING)
        paper_policy = None if raw_paper_policy is None else ScheduledActionPolicy(raw_paper_policy)
        if ScheduledDomain.PAPER_HOLDING in policy.domains and paper_policy not in {
            ScheduledActionPolicy.PROPOSE_ONLY,
            ScheduledActionPolicy.REPLAY_CONFIRMED_RULES_ONLY,
        }:
            raise ValueError("paper scheduled policy must propose or replay confirmed rules only")
        if (
            paper_policy is ScheduledActionPolicy.REPLAY_CONFIRMED_RULES_ONLY
            and not policy.confirmed_paper_replay_allowed
        ):
            raise ValueError("confirmed paper replay requires an explicit policy opt-in")
        if (
            policy.confirmed_paper_replay_allowed
            and paper_policy is not ScheduledActionPolicy.REPLAY_CONFIRMED_RULES_ONLY
        ):
            raise ValueError("paper replay opt-in requires REPLAY_CONFIRMED_RULES_ONLY")
        if policy.disclosure_level is DisclosureLevel.EXPLICIT_FULL and not policy.field_allowlist:
            raise ValueError("EXPLICIT_FULL still requires an explicit field allowlist")

    @staticmethod
    def _enforce_domain_action(
        policy: ScheduledResearchPolicy, digest: MaterialChangeDigest
    ) -> None:
        action_policy = policy.action_policy_by_domain[digest.domain]
        if digest.actual_execution_allowed:
            raise ValueError("scheduled digest cannot allow actual execution")
        if (
            digest.domain is ScheduledDomain.ACTUAL_HOLDING
            and action_policy is not ScheduledActionPolicy.PUSH_ADVISORY_ONLY
        ):
            raise ValueError("actual holding digest must remain advisory")
        if digest.domain is ScheduledDomain.WATCHLIST and digest.action in {
            "ADD_REVIEW",
            "TRIM_REVIEW",
            "EXIT_REVIEW",
        }:
            raise ValueError("watchlist analysis cannot emit a position adjustment")

    @staticmethod
    def _notification_required(
        policy: ScheduledResearchPolicy, digest: MaterialChangeDigest
    ) -> bool:
        if digest.domain is ScheduledDomain.ACTUAL_HOLDING:
            return digest.severity in policy.material_severities or digest.action in {
                "ADD_REVIEW",
                "TRIM_REVIEW",
                "EXIT_REVIEW",
            }
        if digest.domain is ScheduledDomain.PAPER_HOLDING:
            return digest.severity == "CRITICAL"
        return False

    @staticmethod
    def _next_watermark(preflight: Any) -> str:
        if not preflight.context.material_events:
            return preflight.as_of.isoformat()
        return max(event.available_at for event in preflight.context.material_events).isoformat()

    def _publish_notification(
        self,
        request: ScheduledRunRequest,
        digests: Sequence[MaterialChangeDigest],
    ) -> None:
        from astock.research.presentation import audit_public_answer

        action_labels = {
            "WAIT": "继续观察",
            "RESEARCH": "补充研究",
            "REVIEW": "复核持仓依据",
            "HOLD": "复核继续持有条件",
            "ADD_REVIEW": "复核加仓条件",
            "TRIM_REVIEW": "复核减仓条件",
            "EXIT_REVIEW": "复核退出条件",
            "REMOVE": "移出观察",
        }
        lines = [
            f"{digest.instrument_id}：{digest.change_summary}；建议：{action_labels[digest.action]}"
            for digest in digests
        ]
        body = "\n".join(lines)
        audit = audit_public_answer(body)
        if not audit.safe_to_send:
            raise ValueError("notification public-content audit failed")
        self.outbox.publish(
            notification_key=f"scheduled:{request.binding_id}:{request.schedule_bucket}",
            title="持仓重大变化提醒",
            body=body,
            metadata={
                "binding_id": request.binding_id,
                "schedule_bucket": request.schedule_bucket,
                "digest_ids": [digest.digest_id for digest in digests],
            },
        )

    def _blocked_receipt(self, request: ScheduledRunRequest, reason: str) -> ScheduledRunReceipt:
        request_fingerprint = self._request_fingerprint(request)
        existing = self._existing_receipt(request, request_fingerprint)
        if existing is not None:
            return existing
        body = {
            "run_id": request.run_id,
            "binding_id": request.binding_id,
            "schedule_bucket": request.schedule_bucket,
            "request_fingerprint": request_fingerprint,
            "outcome": ScheduledRunOutcome.BLOCKED,
            "preflight_receipt_id": None,
            "capability_receipt_id": None,
            "digest_ids": (),
            "watchlist_revision_ids": (),
            "paper_replay_ids": (),
            "notification_required": False,
            "notification_reason": reason,
            "next_watermark": None,
            "economic_write_count": 0,
            "completed_at": utc_now(),
        }
        receipt_hash = content_hash(body)
        receipt = ScheduledRunReceipt(
            receipt_id=f"scheduled-receipt-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_hash)}",
            receipt_hash=receipt_hash,
            **body,
        )
        # Missing/invalid bindings cannot satisfy the FK. Persist only when the
        # binding exists; the typed blocked receipt is still returned to callers.
        if self.store.get_binding(request.binding_id) is not None:
            self.store.save_scheduled_receipt(
                receipt,
                idempotency_key=request.idempotency_key,
            )
            self.store.save_scheduled_checkpoint(
                request,
                status="BLOCKED",
                degradation_reasons=(reason,),
                final_receipt_id=receipt.receipt_id,
                job_status="PERMANENT_FAILED"
                if reason == "MISSED_RUN_EXPIRED"
                else "BLOCKED_MANUAL",
            )
        return receipt


def policy_from_config(
    config: Mapping[str, Any], *, source_audit_policy: ScheduledInputAuditPolicy | None = None
) -> ScheduledResearchPolicy:
    source_policy = source_audit_policy or load_input_audit_policy()
    domains = tuple(ScheduledDomain(name) for name in config["domains"])
    windows = tuple(ScheduledWindow(name) for name in config["windows"])
    action_policy = {
        domain: ScheduledActionPolicy(config["domains"][domain.value]["action_policy"])
        for domain in domains
    }
    body = {
        "policy_id": "scheduled-investor-tracking",
        "version": str(config["version"]),
        "domains": domains,
        "windows": windows,
        "market_timezone": str(config.get("market_timezone", "Asia/Shanghai")),
        "intraday_min_interval_minutes": int(
            config["windows"].get("INTRADAY", {}).get("minimum_interval_minutes", 60)
        ),
        "max_subjects_per_run": int(config.get("max_subjects_per_run", 50)),
        "material_severities": tuple(config.get("material_severities", ["HIGH", "CRITICAL"])),
        "disclosure_level": DisclosureLevel(config.get("disclosure_level", "MINIMUM")),
        "field_allowlist": tuple(config.get("field_allowlist", [])),
        "action_policy_by_domain": action_policy,
        "confirmed_paper_replay_allowed": bool(
            config["domains"][ScheduledDomain.PAPER_HOLDING.value].get(
                "allow_confirmed_order_replay", False
            )
        ),
        "source_audit_policy_hash": content_hash(source_policy),
        "economic_writes_allowed": False,
    }
    return ScheduledResearchPolicy(policy_hash=content_hash(body), **body)
