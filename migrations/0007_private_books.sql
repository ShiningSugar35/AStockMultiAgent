CREATE TABLE book_source_manifest (
    manifest_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    pit_id TEXT NOT NULL REFERENCES point_in_time_metadata(pit_id),
    file_sha256 TEXT NOT NULL,
    file_version TEXT NOT NULL,
    source_page_count INTEGER NOT NULL CHECK (source_page_count >= 0),
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, file_version)
);

CREATE TABLE book_parse_report (
    book_parse_report_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES book_source_manifest(manifest_id),
    parser_version TEXT NOT NULL,
    parse_scope TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(manifest_id, parser_version, parse_scope, report_object_hash)
);

CREATE INDEX idx_book_manifest_snapshot
ON book_source_manifest(snapshot_id);
