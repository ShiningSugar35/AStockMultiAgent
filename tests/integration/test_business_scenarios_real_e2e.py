from __future__ import annotations

import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from astock.committee import CommitteeService, load_committee_rules
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence import EvidenceRepository
from astock.external_accounts import ExternalAccountRepository
from astock.financial_integrity import FinancialIntegrityService
from astock.investor_orchestration.account_operations import ExternalAccountOperationReceiptService
from astock.investor_orchestration.capabilities import CapabilityExecutionResult, CapabilityHandler
from astock.investor_orchestration.models import (
    DatePrecision,
    InvestorRequestEnvelope,
    MarketRegimeFeatureSnapshot,
    PortfolioLane,
    SideEffectClass,
)
from astock.investor_orchestration.paper_operations import PaperPreparationReceiptService
from astock.investor_orchestration.regime import MarketRegimeService
from astock.investor_orchestration.scenarios import ScenarioContractRunner
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.research import (
    PositionLifecycleService,
    ResearchCoreService,
    ResearchSkillService,
    load_position_lifecycle_config,
    load_research_core_config,
    load_research_skill_registry,
)
from astock.research.institutional import InstitutionalResearchService
from astock.research.team import ResearchTeamService
from astock.research.trading_classification import TradingClassificationService
from astock.schemas import (
    AvailabilityBasis,
    BaseCaseBuildRequest,
    CommitteeAccessPolicy,
    CommitteeAssessment,
    CommitteeCoverageMetrics,
    CommitteeDecisionRequest,
    CommitteeDecisionScope,
    CommitteeEntryOrderType,
    CommitteePortfolioRiskState,
    CommitteeProtocolDraft,
    CommitteeRatioRange,
    DecisionReferenceStatus,
    EvidenceFreezeRequest,
    FinancialAuditRequest,
    FinancialIndustryProfile,
    FrozenEvidencePack,
    HoldingReviewRequest,
    IndustryBottleneckDiagnosticRequestV2,
    LifecycleCondition,
    LifecycleMetricDefinition,
    LifecycleSourceType,
    PaperTradingClassification,
    PointInTimeStatus,
    PositionAction,
    PositionPlanCreateRequest,
    ResearchMemoArtifact,
    ResearchMemoComposeRequestV2,
    SpecialistRoutePlan,
)
from astock.schemas.external_accounts import (
    ExternalAccountEvent,
    ExternalAccountEventDraft,
    ExternalAccountEventType,
)
from astock.schemas.institutional_research import (
    InstitutionalDecisionContextBuildRequest,
    InstitutionalDecisionContextDraft,
    MarketPriceAnchor,
)
from astock.schemas.market import Market
from astock.schemas.portfolio import PortfolioAnalysisRequest, PortfolioHoldingInput
from astock.schemas.portfolio_decision import ETFResearchMetrics
from astock.schemas.research_runtime import (
    ClassifiedTradeProtocol,
    TradeProtocolOutcome,
    TradingClassificationDraft,
    TradingPriceLimitRegime,
    TradingSpecialRegime,
)
from astock.schemas.research_team import (
    RecommendationReadinessRequest,
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTaskRole,
    ResearchTeamTaskState,
)
from tests.helpers import make_financial_facts
from tests.integration.test_portfolio_service import (
    NOW as PORTFOLIO_NOW,
)
from tests.integration.test_portfolio_service import (
    _history,
    _RecordedPortfolioService,
)
from tests.integration.test_research_core import _draft
from tests.integration.test_research_diagnostics import (
    _diagnostics,
    _industry_contract,
    _route,
    _structured_memo,
)
from tests.unit.test_institutional_research import (
    COMPANY,
    _finalize_request,
    _register_claim_with_evidence,
    _statement,
)
from tests.unit.test_institutional_research import (
    NOW as INSTITUTIONAL_NOW,
)
from tests.unit.test_paper_operations import (
    _confirmation as _paper_confirmation,
)
from tests.unit.test_paper_operations import (
    _request as _paper_request,
)
from tests.unit.test_paper_operations import (
    _service as _paper_service,
)
from tests.unit.test_research_team import (
    NOW as TEAM_NOW,
)
from tests.unit.test_research_team import (
    _register_acquisition_report,
    _register_seed_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RecordedBank:
    store: InvestorOrchestrationStore
    state: StateStore
    objects: ObjectStore
    question_time: datetime
    handlers: dict[str, CapabilityHandler]


def _register_model(
    state: StateStore,
    objects: ObjectStore,
    value: BaseModel,
    *,
    artifact_id: str | None = None,
    input_hashes: list[str] | None = None,
) -> str:
    identity = artifact_id or f"{type(value).__name__}:business-e2e:{uuid.uuid4()}"
    ref = objects.put_json(value.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=identity,
        artifact_type=type(value).__name__,
        schema_version=str(getattr(value, "schema_version", "1.0")),
        object_hash=ref.sha256,
        input_hashes=input_hashes or [],
    )
    return identity


def _market_anchor(
    state: StateStore,
    objects: ObjectStore,
    *,
    at: datetime,
    suffix: str,
) -> tuple[str, MarketPriceAnchor]:
    source_id = f"market-reference:business-e2e:{suffix}"
    source_ref = objects.put_json(
        {
            "instrument_id": f"XSHG:{COMPANY}",
            "price": "10.00",
            "observed_at": at.isoformat(),
            "recorded_boundary": True,
        }
    )
    state.register_artifact(
        artifact_id=source_id,
        artifact_type="MarketReferenceRelease",
        schema_version="recorded-business-e2e-v1",
        object_hash=source_ref.sha256,
        input_hashes=[],
    )
    anchor = MarketPriceAnchor(
        price=Decimal("10.00"),
        observed_at=at,
        available_to_system_at=at,
        source_artifact_id=source_id,
        source_object_hash=source_ref.sha256,
        created_at=at,
    )
    return _register_model(
        state,
        objects,
        anchor,
        input_hashes=[source_ref.sha256],
    ), anchor


def _financial_pack(state: StateStore, objects: ObjectStore) -> str:
    facts = make_financial_facts(
        state,
        objects,
        company_id=COMPANY,
        source_suffix="business-e2e",
    )
    service = FinancialIntegrityService(
        state,
        objects,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml",
    )
    execution = service.run(
        FinancialAuditRequest(
            company_id=COMPANY,
            as_of=INSTITUTIONAL_NOW,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            facts=facts,
        )
    )
    return f"FinancialIntegrityEvidencePack:{execution.pack.audit_run_id}"


def _complete_company_team(
    team: ResearchTeamService,
    *,
    company_id: str,
    evidence_id: str,
    financial_id: str,
    fundamental_id: str,
    industry_id: str,
    valuation_id: str,
    current_market_id: str,
    decision_context_id: str,
) -> dict[str, str]:
    acquisition_id, _ = _register_acquisition_report(
        team.state,
        team.objects,
        company_id=company_id,
        ready=True,
    )
    plan = team.create_company_plan(
        company_id=company_id,
        acquisition_report_artifact_id=acquisition_id,
        as_of=TEAM_NOW,
        cpu_count=4,
        memory_gib=8,
    )
    preferred_member = {
        "industry-value-chain": industry_id,
        "company-financial-integrity": financial_id,
        "company-fundamental": fundamental_id,
        "company-market-context": current_market_id,
        "valuation": valuation_id,
    }
    output_ids: dict[str, str] = {}
    for task in plan.tasks:
        if task.role is ResearchTaskRole.RECOMMENDATION_GATE:
            continue
        member_id = preferred_member.get(task.task_id, decision_context_id)
        output = ResearchRoleOutput(
            plan_id=plan.plan_id,
            task_id=task.task_id,
            output_contract=task.output_contract,
            member_artifact_ids=[member_id],
            evidence_ids=[evidence_id],
            readiness_check_results={check: True for check in task.readiness_checks},
            summary="已完成该研究角色的已记录证据复核。",
            created_at=TEAM_NOW,
        )
        registration = team.register_role_output(output)
        artifact_id = str(registration["artifact_id"])
        output_ids[task.task_id] = artifact_id
        context_id = f"business-e2e:{task.task_id}"
        if task.task_id == "bull-case":
            context_id = "business-e2e:bull-independent"
        elif task.task_id == "bear-case":
            context_id = "business-e2e:bear-independent"
        team.register_role_result(
            ResearchRoleResult(
                plan_id=plan.plan_id,
                task_id=task.task_id,
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id=context_id,
                output_artifact_ids=[artifact_id],
                evidence_ids=[evidence_id],
                summary=f"Recorded business E2E result for {task.task_id}.",
                created_at=TEAM_NOW,
            )
        )
    return output_ids


def _portfolio_report(root: Path, state: StateStore, objects: ObjectStore) -> str:
    from astock.committee.config import load_committee_rules

    reference = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(root / "portfolio-parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    histories = {
        COMPANY: _history(state, objects, COMPANY, Market.XSHG, drift=0.0008),
        "600002": _history(state, objects, "600002", Market.XSHG, drift=0.0004),
        "000300": _history(state, objects, "000300", Market.INDEX, drift=0.0005),
    }
    service = _RecordedPortfolioService(
        state,
        objects,
        reference,
        load_committee_rules(PROJECT_ROOT / "configs" / "committee_rules.yaml"),
        histories=histories,
    )
    report = service.analyze(
        PortfolioAnalysisRequest(
            portfolio_id="portfolio:business-e2e",
            as_of=PORTFOLIO_NOW,
            holdings=[
                PortfolioHoldingInput(
                    company_id=COMPANY,
                    market=Market.XSHG,
                    weight=0.10,
                    industry_tag="recorded-a",
                ),
                PortfolioHoldingInput(
                    company_id="600002",
                    market=Market.XSHG,
                    weight=0.10,
                    industry_tag="recorded-b",
                ),
            ],
            lookback_sessions=80,
            minimum_common_sessions=60,
        )
    )
    return f"PortfolioAnalysisReport:{report.report_id}"


def _recorded_etf_metrics(state: StateStore, objects: ObjectStore) -> str:
    """Freeze typed ETF research evidence from an explicit recorded boundary."""
    source_id = "RecordedETFResearchSource:business-e2e"
    source_ref = objects.put_json(
        {
            "instrument_id": "XSHG:510300",
            "observed_at": TEAM_NOW.isoformat(),
            "observation_count": 60,
            "recorded_boundary": True,
        }
    )
    state.register_artifact(
        artifact_id=source_id,
        artifact_type="RecordedETFResearchSource",
        schema_version="recorded-business-e2e-v1",
        object_hash=source_ref.sha256,
        input_hashes=[],
    )
    metrics = ETFResearchMetrics(
        metrics_id="business-e2e-etf-510300",
        profile_artifact_id="recorded-etf-profile:510300",
        instrument_id="XSHG:510300",
        market=Market.XSHG,
        symbol="510300",
        as_of=TEAM_NOW,
        observation_count=60,
        average_daily_amount_cny=500_000_000.0,
        annualized_volatility=0.15,
        tracking_benchmark_instrument_id="INDEX:000300",
        tracking_error_annualized=0.02,
        management_fee_bps=15,
        custody_fee_bps=5,
        total_expense_ratio_bps=20,
        warning_codes=["ETF_PREMIUM_DISCOUNT_UNAVAILABLE_NO_NAV_SERIES"],
        source_artifact_ids=[source_id],
        source_object_hashes=[source_ref.sha256],
        created_at=TEAM_NOW,
    )
    return _register_model(
        state,
        objects,
        metrics,
        artifact_id=f"ETFResearchMetrics:{metrics.metrics_id}",
        input_hashes=[source_ref.sha256],
    )


def _full_market_readiness(
    team: ResearchTeamService,
    *,
    evidence_id: str,
    financial_id: str,
    current_market_id: str,
    industry_id: str,
    fundamental_id: str,
    company_research_id: str,
    event_research_id: str,
    valuation_id: str,
    red_team_id: str,
    committee_id: str,
    portfolio_id: str,
) -> str:
    """Complete the full-market DAG with registered canonical member artifacts."""
    plan = team.create_full_market_plan(as_of=TEAM_NOW, cpu_count=4, memory_gib=8)
    universe_id = f"ResearchSeedReport:business-e2e:{plan.plan_id}"
    _register_seed_report(
        team.state,
        team.objects,
        artifact_id=universe_id,
        full=True,
    )
    members = {
        "cio-intent": company_research_id,
        "macro-regime": current_market_id,
        "policy-regime": industry_id,
        "liquidity-risk": current_market_id,
        "universe-acquisition": universe_id,
        "blind-candidate-scan": company_research_id,
        "sector-comparison": industry_id,
        "company-fundamental": fundamental_id,
        "company-financial-integrity": financial_id,
        "company-catalyst": event_research_id,
        "company-market-context": current_market_id,
        "valuation": valuation_id,
        "bull-case": company_research_id,
        "bear-case": company_research_id,
        "independent-review": red_team_id,
        "committee": committee_id,
        "portfolio-construction": portfolio_id,
    }
    for task in plan.tasks:
        if task.role is ResearchTaskRole.RECOMMENDATION_GATE:
            continue
        output = ResearchRoleOutput(
            plan_id=plan.plan_id,
            task_id=task.task_id,
            output_contract=task.output_contract,
            member_artifact_ids=[members[task.task_id]],
            evidence_ids=[evidence_id],
            readiness_check_results={check: True for check in task.readiness_checks},
            summary=f"Recorded full-market E2E evidence for {task.task_id}.",
            created_at=TEAM_NOW,
        )
        registration = team.register_role_output(output)
        artifact_id = str(registration["artifact_id"])
        context_id = f"business-e2e:full-market:{task.task_id}"
        if task.task_id == "bull-case":
            context_id = "business-e2e:full-market:bull-independent"
        elif task.task_id == "bear-case":
            context_id = "business-e2e:full-market:bear-independent"
        team.register_role_result(
            ResearchRoleResult(
                plan_id=plan.plan_id,
                task_id=task.task_id,
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id=context_id,
                output_artifact_ids=[artifact_id],
                evidence_ids=[evidence_id],
                summary=f"Recorded full-market result for {task.task_id}.",
                created_at=TEAM_NOW,
            )
        )
    report = team.evaluate_readiness(
        RecommendationReadinessRequest(
            plan_id=plan.plan_id,
            checks={check: True for check in team.policy.required_checks},
            created_at=TEAM_NOW,
        )
    )
    assert report.formal_recommendation_allowed
    assert not report.missing_or_failed_checks
    return f"RecommendationReadinessReport:{report.report_id}"


def _lifecycle_artifacts(
    state: StateStore,
    objects: ObjectStore,
    *,
    claim_id: str,
    evidence_id: str,
) -> tuple[dict[str, str], str]:
    evidence = EvidenceRepository(state).get_evidence(evidence_id)
    assert evidence is not None
    PointInTimeService(PointInTimeRepository(state), state, objects).create(
        source_id="pit-source:business-e2e",
        source_document_id=evidence.document_id,
        source_snapshot_id=evidence.snapshot_id,
        published_at=INSTITUTIONAL_NOW,
        effective_at=INSTITUTIONAL_NOW,
        ingested_at=INSTITUTIONAL_NOW,
        available_to_system_at=INSTITUTIONAL_NOW,
        point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
        availability_basis=AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP,
    )
    core = ResearchCoreService(
        state,
        objects,
        load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml"),
    )
    frozen = core.freeze_evidence(
        EvidenceFreezeRequest(
            company_id=COMPANY,
            as_of=INSTITUTIONAL_NOW,
            formal_historical=True,
            allow_approximated=False,
            claim_ids=[claim_id],
        )
    )
    base = core.build_base_case(
        BaseCaseBuildRequest(
            evidence_pack_id=frozen.pack.pack_id,
            draft=_draft(INSTITUTIONAL_NOW, evidence_id).model_copy(update={"company_id": COMPANY}),
        )
    )
    skills = ResearchSkillService(
        state,
        objects,
        load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml"),
    )
    route = _route(
        skills,
        base.pack.base_case_id,
        skill_ids=["IndustryBottleneckSkill"],
        inputs=["industry_evidence"],
        horizon="long",
    )
    diagnostics = _diagnostics(state, skills)
    delta = diagnostics.diagnose(
        IndustryBottleneckDiagnosticRequestV2(
            base_case_id=base.pack.base_case_id,
            route_plan_id=route.route_plan_id,
            method_contract=_industry_contract(base.pack, [evidence_id]),
        )
    )
    memo = diagnostics.compose_memo(
        ResearchMemoComposeRequestV2(
            base_case_id=base.pack.base_case_id,
            route_plan_id=route.route_plan_id,
            delta_ids=[delta.delta.delta_id],
            structured_memo=_structured_memo(delta.delta, base.pack),
        )
    ).memo
    decision_id = "user-decision:business-e2e"
    decision_ref = objects.put_json(
        {
            "decision_id": decision_id,
            "company_id": COMPANY,
            "recorded_at": INSTITUTIONAL_NOW.isoformat(),
            "decision": "HOLD",
            "recorded_boundary": True,
        }
    )
    state.register_artifact(
        artifact_id=decision_id,
        artifact_type="RecordedUserDecision",
        schema_version="recorded-business-e2e-v1",
        object_hash=decision_ref.sha256,
        input_hashes=[frozen.object_sha256],
    )
    lifecycle = PositionLifecycleService(
        state,
        objects,
        load_position_lifecycle_config(PROJECT_ROOT / "configs" / "position_lifecycle.yaml"),
    )
    plan = lifecycle.create_plan(
        PositionPlanCreateRequest(
            position_id=f"monitor:{COMPANY}",
            company_id=COMPANY,
            decision_id=decision_id,
            decision_reference_status=DecisionReferenceStatus.REGISTERED_ARTIFACT,
            base_case_id=base.pack.base_case_id,
            route_plan_id=route.route_plan_id,
            memo_id=memo.memo_id,
            as_of=base.pack.as_of,
            thesis_summary="公司经营假设仍需按正式披露持续复核。",
            entry_assumptions=["需求和利润率维持在已核实区间。"],
            holding_horizon="long",
            key_value_drivers=["销量", "价格", "利润率"],
            validation_metrics=[
                LifecycleMetricDefinition(
                    metric_id="metric:business-e2e",
                    name="经营假设核验指标",
                    unit="ratio",
                    evidence_ids=[evidence_id],
                )
            ],
            monitoring_sources=["price", "fundamental", "event", "manual"],
            monitoring_cadence={
                "price": "daily",
                "fundamental": "on_disclosure",
                "event": "daily",
                "manual": "on_request",
            },
            conditions=[
                LifecycleCondition(
                    rule_id="exit-risk",
                    signal_code="THESIS_INVALIDATED",
                    action=PositionAction.EXIT,
                    source_type=LifecycleSourceType.FUNDAMENTAL,
                    description="核心投资假设被正式证据推翻时复核退出。",
                    hard_block=True,
                ),
                LifecycleCondition(
                    rule_id="review-gap",
                    signal_code="EVIDENCE_GAP_OPEN",
                    action=PositionAction.REVIEW,
                    source_type=LifecycleSourceType.MANUAL,
                    description="出现关键证据缺口时暂停动作并复核。",
                    hard_block=True,
                ),
                LifecycleCondition(
                    rule_id="trim-risk",
                    signal_code="RISK_RISING",
                    action=PositionAction.TRIM,
                    source_type=LifecycleSourceType.PRICE,
                    description="风险上升或估值透支时复核减仓。",
                ),
                LifecycleCondition(
                    rule_id="add-strength",
                    signal_code="FUNDAMENTALS_STRENGTHENED",
                    action=PositionAction.ADD,
                    source_type=LifecycleSourceType.EVENT,
                    description="估值与基本面同时改善后再复核加仓。",
                ),
            ],
            manual_information_needs=["正式披露不足时由用户补充持仓事实。"],
            next_review_at=base.pack.as_of + timedelta(days=7),
        )
    ).plan
    assert plan.plan_id is not None and plan.as_of is not None
    review_evidence_id = "evidence:business-e2e-review"
    review_evidence = evidence.model_copy(
        update={
            "evidence_id": review_evidence_id,
            "available_to_system_at": plan.as_of + timedelta(hours=12),
            "created_at": plan.as_of + timedelta(hours=12),
        }
    )
    EvidenceRepository(state).register_evidence(review_evidence)
    review = lifecycle.review(
        HoldingReviewRequest(
            plan_id=plan.plan_id,
            from_as_of=plan.as_of,
            to_as_of=plan.as_of + timedelta(days=1),
            added_evidence_ids=[review_evidence_id],
            changed_claim_ids=[],
            invalidated_evidence_ids=[],
            unresolved_conflict_ids=[],
            signals=[],
        )
    ).review
    return (
        {
            "FrozenEvidencePack": f"FrozenEvidencePack:{frozen.pack.pack_id}",
            "BaseCasePack": f"BaseCasePack:{base.pack.base_case_id}",
            "SpecialistRoutePlan": f"SpecialistRoutePlan:{route.route_plan_id}",
            "SpecialistDelta": f"SpecialistDelta:{delta.delta.delta_id}",
            "SpecialistDiagnosticReport": (
                f"SpecialistDiagnosticReport:{delta.report.diagnostic_id}"
            ),
            "ResearchMemoArtifact": f"ResearchMemoArtifact:{memo.memo_id}",
        },
        f"HoldingReviewPack:{review.review_id}",
    )


def _committee_protocol(
    state: StateStore,
    objects: ObjectStore,
    *,
    research_artifacts: dict[str, str],
    financial_id: str,
    evidence_id: str,
    as_of: datetime,
) -> str:
    """Run the real committee, then conservatively classify unresolved trading facts as WATCH."""
    rules = load_committee_rules(PROJECT_ROOT / "configs" / "committee_rules.yaml")
    if rules.effective_from > as_of:
        raise ValueError("committee rules are not yet effective for the recorded E2E cutoff")
    committee = CommitteeService(state, objects, rules)

    def load(artifact_id: str, model: type[BaseModel]) -> BaseModel:
        record = state.artifact_record(artifact_id)
        assert record is not None and record["type"] == model.__name__
        return model.model_validate_json(objects.get_bytes(str(record["object_hash"])))

    frozen = load(research_artifacts["FrozenEvidencePack"], FrozenEvidencePack)
    route = load(research_artifacts["SpecialistRoutePlan"], SpecialistRoutePlan)
    memo = load(research_artifacts["ResearchMemoArtifact"], ResearchMemoArtifact)
    assert isinstance(frozen, FrozenEvidencePack)
    assert isinstance(route, SpecialistRoutePlan)
    assert isinstance(memo, ResearchMemoArtifact)
    skill_versions = {item.skill_id: item.skill_version for item in route.selected}
    skill_versions["ResearchMemoComposer"] = memo.composer_version or "research-memo-composer-v1"
    selected = [*research_artifacts.values(), financial_id]
    references = sorted(
        (committee.resolve_reference(artifact_id) for artifact_id in selected),
        key=lambda item: item.artifact_id,
    )
    assessment = CommitteeAssessment(
        company_id=COMPANY,
        scope=CommitteeDecisionScope.NEW_CANDIDATE,
        as_of=as_of,
        expected_return_range=CommitteeRatioRange(
            lower=Decimal("0.12"),
            upper=Decimal("0.25"),
            evidence_ids=[evidence_id],
            created_at=as_of,
        ),
        downside_range=CommitteeRatioRange(
            lower=Decimal("-0.20"),
            upper=Decimal("-0.05"),
            evidence_ids=[evidence_id],
            created_at=as_of,
        ),
        confidence=Decimal("0.80"),
        coverage=CommitteeCoverageMetrics(
            data_coverage=Decimal("1"),
            evidence_coverage=Decimal("1"),
            specialist_coverage=Decimal("1"),
            pit_coverage=Decimal("1"),
            liquidity_score=Decimal("1"),
            evidence_ids=[evidence_id],
            created_at=as_of,
        ),
        portfolio_risk=CommitteePortfolioRiskState(
            current_total_exposure=Decimal("0"),
            post_decision_total_exposure=Decimal("0.04"),
            current_industry_exposure=Decimal("0"),
            post_decision_industry_exposure=Decimal("0.04"),
            max_abs_correlation=Decimal("0.30"),
            portfolio_drawdown=Decimal("0.03"),
            consecutive_loss_count=0,
            evidence_ids=[evidence_id],
            created_at=as_of,
        ),
        tradable=True,
        market_data_quality_pass=True,
        current_position=Decimal("0"),
        requested_position=Decimal("0.04"),
        holding_horizon_days=180,
        review_at=as_of + timedelta(days=7),
        support_evidence_ids=[evidence_id],
        signal_evidence_ids={},
        optional_narrative_requested=False,
        estimated_provider_cost_cny=Decimal("0"),
        protocol=CommitteeProtocolDraft(
            strategy_id="business-e2e-value-strategy",
            skill_versions=dict(sorted(skill_versions.items())),
            earliest_executable_time=as_of + timedelta(days=1),
            entry_rule="仅在冻结研究条件仍成立且价格条件满足时进入模拟确认流程。",
            entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
            position_size_rule="模拟仓位上限为组合风险预算允许值，真实交易仍由用户自行决定。",
            price_stop_rule="价格风险触发时重新研究，不自动真实卖出。",
            volatility_stop_rule="波动异常时暂停新增模拟风险。",
            trailing_stop_rule="移动止损只用于模拟规则复核。",
            time_stop_rule="超过研究期限必须重新验证论文。",
            thesis_invalidation_rule="正式证据推翻核心盈利假设时退出候选。",
            take_profit_rule="估值透支研究假设时重新评估。",
            review_events=["ANNUAL_REPORT", "MATERIAL_DISCLOSURE"],
            max_holding_period_days=730,
            cost_model_version="cn-equity-cost-v1",
            fill_model_version="paper-fill-v1",
            evidence_snapshot_id=frozen.pack_id,
            evidence_ids=[evidence_id],
            created_at=as_of,
        ),
        created_at=as_of,
    )
    execution = committee.decide(
        CommitteeDecisionRequest(
            artifact_references=references,
            assessment=assessment,
            access_policy=CommitteeAccessPolicy(
                frozen_artifact_hashes=sorted(item.object_sha256 for item in references),
                created_at=as_of,
            ),
            created_at=as_of,
        )
    )
    decision_id = f"DecisionPack:{execution.decision.decision_id}"
    protocol_id = f"TradeProtocol:{execution.protocol.protocol_id}"
    decision_record = state.artifact_record(decision_id)
    protocol_record = state.artifact_record(protocol_id)
    assert decision_record is not None and protocol_record is not None

    source_id = "RecordedTradingClassificationSource:business-e2e"
    source_ref = objects.put_json(
        {
            "instrument_id": f"XSHG:{COMPANY}",
            "observed_at": as_of.isoformat(),
            "recorded_boundary": True,
            "runtime_resolved": False,
        }
    )
    state.register_artifact(
        artifact_id=source_id,
        artifact_type="RecordedTradingClassificationSource",
        schema_version="recorded-business-e2e-v1",
        object_hash=source_ref.sha256,
        input_hashes=[],
    )
    classification = TradingClassificationService(state, objects).freeze(
        TradingClassificationDraft(
            company_id=COMPANY,
            market=Market.XSHG,
            symbol=COMPANY,
            as_of=as_of,
            effective_from=as_of - timedelta(minutes=1),
            valid_until=as_of + timedelta(days=1),
            classification=PaperTradingClassification(
                instrument_id=f"XSHG:{COMPANY}",
                board="MAIN",
                risk_status="NORMAL",
                fixed_price_limit_eligible=True,
                suspension_status_verified=True,
                suspended=False,
                evidence_id=evidence_id,
                created_at=as_of,
            ),
            special_no_price_limit=False,
            special_regime=TradingSpecialRegime.ORDINARY,
            price_limit_regime=TradingPriceLimitRegime.FIXED,
            price_limit_rate_bps=1000,
            source_artifact_ids=[source_id],
            created_at=as_of,
        )
    )
    frozen_hashes = sorted(
        {
            str(decision_record["object_hash"]),
            str(protocol_record["object_hash"]),
            classification.object_hash,
        }
    )
    final = ClassifiedTradeProtocol(
        protocol_id=f"classified-trade-protocol:business-e2e:{execution.decision.decision_id}",
        company_id=COMPANY,
        as_of=as_of,
        decision_pack_artifact_id=decision_id,
        decision_pack_object_hash=str(decision_record["object_hash"]),
        committee_protocol_artifact_id=protocol_id,
        committee_protocol_object_hash=str(protocol_record["object_hash"]),
        trading_classification_artifact_id=classification.artifact_id,
        trading_classification_object_hash=classification.object_hash,
        committee_outcome=execution.protocol.outcome,
        final_outcome=TradeProtocolOutcome.WATCH,
        board=classification.release.classification.board,
        risk_status=classification.release.classification.risk_status,
        special_regime=classification.release.special_regime,
        price_limit_regime=classification.release.price_limit_regime,
        price_limit_rate_bps=classification.release.price_limit_rate_bps,
        blocking_codes=["TRADING_CLASSIFICATION_NOT_RUNTIME_RESOLVED"],
        frozen_input_hashes=frozen_hashes,
        paper_simulation_allowed=False,
        created_at=as_of,
    )
    return _register_model(
        state,
        objects,
        final,
        artifact_id=f"ClassifiedTradeProtocol:{final.protocol_id}",
        input_hashes=frozen_hashes,
    )


def _append_account_drafts(
    repository: ExternalAccountRepository,
    drafts: list[ExternalAccountEventDraft],
) -> tuple[list[ExternalAccountEvent], list[str]]:
    inserted, duplicates = repository.append_drafts(drafts)
    event_ids = [*inserted, *duplicates]
    events_by_id: dict[str, ExternalAccountEvent] = {}
    for account_id in sorted({draft.account_id for draft in drafts}):
        events_by_id.update({event.event_id: event for event in repository.list_events(account_id)})
    return [events_by_id[event_id] for event_id in event_ids], duplicates


def _account_draft(
    *,
    account_id: str,
    event_type: ExternalAccountEventType,
    key: str,
    occurred_at: datetime,
    available_at: datetime,
    precision: Literal["EXACT", "DATE_ONLY", "MONTH_ONLY", "UNKNOWN"] = "EXACT",
    market: Market | None = None,
    symbol: str | None = None,
    side: Literal["BUY", "SELL"] | None = None,
    quantity: int | None = None,
    price: str | None = None,
    amount: str | None = None,
    replaces_event_id: str | None = None,
    note: str = "",
) -> ExternalAccountEventDraft:
    return ExternalAccountEventDraft(
        account_id=account_id,
        event_type=event_type,
        occurred_at=occurred_at,
        occurred_at_precision=precision,
        available_to_system_at=available_at,
        market=market,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price_cny=Decimal(price) if price is not None else None,
        amount_cny=Decimal(amount) if amount is not None else None,
        replaces_event_id=replaces_event_id,
        idempotency_key=key,
        note=note,
        created_at=available_at,
    )


def _seed_external_accounts(
    bank: RecordedBank,
) -> tuple[ExternalAccountRepository, dict[str, ExternalAccountEvent]]:
    repository = ExternalAccountRepository(bank.state, bank.objects)
    seed_at = bank.question_time - timedelta(days=30)
    account_ids = (
        "holding-read",
        "holding-provisional",
        "trade-date",
        "sell-date",
        "correction",
        "multi-a",
        "multi-b",
        "transfer-a",
        "transfer-b",
        "cash-b",
        "cash-a",
        "duplicate",
    )
    for account_id in account_ids:
        repository.create_account(
            account_id=account_id,
            display_name=f"业务场景隔离账户 {account_id}",
            created_at=seed_at,
        )
        repository.append_drafts(
            [
                _account_draft(
                    account_id=account_id,
                    event_type=ExternalAccountEventType.CASH_DEPOSIT,
                    key=f"seed-cash-{account_id}",
                    occurred_at=seed_at,
                    available_at=seed_at,
                    amount="100000",
                    note="recorded E2E seed cash",
                )
            ]
        )

    seeded: dict[str, ExternalAccountEvent] = {}
    for account_id, quantity, price, label in (
        ("holding-read", 1000, "30", "holding-read"),
        ("sell-date", 500, "10", "sell-date"),
        ("correction", 300, "12", "correction-original"),
        ("transfer-a", 1500, "10", "transfer-source"),
    ):
        events, _ = _append_account_drafts(
            repository,
            [
                _account_draft(
                    account_id=account_id,
                    event_type=ExternalAccountEventType.TRADE,
                    key=f"seed-trade-{label}",
                    occurred_at=seed_at + timedelta(days=1),
                    available_at=seed_at + timedelta(days=1),
                    market=Market.XSHG,
                    symbol=COMPANY,
                    side="BUY",
                    quantity=quantity,
                    price=price,
                    note="recorded E2E seed position",
                )
            ],
        )
        seeded[label] = events[0]

    duplicate_events, _ = _append_account_drafts(
        repository,
        [
            _account_draft(
                account_id="duplicate",
                event_type=ExternalAccountEventType.TRADE,
                key="scenario-29-existing-trade",
                occurred_at=seed_at + timedelta(days=2),
                available_at=seed_at + timedelta(days=2),
                market=Market.XSHG,
                symbol=COMPANY,
                side="BUY",
                quantity=100,
                price="11",
                note="recorded E2E duplicate probe",
            )
        ],
    )
    seeded["duplicate"] = duplicate_events[0]
    seeded["holding-read-artifact"] = seeded["holding-read"]
    _register_model(
        bank.state,
        bank.objects,
        seeded["holding-read"],
        artifact_id=f"ExternalAccountEvent:{seeded['holding-read'].event_id}",
    )
    return repository, seeded


def _external_account_handler(
    bank: RecordedBank,
    repository: ExternalAccountRepository,
    seeded: dict[str, ExternalAccountEvent],
):
    receipts = ExternalAccountOperationReceiptService(bank.store, bank.objects)
    subjects = ResearchSubjectRegistryService(bank.store)

    def handle(
        request: InvestorRequestEnvelope,
        _preflight: object,
    ) -> CapabilityExecutionResult:
        scenario_id = int(request.metadata["scenario_id"])
        cutoff = request.evidence_cutoff
        if scenario_id == 12:
            event = seeded["holding-read"]
            return CapabilityExecutionResult(
                artifact_ids=(f"ExternalAccountEvent:{event.event_id}",),
                source_revision="recorded-economic-e2e-v1",
            )
        if scenario_id == 13:
            assertion = subjects.record_provisional_position(
                account_id="holding-provisional",
                lane=PortfolioLane.ACTUAL,
                instrument_id=f"XSHG:{COMPANY}",
                quantity=Decimal("1000"),
                asserted_at=datetime(2026, 5, 1, tzinfo=UTC),
                date_precision=DatePrecision.MONTH_ONLY,
                source_text=request.raw_text,
                idempotency_key="business-e2e-scenario-13-month-only",
            )
            artifact_id, _ = receipts.freeze(
                request_id=request.request_id,
                as_of=cutoff,
                status="PROVISIONAL",
                assertions=[assertion],
            )
            return CapabilityExecutionResult(
                artifact_ids=(artifact_id,), source_revision="recorded-economic-e2e-v1"
            )

        shanghai = ZoneInfo("Asia/Shanghai")
        local = request.question_time.astimezone(shanghai)
        date_anchor = datetime(local.year, local.month, local.day, tzinfo=shanghai)
        status = "RECORDED"
        duplicate_ids: list[str] = []
        events: list[ExternalAccountEvent]
        if scenario_id == 22:
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="trade-date",
                        event_type=ExternalAccountEventType.TRADE,
                        key="scenario-22-buy",
                        occurred_at=date_anchor - timedelta(days=1),
                        available_at=cutoff,
                        precision="DATE_ONLY",
                        market=Market.XSHG,
                        symbol=COMPANY,
                        side="BUY",
                        quantity=300,
                        price="12.35",
                        note="用户声明：昨天买入，成交时刻未知",
                    )
                ],
            )
        elif scenario_id == 23:
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="sell-date",
                        event_type=ExternalAccountEventType.TRADE,
                        key="scenario-23-sell",
                        occurred_at=date_anchor,
                        available_at=cutoff,
                        precision="DATE_ONLY",
                        market=Market.XSHG,
                        symbol=COMPANY,
                        side="SELL",
                        quantity=200,
                        price="15.82",
                        note="用户声明：今天卖出，成交时刻未知",
                    )
                ],
            )
        elif scenario_id == 24:
            original = seeded["correction-original"]
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="correction",
                        event_type=ExternalAccountEventType.TRADE,
                        key="scenario-24-replacement",
                        occurred_at=original.occurred_at,
                        available_at=cutoff,
                        precision=original.occurred_at_precision,
                        market=original.market,
                        symbol=original.symbol,
                        side=original.side,
                        quantity=600,
                        price=str(original.price_cny),
                        replaces_event_id=original.event_id,
                        note="用户更正原买入数量；保留旧事件",
                    )
                ],
            )
        elif scenario_id == 25:
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="multi-a",
                        event_type=ExternalAccountEventType.TRADE,
                        key="scenario-25-a",
                        occurred_at=cutoff - timedelta(days=1),
                        available_at=cutoff,
                        market=Market.XSHG,
                        symbol=COMPANY,
                        side="BUY",
                        quantity=100,
                        price="10",
                        note="A账户独立记录",
                    ),
                    _account_draft(
                        account_id="multi-b",
                        event_type=ExternalAccountEventType.TRADE,
                        key="scenario-25-b",
                        occurred_at=cutoff - timedelta(days=1),
                        available_at=cutoff,
                        market=Market.XSHG,
                        symbol=COMPANY,
                        side="BUY",
                        quantity=200,
                        price="10",
                        note="B账户独立记录",
                    ),
                ],
            )
        elif scenario_id == 26:
            correlation = "scenario-26-transfer-pair"
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="transfer-a",
                        event_type=ExternalAccountEventType.SECURITY_TRANSFER_OUT,
                        key=f"{correlation}-out",
                        occurred_at=cutoff,
                        available_at=cutoff,
                        market=Market.XSHG,
                        symbol=COMPANY,
                        quantity=1000,
                        note=f"correlation:{correlation}",
                    ),
                    _account_draft(
                        account_id="transfer-b",
                        event_type=ExternalAccountEventType.SECURITY_TRANSFER_IN,
                        key=f"{correlation}-in",
                        occurred_at=cutoff,
                        available_at=cutoff,
                        market=Market.XSHG,
                        symbol=COMPANY,
                        quantity=1000,
                        price="10",
                        note=f"correlation:{correlation}",
                    ),
                ],
            )
        elif scenario_id == 27:
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="cash-b",
                        event_type=ExternalAccountEventType.CASH_DEPOSIT,
                        key="scenario-27-deposit",
                        occurred_at=cutoff,
                        available_at=cutoff,
                        amount="50000",
                        note="用户声明向B账户转入现金",
                    )
                ],
            )
        elif scenario_id == 28:
            events, _ = _append_account_drafts(
                repository,
                [
                    _account_draft(
                        account_id="cash-a",
                        event_type=ExternalAccountEventType.CASH_WITHDRAWAL,
                        key="scenario-28-withdrawal",
                        occurred_at=cutoff,
                        available_at=cutoff,
                        amount="20000",
                        note="用户声明从账户转出现金",
                    )
                ],
            )
        elif scenario_id == 29:
            existing = seeded["duplicate"]
            inserted, duplicates = repository.append_events([existing])
            assert inserted == [] and duplicates == [existing.event_id]
            events = [existing]
            duplicate_ids = duplicates
            status = "NO_CHANGE"
        else:
            raise AssertionError(f"unexpected external-account scenario {scenario_id}")

        artifact_id, _ = receipts.freeze(
            request_id=request.request_id,
            as_of=cutoff,
            status=status,
            events=events,
            duplicate_event_ids=duplicate_ids,
        )
        return CapabilityExecutionResult(
            artifact_ids=(artifact_id,), source_revision="recorded-economic-e2e-v1"
        )

    return handle


