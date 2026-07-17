CREATE TABLE research_skill_registry_index (
    registry_version TEXT PRIMARY KEY,
    skill_count INTEGER NOT NULL CHECK (skill_count >= 1),
    specialist_count INTEGER NOT NULL CHECK (specialist_count >= 1),
    max_specialists INTEGER NOT NULL CHECK (max_specialists BETWEEN 1 AND 3),
    object_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE specialist_route_plan_index (
    route_plan_id TEXT PRIMARY KEY,
    base_case_id TEXT NOT NULL REFERENCES base_case_pack_index(base_case_id),
    evidence_pack_id TEXT NOT NULL REFERENCES frozen_evidence_pack_index(pack_id),
    registry_version TEXT NOT NULL REFERENCES research_skill_registry_index(registry_version),
    coverage_status TEXT NOT NULL,
    confidence_cap REAL NOT NULL CHECK (confidence_cap BETWEEN 0 AND 1),
    selected_count INTEGER NOT NULL CHECK (selected_count BETWEEN 0 AND 3),
    unavailable_count INTEGER NOT NULL CHECK (unavailable_count >= 0),
    degradation_count INTEGER NOT NULL CHECK (degradation_count >= 0),
    object_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(base_case_id, registry_version, request_hash)
);

CREATE INDEX idx_specialist_route_base_case
ON specialist_route_plan_index(base_case_id, created_at, route_plan_id);

CREATE TABLE specialist_delta_index (
    delta_id TEXT PRIMARY KEY,
    base_case_id TEXT NOT NULL REFERENCES base_case_pack_index(base_case_id),
    route_plan_id TEXT NOT NULL REFERENCES specialist_route_plan_index(route_plan_id),
    skill_id TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    incremental_finding_count INTEGER NOT NULL CHECK (incremental_finding_count >= 0),
    correction_count INTEGER NOT NULL CHECK (correction_count >= 0),
    metric_count INTEGER NOT NULL CHECK (metric_count >= 0),
    evidence_request_count INTEGER NOT NULL CHECK (evidence_request_count >= 0),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    confidence_delta REAL NOT NULL CHECK (confidence_delta BETWEEN -0.25 AND 0.25),
    object_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(route_plan_id, skill_id, skill_version, request_hash)
);

CREATE INDEX idx_specialist_delta_route
ON specialist_delta_index(route_plan_id, skill_id, skill_version, created_at, delta_id);
