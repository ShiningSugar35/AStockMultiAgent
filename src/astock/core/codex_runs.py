"""Frozen-input Codex runs, strict artifact validation, audit, and recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from astock.core.atomic import atomic_write_bytes, atomic_write_text
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.policy import PolicyEngine
from astock.core.state import StateStore
from astock.schemas import (
    AuthorCollectionCoverageReport,
    BaseCasePack,
    CodexArtifactReference,
    CodexArtifactRole,
    CodexDraft,
    CodexRunInputManifest,
    ContextBudgetReport,
    CounterCasePack,
    DecisionPack,
    FinancialIntegrityEvidencePack,
    FrozenEvidencePack,
    HoldingEvidenceUpdate,
    HoldingReviewPack,
    Phase8AdmissionReport,
    PositionActionProposal,
    PositionMonitoringPlan,
    ResearchMemoArtifact,
    RunManifest,
    RunMode,
    RunStatus,
    ShadowEvaluationReport,
    SpecialistDelta,
    SpecialistDiagnosticReport,
    SpecialistRoutePlan,
    TradeProtocol,
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
    "HoldingEvidenceUpdate": HoldingEvidenceUpdate,
    "HoldingReviewPack": HoldingReviewPack,
    "PositionActionProposal": PositionActionProposal,
    "AuthorCollectionCoverageReport": AuthorCollectionCoverageReport,
    "FinancialIntegrityEvidencePack": FinancialIntegrityEvidencePack,
    "CounterCasePack": CounterCasePack,
    "DecisionPack": DecisionPack,
    "TradeProtocol": TradeProtocol,
    "ShadowEvaluationReport": ShadowEvaluationReport,
    "Phase8AdmissionReport": Phase8AdmissionReport,
}

_REGISTERED_PHASE4_OUTPUTS: dict[str, tuple[str, str]] = {
    "FrozenEvidencePack": ("pack_id", "FrozenEvidencePack"),
    "BaseCasePack": ("base_case_id", "BaseCasePack"),
    "SpecialistRoutePlan": ("route_plan_id", "SpecialistRoutePlan"),
    "SpecialistDelta": ("delta_id", "SpecialistDelta"),
    "SpecialistDiagnosticReport": (
        "diagnostic_id",
        "SpecialistDiagnosticReport",
    ),
    "ResearchMemoArtifact": ("memo_id", "ResearchMemoArtifact"),
    "PositionMonitoringPlan": ("plan_id", "PositionMonitoringPlan"),
    "HoldingEvidenceUpdate": ("update_id", "HoldingEvidenceUpdate"),
    "HoldingReviewPack": ("review_id", "HoldingReviewPack"),
    "PositionActionProposal": ("proposal_id", "PositionActionProposal"),
}

_REGISTERED_COMMITTEE_OUTPUTS: dict[str, tuple[str, str]] = {
    "CounterCasePack": ("counter_case_id", "CounterCasePack"),
    "DecisionPack": ("decision_id", "DecisionPack"),
    "TradeProtocol": ("protocol_id", "TradeProtocol"),
}

_REGISTERED_SHADOW_OUTPUTS: dict[str, tuple[str, str]] = {
    "ShadowEvaluationReport": ("report_id", "ShadowEvaluationReport"),
    "Phase8AdmissionReport": ("admission_id", "Phase8AdmissionReport"),
}

_REGISTERED_STRICT_OUTPUTS = {
    **_REGISTERED_PHASE4_OUTPUTS,
    **_REGISTERED_COMMITTEE_OUTPUTS,
    **_REGISTERED_SHADOW_OUTPUTS,
}


def registered_phase4_artifact_types() -> list[str]:
    return sorted(_REGISTERED_PHASE4_OUTPUTS)


def registered_committee_artifact_types() -> list[str]:
    return sorted(_REGISTERED_COMMITTEE_OUTPUTS)


def registered_shadow_artifact_types() -> list[str]:
    return sorted(_REGISTERED_SHADOW_OUTPUTS)


def registered_strict_artifact_types() -> list[str]:
    return sorted(_REGISTERED_STRICT_OUTPUTS)


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

    def resolve_artifact_reference(
        self,
        artifact_id: str,
        *,
        role: CodexArtifactRole = CodexArtifactRole.CONTEXT,
    ) -> CodexArtifactReference:
        row = self._artifact_registry_row(artifact_id)
        if row is None:
            raise ValueError(f"unknown registered artifact: {artifact_id}")
        object_hash = str(row["object_hash"])
        if not self.object_store.verify(object_hash):
            raise ValueError(f"registered artifact object is unavailable: {artifact_id}")
        return CodexArtifactReference(
            artifact_id=artifact_id,
            artifact_type=str(row["type"]),
            object_sha256=object_hash,
            role=role,
        )

    def initialize(
        self,
        request: dict[str, Any],
        *,
        context_budget: ContextBudgetReport | None = None,
        input_manifest: CodexRunInputManifest | dict[str, Any] | None = None,
    ) -> RunManifest:
        frozen_inputs = self._normalize_input_manifest(input_manifest, context_budget)
        self._validate_input_references(frozen_inputs)
        now = datetime.now(UTC)
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}"
        run_dir = self.runtime_root / "codex_runs" / run_id
        input_hashes = sorted(
            item.object_sha256 for item in frozen_inputs.artifact_references
        )
        manifest = RunManifest(
            run_id=run_id,
            mode=RunMode.CODEX_INTERACTIVE,
            request_hash=content_hash(request),
            as_of=now,
            node_plans=[],
            input_hashes=input_hashes,
            artifact_hashes=[],
            policy_version=self.policy_engine.version,
            provider_versions={},
            status=RunStatus.PENDING,
        )
        budget = context_budget or ContextBudgetReport(
            selected_skills=frozen_inputs.selected_skills,
            selected_artifacts=[
                item.artifact_id for item in frozen_inputs.artifact_references
            ],
        )
        atomic_write_bytes(run_dir / "request.json", canonical_json_bytes(request))
        atomic_write_bytes(
            run_dir / "input_manifest.json",
            canonical_json_bytes(frozen_inputs.model_dump(mode="json")),
        )
        atomic_write_bytes(
            run_dir / "context_budget.json",
            canonical_json_bytes(budget.model_dump(mode="json")),
        )
        atomic_write_bytes(
            run_dir / "run_manifest.json",
            canonical_json_bytes(manifest.model_dump(mode="json")),
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
            for item in frozen_inputs.artifact_references:
                connection.execute(
                    "INSERT INTO codex_run_input_index("
                    "run_id,artifact_id,artifact_type,artifact_role,object_hash,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        run_id,
                        item.artifact_id,
                        item.artifact_type,
                        item.role.value,
                        item.object_sha256,
                        now.isoformat(),
                    ),
                )
        return manifest

    def stage_draft(self, run_id: str, source: Path) -> Path:
        run_dir = self._run_dir(run_id)
        draft = CodexDraft.model_validate_json(source.read_text(encoding="utf-8"))
        destination = run_dir / "result_draft.json"
        payload = canonical_json_bytes(draft.model_dump(mode="json"))
        row = self._run_row(run_id)
        if row is None:
            raise FileNotFoundError(f"Unknown run_id: {run_id}")
        if str(row["status"]) == RunStatus.SUCCEEDED.value:
            if not destination.is_file() or destination.read_bytes() != payload:
                raise ValueError("a completed Codex run cannot replace its draft")
            return destination
        atomic_write_bytes(destination, payload)
        return destination

    def import_draft(self, run_id: str) -> ValidationReport:
        run_dir = self._run_dir(run_id)
        draft_path = run_dir / "result_draft.json"
        try:
            manifest = RunManifest.model_validate_json(
                (run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            frozen_inputs = CodexRunInputManifest.model_validate_json(
                (run_dir / "input_manifest.json").read_text(encoding="utf-8")
            )
            self._validate_input_references(frozen_inputs)
            self._validate_run_bindings(run_id, run_dir, manifest, frozen_inputs)
            draft = CodexDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return ValidationReport(valid=False, errors=["CODEX_RUN_OR_DRAFT_INVALID"])
        model = _ARTIFACT_MODELS.get(draft.artifact_type)
        if model is None:
            return ValidationReport(
                valid=False,
                errors=["UNSUPPORTED_CODEX_ARTIFACT_TYPE"],
            )
        try:
            payload = dict(draft.payload)
            payload.setdefault("created_at", draft.created_at)
            artifact = model.model_validate(payload)
        except ValidationError:
            return ValidationReport(valid=False, errors=["INVALID_CODEX_ARTIFACT_PAYLOAD"])

        errors: list[str] = []
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

        source_artifact_id: str | None = None
        source_object_hash: str | None = None
        if frozen_inputs.require_registered_output:
            try:
                source_artifact_id, source_object_hash = self._registered_output_identity(
                    draft.artifact_type,
                    artifact,
                )
                if source_artifact_id not in {
                    item.artifact_id for item in frozen_inputs.artifact_references
                }:
                    raise ValueError("STRICT_CODEX_OUTPUT_NOT_FROZEN_INPUT")
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            return ValidationReport(valid=False, errors=errors)

        validated = {
            "artifact_type": draft.artifact_type,
            "payload": artifact.model_dump(mode="json"),
            "citations": draft.citations,
            "input_hashes": manifest.input_hashes,
            "source_artifact_id": source_artifact_id,
        }
        object_ref = self.object_store.put_json(validated)
        draft_hash = sha256_bytes(draft_path.read_bytes())
        validated_artifact_id = f"CodexValidatedArtifact:{object_ref.sha256}"
        existing_output = self._output_row(run_id)
        if existing_output is not None and (
            str(existing_output["validated_artifact_id"]) != validated_artifact_id
            or str(existing_output["output_object_hash"]) != object_ref.sha256
        ):
            return ValidationReport(
                valid=False,
                errors=["a completed Codex run cannot replace its validated output"],
            )
        self.state.register_artifact(
            artifact_id=validated_artifact_id,
            artifact_type=draft.artifact_type,
            schema_version=artifact.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=manifest.input_hashes,
        )
        completed_manifest = manifest.model_copy(
            update={
                "artifact_hashes": list(
                    dict.fromkeys([*manifest.artifact_hashes, object_ref.sha256])
                ),
                "status": RunStatus.SUCCEEDED,
            }
        )
        self._write_validated_run_files(
            run_dir,
            run_id,
            draft,
            validated,
            completed_manifest,
            object_ref.sha256,
        )
        self._finalize_import(
            run_id=run_id,
            validated_artifact_id=validated_artifact_id,
            output_artifact_type=draft.artifact_type,
            output_object_hash=object_ref.sha256,
            draft_hash=draft_hash,
            source_artifact_id=source_artifact_id,
            source_object_hash=source_object_hash,
            input_count=len(manifest.input_hashes),
            citation_count=len(draft.citations),
            strict_registered_output=frozen_inputs.require_registered_output,
        )
        return ValidationReport(valid=True, artifact_hash=object_ref.sha256)

    def status(self, run_id: str) -> dict[str, object]:
        self._validate_run_id(run_id)
        row = self._run_row(run_id)
        if row is None:
            return {"status": "NOT_RUN", "run_id": run_id}
        with self.state.connect() as connection:
            inputs = connection.execute(
                "SELECT artifact_id,artifact_type,artifact_role,object_hash "
                "FROM codex_run_input_index WHERE run_id=? ORDER BY artifact_id",
                (run_id,),
            ).fetchall()
        output = self._output_row(run_id)
        return {
            "status": str(row["status"]),
            "run_id": run_id,
            "mode": str(row["mode"]),
            "as_of": str(row["as_of"]),
            "input_count": len(inputs),
            "inputs": [dict(item) for item in inputs],
            "output": dict(output) if output is not None else None,
        }

    def audit(self, run_id: str) -> dict[str, object]:
        self._validate_run_id(run_id)
        row = self._run_row(run_id)
        if row is None:
            return {"status": "NOT_RUN", "run_id": run_id}
        run_dir = self.runtime_root / "codex_runs" / run_id
        findings = {
            "RUN_DIRECTORY_MISSING": int(not run_dir.is_dir()),
            "REQUEST_MISMATCH": 0,
            "RUN_MANIFEST_INVALID": 0,
            "INPUT_MANIFEST_INVALID": 0,
            "INPUT_HASH_MISMATCH": 0,
            "INPUT_INDEX_MISMATCH": 0,
            "INPUT_ARTIFACT_MISMATCH": 0,
            "RUN_STATUS_MISMATCH": 0,
            "OUTPUT_INDEX_MISSING": 0,
            "DRAFT_FILE_MISMATCH": 0,
            "OUTPUT_FILE_MISMATCH": 0,
            "OUTPUT_ARTIFACT_MISMATCH": 0,
            "SOURCE_ARTIFACT_MISMATCH": 0,
        }
        manifest: RunManifest | None = None
        frozen_inputs: CodexRunInputManifest | None = None
        if run_dir.is_dir():
            try:
                request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
                findings["REQUEST_MISMATCH"] = int(
                    content_hash(request) != str(row["plan_hash"])
                )
            except (OSError, json.JSONDecodeError):
                findings["REQUEST_MISMATCH"] = 1
            try:
                manifest = RunManifest.model_validate_json(
                    (run_dir / "run_manifest.json").read_text(encoding="utf-8")
                )
                findings["RUN_MANIFEST_INVALID"] = int(
                    manifest.run_id != run_id
                    or manifest.request_hash != str(row["plan_hash"])
                    or manifest.mode.value != str(row["mode"])
                    or manifest.as_of.isoformat() != str(row["as_of"])
                    or manifest.policy_version != self.policy_engine.version
                )
                findings["RUN_STATUS_MISMATCH"] = int(
                    manifest.status.value != str(row["status"])
                )
            except (OSError, ValidationError):
                findings["RUN_MANIFEST_INVALID"] = 1
            try:
                frozen_inputs = CodexRunInputManifest.model_validate_json(
                    (run_dir / "input_manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                findings["INPUT_MANIFEST_INVALID"] = 1

        with self.state.connect() as connection:
            input_rows = connection.execute(
                "SELECT artifact_id,artifact_type,artifact_role,object_hash "
                "FROM codex_run_input_index WHERE run_id=? ORDER BY artifact_id",
                (run_id,),
            ).fetchall()
        if frozen_inputs is not None:
            expected_hashes = sorted(
                item.object_sha256 for item in frozen_inputs.artifact_references
            )
            findings["INPUT_HASH_MISMATCH"] = int(
                manifest is None or manifest.input_hashes != expected_hashes
            )
            expected_rows = sorted(
                (
                    item.artifact_id,
                    item.artifact_type,
                    item.role.value,
                    item.object_sha256,
                )
                for item in frozen_inputs.artifact_references
            )
            actual_rows = [tuple(item) for item in input_rows]
            findings["INPUT_INDEX_MISMATCH"] = int(actual_rows != expected_rows)
            for item in frozen_inputs.artifact_references:
                registry = self._artifact_registry_row(item.artifact_id)
                findings["INPUT_ARTIFACT_MISMATCH"] += int(
                    registry is None
                    or str(registry["type"]) != item.artifact_type
                    or str(registry["object_hash"]) != item.object_sha256
                    or not self.object_store.verify(item.object_sha256)
                )
        elif input_rows:
            findings["INPUT_INDEX_MISMATCH"] = len(input_rows)

        output = self._output_row(run_id)
        succeeded = str(row["status"]) == RunStatus.SUCCEEDED.value
        findings["OUTPUT_INDEX_MISSING"] = int(succeeded and output is None)
        if output is not None:
            output_hash = str(output["output_object_hash"])
            artifact_id = str(output["validated_artifact_id"])
            registry = self._artifact_registry_row(artifact_id)
            expected_inputs = manifest.input_hashes if manifest is not None else []
            registry_inputs: list[str] = []
            if registry is not None:
                try:
                    registry_inputs = list(json.loads(str(registry["input_hashes_json"])))
                except (json.JSONDecodeError, TypeError):
                    registry_inputs = []
            findings["OUTPUT_ARTIFACT_MISMATCH"] = int(
                registry is None
                or str(registry["type"]) != str(output["output_artifact_type"])
                or str(registry["object_hash"]) != output_hash
                or registry_inputs != expected_inputs
                or not self.object_store.verify(output_hash)
            )
            validated_path = run_dir / "validated_result.json"
            citations_path = run_dir / "citations.json"
            draft_path = run_dir / "result_draft.json"
            try:
                findings["DRAFT_FILE_MISMATCH"] = int(
                    sha256_bytes(draft_path.read_bytes()) != str(output["draft_hash"])
                )
            except OSError:
                findings["DRAFT_FILE_MISMATCH"] = 1
            try:
                validated_bytes = validated_path.read_bytes()
                wrapper = json.loads(validated_bytes)
                citations = json.loads(citations_path.read_text(encoding="utf-8"))
                findings["OUTPUT_FILE_MISMATCH"] = int(
                    sha256_bytes(validated_bytes) != output_hash
                    or wrapper.get("citations") != citations
                    or len(citations) != int(str(output["citation_count"]))
                    or manifest is None
                    or output_hash not in manifest.artifact_hashes
                )
            except (OSError, json.JSONDecodeError, TypeError):
                findings["OUTPUT_FILE_MISMATCH"] = 1
            source_id = output["source_artifact_id"]
            source_hash = output["source_object_hash"]
            if int(str(output["strict_registered_output"])):
                source = (
                    self._artifact_registry_row(str(source_id))
                    if source_id is not None
                    else None
                )
                findings["SOURCE_ARTIFACT_MISMATCH"] = int(
                    source is None
                    or source_hash is None
                    or str(source["object_hash"]) != str(source_hash)
                    or not self.object_store.verify(str(source_hash))
                )
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "run_id": run_id,
            "run_status": str(row["status"]),
            "input_count": len(input_rows),
            "output_artifact_id": (
                str(output["validated_artifact_id"]) if output is not None else None
            ),
            "finding_codes": finding_codes,
            "finding_counts": findings,
        }

    def recover(self, run_id: str) -> ValidationReport:
        try:
            run_dir = self._run_dir(run_id)
            manifest = RunManifest.model_validate_json(
                (run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            frozen_inputs = CodexRunInputManifest.model_validate_json(
                (run_dir / "input_manifest.json").read_text(encoding="utf-8")
            )
            self._validate_input_references(frozen_inputs)
            self._validate_run_bindings(run_id, run_dir, manifest, frozen_inputs)
        except (OSError, ValidationError, ValueError):
            return ValidationReport(valid=False, errors=["CODEX_RUN_INPUTS_INVALID"])
        audit = self.audit(run_id)
        output = self._output_row(run_id)
        if audit["status"] == "PASS" and output is not None:
            return ValidationReport(
                valid=True,
                artifact_hash=str(output["output_object_hash"]),
            )
        if not (run_dir / "result_draft.json").is_file():
            return ValidationReport(
                valid=False,
                errors=["Codex run has no staged draft to recover"],
            )
        return self.import_draft(run_id)

    def _normalize_input_manifest(
        self,
        value: CodexRunInputManifest | dict[str, Any] | None,
        context_budget: ContextBudgetReport | None,
    ) -> CodexRunInputManifest:
        if isinstance(value, CodexRunInputManifest):
            return value
        if value is None:
            return CodexRunInputManifest(
                selected_skills=(context_budget.selected_skills if context_budget else []),
                legacy_artifact_paths=(
                    context_budget.selected_artifacts if context_budget else []
                ),
            )
        if "artifacts" in value and "artifact_references" not in value:
            return CodexRunInputManifest(
                selected_skills=(context_budget.selected_skills if context_budget else []),
                legacy_artifact_paths=[str(item) for item in value.get("artifacts", [])],
            )
        return CodexRunInputManifest.model_validate(value)

    def _validate_input_references(self, manifest: CodexRunInputManifest) -> None:
        for item in manifest.artifact_references:
            row = self._artifact_registry_row(item.artifact_id)
            if row is None:
                raise ValueError(f"unknown registered artifact: {item.artifact_id}")
            if (
                str(row["type"]) != item.artifact_type
                or str(row["object_hash"]) != item.object_sha256
            ):
                raise ValueError(f"registered artifact identity mismatch: {item.artifact_id}")
            if not self.object_store.verify(item.object_sha256):
                raise ValueError(f"registered artifact object is unavailable: {item.artifact_id}")

    def _registered_output_identity(
        self,
        artifact_type: str,
        artifact: AStockModel,
    ) -> tuple[str, str]:
        identity = _REGISTERED_STRICT_OUTPUTS.get(artifact_type)
        if identity is None:
            raise ValueError("STRICT_CODEX_OUTPUT_TYPE_UNSUPPORTED")
        field_name, prefix = identity
        primary_id = getattr(artifact, field_name, None)
        if not isinstance(primary_id, str) or not primary_id:
            raise ValueError("STRICT_CODEX_OUTPUT_ID_MISSING")
        artifact_id = f"{prefix}:{primary_id}"
        row = self._artifact_registry_row(artifact_id)
        object_hash = sha256_bytes(
            canonical_json_bytes(artifact.model_dump(mode="json"))
        )
        if (
            row is None
            or str(row["type"]) != artifact_type
            or str(row["object_hash"]) != object_hash
            or not self.object_store.verify(object_hash)
        ):
            raise ValueError("STRICT_CODEX_OUTPUT_NOT_REGISTERED")
        return artifact_id, object_hash

    def _validate_run_bindings(
        self,
        run_id: str,
        run_dir: Path,
        manifest: RunManifest,
        frozen_inputs: CodexRunInputManifest,
    ) -> None:
        row = self._run_row(run_id)
        if row is None:
            raise ValueError("CODEX_RUN_BINDING_INVALID")
        try:
            request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("CODEX_RUN_BINDING_INVALID") from exc
        expected_hashes = sorted(
            item.object_sha256 for item in frozen_inputs.artifact_references
        )
        with self.state.connect() as connection:
            input_rows = connection.execute(
                "SELECT artifact_id,artifact_type,artifact_role,object_hash "
                "FROM codex_run_input_index WHERE run_id=? ORDER BY artifact_id",
                (run_id,),
            ).fetchall()
        expected_rows = sorted(
            (
                item.artifact_id,
                item.artifact_type,
                item.role.value,
                item.object_sha256,
            )
            for item in frozen_inputs.artifact_references
        )
        if (
            manifest.run_id != run_id
            or manifest.mode is not RunMode.CODEX_INTERACTIVE
            or manifest.policy_version != self.policy_engine.version
            or manifest.request_hash != str(row["plan_hash"])
            or content_hash(request) != manifest.request_hash
            or manifest.input_hashes != expected_hashes
            or [tuple(item) for item in input_rows] != expected_rows
        ):
            raise ValueError("CODEX_RUN_BINDING_INVALID")

    def _write_validated_run_files(
        self,
        run_dir: Path,
        run_id: str,
        draft: CodexDraft,
        validated: dict[str, object],
        manifest: RunManifest,
        object_hash: str,
    ) -> None:
        atomic_write_bytes(run_dir / "validated_result.json", canonical_json_bytes(validated))
        atomic_write_bytes(run_dir / "citations.json", canonical_json_bytes(draft.citations))
        atomic_write_text(
            run_dir / "run_summary.md",
            f"# Codex Run {run_id}\n\nValidated `{draft.artifact_type}` as "
            f"`{object_hash}`.\n",
        )
        atomic_write_bytes(
            run_dir / "run_manifest.json",
            canonical_json_bytes(manifest.model_dump(mode="json")),
        )

    def _finalize_import(
        self,
        *,
        run_id: str,
        validated_artifact_id: str,
        output_artifact_type: str,
        output_object_hash: str,
        draft_hash: str,
        source_artifact_id: str | None,
        source_object_hash: str | None,
        input_count: int,
        citation_count: int,
        strict_registered_output: bool,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        expected = (
            validated_artifact_id,
            output_artifact_type,
            output_object_hash,
            draft_hash,
            source_artifact_id,
            source_object_hash,
            input_count,
            citation_count,
            int(strict_registered_output),
            RunStatus.SUCCEEDED.value,
        )
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT validated_artifact_id,output_artifact_type,output_object_hash,draft_hash,"
                "source_artifact_id,source_object_hash,input_count,citation_count,"
                "strict_registered_output,status FROM codex_run_output_index WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is not None and tuple(existing) != expected:
                raise ValueError(f"Codex run output identity collision: {run_id}")
            if existing is None:
                connection.execute(
                    "INSERT INTO codex_run_output_index("
                    "run_id,validated_artifact_id,output_artifact_type,output_object_hash,draft_hash,"
                    "source_artifact_id,source_object_hash,input_count,citation_count,"
                    "strict_registered_output,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, *expected, now),
                )
            connection.execute(
                "UPDATE run SET status=?,ended_at=? WHERE run_id=?",
                (RunStatus.SUCCEEDED.value, now, run_id),
            )

    def _artifact_registry_row(self, artifact_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT artifact_id,type,schema_version,object_hash,input_hashes_json,created_at "
                "FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return dict(row) if row else None

    def _run_row(self, run_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_id,mode,status,as_of,plan_hash,started_at,ended_at "
                "FROM run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def _output_row(self, run_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_id,validated_artifact_id,output_artifact_type,"
                "output_object_hash,draft_hash,source_artifact_id,source_object_hash,input_count,"
                "citation_count,strict_registered_output,status,created_at "
                "FROM codex_run_output_index WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def _run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        run_dir = self.runtime_root / "codex_runs" / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Unknown run_id: {run_id}")
        return run_dir

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not run_id or any(
            char not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
            for char in run_id
        ):
            raise ValueError("invalid run_id")


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


__all__ = [
    "CodexRunService",
    "build_context_budget",
    "registered_committee_artifact_types",
    "registered_phase4_artifact_types",
    "registered_shadow_artifact_types",
    "registered_strict_artifact_types",
]