def _build_economic_bank(root: Path) -> RecordedBank:
    bank = _build_recorded_bank(root)
    repository, seeded = _seed_external_accounts(bank)
    paper_service, ledger = _paper_service(bank.state, bank.objects)
    operation = _paper_request()
    paper_service.execute(operation, _paper_confirmation(operation))
    # Economic fixtures are created after the recorded research bank. Freeze the
    # request only after those account/ledger facts exist so PIT remains causal.
    question_time = datetime.now(UTC)
    nav = ledger.portfolio_nav("paper", as_of=question_time).model_copy(
        update={"created_at": question_time}
    )
    nav_id = _register_model(
        bank.state,
        bank.objects,
        nav,
        artifact_id="PortfolioNAV:business-e2e-paper-status",
    )
    paper_receipts = PaperPreparationReceiptService(bank.store, bank.objects)

    def paper_handler(
        request: InvestorRequestEnvelope,
        _preflight: object,
    ) -> CapabilityExecutionResult:
        scenario_id = int(request.metadata["scenario_id"])
        if scenario_id == 95:
            artifact_id, _ = paper_receipts.needs_info(
                request_id=request.request_id,
                account_id="paper",
                as_of=request.evidence_cutoff,
                instrument_id=f"XSHG:{COMPANY}",
                side="BUY",
                quantity=1000,
                missing_fields=["LIMIT_PRICE", "ORDER_TYPE"],
            )
        elif scenario_id == 96:
            artifact_id = nav_id
        else:
            raise AssertionError(f"unexpected paper scenario {scenario_id}")
        return CapabilityExecutionResult(
            artifact_ids=(artifact_id,), source_revision="recorded-paper-economic-e2e-v1"
        )

    handlers = dict(bank.handlers)
    handlers["EXTERNAL_ACCOUNT"] = _external_account_handler(bank, repository, seeded)
    handlers["PAPER"] = paper_handler
    return RecordedBank(bank.store, bank.state, bank.objects, question_time, handlers)


