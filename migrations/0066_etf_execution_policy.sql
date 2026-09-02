-- R-02: exact instrument-specific ETF paper execution bindings.
-- Migration 0065 is reserved by R-03; R-02 therefore starts at 0066.

ALTER TABLE order_record ADD COLUMN limit_price_milli_yuan INTEGER
    CHECK(limit_price_milli_yuan IS NULL OR limit_price_milli_yuan > 0);

ALTER TABLE fill ADD COLUMN price_milli_yuan INTEGER
    CHECK(price_milli_yuan IS NULL OR price_milli_yuan > 0);

ALTER TABLE paper_settlement_identity ADD COLUMN settlement_cycle TEXT NOT NULL DEFAULT 'T1'
    CHECK(settlement_cycle IN ('T0','T1'));

CREATE TABLE paper_order_rule_binding_v2 (
    order_id TEXT PRIMARY KEY REFERENCES order_record(order_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES paper_operation_request(operation_id),
    market TEXT NOT NULL CHECK(market IN ('XSHG','XSHE','BJSE')),
    symbol TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    instrument_type TEXT NOT NULL CHECK(instrument_type IN ('STOCK','ETF')),
    board TEXT NOT NULL CHECK(board IN ('MAIN','STAR','CHINEXT','BSE','ETF')),
    risk_status TEXT NOT NULL CHECK(risk_status IN ('NORMAL','RISK_WARNING')),
    trading_rule_version TEXT NOT NULL,
    validity TEXT NOT NULL CHECK(validity IN ('DAY','GTC')),
    expires_at TEXT,
    calendar_release_id TEXT NOT NULL,
    instrument_release_id TEXT NOT NULL,
    daily_release_id TEXT NOT NULL,
    fee_rule_version TEXT NOT NULL,
    fee_schedule_hash TEXT NOT NULL,
    price_limit_rule_version TEXT,
    execution_policy_rule_version TEXT,
    execution_policy_hash TEXT,
    buy_lot_size INTEGER NOT NULL CHECK(buy_lot_size > 0),
    sell_lot_size INTEGER NOT NULL CHECK(sell_lot_size > 0),
    allow_odd_lot_full_exit INTEGER NOT NULL CHECK(allow_odd_lot_full_exit IN (0,1)),
    tick_size_milli_yuan INTEGER NOT NULL CHECK(tick_size_milli_yuan > 0),
    settlement_cycle TEXT NOT NULL CHECK(settlement_cycle IN ('T0','T1')),
    confirmation_id TEXT NOT NULL REFERENCES paper_operation_confirmation(confirmation_id),
    authorization_key_id TEXT NOT NULL,
    confirmation_hash TEXT NOT NULL,
    previous_close_fen INTEGER NOT NULL CHECK(previous_close_fen > 0),
    previous_close_milli_yuan INTEGER NOT NULL CHECK(previous_close_milli_yuan > 0),
    price_limit_bps INTEGER NOT NULL CHECK(price_limit_bps > 0),
    is_st INTEGER NOT NULL CHECK(is_st IN (0,1)),
    CHECK(instrument_id = market || ':' || symbol),
    CHECK((validity='DAY' AND expires_at IS NOT NULL) OR (validity='GTC' AND expires_at IS NULL)),
    CHECK((instrument_type='STOCK' AND board IN ('MAIN','STAR','CHINEXT','BSE')) OR
          (instrument_type='ETF' AND board='ETF')),
    CHECK((instrument_type='STOCK' AND execution_policy_rule_version IS NULL AND execution_policy_hash IS NULL) OR
          (instrument_type='ETF' AND execution_policy_rule_version IS NOT NULL AND execution_policy_hash IS NOT NULL))
);

INSERT INTO paper_order_rule_binding_v2(
    order_id,operation_id,market,symbol,instrument_id,instrument_type,board,risk_status,
    trading_rule_version,validity,expires_at,calendar_release_id,instrument_release_id,
    daily_release_id,fee_rule_version,fee_schedule_hash,price_limit_rule_version,
    execution_policy_rule_version,execution_policy_hash,buy_lot_size,sell_lot_size,
    allow_odd_lot_full_exit,tick_size_milli_yuan,settlement_cycle,confirmation_id,
    authorization_key_id,confirmation_hash,previous_close_fen,previous_close_milli_yuan,
    price_limit_bps,is_st
)
SELECT
    order_id,operation_id,market,symbol,instrument_id,'STOCK',board,risk_status,
    trading_rule_version,validity,expires_at,calendar_release_id,instrument_release_id,
    daily_release_id,fee_rule_version,fee_schedule_hash,NULL,NULL,NULL,
    100,100,0,10,'T1',confirmation_id,authorization_key_id,confirmation_hash,
    previous_close_fen,previous_close_fen * 10,price_limit_bps,is_st
FROM paper_order_rule_binding;

DROP TABLE paper_order_rule_binding;
ALTER TABLE paper_order_rule_binding_v2 RENAME TO paper_order_rule_binding;

CREATE INDEX idx_paper_order_expiry
ON paper_order_rule_binding(validity, expires_at);
CREATE INDEX idx_paper_order_instrument_policy
ON paper_order_rule_binding(instrument_type, market, instrument_id, execution_policy_rule_version);

CREATE TABLE etf_execution_policy_release (
    rule_version TEXT PRIMARY KEY,
    policy_hash TEXT NOT NULL UNIQUE,
    policy_object_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    execution_enabled INTEGER NOT NULL CHECK(execution_enabled IN (0,1)),
    registered_at TEXT NOT NULL
);
