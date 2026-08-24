CREATE TABLE continuous_monitor_target (
    target_id TEXT PRIMARY KEY,
    market TEXT NOT NULL CHECK (market IN ('XSHG','XSHE','BJSE','INDEX')),
    symbol TEXT NOT NULL,
    company_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','PAUSED','REMOVED')),
    object_hash TEXT NOT NULL,
    enrolled_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_price REAL,
    high_watermark_price REAL,
    last_market_at TEXT,
    last_review_at TEXT,
    UNIQUE(market, symbol)
);

CREATE INDEX idx_continuous_monitor_target_active
ON continuous_monitor_target(status, market, symbol, target_id);

CREATE TABLE continuous_monitor_rule (
    rule_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES continuous_monitor_target(target_id),
    metric TEXT NOT NULL CHECK (metric IN ('LAST_PRICE','RETURN_1D','RETURN_5D','DRAWDOWN_FROM_WATCH_HIGH','VOLUME_RATIO','DAYS_SINCE_REVIEW')),
    comparison TEXT NOT NULL CHECK (comparison IN ('GT','GE','LT','LE','EQ')),
    threshold REAL NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('OBSERVE','REVIEW','ENTER_PAPER_CANDIDATE','ADD_REVIEW','TRIM_REVIEW','EXIT_REVIEW')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WATCH','MATERIAL','CRITICAL')),
    cooldown_seconds INTEGER NOT NULL CHECK (cooldown_seconds >= 0),
    affected_modules_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0,1)),
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_triggered_at TEXT
);

CREATE INDEX idx_continuous_monitor_rule_target
ON continuous_monitor_rule(target_id, active, metric, rule_id);

CREATE TABLE continuous_monitor_event (
    event_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES continuous_monitor_target(target_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('PRICE_BAR_UPDATED','PRICE_TRIGGER','DRAWDOWN_TRIGGER','OFFICIAL_DISCLOSURE','NEWS_LEAD','CATALYST_DUE','CATALYST_CHANGED','SCHEDULED_REVIEW_DUE','PAPER_REPLAY_DUE','DATA_SOURCE_DEGRADED','RESEARCH_TASK_CREATED')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WATCH','MATERIAL','CRITICAL')),
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    payload_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    affected_modules_json TEXT NOT NULL,
    requires_research INTEGER NOT NULL CHECK (requires_research IN (0,1)),
    object_hash TEXT NOT NULL,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_continuous_monitor_event_unacked
ON continuous_monitor_event(acknowledged_at, severity, available_at, event_id);
CREATE INDEX idx_continuous_monitor_event_target
ON continuous_monitor_event(target_id, available_at, event_id);

CREATE TABLE continuous_monitor_task (
    task_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES continuous_monitor_event(event_id),
    target_id TEXT NOT NULL REFERENCES continuous_monitor_target(target_id),
    company_id TEXT NOT NULL,
    requested_modules_json TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('LOW','NORMAL','HIGH','URGENT')),
    status TEXT NOT NULL CHECK (status IN ('PENDING','CLAIMED','COMPLETED','FAILED')),
    object_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claimed_by TEXT,
    claim_expires_at TEXT,
    last_error TEXT,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_continuous_monitor_task_pending
ON continuous_monitor_task(status, priority, available_at, task_id);

CREATE TABLE continuous_monitor_run (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED','PARTIAL','FAILED')),
    live INTEGER NOT NULL CHECK (live IN (0,1)),
    target_count INTEGER NOT NULL CHECK (target_count >= 0),
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    task_count INTEGER NOT NULL CHECK (task_count >= 0),
    source_success_json TEXT NOT NULL,
    source_failure_json TEXT NOT NULL,
    object_hash TEXT NOT NULL
);

CREATE INDEX idx_continuous_monitor_run_time
ON continuous_monitor_run(started_at, run_id);

CREATE TABLE continuous_monitor_source_cursor (
    target_id TEXT NOT NULL REFERENCES continuous_monitor_target(target_id),
    source TEXT NOT NULL CHECK (source IN ('MARKET_60M','CNINFO','GDELT','CATALYST','SCHEDULE','PAPER')),
    cursor TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    retry_after TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(target_id, source)
);

CREATE TABLE continuous_monitor_daemon (
    singleton_id TEXT PRIMARY KEY CHECK (singleton_id = 'default'),
    owner_id TEXT,
    pid INTEGER,
    state TEXT NOT NULL CHECK (state IN ('STOPPED','RUNNING','STOPPING','FAILED')),
    started_at TEXT,
    heartbeat_at TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0 CHECK (stop_requested IN (0,1)),
    last_run_id TEXT,
    details_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO continuous_monitor_daemon(
    singleton_id, owner_id, pid, state, started_at, heartbeat_at,
    stop_requested, last_run_id, details_json, updated_at
) VALUES (
    'default', NULL, NULL, 'STOPPED', NULL, NULL, 0, NULL, '{}',
    strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
);
