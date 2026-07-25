"""Execute a minimal evidence collection task into a traceable run artifact."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    EvidenceCollectionRun,
    EvidenceCollectionRunStatus,
    EvidenceCollectionTask,
)


@dataclass(frozen=True, slots=True)
class EvidenceCollectionRunExecution:
    run: EvidenceCollectionRun
    artifact_id: str
    object_sha256: str
    reused_existing: bool


class EvidenceCollectionRunService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def create_run(self, task_artifact_id: str) -> EvidenceCollectionRunExecution:
        task, task_object_hash = self._load_task(task_artifact_id)
        now = datetime.now(UTC)
        run = EvidenceCollectionRun(
            task_artifact_id=task_artifact_id,
            status=EvidenceCollectionRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            collected_items=[],
            missing_items=task.required_sources,
        )
        artifact_id = (
            f"EvidenceCollectionRun:{content_hash({'task_artifact_id': task_artifact_id})}"
        )
        existing_object_hash = self._artifact_object_hash(artifact_id)
        if existing_object_hash is not None:
            run = EvidenceCollectionRun.model_validate_json(
                self.objects.get_bytes(existing_object_hash)
            )
            return EvidenceCollectionRunExecution(
                artifact_id=artifact_id,
                object_sha256=existing_object_hash,
                run=run,
                reused_existing=True,
            )
        object_ref = self.objects.put_json(run.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="EvidenceCollectionRun",
            schema_version=run.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[task_object_hash],
        )
        return EvidenceCollectionRunExecution(
            run=run,
            artifact_id=artifact_id,
            object_sha256=object_ref.sha256,
            reused_existing=False,
        )

    def _load_task(self, task_artifact_id: str) -> tuple[EvidenceCollectionTask, str]:
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
        return task, object_hash

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
