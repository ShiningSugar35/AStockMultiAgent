-- Keep incomplete scheduled executions recoverable without consuming the one final receipt per bucket.
-- Historical DEGRADED receipts are preserved byte-for-byte in an append-only attempts table,
-- then removed from the final accepted-run table so a later fully prepared request can complete.
CREATE TABLE scheduled_research_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    schedule_bucket TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome = 'DEGRADED'),
    request_fingerprint TEXT,
    attempt_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    FOREIGN KEY(binding_id) REFERENCES scheduled_task_bindings(binding_id)
);

CREATE INDEX idx_scheduled_research_attempts_binding_bucket
    ON scheduled_research_attempts(binding_id, schedule_bucket, attempted_at);

INSERT INTO scheduled_research_attempts (
    attempt_id, run_id, binding_id, schedule_bucket, idempotency_key, outcome,
    request_fingerprint, attempt_hash, payload_json, attempted_at
)
SELECT receipt_id, run_id, binding_id, schedule_bucket, idempotency_key, outcome,
       json_extract(payload_json, '$.request_fingerprint'), receipt_hash, payload_json, completed_at
FROM scheduled_research_runs
WHERE outcome = 'DEGRADED';

DELETE FROM scheduled_research_runs WHERE outcome = 'DEGRADED';

CREATE TRIGGER scheduled_research_runs_no_degraded_final
BEFORE INSERT ON scheduled_research_runs
WHEN NEW.outcome = 'DEGRADED'
BEGIN
    SELECT RAISE(ABORT, 'DEGRADED belongs in scheduled_research_attempts');
END;

CREATE TRIGGER scheduled_research_attempts_no_update
BEFORE UPDATE ON scheduled_research_attempts
BEGIN
    SELECT RAISE(ABORT, 'scheduled_research_attempts is append-only');
END;

CREATE TRIGGER scheduled_research_attempts_no_delete
BEFORE DELETE ON scheduled_research_attempts
BEGIN
    SELECT RAISE(ABORT, 'scheduled_research_attempts is append-only');
END;
