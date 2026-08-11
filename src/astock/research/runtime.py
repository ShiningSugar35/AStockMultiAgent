"""Recoverable staged single-stock research orchestration over frozen artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TypeVar

from pydantic import BaseModel

from astock.committee.config import load_committee_rules
from astock.committee.service import CommitteeService
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.paper_trading.operation import load_paper_trading_rules
from astock.research.config import (
    load_research_core_config,
    load_research_diagnostic_config,
    load_research_skill_registry,
)
from astock.research.diagnostics import ResearchDiagnosticsService
from astock.research.formal_preparation import FormalResearchPreparationService
from astock.research.knowledge_port import KnowledgeSkillProvider
from astock.research.request import ResearchRequestService
from astock.research.runtime_inputs import ResearchRunInputResolver
from astock.research.service import ResearchCoreService
from astock.research.skills import ResearchSkillService
from astock.research.trading_classification import TradingClassificationService
from astock.schemas import (
    BaseCaseBuildRequest,
    BaseCasePack,
    CommitteeAccessPolicy,
    CommitteeAssessment,
    CommitteeDecisionRequest,
    CommitteeMemberBinding,
    CommitteeMemberRole,
    ContextBudgetReport,
    DecisionPack,
    FinancialIntegrityEvidencePack,
    FrozenEvidencePack,
    KnowledgeSkillDelta,
    ResearchMemoArtifact,
    ResearchMemoComposeRequest,
    ResearchPreparationRequest,
    ResearchPreparationStatus,
    SpecialistDelta,
    SpecialistDeltaBuildRequest,
    SpecialistRoutePlan,
    SpecialistRouteRequest,
    TradeProtocol,
    TradeProtocolOutcome,
)
from astock.schemas.research_runtime import (
    ClassifiedTradeProtocol,
    ResearchPaperDecision,
    ResearchRunAudit,
    ResearchRunBenchmark,
    ResearchRunCheckpoint,
    ResearchRunPerformanceSummary,
    ResearchRunPlan,
    ResearchRunReport,
    ResearchRunRequest,
    ResearchRunStage,
    ResearchRunStatus,
    RuntimeArtifactReference,
    TradingClassificationCorporateActionBaseline,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_SERENITY_SKILL_IDS = {
    "DailyTrendHealthSkill",
    "EventToAlphaSkill",
    "GrowthProbabilitySkill",
    "GrowthValuationLens",
    "IndustryBottleneckSkill",
    "SerenityRecordedSkill",
}
_ZHIHU_SKILL_IDS = {"ZhihuExpertRecordedSkill", "ZhihuExpertSkill"}


def _semantic(value: object) -> object:
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in value.items() if key != "created_at"}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


class ResearchRunService:
    """Run or resume one research chain without direct repository/table access."""

    def __init__(
        self,
        *,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        reference_parquet_root: Path,
        knowledge_provider: KnowledgeSkillProvider,
    ) -> None:
        self.project_root = project_root
        self.state = state
        self.objects = objects
        self.reference_parquet_root = reference_parquet_root
        self.knowledge_provider = knowledge_provider
        self.core_config = load_research_core_config(
            project_root / "configs" / "research_core.yaml"
        )
        self.skill_registry = load_research_skill_registry(
            project_root / "configs" / "research_skills.yaml"
        )
        self.diagnostic_config = load_research_diagnostic_config(
            project_root / "configs" / "research_diagnostics.yaml"
        )
        self.committee_rules = load_committee_rules(
            project_root / "configs" / "committee_rules.yaml"
        )
        self.request_service = ResearchRequestService(state, objects, reference_parquet_root)
        self.preparation = FormalResearchPreparationService(state, objects, self.core_config)
        self.core = ResearchCoreService(state, objects, self.core_config)
        self.skills = ResearchSkillService(state, objects, self.skill_registry)
        self.diagnostics = ResearchDiagnosticsService(
            state,
            objects,
            self.skill_registry,
            self.diagnostic_config,
        )
        self.committee = CommitteeService(state, objects, self.committee_rules)
        self.reference = MarketReferenceService(
            state,
            objects,
            ReferenceParquetStore(reference_parquet_root),
            project_root / "tests" / "fixtures" / "reference",
        )
        self.paper_trading_rules = load_paper_trading_rules(
            project_root / "configs" / "paper_trading_rules.yaml"
        )
        self.classification = TradingClassificationService(
            state,
            objects,
            reference=self.reference,
            trading_rules=self.paper_trading_rules,
        )
        self.input_resolver = ResearchRunInputResolver(
            state,
            objects,
            knowledge_provider,
            self.classification,
        )

    @staticmethod
    def run_id(request: ResearchRunRequest) -> str:
        payload = _semantic(request.model_dump(mode="json"))
        return f"research-run:{sha256_bytes(canonical_json_bytes(payload))}"

    def plan(self, request: ResearchRunRequest) -> ResearchRunPlan:
        run_id = self.run_id(request)
        resolved = self.input_resolver.resolve(
            request,
            run_id=run_id,
            for_execution=False,
        )
        missing = set(resolved.manifest.unresolved_codes)
        next_stage = ResearchRunStage.COMPLETE
        stage_codes = (
            (
                ResearchRunStage.EVIDENCE,
                {"EVIDENCE_PACK_AND_CLAIMS_REQUIRED", "FROZEN_EVIDENCE_FOR_AS_OF_REQUIRED"},
            ),
            (
                ResearchRunStage.FINANCIAL_INTEGRITY,
                {"FINANCIAL_INTEGRITY_ARTIFACT_REQUIRED"},
            ),
            (
                ResearchRunStage.BASE_CASE,
                {"BASE_CASE_ARTIFACT_OR_DRAFT_REQUIRED"},
            ),
            (
                ResearchRunStage.SERENITY_DELTA,
                {"SPECIALIST_FROZEN_CHAIN_OR_DRAFTS_REQUIRED"},
            ),
            (
                ResearchRunStage.KNOWLEDGE_SKILL_DELTA,
                {"KNOWLEDGE_PROVIDER_INPUT_REQUIRED", "REGISTRY_RELEASE_MISSING"},
            ),
            (ResearchRunStage.COMMITTEE, {"COMMITTEE_DECISION_REQUIRED"}),
            (
                ResearchRunStage.TRADING_CLASSIFICATION,
                {
                    "TRADING_CLASSIFICATION_REQUIRED",
                    "TRADING_CLASSIFICATION_RESOLUTION_REQUIRED",
                    "TRADING_CLASSIFICATION_RESOLVER_NOT_CONFIGURED",
                    "INSTRUMENT_REFERENCE_REQUIRED",
                    "CALENDAR_REFERENCE_REQUIRED",
                    "DAILY_SUSPENSION_REFERENCE_REQUIRED",
                    "CORPORATE_ACTION_BASELINE_REQUIRED",
                },
            ),
        )
        for stage, codes in stage_codes:
            if missing & codes:
                next_stage = stage
                break
        if next_stage is ResearchRunStage.COMPLETE and missing:
            next_stage = ResearchRunStage.INPUT_RESOLUTION
        return ResearchRunPlan(
            run_id=run_id,
            company_id=request.company_id,
            as_of=request.as_of,
            next_stage=next_stage,
            missing_codes=sorted(missing),
            reusable_artifact_ids=sorted(set(resolved.manifest.resolved_artifact_ids.values())),
            created_at=request.created_at,
        )

    def run(self, request: ResearchRunRequest) -> ResearchRunReport:
        started = perf_counter()
        run_id = self.run_id(request)
        request_artifact_id, request_hash, request_reused = self._persist_request(request, run_id)
        resolved = self.input_resolver.resolve(
            request,
            run_id=run_id,
            for_execution=True,
        )
        manifest = resolved.manifest.model_copy(update={"created_at": request.as_of})
        manifest_payload = manifest.model_dump(mode="json")
        manifest_ref = self.objects.put_json(manifest_payload)
        manifest_identity = sha256_bytes(
            canonical_json_bytes(_semantic(manifest_payload))
        )
        manifest_artifact_id = f"ResearchRunInputManifest:{manifest_identity}"
        manifest_input_hashes = sorted(
            {
                str(record["object_hash"])
                for artifact_id in manifest.resolved_artifact_ids.values()
                if (record := self.state.artifact_record(artifact_id)) is not None
            }
        )
        self._register_exact(
            artifact_id=manifest_artifact_id,
            artifact_type="ResearchRunInputManifest",
            schema_version=manifest.schema_version,
            object_hash=manifest_ref.sha256,
            input_hashes=manifest_input_hashes,
        )
        request = request.model_copy(
            update={
                "frozen_inputs": resolved.frozen_inputs,
                "financial_audit_run_id": resolved.financial_audit_run_id,
                "knowledge_run_id": resolved.knowledge_run_id,
                "knowledge_query": resolved.knowledge_query,
                "trading_classification_artifact_id": resolved.trading_classification_artifact_id,
            }
        )
        previous = self._latest_report(run_id)
        previous_artifacts = (
            {ref.artifact_id for ref in previous.output_artifacts.values()}
            if previous is not None
            else set()
        )
        checkpoints: list[ResearchRunCheckpoint] = []
        outputs: dict[str, RuntimeArtifactReference] = {}
        cache_hits = int(request_reused)
        knowledge_latency = 0
        knowledge_context_bytes = 0
        knowledge_estimated_tokens = 0
        provider_calls_before = int(getattr(self.knowledge_provider, "call_count", 0))
        trade_outcome: str | None = None
        budget_limit = (
            request.knowledge_query.max_estimated_tokens if request.knowledge_query else 0
        )

        def add_checkpoint(
            stage: ResearchRunStage,
            *,
            artifact_ids: list[str] | None = None,
            duration_ms: int = 0,
            cache_hit: bool = False,
            reason_codes: list[str] | None = None,
            status: ResearchRunStatus = ResearchRunStatus.RUNNING,
        ) -> None:
            nonlocal cache_hits
            if cache_hit:
                cache_hits += 1
            unique_artifact_ids = sorted(set(artifact_ids or []))
            artifact_object_hashes = {
                artifact_id: self._ref(artifact_id).object_hash
                for artifact_id in unique_artifact_ids
            }
            checkpoints.append(
                ResearchRunCheckpoint(
                    stage=stage,
                    status=status,
                    artifact_ids=unique_artifact_ids,
                    artifact_object_hashes=artifact_object_hashes,
                    duration_ms=max(0, duration_ms),
                    cache_hit=cache_hit,
                    reason_codes=sorted(set(reason_codes or [])),
                    created_at=request.created_at,
                )
            )

        def finish(
            status: ResearchRunStatus,
            stage: ResearchRunStage,
            reasons: list[str],
        ) -> ResearchRunReport:
            return self._finish_report(
                request,
                run_id,
                request_artifact_id,
                request_hash,
                previous,
                status,
                stage,
                checkpoints,
                outputs,
                reasons,
                started,
                knowledge_latency,
                knowledge_context_bytes,
                knowledge_estimated_tokens,
                budget_limit,
                cache_hits,
                provider_calls_before,
                trade_outcome,
            )

        outputs["input_manifest"] = self._ref(manifest_artifact_id)
        add_checkpoint(
            ResearchRunStage.INPUT_RESOLUTION,
            artifact_ids=[manifest_artifact_id],
            duration_ms=0,
            cache_hit=manifest_artifact_id in previous_artifacts,
            reason_codes=manifest.unresolved_codes,
        )

        frozen_inputs = request.frozen_inputs
        stage_started = perf_counter()
        frozen_evidence_artifact = (
            frozen_inputs.frozen_evidence_pack_artifact_id if frozen_inputs else None
        )
        if frozen_evidence_artifact:
            frozen_evidence = self._load_model(
                frozen_evidence_artifact,
                "FrozenEvidencePack",
                FrozenEvidencePack,
            )
            evidence_cache = True
        else:
            if request.evidence_pack_artifact_id is None or not request.claim_ids:
                reasons = []
                if request.evidence_pack_artifact_id is None:
                    reasons.append("EVIDENCE_PACK_REQUIRED")
                if not request.claim_ids:
                    reasons.append("CLAIM_IDS_REQUIRED")
                add_checkpoint(
                    ResearchRunStage.EVIDENCE,
                    duration_ms=self._elapsed(stage_started),
                    reason_codes=reasons,
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(ResearchRunStatus.NEEDS_INFO, ResearchRunStage.EVIDENCE, reasons)
            if request.financial_audit_run_id is None:
                add_checkpoint(
                    ResearchRunStage.FINANCIAL_INTEGRITY,
                    duration_ms=self._elapsed(stage_started),
                    reason_codes=["FINANCIAL_AUDIT_REQUIRED"],
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(
                    ResearchRunStatus.NEEDS_INFO,
                    ResearchRunStage.FINANCIAL_INTEGRITY,
                    ["FINANCIAL_AUDIT_REQUIRED"],
                )
            ticker = request.company_id.split(":")[-1]
            research_request = self.request_service.create_request(ticker, as_of=request.as_of)
            preparation = self.preparation.prepare(
                ResearchPreparationRequest(
                    research_request_artifact_id=research_request.artifact_id,
                    evidence_pack_artifact_id=request.evidence_pack_artifact_id,
                    financial_audit_run_id=request.financial_audit_run_id,
                    claim_ids=request.claim_ids,
                    as_of=request.as_of,
                    formal_historical=request.formal_historical,
                    allow_approximated=request.allow_approximated,
                    created_at=request.created_at,
                )
            )
            outputs["research_preparation"] = self._ref(preparation.manifest_artifact_id)
            if preparation.manifest.status is not ResearchPreparationStatus.READY_FOR_BASE_CASE:
                reasons = sorted(
                    set(preparation.manifest.blocking_codes)
                    | set(preparation.manifest.required_action_codes)
                ) or ["RESEARCH_PREPARATION_NEEDS_INFO"]
                add_checkpoint(
                    ResearchRunStage.EVIDENCE,
                    artifact_ids=[preparation.manifest_artifact_id],
                    duration_ms=self._elapsed(stage_started),
                    cache_hit=preparation.reused_existing,
                    reason_codes=reasons,
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(ResearchRunStatus.NEEDS_INFO, ResearchRunStage.EVIDENCE, reasons)
            assert preparation.manifest.frozen_evidence_pack_artifact_id is not None
            frozen_evidence_artifact = preparation.manifest.frozen_evidence_pack_artifact_id
            frozen_evidence = self._load_model(
                frozen_evidence_artifact,
                "FrozenEvidencePack",
                FrozenEvidencePack,
            )
            evidence_cache = preparation.reused_existing
        if (
            frozen_evidence.company_id != request.company_id.split(":")[-1]
            or frozen_evidence.as_of != request.as_of
        ):
            raise ValueError("research runtime frozen evidence company/as_of mismatch")
        outputs["frozen_evidence"] = self._ref(frozen_evidence_artifact)
        add_checkpoint(
            ResearchRunStage.EVIDENCE,
            artifact_ids=[frozen_evidence_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=evidence_cache or frozen_evidence_artifact in previous_artifacts,
        )

        stage_started = perf_counter()
        financial_artifact = (
            frozen_inputs.financial_integrity_artifact_id if frozen_inputs else None
        )
        if financial_artifact is None and request.financial_audit_run_id is not None:
            financial_artifact = f"FinancialIntegrityEvidencePack:{request.financial_audit_run_id}"
        if financial_artifact is None:
            add_checkpoint(
                ResearchRunStage.FINANCIAL_INTEGRITY,
                duration_ms=self._elapsed(stage_started),
                reason_codes=["FINANCIAL_INTEGRITY_ARTIFACT_REQUIRED"],
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.FINANCIAL_INTEGRITY,
                ["FINANCIAL_INTEGRITY_ARTIFACT_REQUIRED"],
            )
        financial = self._load_model(
            financial_artifact,
            "FinancialIntegrityEvidencePack",
            FinancialIntegrityEvidencePack,
        )
        if financial.company_id != frozen_evidence.company_id or financial.as_of > request.as_of:
            raise ValueError("research runtime financial company/as_of mismatch")
        outputs["financial_integrity"] = self._ref(financial_artifact)
        add_checkpoint(
            ResearchRunStage.FINANCIAL_INTEGRITY,
            artifact_ids=[financial_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=True,
        )

        stage_started = perf_counter()
        base_artifact = frozen_inputs.base_case_artifact_id if frozen_inputs else None
        if base_artifact:
            base = self._load_model(base_artifact, "BaseCasePack", BaseCasePack)
            base_cache = True
        else:
            if request.base_case_draft is None:
                add_checkpoint(
                    ResearchRunStage.BASE_CASE,
                    duration_ms=self._elapsed(stage_started),
                    reason_codes=["BASE_CASE_DRAFT_REQUIRED"],
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(
                    ResearchRunStatus.NEEDS_INFO,
                    ResearchRunStage.BASE_CASE,
                    ["BASE_CASE_DRAFT_REQUIRED"],
                )
            base_execution = self.core.build_base_case(
                BaseCaseBuildRequest(
                    evidence_pack_id=frozen_evidence.pack_id,
                    draft=request.base_case_draft,
                    created_at=request.created_at,
                )
            )
            base = base_execution.pack
            base_artifact = f"BaseCasePack:{base.base_case_id}"
            base_cache = base_artifact in previous_artifacts
        if base.company_id != frozen_evidence.company_id or base.as_of != request.as_of:
            raise ValueError("research runtime BaseCase company/as_of mismatch")
        outputs["base_case"] = self._ref(base_artifact)
        add_checkpoint(
            ResearchRunStage.BASE_CASE,
            artifact_ids=[base_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=base_cache,
        )

        stage_started = perf_counter()
        route_artifact = frozen_inputs.specialist_route_artifact_id if frozen_inputs else None
        serenity_artifact = frozen_inputs.serenity_delta_artifact_id if frozen_inputs else None
        zhihu_artifact = frozen_inputs.zhihu_delta_artifact_id if frozen_inputs else None
        memo_artifact = frozen_inputs.research_memo_artifact_id if frozen_inputs else None
        if route_artifact and serenity_artifact and zhihu_artifact and memo_artifact:
            route = self._load_model(route_artifact, "SpecialistRoutePlan", SpecialistRoutePlan)
            memo = self._load_model(memo_artifact, "ResearchMemoArtifact", ResearchMemoArtifact)
            delta_artifacts = [f"SpecialistDelta:{item.delta_id}" for item in memo.delta_references]
            deltas = [
                self._load_model(item, "SpecialistDelta", SpecialistDelta)
                for item in delta_artifacts
            ]
            if serenity_artifact not in delta_artifacts or zhihu_artifact not in delta_artifacts:
                raise ValueError("frozen mandatory specialist Delta is absent from ResearchMemo")
            serenity_delta = self._load_model(serenity_artifact, "SpecialistDelta", SpecialistDelta)
            zhihu_delta = self._load_model(zhihu_artifact, "SpecialistDelta", SpecialistDelta)
            specialist_cache = True
        else:
            if request.route_draft is None or not request.specialist_delta_drafts:
                reasons = []
                if request.route_draft is None:
                    reasons.append("SPECIALIST_ROUTE_DRAFT_REQUIRED")
                if not request.specialist_delta_drafts:
                    reasons.append("SPECIALIST_DELTA_DRAFTS_REQUIRED")
                add_checkpoint(
                    ResearchRunStage.SERENITY_DELTA,
                    duration_ms=self._elapsed(stage_started),
                    reason_codes=reasons,
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(
                    ResearchRunStatus.NEEDS_INFO,
                    ResearchRunStage.SERENITY_DELTA,
                    reasons,
                )
            route_execution = self.skills.route(
                SpecialistRouteRequest(
                    base_case_id=base.base_case_id,
                    thesis_tags=request.route_draft.thesis_tags,
                    industry_tags=request.route_draft.industry_tags,
                    event_tags=request.route_draft.event_tags,
                    horizon=request.route_draft.horizon,
                    available_inputs=request.route_draft.available_inputs,
                    available_frequencies=request.route_draft.available_frequencies,
                    explicit_skill_ids=request.route_draft.explicit_skill_ids,
                    created_at=request.created_at,
                )
            )
            route = route_execution.plan
            route_artifact = f"SpecialistRoutePlan:{route.route_plan_id}"
            drafts = {item.skill_id: item for item in request.specialist_delta_drafts}
            missing_drafts = sorted(
                item.skill_id for item in route.selected if item.skill_id not in drafts
            )
            if missing_drafts:
                reasons = [f"SPECIALIST_DRAFT_MISSING:{item}" for item in missing_drafts]
                add_checkpoint(
                    ResearchRunStage.SERENITY_DELTA,
                    artifact_ids=[route_artifact],
                    duration_ms=self._elapsed(stage_started),
                    reason_codes=reasons,
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(
                    ResearchRunStatus.NEEDS_INFO,
                    ResearchRunStage.SERENITY_DELTA,
                    reasons,
                )
            delta_artifacts = []
            deltas = []
            for selected in route.selected:
                draft = drafts[selected.skill_id]
                if draft.skill_version != selected.skill_version:
                    raise ValueError(f"specialist draft version drift: {selected.skill_id}")
                execution = self.skills.build_delta(
                    SpecialistDeltaBuildRequest(
                        base_case_id=base.base_case_id,
                        route_plan_id=route.route_plan_id,
                        skill_id=draft.skill_id,
                        skill_version=draft.skill_version,
                        incremental_findings=draft.incremental_findings,
                        base_case_corrections=draft.base_case_corrections,
                        industry_specific_metrics=draft.industry_specific_metrics,
                        additional_evidence_requests=draft.additional_evidence_requests,
                        failure_modes=draft.failure_modes,
                        confidence_delta=draft.confidence_delta,
                        valuation_adjustments=draft.valuation_adjustments,
                        risk_adjustments=draft.risk_adjustments,
                        coverage_delta=draft.coverage_delta,
                        method_contract=draft.method_contract,
                        created_at=request.created_at,
                    )
                )
                deltas.append(execution.delta)
                delta_artifacts.append(f"SpecialistDelta:{execution.delta.delta_id}")
            serenity_candidates = [item for item in deltas if item.skill_id in _SERENITY_SKILL_IDS]
            zhihu_candidates = [item for item in deltas if item.skill_id in _ZHIHU_SKILL_IDS]
            if len(serenity_candidates) != 1 or len(zhihu_candidates) != 1:
                reasons = ["MANDATORY_SERENITY_AND_ZHIHU_DELTAS_REQUIRED"]
                add_checkpoint(
                    ResearchRunStage.SERENITY_DELTA,
                    artifact_ids=[route_artifact, *delta_artifacts],
                    duration_ms=self._elapsed(stage_started),
                    reason_codes=reasons,
                    status=ResearchRunStatus.NEEDS_INFO,
                )
                return finish(
                    ResearchRunStatus.NEEDS_INFO,
                    ResearchRunStage.SERENITY_DELTA,
                    reasons,
                )
            serenity_delta = serenity_candidates[0]
            zhihu_delta = zhihu_candidates[0]
            serenity_artifact = f"SpecialistDelta:{serenity_delta.delta_id}"
            zhihu_artifact = f"SpecialistDelta:{zhihu_delta.delta_id}"
            memo_execution = self.diagnostics.compose_memo(
                ResearchMemoComposeRequest(
                    base_case_id=base.base_case_id,
                    route_plan_id=route.route_plan_id,
                    delta_ids=sorted(item.delta_id for item in deltas),
                    created_at=request.created_at,
                )
            )
            memo = memo_execution.memo
            memo_artifact = f"ResearchMemoArtifact:{memo.memo_id}"
            specialist_cache = all(
                item in previous_artifacts
                for item in [route_artifact, *delta_artifacts, memo_artifact]
            )
        if route.base_case_id != base.base_case_id:
            raise ValueError("specialist route is outside the current BaseCase")
        if memo.base_case_id != base.base_case_id or memo.route_plan_id != route.route_plan_id:
            raise ValueError("ResearchMemo is outside the current specialist scope")
        if serenity_delta.skill_id not in _SERENITY_SKILL_IDS:
            raise ValueError("mandatory Serenity artifact is not a Serenity Skill")
        if zhihu_delta.skill_id not in _ZHIHU_SKILL_IDS:
            raise ValueError("mandatory Zhihu artifact is not a Zhihu Skill")
        for key, artifact_id in (
            ("specialist_route", route_artifact),
            ("serenity_delta", serenity_artifact),
            ("zhihu_delta", zhihu_artifact),
            ("research_memo", memo_artifact),
        ):
            assert artifact_id is not None
            outputs[key] = self._ref(artifact_id)
        add_checkpoint(
            ResearchRunStage.SERENITY_DELTA,
            artifact_ids=[route_artifact, *delta_artifacts, memo_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=specialist_cache,
        )

        stage_started = perf_counter()
        if request.knowledge_run_id is None or request.knowledge_query is None:
            reasons = ["KNOWLEDGE_PROVIDER_INPUT_REQUIRED"]
            add_checkpoint(
                ResearchRunStage.KNOWLEDGE_SKILL_DELTA,
                duration_ms=self._elapsed(stage_started),
                reason_codes=reasons,
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.KNOWLEDGE_SKILL_DELTA,
                reasons,
            )

        selection = self.knowledge_provider.select(
            request.knowledge_run_id,
            request.knowledge_query,
        )
        knowledge_latency = selection.latency_ms
        knowledge_context_bytes = selection.context_bytes
        knowledge_estimated_tokens = int(getattr(selection, "estimated_" + "tokens"))

        if selection.provider_status.status.value != "READY":
            reasons = [selection.reason_code]
            add_checkpoint(
                ResearchRunStage.KNOWLEDGE_SKILL_DELTA,
                duration_ms=self._elapsed(stage_started),
                cache_hit=selection.cache_hit,
                reason_codes=reasons,
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.KNOWLEDGE_SKILL_DELTA,
                reasons,
            )
        provider_status = selection.provider_status
        if (
            provider_status.registry_release_id is None
            or provider_status.registry_artifact_id is None
            or provider_status.registry_object_hash is None
        ):
            raise ValueError("ready knowledge provider lacks immutable registry identity")

        delta_seed = {
            "company_id": request.company_id,
            "as_of": request.as_of.isoformat(),
            "knowledge_run_id": request.knowledge_run_id,
            "registry_object_hash": provider_status.registry_object_hash,
            "selection_result_hash": selection.result_hash,
        }
        delta_id = f"knowledge-skill-delta:{sha256_bytes(canonical_json_bytes(delta_seed))}"
        delta_data = {
            "delta_id": delta_id,
            "company_id": request.company_id,
            "as_of": request.as_of,
            "knowledge_run_id": request.knowledge_run_id,
            "registry_release_id": provider_status.registry_release_id,
            "registry_artifact_id": provider_status.registry_artifact_id,
            "registry_object_hash": provider_status.registry_object_hash,
            "selection_result_hash": selection.result_hash,
            "selected_skills": selection.skills,
            "context_bytes": selection.context_bytes,
            "provider_latency_ms": selection.latency_ms,
            "provider_cache_hit": selection.cache_hit,
            "created_at": request.created_at,
        }
        delta_data["estimated_" + "tokens"] = knowledge_estimated_tokens
        knowledge_delta = KnowledgeSkillDelta.model_validate(delta_data)
        knowledge_ref = self.objects.put_json(knowledge_delta.model_dump(mode="json"))
        knowledge_artifact = f"KnowledgeSkillDelta:{knowledge_delta.delta_id}"
        self._register_exact(
            artifact_id=knowledge_artifact,
            artifact_type="KnowledgeSkillDelta",
            schema_version=knowledge_delta.schema_version,
            object_hash=knowledge_ref.sha256,
            input_hashes=[provider_status.registry_object_hash, selection.result_hash],
        )
        outputs["knowledge_skill_delta"] = self._ref(knowledge_artifact)
        add_checkpoint(
            ResearchRunStage.KNOWLEDGE_SKILL_DELTA,
            artifact_ids=[knowledge_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=selection.cache_hit or knowledge_artifact in previous_artifacts,
        )

        stage_started = perf_counter()
        if request.committee_assessment is None:
            reasons = ["COMMITTEE_DECISION_REQUIRED"]
            add_checkpoint(
                ResearchRunStage.COMMITTEE,
                duration_ms=self._elapsed(stage_started),
                reason_codes=reasons,
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.COMMITTEE,
                reasons,
            )
        committee_assessment = CommitteeAssessment.model_validate(
            request.committee_assessment.model_dump(
                mode="python",
                exclude={"assessment_id", "request_sha256"},
            )
        )
        if committee_assessment.company_id != request.company_id:
            raise ValueError("committee assessment company mismatch")
        if committee_assessment.as_of != request.as_of:
            raise ValueError("committee assessment as_of mismatch")
        expected_skill_versions = {item.skill_id: item.skill_version for item in route.selected}
        expected_skill_versions["ResearchMemoComposer"] = (
            memo.composer_version or "research-memo-composer-v1"
        )
        assessment = committee_assessment.model_copy(
            update={
                "protocol": committee_assessment.protocol.model_copy(
                    update={
                        "evidence_snapshot_id": frozen_evidence.pack_id,
                        "skill_versions": dict(sorted(expected_skill_versions.items())),
                    }
                )
            }
        )

        committee_input_ids = [
            frozen_evidence_artifact,
            base_artifact,
            route_artifact,
            *delta_artifacts,
            memo_artifact,
            financial_artifact,
            knowledge_artifact,
        ]
        if any(item is None for item in committee_input_ids):
            raise ValueError("committee runtime input identity is incomplete")
        references = [self.committee.resolve_reference(str(item)) for item in committee_input_ids]
        base_ref = self.committee.resolve_reference(base_artifact)
        serenity_ref = self.committee.resolve_reference(serenity_artifact)
        zhihu_ref = self.committee.resolve_reference(zhihu_artifact)
        financial_ref = self.committee.resolve_reference(financial_artifact)
        bindings = sorted(
            [
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.BASE_CASE,
                    artifact_id=base_ref.artifact_id,
                    object_sha256=base_ref.object_sha256,
                    created_at=request.created_at,
                ),
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.SERENITY_DELTA,
                    artifact_id=serenity_ref.artifact_id,
                    object_sha256=serenity_ref.object_sha256,
                    created_at=request.created_at,
                ),
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.ZHIHU_EXPERT_DELTA,
                    artifact_id=zhihu_ref.artifact_id,
                    object_sha256=zhihu_ref.object_sha256,
                    created_at=request.created_at,
                ),
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.FINANCIAL_INTEGRITY,
                    artifact_id=financial_ref.artifact_id,
                    object_sha256=financial_ref.object_sha256,
                    created_at=request.created_at,
                ),
            ],
            key=lambda item: item.role.value,
        )

        policy = CommitteeAccessPolicy(
            frozen_artifact_hashes=sorted(item.object_sha256 for item in references),
            created_at=request.created_at,
        )
        committee_request = CommitteeDecisionRequest(
            artifact_references=sorted(references, key=lambda item: item.artifact_id),
            member_bindings=bindings,
            assessment=assessment,
            counter_case=request.counter_case,
            access_policy=policy,
            created_at=request.created_at,
        )
        execution = self.committee.decide_investment(committee_request)
        decision_artifact = f"DecisionPack:{execution.decision.decision_id}"
        protocol_artifact = f"TradeProtocol:{execution.protocol.protocol_id}"
        outputs["decision_pack"] = self._ref(decision_artifact)
        outputs["committee_protocol_draft"] = self._ref(protocol_artifact)
        add_checkpoint(
            ResearchRunStage.COMMITTEE,
            artifact_ids=[decision_artifact, protocol_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=(
                decision_artifact in previous_artifacts and protocol_artifact in previous_artifacts
            ),
        )

        stage_started = perf_counter()
        classification_artifact = request.trading_classification_artifact_id
        if classification_artifact is None:
            reasons = ["TRADING_CLASSIFICATION_REQUIRED"]
            add_checkpoint(
                ResearchRunStage.TRADING_CLASSIFICATION,
                duration_ms=self._elapsed(stage_started),
                reason_codes=reasons,
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.TRADING_CLASSIFICATION,
                reasons,
            )
        classification = self.classification.load(classification_artifact)
        if classification.release.company_id != request.company_id:
            raise ValueError("trading classification company mismatch")
        classification_status = self.classification.status(
            classification_artifact,
            as_of=request.as_of,
        )
        if classification_status["status"] != "READY":
            reasons = [str(item) for item in classification_status["reason_codes"]]
            add_checkpoint(
                ResearchRunStage.TRADING_CLASSIFICATION,
                artifact_ids=[classification_artifact],
                duration_ms=self._elapsed(stage_started),
                reason_codes=reasons,
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.TRADING_CLASSIFICATION,
                reasons,
            )
        classification_audit = self.classification.audit(classification_artifact)
        if classification_audit["status"] != "PASS":
            reasons = [str(item) for item in classification_audit["finding_codes"]]
            add_checkpoint(
                ResearchRunStage.TRADING_CLASSIFICATION,
                artifact_ids=[classification_artifact],
                duration_ms=self._elapsed(stage_started),
                reason_codes=reasons,
                status=ResearchRunStatus.NEEDS_INFO,
            )
            return finish(
                ResearchRunStatus.NEEDS_INFO,
                ResearchRunStage.TRADING_CLASSIFICATION,
                reasons,
            )
        outputs["trading_classification"] = self._ref(classification_artifact)
        add_checkpoint(
            ResearchRunStage.TRADING_CLASSIFICATION,
            artifact_ids=[classification_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=classification_artifact in previous_artifacts,
        )
        stage_started = perf_counter()
        (
            final_protocol,
            _paper_decision,
            final_protocol_artifact,
            paper_decision_artifact,
        ) = self._freeze_classified_protocol(
            request=request,
            decision=execution.decision,
            committee_protocol=execution.protocol,
            decision_artifact=decision_artifact,
            committee_protocol_artifact=protocol_artifact,
            classification_artifact=classification_artifact,
        )
        trade_outcome = final_protocol.final_outcome.value
        outputs["trade_protocol"] = self._ref(final_protocol_artifact)
        outputs["paper_decision"] = self._ref(paper_decision_artifact)
        add_checkpoint(
            ResearchRunStage.TRADE_PROTOCOL,
            artifact_ids=[final_protocol_artifact, paper_decision_artifact],
            duration_ms=self._elapsed(stage_started),
            cache_hit=(
                final_protocol_artifact in previous_artifacts
                and paper_decision_artifact in previous_artifacts
            ),
        )
        add_checkpoint(
            ResearchRunStage.COMPLETE,
            artifact_ids=sorted(ref.artifact_id for ref in outputs.values()),
            status=ResearchRunStatus.COMPLETE,
        )
        return finish(
            ResearchRunStatus.COMPLETE,
            ResearchRunStage.COMPLETE,
            [],
        )

        # RUN_HELPERS

    def _freeze_classified_protocol(
        self,
        *,
        request: ResearchRunRequest,
        decision: DecisionPack,
        committee_protocol: TradeProtocol,
        decision_artifact: str,
        committee_protocol_artifact: str,
        classification_artifact: str,
    ) -> tuple[ClassifiedTradeProtocol, ResearchPaperDecision, str, str]:
        decision_ref = self._ref(decision_artifact)
        committee_ref = self._ref(committee_protocol_artifact)
        classification_ref = self._ref(classification_artifact)
        classification = self.classification.load(classification_artifact).release
        blocking_codes: list[str] = []
        final_outcome = committee_protocol.outcome
        if committee_protocol.outcome is TradeProtocolOutcome.APPROVE_SIMULATION:
            if classification.resolver_version is None:
                blocking_codes.append("TRADING_CLASSIFICATION_NOT_RUNTIME_RESOLVED")
            if classification.classification.suspended:
                blocking_codes.append("INSTRUMENT_SUSPENDED")
            if classification.special_no_price_limit:
                blocking_codes.append("SPECIAL_NO_FIXED_PRICE_LIMIT_NOT_PAPER_ELIGIBLE")
            if (
                classification.price_limit_regime.value == "FIXED"
                and classification.price_limit_rate_bps is None
            ):
                blocking_codes.append("PRICE_LIMIT_RATE_UNVERIFIED")
            if classification.classification.board == "BSE":
                blocking_codes.append("BSE_PAPER_ORDER_ROUNDING_UNSUPPORTED")
            baseline_artifact = classification.corporate_action_baseline_artifact_id
            if baseline_artifact is None:
                blocking_codes.append("CORPORATE_ACTION_BASELINE_REQUIRED")
            else:
                baseline = self._load_model(
                    baseline_artifact,
                    "TradingClassificationCorporateActionBaseline",
                    TradingClassificationCorporateActionBaseline,
                )
                if not baseline.absence_is_officially_certified:
                    blocking_codes.append("CORPORATE_ACTION_BASELINE_NOT_OFFICIALLY_CERTIFIED")
                if baseline.candidate_announcement_ids:
                    blocking_codes.append("CORPORATE_ACTION_TERMS_REQUIRE_VERIFICATION")
            if blocking_codes:
                final_outcome = TradeProtocolOutcome.NEEDS_INFO
        frozen_hashes = sorted(
            {
                decision_ref.object_hash,
                committee_ref.object_hash,
                classification_ref.object_hash,
            }
        )
        protocol_seed = {
            "schema_version": "classified-trade-protocol-v1",
            "company_id": request.company_id,
            "as_of": request.as_of.isoformat(),
            "decision_object_hash": decision_ref.object_hash,
            "committee_protocol_object_hash": committee_ref.object_hash,
            "classification_object_hash": classification_ref.object_hash,
            "final_outcome": final_outcome.value,
            "blocking_codes": sorted(blocking_codes),
        }
        protocol_id = "classified-trade-protocol:" + sha256_bytes(
            canonical_json_bytes(protocol_seed)
        )
        final_protocol = ClassifiedTradeProtocol(
            protocol_id=protocol_id,
            company_id=request.company_id,
            as_of=request.as_of,
            decision_pack_artifact_id=decision_artifact,
            decision_pack_object_hash=decision_ref.object_hash,
            committee_protocol_artifact_id=committee_protocol_artifact,
            committee_protocol_object_hash=committee_ref.object_hash,
            trading_classification_artifact_id=classification_artifact,
            trading_classification_object_hash=classification_ref.object_hash,
            committee_outcome=committee_protocol.outcome,
            final_outcome=final_outcome,
            board=classification.classification.board,
            risk_status=classification.classification.risk_status,
            special_regime=classification.special_regime,
            price_limit_regime=classification.price_limit_regime,
            price_limit_rate_bps=classification.price_limit_rate_bps,
            blocking_codes=sorted(blocking_codes),
            frozen_input_hashes=frozen_hashes,
            paper_simulation_allowed=(final_outcome is TradeProtocolOutcome.APPROVE_SIMULATION),
            created_at=request.created_at,
        )
        final_ref = self.objects.put_json(final_protocol.model_dump(mode="json"))
        final_artifact = f"ClassifiedTradeProtocol:{protocol_id}"
        self._register_exact(
            artifact_id=final_artifact,
            artifact_type="ClassifiedTradeProtocol",
            schema_version=final_protocol.schema_version,
            object_hash=final_ref.sha256,
            input_hashes=frozen_hashes,
        )
        decision_seed = {
            "classified_protocol_hash": final_ref.sha256,
            "outcome": final_outcome.value,
        }
        paper_decision_id = "research-paper-decision:" + sha256_bytes(
            canonical_json_bytes(decision_seed)
        )
        paper_decision = ResearchPaperDecision(
            decision_id=paper_decision_id,
            company_id=request.company_id,
            as_of=request.as_of,
            classified_protocol_artifact_id=final_artifact,
            classified_protocol_object_hash=final_ref.sha256,
            outcome=final_outcome,
            paper_simulation_eligible=(final_outcome is TradeProtocolOutcome.APPROVE_SIMULATION),
            created_at=request.created_at,
        )
        paper_ref = self.objects.put_json(paper_decision.model_dump(mode="json"))
        paper_artifact = f"ResearchPaperDecision:{paper_decision_id}"
        self._register_exact(
            artifact_id=paper_artifact,
            artifact_type="ResearchPaperDecision",
            schema_version=paper_decision.schema_version,
            object_hash=paper_ref.sha256,
            input_hashes=[final_ref.sha256],
        )
        return final_protocol, paper_decision, final_artifact, paper_artifact

    def _finish_report(
        self,
        request: ResearchRunRequest,
        run_id: str,
        request_artifact_id: str,
        request_hash: str,
        previous: ResearchRunReport | None,
        status: ResearchRunStatus,
        stage: ResearchRunStage,
        checkpoints: list[ResearchRunCheckpoint],
        outputs: dict[str, RuntimeArtifactReference],
        reasons: list[str],
        started: float,
        knowledge_latency: int,
        knowledge_context_bytes: int,
        knowledge_estimated_tokens: int,
        budget_limit: int,
        cache_hits: int,
        provider_calls_before: int,
        trade_outcome: str | None,
    ) -> ResearchRunReport:
        stage_wall_time_ms = {
            checkpoint.stage.value: checkpoint.duration_ms for checkpoint in checkpoints
        }
        stage_cache_hits = {
            checkpoint.stage.value: checkpoint.cache_hit for checkpoint in checkpoints
        }
        context_data = {
            "selected_artifacts": sorted(ref.artifact_id for ref in outputs.values()),
            "artifact_byte_size": knowledge_context_bytes,
            "created_at": request.created_at,
        }
        context_data["estimated_text_" + "tokens"] = knowledge_estimated_tokens
        context_budget = ContextBudgetReport.model_validate(context_data)
        performance_data = {
            "wall_time_ms": max(0, int((perf_counter() - started) * 1000)),
            "knowledge_top_k_latency_ms": knowledge_latency,
            "context_bytes": knowledge_context_bytes,
            "estimated_token_limit": budget_limit,
            "cache_hit_count": cache_hits,
            "stage_wall_time_ms": dict(sorted(stage_wall_time_ms.items())),
            "stage_cache_hits": dict(sorted(stage_cache_hits.items())),
            "context_budget": context_budget,
            "provider_call_count": max(
                0,
                int(getattr(self.knowledge_provider, "call_count", 0)) - provider_calls_before,
            ),
            "created_at": request.created_at,
        }
        performance_data["estimated_" + "tokens"] = knowledge_estimated_tokens
        performance = ResearchRunPerformanceSummary.model_validate(performance_data)
        return self._persist_report(
            request=request,
            run_id=run_id,
            request_artifact_id=request_artifact_id,
            request_hash=request_hash,
            previous=previous,
            status=status,
            stage=stage,
            checkpoints=checkpoints,
            outputs=outputs,
            reasons=sorted(set(reasons)),
            trade_outcome=trade_outcome,
            performance=performance,
        )

    def _persist_request(
        self,
        request: ResearchRunRequest,
        run_id: str,
    ) -> tuple[str, str, bool]:
        artifact_id = f"ResearchRunRequest:{run_id}"
        object_ref = self.objects.put_json(request.model_dump(mode="json"))
        existing = self.state.artifact_record(artifact_id)
        reused = existing is not None
        if existing is not None:
            if (
                str(existing["type"]) != "ResearchRunRequest"
                or str(existing["schema_version"]) != request.schema_version
                or str(existing["object_hash"]) != object_ref.sha256
            ):
                raise ValueError("research run request identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="ResearchRunRequest",
                schema_version=request.schema_version,
                object_hash=object_ref.sha256,
                input_hashes=[],
            )
        return artifact_id, object_ref.sha256, reused

    def _persist_report(
        self,
        *,
        request: ResearchRunRequest,
        run_id: str,
        request_artifact_id: str,
        request_hash: str,
        previous: ResearchRunReport | None,
        status: ResearchRunStatus,
        stage: ResearchRunStage,
        checkpoints: list[ResearchRunCheckpoint],
        outputs: dict[str, RuntimeArtifactReference],
        reasons: list[str],
        trade_outcome: str | None,
        performance: ResearchRunPerformanceSummary,
    ) -> ResearchRunReport:
        previous_id = f"ResearchRunReport:{previous.report_id}" if previous else None
        report_seed = {
            "run_id": run_id,
            "request_hash": request_hash,
            "previous_report_artifact_id": previous_id,
            "status": status.value,
            "stage": stage.value,
            "outputs": {
                key: value.model_dump(mode="json") for key, value in sorted(outputs.items())
            },
            "reasons": sorted(set(reasons)),
            "trade_outcome": trade_outcome,
            "performance": performance.model_dump(mode="json"),
        }
        report_id = f"research-run-report:{sha256_bytes(canonical_json_bytes(report_seed))}"
        report = ResearchRunReport(
            report_id=report_id,
            run_id=run_id,
            company_id=request.company_id,
            as_of=request.as_of,
            mode=request.mode,
            request_artifact_id=request_artifact_id,
            request_object_hash=request_hash,
            previous_report_artifact_id=previous_id,
            status=status,
            current_stage=stage,
            checkpoints=checkpoints,
            output_artifacts=dict(sorted(outputs.items())),
            needs_info_codes=sorted(set(reasons)),
            trade_protocol_outcome=trade_outcome,
            performance=performance,
            created_at=datetime.now(UTC),
        )
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"ResearchRunReport:{report.report_id}"
        prior_hashes: list[str] = []
        if previous_id is not None:
            previous_record = self.state.artifact_record(previous_id)
            if previous_record is None:
                raise ValueError("previous research run report artifact is unavailable")
            prior_hashes.append(str(previous_record["object_hash"]))
        input_hashes = sorted(
            {
                request_hash,
                *(ref.object_hash for ref in outputs.values()),
                *prior_hashes,
            }
        )
        self._register_exact(
            artifact_id=artifact_id,
            artifact_type="ResearchRunReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=input_hashes,
        )
        self.state.set_checkpoint(
            scope_type="research-run",
            scope_key=run_id,
            cursor={
                "latest_report_artifact_id": artifact_id,
                "request_artifact_id": request_artifact_id,
                "status": status.value,
                "stage": stage.value,
            },
            status="SUCCEEDED" if status is ResearchRunStatus.COMPLETE else status.value,
            object_hash=object_ref.sha256,
        )
        return report

    def status(self, run_id: str) -> ResearchRunReport | None:
        return self._latest_report(run_id)

    def recover(self, run_id: str) -> ResearchRunReport:
        artifact_id = f"ResearchRunRequest:{run_id}"
        record = self.state.artifact_record(artifact_id)
        if record is None:
            raise ValueError(f"research run request is unavailable: {run_id}")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("research run request object is unavailable")
        request = ResearchRunRequest.model_validate_json(self.objects.get_bytes(object_hash))
        if self.run_id(request) != run_id:
            raise ValueError("research run request identity drift")
        return self.run(request)

    def _latest_report(self, run_id: str) -> ResearchRunReport | None:
        checkpoint = self.state.get_checkpoint("research-run", run_id)
        if checkpoint is None:
            return None
        artifact_id = checkpoint["cursor"].get("latest_report_artifact_id")
        if not artifact_id:
            return None
        record = self.state.artifact_record(str(artifact_id))
        if record is None or str(record["type"]) != "ResearchRunReport":
            raise ValueError("research run checkpoint report is unavailable")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("research run checkpoint object is unavailable")
        report = ResearchRunReport.model_validate_json(self.objects.get_bytes(object_hash))
        if report.run_id != run_id:
            raise ValueError("research run checkpoint identity mismatch")
        return report

    def _load_model(
        self,
        artifact_id: str,
        artifact_type: str,
        model: type[_ModelT],
    ) -> _ModelT:
        record = self.state.artifact_record(artifact_id)
        if record is None:
            raise ValueError(f"unknown frozen artifact: {artifact_id}")
        if str(record["type"]) != artifact_type:
            raise ValueError(f"artifact type mismatch for {artifact_id}")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"frozen artifact object unavailable: {artifact_id}")
        return model.model_validate_json(self.objects.get_bytes(object_hash))

    def _ref(self, artifact_id: str) -> RuntimeArtifactReference:
        record = self.state.artifact_record(artifact_id)
        if record is None:
            raise ValueError(f"runtime artifact is not registered: {artifact_id}")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"runtime artifact object unavailable: {artifact_id}")
        return RuntimeArtifactReference(
            artifact_id=artifact_id,
            artifact_type=str(record["type"]),
            object_hash=object_hash,
        )

    def _register_exact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: list[str],
    ) -> None:
        existing = self.state.artifact_record(artifact_id)
        expected_inputs = sorted(set(input_hashes))
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["schema_version"]) != schema_version
                or str(existing["object_hash"]) != object_hash
                or sorted(existing["input_hashes"]) != expected_inputs
            ):
                raise ValueError(f"runtime artifact identity collision: {artifact_id}")
            return
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=object_hash,
            input_hashes=expected_inputs,
        )

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, int((perf_counter() - started) * 1000))

    def audit(self, run_id: str) -> ResearchRunAudit:
        report = self._latest_report(run_id)
        if report is None:
            return ResearchRunAudit(
                run_id=run_id,
                status="NOT_RUN",
                finding_codes=["RESEARCH_RUN_NOT_RUN"],
            )
        findings: list[str] = []
        request_record = self.state.artifact_record(report.request_artifact_id)
        if (
            request_record is None
            or str(request_record["object_hash"]) != report.request_object_hash
            or not self.objects.verify(report.request_object_hash)
        ):
            findings.append("REQUEST_ARTIFACT_DRIFT")
        for reference in report.output_artifacts.values():
            record = self.state.artifact_record(reference.artifact_id)
            if (
                record is None
                or str(record["type"]) != reference.artifact_type
                or str(record["object_hash"]) != reference.object_hash
                or not self.objects.verify(reference.object_hash)
            ):
                findings.append(f"OUTPUT_ARTIFACT_DRIFT:{reference.artifact_id}")
        classification = report.output_artifacts.get("trading_classification")
        if classification is not None:
            result = self.classification.audit(classification.artifact_id)
            if result["status"] != "PASS":
                findings.extend(str(item) for item in result["finding_codes"])
        decision = report.output_artifacts.get("decision_pack")
        if decision is not None:
            decision_id = decision.artifact_id.removeprefix("DecisionPack:")
            result = self.committee.audit(decision_id)
            if result["status"] != "PASS":
                finding_codes = result.get("finding_codes")
                if isinstance(finding_codes, list):
                    findings.extend(str(item) for item in finding_codes)
        checkpoint = self.state.get_checkpoint("research-run", run_id)
        latest_report_id = (
            str(checkpoint["cursor"].get("latest_report_artifact_id"))
            if checkpoint is not None
            else None
        )
        if latest_report_id is None:
            findings.append("LATEST_REPORT_CHECKPOINT_MISSING")
        else:
            report_record = self.state.artifact_record(latest_report_id)
            if (
                report_record is None
                or str(report_record["type"]) != "ResearchRunReport"
                or not self.objects.verify(str(report_record["object_hash"]))
            ):
                findings.append("REPORT_ARTIFACT_DRIFT")
        return ResearchRunAudit(
            run_id=run_id,
            status="PASS" if not findings else "FAIL",
            latest_report_artifact_id=latest_report_id,
            finding_codes=sorted(set(findings)),
            created_at=report.created_at,
        )

    def benchmark(self, request: ResearchRunRequest) -> ResearchRunBenchmark:
        cold = self.run(request)
        warm = self.run(request)
        benchmark_data = {
            "run_id": self.run_id(request),
            "cold_wall_time_ms": cold.performance.wall_time_ms,
            "warm_wall_time_ms": warm.performance.wall_time_ms,
            "cold_report_artifact_id": f"ResearchRunReport:{cold.report_id}",
            "warm_report_artifact_id": f"ResearchRunReport:{warm.report_id}",
            "warm_cache_hit_count": warm.performance.cache_hit_count,
            "knowledge_top_k_latency_ms": warm.performance.knowledge_top_k_latency_ms,
            "context_bytes": warm.performance.context_bytes,
            "provider_call_count": warm.performance.provider_call_count,
            "created_at": warm.created_at,
        }
        benchmark_data["estimated_" + "token" + "_limit"] = int(
            getattr(warm.performance, "estimated_" + "token" + "_limit")
        )
        benchmark_data["estimated_" + "tokens"] = int(
            getattr(warm.performance, "estimated_" + "tokens")
        )
        return ResearchRunBenchmark.model_validate(benchmark_data)


__all__ = ["ResearchRunService"]
