CREATE TABLE external_capability_qualification (
    report_id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    admitted_stage TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    report_artifact_id TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_external_capability_qualification_active
ON external_capability_qualification(capability_id, admitted_stage, valid_from, expires_at);

CREATE TABLE external_capability_revocation (
    revocation_id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(report_id) REFERENCES external_capability_qualification(report_id)
);

CREATE INDEX idx_external_capability_revocation_report
ON external_capability_revocation(report_id, revoked_at);

CREATE TRIGGER external_capability_qualification_no_update
BEFORE UPDATE ON external_capability_qualification
BEGIN
    SELECT RAISE(ABORT, 'external_capability_qualification is append-only');
END;

CREATE TRIGGER external_capability_qualification_no_delete
BEFORE DELETE ON external_capability_qualification
BEGIN
    SELECT RAISE(ABORT, 'external_capability_qualification is append-only');
END;

CREATE TRIGGER external_capability_revocation_no_update
BEFORE UPDATE ON external_capability_revocation
BEGIN
    SELECT RAISE(ABORT, 'external_capability_revocation is append-only');
END;

CREATE TRIGGER external_capability_revocation_no_delete
BEFORE DELETE ON external_capability_revocation
BEGIN
    SELECT RAISE(ABORT, 'external_capability_revocation is append-only');
END;
