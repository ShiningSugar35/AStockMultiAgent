CREATE TABLE knowledge_semantic_skill_run (
    run_id TEXT PRIMARY KEY,
    base_run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    parent_registry_release_id TEXT NOT NULL,
    parent_registry_object_hash TEXT NOT NULL
        CHECK(length(parent_registry_object_hash) = 64
            AND parent_registry_object_hash NOT GLOB '*[^0-9a-f]*'),
    semantic_run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    llm_batch_id TEXT NOT NULL REFERENCES knowledge_llm_batch(batch_id),
    generation_policy_version TEXT NOT NULL CHECK(length(trim(generation_policy_version)) > 0),
    raw_candidate_count INTEGER NOT NULL CHECK(raw_candidate_count >= 1),
    effective_skill_count INTEGER NOT NULL CHECK(effective_skill_count >= 1),
    author_source_ids_json TEXT NOT NULL CHECK(json_valid(author_source_ids_json)),
    run_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    run_object_hash TEXT NOT NULL
        CHECK(length(run_object_hash) = 64 AND run_object_hash NOT GLOB '*[^0-9a-f]*'),
    run_json TEXT NOT NULL CHECK(json_valid(run_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL,
    UNIQUE(base_run_id, parent_registry_object_hash, semantic_run_id, llm_batch_id)
);

CREATE TABLE knowledge_semantic_skill_group (
    group_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_skill_run(run_id),
    final_skill_id TEXT NOT NULL UNIQUE CHECK(length(trim(final_skill_id)) > 0),
    skill_name TEXT NOT NULL CHECK(length(trim(skill_name)) > 0),
    primary_module TEXT NOT NULL CHECK(primary_module IN (
        'SOURCING_SCREENING',
        'FUNDAMENTAL_RESEARCH',
        'VALUATION_PRICING',
        'PORTFOLIO_CONSTRUCTION',
        'POSITION_RISK_MANAGEMENT',
        'PSYCHOLOGY_BEHAVIOR'
    )),
    secondary_modules_json TEXT NOT NULL CHECK(json_valid(secondary_modules_json)),
    decision_question TEXT NOT NULL CHECK(length(trim(decision_question)) > 0),
    core_principle TEXT NOT NULL CHECK(length(trim(core_principle)) >= 20),
    method_categories_json TEXT NOT NULL CHECK(json_valid(method_categories_json)),
    applicable_industries_json TEXT NOT NULL CHECK(json_valid(applicable_industries_json)),
    holding_horizon_json TEXT NOT NULL CHECK(json_valid(holding_horizon_json)),
    candidate_ids_json TEXT NOT NULL CHECK(json_valid(candidate_ids_json)),
    argument_unit_ids_json TEXT NOT NULL CHECK(json_valid(argument_unit_ids_json)),
    author_source_ids_json TEXT NOT NULL CHECK(json_valid(author_source_ids_json)),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    skill_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    skill_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(skill_object_hash) = 64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_json TEXT NOT NULL CHECK(json_valid(skill_json)),
    generation_audit_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    generation_audit_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(generation_audit_object_hash) = 64
            AND generation_audit_object_hash NOT GLOB '*[^0-9a-f]*'),
    generation_audit_json TEXT NOT NULL CHECK(json_valid(generation_audit_json)),
    generation_audit_status TEXT NOT NULL CHECK(generation_audit_status = 'PASS'),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, final_skill_id)
);

