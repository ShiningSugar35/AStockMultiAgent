"""Validated Codex run initialization and artifact-only import."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from astock.core.atomic import atomic_write_bytes, atomic_write_text
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.policy import PolicyEngine
from astock.core.state import StateStore
from astock.schemas import (
    AuthorCollectionCoverageReport,
    BaseCasePack,
    CodexDraft,
    ContextBudgetReport,
    FinancialIntegrityEvidencePack,
    FrozenEvidencePack,
    HoldingReviewPack,
    PositionMonitoringPlan,
    ResearchMemoArtifact,
    RunManifest,
    RunMode,
    RunStatus,
    SpecialistDelta,
    SpecialistDiagnosticReport,
    SpecialistRoutePlan,
    ValidationReport,
)
from astock.schemas.base import AStockModel

_ARTIFACT_MODELS: dict[str, type[AStockModel]] = {
    "FrozenEvidencePack": FrozenEvidencePack,
    "BaseCasePack": BaseCasePack,
    "SpecialistRoutePlan": SpecialistRoutePlan,
    "SpecialistDelta": SpecialistDelta,
    "SpecialistDiagnosticReport": SpecialistDiagnosticReport,
    "ResearchMemoArtifact": ResearchMemoArtifact,
    "ContextBudgetReport": ContextBudgetReport,
    "PositionMonitoringPlan": PositionMonitoringPlan,
    "HoldingReviewPack": HoldingReviewPack,
    "AuthorCollectionCoverageReport": AuthorCollectionCoverageReport,
    "FinancialIntegrityEvidencePack": FinancialIntegrityEvidencePack,
}


class CodexRunService:
    def __init__(
        self,
        runtime_root: Path,
        object_store: ObjectStore,
        state: StateStore,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.runtime_root = runtime_root.resolve()
        self.object_store = object_store
        self.state = state
        self.policy_engine = policy_engine or PolicyEngine()

    def initialize(
        self,
        request: dict[str, Any],
        *,
        context_budget: ContextBudgetReport | None = None,
        input_manifest: dict[str, Any] | None = None,
    ) -> RunManifest:
        now = datetime.now(UTC)
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}"
        run_dir = self.runtime_root / "codex_runs" / run_id
        manifest = RunManifest(
            run_id=run_id,
            mode=RunMode.CODEX_INTERACTIVE,
            request_hash=content_hash(request),
            as_of=now,
            node_plans=[],
            input_hashes=[],
            artifact_hashes=[],
            policy_version=self.policy_engine.version,
            provider_versions={},
            status=RunStatus.PENDING,
        )
        budget = context_budget or ContextBudgetReport()
        atomic_write_bytes(run_dir / "request.json", canonical_json_bytes(request))
        atomic_write_bytes(
            run_dir / "input_manifest.json", canonical_json_bytes(input_manifest or {})
        )
        atomic_write_bytes(
            run_dir / "context_budget.json", canonical_json_bytes(budget.model_dump(mode="json"))
        )
        atomic_write_bytes(
            run_dir / "run_manifest.json", canonical_json_bytes(manifest.model_dump(mode="json"))
        )
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO run(run_id,mode,status,as_of,plan_hash,started_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    manifest.mode.value,
                    manifest.status.value,
                    manifest.as_of.isoformat(),
                    manifest.request_hash,
                    now.isoformat(),
                ),
            )
        return manifest

    def stage_draft(self, run_id: str, source: Path) -> Path:
        run_dir = self._run_dir(run_id)
        draft = CodexDraft.model_validate_json(source.read_text(encoding="utf-8"))
        destination = run_dir / "result_draft.json"
        atomic_write_bytes(destination, canonical_json_bytes(draft.model_dump(mode="json")))
        return destination

    def import_draft(self, run_id: str) -> ValidationReport:
        run_dir = self._run_dir(run_id)
        draft_path = run_dir / "result_draft.json"
        errors: list[str] = []
        try:
            draft = CodexDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            return ValidationReport(valid=False, errors=[str(exc)])
        model = _ARTIFACT_MODELS.get(draft.artifact_type)
        if model is None:
            return ValidationReport(
                valid=False,
                errors=[f"Unsupported artifact_type: {draft.artifact_type}"],
            )
        try:
            payload = dict(draft.payload)
            payload.setdefault("created_at", draft.created_at)
            artifact = model.model_validate(payload)
        except ValidationError as exc:
            return ValidationReport(valid=False, errors=[str(exc)])
        evidence_ids = _collect_evidence_ids(artifact.model_dump(mode="json"))
        missing_citations = sorted(item for item in evidence_ids if item not in draft.citations)
        if missing_citations:
            errors.append(f"Missing citations for evidence IDs: {', '.join(missing_citations)}")
        empty_citations = sorted(
            item
            for item in evidence_ids
            if item in draft.citations and not draft.citations[item].strip()
        )
        if empty_citations:
            errors.append(f"Empty citation locators for evidence IDs: {', '.join(empty_citations)}")
        self.policy_engine.check_codex_import(draft)
        if errors:
            return ValidationReport(valid=False, errors=errors)
        validated = {
            "artifact_type": draft.artifact_type,
            "payload": artifact.model_dump(mode="json"),
            "citations": draft.citations,
        }
        object_ref = self.object_store.put_json(validated)
        artifact_id = f"{draft.artifact_type}:{object_ref.sha256}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=draft.artifact_type,
            schema_version=artifact.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[],
        )
        atomic_write_bytes(run_dir / "validated_result.json", canonical_json_bytes(validated))
        atomic_write_bytes(run_dir / "citations.json", canonical_json_bytes(draft.citations))
        atomic_write_text(
            run_dir / "run_summary.md",
            f"# Codex Run {run_id}\n\nValidated `{draft.artifact_type}` as "
            f"`{object_ref.sha256}`.\n",
        )
        manifest_path = run_dir / "run_manifest.json"
        manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        manifest = manifest.model_copy(
            update={
                "artifact_hashes": list(
                    dict.fromkeys([*manifest.artifact_hashes, object_ref.sha256])
                ),
                "status": RunStatus.SUCCEEDED,
            }
        )
        atomic_write_bytes(
            manifest_path,
            canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE run SET status=?,ended_at=? WHERE run_id=?",
                (RunStatus.SUCCEEDED.value, datetime.now(UTC).isoformat(), run_id),
            )
        return ValidationReport(valid=True, artifact_hash=object_ref.sha256)

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or any(
            char not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
            for char in run_id
        ):
            raise ValueError("invalid run_id")
        run_dir = self.runtime_root / "codex_runs" / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Unknown run_id: {run_id}")
        return run_dir


def build_context_budget(
    *,
    skills: list[str],
    artifact_paths: list[Path],
    full_documents: list[str] | None = None,
    evidence_excerpts: list[str] | None = None,
) -> ContextBudgetReport:
    seen: set[Path] = set()
    selected: list[str] = []
    duplicates: list[str] = []
    total_bytes = 0
    for raw_path in artifact_paths:
        path = raw_path.resolve()
        if path in seen:
            duplicates.append(str(path))
            continue
        seen.add(path)
        selected.append(str(path))
        if path.is_file():
            total_bytes += path.stat().st_size
    return ContextBudgetReport(
        selected_skills=list(dict.fromkeys(skills)),
        selected_artifacts=selected,
        artifact_byte_size=total_bytes,
        estimated_text_tokens=(total_bytes + 3) // 4,
        full_documents_to_open=full_documents or [],
        evidence_excerpts_to_open=evidence_excerpts or [],
        duplicate_inputs_avoided=duplicates,
    )


def _collect_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" and isinstance(child, list):
                found.update(str(item) for item in child)
            else:
                found.update(_collect_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_evidence_ids(child))
    return found
