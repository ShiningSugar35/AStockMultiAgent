"""SQLite metadata writer for immutable source documents and snapshots."""

from __future__ import annotations

import json

from astock.core.state import StateStore, utc_now_text
from astock.schemas import SourceDocument, SourceSnapshot


class DocumentRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def register(self, document: SourceDocument, snapshot: SourceSnapshot) -> None:
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT title,publisher,document_type,company_ids_json,published_at,effective_at,"
                "disclosure_id,source_url,rights_status FROM source_document WHERE document_id=?",
                (document.document_id,),
            ).fetchone()
            values = (
                document.title,
                document.publisher,
                document.document_type.value,
                json.dumps(document.company_ids, ensure_ascii=False, separators=(",", ":")),
                document.published_at.isoformat(),
                document.effective_at.isoformat() if document.effective_at else None,
                document.disclosure_id,
                document.source_url,
                document.rights_status,
            )
            if existing is not None and tuple(existing) != values:
                raise ValueError(f"Document identity collision: {document.document_id}")
            if existing is None:
                connection.execute(
                    "INSERT INTO source_document(document_id,title,publisher,document_type,"
                    "company_ids_json,published_at,effective_at,disclosure_id,source_url,"
                    "rights_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (document.document_id, *values, utc_now_text()),
                )
            connection.execute(
                "INSERT OR IGNORE INTO document_snapshot(document_id,snapshot_id,linked_at) "
                "VALUES(?,?,?)",
                (document.document_id, snapshot.snapshot_id, utc_now_text()),
            )

    def get(self, document_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_document WHERE document_id=?", (document_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["company_ids"] = json.loads(str(result.pop("company_ids_json")))
        return result
