from __future__ import annotations

import json
from pathlib import Path

import pytest

from astock.core.codex_runs import CodexRunService, build_context_budget
from astock.core.errors import PolicyError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore


def service(tmp_path: Path, state: StateStore) -> CodexRunService:
    return CodexRunService(tmp_path / "runtime", ObjectStore(tmp_path / "objects"), state)


def test_context_budget_deduplicates_chinese_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "研究工件.json"
    artifact.write_text("证据", encoding="utf-8")
    report = build_context_budget(
        skills=["holding-monitor", "holding-monitor"],
        artifact_paths=[artifact, artifact],
    )
    assert report.selected_skills == ["holding-monitor"]
    assert report.artifact_byte_size == len("证据".encode())
    assert report.duplicate_inputs_avoided == [str(artifact.resolve())]


def test_codex_draft_is_validated_and_registered(tmp_path: Path, state: StateStore) -> None:
    runs = service(tmp_path, state)
    manifest = runs.initialize({"request": "plan context"})
    draft = tmp_path / "draft.json"
    draft.write_text(
        json.dumps(
            {
                "artifact_type": "ContextBudgetReport",
                "payload": {
                    "selected_skills": ["holding-monitor"],
                    "selected_artifacts": [],
                    "artifact_byte_size": 0,
                    "estimated_text_tokens": 0,
                    "full_documents_to_open": [],
                    "evidence_excerpts_to_open": [],
                    "expected_browser_steps": 0,
                    "expected_mcp_calls": 0,
                    "expected_api_calls": 1,
                    "duplicate_inputs_avoided": [],
                },
                "citations": {},
                "requested_commands": [],
            }
        ),
        encoding="utf-8",
    )
    runs.stage_draft(manifest.run_id, draft)
    report = runs.import_draft(manifest.run_id)
    assert report.valid
    assert report.artifact_hash is not None
    assert (
        tmp_path / "runtime" / "codex_runs" / manifest.run_id / "validated_result.json"
    ).is_file()
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == 1


def test_codex_import_rejects_direct_commands(tmp_path: Path, state: StateStore) -> None:
    runs = service(tmp_path, state)
    manifest = runs.initialize({"request": "buy now"})
    draft = tmp_path / "unsafe.json"
    draft.write_text(
        json.dumps(
            {
                "artifact_type": "ContextBudgetReport",
                "payload": {},
                "citations": {},
                "requested_commands": [{"type": "real_order", "symbol": "600519"}],
            }
        ),
        encoding="utf-8",
    )
    runs.stage_draft(manifest.run_id, draft)
    with pytest.raises(PolicyError):
        runs.import_draft(manifest.run_id)
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == 0
