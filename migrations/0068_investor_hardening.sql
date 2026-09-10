-- Additive repair: monotone cache invalidation and recoverable notification delivery.
-- Existing economic facts, 0067 receipts and immutable migration identities are retained.
CREATE TABLE orchestration_state_revision (
    scope TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
);
INSERT INTO orchestration_state_revision(scope, generation) VALUES
    ('actual', lower(hex(randomblob(16)))),
    ('paper', lower(hex(randomblob(16)))),
    ('monitor', lower(hex(randomblob(16)))),
    ('subjects', lower(hex(randomblob(16)))),
    ('regime', lower(hex(randomblob(16))));

CREATE TABLE scheduled_notification_delivery (
    notification_id TEXT PRIMARY KEY REFERENCES scheduled_notifications(notification_id),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','SENDING','SENT')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    owner_id TEXT,
    claim_expires_at TEXT,
    last_error_class TEXT,
    delivery_reference TEXT,
    sent_at TEXT
);
INSERT INTO scheduled_notification_delivery(notification_id)
    SELECT notification_id FROM scheduled_notifications;
CREATE INDEX idx_scheduled_notification_delivery_pending
    ON scheduled_notification_delivery(status, claim_expires_at);

CREATE TRIGGER investor_revision_actual_account_insert AFTER INSERT ON external_account
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='actual'; END;
CREATE TRIGGER investor_revision_actual_account_update AFTER UPDATE ON external_account
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='actual'; END;
CREATE TRIGGER investor_revision_actual_account_delete AFTER DELETE ON external_account
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='actual'; END;
CREATE TRIGGER investor_revision_actual_event_insert AFTER INSERT ON external_account_event
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='actual'; END;
CREATE TRIGGER investor_revision_actual_event_update AFTER UPDATE ON external_account_event
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='actual'; END;
CREATE TRIGGER investor_revision_actual_event_delete AFTER DELETE ON external_account_event
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='actual'; END;

CREATE TRIGGER investor_revision_paper_account_insert AFTER INSERT ON paper_account
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_paper_account_update AFTER UPDATE ON paper_account
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_paper_account_delete AFTER DELETE ON paper_account
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_journal_insert AFTER INSERT ON journal
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_journal_update AFTER UPDATE ON journal
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_journal_delete AFTER DELETE ON journal
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_ledger_insert AFTER INSERT ON ledger_entry
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_ledger_update AFTER UPDATE ON ledger_entry
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_ledger_delete AFTER DELETE ON ledger_entry
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_position_insert AFTER INSERT ON position
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_position_update AFTER UPDATE ON position
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_position_delete AFTER DELETE ON position
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_order_insert AFTER INSERT ON order_record
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_order_update AFTER UPDATE ON order_record
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_order_delete AFTER DELETE ON order_record
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_fill_insert AFTER INSERT ON fill
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_fill_update AFTER UPDATE ON fill
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_fill_delete AFTER DELETE ON fill
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_settlement_insert AFTER INSERT ON position_settlement
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_settlement_update AFTER UPDATE ON position_settlement
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_settlement_delete AFTER DELETE ON position_settlement
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_identity_insert AFTER INSERT ON paper_position_identity
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_identity_update AFTER UPDATE ON paper_position_identity
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_identity_delete AFTER DELETE ON paper_position_identity
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_cost_insert AFTER INSERT ON paper_position_cost
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_cost_update AFTER UPDATE ON paper_position_cost
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_cost_delete AFTER DELETE ON paper_position_cost
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_binding_insert AFTER INSERT ON paper_order_rule_binding
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_binding_update AFTER UPDATE ON paper_order_rule_binding
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;
CREATE TRIGGER investor_revision_binding_delete AFTER DELETE ON paper_order_rule_binding
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='paper'; END;

CREATE TRIGGER investor_revision_monitor_event_insert AFTER INSERT ON continuous_monitor_event
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_event_update AFTER UPDATE ON continuous_monitor_event
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_event_delete AFTER DELETE ON continuous_monitor_event
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_task_insert AFTER INSERT ON continuous_monitor_task
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_task_update AFTER UPDATE ON continuous_monitor_task
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_task_delete AFTER DELETE ON continuous_monitor_task
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_target_insert AFTER INSERT ON continuous_monitor_target
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_target_update AFTER UPDATE ON continuous_monitor_target
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_monitor_target_delete AFTER DELETE ON continuous_monitor_target
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='monitor'; END;
CREATE TRIGGER investor_revision_subject_insert AFTER INSERT ON research_subject_events
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='subjects'; END;
CREATE TRIGGER investor_revision_subject_update AFTER UPDATE ON research_subject_events
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='subjects'; END;
CREATE TRIGGER investor_revision_subject_delete AFTER DELETE ON research_subject_events
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='subjects'; END;
CREATE TRIGGER investor_revision_regime_insert AFTER INSERT ON market_regime_snapshots_v2
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='regime'; END;
CREATE TRIGGER investor_revision_regime_update AFTER UPDATE ON market_regime_snapshots_v2
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='regime'; END;
CREATE TRIGGER investor_revision_regime_delete AFTER DELETE ON market_regime_snapshots_v2
BEGIN UPDATE orchestration_state_revision SET revision=revision+1 WHERE scope='regime'; END;