def _build_recorded_bank(root: Path) -> RecordedBank:
    store = InvestorOrchestrationStore(root / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(root / "objects" / "sha256")
    question_time = datetime.now(UTC) - timedelta(minutes=2)

    current_market_id, _ = _market_anchor(
        state,
        objects,
        at=question_time - timedelta(minutes=1),
        suffix="current",
    )
    _, valuation_anchor = _market_anchor(
        state,
        objects,
        at=INSTITUTIONAL_NOW,
        suffix="institutional",
    )

    regime_features = MarketRegimeFeatureSnapshot(
        feature_snapshot_id="business-e2e-regime-features",
        as_of=question_time - timedelta(minutes=1),
        trend_score=0.10,
        breadth_score=0.10,
        tail_risk_score=0.10,
        liquidity_score=0.10,
        valuation_fragility_score=0.05,
        earnings_diffusion_score=0.05,
        macro_credit_score=0.05,
        family_coverage={
            "trend": 1.0,
            "breadth": 1.0,
            "tail": 1.0,
            "liquidity": 1.0,
            "valuation": 1.0,
            "earnings": 1.0,
            "macro": 1.0,
        },
        source_revisions={"recorded-business-e2e": "v1"},
        ood_score=0.0,
    )
    MarketRegimeService(
        store,
        PROJECT_ROOT / "configs" / "market_regime_v2.yaml",
    ).infer(regime_features, persist=True)

    claim_id = "claim:business-e2e"
    evidence_id, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim_id,
        evidence_id="evidence:business-e2e",
        source_id="cninfo-disclosures:business-e2e",
    )
    research_artifacts, holding_review_id = _lifecycle_artifacts(
        state,
        objects,
        claim_id=claim_id,
        evidence_id=evidence_id,
    )
    institutional = InstitutionalResearchService(state, objects)
    bundle = institutional.finalize(
        _finalize_request(
            research_artifacts["FrozenEvidencePack"],
            claim_id,
            evidence_id,
            market_price_anchor=valuation_anchor,
        )
    )
    investment_thesis = _statement(
        claim_id,
        evidence_id,
        text="公司盈利改善依赖销量、价格与利润率按已核实假设兑现。",
    )
    variant_perception = _statement(
        claim_id,
        evidence_id,
        text="市场可能尚未充分反映经营效率改善对中期现金流的影响。",
    )
    competing_hypothesis = _statement(
        claim_id,
        evidence_id,
        text="若需求走弱或成本上升，利润改善可能低于当前研究假设。",
    )
    decision_context = institutional.build_decision_context(
        InstitutionalDecisionContextBuildRequest(
            company_id=COMPANY,
            as_of=INSTITUTIONAL_NOW,
            fundamental_model_bundle_artifact_id=f"FundamentalModelBundle:{bundle.bundle_id}",
            draft=InstitutionalDecisionContextDraft(
                decision_question="What must be true for this recorded E2E research case to work?",
                decision_horizon_end=date(2028, 12, 31),
                investment_thesis=investment_thesis,
                variant_perception=variant_perception,
                key_driver_ids=["margin", "price", "units"],
                competing_hypotheses=[competing_hypothesis],
                portfolio_context="Recorded integration fixture; sizing remains downstream.",
                created_at=INSTITUTIONAL_NOW,
            ),
            created_at=INSTITUTIONAL_NOW,
        )
    )
    company_research_id = f"InstitutionalDecisionContext:{decision_context.context_id}"
    financial_id = _financial_pack(state, objects)
    team = ResearchTeamService(project_root=PROJECT_ROOT, state=state, objects=objects)
    role_ids = _complete_company_team(
        team,
        company_id=COMPANY,
        evidence_id=evidence_id,
        financial_id=financial_id,
        fundamental_id=f"FundamentalModelBundle:{bundle.bundle_id}",
        industry_id=bundle.industry_profile_artifact_id,
        valuation_id=bundle.valuation_pack_artifact_id,
        current_market_id=current_market_id,
        decision_context_id=company_research_id,
    )
    portfolio_id = _portfolio_report(root, state, objects)
    committee_id = _committee_protocol(
        state,
        objects,
        research_artifacts=research_artifacts,
        financial_id=financial_id,
        evidence_id=evidence_id,
        as_of=question_time - timedelta(seconds=30),
    )
    full_market_id = _full_market_readiness(
        team,
        evidence_id=evidence_id,
        financial_id=financial_id,
        current_market_id=current_market_id,
        industry_id=bundle.industry_profile_artifact_id,
        fundamental_id=f"FundamentalModelBundle:{bundle.bundle_id}",
        company_research_id=company_research_id,
        event_research_id=role_ids["company-catalyst"],
        valuation_id=bundle.valuation_pack_artifact_id,
        red_team_id=role_ids["investment-red-team"],
        committee_id=committee_id,
        portfolio_id=portfolio_id,
    )
    etf_id = _recorded_etf_metrics(state, objects)

    artifact_by_capability = {
        "CURRENT_MARKET": current_market_id,
        "INDUSTRY": bundle.industry_profile_artifact_id,
        "COMPANY_RESEARCH": company_research_id,
        "FINANCIAL_INTEGRITY": financial_id,
        "GOVERNANCE": role_ids["governance-management-quality"],
        "EVENT_RESEARCH": role_ids["company-catalyst"],
        "FORECAST_VALUATION": bundle.valuation_pack_artifact_id,
        "RED_TEAM": role_ids["investment-red-team"],
        "COMMITTEE": committee_id,
        "PORTFOLIO": portfolio_id,
        "HOLDING_REVIEW": holding_review_id,
        "FULL_MARKET": full_market_id,
        "ETF": etf_id,
    }
    handlers: dict[str, CapabilityHandler] = {
        capability_id: (
            lambda _request, _preflight, artifact_id=artifact_id: CapabilityExecutionResult(
                artifact_ids=(artifact_id,),
                source_revision="recorded-business-e2e-v1",
            )
        )
        for capability_id, artifact_id in artifact_by_capability.items()
    }
    # Freeze the user request only after every recorded research artifact exists.
    # This keeps the fixture PIT-causal instead of creating evidence after the decision cutoff.
    question_time = datetime.now(UTC)
    return RecordedBank(store, state, objects, question_time, handlers)


