CREATE INDEX idx_source_snapshot_object_hash
    ON source_snapshot_index(object_hash);

CREATE TABLE official_document_lookup (
    capture_artifact_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    admission_snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    company_id TEXT NOT NULL,
    period_end TEXT,
    document_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY(capture_artifact_id, company_id)
);

CREATE INDEX idx_official_document_lookup_company_period_type
    ON official_document_lookup(company_id, period_end, document_type, published_at);

CREATE INDEX idx_official_document_lookup_snapshot
    ON official_document_lookup(snapshot_id);
