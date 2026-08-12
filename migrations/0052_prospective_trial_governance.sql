CREATE TABLE prospective_governance_config_index (
    config_version TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    effective_from TEXT NOT NULL,
    independence_contract_version TEXT NOT NULL,
    market_regime_rule_version TEXT NOT NULL,
    statistics_version TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(study_id, config_id, config_hash)
);

CREATE TABLE prospective_trial_event_index (
    trial_event_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    config_version TEXT NOT NULL REFERENCES prospective_governance_config_index(config_version),
    research_trial_id TEXT NOT NULL,
    funnel_event_id TEXT NOT NULL,
    company_id TEXT,
    decision_time TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN ('SEED','PROMOTION','CANDIDATE','COMMITTEE','FORMAL_ASSIGNMENT')
    ),
    outcome TEXT NOT NULL,
    independence_unit_id TEXT NOT NULL,
    formal_assignment_id TEXT,
    formal_trade_event INTEGER NOT NULL DEFAULT 0 CHECK (formal_trade_event = 0),
    frozen_input_set_hash TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    trial_event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    UNIQUE(study_id, funnel_event_id),
    UNIQUE(study_id, trial_event_hash)
);

CREATE INDEX idx_prospective_trial_study_time
ON prospective_trial_event_index(study_id, decision_time, trial_event_id);

CREATE INDEX idx_prospective_trial_independence
ON prospective_trial_event_index(study_id, independence_unit_id, decision_time);

CREATE TABLE prospective_trial_cluster_index (
    trial_event_id TEXT NOT NULL REFERENCES prospective_trial_event_index(trial_event_id),
    cluster_type TEXT NOT NULL CHECK (
        cluster_type IN ('STOCK','INDUSTRY','THEME','DECISION_DATE','SHARED_CATALYST')
    ),
    cluster_id TEXT NOT NULL,
    PRIMARY KEY (trial_event_id, cluster_type, cluster_id)
);

CREATE INDEX idx_prospective_cluster_lookup
ON prospective_trial_cluster_index(cluster_type, cluster_id, trial_event_id);

CREATE TABLE prospective_trial_input_index (
    trial_event_id TEXT NOT NULL REFERENCES prospective_trial_event_index(trial_event_id),
    artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    artifact_type TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    available_at TEXT NOT NULL,
    PRIMARY KEY (trial_event_id, artifact_id)
);

CREATE TABLE prospective_statistics_plan_index (
    plan_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    config_version TEXT NOT NULL REFERENCES prospective_governance_config_index(config_version),
    independence_unit_count INTEGER NOT NULL CHECK (independence_unit_count >= 0),
    independence_sample_floor_reached INTEGER NOT NULL CHECK (
        independence_sample_floor_reached IN (0,1)
    ),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(study_id, config_version, input_hash)
);
