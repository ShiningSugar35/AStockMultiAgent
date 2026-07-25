"""Transform EvidenceCollectionRun artifacts into lightweight analysis packs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import EvidenceCollectionRun, EvidenceCollectionTask, EvidencePack


@dataclass(frozen=True, slots=True)
class EvidencePackExecution:
    pack: EvidencePack
    artifact_id: str
    object_sha256: str
    reused_existing: bool


class EvidencePackService:
    """Create one deterministic EvidencePack from a prior EvidenceCollectionRun.

    EvidencePack is the analysis consumption artifact for later research stages;
    it intentionally complements rather than replaces FrozenEvidencePack, which
    remains the immutable audit output used for integrity and reporting.
    """

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def create_pack(self, run_artifact_id: str) -> EvidencePackExecution:
        run, run_object_hash = self._load_run(run_artifact_id)
        task = self._load_task(run.task_artifact_id)
        pack = EvidencePack(
            run_artifact_id=run_artifact_id,
            company=task.company,
            ticker=task.ticker,
            evidence_items=run.collected_items,
            missing_items=run.missing_items,
            generated_at=datetime.now(UTC),
        )
        artifact_id = f"EvidencePack:{content_hash({'run_artifact_id': run_artifact_id})}"
        existing_object_hash = self._artifact_object_hash(artifact_id)
        if existing_object_hash is not None:
            return EvidencePackExecution(
                pack=EvidencePack.model_validate_json(
                    self.objects.get_bytes(existing_object_hash)
                ),
                artifact_id=artifact_id,
                object_sha256=existing_object_hash,
                reused_existing=True,
            )
        object_ref = self.objects.put_json(pack.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="EvidencePack",
            schema_version=pack.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[run_object_hash],
        )
        return EvidencePackExecution(
            pack=pack,
            artifact_id=artifact_id,
            object_sha256=object_ref.sha256,
            reused_existing=False,
        )

    def _load_run(self, run_artifact_id: str) -> tuple[EvidenceCollectionRun, str]:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
                (run_artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown evidence collection run artifact: {run_artifact_id}")
        if str(row["type"]) != "EvidenceCollectionRun":
            raise ValueError(f"artifact is not a evidence collection run: {run_artifact_id}")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(
                f"evidence collection run artifact object is unavailable: {run_artifact_id}"
            )
        run = EvidenceCollectionRun.model_validate_json(self.objects.get_bytes(object_hash))
        return run, object_hash

    def _load_task(self, task_artifact_id: str) -> EvidenceCollectionTask:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
                (task_artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown evidence collection task artifact: {task_artifact_id}")
        if str(row["type"]) != "EvidenceCollectionTask":
            raise ValueError(f"artifact is not a evidence collection task: {task_artifact_id}")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(
                f"evidence collection task artifact object is unavailable: {task_artifact_id}"
            )
        task = EvidenceCollectionTask.model_validate_json(self.objects.get_bytes(object_hash))
        return task

    def _artifact_object_hash(self, artifact_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        object_hash = str(row["object_hash"])
        return object_hash if self.objects.verify(object_hash) else None
