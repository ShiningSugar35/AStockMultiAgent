CREATE TABLE candidate_input_release (
    input_release_id TEXT PRIMARY KEY,
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    manifest_object_hash TEXT NOT NULL,
    manifest_schema_version TEXT NOT NULL,
    source_mode TEXT NOT NULL CHECK(source_mode IN ('LOCAL','RECORDED','LIVE')),
    as_of TEXT NOT NULL,
    artifact_count INTEGER NOT NULL CHECK(artifact_count > 0),
    company_count INTEGER NOT NULL CHECK(company_count > 0),
    expected_company_count INTEGER NOT NULL CHECK(expected_company_count > 0),
    universe_semantic_hash TEXT NOT NULL,
    coverage_proof_artifact_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE candidate_scan_run (
    scan_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    input_release_id TEXT NOT NULL REFERENCES candidate_input_release(input_release_id),
    rules_version TEXT NOT NULL CHECK(rules_version='candidate-scan-v1'),
    as_of TEXT NOT NULL,
    formal_historical INTEGER NOT NULL CHECK(formal_historical IN (0,1)),
    live INTEGER NOT NULL CHECK(live IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','NEEDS_INFO','FAILED')),
    checkpoint_step TEXT NOT NULL CHECK(checkpoint_step IN (
        'INPUT_REGISTERED','INPUTS_VALIDATED','SIGNALS_WRITTEN',
        'CANDIDATES_WRITTEN','REGISTRY_COMMITTED','COMPLETE'
    )),
    report_artifact_id TEXT REFERENCES artifact_registry(artifact_id),
    report_object_hash TEXT,
    signal_manifest_id TEXT,
    universe_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(request_hash,input_release_id,rules_version)
);

CREATE TABLE candidate_scan_attempt (
    attempt_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES candidate_scan_run(scan_id),
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    status TEXT NOT NULL CHECK(status IN (
        'RUNNING','SUCCEEDED','FAILED','INTERRUPTED_RECOVERED'
    )),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_class TEXT,
    UNIQUE(scan_id,ordinal)
);

CREATE TABLE candidate_signal_manifest (
    signal_manifest_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL UNIQUE REFERENCES candidate_scan_run(scan_id),
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    manifest_object_hash TEXT NOT NULL,
    parquet_descriptor_json TEXT NOT NULL,
    signal_count INTEGER NOT NULL CHECK(signal_count >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE candidate_identity (
    candidate_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(instrument_id)
);

CREATE TABLE candidate_record_version (
    candidate_version_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_identity(candidate_id),
    scan_id TEXT NOT NULL REFERENCES candidate_scan_run(scan_id),
    previous_version_id TEXT REFERENCES candidate_record_version(candidate_version_id),
    lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN (
        'OBSERVATION','RESEARCH_READY','REVIEW_DUE','CLOSED'
    )),
    strength TEXT NOT NULL CHECK(strength IN ('NONE','WEAK','MODERATE','STRONG')),
    evaluation_status TEXT NOT NULL CHECK(evaluation_status IN ('EVALUATED','NEEDS_INFO')),
    miss_count INTEGER NOT NULL CHECK(miss_count >= 0),
    reactivation_count INTEGER NOT NULL CHECK(reactivation_count >= 0),
    record_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    record_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id,scan_id)
);

CREATE TABLE candidate_scan_member (
    scan_id TEXT NOT NULL REFERENCES candidate_scan_run(scan_id),
    candidate_id TEXT NOT NULL REFERENCES candidate_identity(candidate_id),
    candidate_version_id TEXT NOT NULL REFERENCES candidate_record_version(candidate_version_id),
    company_id TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    strength TEXT NOT NULL,
    PRIMARY KEY(scan_id,candidate_id)
);

CREATE TABLE candidate_universe_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL UNIQUE REFERENCES candidate_scan_run(scan_id),
    snapshot_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    snapshot_object_hash TEXT NOT NULL,
    member_descriptor_json TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK(member_count >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE candidate_audit (
    audit_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES candidate_scan_run(scan_id),
    audit_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    audit_object_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PASS','FAIL')),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_candidate_scan_asof
ON candidate_scan_run(as_of DESC,scan_id DESC);

CREATE INDEX idx_candidate_record_latest
ON candidate_record_version(candidate_id,created_at DESC,candidate_version_id DESC);

CREATE INDEX idx_candidate_identity_company
ON candidate_identity(company_id);
