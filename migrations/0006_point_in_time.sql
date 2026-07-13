CREATE TABLE point_in_time_metadata (
    pit_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    source_document_id TEXT REFERENCES source_document(document_id),
    source_snapshot_id TEXT REFERENCES source_snapshot_index(snapshot_id),
    period_end TEXT,
    published_at TEXT,
    effective_at TEXT,
    ingested_at TEXT NOT NULL,
    available_to_system_at TEXT NOT NULL,
    revised_at TEXT,
    supersedes_source_id TEXT,
    point_in_time_status TEXT NOT NULL,
    availability_basis TEXT NOT NULL,
    pit_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(supersedes_source_id) REFERENCES point_in_time_metadata(source_id)
);

CREATE INDEX idx_pit_available_status
ON point_in_time_metadata(available_to_system_at, point_in_time_status);

CREATE INDEX idx_pit_supersedes
ON point_in_time_metadata(supersedes_source_id);
