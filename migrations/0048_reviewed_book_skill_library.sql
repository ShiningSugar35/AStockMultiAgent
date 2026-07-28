CREATE TABLE knowledge_reviewed_semantic_run (
    run_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    author_source_id TEXT NOT NULL,
    review_workbook_hash TEXT NOT NULL,
    source_pdf_hash TEXT NOT NULL,
    input_manifest_hash TEXT NOT NULL UNIQUE,
    pipeline_version TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN (
        'INPUT_VERIFIED',
        'REVIEW_APPLIED',
        'ARGUMENTS_BUILT',
        'EMBEDDINGS_RECOMPUTED',
        'SKILLS_DISTILLED',
        'COMPLETE',
        'NEEDS_USER_REVIEW',
        'FAILED'
    )),
    review_record_count INTEGER NOT NULL CHECK(review_record_count >= 0),
    reviewed_argument_count INTEGER NOT NULL CHECK(reviewed_argument_count >= 0),
    unresolved_count INTEGER NOT NULL CHECK(unresolved_count >= 0),
    run_object_hash TEXT NOT NULL,
    run_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE knowledge_review_decision (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    excel_row INTEGER NOT NULL CHECK(excel_row >= 2),
    source_argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
    verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REJECT','MODIFY')),
    application_status TEXT NOT NULL CHECK(application_status IN (
        'APPLIED',
        'EXCLUDED',
        'NEEDS_USER_REVIEW'
    )),
    decision_object_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, excel_row),
    UNIQUE(run_id, source_argument_unit_id)
);

CREATE TABLE knowledge_review_decision_candidate_range (
    decision_id TEXT NOT NULL REFERENCES knowledge_review_decision(decision_id),
    range_ordinal INTEGER NOT NULL CHECK(range_ordinal >= 1),
    start_page INTEGER NOT NULL CHECK(start_page >= 1),
    start_paragraph_ordinal INTEGER NOT NULL CHECK(start_paragraph_ordinal >= 1),
    end_page INTEGER NOT NULL CHECK(end_page >= start_page),
    end_paragraph_ordinal INTEGER NOT NULL CHECK(end_paragraph_ordinal >= 1),
    range_json TEXT NOT NULL,
    PRIMARY KEY(decision_id, range_ordinal)
);

CREATE TABLE knowledge_reviewed_argument_unit (
    argument_unit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    decision_id TEXT NOT NULL REFERENCES knowledge_review_decision(decision_id),
    author_source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    text_object_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('READY','NEEDS_USER_REVIEW')),
    topic_relevance REAL NOT NULL CHECK(topic_relevance BETWEEN 0.0 AND 1.0),
    methodological_completeness REAL NOT NULL
        CHECK(methodological_completeness BETWEEN 0.0 AND 1.0),
    standalone_distillable INTEGER NOT NULL CHECK(standalone_distillable IN (0, 1)),
    method_categories_json TEXT NOT NULL,
    rhetorical_roles_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    unit_object_hash TEXT NOT NULL,
    unit_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, unit_object_hash)
);

CREATE TABLE knowledge_reviewed_argument_decision_ref (
    argument_unit_id TEXT NOT NULL
        REFERENCES knowledge_reviewed_argument_unit(argument_unit_id),
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    decision_id TEXT NOT NULL REFERENCES knowledge_review_decision(decision_id),
    PRIMARY KEY(argument_unit_id, ref_ordinal),
    UNIQUE(argument_unit_id, decision_id)
);

CREATE TABLE knowledge_reviewed_argument_paragraph_ref (
    argument_unit_id TEXT NOT NULL
        REFERENCES knowledge_reviewed_argument_unit(argument_unit_id),
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    source_paragraph_id TEXT NOT NULL REFERENCES knowledge_paragraph_unit(paragraph_id),
    item_id TEXT NOT NULL REFERENCES knowledge_semantic_content_item(item_id),
    content_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    paragraph_ordinal INTEGER NOT NULL CHECK(paragraph_ordinal >= 1),
    paragraph_head TEXT NOT NULL,
    text_object_hash TEXT NOT NULL,
    rhetorical_role TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    visual_evidence_ids_json TEXT NOT NULL,
    visual_chart_unit_ids_json TEXT NOT NULL,
    PRIMARY KEY(argument_unit_id, ref_ordinal),
    UNIQUE(argument_unit_id, source_paragraph_id)
);

CREATE TABLE knowledge_reviewed_argument_relation (
    relation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    argument_unit_id TEXT NOT NULL
        REFERENCES knowledge_reviewed_argument_unit(argument_unit_id),
    source_ref_ordinal INTEGER NOT NULL CHECK(source_ref_ordinal >= 1),
    target_ref_ordinal INTEGER NOT NULL CHECK(target_ref_ordinal >= 1),
    relation_type TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    relation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(argument_unit_id, source_ref_ordinal)
        REFERENCES knowledge_reviewed_argument_paragraph_ref(argument_unit_id, ref_ordinal),
    FOREIGN KEY(argument_unit_id, target_ref_ordinal)
        REFERENCES knowledge_reviewed_argument_paragraph_ref(argument_unit_id, ref_ordinal),
    UNIQUE(argument_unit_id, source_ref_ordinal, target_ref_ordinal, relation_type)
);

