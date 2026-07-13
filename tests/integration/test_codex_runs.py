from __future__ import annotations

import json
from pathlib import Path

import pytest

from astock.core.codex_runs import CodexRunService, build_context_budget
from astock.core.errors import PolicyError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import RunManifest, RunStatus


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
    run_dir = tmp_path / "runtime" / "codex_runs" / manifest.run_id
    expected_files = {
        "request.json",
        "input_manifest.json",
        "context_budget.json",
        "run_manifest.json",
        "result_draft.json",
        "validated_result.json",
        "citations.json",
        "run_summary.md",
    }
    assert {path.name for path in run_dir.iterdir()} == expected_files
    stored_manifest = RunManifest.model_validate_json(
        (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert stored_manifest.status is RunStatus.SUCCEEDED
    assert stored_manifest.artifact_hashes == [report.artifact_hash]
    repeated = runs.import_draft(manifest.run_id)
    assert repeated.valid
    assert repeated.artifact_hash == report.artifact_hash
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


def test_codex_import_requires_nonempty_locator_for_every_evidence_id(
    tmp_path: Path,
    state: StateStore,
) -> None:
    runs = service(tmp_path, state)
    manifest = runs.initialize({"request": "review holding"})
    draft = tmp_path / "uncited.json"
    draft.write_text(
        json.dumps(
            {
                "artifact_type": "HoldingReviewPack",
                "payload": {
                    "position_id": "paper:600519",
                    "as_of": "2026-07-13T15:00:00+08:00",
                    "new_market_data": [],
                    "new_disclosures": [],
                    "new_regulatory_events": [],
                    "new_industry_data": [],
                    "new_news_leads": [],
                    "manual_evidence_updates": [],
                    "thesis_strength_change": "UNCHANGED",
                    "risk_change": "UNCHANGED",
                    "triggered_rules": [],
                    "unresolved_conflicts": [],
                    "recommended_action": "REVIEW",
                    "action_confidence": 0.5,
                    "evidence_ids": ["evidence-1"],
                    "next_review_conditions": ["new official disclosure"],
                },
                "citations": {"evidence-1": ""},
                "requested_commands": [],
            }
        ),
        encoding="utf-8",
    )
    runs.stage_draft(manifest.run_id, draft)
    report = runs.import_draft(manifest.run_id)
    assert not report.valid
    assert report.errors == ["Empty citation locators for evidence IDs: evidence-1"]
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == 0
