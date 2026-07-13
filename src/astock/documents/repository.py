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

    def get_model(self, document_id: str) -> SourceDocument | None:
        stored = self.get(document_id)
        if stored is None:
            return None
        return SourceDocument.model_validate(
            {
                key: stored[key]
                for key in (
                    "document_id",
                    "title",
                    "publisher",
                    "document_type",
                    "company_ids",
                    "published_at",
                    "effective_at",
                    "disclosure_id",
                    "source_url",
                    "rights_status",
                )
            }
        )

    def latest_snapshot(self, document_id: str) -> SourceSnapshot | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT i.snapshot_id,i.source_id,i.object_hash,i.fetched_at,"
                "i.availability_at,i.fetch_status,d.source_url,d.mime,d.byte_size,"
                "d.headers_hash,d.rights_status FROM document_snapshot ds "
                "JOIN source_snapshot_index i ON i.snapshot_id=ds.snapshot_id "
                "JOIN source_snapshot_detail d ON d.snapshot_id=ds.snapshot_id "
                "WHERE ds.document_id=? ORDER BY ds.linked_at DESC,ds.snapshot_id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceSnapshot(
            snapshot_id=row["snapshot_id"],
            source_id=row["source_id"],
            object_sha256=row["object_hash"],
            fetched_at=row["fetched_at"],
            available_to_system_at=row["availability_at"],
            source_url=row["source_url"],
            mime=row["mime"],
            byte_size=row["byte_size"],
            headers_hash=row["headers_hash"],
            fetch_status=row["fetch_status"],
            rights_status=row["rights_status"],
        )

    def snapshot(self, snapshot_id: str) -> SourceSnapshot | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT i.snapshot_id,i.source_id,i.object_hash,i.fetched_at,"
                "i.availability_at,i.fetch_status,d.source_url,d.mime,d.byte_size,"
                "d.headers_hash,d.rights_status FROM source_snapshot_index i "
                "JOIN source_snapshot_detail d ON d.snapshot_id=i.snapshot_id "
                "WHERE i.snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceSnapshot(
            snapshot_id=row["snapshot_id"],
            source_id=row["source_id"],
            object_sha256=row["object_hash"],
            fetched_at=row["fetched_at"],
            available_to_system_at=row["availability_at"],
            source_url=row["source_url"],
            mime=row["mime"],
            byte_size=row["byte_size"],
            headers_hash=row["headers_hash"],
            fetch_status=row["fetch_status"],
            rights_status=row["rights_status"],
        )
