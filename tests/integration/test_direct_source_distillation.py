from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.errors import DataQualityError
from astock.core.hashing import sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.direct_source_distillation_service import (
    DirectSourceDistillationService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "direct-run:synthetic"
PDF_BATCH = "b01"
DOCX_BATCH = "b02"
VISUAL_ID = "image-evidence:duplicate"


def _locator(
    kind: str,
    unit_index: int,
    text: str,
) -> dict[str, object]:
    return {
        "source_kind": kind,
        "unit_index": unit_index,
        "start_offset": 0,
        "end_offset": len(text),
    }


def _test_migrations(tmp_path: Path) -> Path:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in (
        "0001_foundation.sql",
        "0049_direct_source_distillation_state.sql",
    ):
        shutil.copy(PROJECT_ROOT / "migrations" / name, migrations / name)
    fixture = migrations / "0002_visual_evidence_fixture.sql"
    fixture.write_text(
        "CREATE TABLE book_image_evidence (\n"
        "  evidence_id TEXT PRIMARY KEY,\n"
        "  page_number INTEGER NOT NULL,\n"
        "  bbox_json TEXT NOT NULL,\n"
        "  image_object_hash TEXT,\n"
        "  duplicate_of_evidence_id TEXT REFERENCES book_image_evidence(evidence_id),\n"
        "  evidence_object_hash TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    return migrations


def _build_scope(tmp_path: Path) -> dict[str, Any]:
    state = StateStore(
        tmp_path / "状态.sqlite",
        _test_migrations(tmp_path),
    )
    state.migrate()
    objects = ObjectStore(tmp_path / "对象" / "sha256")
    pdf_source = objects.put_bytes(b"%PDF-synthetic-private-source")
    docx_source = objects.put_bytes(b"PK-synthetic-private-docx")
    texts = {
        "before": "前置上下文",
        "pdf": "xxslice rule bodyzz",
        "after": "后置上下文",
        "docx": "AAclauseBB",
    }
    text_hashes = {
        key: objects.put_bytes(value.encode("utf-8")).sha256
        for key, value in texts.items()
    }
    image = objects.put_bytes(b"synthetic-visual-evidence")
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO book_image_evidence("
            "evidence_id,page_number,bbox_json,image_object_hash,"
            "duplicate_of_evidence_id,evidence_object_hash"
            ") VALUES(?,?,?,?,?,?)",
            (
                "image-evidence:original",
                6,
                '{"x0":1,"y0":2,"x1":3,"y1":4}',
                image.sha256,
                None,
                "e" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO book_image_evidence("
            "evidence_id,page_number,bbox_json,image_object_hash,"
            "duplicate_of_evidence_id,evidence_object_hash"
            ") VALUES(?,?,?,?,?,?)",
            (
                VISUAL_ID,
                6,
                '{"x0":5,"y0":6,"x1":7,"y1":8}',
                None,
                "image-evidence:original",
                "f" * 64,
            ),
        )
    manifest = {
        "schema_version": "direct-source-run-init-v1",
        "run_id": RUN_ID,
        "pipeline_version": "direct-pipeline-v1",
        "sources": [
            {
                "source_id": "source:pdf",
                "source_kind": "PDF",
                "source_file_hash": pdf_source.sha256,
            },
            {
                "source_id": "source:docx",
                "source_kind": "DOCX",
                "source_file_hash": docx_source.sha256,
            },
        ],
        "batches": [
            {
                "batch_id": PDF_BATCH,
                "source_id": "source:pdf",
                "chapter_unit_id": "chapter:pdf",
                "ordinal": 1,
                "context_before": [
                    {
                        "fragment_id": "fragment:before",
                        "object_hash": text_hashes["before"],
                        "locator": _locator(
                            "PDF",
                            5,
                            texts["before"],
                        ),
                    }
                ],
                "current_fragments": [
                    {
                        "fragment_id": "fragment:pdf",
                        "object_hash": text_hashes["pdf"],
                        "locator": _locator(
                            "PDF",
                            6,
                            texts["pdf"],
                        ),
                    }
                ],
                "context_after": [
                    {
                        "fragment_id": "fragment:after",
                        "object_hash": text_hashes["after"],
                        "locator": _locator(
                            "PDF",
                            7,
                            texts["after"],
                        ),
                    }
                ],
                "visual_evidence_ids": [VISUAL_ID],
            },
            {
                "batch_id": DOCX_BATCH,
                "source_id": "source:docx",
                "chapter_unit_id": "chapter:docx",
                "ordinal": 2,
                "current_fragments": [
                    {
                        "fragment_id": "fragment:docx",
                        "object_hash": text_hashes["docx"],
                        "locator": _locator(
                            "DOCX",
                            3,
                            texts["docx"],
                        ),
                    }
                ],
            },
        ],
    }
    return {
        "state": state,
        "objects": objects,
        "service": DirectSourceDistillationService(state, objects),
        "manifest": manifest,
        "pdf_source_hash": pdf_source.sha256,
        "docx_source_hash": docx_source.sha256,
        "text_hashes": text_hashes,
        "texts": texts,
        "visual_hash": image.sha256,
    }


def _ready_semantics(
    primary: str,
    secondary: str,
) -> dict[str, object]:
    return {
        "skill_name": "Trace frozen evidence",
        "primary_module": primary,
        "secondary_modules": [secondary],
        "decision_question": "Does frozen evidence support the operating rule?",
        "core_principle": (
            "Use the precise cited slice before drawing a conclusion."
        ),
        "applicable_conditions": ["The source is frozen and auditable."],
        "reasoning_steps": ["Compare the cited fact with the stated condition."],
        "required_evidence": ["A precise immutable source slice."],
        "positive_signals": ["The cited condition is directly supported."],
        "negative_signals": ["The cited condition conflicts with the rule."],
        "invalidation_conditions": ["The source slice is contradicted."],
        "failure_modes": ["A locator is accepted without hash verification."],
        "confidence": 0.8,
        "status": "READY_FOR_SHADOW",
        "uncertainty_reason": None,
    }


def _needs_semantics() -> dict[str, object]:
    return {
        "skill_name": "Unresolved behavior rule",
        "primary_module": "PSYCHOLOGY_BEHAVIOR",
        "secondary_modules": [],
        "decision_question": (
            "Is the behavior rule bounded by a stated condition?"
        ),
        "core_principle": (
            "Retain the unresolved rule for explicit user review."
        ),
        "applicable_conditions": [],
        "reasoning_steps": [],
        "required_evidence": [],
        "positive_signals": [],
        "negative_signals": [],
        "invalidation_conditions": [],
        "failure_modes": [],
        "confidence": 0.4,
        "status": "NEEDS_USER_REVIEW",
        "uncertainty_reason": (
            "The frozen section omits the condition that limits this rule."
        ),
    }


def _source_ref(
    scope: dict[str, Any],
    kind: str,
) -> dict[str, object]:
    if kind == "PDF":
        cited = "slice"
        return {
            "source_file_hash": scope["pdf_source_hash"],
            "source_kind": "PDF",
            "page_number": 6,
            "locator": "pdf-page-6;normalized-page-text;chars=2:7",
            "source_object_hash": sha256_bytes(cited.encode("utf-8")),
            "visual_evidence_ids": [VISUAL_ID],
            "paragraph_head": cited,
        }
    cited = "clause"
    return {
        "source_file_hash": scope["docx_source_hash"],
        "source_kind": "DOCX",
        "paragraph_number": 3,
        "locator": (
            "docx-paragraph-3;normalized-paragraph-text;chars=2:8"
        ),
        "source_object_hash": sha256_bytes(cited.encode("utf-8")),
        "visual_evidence_ids": [],
        "paragraph_head": cited,
    }


def _public_batch(
    scope: dict[str, Any],
    packet: dict[str, Any],
    *,
    kind: str,
) -> dict[str, object]:
    is_pdf = kind == "PDF"
    primary = (
        "FUNDAMENTAL_RESEARCH" if is_pdf else "VALUATION_PRICING"
    )
    secondary = (
        "VALUATION_PRICING" if is_pdf else "FUNDAMENTAL_RESEARCH"
    )
    ready = _ready_semantics(primary, secondary) | {
        "source_refs": [_source_ref(scope, kind)]
    }
    skills: list[dict[str, object]] = [ready]
    if is_pdf:
        skills.append(_needs_semantics() | {"source_refs": []})
    return {
        "schema_version": "direct-source-skill-batch-v1",
        "source_kind": kind,
        "batch_id": packet["batch_id"],
        "section_title": (
            "Synthetic PDF section"
            if is_pdf
            else "Synthetic DOCX section"
        ),
        "locator": (
            {"page_start": 6, "page_end": 6}
            if is_pdf
            else {"start_paragraph": 3, "end_paragraph": 3}
        ),
        "source_file_hash": (
            scope["pdf_source_hash"]
            if is_pdf
            else scope["docx_source_hash"]
        ),
        "batch_text_object_hash": packet["batch_text_object_hash"],
        "sol_distillation_version": "sol-direct-v1",
        "hash_contract": packet["hash_contract"],
        "visual_evidence_refs": [VISUAL_ID] if is_pdf else [],
        "skills": skills,
        "no_skill_reason": None,
        "open_questions": [],
    }


def _dedup_manifest(candidate_ids: list[str]) -> dict[str, object]:
    ready = _ready_semantics(
        "FUNDAMENTAL_RESEARCH",
        "VALUATION_PRICING",
    )
    needs = _needs_semantics()
    return {
        "schema_version": "direct-source-dedup-manifest-v1",
        "manifest_id": "dedup:synthetic",
        "run_id": RUN_ID,
        "sol_version": "sol-final-v1",
        "sol_version_hash": sha256_bytes(b"sol-final-v1"),
        "embedding_usage": "POST_GENERATION_ASSIST_ONLY",
        "sol_confirmed": True,
        "final_skills": [
            ready
            | {
                "final_skill_id": "final:ready",
                "candidate_ids": [candidate_ids[0], candidate_ids[2]],
            },
            needs
            | {
                "final_skill_id": "final:needs",
                "candidate_ids": [candidate_ids[1]],
            },
        ],
        "formal_committee_weight_allowed": False,
    }


def _initialize_and_import(
    scope: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, object],
    dict[str, object],
    list[str],
]:
    service = scope["service"]
    service.init(scope["manifest"])
    pdf_packet = service.packet_export(RUN_ID, PDF_BATCH)
    docx_packet = service.packet_export(RUN_ID, DOCX_BATCH)
    pdf_output = _public_batch(
        scope,
        pdf_packet,
        kind="PDF",
    )
    docx_output = _public_batch(
        scope,
        docx_packet,
        kind="DOCX",
    )
    pdf_result = service.batch_import(
        RUN_ID,
        PDF_BATCH,
        pdf_output,
    )
    docx_result = service.batch_import(
        RUN_ID,
        DOCX_BATCH,
        docx_output,
    )
    candidate_ids = [
        *pdf_result["candidate_ids"],
        *docx_result["candidate_ids"],
    ]
    return (
        pdf_packet,
        docx_packet,
        pdf_output,
        docx_output,
        candidate_ids,
    )


def test_end_to_end_pdf_visual_docx_dedup_shadow_and_audit(
    tmp_path: Path,
) -> None:
    scope = _build_scope(tmp_path)
    service = scope["service"]
    initialized = service.init(scope["manifest"])
    assert initialized["idempotent_replay"] is False
    pdf_packet = service.packet_export(RUN_ID, PDF_BATCH)
    docx_packet = service.packet_export(RUN_ID, DOCX_BATCH)
    assert pdf_packet["chapter_body"][0]["text"] == scope["texts"]["pdf"]
    assert len(pdf_packet["context_before"]) == 1
    assert len(pdf_packet["context_after"]) == 1
    assert pdf_packet["reviewed_argument_units_used"] is False
    assert docx_packet["visual_evidence_refs"] == []

    pdf_output = _public_batch(
        scope,
        pdf_packet,
        kind="PDF",
    )
    docx_output = _public_batch(
        scope,
        docx_packet,
        kind="DOCX",
    )
    dry = service.dry_convert_user_batch(pdf_output)
    assert dry["status"] == "DRY_CONVERTIBLE"
    assert dry["visual_objects_verified"] == 1
    with scope["state"].connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_direct_raw_sol_candidate"
        ).fetchone()[0] == 0

    pdf_result_file = tmp_path / "b01.json"
    pdf_result_file.write_text(
        json.dumps(pdf_output, ensure_ascii=False),
        encoding="utf-8",
    )
    pdf_import = service.batch_import_file(
        RUN_ID,
        PDF_BATCH,
        pdf_result_file,
    )
    docx_import = service.batch_import(
        RUN_ID,
        DOCX_BATCH,
        docx_output,
    )
    candidate_ids = [
        *pdf_import["candidate_ids"],
        *docx_import["candidate_ids"],
    ]
    finalized = service.finalize(_dedup_manifest(candidate_ids))
    assert finalized["final_skill_count"] == 2
    assert finalized["shadow_skill_count"] == 1
    assert finalized["non_ready_skill_count"] == 1

    with scope["state"].connect() as connection:
        source_row = connection.execute(
            "SELECT fragment_object_hash,source_object_hash,slice_hash "
            "FROM knowledge_direct_candidate_source_ref "
            "WHERE source_kind='PDF'"
        ).fetchone()
        assert source_row["fragment_object_hash"] == scope["text_hashes"]["pdf"]
        expected_slice_hash = sha256_bytes(b"slice")
        assert source_row["source_object_hash"] == expected_slice_hash
        assert source_row["slice_hash"] == expected_slice_hash
        visual_row = connection.execute(
            "SELECT evidence_id,object_hash "
            "FROM knowledge_direct_candidate_visual_ref"
        ).fetchone()
        assert tuple(visual_row) == (VISUAL_ID, scope["visual_hash"])
        module_rows = connection.execute(
            "SELECT module_role,module "
            "FROM knowledge_direct_final_skill_module "
            "WHERE final_skill_id='final:ready' ORDER BY module_role"
        ).fetchall()
        assert {tuple(row) for row in module_rows} == {
            ("PRIMARY", "FUNDAMENTAL_RESEARCH"),
            ("SECONDARY", "VALUATION_PRICING"),
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_direct_final_source_ref "
            "WHERE final_skill_id='final:ready'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_direct_final_visual_ref "
            "WHERE final_skill_id='final:ready'"
        ).fetchone()[0] == 1

    shadow = service.shadow_context(RUN_ID)
    assert shadow["shadow_skill_ids"] == ["final:ready"]
    assert shadow["non_ready_count"] == 1
    assert [item["status"] for item in shadow["skills"]] == [
        "READY_FOR_SHADOW"
    ]
    status = service.status(RUN_ID)
    assert status["module_counts"]["FUNDAMENTAL_RESEARCH"] == 1
    assert status["module_counts"]["VALUATION_PRICING"] == 1
    assert status["module_counts"]["PSYCHOLOGY_BEHAVIOR"] == 1
    audit = service.audit(RUN_ID)
    assert audit["status"] == "PASS", audit["findings"]
    assert audit["foreign_key_check"] == 0
    assert audit["reviewed_argument_units_used"] is False

    assert service.init(scope["manifest"])["idempotent_replay"] is True
    assert service.packet_export(
        RUN_ID,
        PDF_BATCH,
    )["idempotent_replay"] is True
    assert service.batch_import(
        RUN_ID,
        PDF_BATCH,
        pdf_output,
    )["idempotent_replay"] is True
    changed = json.loads(json.dumps(pdf_output))
    changed["skills"][0]["core_principle"] = (
        "A changed public output cannot reuse the batch identity."
    )
    with pytest.raises(ValueError, match="content collision"):
        service.batch_import(RUN_ID, PDF_BATCH, changed)
    assert service.finalize(
        _dedup_manifest(candidate_ids)
    )["idempotent_replay"] is True


def test_source_hash_failure_and_repository_transaction_rollback(
    tmp_path: Path,
) -> None:
    scope = _build_scope(tmp_path)
    service = scope["service"]
    service.init(scope["manifest"])
    packet = service.packet_export(RUN_ID, PDF_BATCH)
    output = _public_batch(scope, packet, kind="PDF")
    forged = json.loads(json.dumps(output))
    forged["skills"][0]["source_refs"][0]["source_object_hash"] = "f" * 64
    with pytest.raises(DataQualityError, match="source slice hash mismatch"):
        service.batch_import(RUN_ID, PDF_BATCH, forged)
    with scope["state"].connect() as connection:
        row = connection.execute(
            "SELECT stage,import_input_hash "
            "FROM knowledge_direct_chapter_batch WHERE batch_id=?",
            (PDF_BATCH,),
        ).fetchone()
        assert tuple(row) == ("PACKET_EXPORTED", None)

    with scope["state"].transaction() as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_import_abort "
            "BEFORE INSERT ON knowledge_direct_raw_sol_candidate "
            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        service.batch_import(RUN_ID, PDF_BATCH, output)
    with scope["state"].connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_direct_raw_sol_candidate"
        ).fetchone()[0] == 0
        row = connection.execute(
            "SELECT stage,import_input_hash,imported_candidate_count "
            "FROM knowledge_direct_chapter_batch WHERE batch_id=?",
            (PDF_BATCH,),
        ).fetchone()
        assert tuple(row) == ("PACKET_EXPORTED", None, None)


def test_finalize_requires_all_batches_and_preserves_single_semantics(
    tmp_path: Path,
) -> None:
    scope = _build_scope(tmp_path)
    service = scope["service"]
    service.init(scope["manifest"])
    packet = service.packet_export(RUN_ID, PDF_BATCH)
    output = _public_batch(scope, packet, kind="PDF")
    imported = service.batch_import(RUN_ID, PDF_BATCH, output)
    candidate_ids = [*imported["candidate_ids"], "unknown:docx"]
    with pytest.raises(DataQualityError, match="all frozen direct chapters"):
        service.finalize(_dedup_manifest(candidate_ids))

    docx_packet = service.packet_export(RUN_ID, DOCX_BATCH)
    docx_output = _public_batch(
        scope,
        docx_packet,
        kind="DOCX",
    )
    docx_import = service.batch_import(
        RUN_ID,
        DOCX_BATCH,
        docx_output,
    )
    actual_ids = [
        *imported["candidate_ids"],
        *docx_import["candidate_ids"],
    ]
    manifest = _dedup_manifest(actual_ids)
    final_skills = manifest["final_skills"]
    assert isinstance(final_skills, list)
    needs = final_skills[1]
    assert isinstance(needs, dict)
    needs["core_principle"] = "A rewritten single-candidate semantic field."
    with pytest.raises(
        DataQualityError,
        match="preserve every user semantic field exactly",
    ):
        service.finalize(manifest)


def test_shadow_context_uses_bundle_db_and_json_status_gates(
    tmp_path: Path,
) -> None:
    scope = _build_scope(tmp_path)
    _, _, _, _, candidate_ids = _initialize_and_import(scope)
    service = scope["service"]
    service.finalize(_dedup_manifest(candidate_ids))
    with scope["state"].transaction() as connection:
        connection.execute(
            "UPDATE knowledge_direct_shadow_bundle "
            "SET shadow_skill_ids_json=?",
            ('["final:ready","final:needs"]',),
        )
    with pytest.raises(DataQualityError, match="membership mismatch"):
        service.shadow_context(RUN_ID)


def test_no_skill_batches_finalize_as_an_empty_shadow_bundle(
    tmp_path: Path,
) -> None:
    scope = _build_scope(tmp_path)
    service = scope["service"]
    service.init(scope["manifest"])
    for batch_id, kind in ((PDF_BATCH, "PDF"), (DOCX_BATCH, "DOCX")):
        packet = service.packet_export(RUN_ID, batch_id)
        output = _public_batch(scope, packet, kind=kind)
        output["skills"] = []
        output["no_skill_reason"] = (
            "The frozen section contains no decision-relevant operating rule."
        )
        imported = service.batch_import(RUN_ID, batch_id, output)
        assert imported["candidate_count"] == 0
    manifest = {
        "schema_version": "direct-source-dedup-manifest-v1",
        "manifest_id": "dedup:empty",
        "run_id": RUN_ID,
        "sol_version": "sol-final-v1",
        "sol_version_hash": sha256_bytes(b"sol-final-v1"),
        "embedding_usage": "POST_GENERATION_ASSIST_ONLY",
        "sol_confirmed": True,
        "final_skills": [],
        "formal_committee_weight_allowed": False,
    }
    finalized = service.finalize(manifest)
    assert finalized["final_skill_count"] == 0
    assert service.shadow_context(RUN_ID)["skills"] == []
    audit = service.audit(RUN_ID)
    assert audit["status"] == "PASS", audit["findings"]


def test_direct_tables_need_no_reviewed_schema(tmp_path: Path) -> None:
    scope = _build_scope(tmp_path)
    initialized = scope["service"].init(scope["manifest"])
    assert initialized["status"] == "INITIALIZED"
    with scope["state"].connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='knowledge_direct_run'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='knowledge_reviewed_argument_unit'"
        ).fetchone() is None
        assert connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall() == []


def test_independent_direct_cli_commands_are_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "knowledge-direct-skill-init",
        "knowledge-direct-skill-packet-export",
        "knowledge-direct-skill-batch-import",
        "knowledge-direct-skill-finalize",
        "knowledge-direct-skill-status",
        "knowledge-direct-skill-audit",
        "knowledge-direct-skill-shadow-context",
    ):
        assert command in result.output
