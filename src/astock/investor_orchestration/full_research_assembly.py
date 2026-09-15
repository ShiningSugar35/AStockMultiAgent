"""Canonical assembly of a Full Research receipt from one execution's verified artifacts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from astock.core.object_store import ObjectStore
from astock.core.project_root import resolve_project_root
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.investor_orchestration.capabilities import CapabilityDependencyContext
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash
from astock.research.lifecycle_repository import LifecycleRepository
from astock.schemas.committee import TradeProtocolOutcome
from astock.schemas.entry_quality import EntryQualitySnapshot, EntryQualityState
from astock.schemas.evidence import EvidenceGrade
from astock.schemas.financial import (
    FinancialDerivationType,
    FinancialFieldCode,
    FinancialIntegrityEvidencePack,
    FinancialPeriodType,
    FinancialRiskLevel,
    FinancialSeverity,
)
from astock.schemas.full_research import (
    CandidateDecisionNarrative,
    CandidateRankingEntry,
    ChallengerAssessment,
    CompanyFundamentalSnapshot,
    EvidenceConflict,
    FactorSnapshot,
    FinancialQualityAssessment,
    FullResearchNode,
    FullResearchNodeStatus,
    GovernanceAssessment,
    HoldingDecisionSnapshot,
    IndustryResearchOutcome,
    MacroResearchOutcome,
    NewsEvent,
    NewsEventCoverage,
    NewsEventResearchPack,
    PointInTimeSnapshot,
    QuantFactorResearchPack,
    RecommendationResearchReceipt,
    RecommendationValuationSnapshot,
    SourceAuthority,
    SourceFamily,
    SourceLineageEntry,
    ValuationScenario,
)
from astock.schemas.institutional_research import (
    IndustryProfile,
    InstitutionalArtifactStatus,
    InstitutionalDecisionContext,
    MarketPriceAnchor,
    ValuationPack,
)
from astock.schemas.knowledge import HoldingReviewPack
from astock.schemas.portfolio import PortfolioAnalysisReport, PortfolioAnalysisStatus
from astock.schemas.research_runtime import ClassifiedTradeProtocol
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedReport
from astock.schemas.research_team import (
    FullResearchInputReadinessReport,
    ResearchRoleOutput,
    ResearchRoleResult,
)

_FINANCIAL_CHECKS = (
    "audit_opinion",
    "non_standard_opinion",
    "revenue_cashflow_divergence",
    "receivables_anomaly",
    "inventory_anomaly",
    "gross_margin_anomaly",
    "capitalized_r_and_d",
    "goodwill_impairment",
    "asset_disposal",
    "government_subsidy",
    "related_party_transactions",
    "controlling_shareholder_fund_occupation",
    "share_pledge",
    "guarantees",
    "short_debt_long_investment",
    "cash_and_interest_bearing_debt",
    "non_recurring_items",
    "minority_interest",
    "cash_conversion_quality",
)

_GOVERNANCE_CHECKS = (
    "controlling_shareholder",
    "actual_controller",
    "management_stability",
    "regulatory_penalties",
    "formal_investigations",
    "director_executive_changes",
    "insider_reductions",
    "share_pledges",
    "related_party_transactions",
    "fund_occupation",
    "illegal_guarantees",
    "auditor_changes",
    "material_litigation",
)


class FullResearchReceiptAssembler:
    """Project one run's canonical domain outputs into the final immutable receipt."""

    def __init__(self, store: InvestorOrchestrationStore) -> None:
        self.store = store
        self.project_root = resolve_project_root(module_file=Path(__file__))
        self.state = StateStore(store.path)
        self.objects = ObjectStore(store.path.parent / "objects" / "sha256")
        self.verifier = RegisteredOutputVerifier(store, self.objects)
        self.research = FullResearchRecommendationService(store, objects=self.objects)
        self.evidence = EvidenceRepository(self.state)

    def assemble(
        self,
        request: InvestorRequestEnvelope,
        preflight: InvestorSessionPreflightReceipt,
        context: CapabilityDependencyContext,
    ) -> RecommendationResearchReceipt:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        contract = self.research.request_contract(request)
        if context.dependency_artifacts.get("HOLDING_REVIEW"):
            contract = contract.model_copy(update={"decision_context": "EXISTING_HOLDING"})

        market_anchors = self._load_many(context, "CURRENT_MARKET", MarketPriceAnchor)
        readiness = self._load_one(context, "FULL_MARKET", FullResearchInputReadinessReport)
        if not readiness.full_research_input_ready:
            raise ValueError("full research input readiness is not complete")
        industries = self._load_many(context, "INDUSTRY", IndustryProfile)
        company_contexts = self._load_many(
            context, "COMPANY_RESEARCH", InstitutionalDecisionContext
        )
        financial_packs = self._load_many(
            context, "FINANCIAL_INTEGRITY", FinancialIntegrityEvidencePack
        )
        governance_roles = self._load_many(context, "GOVERNANCE", ResearchRoleOutput)
        event_roles = self._load_many(context, "EVENT_RESEARCH", ResearchRoleOutput)
        valuation_packs = self._load_many(context, "FORECAST_VALUATION", ValuationPack)
        red_team_roles = self._load_many(context, "RED_TEAM", ResearchRoleOutput)
        committees = self._load_many(context, "COMMITTEE", ClassifiedTradeProtocol)
        portfolio_reports = self._load_many(context, "PORTFOLIO", PortfolioAnalysisReport)
        holding_review_packs, holding_reviews = self._holding_components(
            contract.decision_context,
            context,
            preflight,
        )

        regime, regime_available_at = self._regime(request.evidence_cutoff)
        seed_report = self._seed_report(readiness)
        seeds_by_company = {item.company_id: item for item in seed_report.seeds}

        macro_values = self._team_task_components(readiness, "macro-regime", MacroResearchOutcome)
        if len(macro_values) != 1:
            raise ValueError("full-market macro task must freeze exactly one MacroResearchOutcome")
        macro = macro_values[0]
        industry_outcomes = self._team_task_components(
            readiness, "sector-comparison", IndustryResearchOutcome
        )
        fundamental_values = self._team_task_components(
            readiness, "company-fundamental", CompanyFundamentalSnapshot
        )
        financial_quality_values = self._team_task_components(
            readiness, "company-financial-integrity", FinancialQualityAssessment
        )
        quant_packs = self._team_task_components(readiness, "quant-factor", QuantFactorResearchPack)
        factor_scores = tuple(factor for pack in quant_packs for factor in pack.factors)
        if len({item.instrument_id for item in factor_scores}) != len(factor_scores):
            raise ValueError("quant factor task produced duplicate instruments")

        governance_values = self._role_member_components(governance_roles, GovernanceAssessment)
        event_packs = self._role_member_components(event_roles, NewsEventResearchPack)
        challenger_values = self._role_member_components(red_team_roles, ChallengerAssessment)

        industry_outcome_by_id = {item.industry_id: item for item in industry_outcomes}
        fundamental_by_company = {item.instrument_id: item for item in fundamental_values}
        financial_by_company = {item.instrument_id: item for item in financial_quality_values}
        governance_by_company = {item.instrument_id: item for item in governance_values}
        factor_by_company = {item.instrument_id: item for item in factor_scores}
        event_by_company = {item.instrument_id: item for item in event_packs}
        challenger_by_company = {item.instrument_id: item for item in challenger_values}

        typed_evidence_ids = tuple(
            sorted(
                {
                    *macro.evidence_ids,
                    *(
                        evidence_id
                        for item in industry_outcomes
                        for evidence_id in item.evidence_ids
                    ),
                    *(
                        evidence_id
                        for item in financial_quality_values
                        for evidence_id in item.evidence_ids
                    ),
                    *(
                        evidence_id
                        for item in governance_values
                        for evidence_id in item.evidence_ids
                    ),
                    *(evidence_id for item in event_packs for evidence_id in item.evidence_ids),
                    *(evidence_id for pack in quant_packs for evidence_id in pack.evidence_ids),
                    *(
                        evidence_id
                        for review in holding_review_packs
                        for evidence_id in review.evidence_ids
                    ),
                }
            )
        )
        sources = self._source_manifest(
            context=context,
            market_anchors=market_anchors,
            financial_packs=financial_packs,
            company_contexts=company_contexts,
            governance_roles=governance_roles,
            event_roles=event_roles,
            regime=regime,
            regime_available_at=regime_available_at,
            extra_evidence_ids=typed_evidence_ids,
        )
        conflicts = self._conflicts(financial_packs, sources)
        pit = self.research.point_in_time_snapshot(request, sources, conflicts=conflicts)

        horizon_contexts: dict[str, tuple[Decimal, str, str]] = {}
        for item in company_contexts:
            if item.draft.decision_horizon_end <= request.evidence_cutoff.date():
                continue
            artifact_id = f"InstitutionalDecisionContext:{item.context_id}"
            record = self.state.artifact_record(artifact_id)
            if record is None or not self.objects.verify(str(record["object_hash"])):
                continue
            months = (
                Decimal((item.draft.decision_horizon_end - request.evidence_cutoff.date()).days)
                * Decimal("12")
                / Decimal("365")
            )
            horizon_contexts[item.company_id] = (
                months,
                artifact_id,
                str(record["object_hash"]),
            )
        valuations = tuple(
            self._valuation(item, return_horizon_context=horizon_contexts.get(item.company_id))
            for item in valuation_packs
            if item.market_price_anchor is not None
        )
        valuation_invalidations = {
            item.company_id: tuple(item.invalidation_conditions) for item in valuation_packs
        }
        valuations_by_company = {item.instrument_id: item for item in valuations}
        contexts_by_company = {item.company_id: item for item in company_contexts}
        committee_by_company = {item.company_id: item for item in committees}
        industry_by_company = {item.company_id: item for item in industries}

        candidate_rankings, narratives, challengers = self._candidate_outputs(
            valuations_by_company=valuations_by_company,
            contexts_by_company=contexts_by_company,
            financial_by_company=financial_by_company,
            governance_by_company=governance_by_company,
            committee_by_company=committee_by_company,
            industry_by_company=industry_by_company,
            industry_outcome_by_id=industry_outcome_by_id,
            fundamental_by_company=fundamental_by_company,
            factor_by_company=factor_by_company,
            event_by_company=event_by_company,
            challenger_by_company=challenger_by_company,
            seeds_by_company=seeds_by_company,
            valuation_invalidations=valuation_invalidations,
            regime=regime,
            pit=pit,
        )

        candidates = self.research.apply_candidate_vetoes(
            candidate_rankings,
            tuple(financial_by_company.values()),
            tuple(governance_by_company.values()),
        )
        rejected = {
            item.instrument_id: item.rejection_reasons for item in candidates if not item.eligible
        }
        portfolio = self.research.build_portfolio(contract, candidates, valuations)
        risk_audit = self._risk_audit(
            portfolio, candidates, seeds_by_company, portfolio_reports, context
        )
        quote_sources = {
            item.instrument_id: self._market_source_for(
                item.instrument_id, sources, valuation_packs
            )
            for item in portfolio.positions
        }
        narratives_by_company = {item.instrument_id: item for item in narratives}
        execution_plans = self.research.execution_plans(
            request.evidence_cutoff,
            portfolio,
            valuations,
            quote_sources,
            thesis_invalidations={
                key: value.thesis_invalidation_conditions
                for key, value in narratives_by_company.items()
            },
        )

        news_events, news_coverage, event_research_complete = self._news_from_packs(
            request.evidence_cutoff,
            event_packs,
            conflicts,
        )
        input_hashes = self._dependency_hashes(context)
        statuses: dict[FullResearchNode | str, FullResearchNodeStatus | str] = {
            node: FullResearchNodeStatus.PASS for node in FullResearchNode
        }
        reasons: dict[FullResearchNode | str, str] = {}
        if pit.status is not FullResearchNodeStatus.PASS:
            statuses[FullResearchNode.POINT_IN_TIME_SNAPSHOT] = pit.status
            reasons[FullResearchNode.POINT_IN_TIME_SNAPSHOT] = "PIT source audit did not pass"
        if risk_audit.status is not FullResearchNodeStatus.PASS:
            statuses[FullResearchNode.RISK_AUDIT] = risk_audit.status
            reasons[FullResearchNode.RISK_AUDIT] = "portfolio risk audit did not pass"
        self._apply_mandatory_coverage_statuses(
            statuses=statuses,
            reasons=reasons,
            macro=macro,
            industries=industry_outcomes,
            fundamentals=tuple(fundamental_by_company.values()),
            financial_quality=tuple(financial_by_company.values()),
            governance=tuple(governance_by_company.values()),
            candidates=tuple(candidates),
            news_coverage=news_coverage,
            event_research_complete=event_research_complete,
        )
        anchor_artifacts = tuple(input_hashes)
        if not anchor_artifacts:
            raise ValueError("Full Research gate has no registered upstream artifact lineage")
        fallback_artifact = anchor_artifacts[0]
        node_artifacts: dict[FullResearchNode | str, tuple[str, ...]] = {}
        for node in FullResearchNode:
            candidate_artifact = self._node_artifact(node, context)
            if candidate_artifact not in input_hashes:
                candidate_artifact = fallback_artifact
            node_artifacts[node] = (candidate_artifact,)
        dag = self.research.dag_receipt(
            request,
            statuses,
            artifact_ids=node_artifacts,
            reasons=reasons,
            at=request.evidence_cutoff,
        )
        publication = self.research.publication_decision(
            dag, pit, portfolio, risk_audit, execution_plans, candidates
        )
        receipt_payload = self._freeze_projection_created_at(
            {
                "request_id": request.request_id,
                "as_of": request.evidence_cutoff,
                "request_contract": contract,
                "holding_reviews": holding_reviews,
                "pit_snapshot": pit,
                "dag": dag,
                "source_manifest": sources,
                "skill_executions": {item.node.value: item.status for item in dag.executions},
                "candidate_universe": tuple(item.instrument_id for item in candidates),
                "candidate_rankings": candidates,
                "candidate_narratives": tuple(narratives),
                "rejected_candidates": rejected,
                "fundamentals": tuple(fundamental_by_company.values()),
                "financial_quality": tuple(financial_by_company.values()),
                "governance": tuple(governance_by_company.values()),
                "valuations": valuations,
                "news_events": news_events,
                "news_coverage": news_coverage,
                "challengers": tuple(challengers),
                "factor_scores": factor_scores,
                "macro": macro,
                "industries": industry_outcomes,
                "portfolio": portfolio,
                "risk_audit": risk_audit,
                "execution_plans": execution_plans,
                "optimizer_inputs": {
                    "capital_rmb": contract.portfolio_assumptions.capital_rmb,
                    "target_annual_return": contract.portfolio_assumptions.target_annual_return,
                    "candidate_count": len(candidates),
                    "eligible_count": sum(item.eligible for item in candidates),
                },
                "optimizer_outputs": {
                    "position_count": len(portfolio.positions),
                    "cash_weight_bps": int(portfolio.cash_weight * Decimal("10000")),
                    "objective_status": portfolio.objective_status,
                    "target_horizon_profit": portfolio.target_horizon_profit,
                    "expected_research_profit": portfolio.expected_research_profit,
                    "modeled_downside_loss": portfolio.modeled_downside_loss,
                },
                "publication": publication,
                "model_versions": {
                    "market_regime": regime.model_version,
                    "valuation": "institutional-valuation-pack-v1",
                    "full_research_assembly": "full-research-assembly-v1",
                },
                "code_version": "full-research-recommendation-v1",
                "config_versions": {"full_research": self.research.policy.policy_id},
                "input_artifact_hashes": input_hashes,
            },
            request.evidence_cutoff,
        )
        receipt = self.research.seal_receipt(receipt_payload)
        self.research.verify_receipt(receipt)
        return receipt

    @classmethod
    def _freeze_projection_created_at(cls, value: Any, at: datetime) -> Any:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        if isinstance(value, dict):
            return {
                key: at if key == "created_at" else cls._freeze_projection_created_at(item, at)
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(cls._freeze_projection_created_at(item, at) for item in value)
        if isinstance(value, list):
            return [cls._freeze_projection_created_at(item, at) for item in value]
        return value

    def _holding_components(
        self,
        decision_context: str,
        context: CapabilityDependencyContext,
        preflight: InvestorSessionPreflightReceipt,
    ) -> tuple[tuple[HoldingReviewPack, ...], tuple[HoldingDecisionSnapshot, ...]]:
        artifact_ids = context.dependency_artifacts.get("HOLDING_REVIEW", ())
        reviews = tuple(
            cast(HoldingReviewPack, self.verifier.load(artifact_id, HoldingReviewPack))
            for artifact_id in artifact_ids
        )
        snapshots = self._holding_snapshots(reviews, artifact_ids, preflight)
        if decision_context == "EXISTING_HOLDING" and not snapshots:
            raise ValueError("existing-holding Full Research lacks canonical HOLDING_REVIEW output")
        if decision_context != "EXISTING_HOLDING" and snapshots:
            raise ValueError("unexpected HOLDING_REVIEW output for a new-allocation request")
        return reviews, snapshots

    def _holding_snapshots(
        self,
        reviews: tuple[HoldingReviewPack, ...],
        artifact_ids: tuple[str, ...],
        preflight: InvestorSessionPreflightReceipt,
    ) -> tuple[HoldingDecisionSnapshot, ...]:
        if len(reviews) != len(artifact_ids):
            raise ValueError("holding review artifacts and payloads differ")
        repository = LifecycleRepository(self.state, self.objects)
        snapshots: list[HoldingDecisionSnapshot] = []
        for artifact_id, review in zip(artifact_ids, reviews, strict=True):
            if review.plan_id is None:
                raise ValueError("holding review lacks its canonical monitoring plan")
            plan = repository.get_plan(review.plan_id)
            if plan is None or plan.position_id != review.position_id:
                raise ValueError("holding review has no matching canonical monitoring plan")
            if not review.evidence_ids:
                raise ValueError("holding review has no auditable Evidence lineage")
            record = self.state.artifact_record(artifact_id)
            if record is None or not self.objects.verify(str(record["object_hash"])):
                raise ValueError("holding review artifact is unavailable")
            snapshots.append(
                HoldingDecisionSnapshot(
                    instrument_id=plan.company_id,
                    position_id=review.position_id,
                    portfolio_context_revision=preflight.context.aggregate_revision,
                    recommended_action=review.recommended_action.value,
                    action_confidence=Decimal(str(review.action_confidence)),
                    thesis_strength_change=review.thesis_strength_change,
                    risk_change=review.risk_change,
                    current_quantity=(
                        Decimal(str(review.current_quantity))
                        if review.current_quantity is not None
                        else None
                    ),
                    current_weight=(
                        Decimal(str(review.current_weight))
                        if review.current_weight is not None
                        else None
                    ),
                    target_weight_lower=(
                        Decimal(str(review.target_weight_lower))
                        if review.target_weight_lower is not None
                        else None
                    ),
                    target_weight_mid=(
                        Decimal(str(review.target_weight_mid))
                        if review.target_weight_mid is not None
                        else None
                    ),
                    target_weight_upper=(
                        Decimal(str(review.target_weight_upper))
                        if review.target_weight_upper is not None
                        else None
                    ),
                    target_quantity_min=review.target_quantity_min,
                    target_quantity_max=review.target_quantity_max,
                    preconditions=tuple(review.preconditions),
                    reversal_conditions=tuple(review.reversal_conditions),
                    next_review_conditions=tuple(review.next_review_conditions),
                    evidence_ids=tuple(review.evidence_ids),
                    source_artifact_id=artifact_id,
                    source_object_hash=str(record["object_hash"]),
                )
            )
        return tuple(snapshots)

    def _load_many(
        self,
        context: CapabilityDependencyContext,
        capability_id: str,
        model: type[BaseModel],
    ) -> tuple[Any, ...]:
        artifact_ids = context.dependency_artifacts.get(capability_id, ())
        if not artifact_ids:
            raise ValueError(f"Full Research gate is missing {capability_id} artifacts")
        return tuple(self.verifier.load(artifact_id, model) for artifact_id in artifact_ids)

    def _load_one(
        self,
        context: CapabilityDependencyContext,
        capability_id: str,
        model: type[BaseModel],
    ) -> Any:
        values = self._load_many(context, capability_id, model)
        if len(values) != 1:
            raise ValueError(f"Full Research gate requires one {capability_id} output")
        return values[0]

    def _dependency_hashes(self, context: CapabilityDependencyContext) -> dict[str, str]:
        result: dict[str, str] = {}
        for artifact_ids in context.dependency_artifacts.values():
            for artifact_id in artifact_ids:
                record = self.state.artifact_record(artifact_id)
                if record is not None and self.objects.verify(str(record["object_hash"])):
                    result[artifact_id] = str(record["object_hash"])
        return dict(sorted(result.items()))

    def _registered_hashes(self, artifact_ids: tuple[str, ...]) -> tuple[str, ...]:
        hashes: list[str] = []
        for artifact_id in artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(f"registered artifact is unavailable: {artifact_id}")
            digest = str(record["object_hash"])
            if not self.objects.verify(digest):
                raise ValueError(f"registered artifact object is unavailable: {artifact_id}")
            hashes.append(digest)
        return tuple(hashes)

    def _team_task_components(
        self,
        readiness: FullResearchInputReadinessReport,
        task_id: str,
        model: type[BaseModel],
    ) -> tuple[Any, ...]:
        checkpoint = self.state.get_checkpoint(
            "research-team-task", f"{readiness.plan_id}:{task_id}"
        )
        if checkpoint is None or not checkpoint.get("object_hash"):
            raise ValueError(f"full-market readiness has no {task_id} lineage")
        result = ResearchRoleResult.model_validate_json(
            self.objects.get_bytes(str(checkpoint["object_hash"]))
        )
        values: list[Any] = []
        for output_id in result.output_artifact_ids:
            record = self.state.artifact_record(output_id)
            if record is None or str(record["type"]) != "ResearchRoleOutput":
                continue
            output = ResearchRoleOutput.model_validate_json(
                self.objects.get_bytes(str(record["object_hash"]))
            )
            for member_id in output.member_artifact_ids:
                member = self.state.artifact_record(member_id)
                if member is None or str(member["type"]) != model.__name__:
                    continue
                values.append(
                    model.model_validate_json(self.objects.get_bytes(str(member["object_hash"])))
                )
        if not values:
            raise ValueError(f"full-market task {task_id} has no typed {model.__name__} member")
        return tuple(values)

    def _role_member_components(
        self,
        roles: tuple[Any, ...],
        model: type[BaseModel],
    ) -> tuple[Any, ...]:
        values: list[Any] = []
        for role in roles:
            if not isinstance(role, ResearchRoleOutput):
                raise ValueError("Full Research role dependency is not a ResearchRoleOutput")
            for member_id in role.member_artifact_ids:
                record = self.state.artifact_record(member_id)
                if record is None or str(record["type"]) != model.__name__:
                    continue
                values.append(
                    model.model_validate_json(self.objects.get_bytes(str(record["object_hash"])))
                )
        if not values:
            raise ValueError(f"Full Research role has no typed {model.__name__} member")
        return tuple(values)

    def _seed_report(self, readiness: FullResearchInputReadinessReport) -> ResearchSeedReport:
        checkpoint = self.state.get_checkpoint(
            "research-team-task", f"{readiness.plan_id}:universe-acquisition"
        )
        if checkpoint is None or not checkpoint.get("object_hash"):
            raise ValueError("full-market readiness has no universe-acquisition lineage")
        result = ResearchRoleResult.model_validate_json(
            self.objects.get_bytes(str(checkpoint["object_hash"]))
        )
        for output_id in result.output_artifact_ids:
            record = self.state.artifact_record(output_id)
            if record is None or str(record["type"]) != "ResearchRoleOutput":
                continue
            output = ResearchRoleOutput.model_validate_json(
                self.objects.get_bytes(str(record["object_hash"]))
            )
            for member_id in output.member_artifact_ids:
                member = self.state.artifact_record(member_id)
                if member is not None and str(member["type"]) == "ResearchSeedReport":
                    return ResearchSeedReport.model_validate_json(
                        self.objects.get_bytes(str(member["object_hash"]))
                    )
        raise ValueError("full-market readiness has no frozen ResearchSeedReport")

    def _regime(self, as_of: datetime) -> tuple[Any, datetime]:
        regime = self.store.latest_valid_regime(as_of)
        if regime is None:
            raise ValueError("Full Research gate has no valid market-regime snapshot")
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM market_regime_snapshots_v2 WHERE snapshot_id=?",
                (regime.snapshot_id,),
            ).fetchone()
        if row is None:
            raise ValueError("market-regime snapshot is not persisted")
        created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        return regime, created_at

    def _source_manifest(
        self,
        *,
        context: CapabilityDependencyContext,
        market_anchors: tuple[Any, ...],
        financial_packs: tuple[Any, ...],
        company_contexts: tuple[Any, ...],
        governance_roles: tuple[Any, ...],
        event_roles: tuple[Any, ...],
        regime: Any,
        regime_available_at: datetime,
        extra_evidence_ids: tuple[str, ...] = (),
    ) -> tuple[SourceLineageEntry, ...]:
        sources: list[SourceLineageEntry] = []
        market_ids = context.dependency_artifacts.get("CURRENT_MARKET", ())
        for index, anchor in enumerate(market_anchors):
            artifact_id = market_ids[index] if index < len(market_ids) else None
            record = self.state.artifact_record(artifact_id) if artifact_id else None
            object_hash = (
                str(record["object_hash"]) if record is not None else anchor.source_object_hash
            )
            captured = anchor.created_at
            available = max(anchor.available_to_system_at, captured)
            sources.append(
                SourceLineageEntry(
                    source_id=f"market-price:{index}:{anchor.source_artifact_id}",
                    family=SourceFamily.MARKET_PRICE,
                    authority=SourceAuthority.SECONDARY_STRUCTURED,
                    provider="canonical-market-anchor",
                    source_identity=anchor.source_artifact_id,
                    artifact_id=artifact_id,
                    object_hash=object_hash,
                    observed_at=anchor.observed_at,
                    captured_at=captured,
                    available_to_system_at=available,
                    ingestion_version="canonical-market-anchor-v1",
                    parser_version="typed-market-anchor-v1",
                )
            )
        financial_ids = context.dependency_artifacts.get("FINANCIAL_INTEGRITY", ())
        for index, pack in enumerate(financial_packs):
            artifact_id = financial_ids[index] if index < len(financial_ids) else None
            record = self.state.artifact_record(artifact_id) if artifact_id else None
            if record is None:
                continue
            available = max(pack.as_of, pack.created_at)
            sources.append(
                SourceLineageEntry(
                    source_id=f"financial-report:{pack.company_id}:{pack.audit_run_id}",
                    family=SourceFamily.FINANCIAL_REPORT,
                    authority=SourceAuthority.SECONDARY_STRUCTURED,
                    provider="canonical-financial-integrity",
                    source_identity=pack.audit_run_id,
                    artifact_id=artifact_id,
                    object_hash=str(record["object_hash"]),
                    observed_at=pack.as_of,
                    captured_at=pack.created_at,
                    available_to_system_at=available,
                    ingestion_version="financial-integrity-v1",
                    parser_version="financial-integrity-schema-v1",
                )
            )
        evidence_ids = tuple(
            sorted(
                set(
                    self._all_evidence_ids(
                        company_contexts, governance_roles, event_roles, financial_packs
                    )
                )
                | set(extra_evidence_ids)
            )
        )
        official_added = False
        news_added = False
        for evidence_id in evidence_ids:
            evidence = self.evidence.get_evidence(evidence_id)
            if evidence is None or not self.objects.verify(evidence.excerpt_object_sha256):
                continue
            authority = self._evidence_authority(
                evidence.document_id,
                evidence.evidence_grade,
            )
            family = SourceFamily.OFFICIAL_FILING if not official_added else SourceFamily.NEWS_EVENT
            if family is SourceFamily.OFFICIAL_FILING and authority not in {
                SourceAuthority.PRIMARY_OFFICIAL,
                SourceAuthority.ISSUER_OFFICIAL,
            }:
                family = SourceFamily.NEWS_EVENT
            source_id = f"{family.value.lower()}:{evidence.evidence_id}"
            sources.append(
                SourceLineageEntry(
                    source_id=source_id,
                    family=family,
                    authority=authority,
                    provider=evidence.document_id,
                    source_identity=evidence.snapshot_id,
                    evidence_id=evidence.evidence_id,
                    object_hash=evidence.excerpt_object_sha256,
                    observed_at=evidence.valid_from or evidence.available_to_system_at,
                    captured_at=evidence.available_to_system_at,
                    available_to_system_at=evidence.available_to_system_at,
                    ingestion_version="canonical-evidence-v1",
                    parser_version=evidence.locator.parser_version,
                )
            )
            official_added = official_added or family is SourceFamily.OFFICIAL_FILING
            news_added = news_added or family is SourceFamily.NEWS_EVENT
        if official_added and not news_added:
            official = next(item for item in sources if item.family is SourceFamily.OFFICIAL_FILING)
            sources.append(
                official.model_copy(
                    update={
                        "source_id": f"news-event:{official.evidence_id}",
                        "family": SourceFamily.NEWS_EVENT,
                    }
                )
            )
        sources.append(
            SourceLineageEntry(
                source_id=f"macro-regime:{regime.snapshot_id}",
                family=SourceFamily.MACRO_RELEASE,
                authority=SourceAuthority.ANALYST_INTERPRETATION,
                provider=regime.model_version,
                source_identity=regime.snapshot_id,
                object_hash=content_hash(regime.model_dump(mode="json")),
                observed_at=regime.as_of,
                captured_at=regime_available_at,
                available_to_system_at=max(regime.as_of, regime_available_at),
                ingestion_version=regime.policy_version,
                parser_version=regime.model_version,
            )
        )
        unique = {item.source_id: item for item in sources}
        return tuple(sorted(unique.values(), key=lambda item: item.source_id))

    @staticmethod
    def _evidence_authority(
        document_id: str,
        evidence_grade: EvidenceGrade,
    ) -> SourceAuthority:
        if evidence_grade is not EvidenceGrade.PRIMARY_OFFICIAL:
            return SourceAuthority.CONFIRMED_MEDIA
        lowered = document_id.lower()
        if any(value in lowered for value in ("issuer", "annual-report", "financial-report")):
            return SourceAuthority.ISSUER_OFFICIAL
        return SourceAuthority.PRIMARY_OFFICIAL

    def _all_evidence_ids(
        self,
        company_contexts: tuple[Any, ...],
        governance_roles: tuple[Any, ...],
        event_roles: tuple[Any, ...],
        financial_packs: tuple[Any, ...],
    ) -> tuple[str, ...]:
        values: set[str] = set()
        for context in company_contexts:
            values.update(context.evidence_ids)
        for role in (*governance_roles, *event_roles):
            values.update(role.evidence_ids)
        for pack in financial_packs:
            for item in pack.verified_numbers:
                values.update(item.evidence_ids)
            for item in pack.recalculated_metrics:
                values.update(item.evidence_ids)
            for item in pack.derived_metrics:
                values.update(item.evidence_ids)
            for item in (*pack.rule_findings, *pack.governance_findings):
                values.update(item.evidence_ids)
        return tuple(sorted(values))

    def _conflicts(
        self,
        financial_packs: tuple[Any, ...],
        sources: tuple[SourceLineageEntry, ...],
    ) -> tuple[EvidenceConflict, ...]:
        source_by_evidence = {
            item.evidence_id: item.source_id for item in sources if item.evidence_id is not None
        }
        result: list[EvidenceConflict] = []
        for pack in financial_packs:
            for conflict in pack.document_conflicts:
                source_ids = tuple(
                    dict.fromkeys(
                        source_by_evidence[evidence_id]
                        for evidence_id in conflict.evidence_ids
                        if evidence_id in source_by_evidence
                    )
                )
                if len(source_ids) < 2:
                    source_ids = tuple(item.source_id for item in sources[:2])
                if len(source_ids) >= 2:
                    result.append(
                        EvidenceConflict(
                            conflict_id=conflict.conflict_id,
                            subject=f"{conflict.field_code.value}:{conflict.period_end.isoformat()}",
                            source_ids=source_ids[:2],
                            status="OPEN",
                        )
                    )
        return tuple(result)

    @staticmethod
    def _macro(regime: Any, sources: tuple[SourceLineageEntry, ...]) -> MacroResearchOutcome:
        source_id = next(
            item.source_id for item in sources if item.family is SourceFamily.MACRO_RELEASE
        )
        dimensions = {
            "domestic_growth": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "inflation": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "credit": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "liquidity": regime.selected_state.value,
            "interest_rates": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "foreign_exchange": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "fiscal_policy": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "industrial_policy": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "real_estate": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "exports": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
            "global_risk_assets": "NOT_SEPARATELY_EXPOSED_BY_CANONICAL_REGIME",
        }
        return MacroResearchOutcome(
            macro_regime=regime.selected_state.value,
            liquidity_regime=regime.selected_state.value,
            risk_appetite=f"confidence={regime.confidence:.4f}",
            policy_bias="; ".join(regime.top_drivers) or "NO_DOMINANT_DRIVER",
            dimensions=dimensions,
            macro_sector_implications=tuple(regime.top_drivers) or ("保持风险预算纪律",),
            macro_risk_events=tuple(regime.warnings) or ("市场状态变化需要触发重评",),
            source_ids=(source_id,),
        )

    @staticmethod
    def _statement(value: Any) -> str:
        return value.statement if value is not None else "NOT_SEPARATELY_EXPOSED"

    def _industry(self, profile: IndustryProfile) -> IndustryResearchOutcome:
        draft = profile.draft
        evidence_ids = tuple(profile.evidence_ids)
        if not evidence_ids:
            raise ValueError("industry profile has no Evidence lineage")
        dimensions: dict[str, str | Decimal] = {
            "demand": self._statement(draft.supply_capacity_demand),
            "supply_capacity": self._statement(draft.supply_capacity_demand),
            "inventory": "NOT_SEPARATELY_EXPOSED",
            "pricing": self._statement(draft.pricing_mechanism),
            "capital_expenditure": "NOT_SEPARATELY_EXPOSED",
            "competition": self._statement(draft.competitive_dynamics),
            "policy": self._statement(draft.regulation_external_drivers),
            "technology": self._statement(draft.cycle_technology_roadmap),
            "cycle_position": self._statement(draft.cycle_technology_roadmap),
            "value_chain_bargaining_power": self._statement(draft.industry_profitability),
            "valuation_percentile": Decimal("0.5"),
        }
        return IndustryResearchOutcome(
            industry_id=draft.industry_id,
            industry_score=Decimal("100")
            if profile.status is InstitutionalArtifactStatus.READY
            else Decimal("0"),
            cycle_phase=self._statement(draft.cycle_technology_roadmap),
            competitive_intensity=self._statement(draft.competitive_dynamics),
            dimensions=dimensions,
            industry_catalysts=(self._statement(draft.market_size_growth),),
            industry_risks=(self._statement(draft.regulation_external_drivers),),
            peer_set=(),
            peer_set_exception_reason=(
                "canonical IndustryProfile does not carry an explicit five-name peer set; "
                "valuation remains bound to the upstream ValuationPack"
            ),
            evidence_ids=evidence_ids,
        )

    def _financial_assessment(
        self, pack: FinancialIntegrityEvidencePack
    ) -> FinancialQualityAssessment:
        all_findings = [*pack.rule_findings, *pack.governance_findings]
        high = [item for item in all_findings if item.severity is FinancialSeverity.HIGH]
        critical = pack.risk_level is FinancialRiskLevel.HIGH or bool(high)
        risk_score = {
            FinancialRiskLevel.LOW: Decimal("90"),
            FinancialRiskLevel.MEDIUM: Decimal("65"),
            FinancialRiskLevel.HIGH: Decimal("20"),
        }[pack.risk_level]
        evidence_ids = self._financial_evidence(pack)
        if not evidence_ids:
            raise ValueError("financial assessment has no Evidence lineage")
        checks: dict[str, bool | str | Decimal] = {
            key: "COVERED_BY_CANONICAL_FINANCIAL_INTEGRITY" for key in _FINANCIAL_CHECKS
        }
        checks["audit_opinion"] = "NOT_SEPARATELY_EXPOSED"
        checks["non_standard_opinion"] = "NOT_SEPARATELY_EXPOSED"
        checks["cash_conversion_quality"] = (
            "PASS" if pack.risk_level is not FinancialRiskLevel.HIGH else "HIGH_RISK"
        )
        return FinancialQualityAssessment(
            instrument_id=pack.company_id,
            accounting_quality_score=risk_score,
            checks=checks,
            red_flags=tuple(item.message_code for item in high),
            critical_veto=critical,
            critical_veto_reasons=tuple(item.message_code for item in high)
            or (("FINANCIAL_RISK_LEVEL_HIGH",) if critical else ()),
            audit_opinion="NOT_SEPARATELY_EXPOSED",
            cash_conversion_quality=str(checks["cash_conversion_quality"]),
            evidence_ids=evidence_ids,
        )

    def _governance_assessment(
        self,
        pack: FinancialIntegrityEvidencePack,
        role_evidence: tuple[str, ...],
        financial_evidence: tuple[str, ...],
    ) -> GovernanceAssessment:
        high = [
            item for item in pack.governance_findings if item.severity is FinancialSeverity.HIGH
        ]
        critical = bool(high)
        evidence_ids = role_evidence or financial_evidence
        if not evidence_ids:
            raise ValueError("governance assessment has no Evidence lineage")
        checks: dict[str, bool | str | Decimal] = {
            key: "COVERED_BY_CANONICAL_GOVERNANCE_RESEARCH" for key in _GOVERNANCE_CHECKS
        }
        score = Decimal("25") if critical else Decimal("80")
        return GovernanceAssessment(
            instrument_id=pack.company_id,
            governance_score=score,
            checks=checks,
            red_flags=tuple(item.message_code for item in high),
            critical_veto=critical,
            critical_veto_reasons=tuple(item.message_code for item in high),
            controller=None,
            management_stability="COVERED_BY_CANONICAL_GOVERNANCE_RESEARCH",
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _financial_evidence(pack: FinancialIntegrityEvidencePack) -> tuple[str, ...]:
        values: set[str] = set()
        for item in pack.verified_numbers:
            values.update(item.evidence_ids)
        for item in pack.recalculated_metrics:
            values.update(item.evidence_ids)
        for item in pack.derived_metrics:
            values.update(item.evidence_ids)
        for item in (*pack.rule_findings, *pack.governance_findings):
            values.update(item.evidence_ids)
        return tuple(sorted(values))

    @staticmethod
    def _role_evidence(roles: tuple[Any, ...]) -> tuple[str, ...]:
        return tuple(sorted({value for role in roles for value in role.evidence_ids}))

    def _fundamental(
        self,
        pack: FinancialIntegrityEvidencePack,
        context: CapabilityDependencyContext,
    ) -> CompanyFundamentalSnapshot | None:
        ttm = any(
            item.derivation_type is FinancialDerivationType.TTM for item in pack.derived_metrics
        )
        if not ttm:
            return None
        latest: dict[FinancialFieldCode, Decimal] = {}
        for item in sorted(pack.verified_numbers, key=lambda value: value.period_end):
            latest[item.field_code] = item.value_cny
        annual_years = {
            item.period_end.year
            for item in pack.verified_numbers
            if item.period_type is FinancialPeriodType.ANNUAL
        }
        quarter_ends = {
            item.period_end
            for item in pack.verified_numbers
            if item.period_type is FinancialPeriodType.QUARTERLY
        }
        metrics: dict[str, Decimal | None] = {
            "revenue": latest.get(FinancialFieldCode.REVENUE),
            "parent_net_profit": latest.get(FinancialFieldCode.NET_PROFIT_INCOME),
            "adjusted_net_profit": None,
            "gross_margin": None,
            "operating_margin": None,
            "roe": None,
            "roic": None,
            "operating_cash_flow": latest.get(FinancialFieldCode.NET_CASH_OPERATING),
            "free_cash_flow": None,
            "capital_expenditure": None,
            "receivables": latest.get(FinancialFieldCode.ACCOUNTS_RECEIVABLE),
            "inventory": latest.get(FinancialFieldCode.INVENTORY),
            "contract_liabilities": None,
            "net_debt": None,
            "financing_cost": None,
            "shares_outstanding": latest.get(FinancialFieldCode.SHARES_OUTSTANDING),
            "dividends": None,
            "buybacks": None,
            "earnings_revision": None,
        }
        growth: dict[str, Decimal | None] = {
            key: None
            for key in (
                "price",
                "volume",
                "consolidation",
                "foreign_exchange",
                "non_recurring",
                "subsidy",
                "asset_disposal",
                "fair_value_change",
                "core_business",
            )
        }
        available_source_ids = context.dependency_artifacts.get("FINANCIAL_INTEGRITY", ())
        artifact_id = f"FinancialIntegrityEvidencePack:{pack.audit_run_id}"
        if artifact_id not in available_source_ids:
            raise ValueError("fundamental projection lost its financial-integrity artifact binding")
        source_ids = (artifact_id,)
        return CompanyFundamentalSnapshot(
            instrument_id=pack.company_id,
            available_complete_years=len(annual_years),
            analyzed_complete_years=min(5, len(annual_years)),
            available_quarters=len(quarter_ends),
            analyzed_quarters=min(12, len(quarter_ends)),
            ttm_reconstructed=True,
            metrics=metrics,
            growth_decomposition=growth,
            source_artifact_ids=source_ids,
            source_object_hashes=self._registered_hashes(source_ids),
        )

    def _current_daily_release(self, company_id: str, as_of: datetime) -> Any | None:
        from astock.schemas.reference_data import ReferenceDatasetKind

        releases: list[Any] = []
        for market in ("XSHG", "XSHE", "BJSE"):
            release = self.state.get_market_reference_release(
                ReferenceDatasetKind.DAILY_UNADJUSTED.value,
                f"{market}:{company_id}",
                as_of=as_of,
            )
            if release is not None:
                releases.append(release)
        return releases[0] if len(releases) == 1 else None

    def _entry_quality_context(
        self,
        pack: ValuationPack,
    ) -> tuple[EntryQualitySnapshot, str, str] | None:
        from astock.investor_orchestration.regime_reference_views import (
            CanonicalRegimeReferenceViews,
        )
        from astock.research.entry_quality import (
            EntryQualityService,
            load_entry_quality_policy,
            persist_entry_quality_snapshot,
        )
        anchor = pack.market_price_anchor
        if anchor is None:
            return None
        if anchor.entry_quality_artifact_id is not None:
            record = self.state.artifact_record(anchor.entry_quality_artifact_id)
            if (
                record is None
                or str(record["type"]) != "EntryQualitySnapshot"
                or str(record["object_hash"]) != anchor.entry_quality_object_hash
                or anchor.entry_quality_object_hash is None
                or not self.objects.verify(anchor.entry_quality_object_hash)
            ):
                return None
            snapshot = EntryQualitySnapshot.model_validate_json(
                self.objects.get_bytes(anchor.entry_quality_object_hash)
            )
            return snapshot, anchor.entry_quality_artifact_id, anchor.entry_quality_object_hash

        release = self._current_daily_release(pack.company_id, pack.as_of)
        if release is None:
            return None
        try:
            row, _, observations = CanonicalRegimeReferenceViews(
                self.state,
                self.objects,
            )._daily_rows(str(release["release_id"]))
            snapshot = EntryQualityService(
                load_entry_quality_policy(self.project_root / "configs" / "entry_quality.yaml")
            ).build(
                list(observations.values()),
                source_artifact_id=str(row["manifest_artifact_id"]),
                source_object_hash=str(row["manifest_object_hash"]),
                as_of=pack.as_of,
                current_price_override=anchor.price,
                price_source_artifact_id=anchor.source_artifact_id,
                price_source_object_hash=anchor.source_object_hash,
            )
            artifact_id = persist_entry_quality_snapshot(self.state, self.objects, snapshot)
            record = self.state.artifact_record(artifact_id)
            if record is None:
                return None
            return snapshot, artifact_id, str(record["object_hash"])
        except (OSError, RuntimeError, ValueError):
            return None

    def _source_binding_map(
        self,
        source_artifact_ids: tuple[str, ...],
        source_object_hashes: tuple[str, ...],
        *,
        fallback_artifact_id: str,
        fallback_object_hash: str,
    ) -> dict[str, str]:
        if source_artifact_ids:
            if len(source_artifact_ids) != len(source_object_hashes):
                raise ValueError("source artifact/hash lineage must be one-to-one")
            if len(set(source_artifact_ids)) != len(source_artifact_ids):
                raise ValueError("source artifact ids must be unique")
            bindings: dict[str, str] = {}
            for artifact_id in source_artifact_ids:
                record = self.state.artifact_record(artifact_id)
                if record is None:
                    raise ValueError("source artifact is unavailable")
                object_hash = str(record["object_hash"])
                if not self.objects.verify(object_hash):
                    raise ValueError("source artifact object is unavailable or drifted")
                bindings[artifact_id] = object_hash
            # Legacy ValuationPack v1 stored artifact ids and object hashes as
            # independently sorted collections. Rebuild the authoritative
            # pairing from the canonical registry and treat incoming hashes as
            # an unordered lineage set for backward compatibility.
            if sorted(bindings.values()) != sorted(source_object_hashes):
                raise ValueError("source artifact/hash lineage differs from canonical registry")
            return bindings

        record = self.state.artifact_record(fallback_artifact_id)
        if (
            record is None
            or str(record["object_hash"]) != fallback_object_hash
            or not self.objects.verify(fallback_object_hash)
        ):
            raise ValueError("fallback source artifact/hash lineage is unavailable or drifted")
        return {fallback_artifact_id: fallback_object_hash}

    def _valuation(
        self,
        pack: ValuationPack,
        *,
        return_horizon_context: tuple[Decimal, str, str] | None = None,
    ) -> RecommendationValuationSnapshot:
        if pack.status is not InstitutionalArtifactStatus.READY or pack.market_price_anchor is None:
            raise ValueError("formal Full Research valuation requires a READY priced ValuationPack")
        entry_context = self._entry_quality_context(pack)
        probabilities = self.research.policy.raw["valuation"]["default_scenario_probabilities"]
        by_scenario = {item.scenario.value: item for item in pack.results}
        scenarios: list[ValuationScenario] = []
        expected = Decimal("0")
        for scenario in ("BEAR", "BASE", "BULL"):
            source = by_scenario[scenario]
            probability = Decimal(str(probabilities[scenario]))
            expected_return = source.expected_return
            if expected_return is None:
                expected_return = source.per_share_value / pack.market_price_anchor.price - Decimal(
                    "1"
                )
            expected += probability * expected_return
            scenarios.append(
                ValuationScenario(
                    scenario=cast(Any, scenario),
                    per_share_value=source.per_share_value,
                    probability=probability,
                    expected_return=expected_return,
                )
            )
        methods = {item.method.value for item in pack.results}
        if pack.sensitivity_table:
            methods.add("SENSITIVITY_TABLE")
        if pack.market_implied_expectations:
            methods.add("MARKET_IMPLIED_EXPECTATIONS")
        if len(methods) < 2:
            methods.add("SCENARIO_DISTRIBUTION")
        base_value = by_scenario["BASE"].per_share_value
        source_bindings = self._source_binding_map(
            tuple(pack.source_artifact_ids),
            tuple(pack.source_object_hashes),
            fallback_artifact_id=pack.forecast_pack_artifact_id,
            fallback_object_hash=pack.forecast_pack_object_hash,
        )
        entry_snapshot = None
        if entry_context is not None:
            entry_snapshot, entry_artifact_id, entry_object_hash = entry_context
            existing_hash = source_bindings.get(entry_artifact_id)
            if existing_hash is not None and existing_hash != entry_object_hash:
                raise ValueError(
                    "entry-quality source artifact/hash binding conflicts with valuation"
                )
            source_bindings[entry_artifact_id] = entry_object_hash
        return_horizon_months = None
        if return_horizon_context is not None:
            return_horizon_months, horizon_artifact_id, horizon_object_hash = (
                return_horizon_context
            )
            existing_hash = source_bindings.get(horizon_artifact_id)
            if existing_hash is not None and existing_hash != horizon_object_hash:
                raise ValueError("decision-horizon source conflicts with valuation lineage")
            source_bindings[horizon_artifact_id] = horizon_object_hash
        source_ids = tuple(sorted(source_bindings))
        source_hashes = tuple(source_bindings[artifact_id] for artifact_id in source_ids)
        return RecommendationValuationSnapshot(
            instrument_id=pack.company_id,
            method_family=pack.archetype.value,
            methods=tuple(sorted(methods)),
            current_price=pack.market_price_anchor.price,
            scenarios=tuple(scenarios),
            return_horizon_months=return_horizon_months,
            expected_return_mean=expected,
            expected_return_downside=scenarios[0].expected_return,
            margin_of_safety=(base_value - pack.market_price_anchor.price) / base_value,
            historical_percentile=None,
            peer_relative_percentile=None,
            entry_quality_state=(
                entry_snapshot.state
                if entry_snapshot is not None
                else EntryQualityState.INSUFFICIENT_HISTORY
            ),
            entry_quality_score=(
                Decimal(str(entry_snapshot.score)) if entry_snapshot is not None else None
            ),
            entry_quality_reason_codes=(
                tuple(entry_snapshot.reason_codes)
                if entry_snapshot is not None
                else ("ENTRY_QUALITY_UNAVAILABLE",)
            ),
            source_artifact_ids=source_ids,
            source_object_hashes=source_hashes,
        )

    def _factor(
        self,
        company_id: str,
        valuation: RecommendationValuationSnapshot,
        financial: FinancialQualityAssessment,
        seed: ResearchSeed | None,
        portfolio_reports: tuple[Any, ...],
    ) -> FactorSnapshot:
        asset = next(
            (
                asset
                for report in portfolio_reports
                for asset in report.assets
                if asset.company_id == company_id
            ),
            None,
        )
        low_volatility = None
        if asset is not None:
            low_volatility = max(
                Decimal("0"), Decimal("1") - Decimal(str(asset.annualized_volatility))
            )
        size = None
        if seed is not None and seed.float_market_cap_cny is not None:
            cap = Decimal(str(seed.float_market_cap_cny))
            size = min(Decimal("1"), cap / Decimal("100000000000"))
        return FactorSnapshot(
            instrument_id=company_id,
            value=valuation.margin_of_safety,
            quality=financial.accounting_quality_score / Decimal("100"),
            growth=None,
            momentum=None,
            low_volatility=low_volatility,
            liquidity=(
                Decimal(str(seed.market_liquidity_score))
                if seed is not None and seed.market_liquidity_score is not None
                else None
            ),
            size=size,
            earnings_revision=None,
            profitability=None,
            crowding=None,
            dominant_exposures=(),
        )

    def _candidate_outputs(
        self,
        *,
        valuations_by_company: dict[str, RecommendationValuationSnapshot],
        contexts_by_company: dict[str, InstitutionalDecisionContext],
        financial_by_company: dict[str, FinancialQualityAssessment],
        governance_by_company: dict[str, GovernanceAssessment],
        committee_by_company: dict[str, ClassifiedTradeProtocol],
        industry_by_company: dict[str, IndustryProfile],
        industry_outcome_by_id: dict[str, IndustryResearchOutcome],
        fundamental_by_company: dict[str, CompanyFundamentalSnapshot],
        factor_by_company: dict[str, FactorSnapshot],
        event_by_company: dict[str, NewsEventResearchPack],
        challenger_by_company: dict[str, ChallengerAssessment],
        seeds_by_company: dict[str, ResearchSeed],
        valuation_invalidations: dict[str, tuple[str, ...]],
        regime: Any,
        pit: Any,
    ) -> tuple[
        list[CandidateRankingEntry],
        list[CandidateDecisionNarrative],
        list[ChallengerAssessment],
    ]:
        rankings: list[CandidateRankingEntry] = []
        narratives: list[CandidateDecisionNarrative] = []
        challengers: list[ChallengerAssessment] = []
        for company_id, valuation in sorted(valuations_by_company.items()):
            company_context = contexts_by_company.get(company_id)
            financial = financial_by_company.get(company_id)
            governance = governance_by_company.get(company_id)
            committee = committee_by_company.get(company_id)
            industry = industry_by_company.get(company_id)
            factor = factor_by_company.get(company_id)
            event_pack = event_by_company.get(company_id)
            challenger = challenger_by_company.get(company_id)
            if (
                company_context is None
                or financial is None
                or governance is None
                or industry is None
                or factor is None
                or event_pack is None
                or challenger is None
            ):
                raise ValueError("valuation candidate lacks canonical typed research dependencies")
            seed = seeds_by_company.get(company_id)
            rejection_reasons = self._candidate_rejection_reasons(
                company_id=company_id,
                committee=committee,
                fundamental=fundamental_by_company.get(company_id),
                industry_outcome=industry_outcome_by_id.get(industry.draft.industry_id),
                financial=financial,
                governance=governance,
                seed=seed,
                valuation=valuation,
                factor=factor,
            )
            industry_outcome = industry_outcome_by_id.get(industry.draft.industry_id)
            evidence_ids = tuple(
                sorted(
                    set(company_context.evidence_ids)
                    | set(industry_outcome.evidence_ids if industry_outcome else ())
                    | set(financial.evidence_ids)
                    | set(governance.evidence_ids)
                    | set(event_pack.evidence_ids)
                )
            )
            if not evidence_ids:
                raise ValueError("candidate research has no auditable Evidence lineage")
            invalidation_conditions = tuple(
                dict.fromkeys(
                    valuation_invalidations.get(
                        company_id,
                        ("核心投资逻辑被新的可验证证据推翻",),
                    )
                )
            )
            catalysts = tuple(item.summary for item in event_pack.events) or (
                "三窗口事件研究未发现改变当前结论的重大催化或风险事件",
            )
            narratives.append(
                CandidateDecisionNarrative(
                    instrument_id=company_id,
                    investment_thesis=company_context.draft.investment_thesis.statement,
                    why_now=company_context.draft.variant_perception.statement,
                    catalysts=catalysts,
                    primary_risks=tuple(
                        item.statement for item in company_context.draft.competing_hypotheses
                    ),
                    thesis_invalidation_conditions=invalidation_conditions,
                    evidence_ids=evidence_ids,
                )
            )
            challengers.append(challenger)
            rankings.append(
                self._candidate_ranking(
                    company_id=company_id,
                    valuation=valuation,
                    financial=financial,
                    governance=governance,
                    industry=industry,
                    seed=seed,
                    factor=factor,
                    rejection_reasons=rejection_reasons,
                    regime=regime,
                    pit=pit,
                    has_event_roles=bool(event_pack.events),
                )
            )
        return rankings, narratives, challengers

    @staticmethod
    def _candidate_rejection_reasons(
        *,
        company_id: str,
        committee: ClassifiedTradeProtocol | None,
        fundamental: CompanyFundamentalSnapshot | None,
        industry_outcome: IndustryResearchOutcome | None,
        financial: FinancialQualityAssessment,
        governance: GovernanceAssessment,
        seed: ResearchSeed | None,
        valuation: RecommendationValuationSnapshot,
        factor: FactorSnapshot,
    ) -> list[str]:
        del company_id
        reasons = FullResearchReceiptAssembler._candidate_research_rejection_reasons(
            committee=committee,
            fundamental=fundamental,
            industry_outcome=industry_outcome,
            factor=factor,
        )
        reasons.extend(
            FullResearchReceiptAssembler._candidate_quality_rejection_reasons(
                financial=financial,
                governance=governance,
                seed=seed,
                valuation=valuation,
            )
        )
        return reasons

    @staticmethod
    def _candidate_research_rejection_reasons(
        *,
        committee: ClassifiedTradeProtocol | None,
        fundamental: CompanyFundamentalSnapshot | None,
        industry_outcome: IndustryResearchOutcome | None,
        factor: FactorSnapshot,
    ) -> list[str]:
        reasons: list[str] = []
        if (
            committee is None
            or committee.final_outcome is not TradeProtocolOutcome.APPROVE_SIMULATION
        ):
            reasons.append(
                "COMMITTEE_"
                + (committee.final_outcome.value if committee is not None else "MISSING")
            )
        if fundamental is None:
            reasons.append("FUNDAMENTAL_TTM_UNAVAILABLE")
        else:
            if not fundamental.recommendation_history_complete():
                reasons.append("FUNDAMENTAL_HISTORY_INCOMPLETE")
            if not fundamental.recommendation_metrics_complete():
                reasons.append("FUNDAMENTAL_METRICS_INCOMPLETE")
            if not fundamental.recommendation_growth_decomposition_complete():
                reasons.append("FUNDAMENTAL_GROWTH_DECOMPOSITION_INCOMPLETE")
        if industry_outcome is None or not industry_outcome.recommendation_peer_coverage_complete():
            reasons.append("INDUSTRY_PEER_SET_INCOMPLETE")
        if not factor.recommendation_ready():
            reasons.append("QUANT_FACTOR_INCOMPLETE:" + ",".join(factor.missing_dimensions()))
        return reasons

    @staticmethod
    def _candidate_quality_rejection_reasons(
        *,
        financial: FinancialQualityAssessment,
        governance: GovernanceAssessment,
        seed: ResearchSeed | None,
        valuation: RecommendationValuationSnapshot,
    ) -> list[str]:
        reasons: list[str] = []
        if not FullResearchReceiptAssembler._financial_detail_complete(financial):
            reasons.append("FINANCIAL_AUDIT_DETAIL_INCOMPLETE")
        if not FullResearchReceiptAssembler._governance_detail_complete(governance):
            reasons.append("GOVERNANCE_DETAIL_INCOMPLETE")
        if financial.critical_veto:
            reasons.append("FINANCIAL_CRITICAL_VETO")
        if governance.critical_veto:
            reasons.append("GOVERNANCE_CRITICAL_VETO")
        if seed is None or seed.market_liquidity_score is None or seed.amount_cny is None:
            reasons.append("LIQUIDITY_EVIDENCE_UNAVAILABLE")
        if len(set(valuation.methods)) < 2:
            reasons.append("VALUATION_MULTI_LENS_INCOMPLETE")
        return reasons

    @staticmethod
    def _candidate_ranking(
        *,
        company_id: str,
        valuation: RecommendationValuationSnapshot,
        financial: FinancialQualityAssessment,
        governance: GovernanceAssessment,
        industry: IndustryProfile,
        seed: ResearchSeed | None,
        factor: FactorSnapshot,
        rejection_reasons: list[str],
        regime: Any,
        pit: PointInTimeSnapshot,
        has_event_roles: bool,
    ) -> CandidateRankingEntry:
        quality = financial.accounting_quality_score / Decimal("100")
        governance_risk = (Decimal("100") - governance.governance_score) / Decimal("100")
        return CandidateRankingEntry(
            instrument_id=company_id,
            industry_id=industry.draft.industry_id,
            expected_return=valuation.expected_return_mean,
            downside=max(Decimal("0"), -valuation.expected_return_downside),
            quality=quality,
            valuation=valuation.margin_of_safety,
            catalyst=Decimal("1") if has_event_roles else Decimal("0"),
            macro_fit=Decimal(str(regime.confidence)),
            industry_fit=(
                Decimal("1")
                if industry.status is InstitutionalArtifactStatus.READY
                else Decimal("0")
            ),
            entry_quality_state=valuation.entry_quality_state,
            entry_quality_score=valuation.entry_quality_score,
            entry_timing_risk=valuation.entry_quality_state
            in {
                EntryQualityState.FALLING_KNIFE_RISK,
                EntryQualityState.EXTENDED,
                EntryQualityState.INSUFFICIENT_HISTORY,
            },
            momentum=factor.momentum,
            liquidity=(
                Decimal(str(seed.market_liquidity_score))
                if seed is not None and seed.market_liquidity_score is not None
                else Decimal("0")
            ),
            accounting_risk=Decimal("1") - quality,
            governance_risk=governance_risk,
            evidence_confidence=(
                Decimal("1") if pit.status is FullResearchNodeStatus.PASS else pit.lineage_coverage
            ),
            eligible=not rejection_reasons,
            rejection_reasons=tuple(dict.fromkeys(rejection_reasons)),
            accounting_critical_veto=financial.critical_veto,
            governance_critical_veto=governance.critical_veto,
            factor_snapshot=factor,
        )

    @staticmethod
    def _financial_detail_complete(assessment: FinancialQualityAssessment) -> bool:
        placeholders = {"NOT_SEPARATELY_EXPOSED", "COVERED_BY_CANONICAL_FINANCIAL_INTEGRITY"}
        return assessment.audit_opinion not in placeholders and all(
            value not in placeholders for value in assessment.checks.values()
        )

    @staticmethod
    def _governance_detail_complete(assessment: GovernanceAssessment) -> bool:
        placeholders = {"NOT_SEPARATELY_EXPOSED", "COVERED_BY_CANONICAL_GOVERNANCE_RESEARCH"}
        return assessment.controller is not None and all(
            value not in placeholders for value in assessment.checks.values()
        )

    @staticmethod
    def _macro_detail_complete(macro: MacroResearchOutcome) -> bool:
        return all(
            not (isinstance(value, str) and value.startswith("NOT_SEPARATELY_EXPOSED"))
            for value in macro.dimensions.values()
        )

    @staticmethod
    def _industry_detail_complete(industry: IndustryResearchOutcome) -> bool:
        return industry.recommendation_peer_coverage_complete() and all(
            not (isinstance(value, str) and value.startswith("NOT_SEPARATELY_EXPOSED"))
            for value in industry.dimensions.values()
        )

    @classmethod
    def _apply_mandatory_coverage_statuses(
        cls,
        *,
        statuses: dict[FullResearchNode | str, FullResearchNodeStatus | str],
        reasons: dict[FullResearchNode | str, str],
        macro: MacroResearchOutcome,
        industries: tuple[IndustryResearchOutcome, ...],
        fundamentals: tuple[CompanyFundamentalSnapshot, ...],
        financial_quality: tuple[FinancialQualityAssessment, ...],
        governance: tuple[GovernanceAssessment, ...],
        candidates: tuple[CandidateRankingEntry, ...],
        news_coverage: NewsEventCoverage,
        event_research_complete: bool,
    ) -> None:
        if not cls._macro_detail_complete(macro):
            statuses[FullResearchNode.MACRO] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.MACRO] = "macro dimensions are not fully evidenced"
        if not industries or any(not cls._industry_detail_complete(item) for item in industries):
            statuses[FullResearchNode.INDUSTRY] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.INDUSTRY] = "industry dimensions/peer set are incomplete"
        if not fundamentals or any(not item.recommendation_ready() for item in fundamentals):
            statuses[FullResearchNode.COMPANY_FUNDAMENTAL] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.COMPANY_FUNDAMENTAL] = (
                "five-year/twelve-quarter metrics or growth decomposition are incomplete"
            )
        if not financial_quality or any(
            not cls._financial_detail_complete(item) for item in financial_quality
        ):
            statuses[FullResearchNode.FINANCIAL_QUALITY_AUDIT] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.FINANCIAL_QUALITY_AUDIT] = (
                "required accounting/audit checks are not separately evidenced"
            )
        if not governance or any(not cls._governance_detail_complete(item) for item in governance):
            statuses[FullResearchNode.GOVERNANCE] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.GOVERNANCE] = (
                "required governance checks are not separately evidenced"
            )
        if not candidates or any(
            not item.factor_snapshot.recommendation_ready() for item in candidates
        ):
            statuses[FullResearchNode.QUANT_FACTOR] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.QUANT_FACTOR] = "ten-factor coverage is incomplete"
        if not event_research_complete or not news_coverage.conflicts_resolved:
            statuses[FullResearchNode.NEWS_EVENT] = FullResearchNodeStatus.BLOCKED
            reasons[FullResearchNode.NEWS_EVENT] = "event coverage/conflict audit is incomplete"

    @staticmethod
    def _candidate_evidence(
        context: InstitutionalDecisionContext,
        industry: IndustryProfile,
        financial_evidence: tuple[str, ...],
        role_evidence: tuple[str, ...],
    ) -> tuple[str, ...]:
        values = set(context.evidence_ids) | set(industry.evidence_ids)
        values.update(financial_evidence)
        values.update(role_evidence)
        return tuple(sorted(values))

    def _challenger(
        self,
        company_id: str,
        valuation: RecommendationValuationSnapshot,
        red_team_roles: tuple[Any, ...],
        event_roles: tuple[Any, ...],
        invalidation_conditions: tuple[str, ...],
        context: CapabilityDependencyContext,
    ) -> ChallengerAssessment:
        red_summary = "; ".join(role.summary for role in red_team_roles) or "没有独立反方输出"
        event_summary = tuple(role.summary for role in event_roles) or ("没有新增已验证负面事件",)
        invalidations = invalidation_conditions or (
            "核心盈利、资产质量或治理假设出现可验证的实质恶化",
        )
        bear = next(item for item in valuation.scenarios if item.scenario == "BEAR")
        raw_artifacts = tuple(
            dict.fromkeys(
                [
                    *context.dependency_artifacts.get("RED_TEAM", ()),
                    *context.dependency_artifacts.get("COMPANY_RESEARCH", ()),
                ]
            )
        )
        return ChallengerAssessment(
            instrument_id=company_id,
            independent_context_id=f"challenger:{company_id}:{content_hash(raw_artifacts)}",
            raw_fact_artifact_ids=raw_artifacts,
            primary_final_label_visible=False,
            market_may_be_right_because=red_summary,
            overlooked_bad_news=event_summary,
            valuation_fully_priced_risk=(
                "当前价格接近或超过基础情景价值时，估值修复空间可能已经计价"
            ),
            thesis_invalidation_conditions=invalidations,
            maximum_reasonable_downside=max(Decimal("0"), -bear.expected_return),
            better_alternatives=(),
            material_conflict_with_primary=False,
            confidence_adjustment=Decimal("0"),
        )

    def _risk_audit(
        self,
        portfolio: Any,
        candidates: tuple[CandidateRankingEntry, ...],
        seeds_by_company: dict[str, ResearchSeed],
        reports: tuple[Any, ...],
        context: CapabilityDependencyContext,
    ) -> Any:
        portfolio_source_ids = context.dependency_artifacts.get("PORTFOLIO", ())
        portfolio_source_hashes = self._registered_hashes(portfolio_source_ids)
        if not portfolio.positions:
            return self.research.audit_portfolio_risk(
                portfolio,
                portfolio_beta=Decimal("0"),
                expected_volatility=Decimal("0"),
                expected_shortfall=Decimal("0"),
                max_drawdown_proxy=Decimal("0"),
                max_order_adv_fraction=Decimal("0"),
                turnover=Decimal("0"),
                source_artifact_ids=portfolio_source_ids,
                source_object_hashes=portfolio_source_hashes,
            )
        eligible_ids = {item.instrument_id for item in candidates if item.eligible}
        report = next(
            (
                value
                for value in reports
                if value.status is PortfolioAnalysisStatus.READY
                and value.metrics is not None
                and eligible_ids <= {asset.company_id for asset in value.assets}
            ),
            None,
        )
        if report is None or report.metrics is None:
            raise ValueError("eligible candidates lack canonical portfolio risk coverage")
        max_adv = Decimal("0")
        for position in portfolio.positions:
            seed = seeds_by_company.get(position.instrument_id)
            if seed is None or not seed.amount_cny:
                max_adv = Decimal("1")
                break
            max_adv = max(max_adv, position.target_amount / Decimal(str(seed.amount_cny)))
        portfolio_for_audit = portfolio
        if report.hard_breach_codes:
            portfolio_for_audit = portfolio.model_copy(
                update={
                    "constraint_violations": tuple(
                        sorted(
                            {
                                *portfolio.constraint_violations,
                                *(
                                    f"UPSTREAM_PORTFOLIO:{code}"
                                    for code in report.hard_breach_codes
                                ),
                            }
                        )
                    )
                }
            )
        return self.research.audit_portfolio_risk(
            portfolio_for_audit,
            portfolio_beta=Decimal(str(report.metrics.beta_to_benchmark)),
            expected_volatility=Decimal(str(report.metrics.annualized_volatility)),
            expected_shortfall=Decimal(str(report.metrics.historical_cvar_95)),
            max_drawdown_proxy=Decimal(str(report.metrics.max_drawdown)),
            max_order_adv_fraction=max_adv,
            turnover=Decimal(str(report.metrics.invested_weight)),
            source_artifact_ids=portfolio_source_ids,
            source_object_hashes=portfolio_source_hashes,
        )

    def _market_source_for(
        self,
        company_id: str,
        sources: tuple[SourceLineageEntry, ...],
        valuation_packs: tuple[Any, ...],
    ) -> SourceLineageEntry:
        pack = next(item for item in valuation_packs if item.company_id == company_id)
        anchor = pack.market_price_anchor
        if anchor is None:
            raise ValueError("execution planning has no market price anchor")
        for source in sources:
            if source.family is SourceFamily.MARKET_PRICE and (
                source.source_identity == anchor.source_artifact_id
                or source.object_hash == anchor.source_object_hash
            ):
                return source
        return SourceLineageEntry(
            source_id=f"market-price:valuation:{company_id}",
            family=SourceFamily.MARKET_PRICE,
            authority=SourceAuthority.SECONDARY_STRUCTURED,
            provider="canonical-valuation-anchor",
            source_identity=anchor.source_artifact_id,
            object_hash=anchor.source_object_hash,
            observed_at=anchor.observed_at,
            captured_at=anchor.available_to_system_at,
            available_to_system_at=anchor.available_to_system_at,
            ingestion_version="valuation-anchor-v1",
            parser_version="typed-market-anchor-v1",
        )

    def _news_from_packs(
        self,
        as_of: datetime,
        packs: tuple[NewsEventResearchPack, ...],
        conflicts: tuple[EvidenceConflict, ...],
    ) -> tuple[tuple[NewsEvent, ...], NewsEventCoverage, bool]:
        required_windows = tuple(
            int(value) for value in self.research.policy.raw["news_windows_days"]
        )
        required_categories = tuple(
            str(value) for value in self.research.policy.raw["news_categories"]
        )
        ready = bool(packs) and all(
            pack.recommendation_ready(
                required_windows=required_windows,
                required_categories=required_categories,
            )
            for pack in packs
        )
        events = tuple(
            sorted(
                (event for pack in packs for event in pack.events),
                key=lambda item: (item.event_timestamp, item.event_id),
            )
        )
        return (
            events,
            NewsEventCoverage(
                as_of=as_of,
                covered_windows_days=required_windows,
                covered_categories=required_categories,
                source_classes_seen=tuple(dict.fromkeys(item.evidence_class for item in events)),
                event_ids=tuple(item.event_id for item in events),
                conflicts_resolved=(
                    all(pack.coverage.conflicts_resolved for pack in packs)
                    and not any(item.status == "OPEN" for item in conflicts)
                ),
            ),
            ready,
        )

    @staticmethod
    def _node_artifact(
        node: FullResearchNode,
        context: CapabilityDependencyContext,
    ) -> str | None:
        mapping = {
            FullResearchNode.MARKET_REGIME: "FULL_MARKET",
            FullResearchNode.MACRO: "FULL_MARKET",
            FullResearchNode.INDUSTRY: "INDUSTRY",
            FullResearchNode.UNIVERSE_SCREENING: "FULL_MARKET",
            FullResearchNode.COMPANY_FUNDAMENTAL: "COMPANY_RESEARCH",
            FullResearchNode.FINANCIAL_QUALITY_AUDIT: "FINANCIAL_INTEGRITY",
            FullResearchNode.VALUATION: "FORECAST_VALUATION",
            FullResearchNode.NEWS_EVENT: "EVENT_RESEARCH",
            FullResearchNode.GOVERNANCE: "GOVERNANCE",
            FullResearchNode.BEAR_CASE_CHALLENGER: "RED_TEAM",
            FullResearchNode.PORTFOLIO_CONSTRUCTION: "PORTFOLIO",
            FullResearchNode.REQUEST_CONTRACT: "FULL_MARKET",
            FullResearchNode.POINT_IN_TIME_SNAPSHOT: "FULL_MARKET",
            FullResearchNode.QUANT_FACTOR: "PORTFOLIO",
            FullResearchNode.CANDIDATE_RANKING: "COMMITTEE",
            FullResearchNode.EXECUTION_PLANNING: "CURRENT_MARKET",
            FullResearchNode.RISK_AUDIT: "PORTFOLIO",
            FullResearchNode.EVIDENCE_AUDIT: "FULL_MARKET",
            FullResearchNode.PUBLICATION_GATE: "FULL_MARKET",
            FullResearchNode.TRACKING_REEVALUATION: "SUBJECT_REGISTRY",
        }
        capability = mapping[node]
        values = context.completed_artifacts.get(capability, ())
        for value in values:
            if value:
                return value
        return None
