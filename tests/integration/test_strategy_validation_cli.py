import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from astock.experiments.cli import main, register_commands
from astock.experiments.validation import Candidate, Request


def write_request(tmp_path):
    request = Request(
        study_id="offline",
        run_id="one",
        executor_version="fixture-v1",
        input_release_hash="a" * 64,
        benchmark_id="index",
        timestamps=tuple(datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(24)),
        candidates=(Candidate(candidate_id="one", strategy_version="v1", parameters={"n": 2}),),
        min_train_bars=4,
        test_bars=2,
        step_bars=2,
        fold_count=2,
        holdout_start=20,
        label_horizon_bars=1,
        purge_bars=1,
        embargo_bars=1,
    )
    path = tmp_path / "request.json"
    path.write_text(request.model_dump_json(), encoding="utf-8")
    return path


def test_standalone_plan_roundtrip(tmp_path, capsys):
    path = write_request(tmp_path)
    assert main(["plan", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["authority"] == "RESEARCH_ONLY"
    assert result["status"] == "PLAN_ONLY_NO_EVALUATION"
    assert len(result["plan"]["folds"]) == 2
    assert result["plan"]["final_holdout"] == {"start": 20, "stop": 24}


def test_standalone_rejects_oversize_and_unknown_authorization(tmp_path, capsys):
    path = write_request(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["production_authorized"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert main(["plan", str(path)]) == 2
    assert not capsys.readouterr().out
    path.write_bytes(b" " * 1048577)
    assert main(["plan", str(path)]) == 2
    assert "INVALID_OR_OVER_BUDGET_REQUEST" in capsys.readouterr().err


def test_extension_requires_host_integration_before_execution(tmp_path):

    class App:
        def __init__(self):
            self.commands = {}

        def command(self, name):
            def register(function):
                self.commands[name] = function
                return function

            return register

    app = App()
    output = []
    register_commands(app, SimpleNamespace(), output.append)
    path = write_request(tmp_path)
    app.commands["strategy-validation-plan"](str(path))
    assert output[0]["status"] == "PLAN_ONLY_NO_EVALUATION"
    assert output[0]["executable_backtest_available"] is False
    assert "strategy-validation-run" in app.commands
    assert "strategy-validation-diagnose" in app.commands
    assert len(output) == 1


@pytest.mark.parametrize("route", ["host", "standalone"])
def test_canonical_cli_runs_real_accounting_and_consumes_holdout(tmp_path, capsys, route):
    """End-to-end simulator check on synthetic bars, no market validity claim."""
    import typer

    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from tests.unit.test_strategy_validation import canonical_fixture, register_canonical

    metadata = InvestorOrchestrationStore(tmp_path / "isolated.sqlite")
    metadata.initialize()
    state = StateStore(metadata.path)
    objects = ObjectStore(tmp_path / "objects")
    release, plan = canonical_fixture()
    artifact = register_canonical(state, objects, release)
    path = tmp_path / "request.json"
    path.write_text(plan.request.model_dump_json(), encoding="utf-8")

    class App:
        commands = {}

        def command(self, name):
            def register(function):
                self.commands[name] = function
                return function

            return register

    app, output = App(), []
    register_commands(app, lambda: (None, state, objects), output.append)
    if route == "host":
        app.commands["strategy-validation-run"](str(path), artifact)
        report = output.pop()
    else:
        arguments = [
            "run",
            str(path),
            "--state-db",
            str(metadata.path),
            "--objects-dir",
            str(tmp_path / "objects"),
            "--market-release",
            artifact,
        ]
        assert main(arguments) == 0
        report = json.loads(capsys.readouterr().out)
        assert main(arguments) == 2
        assert not capsys.readouterr().out
    assert report["market_validation_claim"] is False
    assert report["paper_ledger_write_allowed"] is False
    assert report["trials"][-1]["query"]["phase"] == "FINAL_HOLDOUT"
    assert report["trials"][0]["metrics"][0]["net_compounded_return"] != "0"
    assert report["selection_receipt"].startswith("StrategySelectionStudyRun:")
    from astock.experiments.validation import digest

    report_hash = report.pop("report_hash")
    assert report_hash == digest(report)
    with pytest.raises(typer.Exit):
        app.commands["strategy-validation-run"](str(path), artifact)
    assert output.pop()["status"] == "UNAVAILABLE"
    with pytest.raises(typer.Exit):
        app.commands["strategy-validation-run"](str(path), "absent")
    assert output.pop()["status"] == "UNAVAILABLE"


def test_standalone_execution_requires_explicit_existing_stores(tmp_path, capsys):
    path = write_request(tmp_path)
    assert main(["run", str(path)]) == 2
    assert not capsys.readouterr().out
    absent = tmp_path / "must-not-be-created.sqlite"
    assert (
        main(
            [
                "run",
                str(path),
                "--state-db",
                str(absent),
                "--objects-dir",
                str(tmp_path),
                "--market-release",
                "absent",
            ]
        )
        == 2
    )
    assert not absent.exists()
