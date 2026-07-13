CREATE TABLE schema_migration (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE job (
    job_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_attempt (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES job(job_id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_class TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1))
);

CREATE TABLE cursor_state (
    cursor_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    value_json TEXT NOT NULL,
    checkpoint_hash TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, scope)
);

CREATE TABLE checkpoint (
    checkpoint_id TEXT PRIMARY KEY,
    job_id TEXT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    status TEXT NOT NULL,
    object_hash TEXT,
    committed_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_key)
);

CREATE TABLE lease_lock (
    lock_key TEXT PRIMARY KEY,
    owner_run_id TEXT NOT NULL,
    lease_until TEXT NOT NULL
);

CREATE TABLE idempotency_key (
    key TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE provider_health (
    provider_id TEXT PRIMARY KEY,
    capability_hash TEXT,
    status TEXT NOT NULL,
    last_probe_at TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error_class TEXT
);

CREATE TABLE run (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    as_of TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE artifact_registry (
    artifact_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    input_hashes_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE source_snapshot_index (
    snapshot_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    availability_at TEXT NOT NULL,
    fetch_status TEXT NOT NULL,
    UNIQUE(source_id, object_hash)
);

CREATE TABLE source_access_decision (
    decision_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    requested_capability TEXT NOT NULL,
    selected_transport TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    fallback_chain_json TEXT NOT NULL,
    request_started_at TEXT NOT NULL,
    request_finished_at TEXT,
    result_hash TEXT,
    failure_class TEXT,
    rate_limit_state TEXT NOT NULL
);

CREATE TABLE collection_scope (
    scope_id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    status TEXT NOT NULL,
    last_cursor TEXT,
    terminal_condition TEXT,
    UNIQUE(author_id, content_type)
);

CREATE TABLE collection_gap (
    gap_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES collection_scope(scope_id),
    cursor_json TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    status TEXT NOT NULL
);

CREATE TABLE paper_account (
    account_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE ledger_account (
    account_id TEXT PRIMARY KEY,
    paper_account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    account_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    normal_balance TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    paper_account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);

CREATE TABLE ledger_entry (
    entry_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES journal(event_id),
    account_id TEXT NOT NULL REFERENCES ledger_account(account_id),
    debit_fen INTEGER NOT NULL DEFAULT 0 CHECK (debit_fen >= 0),
    credit_fen INTEGER NOT NULL DEFAULT 0 CHECK (credit_fen >= 0),
    CHECK ((debit_fen > 0 AND credit_fen = 0) OR (credit_fen > 0 AND debit_fen = 0))
);

CREATE TABLE order_record (
    order_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    client_request_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
    limit_price_fen INTEGER,
    reserved_fen INTEGER NOT NULL DEFAULT 0 CHECK (reserved_fen >= 0),
    reserved_qty INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    effective_rule_version TEXT NOT NULL,
    UNIQUE(account_id, client_request_id)
);

CREATE TABLE fill (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES order_record(order_id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    price_fen INTEGER NOT NULL CHECK (price_fen > 0),
    commission_fen INTEGER NOT NULL DEFAULT 0,
    tax_fen INTEGER NOT NULL DEFAULT 0,
    transfer_fee_fen INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL,
    replay_quality TEXT NOT NULL
);

CREATE TABLE position (
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    symbol TEXT NOT NULL,
    qty_total INTEGER NOT NULL DEFAULT 0 CHECK (qty_total >= 0),
    qty_available INTEGER NOT NULL DEFAULT 0 CHECK (qty_available >= 0),
    avg_cost_fen INTEGER NOT NULL DEFAULT 0 CHECK (avg_cost_fen >= 0),
    realized_pnl_fen INTEGER NOT NULL DEFAULT 0,
    as_of_event_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, symbol)
);

CREATE TABLE replay_checkpoint (
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    symbol TEXT NOT NULL,
    requested_resolution TEXT NOT NULL,
    actual_resolution TEXT NOT NULL,
    replay_quality TEXT NOT NULL,
    provider_id TEXT,
    coverage_start TEXT,
    coverage_end TEXT,
    missing_bars INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT,
    last_event_seq INTEGER NOT NULL DEFAULT 0,
    market_cursor TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, symbol)
);

CREATE TABLE corporate_action_event (
    event_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_id TEXT NOT NULL,
    rule_version TEXT NOT NULL
);

CREATE INDEX idx_job_status ON job(status, priority, created_at);
CREATE INDEX idx_checkpoint_scope ON checkpoint(scope_type, scope_key);
CREATE INDEX idx_journal_account_seq ON journal(paper_account_id, seq);
CREATE INDEX idx_fill_order ON fill(order_id, occurred_at);
