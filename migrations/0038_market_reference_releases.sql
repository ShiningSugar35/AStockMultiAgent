CREATE TABLE market_reference_release (
    release_id TEXT PRIMARY KEY,
    dataset_kind TEXT NOT NULL CHECK(dataset_kind IN (
        'INSTRUMENT_MASTER','TRADING_CALENDAR','DAILY_UNADJUSTED','CORPORATE_ACTION'
    )),
    scope_key TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    previous_release_id TEXT REFERENCES market_reference_release(release_id),
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    manifest_object_hash TEXT NOT NULL,
    available_to_system_at TEXT NOT NULL,
    coverage_status TEXT NOT NULL CHECK(coverage_status IN ('COMPLETE','PARTIAL','EMPTY')),
    pit_status TEXT NOT NULL CHECK(pit_status IN ('CERTIFIED','RECONSTRUCTED','UNVERIFIED')),
    created_at TEXT NOT NULL,
    UNIQUE(dataset_kind, scope_key, content_hash)
);

CREATE TABLE market_reference_head (
    dataset_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    release_id TEXT NOT NULL REFERENCES market_reference_release(release_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(dataset_kind, scope_key)
);

CREATE INDEX idx_market_reference_release_asof
ON market_reference_release(dataset_kind, scope_key, available_to_system_at DESC, release_id DESC);
