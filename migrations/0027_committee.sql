CREATE TABLE committee_rule_index (
    rules_version TEXT PRIMARY KEY,
    rule_set_id TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE committee_assessment_index (
    assessment_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    decision_scope TEXT NOT NULL,
    as_of TEXT NOT NULL,
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 1),
    object_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(company_id, decision_scope, as_of, request_hash)
);

CREATE INDEX idx_committee_assessment_company
ON committee_assessment_index(company_id, as_of, assessment_id);

CREATE TABLE counter_case_index (
    counter_case_id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES committee_assessment_index(assessment_id),
    company_id TEXT NOT NULL,
    decision_scope TEXT NOT NULL,
    as_of TEXT NOT NULL,
    trigger_count INTEGER NOT NULL CHECK (trigger_count >= 1),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 1),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(assessment_id, input_hash)
);

CREATE TABLE committee_bundle_index (
    bundle_id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES committee_assessment_index(assessment_id),
    counter_case_id TEXT REFERENCES counter_case_index(counter_case_id),
    company_id TEXT NOT NULL,
    decision_scope TEXT NOT NULL,
    as_of TEXT NOT NULL,
    rules_version TEXT NOT NULL REFERENCES committee_rule_index(rules_version),
    engine_version TEXT NOT NULL,
    input_count INTEGER NOT NULL CHECK (input_count >= 2),
    object_hash TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_committee_bundle_company
ON committee_bundle_index(company_id, as_of, bundle_id);

CREATE TABLE committee_bundle_input_index (
    bundle_id TEXT NOT NULL REFERENCES committee_bundle_index(bundle_id),
    artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    artifact_type TEXT NOT NULL,
    artifact_role TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (bundle_id, artifact_id),
    UNIQUE (bundle_id, object_hash)
);

CREATE INDEX idx_committee_bundle_input_artifact
ON committee_bundle_input_index(artifact_id, bundle_id);

CREATE TABLE committee_decision_index (
    decision_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL REFERENCES committee_bundle_index(bundle_id),
    company_id TEXT NOT NULL,
    decision_scope TEXT NOT NULL,
    as_of TEXT NOT NULL,
    rules_version TEXT NOT NULL REFERENCES committee_rule_index(rules_version),
    engine_version TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (
        verdict IN ('REJECT','NEEDS_INFO','WATCH','PAPER_ELIGIBLE','PAPER_HOLD','PAPER_EXIT')
    ),
    hard_block_count INTEGER NOT NULL CHECK (hard_block_count >= 0),
    needs_info_count INTEGER NOT NULL CHECK (needs_info_count >= 0),
    counter_case_id TEXT REFERENCES counter_case_index(counter_case_id),
    object_hash TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(bundle_id, decision_hash)
);

CREATE INDEX idx_committee_decision_company
ON committee_decision_index(company_id, as_of, decision_id);

CREATE TABLE committee_trade_protocol_index (
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

CREATE TABLE committee_investigation_task_index (
    task_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES committee_decision_index(decision_id),
    bundle_id TEXT NOT NULL REFERENCES committee_bundle_index(bundle_id),
    reason_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED','CANCELLED')),
    resolution_artifact_id TEXT REFERENCES artifact_registry(artifact_id),
    resolution_object_hash TEXT,
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, reason_code)
);

CREATE INDEX idx_committee_task_status
ON committee_investigation_task_index(status, decision_id, task_id);
