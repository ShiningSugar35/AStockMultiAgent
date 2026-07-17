CREATE TABLE financial_anomaly_dataset_manifest (
    audit_run_id TEXT NOT NULL REFERENCES financial_audit_run(audit_run_id),
    dataset_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    feature_count INTEGER NOT NULL CHECK (feature_count > 0),
    training_sample_count INTEGER NOT NULL CHECK (training_sample_count > 0),
    evaluation_sample_count INTEGER NOT NULL CHECK (evaluation_sample_count >= 0),
    target_sample_id TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (audit_run_id, dataset_id)
);

CREATE TABLE financial_anomaly_model_manifest (
    model_artifact_id TEXT PRIMARY KEY,
    audit_run_id TEXT NOT NULL REFERENCES financial_audit_run(audit_run_id),
    dataset_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_type TEXT NOT NULL,
    model_version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    library_versions_json TEXT NOT NULL,
    dataset_object_hash TEXT NOT NULL,
    serialized_model_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (audit_run_id, dataset_id)
        REFERENCES financial_anomaly_dataset_manifest(audit_run_id, dataset_id)
);

CREATE INDEX idx_financial_anomaly_model_run
ON financial_anomaly_model_manifest(audit_run_id, dataset_id, model_id);
