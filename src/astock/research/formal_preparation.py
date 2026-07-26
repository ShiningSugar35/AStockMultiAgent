"""Formal evidence-readiness gate ending at FrozenEvidencePack."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.financial_integrity.repository import FinancialIntegrityRepository
from astock.pit.repository import PointInTimeRepository
from astock.pit.service import PointInTimeService
from astock.research.service import ResearchCoreService
from astock.schemas import (
    ClaimStatus,
    ConflictResolutionStatus,
    EvidenceCollectionRun,
    EvidenceCollectionRunStatus,
    EvidenceCollectionTask,
    EvidenceFreezeRequest,
    EvidencePack,
    FinancialIntegrityEvidencePack,
    FrozenEvidencePack,
    ResearchCoreConfig,
    ResearchPreparationManifest,
    ResearchPreparationRequest,
    ResearchPreparationStatus,
    ResearchRequest,
    RunStatus,
)
from astock.schemas.base import AStockModel

_ModelT = TypeVar("_ModelT", bound=AStockModel)


class ResearchPreparationRejectedError(ValueError):
    """The request or durable lineage is invalid and must not create a manifest."""


@dataclass(frozen=True, slots=True)
class ResearchPreparationExecution:
    manifest: ResearchPreparationManifest
    manifest_artifact_id: str
    manifest_object_sha256: str
    reused_existing: bool


@dataclass(frozen=True, slots=True)
class _Artifact:
    artifact_id: str
    object_hash: str
    input_hashes: list[str]


class FormalResearchPreparationService:
    """Validate formal readiness and reuse the existing frozen-evidence service."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        research_core_config: ResearchCoreConfig,
    ) -> None:
        self.state = state
        self.objects = objects
        self.financial_repository = FinancialIntegrityRepository(state, objects)
        self.evidence_repository = EvidenceRepository(state)
        self.pit_repository = PointInTimeRepository(state)
        self.research_core = ResearchCoreService(state, objects, research_core_config)

    def prepare(
        self,
        request: ResearchPreparationRequest,
    ) -> ResearchPreparationExecution:
        request = request.model_copy(update={"as_of": request.as_of.astimezone(UTC)})
        research_request, request_artifact = self._load_artifact(
            request.research_request_artifact_id,
            "ResearchRequest",
            ResearchRequest,
        )
        evidence_pack, pack_artifact = self._load_artifact(
            request.evidence_pack_artifact_id,
            "EvidencePack",
            EvidencePack,
        )
        run, run_artifact = self._load_artifact(
            evidence_pack.run_artifact_id,
            "EvidenceCollectionRun",
            EvidenceCollectionRun,
        )
        task, task_artifact = self._load_artifact(
            run.task_artifact_id,
            "EvidenceCollectionTask",
            EvidenceCollectionTask,
        )
        self._validate_collection_lineage(
            request,
            research_request,
            request_artifact,
            task,
            task_artifact,
            run,
            run_artifact,
            evidence_pack,
            pack_artifact,
        )

        blocking_codes: set[str] = set()
        if (
            run.status is not EvidenceCollectionRunStatus.COMPLETED
            or not run.collected_items
            or bool(run.missing_items)
        ):
            blocking_codes.add("EVIDENCE_COLLECTION_INCOMPLETE")
        if not evidence_pack.evidence_items or evidence_pack.missing_items:
            blocking_codes.add("FORMAL_EVIDENCE_MISSING")
        for artifact_id in run.collected_items:
            self._validate_collected_artifact(artifact_id)

        financial_pack, financial_object_hash = self._load_financial_pack(
            request,
            research_request,
            blocking_codes,
        )
        financial_manual_task_ids = (
            sorted(task.task_id for task in financial_pack.manual_tasks)
            if financial_pack is not None
            else []
        )
        if financial_pack is not None:
            if financial_pack.status is RunStatus.NEEDS_INFO:
                blocking_codes.add("FINANCIAL_AUDIT_NEEDS_INFO")
            if financial_pack.hard_blocks:
                blocking_codes.add("FINANCIAL_HARD_BLOCK")

        self._validate_claim_readiness(request, research_request, blocking_codes)

        identity = content_hash(
            {
                "research_request_object_hash": request_artifact.object_hash,
                "evidence_pack_object_hash": pack_artifact.object_hash,
                "financial_audit_run_id": request.financial_audit_run_id,
                "financial_audit_object_hash": financial_object_hash,
                "claim_ids": request.claim_ids,
                "as_of": request.as_of,
                "formal_historical": request.formal_historical,
                "allow_approximated": request.allow_approximated,
            }
        )
        manifest_artifact_id = f"ResearchPreparationManifest:{identity}"
        existing = self._existing_manifest(manifest_artifact_id)
        if existing is not None:
            self._validate_existing_manifest(existing, request)
            return ResearchPreparationExecution(
                manifest=existing[0],
                manifest_artifact_id=manifest_artifact_id,
                manifest_object_sha256=existing[1],
                reused_existing=True,
            )

        input_object_hash_set: set[str] = {
            request_artifact.object_hash,
            task_artifact.object_hash,
            run_artifact.object_hash,
            pack_artifact.object_hash,
        }
        if financial_object_hash is not None:
            input_object_hash_set.add(financial_object_hash)
        input_object_hashes = sorted(input_object_hash_set)
        frozen_pack_id: str | None = None
        frozen_artifact_id: str | None = None
        status = ResearchPreparationStatus.NEEDS_INFO
        if not blocking_codes:
            frozen = self.research_core.freeze_evidence(
                EvidenceFreezeRequest(
                    company_id=research_request.ticker,
                    as_of=request.as_of,
                    claim_ids=request.claim_ids,
                    formal_historical=request.formal_historical,
                    allow_approximated=request.allow_approximated,
                )
            )
            frozen_pack_id = frozen.pack.pack_id
            frozen_artifact_id = f"FrozenEvidencePack:{frozen.pack.pack_id}"
            input_object_hashes.append(frozen.object_sha256)
            input_object_hashes = sorted(set(input_object_hashes))
            status = ResearchPreparationStatus.READY_FOR_BASE_CASE

        codes = sorted(blocking_codes)
        manifest = ResearchPreparationManifest(
            research_request_artifact_id=request.research_request_artifact_id,
            evidence_pack_artifact_id=request.evidence_pack_artifact_id,
            financial_audit_run_id=request.financial_audit_run_id,
            company_id=research_request.ticker,
            ticker=research_request.ticker,
            as_of=request.as_of,
            status=status,
            claim_ids=request.claim_ids,
            blocking_codes=codes,
            required_action_codes=codes,
            financial_manual_task_ids=financial_manual_task_ids,
            frozen_evidence_pack_id=frozen_pack_id,
            frozen_evidence_pack_artifact_id=frozen_artifact_id,
            input_object_hashes=input_object_hashes,
        )
        object_ref = self.objects.put_json(manifest.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=manifest_artifact_id,
            artifact_type="ResearchPreparationManifest",
            schema_version=manifest.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=manifest.input_object_hashes,
        )
        if status is ResearchPreparationStatus.READY_FOR_BASE_CASE:
            self.state.set_checkpoint(
                scope_type="research-formal-preparation",
                scope_key=identity,
                cursor={
                    "manifest_artifact_id": manifest_artifact_id,
                    "frozen_evidence_pack_id": frozen_pack_id,
                },
                status="SUCCEEDED",
                object_hash=object_ref.sha256,
            )
        return ResearchPreparationExecution(
            manifest=manifest,
            manifest_artifact_id=manifest_artifact_id,
            manifest_object_sha256=object_ref.sha256,
            reused_existing=False,
        )

    def _load_artifact(
        self,
        artifact_id: str,
        expected_type: str,
        model_type: type[_ModelT],
    ) -> tuple[_ModelT, _Artifact]:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,object_hash,input_hashes_json FROM artifact_registry "
                "WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ResearchPreparationRejectedError(f"unknown {expected_type} artifact")
        if str(row["type"]) != expected_type:
            raise ResearchPreparationRejectedError(f"artifact is not {expected_type}")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ResearchPreparationRejectedError(f"{expected_type} object is unavailable")
        try:
            model = model_type.model_validate_json(self.objects.get_bytes(object_hash))
            input_hashes = json.loads(str(row["input_hashes_json"]))
        except (TypeError, ValueError) as exc:
            raise ResearchPreparationRejectedError(
                f"{expected_type} artifact is invalid"
            ) from exc
        if not isinstance(input_hashes, list) or not all(
            isinstance(item, str) for item in input_hashes
        ):
            raise ResearchPreparationRejectedError(
                f"{expected_type} artifact lineage is invalid"
            )
        return model, _Artifact(artifact_id, object_hash, input_hashes)

    def _validate_collection_lineage(
        self,
        request: ResearchPreparationRequest,
        research_request: ResearchRequest,
        request_artifact: _Artifact,
        task: EvidenceCollectionTask,
        task_artifact: _Artifact,
        run: EvidenceCollectionRun,
        run_artifact: _Artifact,
        evidence_pack: EvidencePack,
        pack_artifact: _Artifact,
    ) -> None:
        if task.request_artifact_id != request.research_request_artifact_id:
            raise ResearchPreparationRejectedError("research task lineage mismatch")
        if run.task_artifact_id != task_artifact.artifact_id:
            raise ResearchPreparationRejectedError("research run lineage mismatch")
        if evidence_pack.run_artifact_id != run_artifact.artifact_id:
            raise ResearchPreparationRejectedError("evidence pack lineage mismatch")
        if request_artifact.object_hash not in task_artifact.input_hashes:
            raise ResearchPreparationRejectedError("research task input lineage mismatch")
        if task_artifact.object_hash not in run_artifact.input_hashes:
            raise ResearchPreparationRejectedError("research run input lineage mismatch")
        if run_artifact.object_hash not in pack_artifact.input_hashes:
            raise ResearchPreparationRejectedError("evidence pack input lineage mismatch")
        if (
            evidence_pack.evidence_items != run.collected_items
            or evidence_pack.missing_items != run.missing_items
        ):
            raise ResearchPreparationRejectedError("evidence pack content lineage mismatch")
        company_ticker_pairs = {
            (research_request.company, research_request.ticker),
            (task.company, task.ticker),
            (evidence_pack.company, evidence_pack.ticker),
        }
        if len(company_ticker_pairs) != 1:
            raise ResearchPreparationRejectedError("research company lineage mismatch")

    def _validate_collected_artifact(self, artifact_id: str) -> None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None or not self.objects.verify(str(row["object_hash"])):
            raise ResearchPreparationRejectedError("collected evidence artifact is unavailable")

    def _load_financial_pack(
        self,
        request: ResearchPreparationRequest,
        research_request: ResearchRequest,
        blocking_codes: set[str],
    ) -> tuple[FinancialIntegrityEvidencePack | None, str | None]:
        record = self.financial_repository.get_run(request.financial_audit_run_id)
        if record is None or record.report_object_hash is None:
            blocking_codes.add("FINANCIAL_AUDIT_NOT_FOUND")
            return None, None
        artifact_id = f"FinancialIntegrityEvidencePack:{request.financial_audit_run_id}"
        pack, artifact = self._load_artifact(
            artifact_id,
            "FinancialIntegrityEvidencePack",
            FinancialIntegrityEvidencePack,
        )
        repository_pack = self.financial_repository.get_pack(
            request.financial_audit_run_id
        )
        if repository_pack is None or repository_pack != pack:
            raise ResearchPreparationRejectedError(
                "financial audit repository lineage mismatch"
            )
        if artifact.object_hash != record.report_object_hash:
            raise ResearchPreparationRejectedError("financial audit object lineage mismatch")
        if pack.audit_run_id != request.financial_audit_run_id:
            raise ResearchPreparationRejectedError("financial audit identity mismatch")
        if (
            record.request_hash != pack.request_hash
            or record.status is not pack.status
            or datetime.fromisoformat(record.as_of) != pack.as_of
        ):
            raise ResearchPreparationRejectedError("financial audit run lineage mismatch")
        if pack.company_id != research_request.ticker or record.company_id != pack.company_id:
            raise ResearchPreparationRejectedError("financial audit company mismatch")
        if pack.as_of > request.as_of:
            raise ResearchPreparationRejectedError("financial audit is newer than requested as_of")
        return pack, artifact.object_hash

    def _validate_claim_readiness(
        self,
        request: ResearchPreparationRequest,
        research_request: ResearchRequest,
        blocking_codes: set[str],
    ) -> None:
        for claim_id in request.claim_ids:
            bundle = self.evidence_repository.get_claim_bundle(claim_id)
            if bundle is None or not bundle.links or bundle.claim.status is ClaimStatus.REJECTED:
                blocking_codes.add("CLAIM_SCOPE_EMPTY")
                continue
            if bundle.claim.subject_id != research_request.ticker:
                blocking_codes.add("CLAIM_COMPANY_MISMATCH")
                continue
            if bundle.claim.as_of > request.as_of:
                blocking_codes.add("PIT_METADATA_REQUIRED")
                continue
            if (
                bundle.conflict is not None
                and bundle.conflict.resolution_status is ConflictResolutionStatus.OPEN
            ):
                blocking_codes.add("OPEN_EVIDENCE_CONFLICT")
            for link in bundle.links:
                evidence = self.evidence_repository.get_evidence(link.evidence_id)
                if evidence is None:
                    raise ResearchPreparationRejectedError(
                        "claim references unavailable evidence"
                    )
                if (
                    evidence.available_to_system_at > request.as_of
                    or (evidence.valid_from is not None and evidence.valid_from > request.as_of)
                    or (evidence.valid_to is not None and evidence.valid_to < request.as_of)
                ):
                    blocking_codes.add("PIT_METADATA_REQUIRED")
                    continue
                metadata = self.pit_repository.for_snapshot(evidence.snapshot_id)
                if len(metadata) != 1:
                    blocking_codes.add("PIT_METADATA_REQUIRED")
                    continue
                try:
                    PointInTimeService.assert_usable(
                        metadata[0],
                        request.as_of,
                        formal_historical=request.formal_historical,
                        allow_approximated=request.allow_approximated,
                    )
                except ValueError:
                    blocking_codes.add("PIT_METADATA_REQUIRED")

    def _existing_manifest(
        self,
        artifact_id: str,
    ) -> tuple[ResearchPreparationManifest, str] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["type"]) != "ResearchPreparationManifest":
            raise ResearchPreparationRejectedError("preparation manifest type mismatch")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ResearchPreparationRejectedError("preparation manifest object is unavailable")
        try:
            manifest = ResearchPreparationManifest.model_validate_json(
                self.objects.get_bytes(object_hash)
            )
        except ValueError as exc:
            raise ResearchPreparationRejectedError(
                "preparation manifest object is invalid"
            ) from exc
        return manifest, object_hash

    def _validate_existing_manifest(
        self,
        existing: tuple[ResearchPreparationManifest, str],
        request: ResearchPreparationRequest,
    ) -> None:
        manifest, _ = existing
        if (
            manifest.research_request_artifact_id
            != request.research_request_artifact_id
            or manifest.evidence_pack_artifact_id != request.evidence_pack_artifact_id
            or manifest.financial_audit_run_id != request.financial_audit_run_id
            or manifest.claim_ids != request.claim_ids
            or manifest.as_of != request.as_of
        ):
            raise ResearchPreparationRejectedError("preparation manifest identity collision")
        if manifest.status is ResearchPreparationStatus.READY_FOR_BASE_CASE:
            assert manifest.frozen_evidence_pack_artifact_id is not None
            assert manifest.frozen_evidence_pack_id is not None
            frozen_pack, _ = self._load_artifact(
                manifest.frozen_evidence_pack_artifact_id,
                "FrozenEvidencePack",
                FrozenEvidencePack,
            )
            if (
                frozen_pack.pack_id != manifest.frozen_evidence_pack_id
                or frozen_pack.company_id != manifest.company_id
                or frozen_pack.as_of != manifest.as_of
            ):
                raise ResearchPreparationRejectedError(
                    "frozen evidence pack manifest lineage mismatch"
                )
            if (
                self.research_core.repository.get_evidence_pack(
                    manifest.frozen_evidence_pack_id
                )
                is None
            ):
                raise ResearchPreparationRejectedError(
                    "frozen evidence pack repository lineage is unavailable"
                )
