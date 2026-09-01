from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from astock.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_report_schema_preferences_publish_status_and_recover(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "运行目录"
    published = tmp_path / "正式报告"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("ASTOCK_REPORT_ROOT", str(published))

    initialized = runner.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert initialized.exit_code == 0, initialized.output

    schema = runner.invoke(app, ["report-schema"])
    assert schema.exit_code == 0, schema.output
    schema_payload = json.loads(schema.output)
    assert schema_payload["schema_version"] == "formal-report-cli-schema-v1"
    assert "ReportRequest" in schema_payload["models"]
    assert "PresentationPreferences" in schema_payload["models"]
    assert schema_payload["broker_execution_allowed"] is False

    base = runner.invoke(app, ["preference-set", "DEFAULT_REPORT_FORMAT", "DOCX"])
    assert base.exit_code == 0, base.output
    assert json.loads(base.output)["layer"] == "BASE"

    overridden = runner.invoke(
        app,
        ["preference-set", "DEFAULT_REPORT_FORMAT", "MD", "--override"],
    )
    assert overridden.exit_code == 0, overridden.output
    assert json.loads(overridden.output)["layer"] == "OVERRIDE"

    preference = runner.invoke(app, ["preference-get", "DEFAULT_REPORT_FORMAT"])
    assert preference.exit_code == 0, preference.output
    assert json.loads(preference.output)["value"] == "MD"

    deleted = runner.invoke(app, ["preference-delete", "DEFAULT_REPORT_FORMAT"])
    assert deleted.exit_code == 0, deleted.output
    assert json.loads(deleted.output)["status"] == "OVERRIDE_DELETED"

    preference = runner.invoke(app, ["preference-get", "DEFAULT_REPORT_FORMAT"])
    assert preference.exit_code == 0, preference.output
    assert json.loads(preference.output)["value"] == "DOCX"

    request_path = tmp_path / "report.json"
    request_path.write_text(
        json.dumps(
            {
                "request_id": "cli-report-1",
                "title": "贵州茅台 CLI 正式报告",
                "narrative": {
                    "subject": "贵州茅台（600519）",
                    "task_type": "DEEP_RESEARCH",
                    "headline": "当前更适合等待。",
                    "valuation_or_odds": ["估值参考 1400 元"],
                    "reasons": ["现金流质量稳定"],
                    "risks": ["需求低于预期"],
                    "change_conditions": ["盈利持续超预期且估值回落"],
                    "data_as_of": datetime(2026, 8, 31, 15, 0, tzinfo=UTC).isoformat(),
                    "citations": ["[S1]"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    published_result = runner.invoke(app, ["report-publish", str(request_path)])
    assert published_result.exit_code == 0, published_result.output
    payload = json.loads(published_result.output)
    assert payload["publish_status"] == "PUBLISHED"
    assert payload["output_format"] == "DOCX"
    assert payload["public_reference"]["file_name"].endswith(".docx")
    assert str(published) not in published_result.output

    status = runner.invoke(app, ["report-status", payload["report_key"]])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["publish_status"] == "PUBLISHED"

    recovered = runner.invoke(app, ["report-recover", payload["report_key"]])
    assert recovered.exit_code == 0, recovered.output
    assert json.loads(recovered.output)["recovered_existing"] is True

    reset = runner.invoke(app, ["preference-reset", "DEFAULT_REPORT_FORMAT"])
    assert reset.exit_code == 0, reset.output
    assert json.loads(reset.output)["status"] == "RESET"
