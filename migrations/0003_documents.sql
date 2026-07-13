CREATE TABLE source_snapshot_detail (
    snapshot_id TEXT PRIMARY KEY REFERENCES source_snapshot_index(snapshot_id),
    source_url TEXT,
    mime TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    headers_hash TEXT,
    rights_status TEXT NOT NULL
);

CREATE TABLE source_document (
    document_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    document_type TEXT NOT NULL,
    company_ids_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    effective_at TEXT,
    disclosure_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    rights_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(publisher, disclosure_id)
);

CREATE TABLE document_snapshot (
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    linked_at TEXT NOT NULL,
    PRIMARY KEY(document_id, snapshot_id)
);

CREATE INDEX idx_source_document_company_time
ON source_document(published_at, publisher);

CREATE INDEX idx_document_snapshot_snapshot
ON document_snapshot(snapshot_id);
