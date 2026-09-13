"""Domain contracts must bind real artifacts, not names or success booleans.

These are isolated contract tests, not the 68 business E2E acceptance suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from astock.investor_orchestration.capabilities import _BASE_NODES
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier, output_model
from astock.investor_orchestration.store import InvestorOrchestrationStore


@pytest.mark.parametrize(
    "capability,expected",
    [
        ("INDUSTRY", "IndustryProfile"),
        ("COMPANY_RESEARCH", "InstitutionalDecisionContext"),
        ("FINANCIAL_INTEGRITY", "FinancialIntegrityEvidencePack"),
        ("GOVERNANCE", "ResearchRoleOutput"),
        ("EVENT_RESEARCH", "ResearchRoleOutput"),
        ("FORECAST_VALUATION", "ValuationPack"),
        ("RED_TEAM", "ResearchRoleOutput"),
        ("COMMITTEE", "ClassifiedTradeProtocol"),
        ("PORTFOLIO", "PortfolioAnalysisReport"),
        ("HOLDING_REVIEW", "HoldingReviewPack"),
        ("FULL_MARKET", "FullResearchInputReadinessReport"),
        ("EXTERNAL_ACCOUNT", "ExternalAccountEvent"),
        ("ETF", "ETFResearchMetrics"),
    ],
)
def test_domain_node_output_is_a_real_canonical_model(capability: str, expected: str) -> None:
    node = _BASE_NODES[capability]
    assert node.output_schema == expected
    model = output_model(node.output_schema)
    assert issubclass(model, BaseModel)
    assert model.__module__.startswith("astock.schemas.")


def test_nested_price_availability_cannot_bypass_temporal_check() -> None:
    cutoff = datetime.now(UTC)
    payload = {
        "as_of": cutoff.isoformat(),
        "market_price_anchor": {
            "observed_at": cutoff.isoformat(),
            "available_to_system_at": (cutoff + timedelta(days=1)).isoformat(),
        },
    }
    with pytest.raises(ValueError, match="frozen|future"):
        RegisteredOutputVerifier._check_time(payload, cutoff)


def test_forecast_dates_are_not_confused_with_future_input_availability() -> None:
    cutoff = datetime.now(UTC)
    RegisteredOutputVerifier._check_time(
        {
            "as_of": cutoff.isoformat(),
            "scenarios": [{"forecast_year": cutoff.year + 3, "expected_year_end": "2030-12-31"}],
        },
        cutoff,
    )


def test_parallel_source_id_hash_arrays_are_verified(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    verifier = RegisteredOutputVerifier(store)
    with pytest.raises(ValueError, match="lineage|source"):
        verifier._verify_linked_hashes(
            {
                "source_artifact_ids": ["not-registered"],
                "source_object_hashes": ["0" * 64],
            }
        )


def test_partial_source_id_hash_binding_is_rejected(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    verifier = RegisteredOutputVerifier(store)
    with pytest.raises(ValueError, match="lineage|source"):
        verifier._verify_linked_hashes({"source_artifact_ids": ["missing-hash"]})


@pytest.fixture(scope="module")
def computed_pipeline(tmp_path_factory: pytest.TempPathFactory):
    """Real deterministic domain services; upstream research input is recorded test data.

    This is not live acquisition, an owner approval, or a complete business E2E.
    """
    from astock.core.object_store import ObjectStore
    from astock.research.institutional import InstitutionalResearchService
    from tests.unit.test_institutional_research import (
        _finalize_request,
        _register_claim_with_evidence,
        _register_frozen_pack,
    )

    directory = tmp_path_factory.mktemp("canonical-domain-pipeline")
    store = InvestorOrchestrationStore(directory / "state.sqlite")
    store.initialize()
    from astock.core.state import StateStore

    state = StateStore(store.path)
    objects = ObjectStore(directory / "objects" / "sha256")
    claim = "claim:domain-contract-source"
    evidence, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim,
        evidence_id="evidence:domain-contract-source",
        source_id="recorded-domain-contract-input",
    )
    frozen = _register_frozen_pack(state, objects, claim_ids=[claim], evidence_ids=[evidence])
    service = InstitutionalResearchService(state, objects)
    bundle = service.finalize(_finalize_request(frozen, claim, evidence))
    assert bundle.status.value == "READY"
    assert service.audit(f"FundamentalModelBundle:{bundle.bundle_id}")["status"] == "PASS"
    return store, state, objects, bundle, evidence


def _request_context(store: InvestorOrchestrationStore):
    import uuid

    from astock.investor_orchestration.models import (
        InvestorRequestEnvelope,
        RequestIntent,
        SideEffectClass,
    )
    from astock.investor_orchestration.preflight import InvestorSessionPreflightService

    identity = f"domain-request:{uuid.uuid4()}"
    request = InvestorRequestEnvelope(
        request_id=identity,
        idempotency_key=identity,
        question_time=datetime.now(UTC),
        user_timezone="Asia/Shanghai",
        raw_text="核验已冻结的测试研究结果",
        normalized_intent=RequestIntent.RESEARCH,
        side_effect=SideEffectClass.READ,
        entity_ids=("XSHG:600001",),
    )
    return request, InvestorSessionPreflightService(store).build(request)


def test_real_industry_and_recomputed_valuation_outputs_are_accepted(computed_pipeline) -> None:
    from decimal import Decimal

    from astock.schemas.institutional_research import ForecastPack

    store, state, objects, bundle, _ = computed_pipeline
    request, preflight = _request_context(store)
    verifier = RegisteredOutputVerifier(store, objects)
    for capability, artifact in (
        ("INDUSTRY", bundle.industry_profile_artifact_id),
        ("FORECAST_VALUATION", bundle.valuation_pack_artifact_id),
    ):
        verifier.verify(_BASE_NODES[capability], (artifact,), request, preflight)
    forecast = verifier.load(bundle.forecast_pack_artifact_id, ForecastPack)
    assert isinstance(forecast, ForecastPack)
    base = next(item for item in forecast.scenarios if item.scenario.value == "BASE")
    assert base.periods[0].fcff == Decimal("15")
    assert state.integrity_check() == "ok"


def test_valid_source_bindings_do_not_depend_on_parallel_array_order(computed_pipeline) -> None:
    store, state, objects, bundle, _ = computed_pipeline
    ids = [bundle.industry_profile_artifact_id, bundle.company_economics_artifact_id]
    hashes = []
    for artifact in ids:
        record = state.artifact_record(artifact)
        assert record is not None
        hashes.append(record["object_hash"])
    assert len(set(hashes)) == 2
    RegisteredOutputVerifier(store, objects)._verify_linked_hashes(
        {
            "source_artifact_ids": ids,
            "source_object_hashes": list(reversed(hashes)),
        }
    )


def test_partial_financial_coverage_cannot_hide_behind_succeeded_status(computed_pipeline) -> None:
    from astock.schemas.financial import FinancialIntegrityEvidencePack
    from astock.schemas.runs import RunStatus
    from tests.unit.test_research_team import _register_financial_pack

    store, state, objects, _, _ = computed_pipeline
    identifier = "FinancialIntegrityEvidencePack:partial-domain-test"
    _register_financial_pack(state, objects, artifact_id=identifier, complete=False)
    verifier = RegisteredOutputVerifier(store, objects)
    partial = verifier.load(identifier, FinancialIntegrityEvidencePack)
    assert isinstance(partial, FinancialIntegrityEvidencePack)
    invalid = partial.model_copy(update={"status": RunStatus.SUCCEEDED, "company_id": "600001"})
    ref = objects.put_json(invalid.model_dump(mode="json"))
    invalid_id = "FinancialIntegrityEvidencePack:misleading-success-domain-test"
    state.register_artifact(
        artifact_id=invalid_id,
        artifact_type="FinancialIntegrityEvidencePack",
        schema_version=invalid.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    request, preflight = _request_context(store)
    with pytest.raises(ValueError, match="SUCCEEDED"):
        verifier.verify(_BASE_NODES["FINANCIAL_INTEGRITY"], (invalid_id,), request, preflight)


def test_real_role_registration_is_accepted_only_for_its_planned_domain(computed_pipeline) -> None:
    from astock.research.team import ResearchTeamService
    from astock.schemas.research_team import (
        ResearchRoleOutput,
        ResearchRoleResult,
        ResearchTeamTaskState,
    )
    from tests.unit.test_research_team import _register_acquisition_report, _typed_role_member_ids

    store, state, objects, bundle, evidence = computed_pipeline
    team = ResearchTeamService(
        project_root=Path(__file__).resolve().parents[2], state=state, objects=objects
    )
    report_id, _ = _register_acquisition_report(state, objects, company_id="600001")
    plan = team.create_company_plan(company_id="600001", acquisition_report_artifact_id=report_id)
    governance_id = None
    for task_id in ("company-intent", "governance-management-quality"):
        task = next(item for item in plan.tasks if item.task_id == task_id)
        typed_members = _typed_role_member_ids(
            team,
            state,
            objects,
            plan_id=plan.plan_id,
            task_id=task_id,
            formal=True,
        )
        member_artifact_ids = typed_members or [bundle.company_economics_artifact_id]
        output = ResearchRoleOutput(
            plan_id=plan.plan_id,
            task_id=task_id,
            output_contract=task.output_contract,
            member_artifact_ids=member_artifact_ids,
            evidence_ids=[evidence],
            readiness_check_results={key: True for key in task.readiness_checks},
            summary="记录式研究输入：依据已冻结公司经济模型复核治理字段。",
        )
        registered = team.register_role_output(output)
        output_id = str(registered["artifact_id"])
        team.register_role_result(
            ResearchRoleResult(
                plan_id=plan.plan_id,
                task_id=task_id,
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id=f"recorded-context:{task_id}",
                output_artifact_ids=[output_id],
                evidence_ids=[evidence],
            )
        )
        governance_id = output_id
    assert governance_id is not None
    request, preflight = _request_context(store)
    verifier = RegisteredOutputVerifier(store, objects)
    verifier.verify(_BASE_NODES["GOVERNANCE"], (governance_id,), request, preflight)
    with pytest.raises(ValueError, match="role/contract"):
        verifier.verify(_BASE_NODES["EVENT_RESEARCH"], (governance_id,), request, preflight)
    alien = request.model_copy(update={"entity_ids": ("XSHE:000001",)})
    with pytest.raises(ValueError, match="another security"):
        verifier.verify(_BASE_NODES["GOVERNANCE"], (governance_id,), alien, preflight)


def test_empty_ready_boolean_is_not_a_full_market_admission(computed_pipeline) -> None:
    from astock.schemas.research_team import (
        FullResearchInputReadinessReport,
        FullResearchInputReadinessStatus,
    )

    store, state, objects, _, _ = computed_pipeline
    output = FullResearchInputReadinessReport(
        report_id="unearned-readiness",
        plan_id="not-a-real-full-market-plan",
        status=FullResearchInputReadinessStatus.READY,
        required_checks=[],
        passed_checks=[],
        missing_or_failed_checks=[],
        full_research_input_ready=True,
    )
    ref = objects.put_json(output.model_dump(mode="json"))
    artifact = "FullResearchInputReadinessReport:unearned-readiness"
    state.register_artifact(
        artifact_id=artifact,
        artifact_type=type(output).__name__,
        schema_version=output.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    request, preflight = _request_context(store)
    with pytest.raises(ValueError, match="FULL_MARKET"):
        RegisteredOutputVerifier(store, objects).verify(
            _BASE_NODES["FULL_MARKET"],
            (artifact,),
            request,
            preflight,
        )


def test_nested_artifact_graph_is_bounded() -> None:
    payload = {"as_of": datetime.now(UTC).isoformat()}
    for _ in range(70):
        payload = {"nested": payload}
    with pytest.raises(ValueError, match="bounded"):
        RegisteredOutputVerifier._check_time(payload, datetime.now(UTC))
