"""Creation and formal historical-use policy for PIT metadata."""

from __future__ import annotations

from datetime import date, datetime

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.pit.repository import PointInTimeRepository
from astock.schemas import (
    AvailabilityBasis,
    PointInTimeMetadata,
    PointInTimeStatus,
)


class PointInTimeService:
    def __init__(
        self,
        repository: PointInTimeRepository,
        state: StateStore,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.repository = repository
        self.state = state
        self.object_store = object_store

    def create(
        self,
        *,
        source_id: str,
        source_document_id: str | None = None,
        source_snapshot_id: str | None = None,
        period_end: date | None = None,
        published_at: datetime | None = None,
        effective_at: datetime | None = None,
        ingested_at: datetime,
        available_to_system_at: datetime,
        revised_at: datetime | None = None,
        supersedes_source_id: str | None = None,
        point_in_time_status: PointInTimeStatus,
        availability_basis: AvailabilityBasis,
    ) -> PointInTimeMetadata:
        identity = {
            "source_id": source_id,
            "source_document_id": source_document_id,
            "source_snapshot_id": source_snapshot_id,
            "period_end": period_end,
            "published_at": published_at,
            "effective_at": effective_at,
            "ingested_at": ingested_at,
            "available_to_system_at": available_to_system_at,
            "revised_at": revised_at,
            "supersedes_source_id": supersedes_source_id,
            "point_in_time_status": point_in_time_status,
            "availability_basis": availability_basis,
        }
        pit_id = f"pit:{sha256_bytes(canonical_json_bytes(identity))}"
        existing = self.repository.get_by_source(source_id)
        if existing is not None:
            if existing.pit_id != pit_id:
                raise ValueError(f"PIT source identity collision: {source_id}")
            self._register_artifact(existing)
            return existing
        metadata = PointInTimeMetadata(pit_id=pit_id, **identity)
        stored = self.repository.register(metadata)
        self._register_artifact(stored)
        return stored

    @staticmethod
    def assert_usable(
        metadata: PointInTimeMetadata,
        as_of: datetime,
        *,
        formal_historical: bool = True,
        allow_approximated: bool = False,
    ) -> PointInTimeMetadata:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        if metadata.available_to_system_at > as_of:
            raise ValueError(f"PIT source was not yet available: {metadata.source_id}")
        if metadata.published_at is not None and metadata.published_at > as_of:
            raise ValueError(f"PIT source was not yet published: {metadata.source_id}")
        if metadata.effective_at is not None and metadata.effective_at > as_of:
            raise ValueError(f"PIT source was not yet effective: {metadata.source_id}")
        if formal_historical:
            allowed = {
                PointInTimeStatus.CERTIFIED,
                PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            }
            if allow_approximated:
                allowed.add(PointInTimeStatus.APPROXIMATED)
            if metadata.point_in_time_status not in allowed:
                raise ValueError(
                    "PIT status is not allowed in formal historical evaluation: "
                    f"{metadata.point_in_time_status.value}"
                )
        return metadata

    def _register_artifact(self, metadata: PointInTimeMetadata) -> None:
        if self.object_store is None:
            return
        artifact = self.object_store.put_json(metadata.model_dump(mode="json"))
        inputs = [
            value
            for value in (metadata.source_snapshot_id, metadata.supersedes_source_id)
            if value is not None
        ]
        self.state.register_artifact(
            artifact_id=f"PointInTimeMetadata:{metadata.pit_id}",
            artifact_type="PointInTimeMetadata",
            schema_version=metadata.schema_version,
            object_hash=artifact.sha256,
            input_hashes=inputs,
        )
