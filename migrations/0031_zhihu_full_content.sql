ALTER TABLE zhihu_content_version
ADD COLUMN content_completeness TEXT NOT NULL DEFAULT 'LISTING_UNVERIFIED'
CHECK (content_completeness IN ('LISTING_UNVERIFIED', 'DETAIL_VERIFIED'));

CREATE INDEX idx_zhihu_content_completeness
ON zhihu_content_version(source_id, content_type, content_id, content_completeness);

CREATE TABLE zhihu_manual_collection_task (
    task_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    response_kind TEXT NOT NULL,
    content_id TEXT,
    parent_comment_id TEXT,
    public_url TEXT NOT NULL,
    last_cursor TEXT,
    failure_class TEXT NOT NULL,
    source_snapshot_id TEXT,
    required_action TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED')),
    task_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_zhihu_manual_collection_task_open
ON zhihu_manual_collection_task(status, source_id, content_type, response_kind, task_id);
