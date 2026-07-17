"""Deterministic, private-safe knowledge cleaning and classification."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astock.books import PrivateDocxRepository
from astock.core.errors import StorageError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentBlockRepository
from astock.knowledge.distillation_repository import DistillationRepository
from astock.knowledge.distillation_storage import ParquetDistillationStore
from astock.schemas import (
    BOOK_DOWNWEIGHT_CLASSES,
    BOOK_KEEP_CLASSES,
    AuthorDistillationReport,
    BookCleaningReport,
    BookContentClass,
    BookMethodCategory,
    BookMethodCoverageMetric,
    BookMethodCoverageReport,
    BookParseReport,
    BookParseScope,
    BookProcessingStatus,
    BookSourceManifest,
    CoverageStatus,
    DistillationClassRuleSet,
    DistillationDecision,
    DistillationLocatorType,
    DistillationReviewQueue,
    DistillationRun,
    DistillationRunStatus,
    DistillationSourceLocator,
    DistillationUnit,
    DocumentType,
    HumanReviewStatus,
    KnowledgeAuditStatus,
    KnowledgeSourceDefinition,
    PrivateDocxParseReport,
    ZhihuCommentNode,
    ZhihuContentRecord,
)

_HTML_TAG = re.compile(r"<[^>]+>")
_LINE = re.compile(r"[^\r\n]+")
_ZERO_WIDTH = re.compile("[\u200b-\u200d\ufeff]")
_WHITESPACE = re.compile(r"\s+")
_BATCH_SIZE = 200

_METHOD_BY_CLASS = {
    BookContentClass.STOCK_SELECTION: BookMethodCategory.STOCK_SELECTION,
    BookContentClass.BUSINESS_MODEL: BookMethodCategory.BUSINESS_MODEL,
    BookContentClass.INDUSTRY: BookMethodCategory.INDUSTRY,
    BookContentClass.VALUATION: BookMethodCategory.VALUATION,
    BookContentClass.FINANCIAL_QUALITY: BookMethodCategory.FINANCIAL_QUALITY,
    BookContentClass.ENTRY: BookMethodCategory.ENTRY,
    BookContentClass.HOLDING_VALIDATION: BookMethodCategory.HOLDING,
    BookContentClass.ADD: BookMethodCategory.ADD,
    BookContentClass.TRIM: BookMethodCategory.TRIM,
    BookContentClass.EXIT: BookMethodCategory.EXIT,
    BookContentClass.RISK_CONTROL: BookMethodCategory.RISK,
    BookContentClass.FAILURE_CASE: BookMethodCategory.FAILURE_CASE,
    BookContentClass.COUNTEREVIDENCE_INVALIDATION: (
        BookMethodCategory.COUNTEREVIDENCE_INVALIDATION
    ),
    BookContentClass.REVIEW_METHOD: BookMethodCategory.REVIEW,
}


@dataclass(frozen=True, slots=True)
class _SourceItem:
    source_id: str
    source_snapshot_id: str
    source_unit_id: str
    source_object_sha256: str
    locator_type: DistillationLocatorType
    parser_or_schema_version: str
    raw_text: str | None
    page_number: int | None = None
    block_index: int | None = None
    content_id: str | None = None
    comment_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ManifestContext:
    manifest: BookSourceManifest
    parse_report_id: str
    original_page_count: int
    original_char_count: int
    successfully_parsed_page_count: int
    ocr_page_count: int


@dataclass(frozen=True, slots=True)
class _InputBundle:
    items: tuple[_SourceItem, ...]
    input_hashes: tuple[str, ...]
    input_source_ids: tuple[str, ...]
    manifests: tuple[_ManifestContext, ...]
    online_content_count: int
    target_author_comment_count: int
    open_collection_gap_count: int
    missing_object_count: int
    upstream_coverage_status: CoverageStatus


@dataclass(frozen=True, slots=True)
class DistillationExecution:
    run: DistillationRun
    report: AuthorDistillationReport
    review_queue: DistillationReviewQueue
    parquet_file: Path
    book_cleaning_report_ids: tuple[str, ...]
    book_method_coverage_report_ids: tuple[str, ...]


class KnowledgeDistillationService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        parquet_root: Path,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.repository = DistillationRepository(state)
        self.parquet_store = ParquetDistillationStore(parquet_root)

    def plan(
        self,
        source: KnowledgeSourceDefinition,
        rules: DistillationClassRuleSet,
    ) -> dict[str, object]:
        bundle = self._discover_inputs(source)
        run_id = self._run_id(source.source_id, rules.rule_version, bundle.input_hashes)
        return {
            "run_id": run_id,
            "author_source_id": source.source_id,
            "classification_rule_version": rules.rule_version,
            "input_source_ids": list(bundle.input_source_ids),
            "local_manifest_count": len(bundle.manifests),
            "input_source_item_count": len(bundle.items),
            "online_content_count": bundle.online_content_count,
            "target_author_comment_count": bundle.target_author_comment_count,
            "open_collection_gap_count": bundle.open_collection_gap_count,
            "missing_object_count": bundle.missing_object_count,
            "upstream_coverage_status": bundle.upstream_coverage_status.value,
        }

    def run(
        self,
        source: KnowledgeSourceDefinition,
        rules: DistillationClassRuleSet,
    ) -> DistillationExecution:
        bundle = self._discover_inputs(source)
        run_id = self._run_id(source.source_id, rules.rule_version, bundle.input_hashes)
        started_at = datetime.now(UTC)
        proposed = DistillationRun(
            run_id=run_id,
            author_source_id=source.source_id,
            classification_rule_version=rules.rule_version,
            input_hashes=list(bundle.input_hashes),
            input_source_ids=list(bundle.input_source_ids),
            status=DistillationRunStatus.RUNNING,
            input_source_item_count=len(bundle.items),
            empty_source_item_count=0,
            produced_unit_count=0,
            canonical_unit_count=0,
            duplicate_unit_count=0,
            started_at=started_at,
            created_at=started_at,
        )
        run = self.repository.register_run(proposed)
        units, empty_source_items = self._build_units(run, bundle, rules)
        for start in range(0, len(units), _BATCH_SIZE):
            batch = units[start : start + _BATCH_SIZE]
            self.repository.register_units(batch)
            self.state.set_checkpoint(
                scope_type="knowledge-distillation",
                scope_key=run.run_id,
                cursor={
                    "registered_unit_count": start + len(batch),
                    "last_unit_id": batch[-1].unit_id,
                },
                status="RUNNING",
                object_hash=batch[-1].normalized_text_sha256,
            )
        stored_units = self.repository.units_for_run(run.run_id)
        if [unit.unit_id for unit in stored_units] != [unit.unit_id for unit in units]:
            raise ValueError("distillation SQLite unit order does not match deterministic output")
        parquet_file = self.parquet_store.write_run(source.source_id, run.run_id, units)
        canonical_count = sum(unit.duplicate_of_unit_id is None for unit in units)
        finished = (
            run
            if run.status is DistillationRunStatus.COMPLETE
            else self.repository.complete_run(
                run.model_copy(
                    update={
                        "status": DistillationRunStatus.COMPLETE,
                        "empty_source_item_count": empty_source_items,
                        "produced_unit_count": len(units),
                        "canonical_unit_count": canonical_count,
                        "duplicate_unit_count": len(units) - canonical_count,
                        "finished_at": datetime.now(UTC),
                    }
                )
            )
        )
        assert finished.finished_at is not None
        self.state.set_checkpoint(
            scope_type="knowledge-distillation",
            scope_key=finished.run_id,
            cursor={
                "registered_unit_count": len(units),
                "last_unit_id": units[-1].unit_id if units else None,
            },
            status="SUCCEEDED",
            object_hash=content_hash([unit.unit_id for unit in units]),
        )
        run_object = self.object_store.put_json(finished.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"DistillationRun:{finished.run_id}",
            artifact_type="DistillationRun",
            schema_version=finished.schema_version,
            object_hash=run_object.sha256,
            input_hashes=finished.input_hashes,
        )
        queue = self._persist_review_queue(finished, units)
        cleaning_ids, method_ids = self._persist_book_reports(
            finished,
            bundle.manifests,
            units,
        )
        report = self._persist_author_report(
            finished,
            bundle,
            units,
            queue,
            parquet_file,
        )
        return DistillationExecution(
            run=finished,
            report=report,
            review_queue=queue,
            parquet_file=parquet_file,
            book_cleaning_report_ids=tuple(cleaning_ids),
            book_method_coverage_report_ids=tuple(method_ids),
        )

    def audit(self, author_source_id: str) -> dict[str, object]:
        report = self.repository.latest_author_report(author_source_id)
        if report is None:
            return {
                "status": "NOT_RUN",
                "author_source_id": author_source_id,
            }
        run = self.repository.get_run(report.run_id)
        if run is None:
            return {
                "status": "PARTIAL",
                "author_source_id": author_source_id,
                "finding_codes": ["RUN_METADATA_MISSING"],
            }
        units = self.repository.units_for_run(run.run_id)
        sqlite_index = {
            unit.unit_id: unit.normalized_text_sha256 for unit in units
        }
        parquet_index = self.parquet_store.unit_hash_index(author_source_id, run.run_id)
        normalized_hashes = {unit.normalized_text_sha256 for unit in units}
        source_hashes = {unit.locator.source_object_sha256 for unit in units}
        missing_normalized = sum(
            not self.object_store.verify(object_hash)
            for object_hash in normalized_hashes
        )
        missing_sources = sum(
            not self.object_store.verify(object_hash) for object_hash in source_hashes
        )
        missing_parquet = len(set(sqlite_index) - set(parquet_index))
        orphan_parquet = len(set(parquet_index) - set(sqlite_index))
        parquet_hash_mismatch = sum(
            sqlite_index[unit_id] != parquet_index[unit_id]
            for unit_id in set(sqlite_index) & set(parquet_index)
        )
        decision_count = sum(
            (
                report.keep_candidate_count,
                report.downweight_candidate_count,
                report.unclassified_count,
            )
        )
        count_mismatch = int(
            len(units) != report.unit_count
            or decision_count != len(units)
            or sum(unit.duplicate_of_unit_id is None for unit in units)
            != report.canonical_unit_count
        )
        findings = {
            "NORMALIZED_OBJECT_MISSING": missing_normalized,
            "SOURCE_OBJECT_MISSING": missing_sources,
            "SQLITE_PARQUET_ROWS_MISSING": missing_parquet,
            "ORPHAN_PARQUET_ROWS": orphan_parquet,
            "SQLITE_PARQUET_HASH_MISMATCH": parquet_hash_mismatch,
            "REPORT_COUNT_MISMATCH": count_mismatch,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "author_source_id": author_source_id,
            "run_id": run.run_id,
            "unit_count": len(units),
            "unique_normalized_object_count": len(normalized_hashes),
            "unique_source_object_count": len(source_hashes),
            "missing_normalized_object_count": missing_normalized,
            "missing_source_object_count": missing_sources,
            "missing_parquet_row_count": missing_parquet,
            "orphan_parquet_row_count": orphan_parquet,
            "parquet_hash_mismatch_count": parquet_hash_mismatch,
            "report_count_mismatch_count": count_mismatch,
            "finding_codes": finding_codes,
        }

    def _discover_inputs(self, source: KnowledgeSourceDefinition) -> _InputBundle:
        items: list[_SourceItem] = []
        input_hashes: set[str] = set()
        input_source_ids: set[str] = set()
        manifest_contexts: list[_ManifestContext] = []
        missing_objects = 0
        manifests = self._author_manifests(source.source_id)
        for manifest in manifests:
            input_hashes.add(manifest.file_sha256)
            input_source_ids.add(manifest.source_id)
            if manifest.document_type is DocumentType.PRIVATE_DOCX:
                report = PrivateDocxRepository(
                    self.state
                ).latest_parse_report_for_manifest(manifest.manifest_id)
                if report is None:
                    continue
                input_hashes.add(report.block_set_sha256)
                if report.report_object_sha256:
                    input_hashes.add(report.report_object_sha256)
                context, source_items, missing = self._docx_items(manifest, report)
            else:
                report = self._full_pdf_report(manifest.manifest_id)
                if report is None:
                    continue
                if report.report_object_sha256:
                    input_hashes.add(report.report_object_sha256)
                context, source_items, missing = self._pdf_items(manifest, report)
            manifest_contexts.append(context)
            items.extend(source_items)
            missing_objects += missing
            input_hashes.update(item.source_object_sha256 for item in source_items)

        contents = self._latest_content(source.source_id)
        comments = self._latest_target_author_comments(source.source_id)
        if source.online_collection_required or contents or comments:
            input_source_ids.add(source.source_id)
        for record in contents:
            item, missing = self._content_item(record)
            items.append(item)
            missing_objects += missing
            input_hashes.update(
                {record.body_object_sha256, record.metadata_sha256}
            )
        for record in comments:
            item, missing = self._comment_item(record)
            items.append(item)
            missing_objects += missing
            input_hashes.update(
                {record.body_object_sha256, record.metadata_sha256}
            )
        if not input_hashes:
            raise ValueError(f"no immutable distillation inputs for {source.source_id}")
        items.sort(key=_source_item_sort_key)
        open_gaps = self._open_gap_count(source.source_id)
        upstream = self._upstream_coverage(source, open_gaps)
        return _InputBundle(
            items=tuple(items),
            input_hashes=tuple(sorted(input_hashes)),
            input_source_ids=tuple(sorted(input_source_ids)),
            manifests=tuple(sorted(manifest_contexts, key=lambda item: item.manifest.source_id)),
            online_content_count=len(contents),
            target_author_comment_count=len(comments),
            open_collection_gap_count=open_gaps,
            missing_object_count=missing_objects,
            upstream_coverage_status=upstream,
        )

    def _author_manifests(self, author_source_id: str) -> list[BookSourceManifest]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM book_source_manifest ORDER BY source_id,file_version"
            ).fetchall()
        manifests = [BookSourceManifest.model_validate_json(row["manifest_json"]) for row in rows]
        return [item for item in manifests if item.author_source_id == author_source_id]

    def _full_pdf_report(self, manifest_id: str) -> BookParseReport | None:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT report_json FROM book_parse_report WHERE manifest_id=? "
                "ORDER BY created_at DESC,book_parse_report_id DESC",
                (manifest_id,),
            ).fetchall()
        reports = [BookParseReport.model_validate_json(row["report_json"]) for row in rows]
        return next(
            (
                report
                for report in reports
                if report.parse_scope is BookParseScope.FULL_SOURCE
                and report.processing_status is BookProcessingStatus.COMPLETE
            ),
            None,
        )

    def _pdf_items(
        self,
        manifest: BookSourceManifest,
        report: BookParseReport,
    ) -> tuple[_ManifestContext, list[_SourceItem], int]:
        items: list[_SourceItem] = []
        missing = 0
        for page in report.pages:
            raw_text, was_missing = self._read_text(page.text_object_sha256)
            missing += was_missing
            items.append(
                _SourceItem(
                    source_id=manifest.source_id,
                    source_snapshot_id=manifest.snapshot_id,
                    source_unit_id=page.page_id,
                    source_object_sha256=page.text_object_sha256,
                    locator_type=DistillationLocatorType.PAGE_TEXT,
                    parser_or_schema_version=page.parser_version,
                    raw_text=raw_text,
                    page_number=page.page_number,
                )
            )
        context = _ManifestContext(
            manifest=manifest,
            parse_report_id=report.book_parse_report_id,
            original_page_count=report.source_page_count,
            original_char_count=report.parsed_text_char_count,
            successfully_parsed_page_count=report.processed_page_count,
            ocr_page_count=report.ocr_page_count,
        )
        return context, items, missing

    def _docx_items(
        self,
        manifest: BookSourceManifest,
        report: PrivateDocxParseReport,
    ) -> tuple[_ManifestContext, list[_SourceItem], int]:
        blocks = DocumentBlockRepository(self.state).blocks_for(
            report.snapshot_id,
            report.parser_version,
        )
        items: list[_SourceItem] = []
        missing = 0
        for block in blocks:
            raw_text, was_missing = self._read_text(block.text_object_sha256)
            missing += was_missing
            items.append(
                _SourceItem(
                    source_id=manifest.source_id,
                    source_snapshot_id=manifest.snapshot_id,
                    source_unit_id=block.block_id,
                    source_object_sha256=block.text_object_sha256,
                    locator_type=DistillationLocatorType.BLOCK_TEXT,
                    parser_or_schema_version=block.parser_version,
                    raw_text=raw_text,
                    block_index=block.block_index,
                )
            )
        context = _ManifestContext(
            manifest=manifest,
            parse_report_id=report.docx_parse_report_id,
            original_page_count=0,
            original_char_count=report.parsed_text_char_count,
            successfully_parsed_page_count=0,
            ocr_page_count=0,
        )
        return context, items, missing

    def _latest_content(self, author_source_id: str) -> list[ZhihuContentRecord]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM zhihu_content_version WHERE source_id=? "
                "ORDER BY content_type,content_id,collected_at,version_id",
                (author_source_id,),
            ).fetchall()
        latest: dict[tuple[str, str], ZhihuContentRecord] = {}
        for row in rows:
            record = ZhihuContentRecord.model_validate_json(row["record_json"])
            latest[(record.content_type.value, record.content_id)] = record
        return [latest[key] for key in sorted(latest)]

    def _latest_target_author_comments(
        self,
        author_source_id: str,
    ) -> list[ZhihuCommentNode]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM zhihu_comment_version WHERE source_id=? "
                "ORDER BY content_type,content_id,comment_id,collected_at,version_id",
                (author_source_id,),
            ).fetchall()
        latest: dict[tuple[str, str, str], ZhihuCommentNode] = {}
        for row in rows:
            record = ZhihuCommentNode.model_validate_json(row["record_json"])
            if record.is_target_author:
                latest[
                    (record.content_type.value, record.content_id, record.comment_id)
                ] = record
        return [latest[key] for key in sorted(latest)]

    def _content_item(self, record: ZhihuContentRecord) -> tuple[_SourceItem, int]:
        raw_text, missing = self._read_text(record.body_object_sha256)
        return (
            _SourceItem(
                source_id=record.author_source_id,
                source_snapshot_id=record.raw_source_snapshot_id,
                source_unit_id=record.version_id,
                source_object_sha256=record.body_object_sha256,
                locator_type=DistillationLocatorType.ZHIHU_CONTENT,
                parser_or_schema_version=record.schema_version,
                raw_text=raw_text,
                content_id=record.content_id,
            ),
            missing,
        )

    def _comment_item(self, record: ZhihuCommentNode) -> tuple[_SourceItem, int]:
        raw_text, missing = self._read_text(record.body_object_sha256)
        return (
            _SourceItem(
                source_id=record.author_source_id,
                source_snapshot_id=record.raw_source_snapshot_id,
                source_unit_id=record.version_id,
                source_object_sha256=record.body_object_sha256,
                locator_type=DistillationLocatorType.ZHIHU_COMMENT,
                parser_or_schema_version=record.schema_version,
                raw_text=raw_text,
                content_id=record.content_id,
                comment_id=record.comment_id,
            ),
            missing,
        )

    def _read_text(self, object_hash: str) -> tuple[str | None, int]:
        try:
            return self.object_store.get_bytes(object_hash).decode("utf-8"), 0
        except (StorageError, UnicodeDecodeError):
            return None, 1

    def _build_units(
        self,
        run: DistillationRun,
        bundle: _InputBundle,
        rules: DistillationClassRuleSet,
    ) -> tuple[list[DistillationUnit], int]:
        canonical_by_hash: dict[str, str] = {}
        units: list[DistillationUnit] = []
        empty_source_items = 0
        for source_item_ordinal, item in enumerate(bundle.items, start=1):
            segments = _segments(item)
            if not segments:
                if item.raw_text is not None:
                    empty_source_items += 1
                continue
            for segment_ordinal, (text, char_start, char_end) in enumerate(
                segments,
                start=1,
            ):
                text_object = self.object_store.put_bytes(text.encode("utf-8"))
                unit_identity = {
                    "run_id": run.run_id,
                    "source_unit_id": item.source_unit_id,
                    "segment_ordinal": segment_ordinal,
                    "normalized_text_sha256": text_object.sha256,
                }
                unit_id = f"distillation-unit:{content_hash(unit_identity)}"
                duplicate_of = canonical_by_hash.get(text_object.sha256)
                content_classes, methods, decision, reasons, scores = _classify(
                    text,
                    rules,
                    duplicate_of_unit_id=duplicate_of,
                )
                locator = DistillationSourceLocator(
                    locator_type=item.locator_type,
                    source_snapshot_id=item.source_snapshot_id,
                    source_unit_id=item.source_unit_id,
                    source_object_sha256=item.source_object_sha256,
                    page_number=item.page_number,
                    block_index=item.block_index,
                    content_id=item.content_id,
                    comment_id=item.comment_id,
                    char_start=char_start,
                    char_end=char_end,
                    parser_or_schema_version=item.parser_or_schema_version,
                    created_at=run.started_at,
                )
                unit = DistillationUnit(
                    unit_id=unit_id,
                    run_id=run.run_id,
                    author_source_id=run.author_source_id,
                    source_id=item.source_id,
                    source_item_ordinal=source_item_ordinal,
                    segment_ordinal=segment_ordinal,
                    locator=locator,
                    normalized_text_sha256=text_object.sha256,
                    normalized_char_count=len(text),
                    duplicate_of_unit_id=duplicate_of,
                    content_classes=content_classes,
                    method_categories=methods,
                    decision=decision,
                    reason_codes=reasons,
                    score_by_content_class=scores,
                    classification_rule_version=rules.rule_version,
                    created_at=run.started_at,
                )
                units.append(unit)
                canonical_by_hash.setdefault(text_object.sha256, unit_id)
        return units, empty_source_items

    def _persist_review_queue(
        self,
        run: DistillationRun,
        units: list[DistillationUnit],
    ) -> DistillationReviewQueue:
        existing_hash = self.repository.review_queue_object_hash_for_run(run.run_id)
        if existing_hash is not None:
            return DistillationReviewQueue.model_validate_json(
                self.object_store.get_bytes(existing_hash)
            )
        unit_ids = [unit.unit_id for unit in units if unit.duplicate_of_unit_id is None]
        assert run.finished_at is not None
        queue_identity = {"run_id": run.run_id, "units": unit_ids}
        queue = DistillationReviewQueue(
            queue_id=f"distillation-review:{content_hash(queue_identity)}",
            run_id=run.run_id,
            author_source_id=run.author_source_id,
            unit_ids=unit_ids,
            human_review_status=HumanReviewStatus.PENDING,
            created_at=run.finished_at,
        )
        queue_object = self.object_store.put_json(queue.model_dump(mode="json"))
        self.repository.register_review_queue(queue, object_hash=queue_object.sha256)
        self.state.register_artifact(
            artifact_id=f"DistillationReviewQueue:{queue.queue_id}",
            artifact_type="DistillationReviewQueue",
            schema_version=queue.schema_version,
            object_hash=queue_object.sha256,
            input_hashes=[content_hash(unit_ids)],
        )
        return queue

    def _persist_book_reports(
        self,
        run: DistillationRun,
        contexts: tuple[_ManifestContext, ...],
        units: list[DistillationUnit],
    ) -> tuple[list[str], list[str]]:
        existing_cleaning, existing_methods = self.repository.book_report_ids_for_run(run.run_id)
        if len(existing_cleaning) == len(contexts) and len(existing_methods) == len(contexts):
            return existing_cleaning, existing_methods
        cleaning_ids: list[str] = []
        method_ids: list[str] = []
        assert run.finished_at is not None
        for context in contexts:
            manifest_units = [
                unit for unit in units if unit.source_id == context.manifest.source_id
            ]
            canonical_units = [
                unit for unit in manifest_units if unit.duplicate_of_unit_id is None
            ]
            cleaning_identity = {
                "run_id": run.run_id,
                "manifest_id": context.manifest.manifest_id,
            }
            cleaning_id = f"book-cleaning:{content_hash(cleaning_identity)}"
            cleaning = BookCleaningReport(
                report_id=cleaning_id,
                manifest_id=context.manifest.manifest_id,
                input_parse_report_ids=[context.parse_report_id],
                cleaning_pipeline_version=run.classification_rule_version,
                processing_status=BookProcessingStatus.COMPLETE,
                original_page_count=context.original_page_count,
                original_char_count=context.original_char_count,
                successfully_parsed_page_count=context.successfully_parsed_page_count,
                ocr_page_count=context.ocr_page_count,
                duplicate_paragraph_count=sum(
                    unit.duplicate_of_unit_id is not None for unit in manifest_units
                ),
                downweight_or_remove_candidate_count=sum(
                    unit.decision is DistillationDecision.DOWNWEIGHT_CANDIDATE
                    for unit in manifest_units
                ),
                methodology_paragraph_count=sum(
                    bool(unit.method_categories) for unit in canonical_units
                ),
                case_paragraph_count=sum(
                    BookContentClass.FAILURE_CASE in unit.content_classes
                    for unit in canonical_units
                ),
                unclassified_paragraph_count=sum(
                    unit.decision is DistillationDecision.UNCLASSIFIED
                    for unit in canonical_units
                ),
                human_review_status=HumanReviewStatus.PENDING,
                downweight_classes=list(BOOK_DOWNWEIGHT_CLASSES),
                keep_classes=list(BOOK_KEEP_CLASSES),
                created_at=run.finished_at,
            )
            cleaning_object = self.object_store.put_json(cleaning.model_dump(mode="json"))
            self.repository.register_book_cleaning_report(
                cleaning,
                run_id=run.run_id,
                object_hash=cleaning_object.sha256,
            )
            self.state.register_artifact(
                artifact_id=f"BookCleaningReport:{cleaning.report_id}",
                artifact_type="BookCleaningReport",
                schema_version=cleaning.schema_version,
                object_hash=cleaning_object.sha256,
                input_hashes=[context.manifest.file_sha256, context.parse_report_id],
            )
            method = _book_method_report(run, context, cleaning, canonical_units)
            method_object = self.object_store.put_json(method.model_dump(mode="json"))
            self.repository.register_book_method_coverage_report(
                method,
                run_id=run.run_id,
                object_hash=method_object.sha256,
            )
            self.state.register_artifact(
                artifact_id=f"BookMethodCoverageReport:{method.report_id}",
                artifact_type="BookMethodCoverageReport",
                schema_version=method.schema_version,
                object_hash=method_object.sha256,
                input_hashes=[cleaning_object.sha256],
            )
            cleaning_ids.append(cleaning.report_id)
            method_ids.append(method.report_id)
        return cleaning_ids, method_ids

    def _persist_author_report(
        self,
        run: DistillationRun,
        bundle: _InputBundle,
        units: list[DistillationUnit],
        queue: DistillationReviewQueue,
        parquet_file: Path,
    ) -> AuthorDistillationReport:
        existing = self.repository.author_report_for_run(run.run_id)
        if existing is not None:
            return existing
        assert run.finished_at is not None
        canonical_units = [unit for unit in units if unit.duplicate_of_unit_id is None]
        content_counts = Counter(
            content_class.value
            for unit in canonical_units
            for content_class in unit.content_classes
        )
        method_counts = Counter(
            method.value
            for unit in canonical_units
            for method in unit.method_categories
        )
        report_identity = {
            "run_id": run.run_id,
            "queue_id": queue.queue_id,
            "parquet_name": parquet_file.name,
        }
        report = AuthorDistillationReport(
            report_id=f"author-distillation:{content_hash(report_identity)}",
            run_id=run.run_id,
            author_source_id=run.author_source_id,
            input_source_ids=run.input_source_ids,
            local_manifest_ids=[item.manifest.manifest_id for item in bundle.manifests],
            input_source_item_count=len(bundle.items),
            empty_source_item_count=run.empty_source_item_count,
            unit_count=len(units),
            canonical_unit_count=len(canonical_units),
            duplicate_unit_count=len(units) - len(canonical_units),
            keep_candidate_count=sum(
                unit.decision is DistillationDecision.KEEP_CANDIDATE for unit in units
            ),
            downweight_candidate_count=sum(
                unit.decision is DistillationDecision.DOWNWEIGHT_CANDIDATE
                for unit in units
            ),
            unclassified_count=sum(
                unit.decision is DistillationDecision.UNCLASSIFIED for unit in units
            ),
            content_class_counts=dict(sorted(content_counts.items())),
            method_category_counts=dict(sorted(method_counts.items())),
            online_content_count=bundle.online_content_count,
            target_author_comment_count=bundle.target_author_comment_count,
            open_collection_gap_count=bundle.open_collection_gap_count,
            missing_object_count=bundle.missing_object_count,
            coverage_status=(
                CoverageStatus.PARTIAL
                if bundle.missing_object_count
                else bundle.upstream_coverage_status
            ),
            human_review_status=HumanReviewStatus.PENDING,
            review_queue_id=queue.queue_id,
            parquet_object_count=1,
            created_at=run.finished_at,
        )
        report_object = self.object_store.put_json(report.model_dump(mode="json"))
        self.repository.register_author_report(report, object_hash=report_object.sha256)
        self.state.register_artifact(
            artifact_id=f"AuthorDistillationReport:{report.report_id}",
            artifact_type="AuthorDistillationReport",
            schema_version=report.schema_version,
            object_hash=report_object.sha256,
            input_hashes=[content_hash([unit.unit_id for unit in units]), queue.queue_id],
        )
        return report

    def _open_gap_count(self, author_source_id: str) -> int:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM collection_gap g "
                "JOIN collection_scope s ON s.scope_id=g.scope_id "
                "WHERE s.author_id=? AND g.status='OPEN'",
                (author_source_id,),
            ).fetchone()
        return int(row[0])

    def _upstream_coverage(
        self,
        source: KnowledgeSourceDefinition,
        open_gaps: int,
    ) -> CoverageStatus:
        if not source.online_collection_required:
            return CoverageStatus.COMPLETE
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM knowledge_coverage_audit_report "
                "ORDER BY audited_at DESC,report_id DESC LIMIT 1"
            ).fetchone()
        if row is not None:
            payload = json.loads(str(row["report_json"]))
            source_report = next(
                (
                    item
                    for item in payload.get("source_reports", [])
                    if item.get("source_id") == source.source_id
                ),
                None,
            )
            if source_report is not None:
                status = source_report.get("status")
                if status == KnowledgeAuditStatus.PASS.value:
                    return CoverageStatus.COMPLETE
                if status == KnowledgeAuditStatus.ACCESS_RESTRICTED.value:
                    return CoverageStatus.ACCESS_RESTRICTED
                return CoverageStatus.PARTIAL
        if open_gaps or source.online_collection_required:
            return CoverageStatus.PARTIAL
        return CoverageStatus.COMPLETE

    @staticmethod
    def _run_id(author_source_id: str, rule_version: str, input_hashes: tuple[str, ...]) -> str:
        identity = {
            "author_source_id": author_source_id,
            "classification_rule_version": rule_version,
            "input_hashes": list(input_hashes),
        }
        return f"knowledge-distillation:{content_hash(identity)}"


def _source_item_sort_key(item: _SourceItem) -> tuple[Any, ...]:
    locator_order = {
        DistillationLocatorType.PAGE_TEXT: 0,
        DistillationLocatorType.BLOCK_TEXT: 1,
        DistillationLocatorType.ZHIHU_CONTENT: 2,
        DistillationLocatorType.ZHIHU_COMMENT: 3,
    }
    return (
        item.source_id,
        locator_order[item.locator_type],
        item.page_number or item.block_index or 0,
        item.content_id or "",
        item.comment_id or "",
        item.source_unit_id,
    )


def _segments(item: _SourceItem) -> list[tuple[str, int, int]]:
    if item.raw_text is None or not item.raw_text:
        return []
    if item.locator_type is DistillationLocatorType.PAGE_TEXT:
        segments: list[tuple[str, int, int]] = []
        for match in _LINE.finditer(item.raw_text):
            normalized = _normalize_text(match.group(0), strip_html=False)
            if normalized:
                segments.append((normalized, match.start(), match.end()))
        return segments
    raw = item.raw_text
    normalized = (
        _normalize_zhihu_body(raw)
        if item.locator_type
        in {DistillationLocatorType.ZHIHU_CONTENT, DistillationLocatorType.ZHIHU_COMMENT}
        else _normalize_text(raw, strip_html=False)
    )
    return [(normalized, 0, len(raw))] if normalized else []


def _normalize_zhihu_body(value: str) -> str:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = value
    if isinstance(decoded, (dict, list)):
        strings = list(_string_leaves(decoded))
        value = " ".join(strings)
    elif isinstance(decoded, str):
        value = decoded
    return _normalize_text(value, strip_html=True)


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [child for item in value for child in _string_leaves(item)]
    if isinstance(value, dict):
        return [
            child
            for key in sorted(value)
            for child in _string_leaves(value[key])
        ]
    return []


def _normalize_text(value: str, *, strip_html: bool) -> str:
    if strip_html:
        value = _HTML_TAG.sub(" ", html.unescape(value))
    value = unicodedata.normalize("NFKC", value)
    value = _ZERO_WIDTH.sub("", value)
    return _WHITESPACE.sub(" ", value).strip()


def _classify(
    text: str,
    rules: DistillationClassRuleSet,
    *,
    duplicate_of_unit_id: str | None,
) -> tuple[
    list[BookContentClass],
    list[BookMethodCategory],
    DistillationDecision,
    list[str],
    dict[str, float],
]:
    folded = text.casefold()
    scores: dict[str, float] = {}
    content_classes: list[BookContentClass] = []
    for content_class in sorted(rules.content_class_terms, key=lambda item: item.value):
        terms = rules.content_class_terms[content_class]
        matches = sum(term.casefold() in folded for term in terms)
        if matches:
            content_classes.append(content_class)
            scores[content_class.value] = round(matches / len(terms), 6)
    reasons: list[str] = []
    if duplicate_of_unit_id is not None:
        if BookContentClass.REPETITION_WITHOUT_NEW_INFORMATION not in content_classes:
            content_classes.append(BookContentClass.REPETITION_WITHOUT_NEW_INFORMATION)
        decision = DistillationDecision.DOWNWEIGHT_CANDIDATE
        reasons.append("EXACT_DUPLICATE")
    elif len(text) < rules.minimum_unit_char_count:
        decision = DistillationDecision.DOWNWEIGHT_CANDIDATE
        reasons.append("BELOW_MINIMUM_LENGTH")
    elif any(item in BOOK_KEEP_CLASSES for item in content_classes):
        decision = DistillationDecision.KEEP_CANDIDATE
        reasons.append("METHOD_CLASS_RULE_MATCH")
    elif any(item in BOOK_DOWNWEIGHT_CLASSES for item in content_classes):
        decision = DistillationDecision.DOWNWEIGHT_CANDIDATE
        reasons.append("DOWNWEIGHT_CLASS_RULE_MATCH")
    else:
        decision = DistillationDecision.UNCLASSIFIED
        reasons.append("NO_CLASS_RULE_MATCH")
    methods = sorted(
        {_METHOD_BY_CLASS[item] for item in content_classes if item in _METHOD_BY_CLASS},
        key=lambda item: item.value,
    )
    return (
        sorted(set(content_classes), key=lambda item: item.value),
        methods,
        decision,
        sorted(reasons),
        dict(sorted(scores.items())),
    )


def _book_method_report(
    run: DistillationRun,
    context: _ManifestContext,
    cleaning: BookCleaningReport,
    units: list[DistillationUnit],
) -> BookMethodCoverageReport:
    assert run.finished_at is not None
    finished_at = run.finished_at
    groups = {
        "selection": {
            BookMethodCategory.STOCK_SELECTION,
            BookMethodCategory.BUSINESS_MODEL,
            BookMethodCategory.INDUSTRY,
            BookMethodCategory.VALUATION,
            BookMethodCategory.FINANCIAL_QUALITY,
        },
        "entry": {BookMethodCategory.ENTRY},
        "holding": {BookMethodCategory.HOLDING},
        "add": {BookMethodCategory.ADD},
        "trim": {BookMethodCategory.TRIM},
        "exit": {BookMethodCategory.EXIT},
        "risk": {
            BookMethodCategory.RISK,
            BookMethodCategory.FAILURE_CASE,
            BookMethodCategory.COUNTEREVIDENCE_INVALIDATION,
        },
        "review": {BookMethodCategory.REVIEW},
    }

    def metric(categories: set[BookMethodCategory]) -> BookMethodCoverageMetric:
        count = sum(bool(set(unit.method_categories) & categories) for unit in units)
        return BookMethodCoverageMetric(
            paragraph_count=count,
            evidence_count=count,
            status=(CoverageStatus.PARTIAL if count else CoverageStatus.INSUFFICIENT_SOURCE),
            created_at=finished_at,
        )

    report_identity = {
        "run_id": run.run_id,
        "manifest_id": context.manifest.manifest_id,
    }
    return BookMethodCoverageReport(
        report_id=f"book-method-coverage:{content_hash(report_identity)}",
        manifest_id=context.manifest.manifest_id,
        input_cleaning_report_id=cleaning.report_id,
        processing_status=BookProcessingStatus.COMPLETE,
        human_review_status=HumanReviewStatus.PENDING,
        selection=metric(groups["selection"]),
        entry=metric(groups["entry"]),
        holding=metric(groups["holding"]),
        add=metric(groups["add"]),
        trim=metric(groups["trim"]),
        exit=metric(groups["exit"]),
        risk=metric(groups["risk"]),
        review=metric(groups["review"]),
        created_at=finished_at,
    )


__all__ = ["DistillationExecution", "KnowledgeDistillationService"]
