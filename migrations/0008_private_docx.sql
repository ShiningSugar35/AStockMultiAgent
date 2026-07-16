CREATE TABLE document_block (
    block_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    block_index INTEGER NOT NULL CHECK (block_index >= 1),
    part_kind TEXT NOT NULL,
    block_kind TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    text_object_hash TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    text_char_count INTEGER NOT NULL CHECK (text_char_count >= 0),
    metadata_object_hash TEXT NOT NULL,
    block_manifest_hash TEXT NOT NULL,
    block_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_id, block_index, parser_version)
);

CREATE TABLE private_docx_parse_report (
    docx_parse_report_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES book_source_manifest(manifest_id),
    parser_version TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(manifest_id, parser_version, report_object_hash)
);

CREATE INDEX idx_document_block_document
ON document_block(document_id, block_index, parser_version);

CREATE INDEX idx_document_block_snapshot
ON document_block(snapshot_id, parser_version);

CREATE INDEX idx_private_docx_report_manifest
ON private_docx_parse_report(manifest_id, parser_version);
