"""Reproducible answer projection over real isolated domain artifacts, never production facts."""

from __future__ import annotations

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.gateway import InvestorAnswerGateway
from astock.investor_orchestration.models import RequestIntent, SideEffectClass
from astock.investor_orchestration.scenarios import (
    BusinessScenario,
    BusinessScenarioManifest,
    ScenarioContractRunner,
)
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.ledger import LedgerService
from tests.unit.test_investor_orchestration_guards import Environment, good_run, registered


@pytest.fixture(scope="module")
def environment(tmp_path_factory: pytest.TempPathFactory) -> Environment:
    root = tmp_path_factory.mktemp("verified-answer-repro")
    store = InvestorOrchestrationStore(root / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(root / "objects" / "sha256")
    ledger = LedgerService(state, objects)
    ledger.initialize_account("guard-paper-account", 100_000)
    return Environment(store, state, objects, ledger)


def test_registered_free_text_cannot_invent_a_cash_balance(environment: Environment) -> None:
    _, preflight, _, coverage, draft, _ = good_run(environment)
    lie = draft.model_copy(update={"conclusion": "模拟账户现金为99999999元。"})
    artifact_id = registered(
        environment, lie, inputs=[preflight.receipt_hash, coverage.receipt_hash]
    )
    answer = InvestorAnswerGateway(environment.store).render(
        lie,
        preflight=preflight,
        coverage=coverage,
        draft_artifact_id=artifact_id,
    )
    assert answer.degraded, "registering free text does not validate its economic claims"
    assert "99999999" not in answer.model_dump_json()


def test_verified_answer_can_be_generated_without_an_untrusted_draft(
    environment: Environment,
) -> None:
    request, preflight, _, coverage, _, _ = good_run(environment)
    service = InvestorOrchestrationService(environment.store)
    publish = service.publish_verified
    with environment.store.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM journal ORDER BY seq")]
    answer = publish(coverage_receipt_id=coverage.receipt_id)
    assert not answer.degraded
    assert answer.request_id == request.request_id
    assert "可用现金为1000元" in answer.conclusion
    assert "1000元" in answer.model_dump_json()
    assert "99999999" not in answer.model_dump_json()
    assert answer.evidence_as_of == preflight.as_of
    assert answer == publish(coverage_receipt_id=coverage.receipt_id)
    with environment.store.connect() as connection:
        after = [tuple(row) for row in connection.execute("SELECT * FROM journal ORDER BY seq")]
    assert before == after


def test_degraded_answer_cannot_receive_a_clean_positive_scenario_score(
    environment: Environment,
) -> None:
    request, preflight, _, coverage, draft, _ = good_run(environment)
    manifest = BusinessScenarioManifest(
        schema_version="business-question-capability-manifest-v1",
        policy_version="recorded-repro-v1",
        source_matrix="recorded-test-boundary",
        scenario_count=1,
        scenarios=(
            BusinessScenario(
                id=96,
                title="核验模拟账户资金",
                intent=RequestIntent.PAPER_STATUS,
                required=("PAPER",),
                allowed_side_effects=(SideEffectClass.READ,),
                side_effect_description="READ",
                acceptance="真实现金与有效公开结论",
                source_chain="recorded fixture",
            ),
        ),
    )
    # Reuse genuine execution receipts. The fake wrapper only isolates the runner's
    # scoring function; it is not evidence of business execution or 68-case E2E.
    from unittest.mock import Mock

    service = InvestorOrchestrationService(environment.store)
    service.execute = Mock(return_value=(preflight, None, coverage))
    result = ScenarioContractRunner(service, manifest).run(96, request, draft, handlers={})
    assert result.answer.degraded
    assert "ANSWER_NOT_CERTIFIED" in result.failures


@pytest.fixture(scope="module")
def projection_class():
    from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector

    return VerifiedAnswerProjector


def test_candidate_projection_directly_uses_verified_cash(environment, projection_class) -> None:
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier

    _, preflight, _, coverage, _, _ = good_run(environment)
    projector = projection_class(RegisteredOutputVerifier(environment.store))
    first = projector.derive(preflight, coverage)
    assert first.conclusion == "该模拟账户可用现金为1000元。"
    assert "1000元" in first.model_dump_json()
    assert first.actual_holding_section is None and first.paper_holding_section is None
    assert first == projector.derive(preflight, coverage)
    artifact_id, persisted = projector.freeze(preflight, coverage)
    assert persisted == first
    assert environment.state.artifact_record(artifact_id) is not None


def test_candidate_projection_reconciles_nav_against_frozen_account(
    environment, projection_class
) -> None:
    from astock.investor_orchestration.capabilities import CapabilityExecutionResult
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
    from tests.unit.test_investor_orchestration_guards import request_for

    request = request_for()
    nav = environment.ledger.portfolio_nav(request.account_id, as_of=request.question_time)
    altered = nav.model_copy(update={"cash_fen": nav.cash_fen + 999, "nav_fen": nav.nav_fen + 999})
    artifact_id = registered(environment, altered)
    preflight, _, coverage = InvestorOrchestrationService(environment.store).execute(
        request,
        handlers={
            "PAPER": lambda *_: CapabilityExecutionResult(artifact_ids=(artifact_id,)),
        },
    )
    assert coverage.coverage_complete
    with pytest.raises(ValueError, match="cash is inconsistent"):
        projection_class(RegisteredOutputVerifier(environment.store)).derive(preflight, coverage)


def test_candidate_projection_rejects_caller_altered_request_fingerprint(
    environment, projection_class
) -> None:
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier

    _, preflight, _, coverage, _, _ = good_run(environment)
    forged = coverage.model_copy(update={"request_fingerprint": "0" * 64})
    with pytest.raises(ValueError, match="binding|coverage"):
        projection_class(RegisteredOutputVerifier(environment.store)).derive(preflight, forged)


@pytest.mark.parametrize(
    "field",
    [
        "conclusion",
        "reasons",
        "risks",
        "actions",
        "change_conditions",
        "actual_holding_section",
        "paper_holding_section",
    ],
)
def test_correct_lineage_does_not_authorize_changed_public_fields(
    environment, projection_class, field
) -> None:
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier

    _, preflight, _, coverage, _, _ = good_run(environment)
    projector = projection_class(RegisteredOutputVerifier(environment.store))
    artifact_id, verified = projector.freeze(preflight, coverage)
    record = environment.state.artifact_record(artifact_id)
    assert record is not None
    poison = "未经核验的金额为88888888元，并应立即买入。"
    changed = verified.model_copy(
        update={
            field: (poison,)
            if field in {"reasons", "risks", "actions", "change_conditions"}
            else poison,
        }
    )
    forged_id = registered(environment, changed, inputs=record["input_hashes"])
    answer = InvestorAnswerGateway(environment.store).render(
        changed,
        preflight=preflight,
        coverage=coverage,
        draft_artifact_id=forged_id,
    )
    assert answer.degraded
    assert "88888888" not in answer.model_dump_json()


def test_generated_projection_survives_json_roundtrip_and_is_idempotent(
    environment, projection_class
) -> None:
    from astock.investor_orchestration.models import InvestorAnswerDraft
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier

    _, preflight, _, coverage, _, _ = good_run(environment)
    verifier = RegisteredOutputVerifier(environment.store)
    projector = projection_class(verifier)
    first_id, first = projector.freeze(preflight, coverage)
    assert verifier.load(first_id, InvestorAnswerDraft) == first
    second_id, second = projector.freeze(preflight, coverage)
    assert (second_id, second) == (first_id, first)
    assert (
        not InvestorAnswerGateway(environment.store)
        .publish_registered(
            draft_artifact_id=first_id,
            coverage_receipt_id=coverage.receipt_id,
        )
        .degraded
    )


def test_corrupted_persisted_answer_cannot_be_republished(environment, projection_class) -> None:
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier

    _, preflight, _, coverage, _, _ = good_run(environment)
    projector = projection_class(RegisteredOutputVerifier(environment.store))
    artifact_id, draft = projector.freeze(preflight, coverage)
    record = environment.state.artifact_record(artifact_id)
    assert record is not None
    environment.objects.path_for(record["object_hash"]).write_bytes(b"isolated corrupt answer")
    gateway = InvestorAnswerGateway(environment.store)
    assert gateway.render(
        draft, preflight=preflight, coverage=coverage, draft_artifact_id=artifact_id
    ).degraded
    assert gateway.publish_verified(coverage_receipt_id=coverage.receipt_id).degraded


def test_real_paper_status_scenario_generates_a_certified_answer_without_a_draft(
    environment,
) -> None:
    from astock.investor_orchestration.capabilities import CapabilityExecutionResult
    from tests.unit.test_investor_orchestration_guards import request_for

    request = request_for()
    assert request.account_id is not None
    nav = environment.ledger.portfolio_nav(request.account_id, as_of=request.question_time)
    artifact_id = registered(environment, nav)
    manifest = BusinessScenarioManifest(
        schema_version="business-question-capability-manifest-v1",
        policy_version="real-nav-slice-v1",
        source_matrix="isolated-ledger",
        scenario_count=1,
        scenarios=(
            BusinessScenario(
                id=96,
                title="核对模拟账户资金",
                intent=RequestIntent.PAPER_STATUS,
                required=("PAPER",),
                allowed_side_effects=(SideEffectClass.READ,),
                side_effect_description="READ",
                acceptance="真实现金与可复核答案",
                source_chain="canonical LedgerService",
            ),
        ),
    )
    runner = ScenarioContractRunner(InvestorOrchestrationService(environment.store), manifest)
    result = runner.run(
        96,
        request,
        handlers={
            "PAPER": lambda *_: CapabilityExecutionResult(artifact_ids=(artifact_id,)),
        },
    )
    assert result.failures == ()
    assert result.coverage_complete
    assert not result.answer.degraded
    assert result.answer.conclusion == "该模拟账户可用现金为1000元。"


def test_missing_required_domain_result_never_gets_a_verified_answer(environment) -> None:
    from tests.unit.test_investor_orchestration_guards import request_for

    request = request_for()
    service = InvestorOrchestrationService(environment.store)
    _, _, coverage = service.execute(request, handlers={})
    assert not coverage.coverage_complete
    result = service.publish_verified(coverage_receipt_id=coverage.receipt_id)
    assert result.degraded
    assert "1000" not in result.conclusion


def test_public_cli_publishes_only_the_reproducible_answer(environment) -> None:
    import json

    from typer.testing import CliRunner

    from astock.investor_orchestration.cli import app

    _, _, _, coverage, _, _ = good_run(environment)
    outcome = CliRunner().invoke(
        app,
        [
            "publish",
            coverage.receipt_id,
            "--database",
            str(environment.store.path),
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert payload["conclusion"] == "该模拟账户可用现金为1000元。"
    assert payload["degraded"] is False
    assert "preflight" not in outcome.stdout
    assert "source_artifact_ids" not in outcome.stdout


def test_registered_cli_uses_verified_inputs_and_never_reexecutes_economic_operations(
    environment, tmp_path
) -> None:
    import json

    from typer.testing import CliRunner

    from astock.investor_orchestration.cli import app

    request, _, _, _, _, artifact_id = good_run(environment)
    source = tmp_path / "read-only-input.json"
    source.write_text(
        json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "artifacts": {"PAPER": [artifact_id]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with environment.store.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM journal ORDER BY seq")]
    command = [
        "execute-registered",
        str(source),
        "--current",
        "--database",
        str(environment.store.path),
    ]
    runner = CliRunner()
    first = runner.invoke(app, command)
    second = runner.invoke(app, command)
    assert first.exit_code == second.exit_code == 0, (first.output, second.output)
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert json.loads(first.stdout)["conclusion"] == "该模拟账户可用现金为1000元。"
    with environment.store.connect() as connection:
        after = [tuple(row) for row in connection.execute("SELECT * FROM journal ORDER BY seq")]
    assert before == after


def test_public_cli_rejects_economic_permissions_without_exposing_raw_input(
    environment, tmp_path
) -> None:
    import json

    from typer.testing import CliRunner

    from astock.investor_orchestration.cli import app

    request, _, _, _, _, artifact_id = good_run(environment)
    request = request.model_copy(
        update={
            "normalized_intent": RequestIntent.PAPER_CONFIRM,
            "side_effect": SideEffectClass.PT_CONFIRM,
            "raw_text": "recorded-private-input-must-not-echo",
        }
    )
    source = tmp_path / "prohibited-operation.json"
    source.write_text(
        json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "artifacts": {"PAPER": [artifact_id]},
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "execute-registered",
            str(source),
            "--current",
            "--database",
            str(environment.store.path),
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "UNAVAILABLE"
    assert request.raw_text not in result.stdout
    assert "Traceback" not in result.stdout


def test_public_cli_does_not_create_an_empty_database_for_missing_state(tmp_path) -> None:
    from typer.testing import CliRunner

    from astock.investor_orchestration.cli import app

    path = tmp_path / "must-not-be-created.sqlite"
    result = CliRunner().invoke(app, ["publish", "unavailable", "--database", str(path)])
    assert result.exit_code == 2
    assert not path.exists()
