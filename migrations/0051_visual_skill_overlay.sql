CREATE TABLE knowledge_visual_skill_run (
    run_id TEXT PRIMARY KEY,
    base_run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    base_registry_release_id TEXT NOT NULL REFERENCES knowledge_skill_registry_release(release_id),
    base_registry_object_hash TEXT NOT NULL
        CHECK(length(base_registry_object_hash) = 64
            AND base_registry_object_hash NOT GLOB '*[^0-9a-f]*'),
    generation_policy_version TEXT NOT NULL CHECK(length(trim(generation_policy_version)) > 0),
    author_source_ids_json TEXT NOT NULL CHECK(json_valid(author_source_ids_json)),
    semantic_run_ids_json TEXT NOT NULL CHECK(json_valid(semantic_run_ids_json)),
    visual_pack_artifact_ids_json TEXT NOT NULL CHECK(json_valid(visual_pack_artifact_ids_json)),
    visual_pack_object_hashes_json TEXT NOT NULL CHECK(json_valid(visual_pack_object_hashes_json)),
    evaluated_argument_count INTEGER NOT NULL CHECK(evaluated_argument_count >= 0),
    candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
    no_skill_count INTEGER NOT NULL CHECK(no_skill_count >= 0),
    run_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    run_object_hash TEXT NOT NULL
        CHECK(length(run_object_hash) = 64 AND run_object_hash NOT GLOB '*[^0-9a-f]*'),
    run_json TEXT NOT NULL CHECK(json_valid(run_json)),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL,
    UNIQUE(base_run_id, base_registry_object_hash, visual_pack_object_hashes_json),
    CHECK(evaluated_argument_count = candidate_count + no_skill_count)
);

CREATE TABLE knowledge_visual_skill_candidate (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_visual_skill_run(run_id),
    author_source_id TEXT NOT NULL CHECK(length(trim(author_source_id)) > 0),
    semantic_run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
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
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    skill_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    skill_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(skill_object_hash) = 64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_json TEXT NOT NULL CHECK(json_valid(skill_json)),
    audit_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    audit_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(audit_object_hash) = 64 AND audit_object_hash NOT GLOB '*[^0-9a-f]*'),
    audit_json TEXT NOT NULL CHECK(json_valid(audit_json)),
    audit_status TEXT NOT NULL CHECK(audit_status = 'PASS'),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, argument_unit_id),
    UNIQUE(run_id, candidate_id)
);

CREATE TABLE knowledge_visual_skill_no_skill (
    run_id TEXT NOT NULL REFERENCES knowledge_visual_skill_run(run_id),
    argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
    author_source_id TEXT NOT NULL CHECK(length(trim(author_source_id)) > 0),
    reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json)),
    record_object_hash TEXT NOT NULL
        CHECK(length(record_object_hash) = 64 AND record_object_hash NOT GLOB '*[^0-9a-f]*'),
    record_json TEXT NOT NULL CHECK(json_valid(record_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, argument_unit_id)
);

CREATE TABLE knowledge_visual_skill_review_decision (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_visual_skill_run(run_id),
    candidate_id TEXT NOT NULL UNIQUE REFERENCES knowledge_visual_skill_candidate(candidate_id),
    final_skill_id TEXT NOT NULL CHECK(length(trim(final_skill_id)) > 0),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision TEXT NOT NULL CHECK(decision IN ('APPROVE', 'REJECT')),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) >= 8),
    decision_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    decision_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(decision_object_hash) = 64 AND decision_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id, candidate_id)
        REFERENCES knowledge_visual_skill_candidate(run_id, candidate_id)
);

CREATE TABLE knowledge_visual_skill_release (
    release_id TEXT PRIMARY KEY,
    registry_version TEXT NOT NULL UNIQUE CHECK(length(trim(registry_version)) > 0),
    base_run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    generation_run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_visual_skill_run(run_id),
    base_registry_release_id TEXT NOT NULL REFERENCES knowledge_skill_registry_release(release_id),
    base_registry_object_hash TEXT NOT NULL
        CHECK(length(base_registry_object_hash) = 64
            AND base_registry_object_hash NOT GLOB '*[^0-9a-f]*'),
    base_admitted_skill_count INTEGER NOT NULL CHECK(base_admitted_skill_count >= 0),
    overlay_candidate_count INTEGER NOT NULL CHECK(overlay_candidate_count >= 0),
    overlay_approved_count INTEGER NOT NULL CHECK(overlay_approved_count >= 0),
    overlay_rejected_count INTEGER NOT NULL CHECK(overlay_rejected_count >= 0),
    overlay_admitted_skill_count INTEGER NOT NULL CHECK(overlay_admitted_skill_count >= 0),
    composite_admitted_skill_count INTEGER NOT NULL CHECK(composite_admitted_skill_count >= 0),
    decision_ids_json TEXT NOT NULL CHECK(json_valid(decision_ids_json)),
    member_ids_json TEXT NOT NULL CHECK(json_valid(member_ids_json)),
    release_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    release_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(release_object_hash) = 64
            AND release_object_hash NOT GLOB '*[^0-9a-f]*'),
    release_json TEXT NOT NULL CHECK(json_valid(release_json)),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL,
    CHECK(overlay_candidate_count = overlay_approved_count + overlay_rejected_count),
    CHECK(overlay_admitted_skill_count = overlay_approved_count),
    CHECK(composite_admitted_skill_count = base_admitted_skill_count + overlay_admitted_skill_count)
);

