CREATE TABLE knowledge_semantic_run (
    run_id TEXT PRIMARY KEY,
    author_source_id TEXT NOT NULL,
    input_manifest_hash TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    stage TEXT NOT NULL,
    run_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE knowledge_semantic_content_item (
    item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    author_source_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    content_version_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    source_object_hash TEXT NOT NULL,
    normalized_object_hash TEXT NOT NULL,
    paragraph_count INTEGER NOT NULL CHECK (paragraph_count >= 0),
    item_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, content_type, content_id)
);

CREATE TABLE knowledge_paragraph_unit (
    paragraph_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    item_id TEXT NOT NULL REFERENCES knowledge_semantic_content_item(item_id),
    author_source_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    text_object_hash TEXT NOT NULL,
    primary_role TEXT NOT NULL,
    standalone_distillable INTEGER NOT NULL CHECK (standalone_distillable IN (0, 1)),
    merge_action TEXT NOT NULL,
    unit_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, item_id, ordinal),
    UNIQUE(run_id, item_id, paragraph_id)
);

CREATE TABLE knowledge_keyword_screen (
    screen_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    item_id TEXT NOT NULL REFERENCES knowledge_semantic_content_item(item_id),
    rule_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    result_object_hash TEXT NOT NULL,
    screen_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, item_id, rule_version)
);

CREATE TABLE knowledge_argument_relation (
    relation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    item_id TEXT NOT NULL REFERENCES knowledge_semantic_content_item(item_id),
    source_paragraph_id TEXT NOT NULL REFERENCES knowledge_paragraph_unit(paragraph_id),
    target_paragraph_id TEXT NOT NULL REFERENCES knowledge_paragraph_unit(paragraph_id),
    relation_type TEXT NOT NULL,
    relation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, item_id, source_paragraph_id, target_paragraph_id, relation_type)
);

CREATE TABLE knowledge_argument_unit (
    argument_unit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    item_id TEXT NOT NULL REFERENCES knowledge_semantic_content_item(item_id),
    author_source_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    start_ordinal INTEGER NOT NULL CHECK (start_ordinal >= 1),
    end_ordinal INTEGER NOT NULL CHECK (end_ordinal >= start_ordinal),
    text_object_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    topic_relevance REAL NOT NULL CHECK (topic_relevance BETWEEN 0.0 AND 1.0),
    methodological_completeness REAL NOT NULL
        CHECK (methodological_completeness BETWEEN 0.0 AND 1.0),
    unit_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, item_id, start_ordinal, end_ordinal)
);

CREATE TABLE knowledge_argument_unit_paragraph_ref (
    argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    paragraph_id TEXT NOT NULL REFERENCES knowledge_paragraph_unit(paragraph_id),
    rhetorical_role TEXT NOT NULL,
    PRIMARY KEY(argument_unit_id, ordinal),
    UNIQUE(argument_unit_id, paragraph_id)
);

CREATE TABLE knowledge_embedding_manifest (
    manifest_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    model_id TEXT NOT NULL,
    model_asset_hash TEXT NOT NULL,
    tokenizer_asset_hash TEXT NOT NULL,
    vector_parquet_hash TEXT NOT NULL,
    score_parquet_hash TEXT NOT NULL,
    manifest_object_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, model_asset_hash, tokenizer_asset_hash)
);

CREATE TABLE knowledge_llm_batch (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    input_manifest_hash TEXT NOT NULL,
    packet_object_hash TEXT NOT NULL,
    response_object_hash TEXT,
    status TEXT NOT NULL,
    batch_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE knowledge_semantic_candidate (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    author_source_id TEXT NOT NULL,
    payload_object_hash TEXT NOT NULL,
    evaluation_status TEXT NOT NULL CHECK (evaluation_status = 'NOT_RUN'),
    approval_status TEXT NOT NULL CHECK (approval_status = 'PENDING'),
    candidate_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_semantic_candidate_au_ref (
    candidate_id TEXT NOT NULL REFERENCES knowledge_semantic_candidate(candidate_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
    PRIMARY KEY(candidate_id, ordinal),
    UNIQUE(candidate_id, argument_unit_id)
);

CREATE INDEX idx_knowledge_semantic_run_author
ON knowledge_semantic_run(author_source_id, started_at, run_id);

CREATE INDEX idx_knowledge_paragraph_item
ON knowledge_paragraph_unit(run_id, item_id, ordinal);

CREATE INDEX idx_knowledge_argument_item
ON knowledge_argument_unit(run_id, item_id, start_ordinal, end_ordinal);
