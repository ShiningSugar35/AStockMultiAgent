CREATE TABLE research_llm_takeover (
    packet_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES research_scheduler_run(run_id),
    task_id TEXT NOT NULL REFERENCES research_scheduler_task(task_id),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    round_index INTEGER NOT NULL CHECK (round_index BETWEEN 1 AND 2),
    status TEXT NOT NULL CHECK (status IN ('PENDING','RESOLVED','UNRESOLVED')),
    dependency_fingerprint TEXT NOT NULL,
    packet_object_hash TEXT NOT NULL,
    result_artifact_id TEXT,
    result_object_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, generation, round_index)
);

CREATE INDEX idx_research_llm_takeover_task
ON research_llm_takeover(task_id, generation, status, round_index);
