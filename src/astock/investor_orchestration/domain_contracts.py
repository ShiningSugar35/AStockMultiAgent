"""Read-only domain admission over existing typed research and account artifacts.

A ResearchRoleOutput is a container, not proof that governance, catalysts and
independent review are interchangeable. Resolve its canonical plan, task and
completed checkpoint before accepting it for a capability.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from astock.evidence.repository import EvidenceRepository
from astock.investor_orchestration.models import CapabilityNode, InvestorRequestEnvelope
from astock.schemas.financial import FinancialIntegrityEvidencePack
from astock.schemas.full_research import RecommendationResearchReceipt
from astock.schemas.institutional_research import (
    FundamentalModelBundle,
    IndustryProfile,
    InstitutionalDecisionContext,
    ValuationPack,
)
from astock.schemas.knowledge import HoldingReviewPack
from astock.schemas.paper import ReplayExecutionReport
from astock.schemas.portfolio import PortfolioAnalysisReport
from astock.schemas.portfolio_decision import ETFResearchMetrics
from astock.schemas.research_team import (
    FullResearchInputReadinessReport,
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTeamPlan,
)

if TYPE_CHECKING:
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier

_ROLE_CONTRACTS = {
    "GOVERNANCE": ("GOVERNANCE", "GOVERNANCE_QUALITY"),
    "EVENT_RESEARCH": ("CATALYST", "CATALYST_RISK"),
    "RED_TEAM": ("REVIEWER", "INDEPENDENT_REVIEW"),
}


class DomainContractAudit:
    def __init__(self, verifier: RegisteredOutputVerifier) -> None:
        self.verifier = verifier
        self._task_results: dict[tuple[str, str], ResearchRoleResult] = {}
        self._checkpoint_witnesses: dict[str, object] = {}

    def check(
        self,
        node: CapabilityNode,
        artifact_id: str,
        output: BaseModel,
        request: InvestorRequestEnvelope,
    ) -> None:
        self._task_results.clear()
        self._checkpoint_witnesses.clear()
        self._check(node, artifact_id, output, request)
        for key, expected in self._checkpoint_witnesses.items():
            if self.verifier.state.get_checkpoint("research-team-task", key) != expected:
                raise ValueError("research dependency changed during output verification")

    def _check(
        self,
        node: CapabilityNode,
        artifact_id: str,
        output: BaseModel,
        request: InvestorRequestEnvelope,
    ) -> None:
        payload = output.model_dump(mode="json")
        if isinstance(output, ReplayExecutionReport):
            from astock.investor_orchestration.replay_output import verify_replay_output

            verify_replay_output(self.verifier, output, request)
        elif isinstance(output, FinancialIntegrityEvidencePack):
            if output.status.value != "SUCCEEDED" or output.coverage_status.value != "COMPLETE":
                raise ValueError(
                    "financial domain completion requires SUCCEEDED and COMPLETE coverage together"
                )
            if not output.input_fact_ids or not output.source_snapshot_ids or not output.pit_ids:
                raise ValueError("financial integrity is missing its canonical input lineage")
            if output.hard_blocks:
                raise ValueError("financial integrity has unresolved hard blocks")
        elif isinstance(output, IndustryProfile):
            if output.status.value != "READY" or output.missing_codes:
                raise ValueError("industry research has not reached READY")
            self._evidence(output.evidence_ids, request.evidence_cutoff)
        elif isinstance(output, ValuationPack):
            if output.status.value != "READY" or output.blocking_codes or not output.results:
                raise ValueError("valuation has not reached its deterministic completion gate")
            self._evidence(output.assumption_evidence_ids, request.evidence_cutoff)
        elif isinstance(output, InstitutionalDecisionContext):
            bundle = self.verifier.load(
                output.fundamental_model_bundle_artifact_id, FundamentalModelBundle
            )
            assert isinstance(bundle, FundamentalModelBundle)
            if bundle.company_id != output.company_id or bundle.as_of != output.as_of:
                raise ValueError("institutional decision and fundamental bundle identities differ")
            if bundle.status.value != "READY" or bundle.blocking_codes:
                raise ValueError("institutional decision requires a READY fundamental bundle")
            self.verifier._check_time(bundle.model_dump(mode="json"), request.evidence_cutoff)
            self.verifier._verify_linked_hashes(bundle.model_dump(mode="json"))
            self._evidence(output.evidence_ids, request.evidence_cutoff)
        elif isinstance(output, ResearchRoleOutput):
            self._role(node, artifact_id, output, request)
        elif isinstance(output, FullResearchInputReadinessReport):
            self._readiness(output, request)
        elif isinstance(output, RecommendationResearchReceipt):
            from astock.investor_orchestration.full_research import (
                FullResearchRecommendationService,
            )

            if artifact_id != output.receipt_id:
                raise ValueError("full-research capability must expose the sealed receipt identity")
            if output.request_id != request.request_id or output.as_of != request.evidence_cutoff:
                raise ValueError("recommendation receipt belongs to another request or as-of")
            replay = FullResearchRecommendationService(
                self.verifier.store, objects=self.verifier.objects
            ).verify_receipt(output)
            if replay["status"] != "PASS" or not output.publication.formal_recommendation_allowed:
                raise ValueError("full-research recommendation has not passed its publication gate")
            for source in output.source_manifest:
                if source.artifact_id is None:
                    continue
                self.verifier._verify_reference(source.artifact_id, source.object_hash)
            for source_id, digest in output.input_artifact_hashes.items():
                self.verifier._verify_reference(source_id, digest)
        elif isinstance(output, PortfolioAnalysisReport):
            if output.status.value not in {"READY", "EMPTY"}:
                raise ValueError("portfolio analysis has missing inputs")
            if output.status.value == "READY" and (output.metrics is None or not output.assets):
                raise ValueError("portfolio READY result lacks assets or computed metrics")
            if output.status.value == "EMPTY" and output.assets:
                raise ValueError("empty portfolio result cannot hide populated assets")
        elif isinstance(output, HoldingReviewPack):
            if output.hard_blocks or output.degradation_codes or output.unresolved_conflicts:
                raise ValueError("holding review has unresolved evidence or rule conflicts")
            from astock.research.lifecycle_repository import LifecycleRepository

            repository = LifecycleRepository(self.verifier.state, self.verifier.objects)
            if output.plan_id is None:
                raise ValueError("holding review is missing its canonical monitoring plan identity")
            plan = repository.get_plan(output.plan_id)
            if plan is None or plan.position_id != output.position_id:
                raise ValueError("holding review has no matching canonical monitoring plan")
            self._require_company(plan.company_id, request)
            self._evidence(output.evidence_ids, request.evidence_cutoff)
        elif isinstance(output, ETFResearchMetrics):
            if output.observation_count <= 0 or not output.source_artifact_ids:
                raise ValueError("ETF metrics have no observed source data")
            # Metrics are research evidence, not permission to recommend or trade.
            if output.recommendation_allowed or output.portfolio_weight_allowed:
                raise ValueError("ETF metrics cannot self-authorize recommendation or weights")
        if payload.get("paper_ledger_write_allowed", False):
            raise ValueError("research artifacts cannot authorize paper-ledger writes")

    def _require_company(self, company_id: str | None, request: InvestorRequestEnvelope) -> None:
        if request.entity_ids and (
            company_id is None
            or not any(
                self.verifier._same_identity(company_id, item) for item in request.entity_ids
            )
        ):
            raise ValueError("domain research belongs to another security")

    def _evidence(self, evidence_ids: list[str], cutoff: datetime) -> None:
        if not evidence_ids:
            raise ValueError("domain research requires verifiable evidence")
        repository = EvidenceRepository(self.verifier.state)
        for evidence_id in evidence_ids:
            evidence = repository.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("domain research references unavailable Evidence")
            if evidence.available_to_system_at > cutoff:
                raise ValueError("domain Evidence was unavailable at the frozen decision time")
            if evidence.fact_status.value in {"CONFLICTED", "UNVERIFIED"}:
                raise ValueError(
                    "unverified or conflicted Evidence cannot certify domain completion"
                )
            self.verifier.objects.get_bytes(evidence.excerpt_object_sha256)

    def _role(
        self,
        node: CapabilityNode,
        artifact_id: str,
        output: ResearchRoleOutput,
        request: InvestorRequestEnvelope,
    ) -> None:
        contract = _ROLE_CONTRACTS.get(node.capability_id)
        if contract is None:
            raise ValueError("research role container is not allowed for this capability")
        plan = self.verifier.load(f"ResearchTeamPlan:{output.plan_id}", ResearchTeamPlan)
        assert isinstance(plan, ResearchTeamPlan)
        self.verifier._check_time(plan.model_dump(mode="json"), request.evidence_cutoff)
        self._require_company(plan.company_id, request)
        task = next((item for item in plan.tasks if item.task_id == output.task_id), None)
        if (
            task is None
            or task.role.value != contract[0]
            or task.output_contract != output.output_contract
        ):
            raise ValueError("research output role/contract does not match the required capability")
        if contract[1] not in task.readiness_checks:
            raise ValueError("planned role omitted its mandatory domain check")
        if set(output.readiness_check_results) != set(task.readiness_checks) or not all(
            output.readiness_check_results.values()
        ):
            raise ValueError("research role has incomplete domain readiness checks")
        completed = self._completed_task(plan, task.task_id, request.evidence_cutoff, set())
        if artifact_id not in completed.output_artifact_ids:
            raise ValueError("research role output is not the canonical completed task output")
        self._evidence(output.evidence_ids, request.evidence_cutoff)
        if node.capability_id == "RED_TEAM":
            bull = self._completed_task(plan, "bull-case", request.evidence_cutoff, set())
            bear = self._completed_task(plan, "bear-case", request.evidence_cutoff, set())
            if bull.independent_context_id == bear.independent_context_id:
                raise ValueError("red-team review requires independent bull and bear contexts")

    def _completed_task(
        self,
        plan: ResearchTeamPlan,
        task_id: str,
        cutoff: datetime,
        visiting: set[str],
    ) -> ResearchRoleResult:
        if task_id in visiting or len(visiting) > 100:
            raise ValueError("research dependency graph is cyclic or exceeds its bounded depth")
        cached = self._task_results.get((plan.plan_id, task_id))
        if cached is not None:
            return cached
        task = next((item for item in plan.tasks if item.task_id == task_id), None)
        if task is None:
            raise ValueError("research dependency references an unknown task")
        checkpoint_key = f"{plan.plan_id}:{task_id}"
        checkpoint = self.verifier.state.get_checkpoint("research-team-task", checkpoint_key)
        self._checkpoint_witnesses[checkpoint_key] = checkpoint
        if checkpoint is None or checkpoint["status"] != "COMPLETE":
            raise ValueError("research task dependency is not complete")
        artifact_id = checkpoint["cursor"].get("artifact_id")
        if not artifact_id:
            raise ValueError("research checkpoint has no registered result identity")
        result = self.verifier.load(str(artifact_id), ResearchRoleResult)
        assert isinstance(result, ResearchRoleResult)
        record = self.verifier.state.artifact_record(str(artifact_id))
        if record is None or record["object_hash"] != checkpoint["object_hash"]:
            raise ValueError("research checkpoint and registered result hashes differ")
        if (
            result.plan_id != plan.plan_id
            or result.task_id != task_id
            or result.state.value != "COMPLETE"
        ):
            raise ValueError("research checkpoint points to a different task or result state")
        self.verifier._check_time(result.model_dump(mode="json"), cutoff)
        evidence_ids: set[str] = set()
        for output_id in result.output_artifact_ids:
            output = self.verifier.load(output_id, ResearchRoleOutput)
            assert isinstance(output, ResearchRoleOutput)
            if (
                output.plan_id != plan.plan_id
                or output.task_id != task_id
                or output.output_contract != task.output_contract
                or not output.member_artifact_ids
            ):
                raise ValueError("research role member lineage differs from the canonical plan")
            self.verifier._check_time(output.model_dump(mode="json"), cutoff)
            output_record = self.verifier.state.artifact_record(output_id)
            assert output_record is not None
            if set(output.readiness_check_results) != set(task.readiness_checks) or not all(
                output.readiness_check_results.values()
            ):
                raise ValueError("research dependency has missing or failed readiness checks")
            if output_record["object_hash"] not in record["input_hashes"]:
                raise ValueError("research result is not bound to its output hashes")
            for member_id in output.member_artifact_ids:
                from astock.investor_orchestration.output_validation import output_model

                member = self.verifier.state.artifact_record(member_id)
                if member is None or member["object_hash"] not in output_record["input_hashes"]:
                    raise ValueError(
                        "research role member is not bound to its registered input hashes"
                    )
                model = self.verifier.load(member_id, output_model(str(member["type"])))
                member_payload = model.model_dump(mode="json")
                self.verifier._check_time(member_payload, cutoff)
                self.verifier._verify_linked_hashes(member_payload)
                if member_payload.get("status") in {
                    "FAILED",
                    "BLOCKED",
                    "NEEDS_INFO",
                    "DEGRADED",
                    "INCOMPLETE",
                }:
                    raise ValueError("research role relies on an incomplete member artifact")
                if isinstance(model, FinancialIntegrityEvidencePack) and (
                    model.status.value != "SUCCEEDED" or model.coverage_status.value != "COMPLETE"
                ):
                    raise ValueError("research role relies on incomplete financial coverage")
            evidence_ids.update(output.evidence_ids)
        if evidence_ids != set(result.evidence_ids):
            raise ValueError("research role result Evidence differs from its outputs")
        for dependency in task.dependencies:
            self._completed_task(plan, dependency, cutoff, visiting | {task_id})
            witness = self._checkpoint_witnesses[f"{plan.plan_id}:{dependency}"]
            if (
                not isinstance(witness, dict)
                or witness.get("object_hash") not in record["input_hashes"]
            ):
                raise ValueError("research result is not bound to its current dependency hashes")
        self._task_results[(plan.plan_id, task_id)] = result
        return result

    def _readiness(
        self,
        output: FullResearchInputReadinessReport,
        request: InvestorRequestEnvelope,
    ) -> None:
        from astock.research.team import ResearchTeamService

        service = ResearchTeamService(
            project_root=Path(__file__).resolve().parents[3],
            state=self.verifier.state,
            objects=self.verifier.objects,
        )
        plan = service.get_plan(output.plan_id)
        if plan is None or plan.scope.value != "FULL_MARKET":
            raise ValueError("full-market capability requires a canonical FULL_MARKET plan")
        required = set(service._required_checks(plan))
        if (
            output.status.value != "READY"
            or not output.full_research_input_ready
            or set(output.required_checks) != required
            or set(output.passed_checks) != required
            or output.missing_or_failed_checks
        ):
            raise ValueError("full-market research has not passed every upstream readiness gate")
        derived = service._derived_readiness_checks(plan)
        if not service._team_dag_complete(plan) or not all(
            derived.get(key) is True for key in required - {"TEAM_DAG_COMPLETE"}
        ):
            raise ValueError(
                "readiness report does not match the current canonical research results"
            )
        self.verifier._check_time(plan.model_dump(mode="json"), request.evidence_cutoff)
        for task in plan.tasks:
            if task.required_for_recommendation:
                self._completed_task(plan, task.task_id, request.evidence_cutoff, set())
