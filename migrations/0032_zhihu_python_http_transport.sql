CREATE TABLE zhihu_imported_response_v2 (
    envelope_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    response_kind TEXT NOT NULL,
    content_type TEXT,
    content_id TEXT,
    parent_comment_id TEXT,
    listing_page INTEGER CHECK (listing_page IS NULL OR listing_page >= 0),
    request_cursor TEXT,
    requested_url TEXT NOT NULL,
    http_status INTEGER NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    response_mime TEXT NOT NULL,
    transport TEXT NOT NULL
        CHECK (transport IN ('CHROME', 'MANUAL_IMPORT', 'PYTHON_HTTP')),
    source_snapshot_id TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    body_byte_size INTEGER NOT NULL CHECK (body_byte_size >= 0),
    import_status TEXT NOT NULL
        CHECK (import_status IN ('PENDING', 'CONSUMED', 'REJECTED')),
    captured_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    consumed_at TEXT,
    record_json TEXT NOT NULL,
    comment_page INTEGER CHECK (comment_page IS NULL OR comment_page >= 0)
);

INSERT INTO zhihu_imported_response_v2 (
    envelope_id,
    source_id,
    response_kind,
    content_type,
    content_id,
    parent_comment_id,
    listing_page,
    request_cursor,
    requested_url,
    http_status,
    response_mime,
    transport,
    source_snapshot_id,
    raw_object_hash,
    body_byte_size,
    import_status,
    captured_at,
    imported_at,
    consumed_at,
    record_json,
    comment_page
)
SELECT
    envelope_id,
    source_id,
    response_kind,
    content_type,
    content_id,
    parent_comment_id,
    listing_page,
    request_cursor,
    requested_url,
    http_status,
    response_mime,
    transport,
    source_snapshot_id,
    raw_object_hash,
    body_byte_size,
    import_status,
    captured_at,
    imported_at,
    consumed_at,
    record_json,
    comment_page
FROM zhihu_imported_response;

DROP TABLE zhihu_imported_response;
ALTER TABLE zhihu_imported_response_v2 RENAME TO zhihu_imported_response;

CREATE INDEX idx_zhihu_imported_response_queue
ON zhihu_imported_response(
    source_id,
    import_status,
    response_kind,
    imported_at,
    envelope_id
);

CREATE UNIQUE INDEX idx_zhihu_imported_response_snapshot_scope
ON zhihu_imported_response(
    source_id,
    response_kind,
    IFNULL(content_type, ''),
    IFNULL(content_id, ''),
    IFNULL(parent_comment_id, ''),
    IFNULL(listing_page, -1),
    IFNULL(request_cursor, ''),
    requested_url,
    raw_object_hash
);
