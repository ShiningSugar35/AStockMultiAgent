"""Regression gates for authentic outputs, request isolation and public answers.

All account events here belong to an isolated, migrated test database. External
network, model calls and production account data are not involved.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.capabilities import (
    CapabilityExecutionResult,
    CapabilityExecutor,
    CapabilityPlanner,
)
from astock.investor_orchestration.gateway import InvestorAnswerGateway
from astock.investor_orchestration.models import (
    CapabilityNode,
    CapabilityRequirement,
    InvestorAnswerDraft,
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier, output_model
from astock.investor_orchestration.paper_operations import PaperPreparationReceiptService
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash
from astock.paper_trading.ledger import LedgerService
from astock.schemas.institutional_research import MarketPriceAnchor
from astock.schemas.paper import PortfolioNAV


@dataclass(frozen=True)
class Environment:
    store: InvestorOrchestrationStore
    state: StateStore
    objects: ObjectStore
    ledger: LedgerService


@pytest.fixture(scope="module")
def environment(tmp_path_factory: pytest.TempPathFactory) -> Environment:
    directory = tmp_path_factory.mktemp("investor-guard-real-state")
    store = InvestorOrchestrationStore(directory / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(directory / "objects" / "sha256")
    ledger = LedgerService(state, objects)
    ledger.initialize_account("guard-paper-account", 100_000)
    return Environment(store, state, objects, ledger)


def request_for(**updates: Any) -> InvestorRequestEnvelope:
    request_id = f"guard-{uuid.uuid4()}"
    values: dict[str, Any] = {
        "request_id": request_id,
        "question_time": datetime.now(UTC),
        "user_timezone": "Asia/Shanghai",
        "raw_text": "查看模拟账户资金",
        "normalized_intent": RequestIntent.PAPER_STATUS,
        "side_effect": SideEffectClass.READ,
        "account_id": "guard-paper-account",
        "idempotency_key": request_id,
    }
    values.update(updates)
    return InvestorRequestEnvelope(**values)


def registered(
    environment: Environment,
    value: BaseModel,
    *,
    inputs: list[str] | None = None,
    artifact_type: str | None = None,
) -> str:
    artifact_id = f"guard-artifact:{uuid.uuid4()}"
    reference = environment.objects.put_json(value.model_dump(mode="json"))
    environment.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type or type(value).__name__,
        schema_version=str(getattr(value, "schema_version", "investor-answer-draft-v1")),
        object_hash=reference.sha256,
        input_hashes=inputs or [],
    )
    return artifact_id


def good_run(environment: Environment):
    request = request_for()
    service = InvestorOrchestrationService(environment.store)
    nav = environment.ledger.portfolio_nav(request.account_id or "", as_of=request.question_time)
    artifact_id = registered(environment, nav)
    preflight, plan, coverage = service.execute(
        request,
        handlers={"PAPER": lambda *_: CapabilityExecutionResult(artifact_ids=(artifact_id,))},
    )
    assert coverage.coverage_complete, coverage.unresolved_conflicts
    assert coverage.outputs_verified
    assert nav.cash_fen == 100_000
    draft = InvestorAnswerDraft(
        request_id=request.request_id,
        conclusion="模拟账户现金为1000元。",
        reasons=("资金数额来自已核对的模拟账本。",),
        risks=("模拟结果不代表真实账户表现。",),
        actions=(),
        change_conditions=("账户发生确认后的资金变化时重新核对。",),
        evidence_as_of=request.question_time,
    )
    return request, preflight, plan, coverage, draft, artifact_id


def test_real_registered_paper_nav_and_bound_public_answer_are_accepted(
    environment: Environment,
) -> None:
    from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector

    _, preflight, _, coverage, _, _ = good_run(environment)
    artifact_id, draft = VerifiedAnswerProjector(
        RegisteredOutputVerifier(environment.store)
    ).freeze(preflight, coverage)
    answer = InvestorAnswerGateway(environment.store).render(
        draft, preflight=preflight, coverage=coverage, draft_artifact_id=artifact_id
    )
    assert not answer.degraded
    assert answer.conclusion == draft.conclusion
    assert answer.actual_holding_section is None
    assert answer.paper_holding_section is None
    assert answer.degradation_reason is None


def test_paper_prepare_needs_info_is_a_certified_safe_terminal_result(
    environment: Environment,
) -> None:
    request = request_for(
        raw_text="模拟买入600519共1000股，但尚未给价格和订单类型",
        normalized_intent=RequestIntent.PAPER_PREPARE,
        side_effect=SideEffectClass.PT_PREPARE,
        entity_ids=("XSHG:600519",),
    )
    preflight, plan = InvestorOrchestrationService(environment.store).prepare(request)
    node = next(item for item in plan.nodes if item.capability_id == "PAPER")
    before = environment.ledger.open_orders("guard-paper-account")
    artifact_id, receipt = PaperPreparationReceiptService(
        environment.store, environment.objects
    ).needs_info(
        request_id=request.request_id,
        account_id="guard-paper-account",
        as_of=request.evidence_cutoff,
        instrument_id="XSHG:600519",
        side="BUY",
        quantity=1000,
        missing_fields=["ORDER_TYPE", "LIMIT_PRICE"],
    )

    RegisteredOutputVerifier(environment.store, environment.objects).verify(
        node, (artifact_id,), request, preflight
    )
    assert receipt.status == "NEEDS_INFO"
    assert receipt.order_created is False
    assert receipt.position_changed is False
    assert environment.ledger.open_orders("guard-paper-account") == before


def test_nonexistent_and_inline_outputs_cannot_certify_coverage(environment: Environment) -> None:
    request = request_for()
    service = InvestorOrchestrationService(environment.store)
    for result in (CapabilityExecutionResult(artifact_ids=("does-not-exist",)), {"success": True}):
        _, _, coverage = service.execute(
            request, handlers={"PAPER": lambda *_, value=result: value}
        )
        assert not coverage.coverage_complete
        assert coverage.required_capability_coverage < 1.0


@pytest.mark.parametrize(
    "field", ["conclusion", "reasons", "risks", "actions", "change_conditions"]
)
def test_all_public_fields_are_audited_even_with_authentic_receipts(
    environment: Environment, field: str
) -> None:
    _, preflight, _, coverage, draft, _ = good_run(environment)
    poison = "MarketPriceAnchor CLAIM_IDS_REQUIRED"
    values = draft.model_dump()
    values[field] = poison if field == "conclusion" else (poison,)
    poisoned = InvestorAnswerDraft(**values)
    artifact_id = registered(
        environment, poisoned, inputs=[preflight.receipt_hash, coverage.receipt_hash]
    )
    answer = InvestorAnswerGateway(environment.store).render(
        poisoned, preflight=preflight, coverage=coverage, draft_artifact_id=artifact_id
    )
    assert answer.degraded
    assert "MarketPriceAnchor" not in answer.model_dump_json()
    assert "CLAIM_IDS_REQUIRED" not in answer.model_dump_json()


def test_unregistered_answer_cannot_use_an_otherwise_valid_coverage_receipt(
    environment: Environment,
) -> None:
    _, preflight, _, coverage, draft, _ = good_run(environment)
    answer = InvestorAnswerGateway(environment.store).render(
        draft, preflight=preflight, coverage=coverage
    )
    assert answer.degraded
    assert draft.conclusion not in answer.model_dump_json()


def test_answer_registration_must_bind_exact_input_hashes(environment: Environment) -> None:
    _, preflight, _, coverage, draft, _ = good_run(environment)
    artifact_id = registered(environment, draft, inputs=["0" * 64])
    answer = InvestorAnswerGateway(environment.store).render(
        draft, preflight=preflight, coverage=coverage, draft_artifact_id=artifact_id
    )
    assert answer.degraded


def test_forged_preflight_payload_cannot_reuse_authentic_identity(environment: Environment) -> None:
    _, preflight, _, coverage, draft, _ = good_run(environment)
    artifact_id = registered(
        environment, draft, inputs=[preflight.receipt_hash, coverage.receipt_hash]
    )
    altered_context = preflight.context.model_copy(update={"aggregate_revision": "forged"})
    altered = preflight.model_copy(update={"context": altered_context})
    answer = InvestorAnswerGateway(environment.store).render(
        draft, preflight=altered, coverage=coverage, draft_artifact_id=artifact_id
    )
    assert answer.degraded


def test_future_and_naive_answer_times_are_rejected(environment: Environment) -> None:
    _, preflight, _, coverage, draft, _ = good_run(environment)
    for invalid_time in (datetime.now(UTC) + timedelta(days=365), datetime(2026, 1, 1)):
        with pytest.raises(ValueError):
            altered = draft.model_copy(update={"evidence_as_of": invalid_time})
            InvestorAnswerGateway(environment.store).render(
                altered, preflight=preflight, coverage=coverage
            )


@pytest.mark.parametrize(
    "intent,side_effect",
    [
        (RequestIntent.PAPER_PREPARE, SideEffectClass.READ),
        (RequestIntent.PAPER_CONFIRM, SideEffectClass.META),
        (RequestIntent.ACCOUNT_FACT_WRITE, SideEffectClass.READ),
        (RequestIntent.RESEARCH, SideEffectClass.EA_WRITE),
    ],
)
def test_intent_cannot_upgrade_declared_side_effect(
    environment: Environment, intent: RequestIntent, side_effect: SideEffectClass
) -> None:
    request = request_for(normalized_intent=intent, side_effect=side_effect)
    preflight = InvestorOrchestrationService(environment.store).preflight_service.build(request)
    with pytest.raises(ValueError, match="side-effect"):
        CapabilityPlanner().plan(request, preflight)


def test_scenario_cannot_remove_a_mandatory_gate(environment: Environment) -> None:
    request = request_for()
    preflight = InvestorOrchestrationService(environment.store).preflight_service.build(request)
    with pytest.raises(ValueError, match="mandatory"):
        CapabilityPlanner().plan(
            request,
            preflight,
            scenario_requirements={"SESSION_PREFLIGHT": CapabilityRequirement.OPTIONAL},
        )


def test_rehashed_plan_cannot_remove_dependency_or_change_schema(environment: Environment) -> None:
    request, preflight, plan, _, _, _ = good_run(environment)
    for changes in ({"dependencies": ()}, {"output_schema": "ResearchNarrativeBundle"}):
        nodes = tuple(
            node.model_copy(update=changes) if node.capability_id == "PAPER" else node
            for node in plan.nodes
        )
        altered = plan.model_copy(update={"nodes": nodes})
        altered = altered.model_copy(
            update={"plan_hash": content_hash(altered.model_dump(exclude={"plan_id", "plan_hash"}))}
        )
        with pytest.raises(ValueError, match="dependency|output contract"):
            CapabilityExecutor({}, store=environment.store).execute(altered, request, preflight)


def test_cross_request_plan_is_rejected_before_handler_execution(environment: Environment) -> None:
    request, preflight, plan, _, _, _ = good_run(environment)
    alien = request.model_copy(update={"request_id": "other-request"})
    with pytest.raises(ValueError, match="identities"):
        CapabilityExecutor({}, store=environment.store).execute(plan, alien, preflight)


def test_registered_output_type_and_account_are_checked(environment: Environment) -> None:
    request, preflight, plan, _, _, artifact_id = good_run(environment)
    verifier = RegisteredOutputVerifier(environment.store)
    assert isinstance(verifier.load(artifact_id, PortfolioNAV), PortfolioNAV)
    with pytest.raises(ValueError, match="type"):
        verifier.load(artifact_id, InvestorAnswerDraft)
    node = next(node for node in plan.nodes if node.capability_id == "PAPER")
    with pytest.raises(ValueError, match="account"):
        verifier.verify(
            node,
            (artifact_id,),
            request.model_copy(update={"account_id": "other-account"}),
            preflight,
        )


def test_registered_object_corruption_is_not_silently_accepted(environment: Environment) -> None:
    request = request_for()
    nav = environment.ledger.portfolio_nav(request.account_id or "", as_of=request.question_time)
    artifact_id = registered(environment, nav)
    record = environment.state.artifact_record(artifact_id)
    assert record is not None
    path: Path = environment.objects.path_for(record["object_hash"])
    path.write_bytes(b"synthetic corruption in isolated test store")
    with pytest.raises(StorageError):
        RegisteredOutputVerifier(environment.store).load(artifact_id, PortfolioNAV)


def test_price_anchor_requires_a_real_source_hash_binding(environment: Environment) -> None:
    request = request_for()
    preflight = InvestorOrchestrationService(environment.store).preflight_service.build(request)
    anchor = MarketPriceAnchor(
        created_at=request.question_time,
        price=Decimal("10"),
        observed_at=request.question_time,
        available_to_system_at=request.question_time,
        source_artifact_id="missing-source",
        source_object_hash="0" * 64,
    )
    artifact_id = registered(environment, anchor)
    node = CapabilityNode(
        capability_id="CURRENT_MARKET",
        requirement=CapabilityRequirement.REQUIRED,
        output_schema="MarketPriceAnchor",
    )
    with pytest.raises(ValueError, match="lineage"):
        RegisteredOutputVerifier(environment.store).verify(node, (artifact_id,), request, preflight)


def test_stale_and_future_registered_output_times_fail_closed() -> None:
    cutoff = datetime.now(UTC)
    with pytest.raises(ValueError, match="stale"):
        RegisteredOutputVerifier._check_time(
            {"observed_at": (cutoff - timedelta(hours=2)).isoformat()}, cutoff, 60
        )
    with pytest.raises(ValueError, match="frozen"):
        RegisteredOutputVerifier._check_time(
            {"available_at": (cutoff + timedelta(days=1)).isoformat()}, cutoff
        )


def test_market_identity_is_not_erased_by_normalization() -> None:
    same = RegisteredOutputVerifier._same_identity
    assert same("600519.XSHG", "XSHG:600519")
    assert not same("600519.XSHG", "XSHE:600519")
    assert not same("600519.XSHG", "000001.XSHE")
    with pytest.raises(ValueError, match="canonical"):
        output_model("NotARealContract")


def test_publication_reuses_completed_receipts_without_reexecuting_handlers(
    environment: Environment,
) -> None:
    from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector

    _, preflight, _, coverage, _, _ = good_run(environment)
    artifact_id, _ = VerifiedAnswerProjector(RegisteredOutputVerifier(environment.store)).freeze(
        preflight, coverage
    )
    service = InvestorOrchestrationService(environment.store)
    with environment.store.connect() as connection:
        before = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM journal),"
                "(SELECT COUNT(*) FROM capability_execution_plans),"
                "(SELECT COUNT(*) FROM capability_coverage_receipts)"
            ).fetchone()
        )
    first = service.publish_registered(
        draft_artifact_id=artifact_id, coverage_receipt_id=coverage.receipt_id
    )
    second = service.publish_registered(
        draft_artifact_id=artifact_id, coverage_receipt_id=coverage.receipt_id
    )
    with environment.store.connect() as connection:
        after = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM journal),"
                "(SELECT COUNT(*) FROM capability_execution_plans),"
                "(SELECT COUNT(*) FROM capability_coverage_receipts)"
            ).fetchone()
        )
    assert first == second
    assert not first.degraded
    assert before == after


def test_persisted_complete_flag_cannot_override_inconsistent_coverage_metrics(
    environment: Environment,
) -> None:
    request, _, _, coverage, _, _ = good_run(environment)
    forged = coverage.model_copy(
        update={
            "receipt_id": f"inconsistent-coverage-{uuid.uuid4()}",
            "required_capability_coverage": 0.0,
        }
    )
    forged = forged.model_copy(
        update={
            "receipt_hash": content_hash(forged.model_dump(exclude={"receipt_id", "receipt_hash"}))
        }
    )
    environment.store.save_coverage_receipt(forged)
    assert not RegisteredOutputVerifier(environment.store).authenticated_coverage(
        request.request_id, forged.receipt_id, forged.receipt_hash
    )


@pytest.mark.parametrize(
    "identity",
    ["SCAM:600519", "600519.UNKNOWN", "XSHG:600519:XSHE", "６００５１９.XSHG", "600519.XSHG.EXTRA"],
)
def test_malformed_market_identity_is_not_reduced_to_a_valid_bare_code(identity: str) -> None:
    assert not RegisteredOutputVerifier._same_identity(identity, "XSHG:600519")


def test_subject_verification_uses_only_requested_primary_keys(
    environment: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = request_for(entity_ids=("600519.XSHG",))
    service = InvestorOrchestrationService(environment.store)
    preflight, _ = service.prepare(request)
    output = service._subject_registry_handler(request, preflight)

    def unexpected_history_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("verifying one subject must not scan the full history")

    monkeypatch.setattr(environment.store, "subject_events", unexpected_history_scan)
    node = CapabilityNode(
        capability_id="SUBJECT_REGISTRY",
        requirement=CapabilityRequirement.REQUIRED,
        output_schema="ResearchSubjectRegistryProjection",
    )
    RegisteredOutputVerifier(environment.store).verify(
        node, output.artifact_ids, request, preflight
    )
    with pytest.raises(ValueError, match="resolved entities"):
        RegisteredOutputVerifier(environment.store).verify(
            node,
            output.artifact_ids,
            request.model_copy(update={"entity_ids": ("600519.XSHG", "000001.XSHE")}),
            preflight,
        )
