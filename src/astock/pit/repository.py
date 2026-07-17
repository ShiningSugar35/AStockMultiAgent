"""Durable point-in-time metadata and revision-chain repository."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas import PointInTimeMetadata


class PointInTimeRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get(self, pit_id: str) -> PointInTimeMetadata | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT pit_json FROM point_in_time_metadata WHERE pit_id=?", (pit_id,)
            ).fetchone()
        return PointInTimeMetadata.model_validate_json(row["pit_json"]) if row else None

    def get_by_source(self, source_id: str) -> PointInTimeMetadata | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT pit_json FROM point_in_time_metadata WHERE source_id=?", (source_id,)
            ).fetchone()
        return PointInTimeMetadata.model_validate_json(row["pit_json"]) if row else None

    def for_snapshot(self, snapshot_id: str) -> list[PointInTimeMetadata]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT pit_json FROM point_in_time_metadata WHERE source_snapshot_id=? "
                "ORDER BY available_to_system_at,pit_id",
                (snapshot_id,),
            ).fetchall()
        return [PointInTimeMetadata.model_validate_json(row["pit_json"]) for row in rows]

    def register(self, metadata: PointInTimeMetadata) -> PointInTimeMetadata:
        serialized = canonical_json_bytes(metadata.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            by_source = connection.execute(
                "SELECT pit_id,pit_json FROM point_in_time_metadata WHERE source_id=?",
                (metadata.source_id,),
            ).fetchone()
            if by_source is not None:
                if by_source["pit_id"] != metadata.pit_id:
                    raise ValueError(f"PIT source identity collision: {metadata.source_id}")
                return PointInTimeMetadata.model_validate_json(by_source["pit_json"])
            by_id = connection.execute(
                "SELECT source_id FROM point_in_time_metadata WHERE pit_id=?",
                (metadata.pit_id,),
            ).fetchone()
            if by_id is not None:
                raise ValueError(f"PIT id collision: {metadata.pit_id}")
            if metadata.supersedes_source_id is not None:
                predecessor = connection.execute(
                    "SELECT 1 FROM point_in_time_metadata WHERE source_id=?",
                    (metadata.supersedes_source_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError(
                        f"Unknown superseded PIT source: {metadata.supersedes_source_id}"
                    )
            connection.execute(
                "INSERT INTO point_in_time_metadata(pit_id,source_id,source_document_id,"
                "source_snapshot_id,period_end,published_at,effective_at,ingested_at,"
                "available_to_system_at,revised_at,supersedes_source_id,point_in_time_status,"
                "availability_basis,pit_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    metadata.pit_id,
                    metadata.source_id,
                    metadata.source_document_id,
                    metadata.source_snapshot_id,
                    metadata.period_end.isoformat() if metadata.period_end else None,
                    metadata.published_at.isoformat() if metadata.published_at else None,
                    metadata.effective_at.isoformat() if metadata.effective_at else None,
                    metadata.ingested_at.isoformat(),
                    metadata.available_to_system_at.isoformat(),
                    metadata.revised_at.isoformat() if metadata.revised_at else None,
                    metadata.supersedes_source_id,
                    metadata.point_in_time_status.value,
                    metadata.availability_basis.value,
                    serialized,
                    metadata.created_at.isoformat(),
                ),
            )
        return metadata

    def revision_chain(self, source_id: str) -> list[PointInTimeMetadata]:
        current = self.get_by_source(source_id)
        if current is None:
            raise ValueError(f"Unknown PIT source: {source_id}")
        chain: list[PointInTimeMetadata] = []
        seen: set[str] = set()
        while current is not None:
            if current.source_id in seen:  # defensive against manually corrupted databases
                raise ValueError(f"PIT revision cycle detected at: {current.source_id}")
            seen.add(current.source_id)
            chain.append(current)
            current = (
                self.get_by_source(current.supersedes_source_id)
                if current.supersedes_source_id
                else None
            )
        chain.reverse()
        return chain
