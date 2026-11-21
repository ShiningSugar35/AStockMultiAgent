import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import typer
from typer.testing import CliRunner

from astock.core.object_store import ObjectStore
from astock.research.debate_evaluation import load_quality_policy
from astock.research.quality_cli import QualityServices, register_commands

ROOT = Path(__file__).resolve().parents[2]


class ReadState:
    def __init__(self):
        self.rows = {}

    def artifact_record(self, identity):
        return self.rows.get(identity)


def app_for(tmp_path):
    state = ReadState()
    objects = ObjectStore(tmp_path / "objects")
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    services = QualityServices(
        cast(Any, state), objects, SimpleNamespace(), cast(Any, SimpleNamespace()), policy
    )
    app = typer.Typer()
    register_commands(
        app, services, lambda value: typer.echo(json.dumps(value, ensure_ascii=False))
    )
    return app, state, objects


def test_unregistered_approved_json_cannot_create_recommendation(tmp_path):
    app, _, _ = app_for(tmp_path)
    request = {
        "receipt_artifact_id": "unregistered",
        "instrument_id": "XSHG:600000",
        "benchmark_id": "XSHG:000001",
        "evaluated_at": "2026-11-13T16:00:00+08:00",
        "holding_sessions": 5,
        "context_id": "test",
    }
    result = CliRunner().invoke(app, ["research-outcome-feedback", json.dumps(request)])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "REGISTERED_ARTIFACT_TYPE_MISMATCH"
    request["approved"] = True
    result = CliRunner().invoke(app, ["research-outcome-feedback", json.dumps(request)])
    assert result.exit_code == 2
    assert "approved" not in result.stdout


def test_private_validation_input_not_echoed(tmp_path):
    app, _, _ = app_for(tmp_path)
    private = "PRIVATE_ACCOUNT_D:/secret/holdings"
    result = CliRunner().invoke(app, ["research-session-brief", json.dumps({"now": private})])
    assert result.exit_code == 2
    assert private not in result.stdout
    assert "Traceback" not in result.stdout
    assert json.loads(result.stdout)["status"] == "ERROR"


def test_object_integrity_checked_before_decode(tmp_path):
    app, state, objects = app_for(tmp_path)
    ref = objects.put_json({"schema_version": "recommendation-research-receipt-v1"})
    state.rows["r"] = {
        "type": "RecommendationResearchReceipt",
        "schema_version": "recommendation-research-receipt-v1",
        "object_hash": ref.sha256,
    }
    ref.path.write_bytes(b"corrupt")
    request = {
        "receipt_artifact_id": "r",
        "instrument_id": "XSHG:600000",
        "benchmark_id": "XSHG:000001",
        "evaluated_at": "2026-11-13T16:00:00+08:00",
        "holding_sessions": 5,
        "context_id": "test",
    }
    result = CliRunner().invoke(app, ["research-outcome-feedback", json.dumps(request)])
    assert result.exit_code == 2
    assert str(tmp_path) not in result.stdout
    assert json.loads(result.stdout)["code"] == "QUALITY_INPUT_OR_CANONICAL_READ_FAILED"


def test_real_full_research_receipt_handles_suffix_identity_and_account_scope(tmp_path):
    from datetime import timedelta

    from astock.investor_orchestration.full_research import FullResearchRecommendationService
    from astock.market_data.official_calendar import load_official_trading_calendar
    from astock.research.outcome_feedback import evaluate_outcome
    from astock.research.session_brief import build_session_brief
    from tests.unit.test_full_research_recommendation import NOW, _build_receipt, _request

    item = _build_receipt(
        FullResearchRecommendationService(),
        _request("分析600001现在是否可以买入", intent="FULL_RESEARCH_RECOMMENDATION"),
    )
    assert item.publication.formal_recommendation_allowed
    identity = item.candidate_universe[0]
    assert identity == "600001.XSHG"
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    calendar = load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml")
    arguments: dict[str, Any] = dict(
        instrument_id="XSHG:600001",
        account_id=item.request_contract.account_id,
        context_id="real-schema",
        now=NOW + timedelta(minutes=1),
        calendar=calendar,
        policy=policy,
        receipt=item,
    )
    result = build_session_brief(**arguments)
    assert result["source_facts"]["current"]["receipt_id"] == item.receipt_id
    assert result["delivery_status"] == "DRAFT" and result["public_approved"] is False
    assert "已发布研究观点" in result["text"]
    assert len(result["text"].splitlines()) > 1
    import pytest

    from astock.research.debate_evaluation import QualityError

    with pytest.raises(QualityError, match="ACCOUNT_MISMATCH"):
        build_session_brief(**{**arguments, "account_id": "different-account"})
    stale = build_session_brief(**{**arguments, "now": NOW + timedelta(days=20)})
    assert stale["source_facts"]["current"] is None
    immature = evaluate_outcome(
        item,
        "XSHG:600001",
        "XSHG:000001",
        (),
        (),
        calendar,
        policy,
        evaluated_at=NOW + timedelta(minutes=1),
        holding_sessions=5,
        context_id="real-schema",
    )
    assert immature["status"] == "IMMATURE"


