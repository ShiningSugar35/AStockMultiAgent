CREATE TABLE position_lifecycle_rule_index (
    rules_version TEXT PRIMARY KEY,
    action_count INTEGER NOT NULL CHECK (action_count = 5),
    object_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE position_monitoring_plan_index (
    plan_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    decision_reference_status TEXT NOT NULL,
    base_case_id TEXT NOT NULL REFERENCES base_case_pack_index(base_case_id),
    route_plan_id TEXT NOT NULL REFERENCES specialist_route_plan_index(route_plan_id),
    memo_id TEXT NOT NULL REFERENCES research_memo_index(memo_id),
    rules_version TEXT NOT NULL REFERENCES position_lifecycle_rule_index(rules_version),
    as_of TEXT NOT NULL,
    next_review_at TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    condition_count INTEGER NOT NULL CHECK (condition_count >= 1),
    baseline_evidence_count INTEGER NOT NULL CHECK (baseline_evidence_count >= 1),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(position_id, rules_version, input_hash)
);

CREATE INDEX idx_position_monitoring_plan_position
ON position_monitoring_plan_index(position_id, as_of, created_at, plan_id);

CREATE TABLE holding_evidence_update_index (
    update_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES position_monitoring_plan_index(plan_id),
    position_id TEXT NOT NULL,
    rules_version TEXT NOT NULL REFERENCES position_lifecycle_rule_index(rules_version),
    from_as_of TEXT NOT NULL,
    to_as_of TEXT NOT NULL,
    added_evidence_count INTEGER NOT NULL CHECK (added_evidence_count >= 0),
    changed_claim_count INTEGER NOT NULL CHECK (changed_claim_count >= 0),
    invalidated_evidence_count INTEGER NOT NULL CHECK (invalidated_evidence_count >= 0),
    unresolved_conflict_count INTEGER NOT NULL CHECK (unresolved_conflict_count >= 0),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, from_as_of, to_as_of, input_hash)
);

CREATE INDEX idx_holding_evidence_update_plan
ON holding_evidence_update_index(plan_id, to_as_of, update_id);

CREATE TABLE holding_review_index (
    review_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES position_monitoring_plan_index(plan_id),
    update_id TEXT NOT NULL REFERENCES holding_evidence_update_index(update_id),
    position_id TEXT NOT NULL,
    rules_version TEXT NOT NULL REFERENCES position_lifecycle_rule_index(rules_version),
    from_as_of TEXT NOT NULL,
    to_as_of TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    action_confidence REAL NOT NULL CHECK (action_confidence BETWEEN 0 AND 1),
    trigger_count INTEGER NOT NULL CHECK (trigger_count >= 0),
    hard_block_count INTEGER NOT NULL CHECK (hard_block_count >= 0),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    proposal_id TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, from_as_of, to_as_of, input_hash)
);

CREATE INDEX idx_holding_review_position
ON holding_review_index(position_id, to_as_of, review_id);

CREATE TABLE position_action_proposal_index (
    proposal_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES holding_review_index(review_id),
    plan_id TEXT NOT NULL REFERENCES position_monitoring_plan_index(plan_id),
    position_id TEXT NOT NULL,
    action TEXT NOT NULL,
    requires_user_confirmation INTEGER NOT NULL CHECK (requires_user_confirmation = 1),
    hard_block_count INTEGER NOT NULL CHECK (hard_block_count >= 0),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_position_action_proposal_position
ON position_action_proposal_index(position_id, created_at, proposal_id);
