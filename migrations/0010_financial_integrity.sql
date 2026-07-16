CREATE TABLE financial_audit_run (
    audit_run_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    company_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    industry_profile TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'NEEDS_INFO', 'FAILED')
    ),
    rule_registry_version TEXT NOT NULL,
    industry_profile_version TEXT NOT NULL,
    request_object_hash TEXT NOT NULL,
    report_object_hash TEXT,
    checkpoint_step TEXT NOT NULL,
    last_error_class TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(request_hash, rule_registry_version, industry_profile_version)
);

CREATE TABLE financial_audit_attempt (
    attempt_id TEXT PRIMARY KEY,
    audit_run_id TEXT NOT NULL REFERENCES financial_audit_run(audit_run_id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_class TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1))
);

CREATE TABLE financial_manual_task (
    task_id TEXT PRIMARY KEY,
    audit_run_id TEXT NOT NULL REFERENCES financial_audit_run(audit_run_id),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'CANCELLED')),
    reason_code TEXT NOT NULL,
    task_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_financial_audit_company_asof
ON financial_audit_run(company_id, as_of, created_at);

CREATE INDEX idx_financial_audit_status
ON financial_audit_run(status, updated_at);

CREATE INDEX idx_financial_attempt_run
ON financial_audit_attempt(audit_run_id, started_at);

CREATE INDEX idx_financial_manual_task_status
ON financial_manual_task(status, audit_run_id);
