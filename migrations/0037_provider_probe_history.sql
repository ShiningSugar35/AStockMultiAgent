CREATE TABLE provider_probe_event (
    probe_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    registry_version TEXT NOT NULL,
    capability_hash TEXT NOT NULL,
    probe_mode TEXT NOT NULL CHECK(probe_mode IN ('RECORDED','LIVE')),
    status TEXT NOT NULL CHECK(status IN ('HEALTHY','DEGRADED','UNAVAILABLE')),
    completed_at TEXT NOT NULL,
    report_artifact_id TEXT NOT NULL UNIQUE,
    report_object_hash TEXT NOT NULL,
    failure_code TEXT,
    failure_count INTEGER NOT NULL CHECK(failure_count >= 0)
);

CREATE INDEX idx_provider_probe_event_provider_completed
ON provider_probe_event(provider_id, completed_at DESC);

ALTER TABLE provider_health ADD COLUMN registry_version TEXT;
ALTER TABLE provider_health ADD COLUMN probe_mode TEXT;
ALTER TABLE provider_health ADD COLUMN report_artifact_id TEXT;
ALTER TABLE provider_health ADD COLUMN report_object_hash TEXT;
ALTER TABLE provider_health ADD COLUMN failure_code TEXT;
ALTER TABLE provider_health ADD COLUMN latest_probe_id TEXT;
