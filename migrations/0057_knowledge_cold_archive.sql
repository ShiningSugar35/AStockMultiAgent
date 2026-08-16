CREATE TABLE IF NOT EXISTS knowledge_cold_archive (
    archive_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK(length(trim(schema_version)) > 0),
    manifest_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(manifest_object_hash) = 64 AND manifest_object_hash NOT GLOB '*[^0-9a-f]*'),
    archive_path TEXT NOT NULL UNIQUE CHECK(length(trim(archive_path)) > 0),
    source_latest_migration TEXT NOT NULL CHECK(length(source_latest_migration) = 4),
    archived_tables_json TEXT NOT NULL CHECK(json_valid(archived_tables_json)),
    protected_rows_json TEXT NOT NULL CHECK(json_valid(protected_rows_json)),
    archived_row_count INTEGER NOT NULL CHECK(archived_row_count >= 0),
    archive_size_bytes INTEGER NOT NULL CHECK(archive_size_bytes >= 0),
    source_db_size_bytes INTEGER NOT NULL CHECK(source_db_size_bytes >= 0),
    status TEXT NOT NULL CHECK(status IN ('READY', 'CORRUPT')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_cold_archive_created
ON knowledge_cold_archive(created_at, archive_id);
