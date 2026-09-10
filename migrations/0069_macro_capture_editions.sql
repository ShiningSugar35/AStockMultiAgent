-- Preserve immutable release payloads while admitting separately versioned parses.
-- No foreign key references this index; raw objects and economic tables are untouched.
CREATE TABLE macro_release_snapshots_v2_editions (
    release_id TEXT PRIMARY KEY,
    authority TEXT NOT NULL,
    release_family TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    capture_mode TEXT NOT NULL CHECK(capture_mode IN ('LIVE', 'RECORDED')),
    capture_policy_hash TEXT NOT NULL,
    UNIQUE(authority, release_family, source_hash, capture_mode, capture_policy_hash)
);

INSERT INTO macro_release_snapshots_v2_editions (
    release_id, authority, release_family, source_url, source_hash, captured_at,
    published_at, parse_status, payload_json, created_at, capture_mode, capture_policy_hash
)
SELECT release_id, authority, release_family, source_url, source_hash, captured_at,
       published_at, parse_status, payload_json, created_at,
       COALESCE(json_extract(payload_json, '$.capture_mode'), 'RECORDED'),
       COALESCE(json_extract(payload_json, '$.capture_policy_hash'), '')
FROM macro_release_snapshots_v2;

DROP TABLE macro_release_snapshots_v2;
ALTER TABLE macro_release_snapshots_v2_editions RENAME TO macro_release_snapshots_v2;
CREATE INDEX idx_macro_release_snapshots_v2_family_time
    ON macro_release_snapshots_v2(authority, release_family, published_at);