@pytest.fixture(scope="module")
def recorded_bank(tmp_path_factory: pytest.TempPathFactory) -> RecordedBank:
    return _build_recorded_bank(tmp_path_factory.mktemp("business-scenarios-real-e2e"))


READ_SCENARIO_IDS = (
    1,
    2,
    3,
    11,
    14,
    *range(31, 39),
    *range(41, 49),
    *range(51, 58),
    *range(61, 68),
    *range(71, 78),
    *range(81, 91),
    *range(91, 95),
)


@pytest.mark.parametrize("scenario_id", READ_SCENARIO_IDS)
def test_recorded_read_scenarios_run_through_real_registered_domains(
    recorded_bank: RecordedBank,
    scenario_id: int,
) -> None:
    orchestration = InvestorOrchestrationService(recorded_bank.store)
    runner = ScenarioContractRunner.from_path(
        orchestration,
        PROJECT_ROOT / "configs" / "business_scenarios_v1.yaml",
    )
    scenario = runner.manifest.get(scenario_id)
    request = InvestorRequestEnvelope(
        request_id=f"business-e2e-{scenario_id}",
        question_time=recorded_bank.question_time,
        user_timezone="Asia/Shanghai",
        raw_text=scenario.title,
        normalized_intent=scenario.intent,
        side_effect=SideEffectClass.READ,
        entity_ids=(
            (COMPANY, "XSHG:510300") if scenario_id in {45, 91, 92, 93, 94} else (COMPANY,)
        ),
        idempotency_key=f"business-e2e-{scenario_id}",
    )
    result = runner.run(scenario_id, request, handlers=recorded_bank.handlers)
    assert result.failures == ()
    assert result.coverage_complete
    assert result.required_capability_coverage == 1.0
    assert result.prohibited_call_count == 0
    assert not result.answer.degraded


