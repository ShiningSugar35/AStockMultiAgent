"""Independent public-entry acceptance for bounded research retries and late results."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import _services, app
from astock.research.runtime import ResearchRunService
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.schemas.research_runtime import ResearchRunRequest

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 18, tzinfo=UTC)


@pytest.fixture
def batch_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    return tmp_path / "batch.json"


def _item(company_id="000001", exchange="XSHE", as_of="2026-09-17T14:00:00+00:00"):
    return {
        "instrument_id": f"{exchange}:{company_id}",
        "request": {
            "company_id": company_id,
            "as_of": as_of,
            "mode": "RECORDED_INPUT",
            "auto_resolve_inputs": False,
            "sync_reference_inputs": False,
        },
    }


def _invoke(path, requests, *, request_id="independent-batch-identity"):
    path.write_text(
        json.dumps({"request_id": request_id, "budget_seconds": 30, "requests": requests}),
        encoding="utf-8",
    )
    return CliRunner().invoke(app, ["research-run-batch", str(path)])


def _run_count():
    _, state, _ = _services()
    with state.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM research_scheduler_run").fetchone()[0]


def test_reordered_batch_reuses_original_run_deadline_and_successes(batch_environment):
    items = [_item(), _item("600000", "XSHG")]
    first = _invoke(batch_environment, items)
    assert first.exit_code == 0, (first.output, first.exception)
    original = json.loads(first.output)
    second = _invoke(batch_environment, list(reversed(items)))
    assert second.exit_code == 0, (second.output, second.exception)
    repeated = json.loads(second.output)
    assert repeated["scheduler_run"]["run_id"] == original["scheduler_run"]["run_id"]
    assert repeated["scheduler_run"]["deadline_at"] == original["scheduler_run"]["deadline_at"]
    assert _run_count() == 1
    assert {task["task_id"] for task in repeated["tasks"]} == {
        task["task_id"] for task in original["tasks"]
    }


@pytest.mark.parametrize("change", ["candidate", "as_of"])
def test_same_explicit_request_cannot_silently_allocate_a_fresh_budget(batch_environment, change):
    first = _invoke(batch_environment, [_item()])
    assert first.exit_code == 0, (first.output, first.exception)
    changed = (
        _item("600000", "XSHG")
        if change == "candidate"
        else _item(as_of="2026-09-18T00:00:00+00:00")
    )
    second = _invoke(batch_environment, [changed])
    assert second.exit_code != 0, (
        "the same caller request was silently assigned a new graph/deadline"
    )
    assert _run_count() == 1


def test_new_explicit_request_can_create_an_independent_graph(batch_environment):
    first = _invoke(batch_environment, [_item()], request_id="explicit-first")
    second = _invoke(batch_environment, [_item("600000", "XSHG")], request_id="explicit-second")
    assert first.exit_code == second.exit_code == 0
    assert _run_count() == 2


def test_deadline_rejected_company_result_never_becomes_a_watch_pool_result(
    batch_environment, monkeypatch
):
    """The batch projection must consume accepted tasks, not a worker's side dict."""
    now = [START]
    original_init = CurrentResearchSlaService.__init__
    original_run = ResearchRunService.run
    run_errors: list[str] = []

    def init_with_controlled_clock(self, state, objects, *, clock=None):
        original_init(self, state, objects, clock=clock or (lambda: now[0]))

    def return_after_deadline(self, request: ResearchRunRequest):
        # Run the actual canonical recorded path; only the scheduler clock is
        # advanced, deterministically reproducing a return after the deadline.
        try:
            report = original_run(self, request)
        except Exception as exc:
            run_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        now[0] = START + timedelta(seconds=31)
        return report

    original_execute = CurrentResearchSlaService.execute

    def execute_with_diagnostic(self, run_id, handlers, **kwargs):
        def observed(task):
            try:
                return handlers[task.node_id](task)
            except Exception as exc:
                run_errors.append(f"{type(exc).__name__}: {exc}")
                raise

        return original_execute(self, run_id, dict.fromkeys(handlers, observed), **kwargs)

    monkeypatch.setattr(CurrentResearchSlaService, "__init__", init_with_controlled_clock)
    monkeypatch.setattr(CurrentResearchSlaService, "execute", execute_with_diagnostic)
    monkeypatch.setattr(ResearchRunService, "run", return_after_deadline)

    # This test controls a synthetic clock and exercises the acceptance fence.
    # Real spawn/cancellation/death are separately tested with OS processes.
    class InlineControlledWorker:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def __call__(self, task):
            return self.target(*self.args)

    monkeypatch.setattr(
        "astock.research.process_worker.KillableResearchWorker", InlineControlledWorker
    )
    response = _invoke(batch_environment, [_item()], request_id="late-company-result")
    assert response.exit_code == 0, (response.output, response.exception)
    assert not run_errors, run_errors
    payload = json.loads(response.output)
    assert payload["scheduler_run"]["status"] == "CANCELLED", payload
    assert payload["tasks"][0]["status"] == "CANCELLED"
    assert payload["tasks"][0]["result_artifact_id"] is None
    assert all(target["latest_result_artifact_id"] is None for target in payload["targets"]), (
        "a rejected late result was promoted into canonical watch-pool state"
    )
    assert {target["state"] for target in payload["targets"]} == {"SUSPENDED"}
