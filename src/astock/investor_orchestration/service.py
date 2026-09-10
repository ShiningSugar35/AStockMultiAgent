from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from astock.investor_orchestration.capabilities import (
    CapabilityExecutionResult,
    CapabilityExecutor,
    CapabilityHandler,
    CapabilityPlanner,
)
from astock.investor_orchestration.closure import (
    InvestmentClosureDecision,
    InvestmentRequestClosurePolicy,
    InvestmentRequestNotTerminalError,
)
from astock.investor_orchestration.gateway import InvestorAnswerGateway
from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    InvestorAnswer,
    InvestorAnswerDraft,
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    SubjectEventKind,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService


class CurrentRegisteredExecution(NamedTuple):
    """Transport over existing frozen artifacts; not another persisted clock."""

    original_request: InvestorRequestEnvelope
    decision_request: InvestorRequestEnvelope
    preflight: InvestorSessionPreflightReceipt
    plan: CapabilityExecutionPlan
    coverage: CapabilityCoverageReceipt


class InvestorOrchestrationService:
    """Request-level facade over preflight, capability execution and public output."""

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        *,
        preflight_service: InvestorSessionPreflightService | None = None,
        planner: CapabilityPlanner | None = None,
        gateway: InvestorAnswerGateway | None = None,
    ) -> None:
        self.store = store
        self.preflight_service = preflight_service or InvestorSessionPreflightService(store)
        self.planner = planner or CapabilityPlanner(store=store)
        self.gateway = gateway or InvestorAnswerGateway(store)
        self.subjects = ResearchSubjectRegistryService(store)

    def freeze_current_request(
        self, request: InvestorRequestEnvelope, *, artifact_ids: tuple[str, ...]
    ) -> InvestorRequestEnvelope:
        """Freeze registered acquisition without rewriting the original question clock."""
        from astock.investor_orchestration.decision_freeze import DecisionFreezeService
        from astock.investor_orchestration.models import SideEffectClass

        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        if (
            request.research_mode != "CURRENT"
            or str(request.metadata.get("analysis_mode", "CURRENT")).upper() != "CURRENT"
        ):
            raise ValueError("historical mode cannot opt into CURRENT acquisition freeze")
        if request.side_effect not in {
            SideEffectClass.READ,
            SideEffectClass.META,
            SideEffectClass.NONE,
        }:
            raise ValueError(
                "current freeze is read-only; economic requests use their original confirmation"
            )
        self._bind_original_request(request)
        return DecisionFreezeService(self.store).freeze(request, artifact_ids=artifact_ids)

    def execute_registered(
        self,
        request: InvestorRequestEnvelope,
        *,
        artifacts: Mapping[str, tuple[str, ...]],
    ) -> tuple[InvestorSessionPreflightReceipt, CapabilityExecutionPlan, CapabilityCoverageReceipt]:
        """Registered execution at the request's existing, unmodified evidence cutoff."""
        return self.execute_registered_inputs(request, artifacts)

    def execute_current_registered(
        self,
        request: InvestorRequestEnvelope,
        *,
        artifacts: Mapping[str, tuple[str, ...]],
    ) -> CurrentRegisteredExecution:
        """Explicit current freeze followed by the same read-only registered executor."""
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        if (
            request.research_mode != "CURRENT"
            or str(request.metadata.get("analysis_mode", "CURRENT")).upper() != "CURRENT"
        ):
            raise ValueError("historical mode must retain its fixed evidence cutoff")
        if set(artifacts) & set(self._built_in_handlers()):
            raise ValueError("registered inputs cannot override a built-in safety boundary")
        input_ids = tuple(sorted({identifier for ids in artifacts.values() for identifier in ids}))
        decision = self.freeze_current_request(request, artifact_ids=input_ids)
        preflight, plan, coverage = self.execute_registered_inputs(decision, artifacts)
        return CurrentRegisteredExecution(request, decision, preflight, plan, coverage)

    def _bind_original_request(self, request: InvestorRequestEnvelope) -> None:
        """A source request ID cannot silently acquire different semantics on retry."""
        from astock.core.object_store import ObjectStore
        from astock.core.state import StateStore
        from astock.investor_orchestration.utils import content_hash

        objects = ObjectStore(self.store.path.parent / "objects" / "sha256")
        state = StateStore(self.store.path)
        identity = f"InvestorRequestEnvelope:{content_hash(request.request_id)}"
        existing = state.artifact_record(identity)
        if existing is not None:
            if existing["type"] != "InvestorRequestEnvelope" or existing["schema_version"] != (
                "investor-request-envelope-v1"
            ):
                raise ValueError("request identity collision with another registered artifact")
            persisted = InvestorRequestEnvelope.model_validate_json(
                objects.get_bytes(str(existing["object_hash"]))
            )
            if persisted != request:
                raise ValueError("request identity is already bound to different content")
            return
        reference = objects.put_json(request.model_dump(mode="json"))
        state.register_artifact(
            artifact_id=identity,
            artifact_type="InvestorRequestEnvelope",
            schema_version="investor-request-envelope-v1",
            object_hash=reference.sha256,
            input_hashes=[],
        )

    def execute_registered_inputs(
        self,
        request: InvestorRequestEnvelope,
        capability_artifacts: Mapping[str, tuple[str, ...]],
    ) -> tuple[InvestorSessionPreflightReceipt, CapabilityExecutionPlan, CapabilityCoverageReceipt]:
        """Consume registered read-only domain results; never invoke economic adapters.

        The existing planner, executor and verifier own admission. Stable retries
        read the original receipt under the existing same-host ownership guard.
        """
        from astock.investor_orchestration.capabilities import (
            _BASE_NODES,
            validate_request_permissions,
        )
        from astock.investor_orchestration.models import SideEffectClass
        from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
        from astock.investor_orchestration.run_ownership import schedule_run_ownership
        from astock.investor_orchestration.utils import content_hash

        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        validate_request_permissions(request)
        if request.side_effect not in {
            SideEffectClass.READ,
            SideEffectClass.META,
            SideEffectClass.NONE,
        }:
            raise ValueError(
                "registered execution is read-only and cannot authorize economic writes"
            )
        references = dict(capability_artifacts)
        if set(references) & set(self._built_in_handlers()):
            raise ValueError("registered inputs cannot override a built-in safety boundary")
        if set(references) - set(_BASE_NODES):
            raise ValueError("registered inputs reference unknown capabilities")
        if any(
            not isinstance(ids, tuple)
            or not ids
            or not all(isinstance(value, str) and value.strip() for value in ids)
            for ids in references.values()
        ):
            raise ValueError("registered inputs require non-empty tuples of artifact identities")
        verifier = RegisteredOutputVerifier(self.store)
        self._bind_original_request(request)
        with schedule_run_ownership(
            self.store.path, "investor-registered-inputs", request.request_id
        ):
            with self.store.connect() as connection:
                row = connection.execute(
                    "SELECT c.payload_json,p.payload_json FROM capability_coverage_receipts c "
                    "JOIN capability_execution_plans p ON p.plan_id=c.plan_id "
                    "WHERE c.request_id=? ORDER BY c.rowid DESC LIMIT 1",
                    (request.request_id,),
                ).fetchone()
            if row is not None:
                coverage = CapabilityCoverageReceipt.model_validate_json(str(row[0]))
                plan = CapabilityExecutionPlan.model_validate_json(str(row[1]))
                preflight = self.store.get_preflight_for_request(request.request_id)
                if preflight is None or coverage.request_fingerprint != content_hash(request):
                    raise ValueError(
                        "registered request identity differs from its persisted execution"
                    )
                nodes = {node.capability_id: node for node in plan.nodes}
                records = {record.capability_id: record for record in coverage.records}
                if plan.policy_version != self.planner.policy_version or any(
                    key not in records or records[key].artifact_ids != ids
                    for key, ids in references.items()
                ):
                    raise ValueError("request is already bound to different inputs or policy")
                external_used = {
                    key
                    for key, record in records.items()
                    if key not in self._built_in_handlers() and record.artifact_ids
                }
                if external_used != set(references):
                    raise ValueError("request is already bound to different capability inputs")
                if not verifier.authenticated_preflight(
                    preflight
                ) or not verifier.authenticated_coverage(
                    request.request_id, coverage.receipt_id, coverage.receipt_hash
                ):
                    raise ValueError("persisted execution has not passed verified coverage")
                for key, ids in references.items():
                    verifier.verify(nodes[key], ids, request, preflight)
                return preflight, plan, coverage

            def handler_for(ids: tuple[str, ...]) -> CapabilityHandler:
                def consume(
                    _request: InvestorRequestEnvelope,
                    _preflight: InvestorSessionPreflightReceipt,
                ) -> CapabilityExecutionResult:
                    return CapabilityExecutionResult(artifact_ids=ids, reused=True)

                return consume

            from astock.investor_orchestration.models import CapabilityRequirement

            preflight, plan = self.prepare(request)
            selected = {node.capability_id: node for node in plan.nodes}
            if any(
                key not in selected or selected[key].requirement is CapabilityRequirement.PROHIBITED
                for key in references
            ):
                raise ValueError(
                    "registered capability is prohibited or not selected by this request"
                )
            handlers = {
                **{key: handler_for(ids) for key, ids in references.items()},
                **self._built_in_handlers(),
            }
            coverage = CapabilityExecutor(handlers, store=self.store).execute(
                plan, request, preflight
            )
            return preflight, plan, coverage

    def prepare(
        self,
        request: InvestorRequestEnvelope,
        *,
        scenario_requirements: Mapping[str, Any] | None = None,
    ) -> tuple[InvestorSessionPreflightReceipt, CapabilityExecutionPlan]:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        self._bind_original_request(request)
        preflight = self.preflight_service.build(request)
        plan = self.planner.plan(
            request,
            preflight,
            scenario_requirements=scenario_requirements,
        )
        return preflight, plan

    def execute(
        self,
        request: InvestorRequestEnvelope,
        *,
        handlers: Mapping[str, CapabilityHandler],
        scenario_requirements: Mapping[str, Any] | None = None,
    ) -> tuple[
        InvestorSessionPreflightReceipt,
        CapabilityExecutionPlan,
        CapabilityCoverageReceipt,
    ]:
        preflight, plan = self.prepare(
            request,
            scenario_requirements=scenario_requirements,
        )
        # Fixed request-time, preflight, registry and gateway handlers are safety
        # boundaries and cannot be replaced by scenario- or model-provided code.
        merged_handlers = {**handlers, **self._built_in_handlers()}
        coverage = CapabilityExecutor(merged_handlers, store=self.store).execute(
            plan,
            request,
            preflight,
        )
        return preflight, plan, coverage

    def closure_status(
        self,
        request: InvestorRequestEnvelope,
        plan: CapabilityExecutionPlan,
        coverage: CapabilityCoverageReceipt,
        *,
        automatic_resolution_exhausted: bool = False,
        private_user_input_required: bool = False,
    ) -> InvestmentClosureDecision:
        """Return the canonical same-request terminal decision for an investor request."""

        return InvestmentRequestClosurePolicy.evaluate(
            request,
            plan,
            coverage,
            automatic_resolution_exhausted=automatic_resolution_exhausted,
            private_user_input_required=private_user_input_required,
        )

    def answer(
        self,
        request: InvestorRequestEnvelope,
        draft: InvestorAnswerDraft,
        *,
        handlers: Mapping[str, CapabilityHandler],
        scenario_requirements: Mapping[str, Any] | None = None,
    ) -> InvestorAnswer:
        preflight, plan, coverage = self.execute(
            request,
            handlers=handlers,
            scenario_requirements=scenario_requirements,
        )
        closure = self.closure_status(request, plan, coverage)
        if closure.same_request_continuation_required:
            raise InvestmentRequestNotTerminalError(
                "material investment request has unfinished automatic research capabilities: "
                + ",".join(closure.missing_capabilities)
            )
        return self.gateway.render(
            draft,
            preflight=preflight,
            coverage=coverage,
        )

    def publish_registered(
        self, *, draft_artifact_id: str, coverage_receipt_id: str
    ) -> InvestorAnswer:
        """Read the exact completed request; never repeat an economic handler."""
        return self.gateway.publish_registered(
            draft_artifact_id=draft_artifact_id,
            coverage_receipt_id=coverage_receipt_id,
        )

    def publish_verified(self, *, coverage_receipt_id: str) -> InvestorAnswer:
        """Generate and audit the answer from already-verified domain artifacts."""
        return self.gateway.publish_verified(coverage_receipt_id=coverage_receipt_id)

    def _built_in_handlers(self) -> dict[str, CapabilityHandler]:
        return {
            "REQUEST_TIME": lambda request, _: CapabilityExecutionResult(
                artifact_ids=(f"request-time:{request.question_time.isoformat()}",)
            ),
            "ENTITY_IDENTITY": lambda request, _: CapabilityExecutionResult(
                artifact_ids=tuple(f"entity:{item}" for item in request.entity_ids)
                or ("entity:none",)
            ),
            "SESSION_PREFLIGHT": lambda _, preflight: CapabilityExecutionResult(
                artifact_ids=(preflight.receipt_id,),
                source_revision=preflight.context.aggregate_revision,
                reused=preflight.built_from_cache,
            ),
            "MARKET_REGIME": self._market_regime_handler,
            "SUBJECT_REGISTRY": self._subject_registry_handler,
            "RESPONSE_GATEWAY": lambda *_: CapabilityExecutionResult(
                artifact_ids=("gateway:required",)
            ),
        }

    @staticmethod
    def _market_regime_handler(
        _request: InvestorRequestEnvelope,
        preflight: InvestorSessionPreflightReceipt,
    ) -> CapabilityExecutionResult:
        if not preflight.regime.available or preflight.regime.snapshot_id is None:
            return CapabilityExecutionResult(
                degraded_reason=preflight.regime.reason or "REGIME_UNAVAILABLE"
            )
        return CapabilityExecutionResult(
            artifact_ids=(preflight.regime.snapshot_id,),
            source_revision=preflight.context.aggregate_revision,
        )

    def _subject_registry_handler(
        self,
        request: InvestorRequestEnvelope,
        _preflight: InvestorSessionPreflightReceipt,
    ) -> CapabilityExecutionResult:
        event_ids: list[str] = []
        for instrument_id in request.entity_ids:
            event = self.subjects.append(
                instrument_id=instrument_id,
                event_type=SubjectEventKind.MENTIONED,
                request_id=request.request_id,
                reason="resolved in investor request",
                available_at=request.question_time,
                idempotency_key=(f"mentioned:{request.request_id}:{instrument_id}"),
            )
            event_ids.append(event.event_id)
        return CapabilityExecutionResult(
            artifact_ids=tuple(event_ids) or ("subject-registry:no-entity",)
        )
