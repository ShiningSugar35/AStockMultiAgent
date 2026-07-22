DROP INDEX idx_market_reference_release_asof;

ALTER TABLE market_reference_head RENAME TO market_reference_head_0038;
ALTER TABLE market_reference_release RENAME TO market_reference_release_0038;

CREATE TABLE market_reference_release (
    release_id TEXT PRIMARY KEY,
    dataset_kind TEXT NOT NULL CHECK(dataset_kind IN (
        'INSTRUMENT_MASTER','TRADING_CALENDAR','DAILY_UNADJUSTED','CORPORATE_ACTION'
    )),
    scope_key TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    previous_release_id TEXT,
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    manifest_object_hash TEXT NOT NULL,
    manifest_schema_version TEXT NOT NULL,
    raw_snapshot_ids_json TEXT NOT NULL,
    observation_files_json TEXT NOT NULL,
    canonical_files_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    available_to_system_at TEXT NOT NULL,
    coverage_status TEXT NOT NULL CHECK(coverage_status IN ('COMPLETE','PARTIAL','EMPTY')),
    pit_status TEXT NOT NULL CHECK(pit_status IN ('CERTIFIED','RECONSTRUCTED','UNVERIFIED')),
    created_at TEXT NOT NULL,
    UNIQUE(dataset_kind, scope_key, release_id),
    FOREIGN KEY(dataset_kind, scope_key, previous_release_id)
        REFERENCES market_reference_release(dataset_kind, scope_key, release_id)
);

INSERT INTO market_reference_release(
    release_id,dataset_kind,scope_key,provider_id,batch_id,content_hash,
    previous_release_id,manifest_artifact_id,manifest_object_hash,
    manifest_schema_version,raw_snapshot_ids_json,observation_files_json,
    canonical_files_json,coverage_json,available_to_system_at,coverage_status,
    pit_status,created_at
)
SELECT
    legacy.release_id,legacy.dataset_kind,legacy.scope_key,legacy.provider_id,
    legacy.batch_id,legacy.content_hash,legacy.previous_release_id,
    legacy.manifest_artifact_id,legacy.manifest_object_hash,
    artifact.schema_version,
    CASE
        WHEN json_valid(artifact.input_hashes_json)
        THEN json_remove(artifact.input_hashes_json, '$[#-1]')
        ELSE '[]'
    END,
    '[]','[]',
    '{"legacy_0038":true,"status":"' || legacy.coverage_status || '"}',
    legacy.available_to_system_at,legacy.coverage_status,'UNVERIFIED',legacy.created_at
FROM market_reference_release_0038 AS legacy
JOIN artifact_registry AS artifact
  ON artifact.artifact_id=legacy.manifest_artifact_id;

CREATE TABLE market_reference_head (
    dataset_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    release_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(dataset_kind, scope_key),
    FOREIGN KEY(dataset_kind, scope_key, release_id)
        REFERENCES market_reference_release(dataset_kind, scope_key, release_id)
);

INSERT INTO market_reference_head(dataset_kind,scope_key,release_id,updated_at)
SELECT dataset_kind,scope_key,release_id,updated_at
FROM market_reference_head_0038;

DROP TABLE market_reference_head_0038;
DROP TABLE market_reference_release_0038;

CREATE TABLE reference_provider_lease (
    lock_key TEXT PRIMARY KEY,
    owner_run_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK(fencing_token >= 0),
    lease_until TEXT NOT NULL
);

CREATE INDEX idx_market_reference_release_asof
ON market_reference_release(dataset_kind, scope_key, available_to_system_at DESC, release_id DESC);
