from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.industry_archetypes import IndustryResearchRegistry
from astock.research.team import (
    ResearchTeamService,
    detect_hardware_budget,
    load_research_team_policy,
)
from astock.schemas.financial import (
    FinancialCoverageStatus,
    FinancialEvidenceGap,
    FinancialFieldCode,
    FinancialGapType,
    FinancialIndustryProfile,
    FinancialIntegrityEvidencePack,
    FinancialRiskLevel,
)
from astock.schemas.market import Market
from astock.schemas.research_acquisition import (
    AcquisitionCapability,
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    ExternalAuthority,
    ExternalResearchNeed,
)
from astock.schemas.research_seeds import (
    ResearchSeedReport,
    ResearchSeedStatus,
    ResearchUniverseCoverageStatus,
)
from astock.schemas.research_team import (
    RecommendationReadinessRequest,
    RecommendationReadinessStatus,
    ResearchCoverageRequest,
    ResearchExecutionBackend,
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTaskRole,
    ResearchTeamScope,
    ResearchTeamTaskState,
)
from astock.schemas.runs import RunStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 5, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> tuple[ResearchTeamService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return (
        ResearchTeamService(project_root=PROJECT_ROOT, state=state, objects=objects),
        state,
        objects,
    )


def _register_output(state: StateStore, objects: ObjectStore, artifact_id: str) -> None:
    ref = objects.put_json({"artifact_id": artifact_id, "test": True})
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="TestResearchOutput",
        schema_version="1.0",
        object_hash=ref.sha256,
        input_hashes=[],
    )


def _register_seed_report(
    state: StateStore,
    objects: ObjectStore,
    *,
    artifact_id: str,
    full: bool,
) -> None:
    ratios = (
        {Market.XSHG: 1.0, Market.XSHE: 1.0, Market.BJSE: 1.0}
        if full
        else {Market.XSHG: 0.99, Market.XSHE: 1.0, Market.BJSE: 1.0}
    )
    report = ResearchSeedReport(
        report_id=artifact_id.removeprefix("ResearchSeedReport:"),
        as_of=NOW,
        data_cutoff_at=NOW,
        status=(ResearchSeedStatus.EMPTY if full else ResearchSeedStatus.NEEDS_INFO),
        profiles=[],
        seeds=[],
        source_snapshot_ids=[],
        source_object_hashes=[],
        warning_codes=[],
        market_coverage_ratios=ratios,
        universe_coverage_status=(
            ResearchUniverseCoverageStatus.FULL if full else ResearchUniverseCoverageStatus.PARTIAL
        ),
        formal_full_market_coverage_allowed=full,
        market_seed_count=0,
        expert_seed_count=0,
        existing_candidate_seed_count=0,
        created_at=NOW,
    )
    ref = objects.put_json(report.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="ResearchSeedReport",
        schema_version=report.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )


def _register_financial_pack(
    state: StateStore,
    objects: ObjectStore,
    *,
    artifact_id: str,
    complete: bool,
) -> None:
    gaps = []
    if not complete:
        gaps = [
            FinancialEvidenceGap(
                gap_id="gap:partial",
                gap_type=FinancialGapType.MISSING_FACT,
                detail_code="FORMAL_FINANCIAL_COMPLETENESS_REQUIRED",
                period_end=None,
                field_codes=[FinancialFieldCode.NET_PROFIT_CASH_FLOW],
                related_rule_ids=[],
            )
        ]
    pack = FinancialIntegrityEvidencePack(
        audit_run_id=artifact_id.removeprefix("FinancialIntegrityEvidencePack:"),
        request_hash="f" * 64,
        status=RunStatus.SUCCEEDED if complete else RunStatus.NEEDS_INFO,
        coverage_status=(
            FinancialCoverageStatus.COMPLETE if complete else FinancialCoverageStatus.PARTIAL
        ),
        company_id="600000",
        as_of=NOW,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        periods=[],
        input_fact_ids=[],
        source_snapshot_ids=[],
        pit_ids=[],
        verified_numbers=[],
        recalculated_metrics=[],
        rule_findings=[],
        evidence_gaps=gaps,
        risk_level=FinancialRiskLevel.LOW,
        rule_versions={},
        model_versions={},
        capability_status={},
        created_at=NOW,
    )
    ref = objects.put_json(pack.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="FinancialIntegrityEvidencePack",
        schema_version=pack.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )


