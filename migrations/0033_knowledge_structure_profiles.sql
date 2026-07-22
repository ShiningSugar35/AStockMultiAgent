CREATE TABLE knowledge_structure_profile (
    profile_id TEXT PRIMARY KEY,
    author_source_id TEXT NOT NULL,
    input_source_id TEXT NOT NULL,
    material_kind TEXT NOT NULL
        CHECK (material_kind IN ('PRIVATE_PDF', 'PRIVATE_DOCX', 'ZHIHU_ONLINE')),
    processing_strategy TEXT NOT NULL,
    input_set_hash TEXT NOT NULL,
    source_item_count INTEGER NOT NULL CHECK (source_item_count >= 0),
    semantic_segment_count INTEGER NOT NULL CHECK (semantic_segment_count >= 0),
    coverage_status TEXT NOT NULL,
    human_review_status TEXT NOT NULL CHECK (human_review_status = 'PENDING'),
    profile_object_hash TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_knowledge_structure_profile_input
ON knowledge_structure_profile(
    author_source_id,
    input_source_id,
    material_kind,
    processing_strategy,
    input_set_hash
);

CREATE INDEX idx_knowledge_structure_profile_latest
ON knowledge_structure_profile(author_source_id, input_source_id, created_at, profile_id);
