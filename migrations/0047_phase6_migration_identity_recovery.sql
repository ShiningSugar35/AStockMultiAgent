CREATE TABLE committee_trade_protocol_index_legacy (
    protocol_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE REFERENCES committee_decision_index(decision_id),
    company_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    protocol_status TEXT NOT NULL CHECK (protocol_status IN ('ACTIVE','BLOCKED')),
    strategy_id TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    requires_user_confirmation INTEGER NOT NULL CHECK (requires_user_confirmation = 1),
    broker_execution_allowed INTEGER NOT NULL CHECK (broker_execution_allowed = 0),
    ledger_write_allowed INTEGER NOT NULL CHECK (ledger_write_allowed = 0),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO committee_trade_protocol_index_legacy(
    protocol_id,
    decision_id,
    company_id,
    verdict,
    protocol_status,
    strategy_id,
    effective_from,
    requires_user_confirmation,
    broker_execution_allowed,
    ledger_write_allowed,
    object_hash,
    input_hash,
    created_at
)
SELECT
    protocol_id,
    decision_id,
    company_id,
    verdict,
    protocol_status,
    strategy_id,
    effective_from,
    requires_user_confirmation,
    broker_execution_allowed,
    ledger_write_allowed,
    object_hash,
    input_hash,
    created_at
FROM committee_trade_protocol_index
WHERE paper_simulation_allowed = 0
  AND ledger_write_allowed = 0;

DELETE FROM committee_trade_protocol_index
WHERE paper_simulation_allowed = 0
  AND ledger_write_allowed = 0;

CREATE TABLE paper_confirmation_key_binding (
    confirmation_id TEXT PRIMARY KEY
        REFERENCES paper_operation_confirmation(confirmation_id),
    key_id TEXT NOT NULL,
    public_key_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
