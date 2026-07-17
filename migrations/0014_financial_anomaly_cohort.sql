ALTER TABLE financial_anomaly_dataset_manifest
ADD COLUMN industry_profile TEXT NOT NULL DEFAULT 'OTHER';

ALTER TABLE financial_anomaly_dataset_manifest
ADD COLUMN peer_cohort_id TEXT NOT NULL DEFAULT 'LEGACY_UNSPECIFIED';
