from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import typer
from typer.testing import CliRunner

from astock.core.hashing import canonical_json_bytes
from astock.providers.forecasts import SampledForecastAggregate
from astock.providers.tushare_reference import TushareReferenceProvider, load_metric_capture
from astock.providers.tushare_structured import DEFAULT_FIELDS
from astock.research.data_cli import register_research_data_commands
from astock.settings import ProjectPaths
from tests.unit.test_sampled_forecasts import capture, report
from tests.unit.test_tushare_reference import NOW, ROOT


def app_for(state, objects):
    app = typer.Typer()
    emitted = []
    paths = ProjectPaths(
        root=ROOT, runtime=state.path.parent, objects=objects.root,
        parquet=state.path.parent / "parquet", manifests=state.path.parent / "manifests",
        state_db=state.path,
    )
    register_research_data_commands(app, lambda: (paths, state, objects), emitted.append)
    return app, emitted


@pytest.mark.parametrize("endpoint,command,subject,period,row_values", [
    ("daily_basic", ["tushare-valuation-capture", "600519", "XSHG", "2026-11-20"],
     "XSHG:600519", "20261120",
     {"ts_code": "600519.SH", "trade_date": "20261120", "close": "10", "total_share": "2"}),
    ("cn_gdp", ["tushare-macro-capture", "cn_gdp", "2026Q3"],
     "CN", "2026Q3", {"quarter": "2026Q3", "gdp": "100"}),
])
def test_capture_cli_emits_registered_identity_loadable_from_original(
    object_store, state, monkeypatch, endpoint, command, subject, period, row_values,
):
    monkeypatch.setenv("TUSHARE_TOKEN", "cli-test-token")
    row = {key: None for key in DEFAULT_FIELDS[endpoint]}
    row.update(row_values)
    payload = {"code": 0, "data": {"fields": list(row), "items": [list(row.values())]}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
    ) as client:
        provider = TushareReferenceProvider(
            object_store, state, project_root=ROOT, client=client, clock=lambda: NOW,
        )
        monkeypatch.setattr(
            "astock.providers.tushare_reference.TushareReferenceProvider",
            lambda *args, **kwargs: provider,
        )
        app, emitted = app_for(state, object_store)
        outcome = CliRunner().invoke(app, command)
    assert outcome.exit_code == 0, str(outcome.exception)
    identifier = emitted[0]["artifact_id"]
    loaded = load_metric_capture(
        state, object_store, identifier, subject_id=subject, as_of=NOW,
    )
    registered = state.artifact_record(identifier)
    assert registered is not None
    assert loaded.period == period and loaded == emitted[0]["capture"]
    assert object_store.get_bytes(registered["object_hash"]) == canonical_json_bytes(
        loaded.model_dump(mode="json")
    )


def test_forecast_aggregate_cli_emits_actual_registered_object(object_store, state):
    captured, _ = capture(
        object_store, state, [report("AP1", "A", 1), report("AP2", "B", 3)],
    )
    app, emitted = app_for(state, object_store)
    outcome = CliRunner().invoke(app, [
        "forecast-sample-aggregate", "600519", "XSHG", "2026",
        (datetime.now(UTC) + timedelta(seconds=1)).isoformat(), "2026-11-01", "2026-11-20",
        "--capture-id", captured.capture_id,
    ])
    assert outcome.exit_code == 0, str(outcome.exception)
    registered = state.artifact_record(emitted[0]["artifact_id"])
    assert registered is not None and registered["type"] == "SampledForecastAggregate"
    loaded = SampledForecastAggregate.model_validate_json(
        object_store.get_bytes(registered["object_hash"])
    )
    assert loaded == emitted[0]["aggregate"]
    assert loaded.mean == Decimal(2) and loaded.institution_count == 2



def test_original_cli_uses_production_batch_worker_and_same_continuation(tmp_path, monkeypatch):
    from tests.unit.test_web_original_resolution import _owned_original_batch

    continuation, acquisition, _web, record, _responses, originals, state, objects = (
        _owned_original_batch(tmp_path)
    )
    monkeypatch.setattr(
        "astock.research.continuation.CurrentResearchContinuationService",
        lambda *args, **kwargs: continuation,
    )
    selection_file = tmp_path / "original-selection.json"
    selection_file.write_text(originals[0].model_dump_json(), encoding="utf-8")
    app, emitted = app_for(state, objects)
    runner = CliRunner()
    outcome = runner.invoke(app, ["web-original-resolve", str(selection_file)])
    assert outcome.exit_code == 0, str(outcome.exception)
    assert emitted[-1]["continuation_id"] == record.continuation_id
    assert emitted[-1]["status"] == "TEAM_RESEARCH_REQUIRED"
    assert acquisition.calls == 2 and not emitted[-1]["investor_view_allowed"]
    assert len(emitted[-1]["automatic_resolution_artifact_ids"]) == 1
    again = runner.invoke(app, ["web-original-resolve", str(selection_file)])
    assert again.exit_code == 0, str(again.exception)
    assert acquisition.calls == 2
    assert any(item["status"] == "ALREADY_APPLIED" for item in emitted[-1]["web_operations"])
