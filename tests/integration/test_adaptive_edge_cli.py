from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from astock.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_adaptive_edge_diagnostic_cli_is_read_only_and_policy_driven(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "adaptive-edge-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    initialized = runner.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert initialized.exit_code == 0, initialized.output

    capability = runner.invoke(
        app,
        ["research-capability-status", "600989", "--market", "XSHG"],
    )
    assert capability.exit_code == 0, capability.output
    capability_payload = json.loads(capability.output)
    assert capability_payload["policy_version"] == "current-research-policy-v1"
    assert capability_payload["lookback_days"] == 120
    assert capability_payload["automatic_resolution_budget_seconds"] == 1800
    assert capability_payload["paper_ledger_write_allowed"] is False
    assert capability_payload["broker_execution_allowed"] is False

    dialect = runner.invoke(app, ["provider-dialect-status"])
    assert dialect.exit_code == 0, dialect.output
    dialect_payload = json.loads(dialect.output)
    providers = {item["provider_id"]: item for item in dialect_payload["providers"]}
    assert providers["sina-financial"]["dialect_version"] == "sina-finance-report-2026-v1"
    assert providers["eastmoney-financial"]["dialect_version"] == (
        "eastmoney-financial-datacenter-2026-v1"
    )

    adaptive = runner.invoke(app, ["adaptive-edge-status"])
    assert adaptive.exit_code == 0, adaptive.output
    adaptive_payload = json.loads(adaptive.output)
    assert adaptive_payload["current_research_policy"] == "current-research-policy-v1"
    assert adaptive_payload["specialist_default_budget"] == 3
    assert adaptive_payload["specialist_maximum_budget"] == 8
    assert adaptive_payload["paper_ledger_write_allowed"] is False
    assert adaptive_payload["broker_execution_allowed"] is False
    assert adaptive_payload["manual_last"] is True

    schemas = runner.invoke(app, ["adaptive-edge-schema"])
    assert schemas.exit_code == 0, schemas.output
    schema_payload = json.loads(schemas.output)
    assert "ResearchPlannerProposal" in schema_payload
    assert "ProviderRecoveryProposal" in schema_payload
    assert "SchemaRepairProposal" in schema_payload
    assert "ProviderDialectCandidateRelease" in schema_payload


def test_candidate_dialect_admission_requires_explicit_approval() -> None:
    rejected = runner.invoke(
        app,
        ["adaptive-schema-repair-admit", "schema-repair-validation:test"],
    )

    assert rejected.exit_code == 2
    assert "--approve is required" in rejected.output


def test_current_acquisition_cli_delegates_lookback_bounds_to_policy() -> None:
    help_result = runner.invoke(app, ["research-acquire-current", "--help"])

    assert help_result.exit_code == 0, help_result.output
    assert "--lookback-days" in help_result.output
    assert "30" not in help_result.output
    assert "730" not in help_result.output
    assert "--planner-plan-arti" in help_result.output
