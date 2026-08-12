from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.config import load_research_skill_registry
from astock.research.production import ResearchProductionService
from astock.schemas.base import AStockModel
from astock.schemas.research import ResearchSkillKind
from astock.schemas.research_production import (
    CatalystKPIRule,
    CatalystMonitorRequest,
    CatalystRecordRequest,
    KPIComparison,
    OrdinalResearchLevel,
    ProductionSkillRole,
    ResearchModule,
    ResearchNeedVector,
    ResearchPriorityBucket,
    ResearchProductionRouteNeedsInfo,
    SkillLifecycleRecommendation,
    ThesisKPIObservation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


class _DummyBaseCase(AStockModel):
    company_id: str
    specialist_tags: list[str]


def _service(tmp_path: Path) -> tuple[ResearchProductionService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    registry = load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml")
    return ResearchProductionService(state, objects, registry), state, objects


def _need(**overrides: object) -> ResearchNeedVector:
    values: dict[str, object] = {
        "need_id": "need:phase12",
        "base_case_id": "base-case:not-required-for-scheduler",
        "company_id": "600001",
        "thesis_tags": ["growth"],
        "industry_tags": ["technology"],
        "event_tags": ["earnings"],
        "ontology_terms": ["growth"],
        "horizon": "medium_term",
        "available_inputs": [],
        "available_frequencies": [],
        "materiality": OrdinalResearchLevel.MEDIUM,
        "novelty": OrdinalResearchLevel.MEDIUM,
        "uncertainty": OrdinalResearchLevel.MEDIUM,
        "portfolio_relevance": OrdinalResearchLevel.MEDIUM,
        "catalyst_urgency": OrdinalResearchLevel.MEDIUM,
        "data_availability": OrdinalResearchLevel.MEDIUM,
        "source_diversity": OrdinalResearchLevel.MEDIUM,
        "estimated_research_cost": OrdinalResearchLevel.MEDIUM,
        "created_at": NOW,
    }
    values.update(overrides)
    return ResearchNeedVector.model_validate(values)


def test_phase12_policy_migrates_skill_roles_without_rewriting_legacy_registry(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    capabilities = {item.kind: item for item in service.capability_vectors()}

    assert (
        capabilities[ResearchSkillKind.GROWTH_PROBABILITY].role
        is ProductionSkillRole.SHARED_HYPOTHESIS
    )
    assert (
        capabilities[ResearchSkillKind.GROWTH_VALUATION].role
        is ProductionSkillRole.CANONICAL_VALUATION
    )
    assert (
        capabilities[ResearchSkillKind.DAILY_TREND_HEALTH].role
        is ProductionSkillRole.MARKET_TRADE_CONTEXT
    )
    assert (
        capabilities[ResearchSkillKind.HOURLY_SWING].role
        is ProductionSkillRole.MARKET_TRADE_CONTEXT
    )
    assert capabilities[ResearchSkillKind.RESEARCH_MEMO].role is ProductionSkillRole.COMPOSER
    assert not service.policy.automatic_skill_modification_allowed
    assert not service.policy.online_weight_learning_allowed


def test_phase12_scheduler_uses_ordinal_priority_and_bounded_dynamic_budget(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    normal = service.schedule(_need())
    urgent = service.schedule(
        _need(
            need_id="need:urgent",
            materiality=OrdinalResearchLevel.CRITICAL,
            novelty=OrdinalResearchLevel.CRITICAL,
            uncertainty=OrdinalResearchLevel.CRITICAL,
            portfolio_relevance=OrdinalResearchLevel.CRITICAL,
            catalyst_urgency=OrdinalResearchLevel.CRITICAL,
            data_availability=OrdinalResearchLevel.CRITICAL,
            source_diversity=OrdinalResearchLevel.CRITICAL,
            estimated_research_cost=OrdinalResearchLevel.HIGH,
        )
    )

    assert normal.priority_bucket is ResearchPriorityBucket.STANDARD
    assert normal.specialist_budget == 2
    assert urgent.priority_bucket is ResearchPriorityBucket.URGENT
    assert urgent.specialist_budget == 4
    assert not urgent.fake_monetary_value_assigned
    assert not urgent.fake_probability_assigned


def test_phase12_missing_base_case_is_structured_needs_info_without_policy_write(
    tmp_path: Path,
) -> None:
    service, state, _ = _service(tmp_path)

    result = service.route_for_user(_need())

    assert isinstance(result, ResearchProductionRouteNeedsInfo)
    assert result.status == "NEEDS_INFO"
    assert result.requested_artifact_type == "BaseCasePack"
    assert result.requested_base_case_id == "base-case:not-required-for-scheduler"
    assert result.requested_artifact_id == ("BaseCasePack:base-case:not-required-for-scheduler")
    assert result.requested_object_hash is None
    assert result.available_base_case_id is None
    assert result.available_base_case_object_hash is None
    assert result.finding_codes == ["BASE_CASE_NOT_REGISTERED"]
    assert result.required_action_codes == ["REGISTER_MATCHING_BASE_CASE"]
    assert not result.paper_ledger_write_allowed
    assert not result.broker_execution_allowed
    with state.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM research_production_policy_index").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM research_production_route_index").fetchone()[0]
            == 0
        )


def test_phase12_route_cli_missing_base_case_returns_exit_three_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    request_file = tmp_path / "research-need.json"
    request_file.write_text(_need().model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    invoked = CliRunner().invoke(app, ["research-production-route", str(request_file)])

    assert invoked.exit_code == 3, invoked.output
    payload = json.loads(invoked.output)
    assert payload["status"] == "NEEDS_INFO"
    assert payload["requested_artifact_type"] == "BaseCasePack"
    assert payload["requested_base_case_id"] == "base-case:not-required-for-scheduler"
    assert payload["requested_object_hash"] is None
    assert payload["finding_codes"] == ["BASE_CASE_NOT_REGISTERED"]
    assert "Traceback" not in invoked.output
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    with state.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM research_production_policy_index").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM research_production_route_index").fetchone()[0]
            == 0
        )


def test_phase12_route_separates_fundamental_budget_from_support_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, objects = _service(tmp_path)
    capabilities = service.capability_vectors()
    all_terms = sorted({term for item in capabilities for term in item.ontology_terms})
    all_inputs = sorted({value for item in capabilities for value in item.required_inputs})
    all_frequencies = sorted(
        {value for item in capabilities for value in item.required_frequencies}
    )
    base = _DummyBaseCase(
        company_id="company:000001",
        specialist_tags=["industrial"],
        created_at=NOW,
    )
    base_ref = objects.put_json(base.model_dump(mode="json"))
    monkeypatch.setattr(service.research, "get_base_case", lambda _base_case_id: base)
    monkeypatch.setattr(
        service.research,
        "base_case_object_hash",
        lambda _base_case_id: base_ref.sha256,
    )
    need = _need(
        need_id="need:routing",
        base_case_id="base-case:legacy-compatible",
        company_id="company:000001",
        ontology_terms=all_terms,
        available_inputs=all_inputs,
        available_frequencies=all_frequencies,
        horizon="medium",
        materiality=OrdinalResearchLevel.CRITICAL,
        uncertainty=OrdinalResearchLevel.CRITICAL,
        novelty=OrdinalResearchLevel.HIGH,
        portfolio_relevance=OrdinalResearchLevel.HIGH,
        catalyst_urgency=OrdinalResearchLevel.HIGH,
        data_availability=OrdinalResearchLevel.HIGH,
        source_diversity=OrdinalResearchLevel.HIGH,
        estimated_research_cost=OrdinalResearchLevel.MEDIUM,
    )

    plan = service.route(need)

    assert len(plan.selected_fundamental_specialists) <= plan.priority.specialist_budget
    assert len(plan.selected_fundamental_specialists) <= 4
    assert all(
        item.role is ProductionSkillRole.FUNDAMENTAL_SPECIALIST
        for item in plan.selected_fundamental_specialists
    )
    assert any(
        item.kind is ResearchSkillKind.GROWTH_PROBABILITY for item in plan.shared_hypothesis_modules
    )
    assert any(
        item.kind is ResearchSkillKind.GROWTH_VALUATION for item in plan.canonical_valuation_modules
    )
    assert any(
        item.kind is ResearchSkillKind.DAILY_TREND_HEALTH
        for item in plan.market_trade_context_modules
    )
    assert all(item.route_score >= 0 for item in plan.selected_fundamental_specialists)
    assert not plan.automatic_skill_modification_allowed
    assert not plan.paper_ledger_write_allowed
    assert not plan.broker_execution_allowed


def test_phase12_catalyst_monitor_reruns_only_affected_modules(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    source = objects.put_json({"kpi_id": "revenue_growth", "value": 0.18})
    state.register_artifact(
        artifact_id="kpi-source:600001:20260812",
        artifact_type="KPIObservationSource",
        schema_version="kpi-source-v1",
        object_hash=source.sha256,
        input_hashes=[],
    )
    catalyst = service.register_catalyst(
        CatalystRecordRequest(
            company_id="600001",
            thesis_id="thesis:growth",
            catalyst_type="earnings",
            expected_from=NOW,
            expected_to=NOW + timedelta(days=10),
            kpi_rules=[
                CatalystKPIRule(
                    kpi_id="revenue_growth",
                    comparison=KPIComparison.GE,
                    threshold=0.15,
                    created_at=NOW,
                )
            ],
            affected_modules=[
                ResearchModule.DRIVER_TREE,
                ResearchModule.FORECAST,
                ResearchModule.VALUATION,
            ],
            created_at=NOW,
        )
    )

    report = service.monitor_catalyst(
        CatalystMonitorRequest(
            catalyst_id=catalyst.catalyst_id,
            as_of=NOW + timedelta(days=1),
            observations=[
                ThesisKPIObservation(
                    kpi_id="revenue_growth",
                    value=0.18,
                    observed_at=NOW + timedelta(hours=1),
                    source_artifact_id="kpi-source:600001:20260812",
                    source_object_hash=source.sha256,
                    created_at=NOW + timedelta(hours=1),
                )
            ],
            created_at=NOW + timedelta(days=1),
        )
    )

    assert report.evaluated_status.value == "CONFIRMED"
    assert report.triggered_kpi_ids == ["revenue_growth"]
    assert report.rerun_modules == [
        ResearchModule.DRIVER_TREE,
        ResearchModule.FORECAST,
        ResearchModule.VALUATION,
    ]
    assert report.no_full_research_rerun
    assert not report.paper_ledger_write_allowed
    assert not report.broker_execution_allowed
    assert service.audit(report.monitor_id)["status"] == "PASS"


def test_phase12_efficiency_never_retires_skill_without_prospective_evidence(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    report = service.efficiency_report()

    assert report.summaries
    assert {item.recommendation for item in report.summaries} == {
        SkillLifecycleRecommendation.INSUFFICIENT_PROSPECTIVE_EVIDENCE
    }
    assert not report.automatic_retirement_allowed
    assert not report.automatic_skill_modification_allowed
