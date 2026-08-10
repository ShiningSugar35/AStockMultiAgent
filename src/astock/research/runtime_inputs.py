"""Resolve reusable frozen inputs for the generic single-stock research runtime."""

from __future__ import annotations

from dataclasses import dataclass

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_integrity.repository import FinancialIntegrityRepository
from astock.research.knowledge_port import KnowledgeSkillProvider
from astock.research.repository import ResearchRepository
from astock.research.trading_classification import TradingClassificationService
from astock.schemas.knowledge_completion import KnowledgeSkillQuery
from astock.schemas.research_runtime import (
    ResearchRunFrozenInputs,
    ResearchRunInputManifest,
    ResearchRunRequest,
    TradingClassificationStatus,
)

_SERENITY_SKILL_IDS = {
    "DailyTrendHealthSkill",
    "EventToAlphaSkill",
    "GrowthProbabilitySkill",
    "GrowthValuationLens",
    "IndustryBottleneckSkill",
    "SerenityRecordedSkill",
}
_ZHIHU_SKILL_IDS = {"ZhihuExpertRecordedSkill", "ZhihuExpertSkill"}
_DEFAULT_KNOWLEDGE_QUERY = "基本面 行业 估值 风险 反证 持仓 退出"


@dataclass(frozen=True, slots=True)
class ResolvedResearchRunInputs:
    frozen_inputs: ResearchRunFrozenInputs
    financial_audit_run_id: str | None
    knowledge_run_id: str | None
    knowledge_query: KnowledgeSkillQuery | None
    trading_classification_artifact_id: str | None
    manifest: ResearchRunInputManifest


