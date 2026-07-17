CREATE TABLE specialist_diagnostic_index (
    diagnostic_id TEXT PRIMARY KEY,
    base_case_id TEXT NOT NULL REFERENCES base_case_pack_index(base_case_id),
    route_plan_id TEXT NOT NULL REFERENCES specialist_route_plan_index(route_plan_id),
    delta_id TEXT NOT NULL REFERENCES specialist_delta_index(delta_id),
    skill_id TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    diagnostics_version TEXT NOT NULL,
    status TEXT NOT NULL,
    signal_count INTEGER NOT NULL CHECK (signal_count >= 1),
    degradation_count INTEGER NOT NULL CHECK (degradation_count >= 0),
    metric_count INTEGER NOT NULL CHECK (metric_count >= 0),
    evidence_request_count INTEGER NOT NULL CHECK (evidence_request_count >= 0),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(route_plan_id, skill_id, skill_version, diagnostics_version, input_hash)
);

CREATE INDEX idx_specialist_diagnostic_base_case
ON specialist_diagnostic_index(base_case_id, skill_id, created_at, diagnostic_id);

CREATE TABLE research_memo_index (
    memo_id TEXT PRIMARY KEY,
    base_case_id TEXT NOT NULL REFERENCES base_case_pack_index(base_case_id),
    route_plan_id TEXT NOT NULL REFERENCES specialist_route_plan_index(route_plan_id),
    company_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    registry_version TEXT NOT NULL REFERENCES research_skill_registry_index(registry_version),
    coverage_status TEXT NOT NULL,
    delta_count INTEGER NOT NULL CHECK (delta_count >= 0),
    missing_selected_count INTEGER NOT NULL CHECK (missing_selected_count >= 0),
    gap_count INTEGER NOT NULL CHECK (gap_count >= 0),
    degradation_count INTEGER NOT NULL CHECK (degradation_count >= 0),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    object_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(base_case_id, route_plan_id, input_hash)
);

CREATE INDEX idx_research_memo_company
ON research_memo_index(company_id, as_of, created_at, memo_id);