@pytest.fixture(scope="module")
def economic_bank(tmp_path_factory: pytest.TempPathFactory) -> RecordedBank:
    return _build_economic_bank(tmp_path_factory.mktemp("business-scenarios-economic-e2e"))


ECONOMIC_SIDE_EFFECTS = {
    12: SideEffectClass.READ,
    13: SideEffectClass.EA_PROVISIONAL,
    22: SideEffectClass.EA_WRITE,
    23: SideEffectClass.EA_WRITE,
    24: SideEffectClass.EA_WRITE,
    25: SideEffectClass.EA_WRITE,
    26: SideEffectClass.EA_WRITE,
    27: SideEffectClass.EA_WRITE,
    28: SideEffectClass.EA_WRITE,
    29: SideEffectClass.EA_WRITE,
    95: SideEffectClass.PT_PREPARE,
    96: SideEffectClass.READ,
}

ECONOMIC_ACCOUNTS = {
    12: "holding-read",
    13: "holding-provisional",
    22: "trade-date",
    23: "sell-date",
    24: "correction",
    27: "cash-b",
    28: "cash-a",
    29: "duplicate",
    95: "paper",
    96: "paper",
}


def _economic_table_snapshot(
    state: StateStore, *, paper: bool
) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        ("paper_account", "journal", "ledger_entry", "order_record", "fill", "position")
        if paper
        else ("external_account", "external_account_event")
    )
    with closing(state.connect()) as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
            for table in tables
        }


