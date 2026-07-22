from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from astock.candidates import (
    CandidateParquetStore,
    CandidateRepository,
    CandidateScanService,
    CandidateTestInputVerifier,
    load_candidate_scan_config,
)
from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.candidates import CandidateScanRequest
from tests.unit.test_candidate_scan import _release

PROJECT_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_candidate_scan_status_and_audit_cli(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "候选CLI运行时"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(runtime / "objects" / "sha256")
    service = CandidateScanService(
        CandidateRepository(state),
        objects,
        CandidateParquetStore(runtime / "data" / "parquet" / "candidates"),
        load_candidate_scan_config(PROJECT_ROOT / "configs" / "candidate_scan.yaml"),
        CandidateTestInputVerifier(objects),
    )
    monkeypatch.setattr(
        "astock.cli._candidate_scan_service",
        lambda paths, state, objects: service,
    )
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(state, objects, "release:cli", as_of)
    release_hash = service.stage_input_release(release)
    request = CandidateScanRequest(
        created_at=as_of,
        request_id="request:cli",
        input_release_id=release.input_release_id,
        input_release_object_hash=release_hash,
        as_of=as_of,
    )
    request_path = tmp_path / "候选请求.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")

    scanned = runner.invoke(app, ["candidate-scan", str(request_path)])
    assert scanned.exit_code == 0, scanned.output
    report = json.loads(scanned.output)
    assert report["status"] == "SUCCEEDED"

    scan_status = runner.invoke(app, ["candidate-status", "--scan-id", report["scan_id"]])
    assert scan_status.exit_code == 0, scan_status.output
    assert json.loads(scan_status.output)["records"][0]["lifecycle_status"] == "RESEARCH_READY"

    company_status = runner.invoke(
        app,
        ["candidate-status", "--company-id", "company:600519"],
    )
    assert company_status.exit_code == 0, company_status.output
    assert json.loads(company_status.output)["record"]["company_id"] == "company:600519"

    audited = runner.invoke(app, ["candidate-audit", report["scan_id"]])
    assert audited.exit_code == 0, audited.output
    assert json.loads(audited.output)["status"] == "PASS"


def test_candidate_status_requires_exactly_one_selector(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    missing = runner.invoke(app, ["candidate-status"])
    assert missing.exit_code == 2
    assert json.loads(missing.output)["failure_code"] == "CANDIDATE_STATUS_FAILED"
