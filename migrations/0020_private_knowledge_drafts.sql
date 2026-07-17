CREATE TABLE private_viewpoint_draft (
    draft_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    author_source_id TEXT NOT NULL,
    method_category TEXT NOT NULL,
    source_unit_id TEXT NOT NULL REFERENCES knowledge_distillation_unit(unit_id),
    source_excerpt_hash TEXT NOT NULL,
    payload_object_hash TEXT NOT NULL,
    proposition_derivation TEXT NOT NULL,
    generation_rule_version TEXT NOT NULL,
    human_review_status TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, method_category, source_unit_id)
);

CREATE INDEX idx_private_viewpoint_draft_author
ON private_viewpoint_draft(author_source_id, run_id, method_category, draft_id);

CREATE TABLE private_skill_candidate_draft (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    author_source_id TEXT NOT NULL,
    target_skill TEXT NOT NULL,
    method_category TEXT NOT NULL,
    payload_object_hash TEXT NOT NULL,
    generation_rule_version TEXT NOT NULL,
    evaluation_status TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, method_category)
);

CREATE INDEX idx_private_skill_candidate_draft_author
ON private_skill_candidate_draft(author_source_id, run_id, target_skill, candidate_id);

CREATE TABLE private_skill_candidate_viewpoint_ref (
    candidate_id TEXT NOT NULL REFERENCES private_skill_candidate_draft(candidate_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    draft_id TEXT NOT NULL REFERENCES private_viewpoint_draft(draft_id),
    PRIMARY KEY(candidate_id, ordinal),
    UNIQUE(candidate_id, draft_id)
);

CREATE TABLE private_skill_candidate_unit_ref (
    candidate_id TEXT NOT NULL REFERENCES private_skill_candidate_draft(candidate_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    unit_id TEXT NOT NULL REFERENCES knowledge_distillation_unit(unit_id),
    PRIMARY KEY(candidate_id, ordinal),
    UNIQUE(candidate_id, unit_id)
);

CREATE TABLE author_draft_generation_report (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_distillation_run(run_id),
    author_source_id TEXT NOT NULL,
    generation_rule_version TEXT NOT NULL,
    viewpoint_draft_count INTEGER NOT NULL CHECK (viewpoint_draft_count >= 0),
    skill_candidate_count INTEGER NOT NULL CHECK (skill_candidate_count >= 0),
    human_review_status TEXT NOT NULL,
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id)
);

CREATE INDEX idx_author_draft_generation_report_latest
ON author_draft_generation_report(author_source_id, created_at, report_id);
