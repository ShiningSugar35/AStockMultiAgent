CREATE TABLE knowledge_source_identity (
    source_id TEXT PRIMARY KEY,
    platform_user_id TEXT NOT NULL,
    url_token TEXT NOT NULL,
    display_name TEXT NOT NULL,
    profile_url TEXT NOT NULL,
    identity_status TEXT NOT NULL,
    profile_snapshot_id TEXT NOT NULL,
    profile_object_hash TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE zhihu_listing_page_manifest (
    page_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    listing_page INTEGER NOT NULL CHECK (listing_page >= 0),
    request_url TEXT NOT NULL,
    request_cursor TEXT,
    next_cursor TEXT,
    is_end INTEGER NOT NULL CHECK (is_end IN (0, 1)),
    content_count INTEGER NOT NULL CHECK (content_count >= 0),
    source_snapshot_id TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    transport TEXT NOT NULL,
    http_status INTEGER NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    structure_version TEXT NOT NULL,
    page_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX idx_zhihu_listing_scope_cursor
ON zhihu_listing_page_manifest(source_id, content_type, listing_page, request_cursor);

CREATE TABLE zhihu_content_version (
    version_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at TEXT,
    updated_at TEXT,
    collected_at TEXT NOT NULL,
    body_object_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    raw_source_snapshot_id TEXT NOT NULL,
    previous_version_id TEXT REFERENCES zhihu_content_version(version_id),
    record_json TEXT NOT NULL,
    UNIQUE(source_id, content_type, content_id, body_object_hash, metadata_hash)
);

CREATE INDEX idx_zhihu_content_latest
ON zhihu_content_version(source_id, content_type, content_id, collected_at, version_id);

CREATE TABLE knowledge_coverage_report (
    report_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    terminal_condition TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_coverage_scope
ON knowledge_coverage_report(source_id, content_type, created_at, report_id);