def test_public_brief_reuses_registered_receipt_and_response_gateway(tmp_path):
    from datetime import timedelta

    from astock.investor_orchestration.full_research import FullResearchRecommendationService
    from astock.market_data.official_calendar import load_official_trading_calendar
    from tests.unit.test_full_research_recommendation import NOW, _build_receipt, _request

    receipt = _build_receipt(
        FullResearchRecommendationService(),
        _request("分析600001现在是否可以买入", intent="FULL_RESEARCH_RECOMMENDATION"),
    )
    assert receipt.publication.formal_recommendation_allowed
    state = ReadState()
    objects = ObjectStore(tmp_path / "objects")
    stored = objects.put_json(receipt.model_dump(mode="json"))
    state.rows["registered-formal-receipt"] = {
        "type": "RecommendationResearchReceipt",
        "schema_version": receipt.schema_version,
        "object_hash": stored.sha256,
        "input_hashes": (),
    }
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    calendar = load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml")
    services = QualityServices(cast(Any, state), objects, SimpleNamespace(), calendar, policy)
    app = typer.Typer()
    register_commands(
        app, services, lambda value: typer.echo(json.dumps(value, ensure_ascii=False))
    )
    request = {
        "instrument_id": "XSHG:600001",
        "account_id": receipt.request_contract.account_id,
        "context_id": "qualified-public-brief",
        "now": (NOW + timedelta(minutes=1)).isoformat(),
        "receipt_artifact_id": "registered-formal-receipt",
    }
    public = CliRunner().invoke(app, ["research-session-brief-public", json.dumps(request)])
    assert public.exit_code == 0, public.output
    output = json.loads(public.output)
    assert output["status"] == "PUBLIC_PRESENTATION_READY"
    assert output["receipt_id"] == receipt.receipt_id
    assert output["public_audit_passed"] is True
    assert output["notification_sent"] is False
    assert output["paper_ledger_write_allowed"] is False
    assert "已发布研究观点" in output["presentation_text"]
    assert "Traceback" not in public.output

    # Removing the formal receipt never falls back to presenting an unverified draft.
    del state.rows["registered-formal-receipt"]
    missing = CliRunner().invoke(app, ["research-session-brief-public", json.dumps(request)])
    assert missing.exit_code == 2
    assert json.loads(missing.output)["code"] == "REGISTERED_ARTIFACT_TYPE_MISMATCH"
    request.pop("receipt_artifact_id")
    unbound = CliRunner().invoke(app, ["research-session-brief-public", json.dumps(request)])
    assert unbound.exit_code == 2
    assert json.loads(unbound.output)["code"] == "PUBLICATION_NOT_ADMITTED"


def test_review_task_consumes_registered_independent_cases_and_frozen_evidence(tmp_path):
    from tests.unit.test_debate_evaluation import inputs

    policy, bull, bear, evidence, proposal = inputs()
    app, state, objects = app_for(tmp_path)

    def store(name: str, value: Any, *, hashes: tuple[str, ...] = ()) -> str:
        ref = objects.put_json(value.model_dump(mode="json"))
        state.rows[name] = {
            "type": type(value).__name__,
            "schema_version": value.schema_version,
            "object_hash": ref.sha256,
            "input_hashes": hashes,
        }
        return ref.sha256

    captured_hash = store("common-source", evidence)
    store("independent-bull", bull, hashes=(captured_hash,))
    store("independent-bear", bear, hashes=(captured_hash,))
    payload = {
        "bull_artifact_id": "independent-bull",
        "bear_artifact_id": "independent-bear",
        "evidence_artifact_id": "common-source",
        "proposal": proposal.model_dump(mode="json"),
    }
    result = CliRunner().invoke(app, ["research-debate-evaluate", json.dumps(payload)])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["result"]["status"] == "DISPUTED"
    assert len(output["result"]["reviewer_questions"]) == 1
    assert len(output["result"]["incremental_tasks"]) <= 2
    assert output["result"]["publication_authority"] is False
    assert output["persistent_writes"] is False

    # Source equality by date is not enough; each independent case must
    # explicitly bind to the same registered source object hash.
    state.rows["independent-bear"]["input_hashes"] = ()
    rejected = CliRunner().invoke(app, ["research-debate-evaluate", json.dumps(payload)])
    assert rejected.exit_code == 2
    assert json.loads(rejected.output)["code"] == "RESEARCH_EVIDENCE_LINEAGE_MISMATCH"
