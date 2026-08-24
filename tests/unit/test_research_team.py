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
from astock.schemas.research_team import (
    RecommendationReadinessRequest,
    RecommendationReadinessStatus,
    ResearchCoverageRequest,
    ResearchExecutionBackend,
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTaskRole,
    ResearchTeamTaskState,
)

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
