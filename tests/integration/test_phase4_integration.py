from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from astock.core.codex_runs import CodexRunService, registered_phase4_artifact_types
from astock.research import (
    Phase4ChainService,
    ResearchRepository,
    load_position_lifecycle_config,
    load_research_core_config,
    load_research_diagnostic_config,
    load_research_skill_registry,
)
from astock.schemas import (
    CodexRunInputManifest,
    ContextBudgetReport,
    HoldingReviewRequest,
)
from astock.schemas.base import AStockModel
from tests.integration.test_position_lifecycle import _service_and_plan

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CITATION = "synthetic exact official locator kept out of SQLite"
_REQUEST_MARKER = "private synthetic Codex request kept out of SQLite"


def _complete_chain(tmp_path: Path, state):
    lifecycle, plan, _ = _service_and_plan(tmp_path, state)
    assert plan.plan_id is not None
    assert plan.as_of is not None
    review = lifecycle.review(
        HoldingReviewRequest(
            plan_id=plan.plan_id,
            from_as_of=plan.as_of,
            to_as_of=plan.as_of + timedelta(days=1),
            added_evidence_ids=[],
            changed_claim_ids=[],
            invalidated_evidence_ids=[],
            unresolved_conflict_ids=[],
            signals=[],
        )
    )
    repository = ResearchRepository(state, lifecycle.object_store)
    assert plan.base_case_id is not None
    assert plan.route_plan_id is not None
    assert plan.memo_id is not None
    base = repository.get_base_case(plan.base_case_id)
    route = repository.get_route_plan(plan.route_plan_id)
    memo = repository.get_research_memo(plan.memo_id)
    assert base is not None
    assert route is not None
    assert memo is not None
    evidence_pack = repository.get_evidence_pack(base.evidence_pack_id)
    delta_summaries = repository.specialist_delta_summaries(route.route_plan_id)
    diagnostic_summaries = repository.diagnostic_report_summaries(base.base_case_id)
    assert evidence_pack is not None
    assert len(delta_summaries) == 1
    assert len(diagnostic_summaries) == 1
    delta = repository.get_specialist_delta(str(delta_summaries[0]["delta_id"]))
    diagnostic = repository.get_diagnostic_report(
        str(diagnostic_summaries[0]["diagnostic_id"])
    )
    assert delta is not None
    assert diagnostic is not None
    artifacts: dict[str, AStockModel] = {
        "FrozenEvidencePack": evidence_pack,
        "BaseCasePack": base,
        "SpecialistRoutePlan": route,
        "SpecialistDelta": delta,
        "SpecialistDiagnosticReport": diagnostic,
        "ResearchMemoArtifact": memo,
        "PositionMonitoringPlan": plan,
        "HoldingEvidenceUpdate": review.update,
        "HoldingReviewPack": review.review,
        "PositionActionProposal": review.proposal,
    }
    artifact_ids = {
        "FrozenEvidencePack": f"FrozenEvidencePack:{evidence_pack.pack_id}",
        "BaseCasePack": f"BaseCasePack:{base.base_case_id}",
        "SpecialistRoutePlan": f"SpecialistRoutePlan:{route.route_plan_id}",
        "SpecialistDelta": f"SpecialistDelta:{delta.delta_id}",
        "SpecialistDiagnosticReport": (
            f"SpecialistDiagnosticReport:{diagnostic.diagnostic_id}"
        ),
        "ResearchMemoArtifact": f"ResearchMemoArtifact:{memo.memo_id}",
        "PositionMonitoringPlan": f"PositionMonitoringPlan:{plan.plan_id}",
        "HoldingEvidenceUpdate": f"HoldingEvidenceUpdate:{review.update.update_id}",
        "HoldingReviewPack": f"HoldingReviewPack:{review.review.review_id}",
        "PositionActionProposal": (
            f"PositionActionProposal:{review.proposal.proposal_id}"
        ),
    }
    return lifecycle, plan, artifacts, artifact_ids


def _evidence_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" and isinstance(child, list):
                found.update(str(item) for item in child)
            else:
                found.update(_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_evidence_ids(child))
    return found


