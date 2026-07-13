"""SQLite metadata repository for private-book manifests and parse reports."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas import BookParseReport, BookSourceManifest


class BookRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_manifest(self, manifest_id: str) -> BookSourceManifest | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM book_source_manifest WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()
        return BookSourceManifest.model_validate_json(row["manifest_json"]) if row else None

    def get_manifest_version(self, source_id: str, file_version: str) -> BookSourceManifest | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM book_source_manifest "
                "WHERE source_id=? AND file_version=?",
                (source_id, file_version),
            ).fetchone()
        return BookSourceManifest.model_validate_json(row["manifest_json"]) if row else None

    def register_manifest(self, manifest: BookSourceManifest) -> BookSourceManifest:
        serialized = canonical_json_bytes(manifest.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT manifest_id,manifest_json FROM book_source_manifest "
                "WHERE source_id=? AND file_version=?",
                (manifest.source_id, manifest.file_version),
            ).fetchone()
            if existing is not None:
                if existing["manifest_id"] != manifest.manifest_id:
                    raise ValueError(
                        "Book source version collision: "
                        f"{manifest.source_id}:{manifest.file_version}"
                    )
                return BookSourceManifest.model_validate_json(existing["manifest_json"])
            connection.execute(
                "INSERT INTO book_source_manifest(manifest_id,source_id,document_id,snapshot_id,"
                "pit_id,file_sha256,file_version,source_page_count,manifest_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest.manifest_id,
                    manifest.source_id,
                    manifest.document_id,
                    manifest.snapshot_id,
                    manifest.pit_id,
                    manifest.file_sha256,
                    manifest.file_version,
                    manifest.source_page_count,
                    serialized,
                    manifest.created_at.isoformat(),
                ),
            )
        return manifest

    def get_parse_report(self, report_id: str) -> BookParseReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM book_parse_report WHERE book_parse_report_id=?",
                (report_id,),
            ).fetchone()
        return BookParseReport.model_validate_json(row["report_json"]) if row else None

    def register_parse_report(self, report: BookParseReport) -> BookParseReport:
        if report.report_object_sha256 is None:
            raise ValueError("BookParseReport must be stored in ObjectStore first")
        serialized = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT report_object_hash,report_json FROM book_parse_report "
                "WHERE book_parse_report_id=?",
                (report.book_parse_report_id,),
            ).fetchone()
            if existing is not None:
                if existing["report_object_hash"] != report.report_object_sha256:
                    raise ValueError(
                        f"Book parse report collision: {report.book_parse_report_id}"
                    )
                return BookParseReport.model_validate_json(existing["report_json"])
            connection.execute(
                "INSERT INTO book_parse_report(book_parse_report_id,manifest_id,parser_version,"
                "parse_scope,report_object_hash,report_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    report.book_parse_report_id,
                    report.manifest_id,
                    report.parser_version,
                    report.parse_scope.value,
                    report.report_object_sha256,
                    serialized,
                    report.created_at.isoformat(),
                ),
            )
        return report