def _assert_economic_postcondition(
    bank: RecordedBank,
    runner: ScenarioContractRunner,
    scenario_id: int,
    request: InvestorRequestEnvelope,
) -> None:
    repository = ExternalAccountRepository(bank.state, bank.objects)
    if scenario_id == 12:
        projection = repository.projection("holding-read", as_of=bank.question_time)
        assert projection.positions[0].quantity == 1000
        return
    if scenario_id == 13:
        projection = repository.projection("holding-provisional", as_of=bank.question_time)
        assert projection.positions == []
        with closing(bank.state.connect()) as connection:
            row = connection.execute(
                "SELECT date_precision,payload_json FROM provisional_position_assertions "
                "WHERE account_id='holding-provisional'"
            ).fetchone()
        assert row is not None and row["date_precision"] == "MONTH_ONLY"
        assert "2026-05" in row["payload_json"]
        return
    if scenario_id == 22:
        projection = repository.projection("trade-date", as_of=bank.question_time)
        assert projection.positions[0].quantity == 300
        assert projection.positions[0].average_cost_cny == Decimal("12.35")
        event = next(
            item
            for item in repository.list_events("trade-date")
            if item.idempotency_key == "scenario-22-buy"
        )
        assert event.occurred_at_precision == "DATE_ONLY"
        return
    if scenario_id == 23:
        projection = repository.projection("sell-date", as_of=bank.question_time)
        assert projection.positions[0].quantity == 300
        assert projection.cash_known and projection.cash_cny == Decimal("98164")
        event = next(
            item
            for item in repository.list_events("sell-date")
            if item.idempotency_key == "scenario-23-sell"
        )
        assert event.occurred_at_precision == "DATE_ONLY"
        return
    if scenario_id == 24:
        projection = repository.projection("correction", as_of=bank.question_time)
        assert projection.positions[0].quantity == 600
        events = repository.list_events("correction")
        replacement = next(
            item for item in events if item.idempotency_key == "scenario-24-replacement"
        )
        assert replacement.replaces_event_id is not None
        assert len(events) == 3
        return
    if scenario_id == 25:
        first = repository.projection("multi-a", as_of=bank.question_time)
        second = repository.projection("multi-b", as_of=bank.question_time)
        assert first.positions[0].quantity == 100
        assert second.positions[0].quantity == 200
        assert first.account_id != second.account_id
        return
    if scenario_id == 26:
        first = repository.projection("transfer-a", as_of=bank.question_time)
        second = repository.projection("transfer-b", as_of=bank.question_time)
        assert first.positions[0].quantity == 500
        assert second.positions[0].quantity == 1000
        assert first.positions[0].quantity + second.positions[0].quantity == 1500
        assert second.positions[0].average_cost_cny == Decimal("10")
        return
    if scenario_id == 27:
        projection = repository.projection("cash-b", as_of=bank.question_time)
        assert projection.cash_known and projection.cash_cny == Decimal("150000")
        return
    if scenario_id == 28:
        projection = repository.projection("cash-a", as_of=bank.question_time)
        assert projection.cash_known and projection.cash_cny == Decimal("80000")
        return
    if scenario_id == 29:
        projection = repository.projection("duplicate", as_of=bank.question_time)
        assert projection.positions[0].quantity == 100
        assert projection.total_event_count == 2
        return
    if scenario_id == 95:
        with closing(bank.state.connect()) as connection:
            assert connection.execute("SELECT COUNT(*) FROM order_record").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0
        repeated = runner.run(
            scenario_id,
            request.model_copy(
                update={
                    "request_id": "business-e2e-95-repeat",
                    "idempotency_key": "business-e2e-95-repeat",
                }
            ),
            handlers=bank.handlers,
        )
        assert repeated.failures == ()
        assert "尚未形成订单" in repeated.answer.conclusion
        with closing(bank.state.connect()) as connection:
            assert connection.execute("SELECT COUNT(*) FROM order_record").fetchone()[0] == 1
        return
    if scenario_id == 96:
        with closing(bank.state.connect()) as connection:
            before = tuple(
                connection.execute(
                    "SELECT order_id,status,filled_qty FROM order_record ORDER BY order_id"
                )
            )
            assert connection.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0
        repeated = runner.run(
            scenario_id,
            request.model_copy(
                update={
                    "request_id": "business-e2e-96-repeat",
                    "idempotency_key": "business-e2e-96-repeat",
                }
            ),
            handlers=bank.handlers,
        )
        assert repeated.failures == ()
        with closing(bank.state.connect()) as connection:
            after = tuple(
                connection.execute(
                    "SELECT order_id,status,filled_qty FROM order_record ORDER BY order_id"
                )
            )
            assert connection.execute("SELECT COUNT(*) FROM fill").fetchone()[0] == 0
        assert before == after
        return
    raise AssertionError(f"missing economic postcondition for scenario {scenario_id}")


