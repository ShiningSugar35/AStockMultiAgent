"""Private-safe structural profiles for source-specific knowledge processing."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime

from astock.books import PrivateDocxRepository
from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentBlockRepository
from astock.knowledge.distillation import (
    _docx_segments,
    _looks_like_pdf_heading,
    _pdf_segments,
    _zhihu_segments,
)
from astock.schemas import (
    BookParseReport,
    BookParseScope,
    BookProcessingStatus,
    BookSourceManifest,
    CoverageStatus,
    DocumentBlockKind,
    DocumentType,
    HumanReviewStatus,
    KnowledgeMaterialKind,
    KnowledgeProcessingStrategy,
    KnowledgeSourceDefinition,
    KnowledgeSourceStructureProfile,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
)

_NONEMPTY_LINE = re.compile(r"[^\r\n]+")
_HTML_HEADING = re.compile(r"<h[1-6](?:\s[^>]*)?>", re.IGNORECASE)
_ZHIHU_SCOPE_POLICY_VERSION = "ZHIHU_BODY_ONLY_V1"


class KnowledgeStructureRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get(self, profile_id: str) -> KnowledgeSourceStructureProfile | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT profile_json FROM knowledge_structure_profile WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        return (
            KnowledgeSourceStructureProfile.model_validate_json(row["profile_json"])
            if row
            else None
        )

    def register(
        self,
        profile: KnowledgeSourceStructureProfile,
        *,
        object_hash: str,
    ) -> KnowledgeSourceStructureProfile:
        payload = canonical_json_bytes(profile.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT profile_json,profile_object_hash FROM knowledge_structure_profile "
                "WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()
            if row is not None:
                if str(row["profile_json"]) != payload or row["profile_object_hash"] != object_hash:
                    raise ValueError(f"knowledge structure profile collision: {profile.profile_id}")
                return KnowledgeSourceStructureProfile.model_validate_json(row["profile_json"])
            connection.execute(
                "INSERT INTO knowledge_structure_profile("
                "profile_id,author_source_id,input_source_id,material_kind,"
                "processing_strategy,input_set_hash,source_item_count,"
                "semantic_segment_count,coverage_status,human_review_status,"
                "profile_object_hash,profile_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile.profile_id,
                    profile.author_source_id,
                    profile.input_source_id,
                    profile.material_kind.value,
                    profile.processing_strategy.value,
                    profile.input_set_sha256,
                    profile.source_item_count,
                    profile.semantic_segment_count,
                    profile.coverage_status.value,
                    profile.human_review_status.value,
                    object_hash,
                    payload,
                    profile.created_at.isoformat(),
                ),
            )
        return profile

    def latest_for_author(self, author_source_id: str) -> list[KnowledgeSourceStructureProfile]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT profile_json FROM knowledge_structure_profile "
                "WHERE author_source_id=? ORDER BY created_at DESC,profile_id DESC",
                (author_source_id,),
            ).fetchall()
        latest: dict[tuple[str, str], KnowledgeSourceStructureProfile] = {}
        for row in rows:
            profile = KnowledgeSourceStructureProfile.model_validate_json(row["profile_json"])
            latest.setdefault((profile.input_source_id, profile.material_kind.value), profile)
        return [latest[key] for key in sorted(latest)]

    def object_hash(self, profile_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT profile_object_hash FROM knowledge_structure_profile WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        return str(row["profile_object_hash"]) if row else None


class KnowledgeStructureProfileService:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store
        self.repository = KnowledgeStructureRepository(state)

    def analyze(
        self,
        source: KnowledgeSourceDefinition,
    ) -> list[KnowledgeSourceStructureProfile]:
        profiles = self._build_profiles(source)
        stored: list[KnowledgeSourceStructureProfile] = []
        for profile in profiles:
            payload = self.object_store.put_json(profile.model_dump(mode="json"))
            registered = self.repository.register(profile, object_hash=payload.sha256)
            self.state.register_artifact(
                artifact_id=f"KnowledgeSourceStructureProfile:{registered.profile_id}",
                artifact_type="KnowledgeSourceStructureProfile",
                schema_version=registered.schema_version,
                object_hash=payload.sha256,
                input_hashes=[registered.input_set_sha256],
            )
            stored.append(registered)
        return stored

    def status(self, author_source_id: str) -> dict[str, object]:
        profiles = self.repository.latest_for_author(author_source_id)
        return {
            "status": "NOT_RUN" if not profiles else "PENDING_REVIEW",
            "author_source_id": author_source_id,
            "profile_count": len(profiles),
            "profiles": profiles,
        }

    def audit(self, source: KnowledgeSourceDefinition) -> dict[str, object]:
        expected = self._build_profiles(source)
        stored = {
            profile.profile_id: profile
            for profile in self.repository.latest_for_author(source.source_id)
        }
        missing_current = sum(profile.profile_id not in stored for profile in expected)
        missing_objects = 0
        invalid_objects = 0
        artifact_mismatches = 0
        for profile in expected:
            persisted = stored.get(profile.profile_id)
            if persisted is None:
                continue
            object_hash = self.repository.object_hash(profile.profile_id)
            if object_hash is None or not self.object_store.verify(object_hash):
                missing_objects += 1
                continue
            try:
                object_profile = KnowledgeSourceStructureProfile.model_validate_json(
                    self.object_store.get_bytes(object_hash)
                )
            except (StorageError, ValueError):
                invalid_objects += 1
                continue
            if object_profile != persisted:
                invalid_objects += 1
            with self.state.connect() as connection:
                row = connection.execute(
                    "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                    (f"KnowledgeSourceStructureProfile:{profile.profile_id}",),
                ).fetchone()
            if row is None or row["object_hash"] != object_hash:
                artifact_mismatches += 1
        findings = {
            "CURRENT_STRUCTURE_PROFILE_MISSING": missing_current,
            "STRUCTURE_PROFILE_OBJECT_MISSING": missing_objects,
            "STRUCTURE_PROFILE_OBJECT_INVALID": invalid_objects,
            "STRUCTURE_PROFILE_ARTIFACT_MISMATCH": artifact_mismatches,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "author_source_id": source.source_id,
            "expected_profile_count": len(expected),
            "stored_current_profile_count": len(stored),
            "missing_current_profile_count": missing_current,
            "missing_object_count": missing_objects,
            "invalid_object_count": invalid_objects,
            "artifact_mismatch_count": artifact_mismatches,
            "coverage_statuses": sorted({item.coverage_status.value for item in expected}),
            "finding_codes": finding_codes,
        }

    def _build_profiles(
        self,
        source: KnowledgeSourceDefinition,
    ) -> list[KnowledgeSourceStructureProfile]:
        profiles: list[KnowledgeSourceStructureProfile] = []
        for manifest in self._manifests(source.source_id):
            profile = (
                self._docx_profile(source, manifest)
                if manifest.document_type is DocumentType.PRIVATE_DOCX
                else self._pdf_profile(source, manifest)
            )
            profiles.append(profile)
        if source.online_collection_required:
            profiles.append(self._online_profile(source))
        return sorted(profiles, key=lambda item: (item.input_source_id, item.material_kind.value))

    def _manifests(self, author_source_id: str) -> list[BookSourceManifest]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM book_source_manifest ORDER BY source_id,file_version"
            ).fetchall()
        manifests = [BookSourceManifest.model_validate_json(row["manifest_json"]) for row in rows]
        return [item for item in manifests if item.author_source_id == author_source_id]

    def _pdf_profile(
        self,
        source: KnowledgeSourceDefinition,
        manifest: BookSourceManifest,
    ) -> KnowledgeSourceStructureProfile:
        report = self._pdf_report(manifest.manifest_id)
        if report is None:
            raise ValueError(f"full PDF parse report is unavailable: {manifest.manifest_id}")
        input_hashes = {manifest.file_sha256}
        lengths: list[int] = []
        line_count = 0
        heading_count = 0
        segment_count = 0
        semantic_empty = 0
        zero_length = 0
        for page in report.pages:
            input_hashes.add(page.text_object_sha256)
            if page.text_char_count == 0:
                zero_length += 1
            raw = self._text(page.text_object_sha256)
            page_segments = _pdf_segments(raw) if raw is not None else []
            if not page_segments:
                semantic_empty += 1
            segment_count += len(page_segments)
            if raw is None:
                continue
            for match in _NONEMPTY_LINE.finditer(raw):
                text = match.group(0).strip()
                if not text:
                    continue
                lengths.append(len(text))
                line_count += 1
                heading_count += int(_looks_like_pdf_heading(text))
        return self._profile(
            source=source,
            input_source_id=manifest.source_id,
            material_kind=KnowledgeMaterialKind.PRIVATE_PDF,
            strategy=KnowledgeProcessingStrategy.PDF_PAGE_WRAPPED_PARAGRAPH_V1,
            input_hashes=input_hashes,
            source_item_count=len(report.pages),
            zero_length_source_item_count=zero_length,
            semantic_empty_source_item_count=semantic_empty,
            structure_unit_count=line_count,
            semantic_segment_count=segment_count,
            page_count=len(report.pages),
            block_count=0,
            heading_count=heading_count,
            table_cell_block_count=0,
            verified_content_count=0,
            target_author_comment_count=0,
            content_type_counts={},
            lengths=lengths,
            actions=[
                "MERGE_PDF_LAYOUT_WRAPS_WITHIN_PAGE",
                "PRESERVE_PAGE_CHAR_RANGE",
                "NEVER_MERGE_ACROSS_PAGES",
            ],
            coverage_status=CoverageStatus.COMPLETE,
        )

    def _docx_profile(
        self,
        source: KnowledgeSourceDefinition,
        manifest: BookSourceManifest,
    ) -> KnowledgeSourceStructureProfile:
        report = PrivateDocxRepository(self.state).latest_parse_report_for_manifest(
            manifest.manifest_id
        )
        if report is None:
            raise ValueError(f"DOCX parse report is unavailable: {manifest.manifest_id}")
        blocks = DocumentBlockRepository(self.state).blocks_for(
            report.snapshot_id,
            report.parser_version,
        )
        input_hashes = {manifest.file_sha256, report.block_set_sha256}
        lengths = [block.text_char_count for block in blocks if block.text_char_count]
        segment_count = 0
        semantic_empty = 0
        for block in blocks:
            input_hashes.update(
                {block.text_object_sha256, block.metadata_object_sha256}
            )
            raw = self._text(block.text_object_sha256)
            segments = _docx_segments(raw) if raw is not None else []
            segment_count += len(segments)
            semantic_empty += int(not segments)
        return self._profile(
            source=source,
            input_source_id=manifest.source_id,
            material_kind=KnowledgeMaterialKind.PRIVATE_DOCX,
            strategy=KnowledgeProcessingStrategy.DOCX_STABLE_BLOCK_V1,
            input_hashes=input_hashes,
            source_item_count=len(blocks),
            zero_length_source_item_count=sum(block.text_char_count == 0 for block in blocks),
            semantic_empty_source_item_count=semantic_empty,
            structure_unit_count=len(blocks),
            semantic_segment_count=segment_count,
            page_count=0,
            block_count=len(blocks),
            heading_count=sum(block.is_heading for block in blocks),
            table_cell_block_count=sum(
                block.block_kind is DocumentBlockKind.TABLE_CELL_PARAGRAPH for block in blocks
            ),
            verified_content_count=0,
            target_author_comment_count=0,
            content_type_counts={},
            lengths=lengths,
            actions=[
                "PRESERVE_DOCX_BLOCK_IDENTITY",
                "PRESERVE_HEADING_BOUNDARIES",
                "SPLIT_ONLY_OVERSIZED_BLOCKS",
                "EXCLUDE_SEMANTIC_EMPTY_BLOCKS",
            ],
            coverage_status=CoverageStatus.COMPLETE,
        )

    def _online_profile(
        self,
        source: KnowledgeSourceDefinition,
    ) -> KnowledgeSourceStructureProfile:
        contents = self._latest_content(
            source.source_id,
            frozenset(source.collection_scope.content_types),
        )
        input_hashes: set[str] = set()
        lengths: list[int] = []
        segment_count = 0
        heading_count = 0
        zero_length = 0
        semantic_empty = 0
        content_counts = Counter(record.content_type.value for record in contents)
        for record in contents:
            input_hashes.update({record.body_object_sha256, record.metadata_sha256})
            raw = self._text(record.body_object_sha256)
            if raw is None or not raw:
                zero_length += 1
            segments = _zhihu_segments(raw) if raw is not None else []
            if not segments:
                semantic_empty += 1
            segment_count += len(segments)
            lengths.extend(len(text) for text, _, _ in segments)
            html_body = _content_html(raw) if raw is not None else ""
            heading_count += len(_HTML_HEADING.findall(html_body))
        return self._profile(
            source=source,
            input_source_id=source.source_id,
            material_kind=KnowledgeMaterialKind.ZHIHU_ONLINE,
            strategy=KnowledgeProcessingStrategy.ZHIHU_VERIFIED_VISIBLE_HTML_V2,
            input_hashes=input_hashes,
            source_item_count=len(contents),
            zero_length_source_item_count=zero_length,
            semantic_empty_source_item_count=semantic_empty,
            structure_unit_count=segment_count,
            semantic_segment_count=segment_count,
            page_count=0,
            block_count=0,
            heading_count=heading_count,
            table_cell_block_count=0,
            verified_content_count=len(contents),
            target_author_comment_count=0,
            content_type_counts=dict(sorted(content_counts.items())),
            lengths=lengths,
            actions=[
                _ZHIHU_SCOPE_POLICY_VERSION,
                "REQUIRE_DETAIL_VERIFIED",
                "SPLIT_VISIBLE_HTML_BLOCKS",
                "USE_THOUGHT_CONTENT_HTML_ONCE",
            ],
            coverage_status=self._online_coverage(source.source_id),
        )

    def _profile(
        self,
        *,
        source: KnowledgeSourceDefinition,
        input_source_id: str,
        material_kind: KnowledgeMaterialKind,
        strategy: KnowledgeProcessingStrategy,
        input_hashes: set[str],
        source_item_count: int,
        zero_length_source_item_count: int,
        semantic_empty_source_item_count: int,
        structure_unit_count: int,
        semantic_segment_count: int,
        page_count: int,
        block_count: int,
        heading_count: int,
        table_cell_block_count: int,
        verified_content_count: int,
        target_author_comment_count: int,
        content_type_counts: dict[str, int],
        lengths: list[int],
        actions: list[str],
        coverage_status: CoverageStatus,
    ) -> KnowledgeSourceStructureProfile:
        input_set_hash = content_hash(sorted(input_hashes))
        identity: dict[str, object] = {
            "author_source_id": source.source_id,
            "input_source_id": input_source_id,
            "material_kind": material_kind.value,
            "processing_strategy": strategy.value,
            "input_set_sha256": input_set_hash,
        }
        if material_kind is KnowledgeMaterialKind.ZHIHU_ONLINE:
            identity.update(
                {
                    "scope_policy_version": _ZHIHU_SCOPE_POLICY_VERSION,
                    "active_content_types": sorted(source.collection_scope.content_types),
                }
            )
        profile_id = f"knowledge-structure:{content_hash(identity)}"
        existing = self.repository.get(profile_id)
        if existing is not None:
            return existing
        return KnowledgeSourceStructureProfile(
            profile_id=profile_id,
            author_source_id=source.source_id,
            input_source_id=input_source_id,
            material_kind=material_kind,
            processing_strategy=strategy,
            input_set_sha256=input_set_hash,
            input_object_count=len(input_hashes),
            source_item_count=source_item_count,
            zero_length_source_item_count=zero_length_source_item_count,
            semantic_empty_source_item_count=semantic_empty_source_item_count,
            structure_unit_count=structure_unit_count,
            semantic_segment_count=semantic_segment_count,
            page_count=page_count,
            block_count=block_count,
            heading_count=heading_count,
            table_cell_block_count=table_cell_block_count,
            verified_content_count=verified_content_count,
            target_author_comment_count=target_author_comment_count,
            content_type_counts=content_type_counts,
            char_count_p50=_quantile(lengths, 0.5),
            char_count_p90=_quantile(lengths, 0.9),
            char_count_max=max(lengths, default=0),
            recommended_action_codes=actions,
            coverage_status=coverage_status,
            human_review_status=HumanReviewStatus.PENDING,
            created_at=datetime.now(UTC),
        )

    def _pdf_report(self, manifest_id: str) -> BookParseReport | None:
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

    def _latest_content(
        self,
        source_id: str,
        active_content_types: frozenset[str],
    ) -> list[ZhihuContentRecord]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM zhihu_content_version WHERE source_id=? "
                "ORDER BY collected_at,version_id",
                (source_id,),
            ).fetchall()
        latest: dict[tuple[str, str], ZhihuContentRecord] = {}
        for row in rows:
            record = ZhihuContentRecord.model_validate_json(row["record_json"])
            if (
                record.content_type.value in active_content_types
                and record.content_completeness is ZhihuContentCompleteness.DETAIL_VERIFIED
            ):
                latest[(record.content_type.value, record.content_id)] = record
        return [latest[key] for key in sorted(latest)]

    def _online_coverage(self, source_id: str) -> CoverageStatus:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM knowledge_coverage_audit_report "
                "ORDER BY audited_at DESC,report_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return CoverageStatus.PARTIAL
        payload = json.loads(str(row["report_json"]))
        report = next(
            (
                item
                for item in payload.get("source_reports", [])
                if item.get("source_id") == source_id
            ),
            None,
        )
        if report is None:
            return CoverageStatus.PARTIAL
        status = report.get("status")
        if status == "ACCESS_RESTRICTED":
            return CoverageStatus.ACCESS_RESTRICTED
        if status in {"PASS", "COMPLETE"}:
            return CoverageStatus.COMPLETE
        return CoverageStatus.PARTIAL

    def _text(self, object_hash: str) -> str | None:
        try:
            return self.object_store.get_bytes(object_hash).decode("utf-8")
        except (StorageError, UnicodeDecodeError):
            return None


def _content_html(raw: str) -> str:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, dict) and isinstance(decoded.get("content_html"), str):
        return str(decoded["content_html"])
    return ""


def _quantile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


__all__ = ["KnowledgeStructureProfileService", "KnowledgeStructureRepository"]
