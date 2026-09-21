"""Exercise the production takeover entry point against isolated canonical state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import (
    KnowledgeCompletionRepository,
    RepositoryKnowledgeSkillProvider,
    _services,
    app,
)
from astock.core.hashing import content_hash
from astock.research.runtime import ResearchRunService
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.research.takeover_validation import validate_research_run_takeover
from astock.schemas.research_runtime import ResearchRunMode, ResearchRunRequest
from astock.schemas.research_sla import (
    LlmTakeoverResult,
    LlmTakeoverResultStatus,
    ResearchTaskCategory,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    paths, state, objects = _services()
    runtime = ResearchRunService(
        project_root=ROOT,
        state=state,
        objects=objects,
        reference_parquet_root=paths.parquet,
        knowledge_provider=RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state), objects
        ),
    )
    request = ResearchRunRequest(
        company_id="000001",
        as_of=datetime(2026, 9, 17, tzinfo=UTC),
        mode=ResearchRunMode.RECORDED_INPUT,
        auto_resolve_inputs=False,
        sync_reference_inputs=False,
    )
    report = runtime.run(request)
    artifact_id = f"ResearchRunReport:{report.report_id}"
    scheduler = CurrentResearchSlaService(state, objects)
    run = scheduler.create_run(
        request_id="cli-takeover",
        budget_seconds=120,
        task_specs=[
            (
                "company-run-0",
                "XSHE:000001",
                ResearchTaskCategory.NETWORK,
                [],
                content_hash(request.model_dump(mode="json", exclude={"created_at"})),
            )
        ],
    )

    def interrupted(_task):
        raise RuntimeError("injected interruption")

    scheduler.execute(run.run_id, {"company-run-0": interrupted})
    task = scheduler.tasks(run.run_id)[0]
    packet = scheduler.takeover_packet(
        request_id=run.request_id,
        task_id=task.task_id,
        trusted_artifact_ids=[],
        attempted_actions=[],
        missing_requirement="recover canonical research report",
        allowed_write_paths=[],
        expected_output_schema="ResearchRunReport",
        dependency_fingerprint=task.input_fingerprint,
    )
    result = LlmTakeoverResult(
        packet_id=packet.packet_id,
        request_id=run.request_id,
        run_id=run.run_id,
        task_id=task.task_id,
        generation=task.generation,
        dependency_fingerprint=task.input_fingerprint,
        status=LlmTakeoverResultStatus.RESOLVED,
        result_artifact_id=artifact_id,
    )
    return state, objects, runtime, scheduler, task, result, artifact_id


def test_cli_applies_only_audited_canonical_report_without_upgrading_research_quality(
    recovery, tmp_path
):
    state, objects, runtime, scheduler, task, result, artifact_id = recovery
    result_file = tmp_path / "takeover.json"
    result_file.write_text(result.model_dump_json(), encoding="utf-8")
    response = CliRunner().invoke(app, ["research-sla-takeover-apply", str(result_file)])
    assert response.exit_code == 0, (response.output, response.exception)
    assert json.loads(response.output)["status"] == "RUNNING"
    assert scheduler._task(task.task_id).result_artifact_id == artifact_id
    # A successful program repair does not fabricate the unavailable investment facts.
    record = state.artifact_record(artifact_id)
    assert record is not None
    assert json.loads(objects.get_bytes(str(record["object_hash"])))["status"] == "NEEDS_INFO"


@pytest.mark.parametrize("mutation", ["company", "request", "node"])
def test_domain_validator_rejects_other_context(recovery, mutation):
    state, objects, runtime, _, task, _, artifact_id = recovery
    changes = {
        "company": {"candidate_id": "XSHG:600000"},
        "request": {"input_fingerprint": "another-input"},
        "node": {"node_id": "valuation"},
    }
    bad_task = task.model_copy(update=changes[mutation])
    with pytest.raises(ValueError):
        validate_research_run_takeover(
            artifact_id, task=bad_task, state=state, objects=objects, audit_run=runtime.audit
        )


def test_cli_rejects_registered_type_name_with_invalid_payload(recovery, tmp_path):
    state, objects, _, scheduler, task, result, _ = recovery
    obj = objects.put_json({"schema_version": "research-run-report-v1"})
    state.register_artifact(
        artifact_id="ResearchRunReport:forged",
        artifact_type="ResearchRunReport",
        schema_version="research-run-report-v1",
        object_hash=obj.sha256,
        input_hashes=[],
    )
    result = result.model_copy(update={"result_artifact_id": "ResearchRunReport:forged"})
    result_file = tmp_path / "forged.json"
    result_file.write_text(result.model_dump_json(), encoding="utf-8")
    response = CliRunner().invoke(app, ["research-sla-takeover-apply", str(result_file)])
    assert response.exit_code != 0
    assert scheduler._task(task.task_id).status.value == "FAILED"
    with state.connect() as connection:
        packet = connection.execute(
            "SELECT status FROM research_llm_takeover WHERE packet_id=?", (result.packet_id,)
        ).fetchone()
    assert packet["status"] == "PENDING"