@pytest.mark.parametrize("scenario_id", tuple(ECONOMIC_SIDE_EFFECTS))
def test_recorded_economic_scenarios_preserve_lane_and_operation_contracts(
    economic_bank: RecordedBank,
    scenario_id: int,
) -> None:
    orchestration = InvestorOrchestrationService(economic_bank.store)
    runner = ScenarioContractRunner.from_path(
        orchestration,
        PROJECT_ROOT / "configs" / "business_scenarios_v1.yaml",
    )
    scenario = runner.manifest.get(scenario_id)
    request = InvestorRequestEnvelope(
        request_id=f"business-e2e-{scenario_id}",
        question_time=economic_bank.question_time,
        user_timezone="Asia/Shanghai",
        raw_text=scenario.title,
        normalized_intent=scenario.intent,
        side_effect=ECONOMIC_SIDE_EFFECTS[scenario_id],
        entity_ids=(COMPANY,),
        account_id=ECONOMIC_ACCOUNTS.get(scenario_id),
        idempotency_key=f"business-e2e-{scenario_id}",
        metadata={"scenario_id": scenario_id},
    )
    opposite_before = _economic_table_snapshot(
        economic_bank.state,
        paper=scenario_id not in {95, 96},
    )
    result = runner.run(scenario_id, request, handlers=economic_bank.handlers)
    opposite_after = _economic_table_snapshot(
        economic_bank.state,
        paper=scenario_id not in {95, 96},
    )

    assert result.failures == ()
    assert result.coverage_complete
    assert result.required_capability_coverage == 1.0
    assert result.prohibited_call_count == 0
    assert result.economic_side_effect_allowed
    assert not result.answer.degraded
    assert opposite_before == opposite_after
    _assert_economic_postcondition(economic_bank, runner, scenario_id, request)


