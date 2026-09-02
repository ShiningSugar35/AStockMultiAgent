CREATE TABLE external_account (
    account_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    account_kind TEXT NOT NULL CHECK (account_kind IN ('MANUAL', 'BROKERAGE_IMPORT', 'OTHER')),
    base_currency TEXT NOT NULL CHECK (base_currency = 'CNY'),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE external_account_event (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) = 64),
    account_id TEXT NOT NULL REFERENCES external_account(account_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'TRADE',
            'CASH_DEPOSIT',
            'CASH_WITHDRAWAL',
            'CASH_ADJUSTMENT',
            'SECURITY_TRANSFER_IN',
            'SECURITY_TRANSFER_OUT',
            'REVERSAL'
        )
    ),
    occurred_at TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 0),
    available_to_system_at TEXT NOT NULL,
    market TEXT,
    symbol TEXT,
    side TEXT CHECK (side IS NULL OR side IN ('BUY', 'SELL')),
    quantity INTEGER CHECK (quantity IS NULL OR quantity > 0),
    price_cny TEXT,
    amount_cny TEXT,
    currency TEXT NOT NULL CHECK (currency = 'CNY'),
    reverses_event_id TEXT REFERENCES external_account_event(event_id),
    replaces_event_id TEXT REFERENCES external_account_event(event_id),
    source_artifact_hash TEXT CHECK (
        source_artifact_hash IS NULL OR length(source_artifact_hash) = 64
    ),
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, idempotency_key),
    CHECK (reverses_event_id IS NULL OR replaces_event_id IS NULL),
    CHECK (reverses_event_id IS NULL OR reverses_event_id <> event_id),
    CHECK (replaces_event_id IS NULL OR replaces_event_id <> event_id)
);

CREATE INDEX idx_external_account_event_account_time
    ON external_account_event(account_id, occurred_at, event_id);
CREATE INDEX idx_external_account_event_reverses
    ON external_account_event(reverses_event_id)
    WHERE reverses_event_id IS NOT NULL;
CREATE INDEX idx_external_account_event_replaces
    ON external_account_event(replaces_event_id)
    WHERE replaces_event_id IS NOT NULL;
CREATE INDEX idx_external_account_event_artifact
    ON external_account_event(source_artifact_hash)
    WHERE source_artifact_hash IS NOT NULL;

CREATE TRIGGER external_account_event_no_update
BEFORE UPDATE ON external_account_event
BEGIN
    SELECT RAISE(ABORT, 'external_account_event is append-only');
END;

CREATE TRIGGER external_account_event_no_delete
BEFORE DELETE ON external_account_event
BEGIN
    SELECT RAISE(ABORT, 'external_account_event is append-only');
END;

CREATE TABLE external_account_import_batch (
    batch_id TEXT PRIMARY KEY CHECK (length(batch_id) = 64),
    source_format TEXT NOT NULL CHECK (source_format IN ('CSV', 'JSON')),
    source_object_hash TEXT NOT NULL CHECK (length(source_object_hash) = 64),
    normalized_object_hash TEXT NOT NULL CHECK (length(normalized_object_hash) = 64),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('PREVIEWED', 'IMPORTED')),
    preview_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    imported_at TEXT
);

CREATE INDEX idx_external_account_import_batch_status
    ON external_account_import_batch(status, created_at);
