"""Exercise the public command wiring against isolated canonical state, offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from astock.cli import app
from astock.investor_orchestration.models import (
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ScheduledWindow,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService, policy_from_config
from astock.investor_orchestration.scheduled_input_coverage import (
    ScheduledInputAuditRequest,
    ScheduledSourceScope,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = CliRunner()


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("source audit commands must not fetch the network")

    monkeypatch.setattr(httpx.Client, "send", forbidden)


def request() -> ScheduledInputAuditRequest:
    cutoff = datetime.now(UTC) - timedelta(seconds=1)
    return ScheduledInputAuditRequest(
        run_id="source-cli-recorded",
        domain=ScheduledDomain.WATCHLIST,
        window=ScheduledWindow.POST_CLOSE,
        scope=ScheduledSourceScope(
            instrument_id="XSHG:600519",
            company_query="recorded-issuer",
            industry_query="recorded-industry",
            policy_families=(("NBS", "RECORDED_POLICY_SCOPE"),),
        ),
        interval_start=cutoff - timedelta(hours=2),
        interval_end=cutoff,
        as_of=cutoff,
    )


def test_schema_command_is_wired_and_does_not_create_a_state_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "not-created" / "state.sqlite"
    monkeypatch.setenv("ASTOCK_STATE_DB", str(database))
    result = RUNNER.invoke(app, ["investor", "schedule-input-schema"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value["request_schema"]["title"] == "ScheduledInputAuditRequest"
    assert value["run_schema"]["title"] == "ScheduledRunRequest"
    assert set(value["source_audit_policy"]["maximum_price_age_seconds"]) == {
        window.value for window in ScheduledWindow
    }
    assert not database.parent.exists()


def test_unknown_database_is_rejected_without_creating_its_parent_directory(tmp_path: Path) -> None:
    database = tmp_path / "must-not-be-created" / "state.sqlite"
    result = RUNNER.invoke(
        app,
        [
            "investor",
            "schedule-input-status",
            "not-a-report",
            "--database",
            str(database),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "already be initialized" in result.output
    assert not database.parent.exists()


def test_missing_input_checks_are_persisted_as_incomplete_and_replay_consistently(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    input_file = tmp_path / "input.json"
    input_file.write_text(request().model_dump_json(), encoding="utf-8")
    arguments = [
        "investor",
        "schedule-input-register",
        str(input_file),
        "--database",
        str(store.path),
    ]
    first = RUNNER.invoke(app, arguments)
    assert first.exit_code == 3, first.output
    payload = json.loads(first.stdout)
    report = payload["report"]
    assert report["all_families_checked"] is False
    assert report["formal_research_complete"] is False
    assert len(report["checks"]) == 5
    assert all(check["status"] == "MISSING" for check in report["checks"])
    second = RUNNER.invoke(app, arguments)
    assert second.exit_code == 3, second.output
    assert json.loads(second.stdout) == payload
    status = RUNNER.invoke(
        app,
        [
            "investor",
            "schedule-input-status",
            payload["artifact_id"],
            "--database",
            str(store.path),
        ],
    )
    assert status.exit_code == 3, status.output
    assert json.loads(status.stdout) == report
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM artifact_registry WHERE type='ScheduledInputCoverageReport'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0


def test_run_file_cannot_create_or_authorize_a_missing_binding(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    run = ScheduledRunRequest(
        run_id="source-cli-unbound",
        binding_id="not-approved",
        schedule_bucket="recorded-window",
        window=ScheduledWindow.POST_CLOSE,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=datetime.now(UTC),
        source_revision_set={},
        policy_hash="0" * 64,
        idempotency_key="source-cli-unbound",
    )
    file = tmp_path / "run.json"
    file.write_text(run.model_dump_json(), encoding="utf-8")
    result = RUNNER.invoke(
        app,
        [
            "investor",
            "schedule-run-file",
            str(file),
            "--database",
            str(store.path),
        ],
    )
    assert result.exit_code == 3, result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "BLOCKED"
    assert payload["source_coverage_complete"] is False
    assert payload["economic_write_count"] == 0
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduled_task_bindings").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0


def test_stage_and_pending_commands_preserve_one_consented_run_identity(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    policy = policy_from_config(config)
    service = ScheduledResearchService(store, InvestorSessionPreflightService(store))
    service.register_policy(policy)
    requested_at = datetime.now(UTC) - timedelta(seconds=2)
    binding = ScheduledTaskBinding(
        binding_id="stage-cli-binding",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_STAGE_TEST",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=requested_at - timedelta(minutes=1),
        consent_hash="stage-cli-consent",
    )
    service.register_binding(binding)
    run = ScheduledRunRequest(
        run_id="stage-cli-run",
        binding_id=binding.binding_id,
        schedule_bucket="2026-09-09:PRE_OPEN:09:10",
        window=ScheduledWindow.PRE_OPEN,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=requested_at,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key="stage-cli-run",
    )
    run_file = tmp_path / "pending-run.json"
    run_file.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    staged = RUNNER.invoke(
        app,
        ["investor", "schedule-run-stage", str(run_file), "--database", str(store.path)],
    )
    assert staged.exit_code == 0, staged.output
    staged_payload = json.loads(staged.stdout)
    assert staged_payload["status"] == "STAGED"
    assert staged_payload["checkpoint_status"] == "PENDING_INPUTS"
    assert staged_payload["request"]["requested_at"] == run.model_dump(mode="json")["requested_at"]

    pending = RUNNER.invoke(
        app,
        [
            "investor",
            "schedule-run-pending",
            binding.binding_id,
            run.schedule_bucket,
            "--database",
            str(store.path),
        ],
    )
    assert pending.exit_code == 0, pending.output
    pending_payload = json.loads(pending.stdout)
    assert pending_payload["status"] == "PENDING_INPUTS"
    assert ScheduledRunRequest.model_validate(pending_payload["request"]) == run

    repeated = RUNNER.invoke(
        app,
        ["investor", "schedule-run-stage", str(run_file), "--database", str(store.path)],
    )
    assert repeated.exit_code == 0, repeated.output
    assert store.completed_schedule_buckets(binding.binding_id) == set()


def test_stage_command_cannot_create_consent_for_an_unapproved_binding(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    config = yaml.safe_load(
        (PROJECT_ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    policy = policy_from_config(config)
    service = ScheduledResearchService(store, InvestorSessionPreflightService(store))
    service.register_policy(policy)
    requested_at = datetime.now(UTC) - timedelta(seconds=2)
    binding = ScheduledTaskBinding(
        binding_id="stage-cli-no-consent",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_STAGE_TEST",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=requested_at - timedelta(minutes=1),
        consent_hash=None,
    )
    service.register_binding(binding)
    run = ScheduledRunRequest(
        run_id="stage-cli-no-consent",
        binding_id=binding.binding_id,
        schedule_bucket="2026-09-09:PRE_OPEN:09:10",
        window=ScheduledWindow.PRE_OPEN,
        domains=(ScheduledDomain.WATCHLIST,),
        requested_at=requested_at,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key="stage-cli-no-consent",
    )
    run_file = tmp_path / "unapproved-run.json"
    run_file.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    result = RUNNER.invoke(
        app,
        ["investor", "schedule-run-stage", str(run_file), "--database", str(store.path)],
    )
    assert result.exit_code == 2, result.output
    assert "explicit scheduled consent" in result.output
    assert store.get_scheduled_checkpoint(binding.binding_id, run.schedule_bucket) is None
