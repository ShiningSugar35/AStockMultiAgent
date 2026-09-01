"""Durable report-manifest repository backed by SQLite and ObjectStore."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.reports import ReportManifest, ReportStatus


class ReportManifestRepository:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def get(self, report_key: str) -> ReportManifest | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT manifest_json FROM report_manifest WHERE report_key=?",
                (report_key,),
            ).fetchone()
        if row is None:
            return None
        return ReportManifest.model_validate_json(str(row["manifest_json"]))

    def list(self, status: ReportStatus | None = None) -> list[ReportManifest]:
        query = "SELECT manifest_json FROM report_manifest"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE publish_status=?"
            parameters = (status.value,)
        query += " ORDER BY created_at DESC,report_key DESC"
        with closing(self.state.connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [ReportManifest.model_validate_json(str(row["manifest_json"])) for row in rows]

    def save(self, manifest: ReportManifest) -> None:
        existing = self.get(manifest.report_key)
        if existing is not None and existing.request_hash != manifest.request_hash:
            raise ValueError(f"report manifest identity collision: {manifest.report_key}")
        converter_json = (
            canonical_json_bytes(manifest.converter.model_dump(mode="json")).decode("utf-8")
            if manifest.converter is not None
            else None
        )
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO report_manifest("
                "report_key,request_hash,input_hashes_json,template_version,renderer,"
                "renderer_version,converter_json,output_format,privacy_level,citation_level,"
                "citations_json,assets_json,output_file_name,output_relative_ref,output_sha256,"
                "output_byte_size,publish_status,degradation_reason,publish_attempts,"
                "destination_policy,recovered_existing,created_at,published_at,"
                "manifest_artifact_id,manifest_object_hash,manifest_json,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(report_key) DO UPDATE SET "
                "renderer=excluded.renderer,renderer_version=excluded.renderer_version,"
                "converter_json=excluded.converter_json,output_format=excluded.output_format,"
                "citations_json=excluded.citations_json,assets_json=excluded.assets_json,"
                "output_file_name=excluded.output_file_name,"
                "output_relative_ref=excluded.output_relative_ref,output_sha256=excluded.output_sha256,"
                "output_byte_size=excluded.output_byte_size,publish_status=excluded.publish_status,"
                "degradation_reason=excluded.degradation_reason,"
                "publish_attempts=excluded.publish_attempts,destination_policy=excluded.destination_policy,"
                "recovered_existing=excluded.recovered_existing,published_at=excluded.published_at,"
                "manifest_artifact_id=excluded.manifest_artifact_id,"
                "manifest_object_hash=excluded.manifest_object_hash,"
                "manifest_json=excluded.manifest_json,updated_at=excluded.updated_at",
                (
                    manifest.report_key,
                    manifest.request_hash,
                    json.dumps(manifest.input_artifact_hashes, separators=(",", ":")),
                    manifest.template_version,
                    manifest.renderer.value,
                    manifest.renderer_version,
                    converter_json,
                    manifest.output_format.value if manifest.output_format else None,
                    manifest.privacy_level.value,
                    manifest.citation_level.value,
                    canonical_json_bytes(manifest.citations.model_dump(mode="json")).decode(
                        "utf-8"
                    ),
                    canonical_json_bytes(manifest.assets.model_dump(mode="json")).decode("utf-8"),
                    manifest.output_file_name,
                    manifest.output_relative_ref,
                    manifest.output_sha256,
                    manifest.output_byte_size,
                    manifest.publish_status.value,
                    manifest.degradation_reason,
                    manifest.publish_attempts,
                    manifest.destination_policy.value,
                    int(manifest.recovered_existing),
                    manifest.created_at.isoformat(),
                    manifest.published_at.isoformat() if manifest.published_at else None,
                    manifest.manifest_artifact_id,
                    manifest.manifest_object_hash,
                    manifest.model_dump_json(),
                    now,
                ),
            )

    def finalize_manifest(self, manifest: ReportManifest) -> ReportManifest:
        core = manifest.model_copy(
            update={"manifest_artifact_id": None, "manifest_object_hash": None}
        )
        stored = self.objects.put_json(core.model_dump(mode="json"))
        artifact_id = f"report-manifest:{manifest.report_key}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ReportManifest",
            schema_version=manifest.schema_version,
            object_hash=stored.sha256,
            input_hashes=[manifest.request_hash, *manifest.input_artifact_hashes],
        )
        finalized = manifest.model_copy(
            update={
                "manifest_artifact_id": artifact_id,
                "manifest_object_hash": stored.sha256,
            }
        )
        self.save(finalized)
        return finalized


__all__ = ["ReportManifestRepository"]
