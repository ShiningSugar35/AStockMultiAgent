CREATE TABLE financial_peer_cohort_manifest (
    audit_run_id TEXT NOT NULL REFERENCES financial_audit_run(audit_run_id),
    cohort_id TEXT NOT NULL,
    industry_profile TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    formula_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    minimum_sample_size INTEGER NOT NULL CHECK (minimum_sample_size >= 3),
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (audit_run_id, cohort_id)
);

CREATE INDEX idx_financial_peer_cohort_metric
ON financial_peer_cohort_manifest(metric_id, formula_version, as_of);
