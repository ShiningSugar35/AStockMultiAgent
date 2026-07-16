"""SQLite metadata repository for private DOCX parse reports."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas import PrivateDocxParseReport


class PrivateDocxRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_parse_report(self, report_id: str) -> PrivateDocxParseReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM private_docx_parse_report "
                "WHERE docx_parse_report_id=?",
                (report_id,),
            ).fetchone()
        return PrivateDocxParseReport.model_validate_json(row["report_json"]) if row else None

    def register_parse_report(self, report: PrivateDocxParseReport) -> PrivateDocxParseReport:
        if report.report_object_sha256 is None:
            raise ValueError("PrivateDocxParseReport must be stored in ObjectStore first")
        serialized = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT report_object_hash,report_json FROM private_docx_parse_report "
                "WHERE docx_parse_report_id=?",
                (report.docx_parse_report_id,),
            ).fetchone()
            if existing is not None:
                if existing["report_object_hash"] != report.report_object_sha256:
                    raise ValueError(
                        f"Private DOCX parse report collision: {report.docx_parse_report_id}"
                    )
                return PrivateDocxParseReport.model_validate_json(existing["report_json"])
            connection.execute(
                "INSERT INTO private_docx_parse_report(docx_parse_report_id,manifest_id,"
                "parser_version,coverage_status,report_object_hash,report_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    report.docx_parse_report_id,
                    report.manifest_id,
                    report.parser_version,
                    report.coverage_status.value,
                    report.report_object_sha256,
                    serialized,
                    report.created_at.isoformat(),
                ),
            )
        return report
