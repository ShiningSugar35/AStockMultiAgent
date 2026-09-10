PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orchestration_schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investor_request_receipts (
    receipt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    as_of TEXT NOT NULL,
    source_revision_hash TEXT NOT NULL,
    receipt_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_subject_events (
    event_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    lane TEXT NOT NULL,
    available_at TEXT NOT NULL,
    request_id TEXT,
    artifact_id TEXT,
    reason TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_subject_events_instrument_time
    ON research_subject_events(instrument_id, available_at, event_id);
CREATE INDEX IF NOT EXISTS idx_research_subject_events_type_time
    ON research_subject_events(event_type, available_at, event_id);

CREATE TRIGGER IF NOT EXISTS research_subject_events_no_update
BEFORE UPDATE ON research_subject_events
BEGIN
    SELECT RAISE(ABORT, 'research_subject_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS research_subject_events_no_delete
BEFORE DELETE ON research_subject_events
BEGIN
    SELECT RAISE(ABORT, 'research_subject_events is append-only');
END;

CREATE TABLE IF NOT EXISTS provisional_position_assertions (
    assertion_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    quantity TEXT NOT NULL,
    asserted_at TEXT NOT NULL,
    date_precision TEXT NOT NULL,
    source_text TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provisional_positions_account_instrument
    ON provisional_position_assertions(account_id, instrument_id, asserted_at);

CREATE TRIGGER IF NOT EXISTS provisional_position_assertions_no_update
BEFORE UPDATE ON provisional_position_assertions
BEGIN
    SELECT RAISE(ABORT, 'provisional_position_assertions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS provisional_position_assertions_no_delete
BEFORE DELETE ON provisional_position_assertions
BEGIN
    SELECT RAISE(ABORT, 'provisional_position_assertions is append-only');
END;

CREATE TABLE IF NOT EXISTS estimated_cost_basis_ranges (
    estimate_id TEXT PRIMARY KEY,
    assertion_id TEXT NOT NULL,
    low TEXT NOT NULL,
    high TEXT NOT NULL,
    currency TEXT NOT NULL,
    method TEXT NOT NULL,
    as_of TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(assertion_id) REFERENCES provisional_position_assertions(assertion_id)
);

CREATE TRIGGER IF NOT EXISTS estimated_cost_basis_ranges_no_update
BEFORE UPDATE ON estimated_cost_basis_ranges
BEGIN
    SELECT RAISE(ABORT, 'estimated_cost_basis_ranges is append-only');
END;

CREATE TRIGGER IF NOT EXISTS estimated_cost_basis_ranges_no_delete
BEFORE DELETE ON estimated_cost_basis_ranges
BEGIN
    SELECT RAISE(ABORT, 'estimated_cost_basis_ranges is append-only');
END;

CREATE TABLE IF NOT EXISTS capability_execution_plans (
    plan_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    plan_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capability_execution_plans_request
    ON capability_execution_plans(request_id, created_at);

CREATE TABLE IF NOT EXISTS capability_coverage_receipts (
    receipt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    receipt_hash TEXT NOT NULL UNIQUE,
    coverage_complete INTEGER NOT NULL CHECK (coverage_complete IN (0, 1)),
    required_coverage REAL NOT NULL,
    prohibited_call_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES capability_execution_plans(plan_id)
);
CREATE INDEX IF NOT EXISTS idx_capability_coverage_receipts_request
    ON capability_coverage_receipts(request_id, created_at);

CREATE TABLE IF NOT EXISTS macro_release_snapshots_v2 (
    release_id TEXT PRIMARY KEY,
    authority TEXT NOT NULL,
    release_family TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(authority, release_family, source_hash)
);
CREATE INDEX IF NOT EXISTS idx_macro_release_snapshots_v2_family_time
    ON macro_release_snapshots_v2(authority, release_family, published_at);

CREATE TABLE IF NOT EXISTS market_regime_snapshots_v2 (
    snapshot_id TEXT PRIMARY KEY,
    feature_snapshot_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    selected_state TEXT NOT NULL,
    confidence REAL NOT NULL,
    coverage REAL NOT NULL,
    valid_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_regime_snapshots_v2_validity
    ON market_regime_snapshots_v2(valid_from, expires_at, as_of);

CREATE TABLE IF NOT EXISTS regime_decision_overlays (
    overlay_id TEXT PRIMARY KEY,
    regime_snapshot_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(regime_snapshot_id) REFERENCES market_regime_snapshots_v2(snapshot_id)
);

CREATE TABLE IF NOT EXISTS scheduled_research_policies (
    policy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    policy_hash TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(policy_id, version)
);

CREATE TABLE IF NOT EXISTS scheduled_task_bindings (
    binding_id TEXT PRIMARY KEY,
    platform_task_id TEXT,
    creation_mode TEXT NOT NULL,
    execution_surface TEXT NOT NULL,
    schedule_expression TEXT NOT NULL,
    timezone TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_task_bindings_platform_task
    ON scheduled_task_bindings(platform_task_id)
    WHERE platform_task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS scheduled_task_binding_events (
    event_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(binding_id) REFERENCES scheduled_task_bindings(binding_id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_binding_events_binding_time
    ON scheduled_task_binding_events(binding_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS scheduled_task_binding_events_no_update
BEFORE UPDATE ON scheduled_task_binding_events
BEGIN
    SELECT RAISE(ABORT, 'scheduled_task_binding_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS scheduled_task_binding_events_no_delete
BEFORE DELETE ON scheduled_task_binding_events
BEGIN
    SELECT RAISE(ABORT, 'scheduled_task_binding_events is append-only');
END;

CREATE TABLE IF NOT EXISTS scheduled_research_runs (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    binding_id TEXT NOT NULL,
    schedule_bucket TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    next_watermark TEXT,
    notification_required INTEGER NOT NULL CHECK (notification_required IN (0, 1)),
    economic_write_count INTEGER NOT NULL CHECK (economic_write_count = 0),
    receipt_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY(binding_id) REFERENCES scheduled_task_bindings(binding_id),
    UNIQUE(binding_id, schedule_bucket)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_research_runs_binding_time
    ON scheduled_research_runs(binding_id, completed_at);

CREATE TABLE IF NOT EXISTS watchlist_analysis_revisions (
    revision_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    thesis_status TEXT NOT NULL,
    previous_revision_id TEXT,
    revision_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(previous_revision_id) REFERENCES watchlist_analysis_revisions(revision_id)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_analysis_revisions_instrument
    ON watchlist_analysis_revisions(instrument_id, as_of, revision_id);

CREATE TRIGGER IF NOT EXISTS watchlist_analysis_revisions_no_update
BEFORE UPDATE ON watchlist_analysis_revisions
BEGIN
    SELECT RAISE(ABORT, 'watchlist_analysis_revisions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS watchlist_analysis_revisions_no_delete
BEFORE DELETE ON watchlist_analysis_revisions
BEGIN
    SELECT RAISE(ABORT, 'watchlist_analysis_revisions is append-only');
END;

CREATE TABLE IF NOT EXISTS scheduled_notifications (
    notification_id TEXT PRIMARY KEY,
    notification_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_shadow_observations (
    observation_id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    request_or_run_id TEXT NOT NULL,
    label_change_only_action INTEGER NOT NULL CHECK (label_change_only_action IN (0, 1)),
    required_capability_coverage REAL NOT NULL,
    prohibited_call_count INTEGER NOT NULL,
    economic_write_count INTEGER NOT NULL,
    material_error INTEGER NOT NULL CHECK (material_error IN (0, 1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(feature_id, request_or_run_id)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_shadow_observations_feature_time
    ON orchestration_shadow_observations(feature_id, observed_at);

CREATE TABLE IF NOT EXISTS controlled_live_checks (
    check_id TEXT PRIMARY KEY,
    check_type TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_controlled_live_checks_type_time
    ON controlled_live_checks(check_type, checked_at);

CREATE TABLE IF NOT EXISTS feature_activation_receipts (
    receipt_id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    assessment_id TEXT,
    owner_approval_id TEXT,
    ledger_write_count INTEGER NOT NULL CHECK (ledger_write_count = 0),
    receipt_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feature_activation_receipts_feature_time
    ON feature_activation_receipts(feature_id, changed_at);

CREATE TABLE IF NOT EXISTS material_change_digests (
    digest_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    severity TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_material_change_digests_run_domain
    ON material_change_digests(run_id, domain, severity);
