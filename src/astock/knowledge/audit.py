"""Coverage and storage-integrity audits for allowlisted knowledge sources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pyarrow.parquet as pq

from astock.books import BookRepository, PrivateDocxRepository
from astock.core.errors import AStockError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentBlockRepository
from astock.knowledge.gaps import count_open_gap_boundaries, gap_cutoff_history_available
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
    ZhihuCommentPage,
    ZhihuContainerType,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuListingPage,
    ZhihuResponseKind,
)

_LOCAL_COVERAGE_BASIS = "USER_CONFIRMED_COMPLETE_EXPORT"
_STALE_RUNNING_AFTER = timedelta(hours=1)
_AUDIT_QUIESCENCE_LAG = timedelta(seconds=30)
_ACTIVE_RESPONSE_KINDS = (
    ZhihuResponseKind.LISTING,
    ZhihuResponseKind.CONTENT_DETAIL,
)


@dataclass(frozen=True, slots=True)
class _ParquetIndex:
    body_hash_by_version: dict[str, str]
    read_error_count: int


def _content_freshness(record: ZhihuContentRecord) -> datetime:
    return record.updated_at or record.published_at or record.collected_at


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

            parse_report = PrivateDocxRepository(self.state).latest_parse_report_for_manifest(
                manifest.manifest_id
            )
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
        *,
        quiescence_lag: timedelta = _AUDIT_QUIESCENCE_LAG,
    ) -> KnowledgeCoverageAuditReport:
        if quiescence_lag < timedelta(0):
            raise ValueError("quiescence_lag cannot be negative")
        audited_at = datetime.now(UTC)
        data_cutoff_at = audited_at - quiescence_lag
        source_reports = [
            self._audit_source(source, data_cutoff_at)
            for source in registry.sources
            if source.enabled
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
            report_id for source in source_reports for report_id in source.local_report_ids
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
                KnowledgeLocalCoverageReport.model_validate_json(
                    row["report_json"]
                ).missing_object_count
                for row in rows
            )
        with self.state.connect() as connection:
            attempt_rows = connection.execute(
                "SELECT j.job_id,a.started_at,a.ended_at FROM job_attempt a "
                "JOIN job j ON j.job_id=a.job_id WHERE j.type LIKE 'zhihu-%'"
            ).fetchall()
        stale_before = data_cutoff_at - _STALE_RUNNING_AFTER
        stale_running_jobs = len(
            {
                str(row["job_id"])
                for row in attempt_rows
                if (started_at := _parse_utc_text(str(row["started_at"])))
                <= data_cutoff_at
                and started_at < stale_before
                and (
                    row["ended_at"] is None
                    or _parse_utc_text(str(row["ended_at"])) > data_cutoff_at
                )
            }
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
        if any(
            "GAP_CUTOFF_HISTORY_UNAVAILABLE" in source.findings
            or any(
                "GAP_CUTOFF_HISTORY_UNAVAILABLE" in scope.findings
                for scope in source.scope_reports
            )
            for source in source_reports
        ):
            findings.append("GAP_CUTOFF_HISTORY_UNAVAILABLE")
        if any("IMPORT_CUTOFF_HISTORY_UNAVAILABLE" in source.findings for source in source_reports):
            findings.append("IMPORT_CUTOFF_HISTORY_UNAVAILABLE")
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
            "data_cutoff_at": data_cutoff_at,
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
            data_cutoff_at=data_cutoff_at,
            audited_at=audited_at,
            created_at=audited_at,
        )
        self._persist_audit_report(report)
        return report

    def _audit_source(
        self,
        source: KnowledgeSourceDefinition,
        data_cutoff_at: datetime,
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
            pending_imports = self.repository.pending_import_count(
                source.source_id,
                response_kinds=_ACTIVE_RESPONSE_KINDS,
                data_cutoff_at=data_cutoff_at,
            )
            open_gaps = self._open_gap_count(source.source_id, data_cutoff_at)
            findings = [] if complete else ["LOCAL_EXPORT_INTEGRITY_INCOMPLETE"]
            if pending_imports:
                findings.append("PENDING_IMPORTED_RESPONSES")
            if open_gaps:
                findings.append("OPEN_COLLECTION_GAPS")
            if (
                self._gap_events_exist(source.source_id)
                and not gap_cutoff_history_available(self.state, data_cutoff_at)
            ):
                findings.append("GAP_CUTOFF_HISTORY_UNAVAILABLE")
            if self.repository.rejected_import_temporal_count(
                source.source_id,
                response_kinds=_ACTIVE_RESPONSE_KINDS,
                data_cutoff_at=data_cutoff_at,
            ):
                findings.append("IMPORT_CUTOFF_HISTORY_UNAVAILABLE")
            findings = sorted(set(findings))
            return KnowledgeSourceCoverageAudit(
                source_id=source.source_id,
                online_collection_required=False,
                identity_status=source.identity_status,
                identity_registered=True,
                local_report_ids=[report.report_id for report in local_reports],
                pending_import_count=pending_imports,
                open_gap_count=open_gaps,
                status=(
                    KnowledgeAuditStatus.USER_CONFIRMED_COMPLETE_EXPORT
                    if complete and not findings
                    else KnowledgeAuditStatus.PARTIAL
                ),
                findings=findings,
            )

        identity_registered = self._identity_registered(source.source_id, data_cutoff_at)
        gap_history_available = gap_cutoff_history_available(self.state, data_cutoff_at)
        scope_reports = [
            self._audit_online_scope(
                source,
                ZhihuContentType(content_type),
                data_cutoff_at,
            )
            for content_type in source.collection_scope.content_types
        ]
        pending_imports = self.repository.pending_import_count(
            source.source_id,
            response_kinds=_ACTIVE_RESPONSE_KINDS,
            data_cutoff_at=data_cutoff_at,
        )
        open_gaps = self._open_gap_count(source.source_id, data_cutoff_at)
        findings: list[str] = []
        if not identity_registered:
            findings.append("CONFIRMED_IDENTITY_NOT_REGISTERED")
        if pending_imports:
            findings.append("PENDING_IMPORTED_RESPONSES")
        if open_gaps:
            findings.append("OPEN_COLLECTION_GAPS")
        if self._gap_events_exist(source.source_id) and not gap_history_available:
            findings.append("GAP_CUTOFF_HISTORY_UNAVAILABLE")
        if self.repository.rejected_import_temporal_count(
            source.source_id,
            response_kinds=_ACTIVE_RESPONSE_KINDS,
            data_cutoff_at=data_cutoff_at,
        ):
            findings.append("IMPORT_CUTOFF_HISTORY_UNAVAILABLE")
        if ZhihuContainerType.COLUMNS in source.collection_scope.container_types:
            findings.append("COLUMN_ENUMERATION_NOT_VERIFIED")
        if any(
            scope.status in {KnowledgeAuditStatus.PARTIAL, KnowledgeAuditStatus.NOT_COLLECTED}
            for scope in scope_reports
        ):
            findings.append("SCOPE_COVERAGE_INCOMPLETE")
        findings = sorted(set(findings))
        if findings:
            status = KnowledgeAuditStatus.PARTIAL
        elif any(scope.status is KnowledgeAuditStatus.ACCESS_RESTRICTED for scope in scope_reports):
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
        data_cutoff_at: datetime,
    ) -> KnowledgeScopeCoverageAudit:
        latest = self.repository.latest_coverage_report(
            source.source_id,
            content_type,
            data_cutoff_at=data_cutoff_at,
        )
        with self.state.connect() as connection:
            content_rows = connection.execute(
                "SELECT version_id,body_object_hash,content_id,record_json,collected_at "
                "FROM zhihu_content_version "
                "WHERE source_id=? AND content_type=?",
                (source.source_id, content_type.value),
            ).fetchall()
            listing_page_rows = connection.execute(
                "SELECT page_json,fetched_at FROM zhihu_listing_page_manifest "
                "WHERE source_id=? AND content_type=?",
                (source.source_id, content_type.value),
            ).fetchall()
        content_rows = [
            row
            for row in content_rows
            if _parse_utc_text(str(row["collected_at"])) <= data_cutoff_at
        ]
        listing_page_rows = [
            row
            for row in listing_page_rows
            if _parse_utc_text(str(row["fetched_at"])) <= data_cutoff_at
        ]
        sqlite_content = {
            str(row["version_id"]): str(row["body_object_hash"]) for row in content_rows
        }
        content_parquet = self._read_parquet_index(
            "knowledge_content",
            source.source_id,
            content_type,
            data_cutoff_at,
        )
        content_ids = set(sqlite_content)
        content_parquet_ids = set(content_parquet.body_hash_by_version)
        missing_content_bodies = sum(
            not self.object_store.verify(body_hash) for body_hash in sqlite_content.values()
        )
        content_hash_mismatches = sum(
            sqlite_content[version_id] != content_parquet.body_hash_by_version[version_id]
            for version_id in content_ids & content_parquet_ids
        )
        content_records = [
            ZhihuContentRecord.model_validate_json(row["record_json"]) for row in content_rows
        ]
        discovered_content_ids = {record.content_id for record in content_records}
        listing_pages: list[ZhihuListingPage] = []
        listing_page_decode_errors = 0
        for row in listing_page_rows:
            try:
                listing_pages.append(ZhihuListingPage.model_validate_json(row["page_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                listing_page_decode_errors += 1
        terminal_pages = [page for page in listing_pages if page.is_end]
        latest_terminal_page = (
            max(terminal_pages, key=lambda page: (page.listing_page, page.fetched_at))
            if terminal_pages
            else None
        )
        reported_totals: set[int] = set()
        listing_total_read_errors = 0
        listing_reported_total: int | None = None
        for page in listing_pages:
            reported_total, read_error = self._listing_reported_total(page)
            listing_total_read_errors += int(read_error)
            if reported_total is not None:
                reported_totals.add(reported_total)
            if latest_terminal_page is not None and page.page_id == latest_terminal_page.page_id:
                listing_reported_total = reported_total
        listing_total_mismatch = (
            abs(len(discovered_content_ids) - listing_reported_total)
            if listing_reported_total is not None
            else 0
        )
        listing_total_changes = max(0, len(reported_totals) - 1)
        detail_verified_ids: set[str] = set()
        detail_stale_ids: set[str] = set()
        for content_id in discovered_content_ids:
            listing_records = [
                record
                for record in content_records
                if record.content_id == content_id
                and record.content_completeness is ZhihuContentCompleteness.LISTING_UNVERIFIED
            ]
            detail_records = [
                record
                for record in content_records
                if record.content_id == content_id
                and record.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
            ]
            if not detail_records:
                continue
            latest_detail = max(detail_records, key=_content_freshness)
            latest_listing = (
                max(listing_records, key=_content_freshness) if listing_records else None
            )
            if latest_listing is None or _content_freshness(latest_detail) >= _content_freshness(
                latest_listing
            ):
                detail_verified_ids.add(content_id)
            else:
                detail_stale_ids.add(content_id)
        gap_history_available = gap_cutoff_history_available(self.state, data_cutoff_at)
        open_gaps = self._scope_open_gap_count(
            source.source_id,
            content_type,
            data_cutoff_at,
        )
        missing_content_parquet = len(content_ids - content_parquet_ids)
        orphan_content_parquet = len(content_parquet_ids - content_ids)
        findings: list[str] = []
        counts = {
            "MISSING_CONTENT_BODY_OBJECTS": missing_content_bodies,
            "MISSING_CONTENT_PARQUET_ROWS": missing_content_parquet,
            "ORPHAN_CONTENT_PARQUET_ROWS": orphan_content_parquet,
            "CONTENT_PARQUET_HASH_MISMATCH": content_hash_mismatches,
            "CONTENT_PARQUET_READ_ERRORS": content_parquet.read_error_count,
            "LISTING_PAGE_DECODE_ERRORS": listing_page_decode_errors,
            "LISTING_TOTAL_READ_ERRORS": listing_total_read_errors,
            "LISTING_TOTAL_MISMATCH": listing_total_mismatch,
            "LISTING_REPORTED_TOTAL_CHANGED": listing_total_changes,
            "CONTENT_DETAILS_INCOMPLETE": (len(discovered_content_ids) - len(detail_verified_ids)),
            "CONTENT_DETAILS_STALE": len(detail_stale_ids),
            "OPEN_COLLECTION_GAPS": open_gaps,
        }
        findings.extend(f"{code}:{count}" for code, count in counts.items() if count)
        if not gap_history_available:
            findings.append("GAP_CUTOFF_HISTORY_UNAVAILABLE")
        integrity_failure = any(
            counts[key]
            for key in (
                "MISSING_CONTENT_BODY_OBJECTS",
                "MISSING_CONTENT_PARQUET_ROWS",
                "ORPHAN_CONTENT_PARQUET_ROWS",
                "CONTENT_PARQUET_HASH_MISMATCH",
                "CONTENT_PARQUET_READ_ERRORS",
                "LISTING_PAGE_DECODE_ERRORS",
                "LISTING_TOTAL_READ_ERRORS",
            )
        )
        detail_incomplete = len(detail_verified_ids) != len(discovered_content_ids)
        listing_incomplete = bool(listing_total_mismatch or listing_total_changes)
        if latest is None:
            status = KnowledgeAuditStatus.NOT_COLLECTED
            findings.append("LISTING_COVERAGE_REPORT_NOT_FOUND")
        elif (
            integrity_failure
            or listing_incomplete
            or detail_incomplete
            or not gap_history_available
        ):
            status = KnowledgeAuditStatus.PARTIAL
        elif latest.coverage_status is CoverageStatus.ACCESS_RESTRICTED:
            status = KnowledgeAuditStatus.ACCESS_RESTRICTED
        elif latest.coverage_status is not CoverageStatus.COMPLETE or open_gaps:
            status = KnowledgeAuditStatus.PARTIAL
        else:
            status = KnowledgeAuditStatus.PASS
        return KnowledgeScopeCoverageAudit(
            content_type=content_type.value,
            data_cutoff_at=data_cutoff_at,
            listing_report_id=latest.report_id if latest else None,
            listing_terminal_condition=latest.terminal_condition if latest else None,
            listing_coverage_status=latest.coverage_status if latest else None,
            listing_reported_total=listing_reported_total,
            listing_unique_content_count=len(discovered_content_ids),
            listing_total_mismatch_count=listing_total_mismatch,
            listing_total_change_count=listing_total_changes,
            listing_page_decode_error_count=listing_page_decode_errors,
            listing_total_read_error_count=listing_total_read_errors,
            sqlite_content_version_count=len(content_ids),
            parquet_content_version_count=len(content_parquet_ids),
            verified_content_body_count=len(content_ids) - missing_content_bodies,
            detail_required_count=len(discovered_content_ids),
            detail_verified_count=len(detail_verified_ids),
            detail_stale_count=len(detail_stale_ids),
            missing_content_body_count=missing_content_bodies,
            missing_content_parquet_count=missing_content_parquet,
            orphan_content_parquet_count=orphan_content_parquet,
            content_parquet_hash_mismatch_count=content_hash_mismatches,
            content_parquet_read_error_count=content_parquet.read_error_count,
            sqlite_comment_version_count=0,
            parquet_comment_version_count=0,
            verified_comment_body_count=0,
            missing_comment_body_count=0,
            missing_comment_parquet_count=0,
            orphan_comment_parquet_count=0,
            comment_parquet_hash_mismatch_count=0,
            comment_parquet_read_error_count=0,
            root_comment_required_count=0,
            root_comment_terminal_count=0,
            root_comment_total_mismatch_count=0,
            root_comment_total_change_count=0,
            platform_comment_total_mismatch_count=0,
            platform_comment_total_change_count=0,
            comment_page_decode_error_count=0,
            comment_total_read_error_count=0,
            comment_pagination_cycle_count=0,
            child_reply_required_count=0,
            child_reply_terminal_count=0,
            child_reply_count_mismatch_count=0,
            open_gap_count=open_gaps,
            status=status,
            findings=sorted(set(findings)),
        )

    def _listing_reported_total(self, page: ZhihuListingPage) -> tuple[int | None, bool]:
        if page.reported_total is not None:
            return page.reported_total, False
        try:
            payload = json.loads(self.object_store.get_bytes(page.raw_object_sha256))
        except (AStockError, UnicodeDecodeError, json.JSONDecodeError):
            return None, True
        if not isinstance(payload, dict):
            return None, True
        paging = payload.get("paging")
        if not isinstance(paging, dict):
            return None, True
        reported_total = paging.get("totals")
        if reported_total is None:
            return None, False
        if (
            isinstance(reported_total, bool)
            or not isinstance(reported_total, int)
            or reported_total < 0
        ):
            return None, True
        return reported_total, False

    def _comment_reported_total(self, page: ZhihuCommentPage) -> tuple[int | None, bool]:
        if page.reported_total is not None:
            return page.reported_total, False
        try:
            payload = json.loads(self.object_store.get_bytes(page.raw_object_sha256))
        except (AStockError, UnicodeDecodeError, json.JSONDecodeError):
            return None, True
        if not isinstance(payload, dict) or not isinstance(payload.get("paging"), dict):
            return None, True
        reported_total = payload["paging"].get("totals")
        if reported_total is None:
            return None, False
        if (
            isinstance(reported_total, bool)
            or not isinstance(reported_total, int)
            or reported_total < 0
        ):
            return None, True
        return reported_total, False

    def _comment_terminals(
        self,
        source_id: str,
        content_type: ZhihuContentType,
        data_cutoff_at: datetime,
    ) -> tuple[set[str], set[tuple[str, str]], int]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT cursor_json FROM checkpoint WHERE scope_type='author-collection' "
                "AND committed_at<=?",
                (data_cutoff_at.isoformat(),),
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
                child_terminal.add((checkpoint.content_id, checkpoint.comment_parent_id))
        return root_terminal, child_terminal, decode_errors

    def _read_parquet_index(
        self,
        dataset_name: str,
        source_id: str,
        content_type: ZhihuContentType,
        data_cutoff_at: datetime,
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
                rows = (
                    pq.ParquetFile(path)
                    .read(columns=["version_id", "body_object_sha256", "collected_at"])
                    .to_pylist()
                )
                for row in rows:
                    collected_at = row.get("collected_at")
                    if not isinstance(collected_at, datetime):
                        read_errors += 1
                        continue
                    if collected_at > data_cutoff_at:
                        continue
                    version_id = str(row["version_id"])
                    body_hash = str(row["body_object_sha256"])
                    previous = body_hash_by_version.get(version_id)
                    if previous is not None and previous != body_hash:
                        read_errors += 1
                    body_hash_by_version[version_id] = body_hash
            except (OSError, ValueError, KeyError):
                read_errors += 1
        return _ParquetIndex(body_hash_by_version, read_errors)

    def _identity_registered(self, source_id: str, data_cutoff_at: datetime) -> bool:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT verified_at FROM knowledge_source_identity WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return row is not None and _parse_utc_text(str(row["verified_at"])) <= data_cutoff_at

    def _gap_events_exist(self, source_id: str) -> bool:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM collection_gap_state_event e "
                "JOIN collection_scope s ON s.scope_id=e.scope_id "
                "WHERE s.author_id=? AND s.content_type NOT LIKE 'comments:%' LIMIT 1",
                (source_id,),
            ).fetchone()
        return row is not None

    def _open_gap_count(self, source_id: str, data_cutoff_at: datetime) -> int:
        return count_open_gap_boundaries(
            self.state,
            source_id,
            excluded_scope_prefix="comments:%",
            data_cutoff_at=data_cutoff_at,
        )

    def _scope_open_gap_count(
        self,
        source_id: str,
        content_type: ZhihuContentType,
        data_cutoff_at: datetime,
    ) -> int:
        return count_open_gap_boundaries(
            self.state,
            source_id,
            content_type=content_type.value,
            data_cutoff_at=data_cutoff_at,
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


def _parse_utc_text(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


__all__ = ["KnowledgeCoverageAuditService"]