def _draft(path: Path, artifact_type: str, artifact: AStockModel) -> Path:
    payload = artifact.model_dump(mode="json")
    path.write_text(
        json.dumps(
            {
                "artifact_type": artifact_type,
                "payload": payload,
                "citations": {
                    evidence_id: _CITATION for evidence_id in _evidence_ids(payload)
                },
                "requested_commands": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _strict_run(
    codex: CodexRunService,
    source_artifact_id: str,
    *,
    request: str = _REQUEST_MARKER,
    extra_artifact_ids: list[str] | None = None,
):
    references = [
        codex.resolve_artifact_reference(artifact_id)
        for artifact_id in [source_artifact_id, *(extra_artifact_ids or [])]
    ]
    return codex.initialize(
        {"request": request},
        context_budget=ContextBudgetReport(
            selected_skills=["holding-monitor"],
            selected_artifacts=[item.artifact_id for item in references],
        ),
        input_manifest=CodexRunInputManifest(
            selected_skills=["holding-monitor"],
            artifact_references=references,
            require_registered_output=True,
        ),
    )


def _chain_service(state, lifecycle) -> Phase4ChainService:
    return Phase4ChainService(
        state,
        lifecycle.object_store,
        load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml"),
        load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml"),
        load_research_diagnostic_config(
            PROJECT_ROOT / "configs" / "research_diagnostics.yaml"
        ),
        load_position_lifecycle_config(
            PROJECT_ROOT / "configs" / "position_lifecycle.yaml"
        ),
    )


def test_complete_phase4_chain_and_all_strict_codex_outputs_are_auditable(
    tmp_path: Path,
    state,
) -> None:
    lifecycle, plan, artifacts, artifact_ids = _complete_chain(tmp_path, state)
    assert sorted(artifacts) == registered_phase4_artifact_types()
    chain = _chain_service(state, lifecycle)
    assert chain.status(plan.company_id, position_id=plan.position_id)["status"] == "AVAILABLE"
    assert chain.audit(plan.company_id, position_id=plan.position_id)["status"] == "PASS"

    codex = CodexRunService(tmp_path / "codex-phase4", lifecycle.object_store, state)
    output_hashes: list[str] = []
    last_run_id: str | None = None
    for artifact_type in registered_phase4_artifact_types():
        run = _strict_run(codex, artifact_ids[artifact_type])
        last_run_id = run.run_id
        codex.stage_draft(
            run.run_id,
            _draft(
                tmp_path / f"{artifact_type}.json",
                artifact_type,
                artifacts[artifact_type],
            ),
        )
        report = codex.import_draft(run.run_id)
        assert report.valid, report.errors
        assert report.artifact_hash is not None
        output_hashes.append(report.artifact_hash)
        assert codex.audit(run.run_id)["status"] == "PASS"
        repeated = codex.import_draft(run.run_id)
        assert repeated.valid
        assert repeated.artifact_hash == report.artifact_hash
    assert len(output_hashes) == len(set(output_hashes))
    assert last_run_id is not None

    with state.transaction() as connection:
        connection.execute(
            "UPDATE codex_run_input_index SET artifact_role='PRIMARY' WHERE run_id=?",
            (last_run_id,),
        )
    tampered = codex.audit(last_run_id)
    assert tampered["status"] == "PARTIAL"
    tampered_codes = tampered["finding_codes"]
    assert isinstance(tampered_codes, list)
    assert "INPUT_INDEX_MISMATCH" in tampered_codes
    rejected_reimport = codex.import_draft(last_run_id)
    assert not rejected_reimport.valid
    assert rejected_reimport.errors == ["CODEX_RUN_OR_DRAFT_INVALID"]

    with state.connect() as connection:
        output_rows = connection.execute(
            "SELECT run_id,validated_artifact_id,output_object_hash,input_count,"
            "citation_count,strict_registered_output,status FROM codex_run_output_index"
        ).fetchall()
        assert len(output_rows) == 10
        safe_metadata = "\n".join(
            str(value)
            for table in ("codex_run_input_index", "codex_run_output_index")
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert _REQUEST_MARKER not in safe_metadata
    assert _CITATION not in safe_metadata
    assert str(tmp_path) not in safe_metadata


def test_strict_codex_rejects_forged_output_and_binds_output_to_inputs(
    tmp_path: Path,
    state,
) -> None:
    lifecycle, _, artifacts, artifact_ids = _complete_chain(tmp_path, state)
    codex = CodexRunService(tmp_path / "codex-strict", lifecycle.object_store, state)
    proposal = artifacts["PositionActionProposal"]
    forged = proposal.model_copy(update={"proposal_id": "proposal:forged-unregistered"})
    forged_run = _strict_run(codex, artifact_ids["BaseCasePack"])
    codex.stage_draft(
        forged_run.run_id,
        _draft(tmp_path / "forged.json", "PositionActionProposal", forged),
    )
    rejected = codex.import_draft(forged_run.run_id)
    assert not rejected.valid
    assert rejected.errors[-1] == "STRICT_CODEX_OUTPUT_NOT_REGISTERED"

    hashes: list[str] = []
    for context_id, suffix in (
        (artifact_ids["BaseCasePack"], "base"),
        (artifact_ids["ResearchMemoArtifact"], "memo"),
    ):
        run = _strict_run(
            codex,
            artifact_ids["PositionActionProposal"],
            extra_artifact_ids=[context_id],
        )
        codex.stage_draft(
            run.run_id,
            _draft(
                tmp_path / f"same-proposal-{suffix}.json",
                "PositionActionProposal",
                proposal,
            ),
        )
        report = codex.import_draft(run.run_id)
        assert report.valid
        assert report.artifact_hash is not None
        hashes.append(report.artifact_hash)
        with state.connect() as connection:
            inputs = json.loads(
                connection.execute(
                    "SELECT input_hashes_json FROM artifact_registry WHERE artifact_id=?",
                    (f"CodexValidatedArtifact:{report.artifact_hash}",),
                ).fetchone()[0]
            )
        assert inputs == run.input_hashes
    assert hashes[0] != hashes[1]


def test_codex_run_recovers_after_files_precede_output_index(
    tmp_path: Path,
    state,
    monkeypatch,
) -> None:
    lifecycle, _, artifacts, artifact_ids = _complete_chain(tmp_path, state)
    codex = CodexRunService(tmp_path / "codex-recovery", lifecycle.object_store, state)
    no_draft = _strict_run(codex, artifact_ids["PositionActionProposal"])
    not_recoverable = codex.recover(no_draft.run_id)
    assert not not_recoverable.valid
    assert not_recoverable.errors == ["Codex run has no staged draft to recover"]
    run = _strict_run(codex, artifact_ids["PositionActionProposal"])
    codex.stage_draft(
        run.run_id,
        _draft(
            tmp_path / "recover-proposal.json",
            "PositionActionProposal",
            artifacts["PositionActionProposal"],
        ),
    )
    original_finalize = codex._finalize_import

    def simulate_crash(**kwargs):
        raise RuntimeError("synthetic crash before output index")

    monkeypatch.setattr(codex, "_finalize_import", simulate_crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        codex.import_draft(run.run_id)
    assert codex.audit(run.run_id)["status"] == "PARTIAL"
    with state.connect() as connection:
        assert connection.execute(
            "SELECT status FROM run WHERE run_id=?", (run.run_id,)
        ).fetchone()[0] == "PENDING"
        assert connection.execute(
            "SELECT COUNT(*) FROM codex_run_output_index WHERE run_id=?", (run.run_id,)
        ).fetchone()[0] == 0

    monkeypatch.setattr(codex, "_finalize_import", original_finalize)
    recovered = codex.recover(run.run_id)
    assert recovered.valid
    assert codex.audit(run.run_id)["status"] == "PASS"
    repeated = codex.recover(run.run_id)
    assert repeated.valid
    assert repeated.artifact_hash == recovered.artifact_hash
    draft_path = tmp_path / "codex-recovery" / "codex_runs" / run.run_id / "result_draft.json"
    draft_path.write_text("{}", encoding="utf-8")
    damaged = codex.audit(run.run_id)
    assert damaged["status"] == "PARTIAL"
    finding_codes = damaged["finding_codes"]
    assert isinstance(finding_codes, list)
    assert "DRAFT_FILE_MISMATCH" in finding_codes


def test_invalid_frozen_input_leaves_no_run_and_chain_detects_parent_damage(
    tmp_path: Path,
    state,
) -> None:
    lifecycle, plan, _, artifact_ids = _complete_chain(tmp_path, state)
    codex = CodexRunService(tmp_path / "codex-invalid", lifecycle.object_store, state)
    reference = codex.resolve_artifact_reference(artifact_ids["BaseCasePack"])
    memo_reference = codex.resolve_artifact_reference(artifact_ids["ResearchMemoArtifact"])
    bad_manifest = CodexRunInputManifest(
        artifact_references=[
            reference.model_copy(update={"object_sha256": "0" * 64})
        ]
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        codex.initialize({"request": _REQUEST_MARKER}, input_manifest=bad_manifest)
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0

    chain = _chain_service(state, lifecycle)
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM artifact_registry WHERE artifact_id=?",
            (artifact_ids["BaseCasePack"],),
        )
    audit = chain.audit(plan.company_id, position_id=plan.position_id)
    assert audit["status"] == "PARTIAL"
    finding_codes = audit["finding_codes"]
    assert isinstance(finding_codes, list)
    assert "CORE:ARTIFACT_REGISTRY_MISMATCH" in finding_codes

    lifecycle.object_store.path_for(memo_reference.object_sha256).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="object is unavailable"):
        codex.initialize(
            {"request": _REQUEST_MARKER},
            input_manifest=CodexRunInputManifest(
                artifact_references=[memo_reference]
            ),
        )
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0


def test_phase4_chain_distinguishes_research_ready_from_unreviewed_position(
    tmp_path: Path,
    state,
) -> None:
    lifecycle, plan, _ = _service_and_plan(tmp_path, state)
    chain = _chain_service(state, lifecycle)
    assert chain.audit(plan.company_id)["status"] == "PASS"
    status = chain.status(plan.company_id, position_id=plan.position_id)
    assert status["status"] == "PARTIAL"
    assert status["missing_stage_codes"] == ["HOLDING_REVIEW_NOT_RUN"]
    audit = chain.audit(plan.company_id, position_id=plan.position_id)
    assert audit["status"] == "PARTIAL"
    assert audit["finding_codes"] == ["HOLDING_REVIEW_NOT_RUN"]
