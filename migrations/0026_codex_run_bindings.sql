CREATE TABLE codex_run_input_index (
    run_id TEXT NOT NULL REFERENCES run(run_id),
    artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    artifact_type TEXT NOT NULL,
    artifact_role TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_id),
    UNIQUE (run_id, object_hash)
);

CREATE INDEX idx_codex_run_input_artifact
ON codex_run_input_index(artifact_id, run_id);

CREATE TABLE codex_run_output_index (
    run_id TEXT PRIMARY KEY REFERENCES run(run_id),
    validated_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    output_artifact_type TEXT NOT NULL,
    output_object_hash TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    source_artifact_id TEXT REFERENCES artifact_registry(artifact_id),
    source_object_hash TEXT,
    input_count INTEGER NOT NULL CHECK (input_count >= 0),
    citation_count INTEGER NOT NULL CHECK (citation_count >= 0),
    strict_registered_output INTEGER NOT NULL
        CHECK (strict_registered_output IN (0, 1)),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_codex_run_output_source
ON codex_run_output_index(source_artifact_id, run_id);
