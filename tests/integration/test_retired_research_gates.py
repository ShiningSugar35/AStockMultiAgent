"""Current CLI schemas must not re-expose retired PIT or broker-permission gates."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from astock.cli import app

ROOT = Path(__file__).resolve().parents[2]


def test_retired_temporal_gate_commands_are_not_registered() -> None:
    commands = {command.name for command in app.registered_commands}
    assert not commands & {
        "pit-temporal-schema",
        "pit-temporal-audit",
        "pit-temporal-artifact-audit",
        "pit-knowledge-cutoff-diagnostic",
    }


def test_shadow_schema_does_not_restore_retired_gates(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    result = CliRunner().invoke(app, ["shadow-schema"])
    assert result.exit_code == 0, (result.output, result.exception)
    payload = json.loads(result.output)
    boundaries = payload["hard_boundaries"]
    assert "future_inputs_allowed" not in boundaries
    assert "not_pit_safe_formal_samples_allowed" not in boundaries
    assert "broker_execution_allowed" not in boundaries
    # This is provenance of measured samples, not a rule against current data.
    assert boundaries["historical_replay_can_count_as_forward"] is False
    assert boundaries["live_forward_snapshot_lineage_required"] is True
