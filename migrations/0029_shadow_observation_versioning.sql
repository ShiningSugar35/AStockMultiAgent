ALTER TABLE shadow_observation_index RENAME TO shadow_observation_index_v1;

CREATE TABLE shadow_observation_index (
    observation_id TEXT PRIMARY KEY,
    observation_version TEXT NOT NULL,
    supersedes_observation_id TEXT REFERENCES shadow_observation_index(observation_id),
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
    UNIQUE(assignment_id, arm_id, horizon_days, observation_version)
);

INSERT INTO shadow_observation_index(
    observation_id,observation_version,supersedes_observation_id,study_id,assignment_id,
    arm_id,regime_id,independence_key,horizon_days,observation_status,formal_eligible,
    signal_time,valuation_time,replay_quality,net_pnl_fen,object_hash,observation_hash,created_at
)
SELECT
    observation_id,'legacy-v1',NULL,study_id,assignment_id,arm_id,regime_id,
    independence_key,horizon_days,observation_status,formal_eligible,signal_time,
    valuation_time,replay_quality,net_pnl_fen,object_hash,observation_hash,created_at
FROM shadow_observation_index_v1;

DROP TABLE shadow_observation_index_v1;

CREATE INDEX idx_shadow_observation_study
ON shadow_observation_index(
    study_id, horizon_days, observation_status, formal_eligible, observation_id
);

CREATE INDEX idx_shadow_observation_series
ON shadow_observation_index(
    assignment_id, arm_id, horizon_days, created_at, observation_id
);
