"""Transactional SQLite metadata for immutable financial-source releases."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.state import StateStore, utc_now_text
from astock.schemas import FinancialSourceReleaseManifest


class FinancialSourceReleaseRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def publish(self, manifest: FinancialSourceReleaseManifest, object_hash: str) -> bool:
        if manifest.release_id != content_hash(_release_identity(manifest)):
            raise ValueError("Financial source release identity mismatch")
        artifact_id = f"financial-source:{manifest.release_id}"
        inputs = [
            manifest.instrument_release_id,
            manifest.instrument_manifest_object_hash,
            manifest.instrument_content_hash,
            *manifest.raw_snapshot_ids,
            *_official_lineage_snapshot_ids(manifest),
            manifest.official_snapshot_id,
            manifest.source_content_hash,
            manifest.certified_content_hash,
        ]
        serialized = {
            "provider_ids_json": canonical_json_bytes(manifest.provider_ids).decode(),
            "raw_snapshot_ids_json": canonical_json_bytes(manifest.raw_snapshot_ids).decode(),
            "source_files_json": canonical_json_bytes(manifest.source_files).decode(),
            "certified_files_json": canonical_json_bytes(manifest.certified_files).decode(),
            "coverage_json": canonical_json_bytes(manifest.coverage).decode(),
        }
        expected = (
            manifest.company_id,
            manifest.instrument_id,
            manifest.market.value,
            manifest.instrument_type.value,
            manifest.instrument_release_id,
            manifest.instrument_manifest_artifact_id,
            manifest.instrument_manifest_object_hash,
            manifest.instrument_content_hash,
            manifest.instrument_available_to_system_at.isoformat(),
            manifest.period_end.isoformat(),
            manifest.period_type.value,
            manifest.previous_release_id,
            manifest.supersedes_release_id,
            artifact_id,
            object_hash,
            manifest.schema_version,
            serialized["provider_ids_json"],
            serialized["raw_snapshot_ids_json"],
            manifest.official_document_id,
            manifest.official_index_snapshot_id,
            manifest.official_snapshot_id,
            manifest.official_pit_id,
            serialized["source_files_json"],
            serialized["certified_files_json"],
            manifest.source_content_hash,
            manifest.certified_content_hash,
            manifest.available_to_system_at.isoformat(),
            manifest.status.value,
            manifest.coverage.source_observation_count,
            manifest.coverage.certified_fact_count,
            serialized["coverage_json"],
        )
        now = utc_now_text()
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT company_id,instrument_id,market,instrument_type,"
                "instrument_release_id,instrument_manifest_artifact_id,"
                "instrument_manifest_object_hash,instrument_content_hash,"
                "instrument_available_to_system_at,period_end,period_type,previous_release_id,"
                "supersedes_release_id,manifest_artifact_id,manifest_object_hash,"
                "manifest_schema_version,provider_ids_json,raw_snapshot_ids_json,"
                "official_document_id,official_index_snapshot_id,official_snapshot_id,"
                "official_pit_id,"
                "source_files_json,certified_files_json,source_content_hash,"
                "certified_content_hash,available_to_system_at,status,"
                "source_observation_count,certified_fact_count,coverage_json "
                "FROM financial_source_release WHERE release_id=?",
                (manifest.release_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError("Financial source release collision")
                return False
            for snapshot_id in [
                *manifest.raw_snapshot_ids,
                *_official_lineage_snapshot_ids(manifest),
                manifest.official_snapshot_id,
            ]:
                if connection.execute(
                    "SELECT 1 FROM source_snapshot_index WHERE snapshot_id=?", (snapshot_id,)
                ).fetchone() is None:
                    raise ValueError("Financial source snapshot is unknown")
            instrument = connection.execute(
                "SELECT r.manifest_artifact_id,r.manifest_object_hash,r.content_hash,"
                "r.available_to_system_at,a.object_hash FROM market_reference_release r "
                "JOIN artifact_registry a ON a.artifact_id=r.manifest_artifact_id "
                "WHERE r.release_id=? AND r.dataset_kind='INSTRUMENT_MASTER'",
                (manifest.instrument_release_id,),
            ).fetchone()
            if instrument is None or tuple(instrument) != (
                manifest.instrument_manifest_artifact_id,
                manifest.instrument_manifest_object_hash,
                manifest.instrument_content_hash,
                manifest.instrument_available_to_system_at.isoformat(),
                manifest.instrument_manifest_object_hash,
            ):
                raise ValueError("Financial source instrument binding is invalid")
            if connection.execute(
                "SELECT 1 FROM point_in_time_metadata WHERE pit_id=?",
                (manifest.official_pit_id,),
            ).fetchone() is None:
                raise ValueError("Financial source PIT is unknown")
            head = connection.execute(
                "SELECT h.release_id,r.available_to_system_at FROM financial_source_head h "
                "JOIN financial_source_release r ON r.company_id=h.company_id "
                "AND r.period_end=h.period_end AND r.period_type=h.period_type "
                "AND r.release_id=h.release_id WHERE h.company_id=? AND h.period_end=? "
                "AND h.period_type=?",
                (manifest.company_id, manifest.period_end.isoformat(), manifest.period_type.value),
            ).fetchone()
            current = str(head["release_id"]) if head is not None else None
            if current != manifest.previous_release_id:
                raise ValueError("Financial source previous head mismatch")
            if (
                head is not None
                and str(head["available_to_system_at"])
                > manifest.available_to_system_at.isoformat()
            ):
                raise ValueError("Financial source head availability cannot move backwards")
            artifact_inputs = json.dumps(inputs, separators=(",", ":"))
            connection.execute(
                "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
                "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    artifact_id,
                    "FinancialSourceReleaseManifest",
                    manifest.schema_version,
                    object_hash,
                    artifact_inputs,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO financial_source_release(release_id,company_id,instrument_id,"
                "market,instrument_type,instrument_release_id,"
                "instrument_manifest_artifact_id,instrument_manifest_object_hash,"
                "instrument_content_hash,instrument_available_to_system_at,period_end,"
                "period_type,previous_release_id,supersedes_release_id,manifest_artifact_id,"
                "manifest_object_hash,manifest_schema_version,provider_ids_json,"
                "raw_snapshot_ids_json,official_document_id,official_index_snapshot_id,"
                "official_snapshot_id,"
                "official_pit_id,source_files_json,certified_files_json,source_content_hash,"
                "certified_content_hash,available_to_system_at,status,"
                "source_observation_count,certified_fact_count,coverage_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (manifest.release_id, *expected, now),
            )
            connection.execute(
                "INSERT INTO financial_source_head(company_id,period_end,period_type,"
                "release_id,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(company_id,period_end,"
                "period_type) DO UPDATE SET release_id=excluded.release_id,"
                "updated_at=excluded.updated_at",
                (
                    manifest.company_id,
                    manifest.period_end.isoformat(),
                    manifest.period_type.value,
                    manifest.release_id,
                    now,
                ),
            )
            scope = (
                f"{manifest.company_id}:{manifest.period_end.isoformat()}:"
                f"{manifest.period_type.value}"
            )
            cursor = canonical_json_bytes(
                {
                    "release_id": manifest.release_id,
                    "certified_content_hash": manifest.certified_content_hash,
                }
            ).decode()
            connection.execute(
                "INSERT INTO checkpoint(checkpoint_id,job_id,scope_type,scope_key,cursor_json,"
                "status,object_hash,committed_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT("
                "scope_type,scope_key) DO UPDATE SET cursor_json=excluded.cursor_json,"
                "status=excluded.status,object_hash=excluded.object_hash,"
                "committed_at=excluded.committed_at",
                (
                    sha256_bytes(f"financial-source:{scope}".encode()),
                    None,
                    "financial-source",
                    scope,
                    cursor,
                    "SUCCEEDED",
                    object_hash,
                    now,
                ),
            )
        return True

    def get(
        self,
        company_id: str,
        period_end: str,
        period_type: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            if as_of is None:
                row = connection.execute(
                    _SELECT_RELEASE
                    + " JOIN financial_source_head h ON h.release_id=r.release_id "
                    "AND h.company_id=r.company_id AND h.period_end=r.period_end "
                    "AND h.period_type=r.period_type WHERE r.company_id=? AND r.period_end=? "
                    "AND r.period_type=?",
                    (company_id, period_end, period_type),
                ).fetchone()
            else:
                row = connection.execute(
                    _SELECT_RELEASE
                    + " WHERE r.company_id=? AND r.period_end=? AND r.period_type=? "
                    "AND r.available_to_system_at<=? "
                    "ORDER BY r.available_to_system_at DESC,r.created_at DESC,"
                    "r.release_id DESC LIMIT 1",
                    (
                        company_id,
                        period_end,
                        period_type,
                        as_of.astimezone(UTC).isoformat(),
                    ),
                ).fetchone()
        return dict(row) if row is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                _SELECT_RELEASE + " ORDER BY r.available_to_system_at,r.release_id"
            ).fetchall()
        return [dict(row) for row in rows]


_SELECT_RELEASE = (
    "SELECT r.*,a.type AS artifact_type,a.schema_version AS artifact_schema_version,"
    "a.object_hash AS artifact_object_hash,a.input_hashes_json FROM "
    "financial_source_release r JOIN artifact_registry a "
    "ON a.artifact_id=r.manifest_artifact_id"
)


def _release_identity(manifest: FinancialSourceReleaseManifest) -> dict[str, object]:
    identity: dict[str, object] = {
        "company_id": manifest.company_id,
        "instrument_id": manifest.instrument_id,
        "market": manifest.market,
        "instrument_type": manifest.instrument_type,
        "instrument_release_id": manifest.instrument_release_id,
        "instrument_manifest_artifact_id": manifest.instrument_manifest_artifact_id,
        "instrument_manifest_object_hash": manifest.instrument_manifest_object_hash,
        "instrument_content_hash": manifest.instrument_content_hash,
        "instrument_available_to_system_at": manifest.instrument_available_to_system_at,
        "period_end": manifest.period_end,
        "period_type": manifest.period_type,
        "previous_release_id": manifest.previous_release_id,
        "supersedes_release_id": manifest.supersedes_release_id,
        "provider_ids": manifest.provider_ids,
        "raw_snapshot_ids": manifest.raw_snapshot_ids,
        "official_document_id": manifest.official_document_id,
        "official_index_snapshot_id": manifest.official_index_snapshot_id,
        "official_snapshot_id": manifest.official_snapshot_id,
        "official_pit_id": manifest.official_pit_id,
        "source_content_hash": manifest.source_content_hash,
        "certified_content_hash": manifest.certified_content_hash,
        "available_to_system_at": manifest.available_to_system_at,
        "coverage": manifest.coverage,
    }
    if manifest.schema_version == "financial-source-release-v2":
        identity.update(
            {
                "official_lineage_kind": manifest.official_lineage_kind,
                "official_lineage_snapshot_ids": manifest.official_lineage_snapshot_ids,
                "official_exhaustive_proof_allowed": (
                    manifest.official_exhaustive_proof_allowed
                ),
            }
        )
    return identity


def _official_lineage_snapshot_ids(manifest: FinancialSourceReleaseManifest) -> list[str]:
    if manifest.schema_version == "financial-source-release-v1":
        return [manifest.official_index_snapshot_id]
    return manifest.official_lineage_snapshot_ids


__all__ = [
    "FinancialSourceReleaseRepository",
    "_official_lineage_snapshot_ids",
    "_release_identity",
]
