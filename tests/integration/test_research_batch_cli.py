from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from astock.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def _request(company_id: str) -> dict[str, object]:
    return {
        "company_id": company_id,
        "as_of": "2026-09-17T14:00:00+00:00",
        "mode": "RECORDED_INPUT",
        "auto_resolve_inputs": False,
        "sync_reference_inputs": False,
    }


def test_research_run_batch_uses_scheduler_and_updates_existing_watch_pool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "batch-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    request_file = tmp_path / "batch.json"
    request_file.write_text(
        json.dumps(
            {
                "request_id": "cli-batch-smoke",
                "budget_seconds": 30,
                "requests": [
                    {"instrument_id": "XSHE:000001", "request": _request("000001")},
                    {"instrument_id": "XSHG:600000", "request": _request("600000")},
                ],
            }
        ),
        encoding="utf-8",
    )

    first = runner.invoke(
        app, ["research-run-batch", str(request_file), "--max-parallel-companies", "2"]
    )
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["scheduler_run"]["status"] == "COMPLETED"
    assert {item["status"] for item in payload["tasks"]} == {"COMPLETED"}
    assert set(payload["reports"]) == {"XSHE:000001", "XSHG:600000"}
    assert {item["status"] for item in payload["reports"].values()} == {"NEEDS_INFO"}
    assert {item["state"] for item in payload["targets"]} == {"REVIEW_DUE"}

    repeated = runner.invoke(
        app,
        ["research-run-batch", str(request_file), "--max-parallel-companies", "2"],
    )
    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["scheduler_run"]["run_id"] == payload["scheduler_run"]["run_id"]
    assert set(repeated_payload["reports"]) == {"XSHE:000001", "XSHG:600000"}
    assert {item["state"] for item in repeated_payload["targets"]} == {"REVIEW_DUE"}
