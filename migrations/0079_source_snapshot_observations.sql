CREATE TABLE source_snapshot_alias (
    requested_snapshot_id TEXT PRIMARY KEY,
    canonical_snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    source_id TEXT NOT NULL,
    object_hash TEXT NOT NULL
);

CREATE INDEX idx_source_snapshot_alias_canonical
    ON source_snapshot_alias(canonical_snapshot_id);

INSERT INTO source_snapshot_alias(
    requested_snapshot_id,
    canonical_snapshot_id,
    source_id,
    object_hash
)
SELECT snapshot_id, snapshot_id, source_id, object_hash
FROM source_snapshot_index;

CREATE TABLE source_snapshot_observation (
    observation_id TEXT PRIMARY KEY,
    requested_snapshot_id TEXT NOT NULL
        REFERENCES source_snapshot_alias(requested_snapshot_id),
    canonical_snapshot_id TEXT NOT NULL
        REFERENCES source_snapshot_index(snapshot_id),
    observed_at TEXT NOT NULL,
    availability_at TEXT NOT NULL,
    source_url TEXT,
    mime TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    headers_hash TEXT,
    fetch_status TEXT NOT NULL,
    rights_status TEXT NOT NULL
);

CREATE INDEX idx_source_snapshot_observation_canonical_time
    ON source_snapshot_observation(canonical_snapshot_id, observed_at);

CREATE INDEX idx_source_snapshot_observation_requested_time
    ON source_snapshot_observation(requested_snapshot_id, observed_at);
