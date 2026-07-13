CREATE TABLE position_settlement (
    settlement_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_account(account_id),
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    trade_date TEXT NOT NULL,
    eligible_on TEXT NOT NULL,
    settled_at TEXT,
    source_event_id TEXT NOT NULL REFERENCES journal(event_id),
    status TEXT NOT NULL
);

CREATE INDEX idx_position_settlement_pending
ON position_settlement(account_id, status, eligible_on);
