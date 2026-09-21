CREATE TABLE research_request_trace_index (
    trace_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('COMPLETED','PARTIAL','FAILED','CANCELLED')),
    source_group TEXT NOT NULL CHECK (source_group IN ('NORMAL','FAILURE')),
    cache_mode TEXT NOT NULL CHECK (cache_mode IN ('COLD','HOT','MIXED')),
    started_at TEXT NOT NULL,
    answer_ready_at TEXT NOT NULL,
    wall_time_ms INTEGER NOT NULL CHECK (wall_time_ms >= 0),
    useful_output INTEGER NOT NULL CHECK (useful_output IN (0,1)),
    provider_call_count INTEGER NOT NULL CHECK (provider_call_count >= 0),
    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
    cache_hit_count INTEGER NOT NULL CHECK (cache_hit_count >= 0),
    cache_miss_count INTEGER NOT NULL CHECK (cache_miss_count >= 0),
    backend_busy_ms INTEGER NOT NULL CHECK (backend_busy_ms >= 0),
    llm_busy_ms INTEGER NOT NULL CHECK (llm_busy_ms >= 0),
    backend_llm_overlap_ms INTEGER NOT NULL CHECK (backend_llm_overlap_ms >= 0),
    ready_scheduler_ms INTEGER NOT NULL CHECK (ready_scheduler_ms >= 0),
    ready_scheduler_idle_ms INTEGER NOT NULL CHECK (ready_scheduler_idle_ms >= 0),
    object_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_research_request_trace_ready
ON research_request_trace_index(answer_ready_at, request_id, trace_id);

CREATE INDEX idx_research_request_trace_source
ON research_request_trace_index(source_group, cache_mode, answer_ready_at, trace_id);

CREATE TABLE research_validation_proof_cache (
    cache_key TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    validator_hash TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    identity_scope TEXT NOT NULL,
    fact_state_hash TEXT NOT NULL,
    proof_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_research_validation_proof_artifact
ON research_validation_proof_cache(artifact_id, object_hash, validator_hash, policy_hash);

CREATE TABLE research_target_state (
    target_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL UNIQUE,
    company_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('POTENTIAL','RESEARCHED','WAITING','REVIEW_DUE','SUSPENDED','EXITED')),
    source_reason TEXT NOT NULL,
    source_artifact_id TEXT,
    latest_result_artifact_id TEXT,
    dependency_fingerprint TEXT NOT NULL,
    module_versions_json TEXT NOT NULL,
    triggers_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    invalidation_reason TEXT,
    last_review_at TEXT,
    next_review_at TEXT,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_research_target_state_queue
ON research_target_state(state, priority DESC, next_review_at, target_id);

CREATE INDEX idx_research_target_state_company
ON research_target_state(company_id, state, target_id);

CREATE TABLE research_scheduler_run (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    deadline_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE research_scheduler_task (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_scheduler_run(run_id),
    node_id TEXT NOT NULL,
    candidate_id TEXT,
    category TEXT NOT NULL CHECK (category IN ('NETWORK','CPU','LLM','WRITE')),
    status TEXT NOT NULL CHECK (status IN ('PENDING','READY','RUNNING','COMPLETED','FAILED','CANCELLED')),
    dependencies_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_owner TEXT,
    lease_expires_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_artifact_id TEXT,
    error_code TEXT,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, node_id, candidate_id, input_fingerprint)
);

CREATE INDEX idx_research_scheduler_task_ready
ON research_scheduler_task(run_id, status, category, updated_at, task_id);

CREATE INDEX idx_research_scheduler_task_candidate
ON research_scheduler_task(candidate_id, status, run_id, task_id);