CREATE TABLE knowledge_visual_skill_member (
    release_id TEXT NOT NULL REFERENCES knowledge_visual_skill_release(release_id),
    member_ordinal INTEGER NOT NULL CHECK(member_ordinal >= 1),
    candidate_id TEXT NOT NULL UNIQUE REFERENCES knowledge_visual_skill_candidate(candidate_id),
    final_skill_id TEXT NOT NULL CHECK(length(trim(final_skill_id)) > 0),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    admission_basis TEXT NOT NULL CHECK(admission_basis = 'APPROVED'),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    PRIMARY KEY(release_id, final_skill_id),
    UNIQUE(release_id, member_ordinal)
);

CREATE INDEX idx_knowledge_visual_skill_candidate_run
ON knowledge_visual_skill_candidate(run_id, author_source_id, final_skill_id);

CREATE INDEX idx_knowledge_visual_skill_no_skill_run
ON knowledge_visual_skill_no_skill(run_id, author_source_id, argument_unit_id);

CREATE INDEX idx_knowledge_visual_skill_release_base
ON knowledge_visual_skill_release(base_run_id, created_at, release_id);

CREATE TRIGGER trg_knowledge_visual_skill_run_no_update
BEFORE UPDATE ON knowledge_visual_skill_run
BEGIN
    SELECT RAISE(ABORT, 'visual Skill generation runs are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_run_no_delete
BEFORE DELETE ON knowledge_visual_skill_run
BEGIN
    SELECT RAISE(ABORT, 'visual Skill generation runs are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_candidate_no_update
BEFORE UPDATE ON knowledge_visual_skill_candidate
BEGIN
    SELECT RAISE(ABORT, 'visual Skill candidates are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_candidate_no_delete
BEFORE DELETE ON knowledge_visual_skill_candidate
BEGIN
    SELECT RAISE(ABORT, 'visual Skill candidates are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_no_skill_no_update
BEFORE UPDATE ON knowledge_visual_skill_no_skill
BEGIN
    SELECT RAISE(ABORT, 'visual no-Skill records are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_no_skill_no_delete
BEFORE DELETE ON knowledge_visual_skill_no_skill
BEGIN
    SELECT RAISE(ABORT, 'visual no-Skill records are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_review_no_update
BEFORE UPDATE ON knowledge_visual_skill_review_decision
BEGIN
    SELECT RAISE(ABORT, 'visual Skill review decisions are append-only');
END;

CREATE TRIGGER trg_knowledge_visual_skill_review_no_delete
BEFORE DELETE ON knowledge_visual_skill_review_decision
BEGIN
    SELECT RAISE(ABORT, 'visual Skill review decisions are append-only');
END;

CREATE TRIGGER trg_knowledge_visual_skill_release_review_closed
BEFORE INSERT ON knowledge_visual_skill_release
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM knowledge_visual_skill_candidate candidate
        LEFT JOIN knowledge_visual_skill_review_decision decision
          ON decision.candidate_id = candidate.candidate_id
        WHERE candidate.run_id = NEW.generation_run_id
          AND decision.decision_id IS NULL
    ) THEN RAISE(ABORT, 'visual Skill release requires closed review') END;
END;

CREATE TRIGGER trg_knowledge_visual_skill_release_no_update
BEFORE UPDATE ON knowledge_visual_skill_release
BEGIN
    SELECT RAISE(ABORT, 'visual Skill releases are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_release_no_delete
BEFORE DELETE ON knowledge_visual_skill_release
BEGIN
    SELECT RAISE(ABORT, 'visual Skill releases are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_member_no_update
BEFORE UPDATE ON knowledge_visual_skill_member
BEGIN
    SELECT RAISE(ABORT, 'visual Skill members are immutable');
END;

CREATE TRIGGER trg_knowledge_visual_skill_member_no_delete
BEFORE DELETE ON knowledge_visual_skill_member
BEGIN
    SELECT RAISE(ABORT, 'visual Skill members are immutable');
END;
