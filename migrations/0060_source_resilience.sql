ALTER TABLE source_access_decision ADD COLUMN selected_source_id TEXT;
ALTER TABLE source_access_decision ADD COLUMN fallback_source_chain_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE source_circuit_breaker (
    source_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('CLOSED','OPEN','HALF_OPEN')),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    opened_at TEXT,
    retry_after_at TEXT,
    last_failure_class TEXT,
    half_open_probe_in_flight INTEGER NOT NULL DEFAULT 0 CHECK (half_open_probe_in_flight IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_id, capability)
);

CREATE INDEX idx_source_circuit_breaker_state
ON source_circuit_breaker(state, retry_after_at, updated_at);
