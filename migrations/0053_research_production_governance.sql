CREATE TABLE research_production_policy_index (
    policy_version TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE research_production_route_index (
    route_plan_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL REFERENCES research_production_policy_index(policy_version),
    registry_version TEXT NOT NULL,
    need_id TEXT NOT NULL,
    base_case_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    priority_bucket TEXT NOT NULL CHECK (priority_bucket IN ('DEFER','STANDARD','HIGH','URGENT')),
    specialist_budget INTEGER NOT NULL CHECK (specialist_budget BETWEEN 2 AND 4),
    specialist_count INTEGER NOT NULL CHECK (specialist_count BETWEEN 0 AND 4),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_version, registry_version, need_id, input_hash)
);

CREATE INDEX idx_research_production_route_company
ON research_production_route_index(company_id, created_at, route_plan_id);

CREATE TABLE skill_usage_event_index (
    usage_event_id TEXT PRIMARY KEY,
    route_plan_id TEXT NOT NULL REFERENCES research_production_route_index(route_plan_id),
    company_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    corrected_claim INTEGER NOT NULL CHECK (corrected_claim IN (0,1)),
    found_gap INTEGER NOT NULL CHECK (found_gap IN (0,1)),
    changed_driver INTEGER NOT NULL CHECK (changed_driver IN (0,1)),
    provided_falsifier INTEGER NOT NULL CHECK (provided_falsifier IN (0,1)),
    changed_ic_state INTEGER NOT NULL CHECK (changed_ic_state IN (0,1)),
    prospective_lift REAL,
    token_cost INTEGER NOT NULL CHECK (token_cost >= 0),
    object_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(route_plan_id, skill_id, usage_event_id)
);

CREATE INDEX idx_skill_usage_skill
ON skill_usage_event_index(skill_id, skill_version, created_at, usage_event_id);

CREATE TABLE catalyst_registry_index (
    catalyst_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    thesis_id TEXT NOT NULL,
    catalyst_type TEXT NOT NULL,
    expected_from TEXT NOT NULL,
    expected_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('EXPECTED','CONFIRMED','MISSED','INVALIDATED','CLOSED')),
    object_hash TEXT NOT NULL,
    catalyst_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(company_id, thesis_id, catalyst_type, expected_from, expected_to)
);

CREATE INDEX idx_catalyst_company_window
ON catalyst_registry_index(company_id, expected_from, expected_to, catalyst_id);

CREATE TABLE catalyst_monitor_index (
    monitor_id TEXT PRIMARY KEY,
    catalyst_id TEXT NOT NULL REFERENCES catalyst_registry_index(catalyst_id),
    as_of TEXT NOT NULL,
    prior_status TEXT NOT NULL,
    evaluated_status TEXT NOT NULL,
    rerun_module_count INTEGER NOT NULL CHECK (rerun_module_count >= 0),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(catalyst_id, as_of, input_hash)
);
