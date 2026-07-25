CREATE TABLE paper_operation_request (
    operation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    operation_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL UNIQUE,
    request_object_hash TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, operation_type, idempotency_key)
);

CREATE TABLE paper_operation_confirmation (
    confirmation_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES paper_operation_request(operation_id),
    request_hash TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmation_hash TEXT NOT NULL UNIQUE,
    confirmation_object_hash TEXT NOT NULL,
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    signature_algorithm TEXT NOT NULL CHECK(signature_algorithm IN ('ED25519','ECDSA_P256_SHA256')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE paper_confirmation_nonce (
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES paper_operation_request(operation_id),
    confirmation_id TEXT NOT NULL REFERENCES paper_operation_confirmation(confirmation_id),
    consumed_at TEXT NOT NULL,
    PRIMARY KEY(key_id, nonce),
    UNIQUE(operation_id, confirmation_id)
);

CREATE TABLE paper_operation_execution (
    operation_id TEXT PRIMARY KEY REFERENCES paper_operation_request(operation_id),
    status TEXT NOT NULL CHECK(status IN (
        'PLANNED','VALIDATED','COMMITTED','COMPLETE','REJECTED','NEEDS_INFO',
        'INTERRUPTED','RECOVERED'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    result_json TEXT,
    result_hash TEXT,
    report_object_hash TEXT,
    completed_at TEXT,
    CHECK ((status IN ('COMMITTED','COMPLETE','RECOVERED')) = (result_hash IS NOT NULL)),
    CHECK ((status IN ('COMPLETE','RECOVERED')) = (report_object_hash IS NOT NULL))
);

CREATE TABLE paper_operation_transition (
    operation_id TEXT NOT NULL REFERENCES paper_operation_request(operation_id),
    transition_seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PLANNED','VALIDATED','COMMITTED','COMPLETE','REJECTED','NEEDS_INFO',
        'INTERRUPTED','RECOVERED'
    )),
    reason_code TEXT,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(operation_id, transition_seq)
);

CREATE TABLE paper_order_rule_binding (
    order_id TEXT PRIMARY KEY REFERENCES order_record(order_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES paper_operation_request(operation_id),
    market TEXT NOT NULL CHECK(market IN ('XSHG','XSHE','BJSE')),
    symbol TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    board TEXT NOT NULL CHECK(board IN ('MAIN','STAR','CHINEXT','BSE')),
    risk_status TEXT NOT NULL CHECK(risk_status IN ('NORMAL','RISK_WARNING')),
    trading_rule_version TEXT NOT NULL,
    validity TEXT NOT NULL CHECK(validity IN ('DAY','GTC')),
    expires_at TEXT,
    calendar_release_id TEXT NOT NULL,
    instrument_release_id TEXT NOT NULL,
    daily_release_id TEXT NOT NULL,
    fee_rule_version TEXT NOT NULL,
    fee_schedule_hash TEXT NOT NULL,
    confirmation_id TEXT NOT NULL REFERENCES paper_operation_confirmation(confirmation_id),
    authorization_key_id TEXT NOT NULL,
    confirmation_hash TEXT NOT NULL,
    previous_close_fen INTEGER NOT NULL CHECK (previous_close_fen > 0),
    price_limit_bps INTEGER NOT NULL CHECK (price_limit_bps > 0),
    is_st INTEGER NOT NULL CHECK (is_st IN (0, 1)),
    CHECK(instrument_id = market || ':' || symbol),
    CHECK((validity='DAY' AND expires_at IS NOT NULL) OR (validity='GTC' AND expires_at IS NULL))
);

CREATE TABLE paper_order_transition (
    order_id TEXT NOT NULL REFERENCES order_record(order_id),
    transition_seq INTEGER NOT NULL,
    from_status TEXT CHECK(from_status IS NULL OR from_status IN (
        'NEW','ACCEPTED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','EXPIRED'
    )),
    to_status TEXT NOT NULL CHECK(to_status IN (
        'NEW','ACCEPTED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','EXPIRED'
    )),
    source_operation_id TEXT REFERENCES paper_operation_request(operation_id),
    source_bar_commit_id TEXT,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(order_id, transition_seq)
);

CREATE TABLE paper_fee_schedule_release (
    rule_version TEXT PRIMARY KEY,
    schedule_hash TEXT NOT NULL UNIQUE,
    schedule_object_hash TEXT NOT NULL,
    schedule_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    markets_json TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE paper_mark_snapshot (
    mark_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES paper_operation_request(operation_id),
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    as_of TEXT NOT NULL,
    prices_json TEXT NOT NULL,
    release_ids_json TEXT NOT NULL,
    nav_json TEXT NOT NULL,
    nav_hash TEXT NOT NULL UNIQUE,
    snapshot_object_hash TEXT NOT NULL
);

CREATE TABLE paper_settlement_policy (
    operation_id TEXT PRIMARY KEY REFERENCES paper_operation_request(operation_id),
    calendar_release_id TEXT NOT NULL,
    open_sessions_hash TEXT NOT NULL,
    policy_object_hash TEXT NOT NULL,
    as_of TEXT NOT NULL
);

CREATE TABLE paper_recovery_snapshot (
    recovery_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES paper_operation_request(operation_id),
    status TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL UNIQUE,
    snapshot_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE paper_fee_accrual (
    order_id TEXT PRIMARY KEY REFERENCES order_record(order_id),
    gross_fen INTEGER NOT NULL DEFAULT 0 CHECK (gross_fen >= 0),
    commission_fen INTEGER NOT NULL DEFAULT 0 CHECK (commission_fen >= 0),
    tax_fen INTEGER NOT NULL DEFAULT 0 CHECK (tax_fen >= 0),
    transfer_fee_fen INTEGER NOT NULL DEFAULT 0 CHECK (transfer_fee_fen >= 0),
    fee_rule_version TEXT NOT NULL,
    fee_schedule_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE paper_replay_bar_commit (
    commit_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    market TEXT NOT NULL CHECK(market IN ('XSHG','XSHE','BJSE')),
    instrument_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bar_observation_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    commit_object_hash TEXT NOT NULL,
    fill_ids_json TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    UNIQUE(account_id, market, instrument_id, bar_observation_id),
    CHECK(instrument_id = market || ':' || symbol)
);

CREATE TABLE paper_position_identity (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('XSHG','XSHE','BJSE')),
    instrument_id TEXT NOT NULL,
    PRIMARY KEY(account_id, symbol),
    FOREIGN KEY(account_id, symbol) REFERENCES position(account_id, symbol),
    UNIQUE(account_id, instrument_id),
    CHECK(instrument_id = market || ':' || symbol)
);

CREATE TABLE paper_settlement_identity (
    settlement_id TEXT PRIMARY KEY REFERENCES position_settlement(settlement_id),
    market TEXT NOT NULL CHECK(market IN ('XSHG','XSHE','BJSE')),
    instrument_id TEXT NOT NULL
);

CREATE TABLE paper_position_cost (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    total_cost_fen INTEGER NOT NULL CHECK(total_cost_fen >= 0),
    PRIMARY KEY(account_id, symbol),
    FOREIGN KEY(account_id, symbol) REFERENCES position(account_id, symbol)
);

CREATE TABLE paper_position_lot (
    lot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    remaining_qty INTEGER NOT NULL CHECK(remaining_qty >= 0),
    total_cost_fen INTEGER NOT NULL CHECK(total_cost_fen >= 0),
    source_fill_id TEXT REFERENCES fill(fill_id),
    source_action_id TEXT REFERENCES corporate_action_event(event_id),
    FOREIGN KEY(account_id, symbol) REFERENCES position(account_id, symbol),
    CHECK((source_fill_id IS NULL) <> (source_action_id IS NULL))
);

CREATE TABLE paper_corporate_action_application (
    action_observation_id TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    release_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES journal(event_id),
    application_hash TEXT NOT NULL,
    application_object_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY(action_observation_id, account_id)
);

INSERT INTO ledger_account(
    account_id,paper_account_id,account_type,currency,normal_balance,status
)
SELECT account_id || ':DIVIDEND_INCOME',account_id,'INCOME','CNY','CREDIT','OPEN'
FROM paper_account;

CREATE INDEX idx_paper_operation_account
ON paper_operation_request(account_id, requested_at, operation_id);

CREATE INDEX idx_paper_order_expiry
ON paper_order_rule_binding(validity, expires_at);