CREATE TABLE knowledge_reviewed_visual_ref (
    visual_ref_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    argument_unit_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL,
    source_visual_ref_id TEXT REFERENCES book_visual_semantic_ref(ref_id),
    chart_unit_id TEXT NOT NULL REFERENCES book_chart_unit(chart_unit_id),
    evidence_id TEXT NOT NULL REFERENCES book_image_evidence(evidence_id),
    ref_object_hash TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    FOREIGN KEY(argument_unit_id, ref_ordinal)
        REFERENCES knowledge_reviewed_argument_paragraph_ref(argument_unit_id, ref_ordinal),
    UNIQUE(argument_unit_id, ref_ordinal, chart_unit_id)
);

CREATE TABLE knowledge_reviewed_embedding_manifest (
    manifest_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_reviewed_semantic_run(run_id),
    model_id TEXT NOT NULL,
    model_asset_hash TEXT NOT NULL,
    tokenizer_asset_hash TEXT NOT NULL,
    vector_parquet_hash TEXT NOT NULL,
    score_parquet_hash TEXT NOT NULL,
    method_vector_parquet_hash TEXT NOT NULL,
    source_embedding_manifest_id TEXT REFERENCES knowledge_embedding_manifest(manifest_id),
    manifest_object_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_reviewed_coverage_report (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_reviewed_semantic_run(run_id),
    coverage_status TEXT NOT NULL CHECK(coverage_status IN (
        'COMPLETE',
        'NEEDS_USER_REVIEW',
        'FAILED'
    )),
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_viewpoint_card (
    card_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    proposition TEXT NOT NULL,
    method_category TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('READY_FOR_SHADOW','NEEDS_USER_REVIEW')),
    card_object_hash TEXT NOT NULL,
    card_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, card_object_hash)
);

CREATE TABLE knowledge_viewpoint_card_au_ref (
    card_id TEXT NOT NULL REFERENCES knowledge_viewpoint_card(card_id),
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    argument_unit_id TEXT NOT NULL
        REFERENCES knowledge_reviewed_argument_unit(argument_unit_id),
    PRIMARY KEY(card_id, ref_ordinal),
    UNIQUE(card_id, argument_unit_id)
);

CREATE TABLE knowledge_method_rule (
    rule_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    semantic_signature_hash TEXT NOT NULL,
    decision_question TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('READY_FOR_SHADOW','NEEDS_USER_REVIEW')),
    rule_object_hash TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, semantic_signature_hash)
);

CREATE TABLE knowledge_method_rule_au_ref (
    rule_id TEXT NOT NULL REFERENCES knowledge_method_rule(rule_id),
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    argument_unit_id TEXT NOT NULL
        REFERENCES knowledge_reviewed_argument_unit(argument_unit_id),
    PRIMARY KEY(rule_id, ref_ordinal),
    UNIQUE(rule_id, argument_unit_id)
);

CREATE TABLE knowledge_reviewed_skill (
    skill_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    skill_kind TEXT NOT NULL CHECK(skill_kind IN (
        'CANDIDATE_SELECTION',
        'POSITION_LIFECYCLE'
    )),
    skill_category TEXT NOT NULL,
    coverage_state TEXT NOT NULL CHECK(coverage_state IN (
        'COVERED',
        'AUTHOR_SILENT',
        'INSUFFICIENT_SOURCE',
        'CONFLICTING_SOURCE'
    )),
    status TEXT NOT NULL CHECK(status IN ('READY_FOR_SHADOW','NEEDS_USER_REVIEW')),
    manifest_object_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, skill_kind, skill_category)
);

CREATE TABLE knowledge_reviewed_skill_rule_ref (
    skill_id TEXT NOT NULL REFERENCES knowledge_reviewed_skill(skill_id),
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    rule_id TEXT NOT NULL REFERENCES knowledge_method_rule(rule_id),
    PRIMARY KEY(skill_id, ref_ordinal),
    UNIQUE(skill_id, rule_id)
);

CREATE TABLE knowledge_reviewed_skill_au_ref (
    skill_id TEXT NOT NULL REFERENCES knowledge_reviewed_skill(skill_id),
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    argument_unit_id TEXT NOT NULL
        REFERENCES knowledge_reviewed_argument_unit(argument_unit_id),
    PRIMARY KEY(skill_id, ref_ordinal),
    UNIQUE(skill_id, argument_unit_id)
);

CREATE TABLE knowledge_author_skill_coverage (
    coverage_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_reviewed_semantic_run(run_id),
    author_source_id TEXT NOT NULL,
    coverage_object_hash TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_reviewed_shadow_bundle (
    bundle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_reviewed_semantic_run(run_id),
    ready_skill_count INTEGER NOT NULL CHECK(ready_skill_count >= 0),
    needs_review_skill_count INTEGER NOT NULL CHECK(needs_review_skill_count >= 0),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    bundle_object_hash TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_reviewed_checkpoint (
    run_id TEXT NOT NULL REFERENCES knowledge_reviewed_semantic_run(run_id),
    stage TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL CHECK(batch_ordinal >= 1),
    cursor_json TEXT NOT NULL,
    checkpoint_object_hash TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY(run_id, stage, batch_ordinal)
);

CREATE INDEX idx_knowledge_reviewed_run_source
ON knowledge_reviewed_semantic_run(source_run_id, started_at, run_id);

CREATE INDEX idx_knowledge_reviewed_argument_run
ON knowledge_reviewed_argument_unit(run_id, status, argument_unit_id);

CREATE INDEX idx_knowledge_reviewed_ref_locator
ON knowledge_reviewed_argument_paragraph_ref(
    page_number,
    paragraph_ordinal,
    argument_unit_id,
    ref_ordinal
);

CREATE INDEX idx_knowledge_reviewed_skill_status
ON knowledge_reviewed_skill(run_id, skill_kind, status, skill_category);
