ALTER TABLE committee_trade_protocol_index
RENAME TO committee_trade_protocol_index_legacy;

CREATE TABLE committee_trade_protocol_index (
    protocol_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE REFERENCES committee_decision_index(decision_id),
    company_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    protocol_status TEXT NOT NULL CHECK(protocol_status IN ('ACTIVE','BLOCKED')),
    strategy_id TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    requires_user_confirmation INTEGER NOT NULL CHECK(requires_user_confirmation = 1),
    broker_execution_allowed INTEGER NOT NULL CHECK(broker_execution_allowed = 0),
    paper_simulation_allowed INTEGER NOT NULL CHECK(paper_simulation_allowed IN (0, 1)),
    ledger_write_allowed INTEGER NOT NULL CHECK(ledger_write_allowed IN (0, 1)),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(paper_simulation_allowed = ledger_write_allowed)
);

-- Legacy protocol objects used the pre-Phase-6 execution contract. Keep their
-- immutable index for audit, but never reinterpret them as paper-only
-- authorizations. Re-running the committee creates a new versioned protocol id
-- in the active index.

CREATE TABLE paper_confirmation_key_binding (
    confirmation_id TEXT PRIMARY KEY
        REFERENCES paper_operation_confirmation(confirmation_id),
    key_id TEXT NOT NULL,
    public_key_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE paper_execution_request_index (
    execution_request_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    trade_protocol_id TEXT NOT NULL REFERENCES committee_trade_protocol_index(protocol_id),
    trade_protocol_object_hash TEXT NOT NULL,
    reference_pack_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    reference_pack_object_hash TEXT NOT NULL,
    operation_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    operation_object_hash TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'WAITING_USER_CONFIRMATION',
        'COMPLETE',
        'REJECTED',
        'NEEDS_INFO',
        'INTERRUPTED'
    )),
    created_at TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key)
);

CREATE INDEX idx_paper_execution_protocol
ON paper_execution_request_index(trade_protocol_id, created_at, execution_request_id);

CREATE TABLE phase6_run_index (
    run_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    data_mode TEXT NOT NULL CHECK(data_mode = 'RECORDED_ACCEPTANCE'),
    research_request_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    memo_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    decision_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    protocol_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    paper_reference_pack_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    report_object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_phase6_run_company
ON phase6_run_index(company_id, created_at, run_id);
