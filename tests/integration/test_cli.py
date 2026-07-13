from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from astock.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_cli_init_probe_and_context_plan(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "中文运行目录"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    initialized = runner.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert initialized.exit_code == 0, initialized.output
    assert json.loads(initialized.output)["database_integrity"] == "ok"

    repeated = runner.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert repeated.exit_code == 0, repeated.output
    assert not json.loads(repeated.output)["account_initialized"]

    probed = runner.invoke(app, ["probe"])
    assert probed.exit_code == 0, probed.output
    probe = json.loads(probed.output)
    assert probe["python_supported"]
    assert probe["modes"]["OPENAI_COMPATIBLE_MODE"] == "OPTIONAL_DISABLED"
    assert {item["provider_id"] for item in probe["providers"]} == {
        "eastmoney-5m",
        "sina-5m",
    }

    artifact = tmp_path / "中文证据.json"
    artifact.write_text("{}", encoding="utf-8")
    planned = runner.invoke(
        app,
        [
            "context-plan",
            "--skill",
            "holding-monitor",
            "--artifact",
            str(artifact),
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert json.loads(planned.output)["artifact_byte_size"] == 2