class ResearchRunInputResolver:
    """Discover only already-frozen research artifacts; never fabricate semantic conclusions."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        knowledge_provider: KnowledgeSkillProvider,
        classification: TradingClassificationService,
    ) -> None:
        self.state = state
        self.objects = objects
        self.research = ResearchRepository(state, objects)
        self.financial = FinancialIntegrityRepository(state, objects)
        self.knowledge_provider = knowledge_provider
        self.classification = classification

    def resolve(
        self,
        request: ResearchRunRequest,
        *,
        run_id: str,
        for_execution: bool,
    ) -> ResolvedResearchRunInputs:
        existing = request.frozen_inputs or ResearchRunFrozenInputs(created_at=request.created_at)
        bindings: dict[str, str] = {}
        missing: set[str] = set()

        frozen_evidence = existing.frozen_evidence_pack_artifact_id
        if frozen_evidence is None and request.auto_resolve_inputs:
            summary = self.research.latest_evidence_pack_summary(request.company_id)
            if summary is not None:
                candidate = self.research.get_evidence_pack(str(summary["pack_id"]))
                if candidate is not None and candidate.as_of == request.as_of:
                    frozen_evidence = f"FrozenEvidencePack:{candidate.pack_id}"
        if frozen_evidence is not None:
            self._bind_or_missing(
                bindings,
                "frozen_evidence",
                frozen_evidence,
                "FROZEN_EVIDENCE_FOR_AS_OF_REQUIRED",
                missing,
            )
        else:
            if request.evidence_pack_artifact_id is None:
                missing.add("EVIDENCE_PACK_REQUIRED")
            if not request.claim_ids:
                missing.add("CLAIM_IDS_REQUIRED")
            if request.evidence_pack_artifact_id is None or not request.claim_ids:
                missing.add("EVIDENCE_PACK_AND_CLAIMS_REQUIRED")

        financial_artifact = existing.financial_integrity_artifact_id
        financial_run_id = request.financial_audit_run_id
        if financial_artifact is None and financial_run_id is not None:
            financial_artifact = f"FinancialIntegrityEvidencePack:{financial_run_id}"
        if financial_artifact is None and request.auto_resolve_inputs:
            financial_run = self.financial.latest_succeeded_run(
                request.company_id,
                as_of=request.as_of,
            )
            if financial_run is not None:
                financial_run_id = financial_run.audit_run_id
                financial_artifact = f"FinancialIntegrityEvidencePack:{financial_run.audit_run_id}"
        self._bind_or_missing(
            bindings,
            "financial_integrity",
            financial_artifact,
            "FINANCIAL_INTEGRITY_ARTIFACT_REQUIRED",
            missing,
        )
        if financial_artifact is None:
            missing.add("FINANCIAL_AUDIT_REQUIRED")

        base_artifact = existing.base_case_artifact_id
        if base_artifact is None and request.auto_resolve_inputs and frozen_evidence is not None:
            base_summary = self.research.latest_base_case_summary(request.company_id)
            if base_summary is not None:
                base = self.research.get_base_case(str(base_summary["base_case_id"]))
                evidence_pack_id = frozen_evidence.removeprefix("FrozenEvidencePack:")
                if (
                    base is not None
                    and base.as_of == request.as_of
                    and base.evidence_pack_id == evidence_pack_id
                ):
                    base_artifact = f"BaseCasePack:{base.base_case_id}"
        if base_artifact is None and request.base_case_draft is None:
            missing.add("BASE_CASE_ARTIFACT_OR_DRAFT_REQUIRED")
            missing.add("BASE_CASE_DRAFT_REQUIRED")
        elif base_artifact is not None:
            self._bind_or_missing(
                bindings,
                "base_case",
                base_artifact,
                "BASE_CASE_ARTIFACT_OR_DRAFT_REQUIRED",
                missing,
            )

        route_artifact = existing.specialist_route_artifact_id
        serenity_artifact = existing.serenity_delta_artifact_id
        zhihu_artifact = existing.zhihu_delta_artifact_id
        memo_artifact = existing.research_memo_artifact_id
        if (
            request.auto_resolve_inputs
            and base_artifact is not None
            and not all((route_artifact, serenity_artifact, zhihu_artifact, memo_artifact))
        ):
            base_case_id = base_artifact.removeprefix("BaseCasePack:")
            route_summary = self.research.latest_route_plan_summary(base_case_id)
            memo_summary = self.research.latest_research_memo_summary(base_case_id)
            if route_summary is not None and memo_summary is not None:
                route = self.research.get_route_plan(str(route_summary["route_plan_id"]))
                memo = self.research.get_research_memo(str(memo_summary["memo_id"]))
                if (
                    route is not None
                    and memo is not None
                    and memo.route_plan_id == route.route_plan_id
                    and memo.as_of == request.as_of
                ):
                    route_artifact = f"SpecialistRoutePlan:{route.route_plan_id}"
                    memo_artifact = f"ResearchMemoArtifact:{memo.memo_id}"
                    deltas = [
                        self.research.get_specialist_delta(item.delta_id)
                        for item in memo.delta_references
                    ]
                    valid_deltas = [item for item in deltas if item is not None]
                    serenity = [
                        item for item in valid_deltas if item.skill_id in _SERENITY_SKILL_IDS
                    ]
                    zhihu = [item for item in valid_deltas if item.skill_id in _ZHIHU_SKILL_IDS]
                    if len(serenity) == 1:
                        serenity_artifact = f"SpecialistDelta:{serenity[0].delta_id}"
                    if len(zhihu) == 1:
                        zhihu_artifact = f"SpecialistDelta:{zhihu[0].delta_id}"
        specialist_complete = all(
            (route_artifact, serenity_artifact, zhihu_artifact, memo_artifact)
        )
        if not specialist_complete and (
            request.route_draft is None or not request.specialist_delta_drafts
        ):
            missing.add("SPECIALIST_FROZEN_CHAIN_OR_DRAFTS_REQUIRED")
        for key, artifact_id in (
            ("specialist_route", route_artifact),
            ("serenity_delta", serenity_artifact),
            ("zhihu_delta", zhihu_artifact),
            ("research_memo", memo_artifact),
        ):
            if artifact_id is not None:
                self._bind_or_missing(
                    bindings,
                    key,
                    artifact_id,
                    "SPECIALIST_FROZEN_CHAIN_OR_DRAFTS_REQUIRED",
                    missing,
                )

        knowledge_run_id = request.knowledge_run_id
        knowledge_query = request.knowledge_query
        if knowledge_run_id is None and request.auto_resolve_inputs:
            default_lookup = getattr(self.knowledge_provider, "default_run_id", None)
            default_candidate = default_lookup() if callable(default_lookup) else None
            default_run = default_candidate if isinstance(default_candidate, str) else None
            if default_run:
                knowledge_run_id = default_run
                knowledge_query = KnowledgeSkillQuery(
                    query=_DEFAULT_KNOWLEDGE_QUERY,
                    top_k=8,
                )
        if knowledge_run_id is None or knowledge_query is None:
            missing.add("KNOWLEDGE_PROVIDER_INPUT_REQUIRED")
        else:
            provider_status = self.knowledge_provider.status(knowledge_run_id)
            if provider_status.status.value != "READY":
                missing.add(provider_status.reason_code)
            elif provider_status.registry_artifact_id:
                bindings["knowledge_registry"] = provider_status.registry_artifact_id

        decision_artifact = existing.decision_pack_artifact_id
        committee_protocol_artifact = existing.committee_protocol_artifact_id
        # Do not reuse the latest company-level committee decision by date alone. The
        # current run will create a new KnowledgeSkillDelta, so an older committee
        # artifact is reusable only when the caller explicitly freezes that exact pair.
        if request.committee_assessment is None and not all(
            (decision_artifact, committee_protocol_artifact)
        ):
            missing.add("COMMITTEE_DECISION_REQUIRED")
            missing.add("COMMITTEE_ASSESSMENT_REQUIRED")
        for key, artifact_id in (
            ("decision_pack", decision_artifact),
            ("committee_protocol", committee_protocol_artifact),
        ):
            if artifact_id is not None:
                self._bind_or_missing(
                    bindings,
                    key,
                    artifact_id,
                    "COMMITTEE_DECISION_REQUIRED",
                    missing,
                )

        classification_artifact = request.trading_classification_artifact_id
        if classification_artifact is None and request.auto_resolve_inputs:
            can_execute_classification = for_execution and not missing
            resolution = (
                self.classification.resolve(
                    request.company_id,
                    request.as_of,
                    live=request.mode.value == "LIVE",
                    sync_reference_inputs=request.sync_reference_inputs,
                )
                if can_execute_classification
                else self.classification.plan_resolution(request.company_id, request.as_of)
            )
            if resolution.status is TradingClassificationStatus.READY:
                classification_artifact = resolution.artifact_id
            else:
                missing.update(resolution.reason_codes)
        if classification_artifact is None:
            missing.add("TRADING_CLASSIFICATION_REQUIRED")
        else:
            self._bind_or_missing(
                bindings,
                "trading_classification",
                classification_artifact,
                "TRADING_CLASSIFICATION_REQUIRED",
                missing,
            )

        frozen = ResearchRunFrozenInputs(
            frozen_evidence_pack_artifact_id=frozen_evidence,
            base_case_artifact_id=base_artifact,
            specialist_route_artifact_id=route_artifact,
            serenity_delta_artifact_id=serenity_artifact,
            zhihu_delta_artifact_id=zhihu_artifact,
            research_memo_artifact_id=memo_artifact,
            financial_integrity_artifact_id=financial_artifact,
            decision_pack_artifact_id=decision_artifact,
            committee_protocol_artifact_id=committee_protocol_artifact,
            created_at=request.created_at,
        )
        manifest = ResearchRunInputManifest(
            run_id=run_id,
            company_id=request.company_id,
            as_of=request.as_of,
            resolved_artifact_ids=dict(sorted(bindings.items())),
            unresolved_codes=sorted(missing),
            knowledge_run_id=knowledge_run_id,
            knowledge_query=knowledge_query,
            auto_resolution_enabled=request.auto_resolve_inputs,
            reference_sync_enabled=request.sync_reference_inputs,
            created_at=request.created_at,
        )
        return ResolvedResearchRunInputs(
            frozen_inputs=frozen,
            financial_audit_run_id=financial_run_id,
            knowledge_run_id=knowledge_run_id,
            knowledge_query=knowledge_query,
            trading_classification_artifact_id=classification_artifact,
            manifest=manifest,
        )

    def _bind_or_missing(
        self,
        bindings: dict[str, str],
        key: str,
        artifact_id: str | None,
        reason_code: str,
        missing: set[str],
    ) -> None:
        if artifact_id is None:
            missing.add(reason_code)
            return
        record = self.state.artifact_record(artifact_id)
        if record is None or not self.objects.verify(str(record["object_hash"])):
            missing.add(reason_code)
            return
        bindings[key] = artifact_id


__all__ = ["ResearchRunInputResolver", "ResolvedResearchRunInputs"]
