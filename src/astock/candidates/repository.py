"""Transactional metadata registry for deterministic candidate scans."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore, utc_now_text
from astock.schemas.candidates import (
    CandidateAuditReport,
    CandidateCheckpointStep,
    CandidateInputRelease,
    CandidateRecord,
    CandidateScanReport,
    CandidateSignalManifest,
    CandidateUniverseSnapshot,
)


class CandidateRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def register_input_release(
        self,
        release: CandidateInputRelease,
        object_hash: str,
    ) -> bool:
        artifact_id = f"candidate-input-release:{release.input_release_id}"
        inputs = [item.object_hash for item in release.artifacts]
        with self.state.transaction() as connection:
            for upstream in release.artifacts:
                if upstream.artifact_type == "SourceSnapshot":
                    registered = connection.execute(
                        "SELECT object_hash FROM source_snapshot_index WHERE snapshot_id=?",
                        (upstream.artifact_id,),
                    ).fetchone()
                    if (
                        registered is None
                        or str(registered["object_hash"]) != upstream.object_hash
                    ):
                        raise ValueError("Candidate input references an unknown snapshot")
                else:
                    registered = connection.execute(
                        "SELECT type,schema_version,object_hash FROM artifact_registry "
                        "WHERE artifact_id=?",
                        (upstream.artifact_id,),
                    ).fetchone()
                    if registered is None or tuple(registered) != (
                        upstream.artifact_type,
                        upstream.artifact_schema_version,
                        upstream.object_hash,
                    ):
                        raise ValueError("Candidate input artifact registration mismatch")
            existing = connection.execute(
                "SELECT manifest_artifact_id,manifest_object_hash,manifest_schema_version,"
                "source_mode,as_of,artifact_count,company_count,expected_company_count,"
                "universe_semantic_hash,coverage_proof_artifact_ids_json "
                "FROM candidate_input_release "
                "WHERE input_release_id=?",
                (release.input_release_id,),
            ).fetchone()
            expected = (
                artifact_id,
                object_hash,
                release.schema_version,
                release.source_mode.value,
                release.as_of.astimezone(UTC).isoformat(),
                len(release.artifacts),
                len(release.companies),
                release.expected_company_count,
                release.company_universe_semantic_hash,
                canonical_json_bytes(release.coverage_proof_artifact_ids).decode(),
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError("Candidate input release identity collision")
                return False
            self._register_artifact(
                connection,
                artifact_id,
                "CandidateInputRelease",
                release.schema_version,
                object_hash,
                inputs,
            )
            connection.execute(
                "INSERT INTO candidate_input_release(input_release_id,manifest_artifact_id,"
                "manifest_object_hash,manifest_schema_version,source_mode,as_of,artifact_count,"
                "company_count,expected_company_count,universe_semantic_hash,"
                "coverage_proof_artifact_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (release.input_release_id, *expected, utc_now_text()),
            )
        return True

    def begin_scan(
        self,
        *,
        scan_id: str,
        request_id: str,
        request_hash: str,
        input_release_id: str,
        rules_version: str,
        as_of: datetime,
        formal_historical: bool,
        live: bool,
    ) -> tuple[str, list[str], bool]:
        interrupted: list[str] = []
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT status,request_hash,input_release_id,rules_version,as_of,"
                "formal_historical,live "
                "FROM candidate_scan_run WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
            expected_identity = (
                request_hash,
                input_release_id,
                rules_version,
                as_of.astimezone(UTC).isoformat(),
                int(formal_historical),
                int(live),
            )
            if row is None:
                now = utc_now_text()
                connection.execute(
                    "INSERT INTO candidate_scan_run(scan_id,request_id,request_hash,"
                    "input_release_id,rules_version,as_of,formal_historical,live,status,"
                    "checkpoint_step,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'RUNNING','INPUT_REGISTERED',?,?)",
                    (scan_id, request_id, *expected_identity, now, now),
                )
            else:
                if tuple(row)[1:] != expected_identity:
                    raise ValueError("Candidate scan identity collision")
                if str(row["status"]) in {"SUCCEEDED", "NEEDS_INFO"}:
                    return "", [], True
                running = connection.execute(
                    "SELECT attempt_id FROM candidate_scan_attempt WHERE scan_id=? "
                    "AND status='RUNNING'",
                    (scan_id,),
                ).fetchall()
                interrupted = [str(item["attempt_id"]) for item in running]
                if interrupted:
                    placeholders = ",".join("?" for _ in interrupted)
                    connection.execute(
                        f"UPDATE candidate_scan_attempt SET status='INTERRUPTED_RECOVERED',"  # noqa: S608
                        f"ended_at=? WHERE attempt_id IN ({placeholders})",
                        (utc_now_text(), *interrupted),
                    )
                connection.execute(
                    "UPDATE candidate_scan_run SET status='RUNNING',updated_at=? WHERE scan_id=?",
                    (utc_now_text(), scan_id),
                )
            recovered = connection.execute(
                "SELECT attempt_id FROM candidate_scan_attempt WHERE scan_id=? "
                "AND status='INTERRUPTED_RECOVERED' ORDER BY ordinal",
                (scan_id,),
            ).fetchall()
            interrupted = [str(item["attempt_id"]) for item in recovered]
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM candidate_scan_attempt WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()[0]
            )
            attempt_id = uuid4().hex
            connection.execute(
                "INSERT INTO candidate_scan_attempt(attempt_id,scan_id,ordinal,status,started_at) "
                "VALUES(?,?,?,'RUNNING',?)",
                (attempt_id, scan_id, ordinal, utc_now_text()),
            )
        return attempt_id, interrupted, False

    def set_checkpoint(self, scan_id: str, step: CandidateCheckpointStep) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE candidate_scan_run SET checkpoint_step=?,updated_at=? WHERE scan_id=?",
                (step.value, utc_now_text(), scan_id),
            )

    def publish_signal_manifest(
        self,
        manifest: CandidateSignalManifest,
        object_hash: str,
    ) -> None:
        artifact_id = f"candidate-signals:{manifest.signal_manifest_id}"
        serialized = canonical_json_bytes(manifest.descriptor).decode()
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT manifest_object_hash,parquet_descriptor_json,signal_count "
                "FROM candidate_signal_manifest WHERE signal_manifest_id=?",
                (manifest.signal_manifest_id,),
            ).fetchone()
            expected = (object_hash, serialized, len(manifest.signal_ids))
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError("Candidate signal manifest collision")
                return
            self._register_artifact(
                connection,
                artifact_id,
                "CandidateSignalManifest",
                manifest.schema_version,
                object_hash,
                [
                    manifest.input_release_id,
                    manifest.signal_object_hash,
                    manifest.descriptor.sha256,
                ],
            )
            connection.execute(
                "INSERT INTO candidate_signal_manifest(signal_manifest_id,scan_id,"
                "manifest_artifact_id,manifest_object_hash,parquet_descriptor_json,"
                "signal_count,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    manifest.signal_manifest_id,
                    manifest.scan_id,
                    artifact_id,
                    object_hash,
                    serialized,
                    len(manifest.signal_ids),
                    utc_now_text(),
                ),
            )
            connection.execute(
                "UPDATE candidate_scan_run SET signal_manifest_id=?,checkpoint_step="
                "'SIGNALS_WRITTEN',updated_at=? WHERE scan_id=?",
                (manifest.signal_manifest_id, utc_now_text(), manifest.scan_id),
            )

    def publish_candidates(
        self,
        records: list[CandidateRecord],
        record_object_hashes: dict[str, str],
        snapshot: CandidateUniverseSnapshot,
        snapshot_object_hash: str,
        member_descriptor_json: str,
    ) -> None:
        snapshot_artifact_id = f"candidate-universe:{snapshot.snapshot_id}"
        with self.state.transaction() as connection:
            for record in records:
                object_hash = record_object_hashes[record.candidate_version_id]
                identity = connection.execute(
                    "SELECT company_id,instrument_id FROM candidate_identity WHERE candidate_id=?",
                    (record.candidate_id,),
                ).fetchone()
                if identity is None:
                    connection.execute(
                        "INSERT INTO candidate_identity(candidate_id,company_id,instrument_id,"
                        "created_at) VALUES(?,?,?,?)",
                        (
                            record.candidate_id,
                            record.company_id,
                            record.instrument_id,
                            utc_now_text(),
                        ),
                    )
                elif tuple(identity) != (record.company_id, record.instrument_id):
                    raise ValueError("Candidate identity collision")
                artifact_id = f"candidate-record:{record.candidate_version_id}"
                self._register_artifact(
                    connection,
                    artifact_id,
                    "CandidateRecord",
                    record.schema_version,
                    object_hash,
                    [record.input_release_id, *record.signal_ids, *record.evidence_ids],
                )
                existing = connection.execute(
                    "SELECT candidate_id,scan_id,previous_version_id,lifecycle_status,strength,"
                    "evaluation_status,miss_count,reactivation_count,record_artifact_id,"
                    "record_object_hash FROM candidate_record_version "
                    "WHERE candidate_version_id=?",
                    (record.candidate_version_id,),
                ).fetchone()
                values = (
                    record.candidate_id,
                    record.scan_id,
                    record.previous_version_id,
                    record.lifecycle_status.value,
                    record.strength.value,
                    record.evaluation_status.value,
                    record.miss_count,
                    record.reactivation_count,
                    artifact_id,
                    object_hash,
                )
                if existing is None:
                    connection.execute(
                        "INSERT INTO candidate_record_version(candidate_version_id,candidate_id,"
                        "scan_id,previous_version_id,lifecycle_status,strength,evaluation_status,"
                        "miss_count,reactivation_count,record_artifact_id,record_object_hash,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (record.candidate_version_id, *values, utc_now_text()),
                    )
                elif tuple(existing) != values:
                    raise ValueError("Candidate record version collision")
                connection.execute(
                    "INSERT INTO candidate_scan_member(scan_id,candidate_id,"
                    "candidate_version_id,company_id,lifecycle_status,strength) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(scan_id,candidate_id) DO UPDATE SET "
                    "candidate_version_id=excluded.candidate_version_id,"
                    "company_id=excluded.company_id,lifecycle_status=excluded.lifecycle_status,"
                    "strength=excluded.strength",
                    (
                        record.scan_id,
                        record.candidate_id,
                        record.candidate_version_id,
                        record.company_id,
                        record.lifecycle_status.value,
                        record.strength.value,
                    ),
                )
            self._register_artifact(
                connection,
                snapshot_artifact_id,
                "CandidateUniverseSnapshot",
                snapshot.schema_version,
                snapshot_object_hash,
                [snapshot.input_release_id, snapshot.semantic_hash],
            )
            existing_snapshot = connection.execute(
                "SELECT snapshot_object_hash,member_descriptor_json,semantic_hash,member_count "
                "FROM candidate_universe_snapshot WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            expected_snapshot = (
                snapshot_object_hash,
                member_descriptor_json,
                snapshot.semantic_hash,
                len(snapshot.members),
            )
            if existing_snapshot is None:
                connection.execute(
                    "INSERT INTO candidate_universe_snapshot(snapshot_id,scan_id,"
                    "snapshot_artifact_id,snapshot_object_hash,member_descriptor_json,"
                    "semantic_hash,member_count,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id,
                        snapshot.scan_id,
                        snapshot_artifact_id,
                        *expected_snapshot,
                        utc_now_text(),
                    ),
                )
            elif tuple(existing_snapshot) != expected_snapshot:
                raise ValueError("Candidate universe snapshot collision")
            connection.execute(
                "UPDATE candidate_scan_run SET universe_snapshot_id=?,checkpoint_step="
                "'REGISTRY_COMMITTED',updated_at=? WHERE scan_id=?",
                (snapshot.snapshot_id, utc_now_text(), snapshot.scan_id),
            )

    def publish_report(
        self,
        report: CandidateScanReport,
        object_hash: str,
        attempt_id: str,
    ) -> None:
        artifact_id = f"candidate-scan-report:{report.scan_id}"
        with self.state.transaction() as connection:
            self._register_artifact(
                connection,
                artifact_id,
                "CandidateScanReport",
                report.schema_version,
                object_hash,
                [report.input_release_id, report.request_hash],
            )
            connection.execute(
                "UPDATE candidate_scan_run SET status=?,checkpoint_step='COMPLETE',"
                "report_artifact_id=?,report_object_hash=?,updated_at=? WHERE scan_id=?",
                (
                    report.status.value,
                    artifact_id,
                    object_hash,
                    utc_now_text(),
                    report.scan_id,
                ),
            )
            connection.execute(
                "UPDATE candidate_scan_attempt SET status='SUCCEEDED',ended_at=? "
                "WHERE attempt_id=? AND status='RUNNING'",
                (utc_now_text(), attempt_id),
            )

    def fail_attempt(self, attempt_id: str, error_class: str) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE candidate_scan_attempt SET status='FAILED',ended_at=?,error_class=? "
                "WHERE attempt_id=? AND status='RUNNING'",
                (utc_now_text(), error_class, attempt_id),
            )

    def publish_audit(self, report: CandidateAuditReport, object_hash: str) -> None:
        artifact_id = f"candidate-audit:{report.audit_id}"
        with self.state.transaction() as connection:
            self._register_artifact(
                connection,
                artifact_id,
                "CandidateAuditReport",
                report.schema_version,
                object_hash,
                [report.scan_id, *report.checked_object_hashes],
            )
            connection.execute(
                "INSERT INTO candidate_audit(audit_id,scan_id,audit_artifact_id,"
                "audit_object_hash,status,created_at) VALUES(?,?,?,?,?,?)",
                (
                    report.audit_id,
                    report.scan_id,
                    artifact_id,
                    object_hash,
                    report.status.value,
                    utc_now_text(),
                ),
            )

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidate_scan_run WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_input_release(self, input_release_id: str) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidate_input_release WHERE input_release_id=?",
                (input_release_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_signal_manifest(self, scan_id: str) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidate_signal_manifest WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_universe(self, scan_id: str) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidate_universe_snapshot WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_scan_records(self, scan_id: str) -> list[dict[str, Any]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT v.*,i.company_id,i.instrument_id,s.as_of AS scan_as_of "
                "FROM candidate_record_version v "
                "JOIN candidate_identity i ON i.candidate_id=v.candidate_id "
                "JOIN candidate_scan_run s ON s.scan_id=v.scan_id "
                "WHERE v.scan_id=? ORDER BY i.company_id",
                (scan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_scan_members(self, scan_id: str) -> list[dict[str, Any]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM candidate_scan_member WHERE scan_id=? "
                "ORDER BY company_id,candidate_id",
                (scan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_records(
        self,
        *,
        before_as_of: datetime,
        exclude_scan_id: str,
    ) -> list[dict[str, Any]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM (SELECT v.*,i.company_id,i.instrument_id,s.as_of AS scan_as_of,"
                "ROW_NUMBER() OVER(PARTITION BY v.candidate_id ORDER BY s.as_of DESC,"
                "v.rowid DESC) AS rank FROM candidate_record_version v JOIN candidate_identity i "
                "ON i.candidate_id=v.candidate_id JOIN candidate_scan_run s "
                "ON s.scan_id=v.scan_id WHERE s.as_of<? AND s.scan_id<>?) "
                "WHERE rank=1 ORDER BY company_id,instrument_id",
                (before_as_of.astimezone(UTC).isoformat(), exclude_scan_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def status_by_company(self, company_id: str) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT v.*,i.company_id,i.instrument_id,s.as_of AS scan_as_of "
                "FROM candidate_identity i "
                "JOIN candidate_record_version v ON v.candidate_id=i.candidate_id "
                "JOIN candidate_scan_run s ON s.scan_id=v.scan_id "
                "WHERE i.company_id=? ORDER BY s.as_of DESC,v.rowid DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_candidate_version(self, candidate_version_id: str) -> dict[str, Any] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT v.*,i.company_id,i.instrument_id,s.as_of AS scan_as_of "
                "FROM candidate_record_version v JOIN candidate_identity i "
                "ON i.candidate_id=v.candidate_id JOIN candidate_scan_run s "
                "ON s.scan_id=v.scan_id WHERE v.candidate_version_id=?",
                (candidate_version_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def artifact_matches(self, artifact_id: str, object_hash: str) -> bool:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return row is not None and str(row["object_hash"]) == object_hash

    @staticmethod
    def _register_artifact(
        connection: Any,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: list[str],
    ) -> None:
        serialized = json.dumps(input_hashes, separators=(",", ":"))
        existing = connection.execute(
            "SELECT type,schema_version,object_hash,input_hashes_json FROM artifact_registry "
            "WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        expected = (artifact_type, schema_version, object_hash, serialized)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"Artifact identity collision: {artifact_id}")
            return
        connection.execute(
            "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
            "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
            (artifact_id, *expected, utc_now_text()),
        )


__all__ = ["CandidateRepository"]
