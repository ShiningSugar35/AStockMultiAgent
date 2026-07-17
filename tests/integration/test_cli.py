from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    ZhihuBrowserResponseEnvelope,
    ZhihuContentType,
    ZhihuResponseKind,
    ZhihuTransport,
)
from tests.helpers import make_financial_request

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
    assert probe["codex_artifacts"]["input_manifest_version"] == "codex-run-input-v2"
    assert len(probe["codex_artifacts"]["strict_phase4_types"]) == 10
    assert probe["codex_artifacts"]["strict_committee_types"] == [
        "CounterCasePack",
        "DecisionPack",
        "TradeProtocol",
    ]
    assert probe["codex_artifacts"]["strict_shadow_types"] == [
        "Phase8AdmissionReport",
        "ShadowEvaluationReport",
    ]
    assert probe["committee"]["status"] == "AVAILABLE"
    assert not probe["committee"]["network_access"]
    assert probe["adaptive_research"] == {
        "adaptive_weights": False,
        "broker_execution": False,
        "implementation_status": "IMPLEMENTED_DISABLED_BOUNDARY",
        "main_paper_ledger_write": False,
        "next_permitted_stage": "PHASE7_FORWARD_EVIDENCE_COLLECTION",
        "online_learning": False,
        "phase8_admission_status": None,
        "reason_codes": ["PHASE7_STUDY_NOT_RUN"],
        "sample_gaps": {
            "independent_decisions": 100,
            "market_regimes": 3,
            "observation_months": "12",
            "walk_forward_folds": 5,
        },
        "shadow_policy_version": "shadow-evaluation-policy-v1",
        "status": "NOT_ENTERED_BY_DESIGN",
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

    sources = runner.invoke(app, ["knowledge-source-list"])
    assert sources.exit_code == 0, sources.output
    source_payload = json.loads(sources.output)
    assert {item["display_name"] for item in source_payload["sources"]} == {
        "MR Dang",
        "黄彦臻",
        "派大星皮皮",
        "寒武纪的鳄鱼",
    }
    crocodile = next(
        item for item in source_payload["sources"] if item["display_name"] == "寒武纪的鳄鱼"
    )
    assert not crocodile["online_collection_required"]

    coverage = runner.invoke(
        app,
        ["zhihu-coverage", "zhihu:mr-dang-77", "--content-type", "answers"],
    )
    assert coverage.exit_code == 0, coverage.output
    assert json.loads(coverage.output) == {"report": None, "status": "NOT_COLLECTED"}

    private_marker = b'{"data":[],"paging":{"is_end":true},"private":"cli-secret"}'
    envelope = ZhihuBrowserResponseEnvelope(
        author_source_id="zhihu:mr-dang-77",
        response_kind=ZhihuResponseKind.LISTING,
        content_type=ZhihuContentType.ANSWERS,
        listing_page=0,
        requested_url=(
            "https://www.zhihu.com/api/v4/members/mr-dang-77/"
            "answers?limit=2&offset=0&sort_by=created"
        ),
        status_code=200,
        response_mime="application/json",
        body_base64=base64.b64encode(private_marker).decode("ascii"),
        transport=ZhihuTransport.CHROME,
        captured_at=datetime.now(UTC),
    )
    envelope_path = runtime / "imports" / "response.json"
    envelope_path.parent.mkdir(parents=True)
    envelope_path.write_text(envelope.model_dump_json(), encoding="utf-8")
    imported = runner.invoke(app, ["zhihu-response-import", str(envelope_path)])
    assert imported.exit_code == 0, imported.output
    import_payload = json.loads(imported.output)
    assert import_payload["status"] == "IMPORTED"
    assert import_payload["import_status"] == "PENDING"
    assert "cli-secret" not in imported.output
    assert str(envelope_path) not in imported.output
    replayed = runner.invoke(
        app,
        ["zhihu-import-replay", import_payload["envelope_id"]],
    )
    assert replayed.exit_code == 0, replayed.output
    replay_payload = json.loads(replayed.output)
    assert replay_payload["status"] == "CONSUMED"
    assert replay_payload["coverage_status"] == "COMPLETE"
    assert "cli-secret" not in replayed.output


def test_private_pdf_cli_output_is_redacted(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "private-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    private_text = "This private fixture sentence must never be emitted by the CLI."
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), private_text)
    pdf_path = tmp_path / "secret-local-book.pdf"
    pdf_path.write_bytes(pdf.tobytes())
    pdf.close()

    invoked = runner.invoke(
        app,
        [
            "private-pdf-ingest",
            str(pdf_path),
            "--source-id",
            "book:test:cli",
            "--title",
            "Private CLI fixture",
            "--author-source-id",
            "author:test",
            "--file-version",
            "v1",
            "--page",
            "1",
            "--no-ocr",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.output)
    assert payload["status"] == "INGESTED"
    assert payload["manifest"]["source_page_count"] == 1
    assert payload["parse"]["processed_page_count"] == 1
    assert private_text not in invoked.output
    assert str(pdf_path) not in invoked.output
    assert pdf_path.name not in invoked.output


def test_private_docx_cli_output_is_redacted(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "private-docx-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    private_text = "This private DOCX sentence must never be emitted by the CLI."
    docx_path = tmp_path / "secret-local-export.docx"
    main_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{main_type}"/>
</Types>""",
        )
        archive.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{private_text}</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>""",
        )

    invoked = runner.invoke(
        app,
        [
            "private-docx-ingest",
            str(docx_path),
            "--source-id",
            "docx:test:cli",
            "--title",
            "Private DOCX CLI fixture",
            "--author-source-id",
            "author:test",
            "--file-version",
            "v1",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    payload = json.loads(invoked.output)
    assert payload["status"] == "INGESTED"
    assert payload["parse"]["coverage_status"] == "COMPLETE"
    assert payload["parse"]["processed_block_count"] == 1
    assert private_text not in invoked.output
    assert str(docx_path) not in invoked.output
    assert docx_path.name not in invoked.output


def test_financial_audit_cli_is_idempotent_and_reports_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "financial-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(runtime / "objects" / "sha256")
    request = make_financial_request(state, objects)
    request = request.model_copy(update={"as_of": datetime(2026, 3, 21, tzinfo=UTC)})
    input_path = tmp_path / "financial-audit-input.json"
    input_path.write_text(
        json.dumps(request.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )

    schema = runner.invoke(app, ["financial-audit-schema"])
    assert schema.exit_code == 0, schema.output
    schema_payload = json.loads(schema.output)
    assert set(schema_payload["required"]) >= {"company_id", "as_of", "industry_profile"}

    first = runner.invoke(app, ["financial-audit", str(input_path)])
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["status"] == "SUCCEEDED"
    assert payload["pack"]["coverage_status"] == "COMPLETE"
    assert not payload["reused_existing"]
    assert len(payload["artifact_hash"]) == 64
    assert str(input_path) not in first.output

    repeated = runner.invoke(app, ["financial-audit", str(input_path)])
    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["reused_existing"]
    assert repeated_payload["artifact_hash"] == payload["artifact_hash"]

    status = runner.invoke(
        app,
        ["financial-audit-status", payload["pack"]["audit_run_id"]],
    )
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["status"] == "SUCCEEDED"
    assert status_payload["checkpoint_step"] == "COMPLETE"
    assert status_payload["attempt_count"] == 1


def test_invalid_base_case_cli_request_is_redacted(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "research-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    secret = "private-research-statement-must-not-be-echoed"
    request = tmp_path / "private-base-case-request.json"
    request.write_text(json.dumps({"draft": {"statement": secret}}), encoding="utf-8")

    invoked = runner.invoke(app, ["research-base-case-build", str(request)])
    assert invoked.exit_code == 2
    assert json.loads(invoked.output) == {
        "error_code": "INVALID_BASE_CASE_REQUEST",
        "status": "REJECTED",
    }
    assert secret not in invoked.output
    assert str(request) not in invoked.output


def test_research_specialist_registry_cli_and_invalid_requests_are_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "specialist-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    listed = runner.invoke(app, ["research-specialist-list"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["status"] == "REGISTERED"
    assert payload["max_specialists"] == 3
    assert len(payload["skills"]) == 7
    assert sum(item["counts_as_specialist"] for item in payload["skills"]) == 6

    secret = "private-specialist-statement-must-not-be-echoed"
    request = tmp_path / "private-specialist-request.json"
    request.write_text(json.dumps({"statement": secret}), encoding="utf-8")
    for command, error_code in (
        ("research-specialist-route", "INVALID_SPECIALIST_ROUTE"),
        ("research-delta-import", "INVALID_SPECIALIST_DELTA"),
    ):
        invoked = runner.invoke(app, [command, str(request)])
        assert invoked.exit_code == 2
        assert json.loads(invoked.output) == {
            "error_code": error_code,
            "status": "REJECTED",
        }
        assert secret not in invoked.output
        assert str(request) not in invoked.output


def test_research_diagnostic_cli_schema_and_invalid_requests_are_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "diagnostic-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    schema = runner.invoke(app, ["research-diagnostic-schema"])
    assert schema.exit_code == 0, schema.output
    payload = json.loads(schema.output)
    assert payload["diagnostics_version"] == "research-diagnostics-v1"
    assert len(payload["diagnostics"]) == 6
    assert payload["memo"]["skill_id"] == "ResearchMemoComposer"

    secret = "private-diagnostic-statement-must-not-be-echoed"
    request = tmp_path / "private-diagnostic-request.json"
    request.write_text(json.dumps({"statement": secret}), encoding="utf-8")
    for command, error_code in (
        ("research-specialist-diagnose", "INVALID_RESEARCH_DIAGNOSTIC"),
        ("research-memo-compose", "INVALID_RESEARCH_MEMO"),
    ):
        invoked = runner.invoke(app, [command, str(request)])
        assert invoked.exit_code == 2
        assert json.loads(invoked.output) == {
            "error_code": error_code,
            "status": "REJECTED",
        }
        assert secret not in invoked.output
        assert str(request) not in invoked.output


def test_position_lifecycle_cli_schema_and_invalid_requests_are_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "position-lifecycle-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    schema = runner.invoke(app, ["position-lifecycle-schema"])
    assert schema.exit_code == 0, schema.output
    payload = json.loads(schema.output)
    assert payload["rules_version"] == "generic-position-lifecycle-v1"
    assert payload["action_priority"] == ["EXIT", "REVIEW", "TRIM", "ADD", "HOLD"]
    assert payload["requires_user_confirmation"]
    assert payload["add_requires_new_evidence"]

    status = runner.invoke(app, ["holding-review-status", "position:not-run"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["status"] == "NOT_RUN"

    secret = "private-position-thesis-must-not-be-echoed"
    request = tmp_path / "private-position-request.json"
    request.write_text(json.dumps({"thesis_summary": secret}), encoding="utf-8")
    for command, error_code in (
        ("position-plan-create", "INVALID_POSITION_PLAN"),
        ("holding-review-run", "INVALID_HOLDING_REVIEW"),
    ):
        invoked = runner.invoke(app, [command, str(request)])
        assert invoked.exit_code == 2
        assert json.loads(invoked.output) == {
            "error_code": error_code,
            "status": "REJECTED",
        }
        assert secret not in invoked.output
        assert str(request) not in invoked.output


def test_phase4_chain_and_strict_codex_cli_fail_closed_without_frozen_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "phase4-terminal-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    status = runner.invoke(app, ["research-chain-status", "company:not-run"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["status"] == "NOT_RUN"
    audit = runner.invoke(app, ["research-chain-audit", "company:not-run"])
    assert audit.exit_code == 0, audit.output
    assert json.loads(audit.output)["finding_codes"] == ["CORE_NOT_RUN"]

    secret = "private-strict-codex-request-must-not-be-echoed"
    strict = runner.invoke(
        app,
        ["codex-run-init", secret, "--require-registered-output"],
    )
    assert strict.exit_code == 2
    assert json.loads(strict.output) == {
        "error_code": "INVALID_CODEX_INPUT",
        "status": "REJECTED",
    }
    assert secret not in strict.output

    missing = runner.invoke(
        app,
        ["context-plan", "--artifact-id", "BaseCasePack:not-registered"],
    )
    assert missing.exit_code == 2
    assert json.loads(missing.output) == {
        "error_code": "INVALID_CODEX_INPUT",
        "status": "REJECTED",
    }

    invalid_id = runner.invoke(app, ["codex-run-status", "../invalid"])
    assert invalid_id.exit_code == 2
    assert json.loads(invalid_id.output) == {
        "error_code": "INVALID_RUN_ID",
        "status": "REJECTED",
    }

    initialized = runner.invoke(app, ["codex-run-init", "safe local validation"])
    assert initialized.exit_code == 0, initialized.output
    run_id = json.loads(initialized.output)["run_id"]
    payload_secret = "private-invalid-payload-must-not-be-echoed"
    invalid_draft = tmp_path / "private-invalid-codex-draft.json"
    invalid_draft.write_text(
        json.dumps(
            {
                "artifact_type": "ContextBudgetReport",
                "payload": {"unexpected_private_field": payload_secret},
                "citations": {},
                "requested_commands": [],
            }
        ),
        encoding="utf-8",
    )
    imported = runner.invoke(app, ["codex-run-import", run_id, str(invalid_draft)])
    assert imported.exit_code == 2
    import_payload = json.loads(imported.output)
    assert import_payload["errors"] == ["INVALID_CODEX_ARTIFACT_PAYLOAD"]
    assert payload_secret not in imported.output
    assert str(invalid_draft) not in imported.output


def test_committee_cli_schema_status_and_invalid_requests_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "committee-terminal-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    schema = runner.invoke(app, ["committee-schema"])
    assert schema.exit_code == 0, schema.output
    schema_payload = json.loads(schema.output)
    assert schema_payload["rules"]["rules_version"] == "committee-rules-v1"
    assert schema_payload["external_access"] == {
        "api": False,
        "browser": False,
        "full_document": False,
        "mcp": False,
        "network": False,
        "new_research": False,
    }

    missing_status = runner.invoke(app, ["committee-status"])
    assert missing_status.exit_code == 2
    assert json.loads(missing_status.output) == {
        "error_code": "COMMITTEE_ID_REQUIRED",
        "status": "REJECTED",
    }
    not_run = runner.invoke(
        app,
        ["committee-status", "--decision-id", "decision:not-run"],
    )
    assert not_run.exit_code == 0, not_run.output
    assert json.loads(not_run.output)["status"] == "NOT_RUN"
    task = runner.invoke(app, ["committee-task-status", "task:not-run"])
    assert task.exit_code == 0, task.output
    assert json.loads(task.output)["status"] == "NOT_RUN"
    unresolved = runner.invoke(
        app,
        ["committee-task-resolve", "task:not-run", "artifact:not-run"],
    )
    assert unresolved.exit_code == 2
    assert json.loads(unresolved.output) == {
        "error_code": "INVALID_TASK_RESOLUTION",
        "status": "REJECTED",
    }

    secret = "private-committee-request-must-not-be-echoed"
    invalid_request = tmp_path / "private-invalid-committee-request.json"
    invalid_request.write_text(json.dumps({"private": secret}), encoding="utf-8")
    for command, error_code in (
        ("committee-plan", "INVALID_COMMITTEE_REQUEST"),
        ("committee-decide", "COMMITTEE_DECISION_REJECTED"),
        ("committee-recover", "COMMITTEE_NOT_RECOVERABLE"),
    ):
        invoked = runner.invoke(app, [command, str(invalid_request)])
        assert invoked.exit_code == 2
        assert json.loads(invoked.output) == {
            "error_code": error_code,
            "status": "REJECTED",
        }
        assert secret not in invoked.output
        assert str(invalid_request) not in invoked.output


def test_shadow_cli_schema_status_admission_and_invalid_requests_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "shadow-terminal-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    schema = runner.invoke(app, ["shadow-schema"])
    assert schema.exit_code == 0, schema.output
    schema_payload = json.loads(schema.output)
    assert schema_payload["policy"]["policy_version"] == "shadow-evaluation-policy-v1"
    assert schema_payload["hard_boundaries"] == {
        "broker_execution_allowed": False,
        "future_inputs_allowed": False,
        "independence_key_is_deterministic": True,
        "main_paper_ledger_write_allowed": False,
        "not_pit_safe_formal_samples_allowed": False,
        "online_weight_changes_allowed": False,
        "weights_frozen": True,
    }

    status = runner.invoke(app, ["shadow-status", "--study-id", "study:not-run"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["status"] == "NOT_RUN"
    audit = runner.invoke(app, ["shadow-audit", "study:not-run"])
    assert audit.exit_code == 0, audit.output
    assert json.loads(audit.output) == {
        "finding_codes": ["SHADOW_STUDY_NOT_RUN"],
        "status": "NOT_RUN",
        "study_id": "study:not-run",
    }
    admission = runner.invoke(app, ["phase8-admission", "study:not-run"])
    assert admission.exit_code == 0, admission.output
    assert json.loads(admission.output) == {
        "broker_execution_allowed": False,
        "online_weight_changes_allowed": False,
        "status": "NOT_RUN",
        "study_id": "study:not-run",
    }
    adaptive = runner.invoke(
        app,
        ["adaptive-research-status", "--study-id", "study:not-run"],
    )
    assert adaptive.exit_code == 0, adaptive.output
    adaptive_payload = json.loads(adaptive.output)
    assert adaptive_payload["implementation_status"] == (
        "IMPLEMENTED_DISABLED_BOUNDARY"
    )
    assert adaptive_payload["capability_status"] == "NOT_ENTERED_BY_DESIGN"
    assert adaptive_payload["reason_codes"] == ["PHASE7_STUDY_NOT_RUN"]
    assert adaptive_payload["observation_month_gap"] == "12"
    assert adaptive_payload["independent_decision_gap"] == 100
    assert adaptive_payload["qualifying_walk_forward_fold_gap"] == 5
    assert adaptive_payload["qualifying_market_regime_gap"] == 3
    assert not adaptive_payload["adaptive_weights_enabled"]
    assert not adaptive_payload["online_learning_allowed"]
    assert not adaptive_payload["main_paper_ledger_write_allowed"]
    assert not adaptive_payload["broker_execution_allowed"]
    forced_adaptive = runner.invoke(
        app,
        ["adaptive-research-status", "--force-enable"],
    )
    assert forced_adaptive.exit_code == 2

    secret = "private-shadow-request-must-not-be-echoed"
    invalid_request = tmp_path / "private-invalid-shadow-request.json"
    invalid_request.write_text(json.dumps({"private": secret}), encoding="utf-8")
    for command, error_code in (
        ("shadow-study-plan", "INVALID_SHADOW_STUDY"),
        ("shadow-study-create", "SHADOW_STUDY_CREATE_REJECTED"),
        ("shadow-assign", "SHADOW_ASSIGNMENT_REJECTED"),
        ("shadow-observation-record", "SHADOW_OBSERVATION_REJECTED"),
        ("shadow-recover", "SHADOW_NOT_RECOVERABLE"),
    ):
        invoked = runner.invoke(app, [command, str(invalid_request)])
        assert invoked.exit_code == 2
        assert json.loads(invoked.output) == {
            "error_code": error_code,
            "status": "REJECTED",
        }
        assert secret not in invoked.output
        assert str(invalid_request) not in invoked.output
