CREATE TABLE shadow_policy_index (
    policy_version TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE shadow_study_index (
    study_id TEXT PRIMARY KEY,
    study_name TEXT NOT NULL,
    study_mode TEXT NOT NULL CHECK (
        study_mode IN ('FORWARD_FORMAL','EXPLORATORY_RETROSPECTIVE')
    ),
    effective_from TEXT NOT NULL,
    observation_end TEXT,
    candidate_policy_id TEXT NOT NULL,
    candidate_policy_version TEXT NOT NULL,
    candidate_set_id TEXT NOT NULL,
    policy_version TEXT NOT NULL REFERENCES shadow_policy_index(policy_version),
    engine_version TEXT NOT NULL,
    evidence_status TEXT NOT NULL CHECK (
        evidence_status IN (
            'COLLECTING','INSUFFICIENT_SAMPLE','PROVISIONAL',
            'EVIDENCE_READY','FAILED_INTEGRITY','CLOSED'
        )
    ),
    arm_count INTEGER NOT NULL CHECK (arm_count >= 1),
    object_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    study_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(study_name, candidate_set_id, policy_version, request_hash)
);

CREATE INDEX idx_shadow_study_status
ON shadow_study_index(evidence_status, effective_from, study_id);

CREATE TABLE shadow_arm_index (
    arm_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    arm_key TEXT NOT NULL,
    arm_type TEXT NOT NULL,
    research_status TEXT NOT NULL,
    specialist_skill_id TEXT,
    specialist_skill_version TEXT,
    benchmark_symbol TEXT,
    object_hash TEXT NOT NULL,
    arm_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(study_id, arm_key),
    UNIQUE(study_id, arm_type, specialist_skill_id, specialist_skill_version)
);

CREATE INDEX idx_shadow_arm_study
ON shadow_arm_index(study_id, arm_type, arm_id);

CREATE TABLE shadow_assignment_index (
    assignment_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    candidate_set_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    independence_key TEXT NOT NULL,
    thesis_version TEXT NOT NULL,
    event_id TEXT NOT NULL,
    trade_protocol_id TEXT NOT NULL,
    arm_signal_count INTEGER NOT NULL CHECK (arm_signal_count >= 1),
    object_hash TEXT NOT NULL,
    assignment_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(study_id, independence_key)
);

CREATE INDEX idx_shadow_assignment_time
ON shadow_assignment_index(study_id, signal_time, assignment_id);

CREATE TABLE shadow_assignment_input_index (
    assignment_id TEXT NOT NULL REFERENCES shadow_assignment_index(assignment_id),
    artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    artifact_type TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (assignment_id, artifact_id),
    UNIQUE (assignment_id, object_hash)
);

CREATE TABLE market_regime_index (
    regime_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    regime_rule_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    regime TEXT NOT NULL CHECK (
        regime IN (
            'PANIC','HIGH_VOL_BULL','TREND_BULL','TREND_BEAR','RANGE','UNCLASSIFIED'
        )
    ),
    feature_snapshot_id TEXT NOT NULL,
    feature_snapshot_hash TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    regime_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(study_id, as_of, feature_snapshot_hash, regime_rule_version)
);

CREATE INDEX idx_market_regime_study
ON market_regime_index(study_id, as_of, regime_id);

CREATE TABLE shadow_observation_index (
    observation_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    assignment_id TEXT NOT NULL REFERENCES shadow_assignment_index(assignment_id),
    arm_id TEXT NOT NULL REFERENCES shadow_arm_index(arm_id),
    regime_id TEXT NOT NULL REFERENCES market_regime_index(regime_id),
    independence_key TEXT NOT NULL,
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (5,20,60)),
    observation_status TEXT NOT NULL CHECK (
        observation_status IN ('PENDING_MATURITY','MATURE','EXCLUDED')
    ),
    formal_eligible INTEGER NOT NULL CHECK (formal_eligible IN (0,1)),
    signal_time TEXT NOT NULL,
    valuation_time TEXT,
    replay_quality TEXT NOT NULL,
    net_pnl_fen INTEGER NOT NULL,
    object_hash TEXT NOT NULL,
    observation_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(assignment_id, arm_id, horizon_days)
);

CREATE INDEX idx_shadow_observation_study
ON shadow_observation_index(
    study_id, horizon_days, observation_status, formal_eligible, observation_id
);

CREATE TABLE shadow_evaluation_run_index (
    run_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    as_of TEXT NOT NULL,
    policy_version TEXT NOT NULL REFERENCES shadow_policy_index(policy_version),
    statistics_version TEXT NOT NULL,
    run_status TEXT NOT NULL CHECK (
        run_status IN ('RUNNING','COMPLETED','FAILED','RECOVERABLE')
    ),
    input_hash TEXT NOT NULL,
    report_id TEXT,
    report_object_hash TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(study_id, as_of, input_hash)
);

CREATE TABLE shadow_report_index (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES shadow_evaluation_run_index(run_id),
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    evidence_status TEXT NOT NULL,
    assignment_count INTEGER NOT NULL CHECK (assignment_count >= 0),
    mature_observation_count INTEGER NOT NULL CHECK (mature_observation_count >= 0),
    independent_decision_count INTEGER NOT NULL CHECK (independent_decision_count >= 0),
    comparison_count INTEGER NOT NULL CHECK (comparison_count >= 0),
    object_hash TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_shadow_report_study
ON shadow_report_index(study_id, created_at, report_id);

CREATE TABLE phase8_admission_index (
    admission_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES shadow_study_index(study_id),
    report_id TEXT NOT NULL UNIQUE REFERENCES shadow_report_index(report_id),
    admission_status TEXT NOT NULL CHECK (
        admission_status IN (
            'ELIGIBLE_RULE_STATE_MACHINE_RESEARCH',
            'NOT_ELIGIBLE_INSUFFICIENT_SAMPLE',
            'NOT_ELIGIBLE_INTEGRITY',
            'NOT_ELIGIBLE_NO_INCREMENT'
        )
    ),
    object_hash TEXT NOT NULL,
    admission_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_phase8_admission_study
ON phase8_admission_index(study_id, created_at, admission_id);