def _register_acquisition_report(
    state: StateStore,
    objects: ObjectStore,
    *,
    company_id: str = "600000",
    ready: bool = True,
) -> tuple[str, str]:
    report_id = f"current-research-acquisition:test:{company_id}:{'ready' if ready else 'gapped'}"
    needs = []
    if not ready:
        needs = [
            ExternalResearchNeed(
                capability=AcquisitionCapability.FINANCIAL_ANNUAL,
                research_question="Find the issuer annual report from an official source.",
                preferred_authorities=[ExternalAuthority.ISSUER_IR],
            )
        ]
    report = CurrentResearchAcquisitionReport(
        report_id=report_id,
        company_id=company_id,
        market=Market.XSHG,
        started_at=NOW,
        decision_as_of=NOW,
        status=(
            CurrentResearchAcquisitionStatus.READY
            if ready
            else CurrentResearchAcquisitionStatus.NEEDS_EXTERNAL_RESEARCH
        ),
        attempts=[],
        external_research_needs=needs,
        created_at=NOW,
    )
    ref = objects.put_json(report.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=report_id,
        artifact_type="CurrentResearchAcquisitionReport",
        schema_version=report.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return report_id, ref.sha256


def _register_role_output(
    service: ResearchTeamService,
    state: StateStore,
    objects: ObjectStore,
    *,
    plan_id: str,
    task_id: str,
    readiness_check_results: dict[str, bool] | None = None,
) -> str:
    plan = service.get_plan(plan_id)
    assert plan is not None
    task = next(item for item in plan.tasks if item.task_id == task_id)
    if task.role is ResearchTaskRole.UNIVERSE:
        universe_full = (
            readiness_check_results.get("UNIVERSE_COVERAGE", True)
            if readiness_check_results is not None
            else True
        )
        member_artifact_id = f"ResearchSeedReport:test-member:{plan_id}:{task_id}"
        _register_seed_report(
            state,
            objects,
            artifact_id=member_artifact_id,
            full=universe_full,
        )
    elif task.role is ResearchTaskRole.FINANCIAL_INTEGRITY:
        financial_complete = (
            readiness_check_results.get("FINANCIAL_INTEGRITY", True)
            if readiness_check_results is not None
            else True
        )
        member_artifact_id = f"FinancialIntegrityEvidencePack:test-member:{plan_id}:{task_id}"
        _register_financial_pack(
            state,
            objects,
            artifact_id=member_artifact_id,
            complete=financial_complete,
        )
    else:
        member_artifact_id = f"test-member:{plan_id}:{task_id}"
        _register_output(state, objects, member_artifact_id)
    output_result = service.register_role_output(
        ResearchRoleOutput(
            plan_id=plan_id,
            task_id=task_id,
            output_contract=task.output_contract,
            member_artifact_ids=[member_artifact_id],
            evidence_ids=[],
            readiness_check_results=(
                readiness_check_results
                if readiness_check_results is not None
                else {check: True for check in task.readiness_checks}
            ),
            summary=f"completed {task_id}",
            created_at=NOW,
        )
    )
    return str(output_result["artifact_id"])


def _complete_task(
    service: ResearchTeamService,
    state: StateStore,
    objects: ObjectStore,
    *,
    plan_id: str,
    task_id: str,
    context_id: str | None = None,
    readiness_check_results: dict[str, bool] | None = None,
) -> None:
    output_artifact_id = _register_role_output(
        service,
        state,
        objects,
        plan_id=plan_id,
        task_id=task_id,
        readiness_check_results=readiness_check_results,
    )
    service.register_role_result(
        ResearchRoleResult(
            plan_id=plan_id,
            task_id=task_id,
            state=ResearchTeamTaskState.COMPLETE,
            independent_context_id=context_id or f"context:{task_id}",
            output_artifact_ids=[output_artifact_id],
            evidence_ids=[],
            created_at=NOW,
        )
    )


def test_research_team_cli_is_discoverable() -> None:
    command_names = {command.name for command in app.registered_commands if command.name}
    assert {
        "research-runtime-profile",
        "research-team-schema",
        "industry-research-archetypes",
        "industry-research-resolve",
        "research-team-plan",
        "research-team-company-plan",
        "research-coverage-score",
        "research-team-status",
        "research-team-role-output",
        "research-team-task-result",
        "research-recommendation-readiness",
    } <= command_names


def test_low_resource_profile_is_laptop_safe() -> None:
    policy = load_research_team_policy(PROJECT_ROOT / "configs" / "research_team.yaml")
    budget = detect_hardware_budget(policy, cpu_count=4, memory_gib=8)

    assert budget.resource_class.value == "LOW_RESOURCE"
    assert budget.provider_workers == 2
    assert budget.agent_workers == 2
    assert budget.duckdb_threads == 2
    assert budget.max_parallel_companies == 2
    assert not budget.background_service_required
    assert not budget.gpu_required


def test_policy_is_on_demand_fail_closed_and_retires_skill_share_gate() -> None:
    policy = load_research_team_policy(PROJECT_ROOT / "configs" / "research_team.yaml")

    assert policy.default_backend is ResearchExecutionBackend.CHAT_ORCHESTRATED
    assert policy.on_demand_only
    assert not policy.background_service_required
    assert not policy.manual_candidate_fallback_allowed
    assert not policy.broker_execution_allowed
    assert not policy.skill_share_gate_enabled
    assert policy.reserve_blind_market_tranche
    assert policy.expert_overlay_max_priority_bonus <= 0.15
    assert "TEAM_DAG_COMPLETE" in policy.required_checks


def test_full_market_plan_has_team_roles_and_hard_order(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW, cpu_count=4, memory_gib=8)
    by_id = {item.task_id: item for item in plan.tasks}

    assert plan.backend is ResearchExecutionBackend.CHAT_ORCHESTRATED
    assert plan.on_demand_acquisition
    assert plan.no_manual_candidate_fallback
    assert not plan.formal_recommendation_allowed
    assert by_id["macro-regime"].stage == by_id["policy-regime"].stage
    assert by_id["bull-case"].stage == by_id["bear-case"].stage
    assert by_id["bull-case"].independent_context_required
    assert by_id["bear-case"].independent_context_required
    assert set(by_id["independent-review"].dependencies) == {"bear-case", "bull-case"}
    assert by_id["committee"].dependencies == ["independent-review"]
    assert by_id["portfolio-construction"].dependencies == ["committee"]
    assert by_id["recommendation-gate"].role is ResearchTaskRole.RECOMMENDATION_GATE
    assert not by_id["recommendation-gate"].required_for_recommendation


def test_company_plan_requires_resolved_acquisition_and_binds_lineage(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    gapped_artifact_id, _ = _register_acquisition_report(state, objects, ready=False)

    with pytest.raises(ValueError, match="acquisition gaps are resolved"):
        service.create_company_plan(
            company_id="600000",
            acquisition_report_artifact_id=gapped_artifact_id,
            as_of=NOW,
        )

    report_artifact_id, report_hash = _register_acquisition_report(state, objects, ready=True)
    plan = service.create_company_plan(
        company_id="600000",
        acquisition_report_artifact_id=report_artifact_id,
        as_of=NOW,
        cpu_count=4,
        memory_gib=8,
    )
    by_id = {item.task_id: item for item in plan.tasks}

    assert plan.scope is ResearchTeamScope.COMPANY
    assert plan.company_id == "600000"
    assert plan.acquisition_report_artifact_id == report_artifact_id
    assert by_id["governance-management-quality"].role is ResearchTaskRole.GOVERNANCE
    assert by_id["model-risk-validation"].role is ResearchTaskRole.MODEL_RISK
    assert set(by_id["committee"].dependencies) == {
        "investment-red-team",
        "model-risk-validation",
    }
    mapped_checks = {check for task in plan.tasks for check in task.readiness_checks}
    assert mapped_checks == set(service.policy.company_required_checks) - {"TEAM_DAG_COMPLETE"}

    record = state.artifact_record(f"ResearchTeamPlan:{plan.plan_id}")
    assert record is not None
    assert report_hash in record["input_hashes"]


def test_same_as_of_and_hardware_produce_same_plan_identity(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    first = service.create_full_market_plan(as_of=NOW, cpu_count=4, memory_gib=8)
    second = service.create_full_market_plan(as_of=NOW, cpu_count=4, memory_gib=8)

    assert first.plan_id == second.plan_id
    assert [item.created_at for item in first.tasks] == [NOW] * len(first.tasks)


def test_task_completion_rejects_missing_dependencies_and_fake_outputs(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)

    with pytest.raises(ValueError, match="ResearchRoleOutput"):
        service.register_role_result(
            ResearchRoleResult(
                plan_id=plan.plan_id,
                task_id="cio-intent",
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id="cio-context",
                output_artifact_ids=["does-not-exist"],
                created_at=NOW,
            )
        )

    cio_task = next(item for item in plan.tasks if item.task_id == "cio-intent")
    with pytest.raises(ValueError, match="registered member artifact"):
        service.register_role_output(
            ResearchRoleOutput(
                plan_id=plan.plan_id,
                task_id="cio-intent",
                output_contract=cio_task.output_contract,
                member_artifact_ids=[],
                readiness_check_results={},
                summary="unsupported self-attestation",
                created_at=NOW,
            )
        )

    macro_artifact = _register_role_output(
        service,
        state,
        objects,
        plan_id=plan.plan_id,
        task_id="macro-regime",
    )
    with pytest.raises(ValueError, match="dependencies"):
        service.register_role_result(
            ResearchRoleResult(
                plan_id=plan.plan_id,
                task_id="macro-regime",
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id="macro-context",
                output_artifact_ids=[macro_artifact],
                created_at=NOW,
            )
        )


def test_readiness_fails_closed_even_when_checks_claim_pass_without_team(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)
    claimed = {check: True for check in service.policy.required_checks}

    report = service.evaluate_readiness(
        RecommendationReadinessRequest(plan_id=plan.plan_id, checks=claimed, created_at=NOW)
    )

    assert report.status is RecommendationReadinessStatus.OBSERVATION_ONLY
    assert not report.formal_recommendation_allowed
    assert "TEAM_DAG_COMPLETE" in report.missing_or_failed_checks


def test_universe_role_cannot_self_attest_with_arbitrary_member_artifact(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)
    task = next(item for item in plan.tasks if item.task_id == "universe-acquisition")
    artifact_id = f"test-member:{plan.plan_id}:universe-acquisition"
    _register_output(state, objects, artifact_id)

    with pytest.raises(ValueError, match="UNIVERSE_COVERAGE must be derived"):
        service.register_role_output(
            ResearchRoleOutput(
                plan_id=plan.plan_id,
                task_id=task.task_id,
                output_contract=task.output_contract,
                member_artifact_ids=[artifact_id],
                readiness_check_results={"UNIVERSE_COVERAGE": True},
                summary="self-attested universe",
                created_at=NOW,
            )
        )


def test_partial_universe_cannot_gain_formal_recommendation_authority(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)

    for task in plan.tasks:
        if task.task_id == "recommendation-gate":
            continue
        context_id = None
        if task.task_id == "bull-case":
            context_id = "bull-independent"
        elif task.task_id == "bear-case":
            context_id = "bear-independent"
        readiness = {"UNIVERSE_COVERAGE": False} if task.task_id == "universe-acquisition" else None
        _complete_task(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            context_id=context_id,
            readiness_check_results=readiness,
        )

    claimed = {check: True for check in service.policy.required_checks}
    report = service.evaluate_readiness(
        RecommendationReadinessRequest(plan_id=plan.plan_id, checks=claimed, created_at=NOW)
    )

    assert report.status is RecommendationReadinessStatus.OBSERVATION_ONLY
    assert not report.formal_recommendation_allowed
    assert "UNIVERSE_COVERAGE" in report.missing_or_failed_checks
    assert "TEAM_DAG_COMPLETE" in report.passed_checks


def test_partial_financial_pack_cannot_open_precise_valuation_or_recommendation(
    tmp_path: Path,
) -> None:
    service, state, objects = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)

    for task in plan.tasks:
        if task.task_id == "valuation":
            break
        if task.task_id == "recommendation-gate":
            continue
        readiness = (
            {"FINANCIAL_INTEGRITY": False}
            if task.task_id == "company-financial-integrity"
            else None
        )
        _complete_task(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            readiness_check_results=readiness,
        )

    status = service.status(plan.plan_id)
    ready_tasks = status["ready_tasks"]
    assert isinstance(ready_tasks, list)
    assert "valuation" in ready_tasks
    with pytest.raises(
        ValueError,
        match="precise VALUATION requires COMPLETE SUCCEEDED financial packs",
    ):
        _register_role_output(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id="valuation",
            readiness_check_results={"VALUATION": True},
        )

    valuation_artifact_id = _register_role_output(
        service,
        state,
        objects,
        plan_id=plan.plan_id,
        task_id="valuation",
        readiness_check_results={"VALUATION": False},
    )
    service.register_role_result(
        ResearchRoleResult(
            plan_id=plan.plan_id,
            task_id="valuation",
            state=ResearchTeamTaskState.COMPLETE,
            independent_context_id="observation-only-valuation",
            output_artifact_ids=[valuation_artifact_id],
            evidence_ids=[],
            created_at=NOW,
        )
    )

    after_valuation = False
    for task in plan.tasks:
        if task.task_id == "valuation":
            after_valuation = True
            continue
        if not after_valuation or task.task_id == "recommendation-gate":
            continue
        context_id = None
        if task.task_id == "bull-case":
            context_id = "bull-independent"
        elif task.task_id == "bear-case":
            context_id = "bear-independent"
        _complete_task(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            context_id=context_id,
        )

    claimed = {check: True for check in service.policy.required_checks}
    report = service.evaluate_readiness(
        RecommendationReadinessRequest(plan_id=plan.plan_id, checks=claimed, created_at=NOW)
    )

    assert report.status is RecommendationReadinessStatus.OBSERVATION_ONLY
    assert not report.formal_recommendation_allowed
    assert report.missing_or_failed_checks == ["FINANCIAL_INTEGRITY", "VALUATION"]
    assert "TEAM_DAG_COMPLETE" in report.passed_checks


def test_bull_bear_independence_and_complete_gate(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)

    for task in plan.tasks:
        if task.task_id in {"bear-case", "recommendation-gate"}:
            continue
        if task.task_id == "independent-review":
            break
        context = "debate-context" if task.task_id == "bull-case" else None
        _complete_task(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            context_id=context,
        )

    artifact_id = _register_role_output(
        service,
        state,
        objects,
        plan_id=plan.plan_id,
        task_id="bear-case",
    )
    with pytest.raises(ValueError, match="different independent_context_id"):
        service.register_role_result(
            ResearchRoleResult(
                plan_id=plan.plan_id,
                task_id="bear-case",
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id="debate-context",
                output_artifact_ids=[artifact_id],
                created_at=NOW,
            )
        )

    _complete_task(
        service,
        state,
        objects,
        plan_id=plan.plan_id,
        task_id="bear-case",
        context_id="bear-independent-context",
    )
    for task_id in ["independent-review", "committee", "portfolio-construction"]:
        _complete_task(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task_id,
        )

    checks = {check: True for check in service.policy.required_checks}
    report = service.evaluate_readiness(
        RecommendationReadinessRequest(plan_id=plan.plan_id, checks=checks, created_at=NOW)
    )

    assert report.status is RecommendationReadinessStatus.READY
    assert report.formal_recommendation_allowed
    assert not report.missing_or_failed_checks
    assert service.status(plan.plan_id)["status"] == "COMPLETE"


def test_request_true_cannot_uplift_a_failed_role_readiness_check(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    plan = service.create_full_market_plan(as_of=NOW)

    for task in plan.tasks:
        if task.task_id == "recommendation-gate":
            continue
        context_id = None
        if task.task_id == "bull-case":
            context_id = "bull-independent"
        elif task.task_id == "bear-case":
            context_id = "bear-independent"
        readiness = {"VALUATION": False} if task.task_id == "valuation" else None
        _complete_task(
            service,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            context_id=context_id,
            readiness_check_results=readiness,
        )

    claimed = {check: True for check in service.policy.required_checks}
    report = service.evaluate_readiness(
        RecommendationReadinessRequest(plan_id=plan.plan_id, checks=claimed, created_at=NOW)
    )

    assert report.status is RecommendationReadinessStatus.OBSERVATION_ONLY
    assert not report.formal_recommendation_allowed
    assert "VALUATION" in report.missing_or_failed_checks
    assert "TEAM_DAG_COMPLETE" in report.passed_checks


def test_industry_archetype_registry_is_broad_but_not_fake_certified_taxonomy() -> None:
    registry = IndustryResearchRegistry.load(
        PROJECT_ROOT / "configs" / "industry_research_archetypes.yaml"
    )

    inventory = registry.inventory()
    archetype_count = inventory["archetype_count"]
    assert isinstance(archetype_count, int)
    assert archetype_count >= 18
    assert inventory["taxonomy_kind"] == "INTERNAL_RESEARCH_ARCHETYPE"
    assert not inventory["certified_external_taxonomy"]
    assert not inventory["private_skill_required_for_analysis"]
    assert registry.resolve("光通信设备").archetype.archetype_id == "COMMUNICATIONS"  # type: ignore[union-attr]
    assert registry.resolve("创新药研发").archetype.archetype_id == "BIOTECH"  # type: ignore[union-attr]
    assert registry.resolve("无法可靠归类的新行业").status == "UNCLASSIFIED"


def test_research_coverage_separates_private_edge_from_core_readiness(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    report = service.evaluate_coverage(
        ResearchCoverageRequest(
            company_id="300308",
            universal_required_ids=["business", "forecast", "valuation"],
            universal_completed_ids=["business", "forecast", "valuation"],
            industry_required_ids=["competition", "kpi"],
            industry_completed_ids=["competition", "kpi"],
            private_skill_available_ids=[],
            private_skill_matched_ids=[],
            evidence_required_ids=["annual", "interim"],
            evidence_satisfied_ids=["annual", "interim"],
            created_at=NOW,
        )
    )

    assert report.core_coverage_pass
    assert report.score.private_skill_coverage == 0
    assert report.score.private_skill_is_edge_only
    assert not report.private_skill_gates_recommendation
