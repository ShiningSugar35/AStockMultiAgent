-- Storage lifecycle plans, audit runs and operations SLO snapshots.

CREATE TABLE IF NOT EXISTS storage_lifecycle_plan (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_storage_lifecycle_plan_created
    ON storage_lifecycle_plan (created_at DESC);
CREATE TABLE IF NOT EXISTS storage_lifecycle_audit_run (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('AUDIT','EXECUTION')),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    finding_codes_json TEXT NOT NULL DEFAULT '[]',
    eligible_file_count INTEGER NOT NULL DEFAULT 0 CHECK (eligible_file_count >= 0),
    eligible_bytes INTEGER NOT NULL DEFAULT 0 CHECK (eligible_bytes >= 0),
    deleted_file_count INTEGER NOT NULL DEFAULT 0 CHECK (deleted_file_count >= 0),
    deleted_bytes INTEGER NOT NULL DEFAULT 0 CHECK (deleted_bytes >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_storage_lifecycle_audit_run_created
    ON storage_lifecycle_audit_run (created_at DESC);

CREATE TABLE IF NOT EXISTS operations_slo_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'WARN')),
    finding_codes_json TEXT NOT NULL DEFAULT '[]',
    evidence_freshness_status TEXT NOT NULL,
    latest_evidence_age_seconds INTEGER,
    provider_degraded_count INTEGER NOT NULL DEFAULT 0,
    open_circuit_count INTEGER NOT NULL DEFAULT 0,
    report_total_count INTEGER NOT NULL DEFAULT 0,
    report_published_count INTEGER NOT NULL DEFAULT 0,
    report_success_rate REAL,
    monitor_pending_task_count INTEGER NOT NULL DEFAULT 0,
    monitor_failed_task_count INTEGER NOT NULL DEFAULT 0,
    runtime_bytes INTEGER NOT NULL DEFAULT 0,
    object_store_bytes INTEGER NOT NULL DEFAULT 0,
    temp_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_slo_snapshot_created
    ON operations_slo_snapshot (created_at DESC);
