"""Coverage and storage-integrity audits for allowlisted knowledge sources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pyarrow.parquet as pq

from astock.books import BookRepository, PrivateDocxRepository
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentBlockRepository
from astock.knowledge.gaps import count_open_gap_boundaries
from astock.knowledge.repository import KnowledgeRepository
from astock.schemas import (
    BookProcessingStatus,
    CollectionCheckpoint,
    CoverageStatus,
    KnowledgeAuditStatus,
    KnowledgeCoverageAuditReport,
    KnowledgeLocalCoverageReport,
    KnowledgeLocalSeedSource,
    KnowledgeScopeCoverageAudit,
    KnowledgeSourceCoverageAudit,
    KnowledgeSourceDefinition,
    KnowledgeSourceRegistry,
    ZhihuContentType,
)

_LOCAL_COVERAGE_BASIS = "USER_CONFIRMED_COMPLETE_EXPORT"
_STALE_RUNNING_AFTER = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class _ParquetIndex:
    body_hash_by_version: dict[str, str]
    read_error_count: int


class KnowledgeCoverageAuditService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        parquet_root: Path,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.parquet_root = parquet_root.resolve()
        self.repository = KnowledgeRepository(state)

    def audit_local_source(
        self,
        source: KnowledgeSourceDefinition,
        *,
        seed_source_id: str | None = None,
    ) -> KnowledgeLocalCoverageReport:
        if source.online_collection_required or not source.local_seed_sources:
            raise ValueError("local coverage requires an allowlisted offline seed")
        seed = self._select_seed(source, seed_source_id)
        audited_at = datetime.now(UTC)
        findings: list[str] = []
        required_hashes: set[str] = set()
        expected_block_count = seed.expected_block_count
        if expected_block_count is None:
            expected_block_count = 0
            findings.append("EXPECTED_BLOCK_COUNT_NOT_CONFIGURED")

        manifest = BookRepository(self.state).get_manifest_version(
            seed.source_id,
            seed.file_version,
        )
        registered_file_sha256: str | None = None
        manifest_id: str | None = None
        snapshot_id: str | None = None
        parse_report_id: str | None = None
        registered_block_count = 0
        verified_text_count = 0
        verified_metadata_count = 0
        file_hash_matches = False
        raw_object_verified = False
        snapshot_matches = False
        parse_report_verified = False
        block_id_set_matches = False
        block_set_object_verified = False

        if manifest is None:
            findings.append("MANIFEST_NOT_FOUND")
        else:
            registered_file_sha256 = manifest.file_sha256
            manifest_id = manifest.manifest_id
            snapshot_id = manifest.snapshot_id
            required_hashes.add(manifest.raw_object_sha256)
            file_hash_matches = (
                manifest.source_id == seed.source_id
                and manifest.author_source_id == source.source_id
                and manifest.file_version == seed.file_version
                and manifest.file_sha256 == seed.expected_sha256
                and manifest.raw_object_sha256 == seed.expected_sha256
                and manifest.rights_status == seed.rights_status
            )
            if not file_hash_matches:
                findings.append("MANIFEST_IDENTITY_OR_HASH_MISMATCH")
            raw_object_verified = self.object_store.verify(manifest.raw_object_sha256)
            if not raw_object_verified:
                findings.append("RAW_SOURCE_OBJECT_MISSING_OR_INVALID")
            snapshot = self.state.get_snapshot(manifest.snapshot_id)
            snapshot_matches = bool(
                snapshot
                and snapshot.source_id == seed.source_id
                and snapshot.object_sha256 == manifest.raw_object_sha256
                and snapshot.rights_status == seed.rights_status
            )
            if not snapshot_matches:
                findings.append("SOURCE_SNAPSHOT_MISSING_OR_MISMATCHED")

            parse_report = PrivateDocxRepository(
                self.state
            ).latest_parse_report_for_manifest(manifest.manifest_id)
            if parse_report is None:
                findings.append("DOCX_PARSE_REPORT_NOT_FOUND")
            else:
                parse_report_id = parse_report.docx_parse_report_id
                if parse_report.report_object_sha256:
                    required_hashes.add(parse_report.report_object_sha256)
                required_hashes.add(parse_report.block_set_sha256)
                parse_report_verified = bool(
                    parse_report.report_object_sha256
                    and self.object_store.verify(parse_report.report_object_sha256)
                    and parse_report.manifest_id == manifest.manifest_id
                    and parse_report.document_id == manifest.document_id
                    and parse_report.snapshot_id == manifest.snapshot_id
                    and parse_report.file_sha256 == manifest.file_sha256
                    and parse_report.processing_status is BookProcessingStatus.COMPLETE
                    and parse_report.coverage_status is CoverageStatus.COMPLETE
                    and not parse_report.gaps
                )
                if not parse_report_verified:
                    findings.append("DOCX_PARSE_REPORT_INCOMPLETE_OR_INVALID")
                blocks = DocumentBlockRepository(self.state).blocks_for(
                    parse_report.snapshot_id,
                    parse_report.parser_version,
                )
                registered_block_count = len(blocks)
                block_ids = [block.block_id for block in blocks]
                block_id_set_matches = (
                    block_ids == parse_report.block_ids
                    and parse_report.processed_block_count == len(blocks)
                    and parse_report.source_paragraph_count == len(blocks)
                )
                if not block_id_set_matches:
                    findings.append("DOCX_BLOCK_ID_SET_MISMATCH")
                block_set_hash = sha256_bytes(canonical_json_bytes(block_ids))
                block_set_object_verified = (
                    block_set_hash == parse_report.block_set_sha256
                    and self.object_store.verify(parse_report.block_set_sha256)
                )
                if not block_set_object_verified:
                    findings.append("DOCX_BLOCK_SET_OBJECT_MISSING_OR_INVALID")
                for block in blocks:
                    required_hashes.add(block.text_object_sha256)
                    required_hashes.add(block.metadata_object_sha256)
                    if self.object_store.verify(block.text_object_sha256):
                        verified_text_count += 1
                    if self.object_store.verify(block.metadata_object_sha256):
                        verified_metadata_count += 1
                if verified_text_count != registered_block_count:
                    findings.append("DOCX_BLOCK_TEXT_OBJECTS_MISSING_OR_INVALID")
                if verified_metadata_count != registered_block_count:
                    findings.append("DOCX_BLOCK_METADATA_OBJECTS_MISSING_OR_INVALID")

        if registered_block_count != expected_block_count:
            findings.append("EXPECTED_BLOCK_COUNT_MISMATCH")
        missing_hashes = {
            object_hash
            for object_hash in required_hashes
            if not self.object_store.verify(object_hash)
        }
        findings = sorted(set(findings))
        complete = not findings and not missing_hashes
        status = (
            KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT
            if complete
            else KnowledgeAuditStatus.PARTIAL
        )
        identity = {
            "author_source_id": source.source_id,
            "seed_source_id": seed.source_id,
            "manifest_id": manifest_id,
            "parse_report_id": parse_report_id,
            "expected_file_sha256": seed.expected_sha256,
            "registered_file_sha256": registered_file_sha256,
            "expected_block_count": expected_block_count,
            "registered_block_count": registered_block_count,
            "findings": findings,
            "audited_at": audited_at,
        }
        report = KnowledgeLocalCoverageReport(
            report_id=f"knowledge-local-coverage:{content_hash(identity)}",
            author_source_id=source.source_id,
            seed_source_id=seed.source_id,
            coverage_basis=_LOCAL_COVERAGE_BASIS,
            expected_file_sha256=seed.expected_sha256,
            registered_file_sha256=registered_file_sha256,
            manifest_id=manifest_id,
            source_snapshot_id=snapshot_id,
            parse_report_id=parse_report_id,
            expected_block_count=expected_block_count,
            registered_block_count=registered_block_count,
            verified_text_object_count=verified_text_count,
            verified_metadata_object_count=verified_metadata_count,
            missing_object_count=len(missing_hashes),
            file_hash_matches=file_hash_matches,
            raw_object_verified=raw_object_verified,
            source_snapshot_matches=snapshot_matches,
            parse_report_verified=parse_report_verified,
            block_id_set_matches=block_id_set_matches,
            block_set_object_verified=block_set_object_verified,
            status=status,
            findings=findings,
            audited_at=audited_at,
            created_at=audited_at,
        )
        self._persist_local_report(report)
        return report

    def audit_registry(
        self,
        registry: KnowledgeSourceRegistry,
    ) -> KnowledgeCoverageAuditReport:
        audited_at = datetime.now(UTC)
        source_reports = [
            self._audit_source(source) for source in registry.sources if source.enabled
        ]
        total_open_gaps = sum(item.open_gap_count for item in source_reports)
        total_pending = sum(item.pending_import_count for item in source_reports)
        missing_objects = sum(
            scope.missing_content_body_count + scope.missing_comment_body_count
            for source in source_reports
            for scope in source.scope_reports
        )
        parquet_mismatches = sum(
            scope.missing_content_parquet_count
            + scope.orphan_content_parquet_count
            + scope.content_parquet_hash_mismatch_count
            + scope.content_parquet_read_error_count
            + scope.missing_comment_parquet_count
            + scope.orphan_comment_parquet_count
            + scope.comment_parquet_hash_mismatch_count
            + scope.comment_parquet_read_error_count
            for source in source_reports
            for scope in source.scope_reports
        )
        local_report_ids = {
            report_id
            for source in source_reports
            for report_id in source.local_report_ids
        }
        if local_report_ids:
            with self.state.connect() as connection:
                placeholders = ",".join("?" for _ in local_report_ids)
                rows = connection.execute(
                    "SELECT report_json FROM knowledge_local_coverage_report "
                    f"WHERE report_id IN ({placeholders})",
                    tuple(sorted(local_report_ids)),
                ).fetchall()
            missing_objects += sum(
                KnowledgeLocalCoverageReport.model_validate_json(row["report_json"]).missing_object_count
                for row in rows
            )
        stale_before = (audited_at - _STALE_RUNNING_AFTER).isoformat()
        with self.state.connect() as connection:
            stale_running_jobs = int(
                connection.execute(
                    "SELECT COUNT(*) FROM job WHERE type LIKE 'zhihu-%' "
                    "AND status='RUNNING' AND updated_at<?",
                    (stale_before,),
                ).fetchone()[0]
            )
        findings: list[str] = []
        if total_open_gaps:
            findings.append("OPEN_COLLECTION_GAPS")
        if total_pending:
            findings.append("PENDING_IMPORTED_RESPONSES")
        if stale_running_jobs:
            findings.append("STALE_RUNNING_ZHIHU_JOBS")
        if missing_objects:
            findings.append("MISSING_OR_INVALID_OBJECTS")
        if parquet_mismatches:
            findings.append("PARQUET_SQLITE_MISMATCH")
        complete_statuses = {
            KnowledgeAuditStatus.PASS,
            KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT,
        }
        if any(source.status not in complete_statuses for source in source_reports):
            findings.append("SOURCE_COVERAGE_INCOMPLETE")
        findings = sorted(set(findings))
        status = KnowledgeAuditStatus.PASS if not findings else KnowledgeAuditStatus.PARTIAL
        identity = {
            "source_reports": [content_hash(item) for item in source_reports],
            "total_open_gap_count": total_open_gaps,
            "total_pending_import_count": total_pending,
            "stale_running_job_count": stale_running_jobs,
            "missing_object_count": missing_objects,
            "parquet_mismatch_count": parquet_mismatches,
            "findings": findings,
            "audited_at": audited_at,
        }
        report = KnowledgeCoverageAuditReport(
            report_id=f"knowledge-coverage-audit:{content_hash(identity)}",
            source_reports=source_reports,
            total_open_gap_count=total_open_gaps,
            total_pending_import_count=total_pending,
            stale_running_job_count=stale_running_jobs,
            missing_object_count=missing_objects,
            parquet_mismatch_count=parquet_mismatches,
            status=status,
            findings=findings,
            audited_at=audited_at,
            created_at=audited_at,
        )
        self._persist_audit_report(report)
        return report

    def _audit_source(
        self,
        source: KnowledgeSourceDefinition,
    ) -> KnowledgeSourceCoverageAudit:
        if not source.online_collection_required:
            local_reports = [
                self.audit_local_source(source, seed_source_id=seed.source_id)
                for seed in source.local_seed_sources
            ]
            complete = bool(local_reports) and all(
                report.status is KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT
                for report in local_reports
            )
            return KnowledgeSourceCoverageAudit(
                source_id=source.source_id,
                online_collection_required=False,
                identity_status=source.identity_status,
                identity_registered=True,
                local_report_ids=[report.report_id for report in local_reports],
                pending_import_count=self.repository.pending_import_count(source.source_id),
                open_gap_count=self._open_gap_count(source.source_id),
                status=(
                    KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT
                    if complete
                    else KnowledgeAuditStatus.PARTIAL
                ),
                findings=([] if complete else ["LOCAL_EXPORT_INTEGRITY_INCOMPLETE"]),
            )

        identity_registered = self._identity_registered(source.source_id)
        scope_reports = [
            self._audit_online_scope(source, ZhihuContentType(content_type))
            for content_type in source.collection_scope.content_types
        ]
        pending_imports = self.repository.pending_import_count(source.source_id)
        open_gaps = self._open_gap_count(source.source_id)
        findings: list[str] = []
        if not identity_registered:
            findings.append("CONFIRMED_IDENTITY_NOT_REGISTERED")
        if pending_imports:
            findings.append("PENDING_IMPORTED_RESPONSES")
        if open_gaps:
            findings.append("OPEN_COLLECTION_GAPS")
        if any(
            scope.status in {KnowledgeAuditStatus.PARTIAL, KnowledgeAuditStatus.NOT_COLLECTED}
            for scope in scope_reports
        ):
            findings.append("SCOPE_COVERAGE_INCOMPLETE")
        findings = sorted(set(findings))
        if findings:
            status = KnowledgeAuditStatus.PARTIAL
        elif any(
            scope.status is KnowledgeAuditStatus.ACCESS_RESTRICTED for scope in scope_reports
        ):
            status = KnowledgeAuditStatus.ACCESS_RESTRICTED
        else:
            status = KnowledgeAuditStatus.PASS
        return KnowledgeSourceCoverageAudit(
            source_id=source.source_id,
            online_collection_required=True,
            identity_status=source.identity_status,
            identity_registered=identity_registered,
            scope_reports=scope_reports,
            pending_import_count=pending_imports,
            open_gap_count=open_gaps,
            status=status,
            findings=findings,
        )

    def _audit_online_scope(
        self,
        source: KnowledgeSourceDefinition,
        content_type: ZhihuContentType,
    ) -> KnowledgeScopeCoverageAudit:
        latest = self.repository.latest_coverage_report(source.source_id, content_type)
        with self.state.connect() as connection:
            content_rows = connection.execute(
                "SELECT version_id,body_object_hash,content_id FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=?",
                (source.source_id, content_type.value),
            ).fetchall()
            comment_rows = connection.execute(
                "SELECT version_id,body_object_hash FROM zhihu_comment_version "
                "WHERE source_id=? AND content_type=?",
                (source.source_id, content_type.value),
            ).fetchall()
            participation_rows = connection.execute(
                "SELECT DISTINCT content_id,root_comment_id "
                "FROM zhihu_author_participation_chain "
                "WHERE source_id=? AND content_type=?",
                (source.source_id, content_type.value),
            ).fetchall()
        sqlite_content = {
            str(row["version_id"]): str(row["body_object_hash"]) for row in content_rows
        }
        sqlite_comment = {
            str(row["version_id"]): str(row["body_object_hash"]) for row in comment_rows
        }
        content_parquet = self._read_parquet_index(
            "knowledge_content", source.source_id, content_type
        )
        comment_parquet = self._read_parquet_index(
            "knowledge_comments", source.source_id, content_type
        )
        content_ids = set(sqlite_content)
        content_parquet_ids = set(content_parquet.body_hash_by_version)
        comment_ids = set(sqlite_comment)
        comment_parquet_ids = set(comment_parquet.body_hash_by_version)
        missing_content_bodies = sum(
            not self.object_store.verify(body_hash) for body_hash in sqlite_content.values()
        )
        missing_comment_bodies = sum(
            not self.object_store.verify(body_hash) for body_hash in sqlite_comment.values()
        )
        content_hash_mismatches = sum(
            sqlite_content[version_id]
            != content_parquet.body_hash_by_version[version_id]
            for version_id in content_ids & content_parquet_ids
        )
        comment_hash_mismatches = sum(
            sqlite_comment[version_id]
            != comment_parquet.body_hash_by_version[version_id]
            for version_id in comment_ids & comment_parquet_ids
        )
        root_required_ids = (
            {str(row["content_id"]) for row in content_rows}
            if source.collection_scope.include_required_comment_pages
            else set()
        )
        child_required = {
            (str(row["content_id"]), str(row["root_comment_id"]))
            for row in participation_rows
        }
        root_terminal, child_terminal, checkpoint_errors = self._comment_terminals(
            source.source_id,
            content_type,
        )
        root_terminal_count = len(root_required_ids & root_terminal)
        child_terminal_count = len(child_required & child_terminal)
        open_gaps = self._scope_open_gap_count(source.source_id, content_type)
        missing_content_parquet = len(content_ids - content_parquet_ids)
        orphan_content_parquet = len(content_parquet_ids - content_ids)
        missing_comment_parquet = len(comment_ids - comment_parquet_ids)
        orphan_comment_parquet = len(comment_parquet_ids - comment_ids)
        findings: list[str] = []
        counts = {
            "MISSING_CONTENT_BODY_OBJECTS": missing_content_bodies,
            "MISSING_COMMENT_BODY_OBJECTS": missing_comment_bodies,
            "MISSING_CONTENT_PARQUET_ROWS": missing_content_parquet,
            "ORPHAN_CONTENT_PARQUET_ROWS": orphan_content_parquet,
            "CONTENT_PARQUET_HASH_MISMATCH": content_hash_mismatches,
            "CONTENT_PARQUET_READ_ERRORS": content_parquet.read_error_count,
            "MISSING_COMMENT_PARQUET_ROWS": missing_comment_parquet,
            "ORPHAN_COMMENT_PARQUET_ROWS": orphan_comment_parquet,
            "COMMENT_PARQUET_HASH_MISMATCH": comment_hash_mismatches,
            "COMMENT_PARQUET_READ_ERRORS": comment_parquet.read_error_count,
            "COMMENT_CHECKPOINT_DECODE_ERRORS": checkpoint_errors,
            "ROOT_COMMENT_SCOPES_INCOMPLETE": len(root_required_ids) - root_terminal_count,
            "CHILD_REPLY_SCOPES_INCOMPLETE": len(child_required) - child_terminal_count,
            "OPEN_COLLECTION_GAPS": open_gaps,
        }
        findings.extend(f"{code}:{count}" for code, count in counts.items() if count)
        integrity_failure = any(
            counts[key]
            for key in (
                "MISSING_CONTENT_BODY_OBJECTS",
                "MISSING_COMMENT_BODY_OBJECTS",
                "MISSING_CONTENT_PARQUET_ROWS",
                "ORPHAN_CONTENT_PARQUET_ROWS",
                "CONTENT_PARQUET_HASH_MISMATCH",
                "CONTENT_PARQUET_READ_ERRORS",
                "MISSING_COMMENT_PARQUET_ROWS",
                "ORPHAN_COMMENT_PARQUET_ROWS",
                "COMMENT_PARQUET_HASH_MISMATCH",
                "COMMENT_PARQUET_READ_ERRORS",
                "COMMENT_CHECKPOINT_DECODE_ERRORS",
            )
        )
        comment_incomplete = (
            root_terminal_count != len(root_required_ids)
            or child_terminal_count != len(child_required)
        )
        if latest is None:
            status = KnowledgeAuditStatus.NOT_COLLECTED
            findings.append("LISTING_COVERAGE_REPORT_NOT_FOUND")
        elif integrity_failure or comment_incomplete:
            status = KnowledgeAuditStatus.PARTIAL
        elif latest.coverage_status is CoverageStatus.ACCESS_RESTRICTED:
            status = KnowledgeAuditStatus.ACCESS_RESTRICTED
        elif latest.coverage_status is not CoverageStatus.COMPLETE or open_gaps:
            status = KnowledgeAuditStatus.PARTIAL
        else:
            status = KnowledgeAuditStatus.PASS
        return KnowledgeScopeCoverageAudit(
            content_type=content_type.value,
            listing_report_id=latest.report_id if latest else None,
            listing_terminal_condition=latest.terminal_condition if latest else None,
            listing_coverage_status=latest.coverage_status if latest else None,
            sqlite_content_version_count=len(content_ids),
            parquet_content_version_count=len(content_parquet_ids),
            verified_content_body_count=len(content_ids) - missing_content_bodies,
            missing_content_body_count=missing_content_bodies,
            missing_content_parquet_count=missing_content_parquet,
            orphan_content_parquet_count=orphan_content_parquet,
            content_parquet_hash_mismatch_count=content_hash_mismatches,
            content_parquet_read_error_count=content_parquet.read_error_count,
            sqlite_comment_version_count=len(comment_ids),
            parquet_comment_version_count=len(comment_parquet_ids),
            verified_comment_body_count=len(comment_ids) - missing_comment_bodies,
            missing_comment_body_count=missing_comment_bodies,
            missing_comment_parquet_count=missing_comment_parquet,
            orphan_comment_parquet_count=orphan_comment_parquet,
            comment_parquet_hash_mismatch_count=comment_hash_mismatches,
            comment_parquet_read_error_count=comment_parquet.read_error_count,
            root_comment_required_count=len(root_required_ids),
            root_comment_terminal_count=root_terminal_count,
            child_reply_required_count=len(child_required),
            child_reply_terminal_count=child_terminal_count,
            open_gap_count=open_gaps,
            status=status,
            findings=sorted(set(findings)),
        )

    def _comment_terminals(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> tuple[set[str], set[tuple[str, str]], int]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT cursor_json FROM checkpoint WHERE scope_type='author-collection'"
            ).fetchall()
        root_terminal: set[str] = set()
        child_terminal: set[tuple[str, str]] = set()
        decode_errors = 0
        for row in rows:
            try:
                checkpoint = CollectionCheckpoint.model_validate(json.loads(row["cursor_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                decode_errors += 1
                continue
            if (
                checkpoint.author != source_id
                or checkpoint.content_type != content_type.value
                or checkpoint.content_id is None
                or checkpoint.terminal_condition is None
            ):
                continue
            if checkpoint.comment_parent_id is None:
                root_terminal.add(checkpoint.content_id)
            else:
                child_terminal.add(
                    (checkpoint.content_id, checkpoint.comment_parent_id)
                )
        return root_terminal, child_terminal, decode_errors

    def _read_parquet_index(
        self,
        dataset_name: str,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> _ParquetIndex:
        source_root = (
            self.parquet_root
            / dataset_name
            / f"author={quote(source_id, safe='-_.')}"
            / f"content_type={quote(content_type.value, safe='-_.')}"
        )
        body_hash_by_version: dict[str, str] = {}
        read_errors = 0
        for path in sorted(source_root.rglob("*.parquet")) if source_root.exists() else []:
            try:
                rows = pq.ParquetFile(path).read(
                    columns=["version_id", "body_object_sha256"]
                ).to_pylist()
                for row in rows:
                    version_id = str(row["version_id"])
                    body_hash = str(row["body_object_sha256"])
                    previous = body_hash_by_version.get(version_id)
                    if previous is not None and previous != body_hash:
                        read_errors += 1
                    body_hash_by_version[version_id] = body_hash
            except (OSError, ValueError, KeyError):
                read_errors += 1
        return _ParquetIndex(body_hash_by_version, read_errors)

    def _identity_registered(self, source_id: str) -> bool:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM knowledge_source_identity WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return row is not None

    def _open_gap_count(self, source_id: str) -> int:
        return count_open_gap_boundaries(self.state, source_id)

    def _scope_open_gap_count(
        self,
        source_id: str,
        content_type: ZhihuContentType,
    ) -> int:
        comment_prefix = f"comments:{content_type.value}:%"
        return count_open_gap_boundaries(
            self.state,
            source_id,
            content_type=content_type.value,
            comment_scope_prefix=comment_prefix,
        )

    @staticmethod
    def _select_seed(
        source: KnowledgeSourceDefinition,
        seed_source_id: str | None,
    ) -> KnowledgeLocalSeedSource:
        if seed_source_id is None:
            if len(source.local_seed_sources) != 1:
                raise ValueError("seed_source_id is required when a source has multiple seeds")
            return source.local_seed_sources[0]
        seed = next(
            (item for item in source.local_seed_sources if item.source_id == seed_source_id),
            None,
        )
        if seed is None:
            raise ValueError(f"unknown local seed: {seed_source_id}")
        return seed

    def _persist_local_report(self, report: KnowledgeLocalCoverageReport) -> None:
        object_ref = self.object_store.put_json(report.model_dump(mode="json"))
        self.repository.register_local_coverage_report(report, object_hash=object_ref.sha256)
        input_hashes = [report.expected_file_sha256]
        if report.registered_file_sha256:
            input_hashes.append(report.registered_file_sha256)
        self.state.register_artifact(
            artifact_id=f"KnowledgeLocalCoverageReport:{report.report_id}",
            artifact_type="KnowledgeLocalCoverageReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted(set(input_hashes)),
        )

    def _persist_audit_report(self, report: KnowledgeCoverageAuditReport) -> None:
        object_ref = self.object_store.put_json(report.model_dump(mode="json"))
        self.repository.register_coverage_audit_report(report, object_hash=object_ref.sha256)
        self.state.register_artifact(
            artifact_id=f"KnowledgeCoverageAuditReport:{report.report_id}",
            artifact_type="KnowledgeCoverageAuditReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[content_hash(item) for item in report.source_reports],
        )


__all__ = ["KnowledgeCoverageAuditService"]
