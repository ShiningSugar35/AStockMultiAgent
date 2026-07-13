"""Durable metadata cache for versioned PDF page parsing."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.state import StateStore, utc_now_text
from astock.schemas import DocumentPage, DocumentParseReport


class DocumentPageRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_page(
        self,
        snapshot_id: str,
        page_number: int,
        parser_version: str,
    ) -> DocumentPage | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT page_json FROM document_page WHERE snapshot_id=? AND page_number=? "
                "AND parser_version=?",
                (snapshot_id, page_number, parser_version),
            ).fetchone()
        return DocumentPage.model_validate_json(row["page_json"]) if row else None

    def get_page_by_id(self, page_id: str) -> DocumentPage | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT page_json FROM document_page WHERE page_id=?", (page_id,)
            ).fetchone()
        return DocumentPage.model_validate_json(row["page_json"]) if row else None

    def register_page(self, page: DocumentPage) -> None:
        page_json = canonical_json_bytes(page.model_dump(mode="json")).decode("utf-8")
        manifest_hash = content_hash(page)
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT page_manifest_hash FROM document_page WHERE snapshot_id=? "
                "AND page_number=? AND parser_version=?",
                (page.snapshot_id, page.page_number, page.parser_version),
            ).fetchone()
            if existing is not None:
                if existing["page_manifest_hash"] != manifest_hash:
                    raise ValueError(
                        f"Page parse collision: {page.snapshot_id}:{page.page_number}:"
                        f"{page.parser_version}"
                    )
                return
            connection.execute(
                "INSERT INTO document_page(page_id,document_id,snapshot_id,page_number,"
                "parser_version,extraction_method,text_object_hash,text_sha256,text_char_count,"
                "page_manifest_hash,page_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    page.page_id,
                    page.document_id,
                    page.snapshot_id,
                    page.page_number,
                    page.parser_version,
                    page.extraction_method.value,
                    page.text_object_sha256,
                    page.text_sha256,
                    page.text_char_count,
                    manifest_hash,
                    page_json,
                    utc_now_text(),
                ),
            )

    def get_report(
        self,
        snapshot_id: str,
        parser_version: str,
        page_scope_hash: str,
    ) -> DocumentParseReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM document_parse_run WHERE snapshot_id=? "
                "AND parser_version=? AND page_scope_hash=?",
                (snapshot_id, parser_version, page_scope_hash),
            ).fetchone()
        return DocumentParseReport.model_validate_json(row["report_json"]) if row else None

    def register_report(
        self,
        report: DocumentParseReport,
        *,
        page_scope_hash: str,
        started_at: str,
        finished_at: str,
    ) -> None:
        report_json = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT report_object_hash FROM document_parse_run WHERE parse_run_id=?",
                (report.parse_run_id,),
            ).fetchone()
            if existing is not None:
                if existing["report_object_hash"] != report.report_object_sha256:
                    raise ValueError(f"Parse report collision: {report.parse_run_id}")
                return
            connection.execute(
                "INSERT INTO document_parse_run(parse_run_id,document_id,snapshot_id,"
                "parser_version,page_scope_hash,status,report_object_hash,report_json,started_at,"
                "finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    report.parse_run_id,
                    report.document_id,
                    report.snapshot_id,
                    report.parser_version,
                    page_scope_hash,
                    report.parse_status.value,
                    report.report_object_sha256,
                    report_json,
                    started_at,
                    finished_at,
                ),
            )

    def page_rows(self, document_id: str) -> list[dict[str, object]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT page_number,parser_version,extraction_method,text_object_hash,"
                "text_char_count FROM document_page WHERE document_id=? "
                "ORDER BY page_number,parser_version",
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]
