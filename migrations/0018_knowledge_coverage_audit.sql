CREATE TABLE knowledge_local_coverage_report (
    report_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    seed_source_id TEXT NOT NULL,
    status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    audited_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_local_coverage_latest
ON knowledge_local_coverage_report(source_id, seed_source_id, audited_at, report_id);

CREATE TABLE knowledge_coverage_audit_report (
    report_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    audited_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_coverage_audit_latest
ON knowledge_coverage_audit_report(audited_at, report_id);