def test_business_scenario_partition_is_exactly_68_unique_ids(
    economic_bank: RecordedBank,
) -> None:
    covered = (*READ_SCENARIO_IDS, *ECONOMIC_SIDE_EFFECTS)
    runner = ScenarioContractRunner.from_path(
        InvestorOrchestrationService(economic_bank.store),
        PROJECT_ROOT / "configs" / "business_scenarios_v1.yaml",
    )
    assert len(covered) == 68
    assert len(set(covered)) == 68
    assert set(covered) == set(runner.ids())


def test_scenario_96_confirmed_recovery_and_publication_variant(tmp_path: Path) -> None:
    """Keep the existing READ case and add real recovery before the frozen public query."""
    from scripts.benchmark_investor_preflight import economic_digest
    from tests.unit.test_investor_replay_result import make_replay_case, publish_replay

    case = make_replay_case(tmp_path)
    recovered = economic_digest(case.state)
    result = publish_replay(case, case.report)
    assert result.scenario_id == 96 and result.failures == ()
    assert result.required_capability_coverage == 1.0
    assert result.prohibited_call_count == 0 and not result.answer.degraded
    assert "模拟成交" in result.answer.conclusion
    assert case.ledger.get_order(case.original_order_id).filled_qty == 100
    assert case.ledger.status("paper")["positions"][0]["qty_total"] == 100
    assert economic_digest(case.state) == recovered
    repeated = case.engine.replay(
        account_id="paper",
        request=case.market_request,
        requested_cursor=case.report.requested_cursor,
        fee_schedule=case.fees,
    )
    assert repeated.fill_ids == [] and repeated.processed_bars == 0
    repeated_answer = publish_replay(case, repeated)
    assert repeated_answer.failures == () and not repeated_answer.answer.degraded
    assert "未新增" in repeated_answer.answer.conclusion
    assert economic_digest(case.state) == recovered