CREATE TABLE knowledge_semantic_skill_group_candidate (
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_skill_run(run_id),
    group_id TEXT NOT NULL REFERENCES knowledge_semantic_skill_group(group_id),
    candidate_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_candidate(candidate_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    PRIMARY KEY(group_id, candidate_id),
    UNIQUE(group_id, ordinal)
);

CREATE TABLE knowledge_semantic_skill_review_decision (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_skill_run(run_id),
    group_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_skill_group(group_id),
    final_skill_id TEXT NOT NULL CHECK(length(trim(final_skill_id)) > 0),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision TEXT NOT NULL CHECK(decision IN ('APPROVE', 'REJECT')),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) >= 8),
    decision_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    decision_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(decision_object_hash) = 64
            AND decision_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed = 0),
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_semantic_skill_release (
    release_id TEXT PRIMARY KEY,
    registry_version TEXT NOT NULL UNIQUE CHECK(length(trim(registry_version)) > 0),
    base_run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    generation_run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_skill_run(run_id),
    parent_registry_release_id TEXT NOT NULL,
    parent_registry_object_hash TEXT NOT NULL
        CHECK(length(parent_registry_object_hash) = 64
            AND parent_registry_object_hash NOT GLOB '*[^0-9a-f]*'),
    parent_admitted_skill_count INTEGER NOT NULL CHECK(parent_admitted_skill_count >= 0),
    raw_candidate_count INTEGER NOT NULL CHECK(raw_candidate_count >= 1),
    effective_skill_count INTEGER NOT NULL CHECK(effective_skill_count >= 1),
    overlay_approved_count INTEGER NOT NULL CHECK(overlay_approved_count >= 0),
    overlay_rejected_count INTEGER NOT NULL CHECK(overlay_rejected_count >= 0),
    overlay_admitted_skill_count INTEGER NOT NULL CHECK(overlay_admitted_skill_count >= 0),
    composite_admitted_skill_count INTEGER NOT NULL CHECK(composite_admitted_skill_count >= 0),
    decision_ids_json TEXT NOT NULL CHECK(json_valid(decision_ids_json)),
    member_ids_json TEXT NOT NULL CHECK(json_valid(member_ids_json)),
    audit_report_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    audit_report_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(audit_report_object_hash) = 64
            AND audit_report_object_hash NOT GLOB '*[^0-9a-f]*'),
    release_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    release_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(release_object_hash) = 64
            AND release_object_hash NOT GLOB '*[^0-9a-f]*'),
    release_json TEXT NOT NULL CHECK(json_valid(release_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL,
    CHECK(effective_skill_count = overlay_approved_count + overlay_rejected_count),
    CHECK(overlay_admitted_skill_count = overlay_approved_count),
    CHECK(composite_admitted_skill_count = parent_admitted_skill_count + overlay_admitted_skill_count)
);

CREATE TABLE knowledge_semantic_skill_member (
    release_id TEXT NOT NULL REFERENCES knowledge_semantic_skill_release(release_id),
    member_ordinal INTEGER NOT NULL CHECK(member_ordinal >= 1),
    group_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_skill_group(group_id),
    final_skill_id TEXT NOT NULL CHECK(length(trim(final_skill_id)) > 0),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    admission_basis TEXT NOT NULL CHECK(admission_basis = 'SEMANTIC_REVIEW_APPROVED'),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    PRIMARY KEY(release_id, final_skill_id),
    UNIQUE(release_id, member_ordinal)
);

CREATE INDEX idx_knowledge_semantic_skill_run_base
ON knowledge_semantic_skill_run(base_run_id, semantic_run_id, llm_batch_id);

CREATE INDEX idx_knowledge_semantic_skill_group_run
ON knowledge_semantic_skill_group(run_id, primary_module, final_skill_id);

CREATE INDEX idx_knowledge_semantic_skill_release_base
ON knowledge_semantic_skill_release(base_run_id, created_at, release_id);

CREATE TRIGGER trg_knowledge_semantic_skill_run_no_update
BEFORE UPDATE ON knowledge_semantic_skill_run
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill generation runs are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_run_no_delete
BEFORE DELETE ON knowledge_semantic_skill_run
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill generation runs are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_group_no_update
BEFORE UPDATE ON knowledge_semantic_skill_group
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill groups are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_group_no_delete
BEFORE DELETE ON knowledge_semantic_skill_group
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill groups are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_group_candidate_no_update
BEFORE UPDATE ON knowledge_semantic_skill_group_candidate
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill group membership is immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_group_candidate_no_delete
BEFORE DELETE ON knowledge_semantic_skill_group_candidate
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill group membership is immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_review_no_update
BEFORE UPDATE ON knowledge_semantic_skill_review_decision
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill review decisions are append-only');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_review_no_delete
BEFORE DELETE ON knowledge_semantic_skill_review_decision
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill review decisions are append-only');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_release_review_closed
BEFORE INSERT ON knowledge_semantic_skill_release
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM knowledge_semantic_skill_group g
        LEFT JOIN knowledge_semantic_skill_review_decision d ON d.group_id = g.group_id
        WHERE g.run_id = NEW.generation_run_id AND d.decision_id IS NULL
    ) THEN RAISE(ABORT, 'semantic Skill release requires closed review') END;
END;

CREATE TRIGGER trg_knowledge_semantic_skill_release_no_update
BEFORE UPDATE ON knowledge_semantic_skill_release
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill releases are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_release_no_delete
BEFORE DELETE ON knowledge_semantic_skill_release
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill releases are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_member_no_update
BEFORE UPDATE ON knowledge_semantic_skill_member
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill members are immutable');
END;

CREATE TRIGGER trg_knowledge_semantic_skill_member_no_delete
BEFORE DELETE ON knowledge_semantic_skill_member
BEGIN
    SELECT RAISE(ABORT, 'semantic Skill members are immutable');
END;
