CREATE TABLE zhihu_comment_page_manifest (
    page_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    parent_comment_id TEXT,
    comment_page INTEGER NOT NULL CHECK (comment_page >= 0),
    request_url TEXT NOT NULL,
    request_cursor TEXT,
    next_cursor TEXT,
    is_end INTEGER NOT NULL CHECK (is_end IN (0, 1)),
    comment_count INTEGER NOT NULL CHECK (comment_count >= 0),
    source_snapshot_id TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    transport TEXT NOT NULL,
    http_status INTEGER NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    structure_version TEXT NOT NULL,
    page_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

ALTER TABLE zhihu_imported_response
ADD COLUMN comment_page INTEGER CHECK (comment_page IS NULL OR comment_page >= 0);

CREATE INDEX idx_zhihu_comment_page_scope
ON zhihu_comment_page_manifest(
    source_id,
    content_type,
    content_id,
    parent_comment_id,
    comment_page,
    request_cursor
);

CREATE TABLE zhihu_comment_version (
    version_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    root_comment_id TEXT NOT NULL,
    parent_comment_id TEXT,
    reply_to_comment_id TEXT,
    platform_author_id TEXT,
    published_at TEXT,
    updated_at TEXT,
    collected_at TEXT NOT NULL,
    body_object_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    raw_source_snapshot_id TEXT NOT NULL,
    previous_version_id TEXT REFERENCES zhihu_comment_version(version_id),
    record_json TEXT NOT NULL,
    UNIQUE(source_id, content_type, content_id, comment_id, body_object_hash, metadata_hash)
);

CREATE INDEX idx_zhihu_comment_latest
ON zhihu_comment_version(
    source_id,
    content_type,
    content_id,
    comment_id,
    collected_at,
    version_id
);

CREATE INDEX idx_zhihu_comment_root
ON zhihu_comment_version(source_id, content_type, content_id, root_comment_id);

CREATE TABLE zhihu_author_participation_chain (
    chain_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    root_comment_id TEXT NOT NULL,
    selection_rule_version TEXT NOT NULL,
    chain_object_hash TEXT NOT NULL,
    chain_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_zhihu_participation_scope
ON zhihu_author_participation_chain(source_id, content_type, content_id, root_comment_id);
