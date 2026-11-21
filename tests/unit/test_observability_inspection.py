"""Inspection is read-only; absent usage is not evidence of zero cost."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.diagnostics import DiagnosticError, inspect_report
from astock.research.observability import (
    AgentObservabilityService,
    summarize_evaluation_traces,
)
from astock.research.production_cli import register_research_production_commands
from astock.schemas.agent_observability import (
    ResearchEvaluationContext,
    ResearchRequestCacheMode,
    ResearchRequestSourceGroup,
    ResearchRequestTrace,
    ResearchRequestTraceStatus,
    ResearchResourceUsage,
)
from astock.settings import ProjectPaths

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)


def _paths(root: Path, runtime: Path | None = None) -> ProjectPaths:
    runtime = runtime or root / "runtime"
    return ProjectPaths(
        root=root, runtime=runtime, objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet", manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )


def _snapshot(root: Path) -> tuple[set[str], dict[str, str]]:
    # SQLite may create protocol sidecars even in mode=ro. Never delete them or
    # use immutable=1, which would hide committed writes still present in WAL.
    directories = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.is_file()
        and not p.name.endswith((".sqlite-wal", ".sqlite-shm"))
    }
    return directories, files


def _app(paths: ProjectPaths, *, allow_materialize: bool = False) -> tuple[typer.Typer, list[str]]:
    app = typer.Typer()
    writes: list[str] = []

    def services() -> tuple[ProjectPaths, StateStore, ObjectStore]:
        if not allow_materialize:
            raise AssertionError("inspection called the materializing service factory")
        writes.append("materialize")
        paths.ensure_directories()
        state = StateStore(paths.state_db, ROOT / "migrations")
        state.migrate()
        return paths, state, ObjectStore(paths.objects)

    def readers() -> tuple[ProjectPaths, StateStore, ObjectStore]:
        return (
            paths, StateStore(paths.state_db, ROOT / "migrations", read_only=True),
            ObjectStore(paths.objects),
        )

    def emit(value: Any) -> None:
        payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        print(json.dumps(payload, ensure_ascii=False))

    register_research_production_commands(app, services, emit, read_only_services=readers)
    return app, writes


@pytest.mark.parametrize("initial", ["missing", "empty_runtime", "empty_database"])
@pytest.mark.parametrize("command", [
    "agent-observability-report", "research-efficiency-report",
    "research-request-performance-report", "research-evaluation-report",
])
def test_inspection_does_not_initialize_or_materialize(
    tmp_path: Path, initial: str, command: str,
) -> None:
    paths = _paths(tmp_path)
    if initial != "missing":
        paths.runtime.mkdir()
    if initial == "empty_database":
        sqlite3.connect(paths.state_db).close()
    before = _snapshot(tmp_path)
    app, writes = _app(paths)
    result = CliRunner().invoke(app, [command])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] in {"NOT_INITIALIZED", "NOT_FOUND", "UNAVAILABLE"}
    assert payload["materialized"] is False
    assert writes == []
    assert _snapshot(tmp_path) == before


def test_explicit_materialize_and_inspect_use_the_same_runtime_override(tmp_path: Path) -> None:
    paths = _paths(ROOT, tmp_path / "isolated-runtime")
    app, writes = _app(paths, allow_materialize=True)
    runner = CliRunner()
    created = runner.invoke(app, ["agent-observability-report", "--materialize"])
    assert created.exit_code == 0, created.output
    report = json.loads(created.stdout)
    before = _snapshot(tmp_path)
    inspected = runner.invoke(app, [
        "agent-observability-report", "--report-id", report["report_id"],
    ])
    assert inspected.exit_code == 0, inspected.output
    payload = json.loads(inspected.stdout)
    assert payload["status"] == "FOUND"
    assert payload["report"] == report
    assert writes == ["materialize"]
    assert _snapshot(tmp_path) == before
    conflict = runner.invoke(app, [
        "agent-observability-report", "--materialize", "--report-id", report["report_id"],
    ])
    assert conflict.exit_code != 0
    assert writes == ["materialize"]


def _register_report(paths: ProjectPaths, raw: bytes) -> tuple[StateStore, str]:
    state = StateStore(paths.state_db, ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    ref = objects.put_bytes(raw)
    state.register_artifact(
        artifact_id="saved-report", artifact_type="AgentObservabilityReport",
        schema_version="test-v1", object_hash=ref.sha256, input_hashes=[],
    )
    return state, ref.sha256


@pytest.mark.parametrize(
    "failure", ["missing_object", "invalid_json", "wrong_identity", "wrong_schema"],
)
def test_registered_report_errors_are_not_misreported_as_missing_database(
    tmp_path: Path, failure: str,
) -> None:
    paths = _paths(tmp_path)
    payload = {"report_id": "saved-report", "schema_version": "test-v1"}
    if failure == "wrong_identity":
        payload["report_id"] = "another-report"
    if failure == "wrong_schema":
        payload["schema_version"] = "different-v1"
    raw = b"not-json-private-input" if failure == "invalid_json" else json.dumps(payload).encode()
    _, digest = _register_report(paths, raw)
    if failure == "missing_object":
        ObjectStore(paths.objects).path_for(digest).unlink()
    before = _snapshot(tmp_path)
    app, writes = _app(paths)
    result = CliRunner().invoke(app, ["agent-observability-report"])
    assert result.exit_code == 0, result.output
    inspected = json.loads(result.stdout)
    assert inspected["status"] == "UNAVAILABLE"
    assert "private-input" not in result.stdout
    assert writes == []
    assert _snapshot(tmp_path) == before


def test_inspection_rejects_unexpected_type_and_never_substitutes_report(tmp_path: Path) -> None:
    with pytest.raises(DiagnosticError, match="UNSUPPORTED_REPORT_TYPE"):
        inspect_report(tmp_path, "ExternalAccountSnapshot")
    paths = _paths(tmp_path)
    _register_report(paths, json.dumps({
        "report_id": "saved-report", "schema_version": "test-v1",
    }).encode())
    assert inspect_report(tmp_path, "SkillEfficiencyReport")["status"] == "NOT_FOUND"
    assert inspect_report(
        tmp_path, "AgentObservabilityReport", report_id="missing"
    )["status"] == "NOT_FOUND"


def test_read_only_store_rejects_writes_but_sees_committed_wal(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    writer = StateStore(database, ROOT / "migrations")
    writer.migrate()
    reader = StateStore(database, ROOT / "migrations", read_only=True)
    with closing(writer.connect()) as active:
        active.execute("CREATE TABLE readonly_probe(value INTEGER)")
        active.execute("INSERT INTO readonly_probe VALUES (37)")
        with closing(reader.connect()) as connection:
            assert connection.execute("SELECT value FROM readonly_probe").fetchone()[0] == 37
            assert connection.total_changes == 0
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("INSERT INTO readonly_probe VALUES (99)")
        assert active.execute("SELECT COUNT(*) FROM readonly_probe").fetchone()[0] == 1
    with pytest.raises(PermissionError, match="READ_ONLY_STATE"):
        reader.migrate()
    with pytest.raises(PermissionError, match="READ_ONLY_STATE"):
        with reader.transaction():
            pytest.fail("read-only transaction must not yield")
    absent = tmp_path / "absent" / "state.sqlite"
    with pytest.raises(FileNotFoundError):
        StateStore(absent, read_only=True).connect()
    assert not absent.parent.exists()


def _trace(
    request_id: str, variant: str, *, seconds: int = 10, replicate: str = "1",
    budget: str = "b" * 64, cache: ResearchRequestCacheMode = ResearchRequestCacheMode.COLD,
    evidence_mode: str = "RECORDED", usage: ResearchResourceUsage | None = None,
) -> ResearchRequestTrace:
    context = ResearchEvaluationContext.model_validate({
        "case_id": "business:1", "input_hash": "a" * 64, "evidence_hash": "e" * 64,
        "budget_hash": budget, "rubric_hash": "f" * 64, "code_revision": variant + "-revision",
        "policy_version": "policy-v1", "host_id": "recorded-host", "model_id": "recorded-model",
        "variant": variant, "replicate_id": replicate, "evidence_mode": evidence_mode,
    })
    return ResearchRequestTrace(
        trace_id="AUTO", request_id=request_id, status=ResearchRequestTraceStatus.COMPLETED,
        source_group=ResearchRequestSourceGroup.NORMAL, cache_mode=cache,
        started_at=NOW, answer_ready_at=NOW + timedelta(seconds=seconds),
        useful_output=True, usage=usage, evaluation=context, created_at=NOW,
    )


def test_unknown_usage_stays_unknown_and_observed_zero_is_preserved() -> None:
    baseline = _trace("before", "baseline")
    candidate = _trace("after", "candidate", seconds=8, usage=ResearchResourceUsage(
        source="HOST_REPORTED", input_tokens=0, output_tokens=0, cost_cny=0,
    ))
    report = summarize_evaluation_traces([baseline, candidate])
    groups = {row["variant"]: row for row in report["groups"]}
    assert groups["baseline"]["measurements"]["input_tokens"]["total"] is None
    assert groups["baseline"]["measurements"]["input_tokens"]["unknown_count"] == 1
    assert groups["candidate"]["measurements"]["input_tokens"]["total"] == 0
    assert report["pairs"][0]["deltas"]["wall_time_ms"] == -2000
    assert report["pairs"][0]["deltas"]["input_tokens"] is None
    assert report["quality_comparison"] == "REQUIRES_INDEPENDENT_REVIEW"


@pytest.mark.parametrize("changed", [
    {"budget": "c" * 64}, {"cache": ResearchRequestCacheMode.HOT},
    {"evidence_mode": "LIVE"}, {"evidence_mode": "FAULT_INJECTION"},
])
def test_unmatched_budget_cache_or_evidence_is_not_paired(changed: dict[str, Any]) -> None:
    report = summarize_evaluation_traces([
        _trace("before", "baseline"), _trace("after", "candidate", **changed),
    ])
    assert report["paired_run_count"] == 0


def test_repeated_outputs_do_not_inflate_independent_input_count() -> None:
    traces = [
        _trace(f"{variant}-{replicate}", variant, replicate=replicate)
        for variant in ("baseline", "candidate") for replicate in ("1", "2")
    ]
    report = summarize_evaluation_traces([*traces, traces[0]])
    assert report["trace_count"] == 4
    assert report["paired_run_count"] == 2
    assert report["distinct_paired_input_count"] == 1
    assert all(row["distinct_input_count"] == 1 for row in report["groups"])


def test_ambiguous_duplicate_and_conflicting_request_are_excluded() -> None:
    first = _trace("before", "baseline")
    report = summarize_evaluation_traces([
        first, _trace("another-before", "baseline"), _trace("after", "candidate"),
    ])
    assert report["duplicate_evaluation_unit_count"] == 1
    assert report["paired_run_count"] == 0
    conflict = summarize_evaluation_traces([
        first, first.model_copy(update={"useful_output": False}), _trace("after", "candidate"),
    ])
    assert conflict["conflicting_request_count"] == 1
    assert conflict["paired_run_count"] == 0


def test_legacy_trace_identity_is_unchanged_and_evaluation_is_read_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    writer = StateStore(paths.state_db, ROOT / "migrations")
    writer.migrate()
    objects = ObjectStore(paths.objects)
    service = AgentObservabilityService(
        writer, objects, project_root=ROOT, manifest_root=paths.manifests,
    )
    trace = _trace("legacy", "baseline").model_copy(update={"evaluation": None})
    semantic = trace.model_dump(
        mode="json", exclude={"trace_id", "created_at", "usage", "evaluation"},
    )
    stored = service.register_request_trace(trace)
    assert stored.trace_id == "research-request-trace:" + content_hash(semantic)
    assert service.register_request_trace(trace) == stored
    before = _snapshot(tmp_path)
    read_service = AgentObservabilityService(
        StateStore(paths.state_db, ROOT / "migrations", read_only=True),
        objects, project_root=ROOT, manifest_root=paths.manifests,
    )
    report = read_service.evaluation_report(lookback_days=0, limit=1)
    assert report["trace_count"] == 1 and report["unbound_trace_count"] == 1
    assert report["groups"] == [] and report["pairs"] == []
    assert report["materialized"] is False
    assert _snapshot(tmp_path) == before


def test_measured_usage_rejects_nonfinite_cost() -> None:
    with pytest.raises(ValueError):
        ResearchResourceUsage(source="HOST_REPORTED", cost_cny=float("nan"))


def test_host_metadata_retries_are_idempotent_without_generated_timestamps(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    state = StateStore(paths.state_db, ROOT / "migrations")
    state.migrate()
    service = AgentObservabilityService(
        state, ObjectStore(paths.objects), project_root=ROOT, manifest_root=paths.manifests,
    )
    payload = _trace("host-retry", "candidate", usage=ResearchResourceUsage(
        source="HOST_REPORTED", input_tokens=13,
    )).model_dump(mode="json", exclude={"created_at"})
    first = ResearchRequestTrace.model_validate_json(json.dumps(payload))
    second = ResearchRequestTrace.model_validate_json(json.dumps(payload))
    assert first.evaluation is not None and first.usage is not None
    assert "created_at" not in first.evaluation.model_dump()
    assert "created_at" not in first.usage.model_dump()
    stored = service.register_request_trace(first)
    assert service.register_request_trace(second) == stored
    # Registration replaces AUTO with a canonical ID; this is still one callback.
    summary = summarize_evaluation_traces([first, second, stored])
    assert summary["trace_count"] == 1
    assert summary["conflicting_request_count"] == 0


def test_budget_variants_are_not_independent_inputs_or_mixed_code_groups() -> None:
    traces = [
        _trace(f"{variant}-{budget}", variant, budget=budget * 64)
        for variant in ("baseline", "candidate") for budget in ("b", "c")
    ]
    report = summarize_evaluation_traces(traces)
    assert report["paired_run_count"] == 2
    assert report["distinct_paired_input_count"] == 1
    assert all(group["distinct_input_count"] == 1 for group in report["groups"])
    changed = _trace("new-code", "candidate", replicate="2")
    assert changed.evaluation is not None
    changed = changed.model_copy(update={
        "evaluation": changed.evaluation.model_copy(update={"code_revision": "different-revision"}),
    })
    groups = summarize_evaluation_traces([*traces, changed])["groups"]
    assert len(groups) == 3
    assert all(len(group["code_revisions"]) == 1 for group in groups)
