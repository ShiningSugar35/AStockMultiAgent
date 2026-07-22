CREATE TABLE financial_source_release (
    release_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('XSHG','XSHE','BJSE')),
    instrument_type TEXT NOT NULL CHECK(instrument_type='STOCK'),
    instrument_release_id TEXT NOT NULL REFERENCES market_reference_release(release_id),
    instrument_manifest_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    instrument_manifest_object_hash TEXT NOT NULL,
    instrument_content_hash TEXT NOT NULL,
    instrument_available_to_system_at TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK(period_type IN ('ANNUAL','SEMIANNUAL','QUARTERLY')),
    previous_release_id TEXT,
    supersedes_release_id TEXT,
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    manifest_object_hash TEXT NOT NULL,
    manifest_schema_version TEXT NOT NULL,
    provider_ids_json TEXT NOT NULL,
    raw_snapshot_ids_json TEXT NOT NULL,
    official_document_id TEXT NOT NULL REFERENCES source_document(document_id),
    official_index_snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    official_snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    official_pit_id TEXT NOT NULL REFERENCES point_in_time_metadata(pit_id),
    source_files_json TEXT NOT NULL,
    certified_files_json TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    certified_content_hash TEXT NOT NULL,
    available_to_system_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status='CERTIFIED'),
    source_observation_count INTEGER NOT NULL CHECK(source_observation_count > 0),
    certified_fact_count INTEGER NOT NULL CHECK(certified_fact_count > 0),
    coverage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(company_id,period_end,period_type,release_id),
    FOREIGN KEY(company_id,period_end,period_type,previous_release_id)
        REFERENCES financial_source_release(company_id,period_end,period_type,release_id),
    FOREIGN KEY(company_id,period_end,period_type,supersedes_release_id)
        REFERENCES financial_source_release(company_id,period_end,period_type,release_id)
);

CREATE TABLE financial_source_head (
    company_id TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK(period_type IN ('ANNUAL','SEMIANNUAL','QUARTERLY')),
    release_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(company_id,period_end,period_type),
    FOREIGN KEY(company_id,period_end,period_type,release_id)
        REFERENCES financial_source_release(company_id,period_end,period_type,release_id)
);

CREATE INDEX idx_financial_source_release_asof
ON financial_source_release(
    company_id,period_end,period_type,available_to_system_at DESC,release_id DESC
);
