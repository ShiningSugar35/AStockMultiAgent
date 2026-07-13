CREATE TABLE document_page (
    page_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    parser_version TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    text_object_hash TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    text_char_count INTEGER NOT NULL CHECK (text_char_count >= 0),
    page_manifest_hash TEXT NOT NULL,
    page_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_id, page_number, parser_version)
);

CREATE TABLE document_parse_run (
    parse_run_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    parser_version TEXT NOT NULL,
    page_scope_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    UNIQUE(snapshot_id, parser_version, page_scope_hash)
);

CREATE INDEX idx_document_page_document
ON document_page(document_id, page_number, parser_version);

CREATE INDEX idx_document_parse_snapshot
ON document_parse_run(snapshot_id, parser_version);
