CREATE TABLE knowledge_distillation_run (
    run_id TEXT PRIMARY KEY,
    author_source_id TEXT NOT NULL,
    classification_rule_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_set_hash TEXT NOT NULL,
    input_source_item_count INTEGER NOT NULL CHECK (input_source_item_count >= 0),
    produced_unit_count INTEGER NOT NULL CHECK (produced_unit_count >= 0),
    run_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX idx_knowledge_distillation_run_author
ON knowledge_distillation_run(author_source_id, started_at, run_id);

CREATE TABLE knowledge_distillation_unit (
    unit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    author_source_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    source_item_ordinal INTEGER NOT NULL CHECK (source_item_ordinal >= 1),
    segment_ordinal INTEGER NOT NULL CHECK (segment_ordinal >= 1),
    normalized_text_hash TEXT NOT NULL,
    duplicate_of_unit_id TEXT REFERENCES knowledge_distillation_unit(unit_id),
    decision TEXT NOT NULL,
    classification_rule_version TEXT NOT NULL,
    unit_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, source_unit_id, segment_ordinal)
);

CREATE INDEX idx_knowledge_distillation_unit_run
ON knowledge_distillation_unit(run_id, source_item_ordinal, segment_ordinal, unit_id);

CREATE INDEX idx_knowledge_distillation_unit_hash
ON knowledge_distillation_unit(run_id, normalized_text_hash, unit_id);

CREATE TABLE author_distillation_report (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    author_source_id TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    human_review_status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_author_distillation_report_latest
ON author_distillation_report(author_source_id, created_at, report_id);

CREATE TABLE distillation_review_queue (
    queue_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    author_source_id TEXT NOT NULL,
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    human_review_status TEXT NOT NULL,
    queue_object_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_distillation_review_queue_author
ON distillation_review_queue(author_source_id, human_review_status, created_at, queue_id);

CREATE TABLE book_cleaning_report (
    report_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES book_source_manifest(manifest_id),
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    processing_status TEXT NOT NULL,
    human_review_status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE book_method_coverage_report (
    report_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES book_source_manifest(manifest_id),
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    processing_status TEXT NOT NULL,
    human_review_status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
