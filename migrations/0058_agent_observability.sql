CREATE TABLE agent_task_observation_index (
    observation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_status TEXT NOT NULL CHECK (task_status IN ('COMPLETED','NEEDS_INFO','FAILED')),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    eligible_skill_count INTEGER NOT NULL CHECK (eligible_skill_count >= 0),
    selected_skill_count INTEGER NOT NULL CHECK (selected_skill_count >= 0),
    completed_skill_count INTEGER NOT NULL CHECK (completed_skill_count >= 0),
    expected_skill_count INTEGER NOT NULL CHECK (expected_skill_count >= 0),
    routing_precision REAL CHECK (routing_precision IS NULL OR (routing_precision >= 0 AND routing_precision <= 1)),
    routing_recall REAL CHECK (routing_recall IS NULL OR (routing_recall >= 0 AND routing_recall <= 1)),
    object_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id)
);

CREATE INDEX idx_agent_task_observation_created
ON agent_task_observation_index(created_at, task_id, observation_id);

CREATE INDEX idx_agent_task_observation_status
ON agent_task_observation_index(task_status, created_at, observation_id);
