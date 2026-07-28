ALTER TABLE shadow_study_index
ADD COLUMN registered_at TEXT;

ALTER TABLE shadow_study_index
ADD COLUMN prospective_eligible INTEGER NOT NULL DEFAULT 0
CHECK (prospective_eligible IN (0, 1));

UPDATE shadow_study_index
SET registered_at = created_at
WHERE registered_at IS NULL;

ALTER TABLE shadow_assignment_index
ADD COLUMN research_memo_id TEXT;

ALTER TABLE shadow_assignment_index
ADD COLUMN decision_id TEXT;

ALTER TABLE shadow_assignment_index
ADD COLUMN registered_at TEXT;

ALTER TABLE shadow_assignment_index
ADD COLUMN prospective_eligible INTEGER NOT NULL DEFAULT 0
CHECK (prospective_eligible IN (0, 1));

UPDATE shadow_assignment_index
SET registered_at = created_at
WHERE registered_at IS NULL;

ALTER TABLE shadow_observation_index
ADD COLUMN outcome_data_source TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'
CHECK (
    outcome_data_source IN (
        'LIVE_FORWARD_MARKET',
        'RETROSPECTIVE_REPLAY',
        'LEGACY_UNVERIFIED'
    )
);

ALTER TABLE shadow_observation_index
ADD COLUMN data_available_at TEXT;

ALTER TABLE shadow_observation_index
ADD COLUMN thesis_status TEXT NOT NULL DEFAULT 'NOT_EVALUATED'
CHECK (
    thesis_status IN (
        'NOT_EVALUATED',
        'STILL_VALID',
        'INVALIDATED',
        'INCONCLUSIVE'
    )
);

ALTER TABLE shadow_observation_index
ADD COLUMN registered_at TEXT;

ALTER TABLE shadow_observation_index
ADD COLUMN forward_data_eligible INTEGER NOT NULL DEFAULT 0
CHECK (forward_data_eligible IN (0, 1));

UPDATE shadow_observation_index
SET registered_at = created_at
WHERE registered_at IS NULL;

CREATE INDEX idx_shadow_study_prospective
ON shadow_study_index(prospective_eligible, registered_at, study_id);

CREATE INDEX idx_shadow_assignment_prospective
ON shadow_assignment_index(
    study_id,
    prospective_eligible,
    signal_time,
    assignment_id
);

CREATE INDEX idx_shadow_observation_forward
ON shadow_observation_index(
    study_id,
    forward_data_eligible,
    horizon_days,
    observation_status,
    observation_id
);
