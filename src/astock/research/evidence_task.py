"""Bridge research intake requests to evidence collection tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import EvidenceCollectionTask, ResearchRequest, ResearchRequestModule


@dataclass(frozen=True, slots=True)
class EvidenceCollectionTaskExecution:
    task: EvidenceCollectionTask
    artifact_id: str
    object_sha256: str
    reused_existing: bool


class EvidenceCollectionTaskService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def create_task(self, request_artifact_id: str) -> EvidenceCollectionTaskExecution:
        request, request_object_hash = self._load_request(request_artifact_id)
        task = EvidenceCollectionTask(
            request_artifact_id=request_artifact_id,
            company=request.company,
            ticker=request.ticker,
            required_sources=self._required_sources(request.requested_modules),
            created_at=datetime.now(UTC),
        )
        artifact_id = (
            f"EvidenceCollectionTask:{content_hash({'request_artifact_id': request_artifact_id})}"
        )
        existing_object_hash = self._artifact_object_hash(artifact_id)
        if existing_object_hash is not None:
            return EvidenceCollectionTaskExecution(
                task=task,
                artifact_id=artifact_id,
                object_sha256=existing_object_hash,
                reused_existing=True,
            )
        object_ref = self.objects.put_json(task.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="EvidenceCollectionTask",
            schema_version=task.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[request_object_hash],
        )
        return EvidenceCollectionTaskExecution(
            task=task,
            artifact_id=artifact_id,
            object_sha256=object_ref.sha256,
            reused_existing=False,
        )

    def _load_request(self, request_artifact_id: str) -> tuple[ResearchRequest, str]:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
                (request_artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown research request artifact: {request_artifact_id}")
        if str(row["type"]) != "ResearchRequest":
            raise ValueError(f"artifact is not a research request: {request_artifact_id}")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(
                f"research request artifact object is unavailable: {request_artifact_id}"
            )
        request = ResearchRequest.model_validate_json(self.objects.get_bytes(object_hash))
        return request, object_hash

    @staticmethod
    def _required_sources(modules: list[ResearchRequestModule]) -> list[str]:
        return sorted(set(module.value for module in modules))

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
