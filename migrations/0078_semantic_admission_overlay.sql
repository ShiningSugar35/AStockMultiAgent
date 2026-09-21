CREATE TABLE knowledge_semantic_admission_run (
    run_id TEXT PRIMARY KEY,
    base_run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    parent_audited_release_id TEXT NOT NULL,
    parent_audited_object_hash TEXT NOT NULL
        CHECK(length(parent_audited_object_hash)=64
            AND parent_audited_object_hash NOT GLOB '*[^0-9a-f]*'),
    owner_policy_artifact_hash TEXT NOT NULL
        CHECK(length(owner_policy_artifact_hash)=64
            AND owner_policy_artifact_hash NOT GLOB '*[^0-9a-f]*'),
    admitted_groups_artifact_hash TEXT NOT NULL
        CHECK(length(admitted_groups_artifact_hash)=64
            AND admitted_groups_artifact_hash NOT GLOB '*[^0-9a-f]*'),
    raw_candidate_count INTEGER NOT NULL CHECK(raw_candidate_count >= 1),
    exact_group_count INTEGER NOT NULL CHECK(exact_group_count >= 1),
    admitted_group_count INTEGER NOT NULL CHECK(admitted_group_count >= 0),
    rejected_group_count INTEGER NOT NULL CHECK(rejected_group_count >= 0),
    author_source_ids_json TEXT NOT NULL CHECK(json_valid(author_source_ids_json)),
    run_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    run_object_hash TEXT NOT NULL
        CHECK(length(run_object_hash)=64 AND run_object_hash NOT GLOB '*[^0-9a-f]*'),
    run_json TEXT NOT NULL CHECK(json_valid(run_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed=0),
    created_at TEXT NOT NULL,
    CHECK(exact_group_count = admitted_group_count + rejected_group_count),
    UNIQUE(base_run_id,parent_audited_object_hash,owner_policy_artifact_hash)
);

CREATE TABLE knowledge_semantic_admission_skill (
    final_skill_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_admission_run(run_id),
    group_id TEXT NOT NULL UNIQUE CHECK(length(trim(group_id)) > 0),
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
    family TEXT NOT NULL CHECK(length(trim(family)) > 0),
    industry_scope TEXT NOT NULL CHECK(length(trim(industry_scope)) > 0),
    holding_horizon TEXT NOT NULL CHECK(length(trim(holding_horizon)) > 0),
    method_categories_json TEXT NOT NULL CHECK(json_valid(method_categories_json)),
    candidate_ids_json TEXT NOT NULL CHECK(json_valid(candidate_ids_json)),
    argument_unit_ids_json TEXT NOT NULL CHECK(json_valid(argument_unit_ids_json)),
    source_snapshot_ids_json TEXT NOT NULL CHECK(json_valid(source_snapshot_ids_json)),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    disposition TEXT NOT NULL CHECK(disposition IN ('ADMIT_AS_IS','REWRITE_AND_ADMIT')),
    skill_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    skill_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(skill_object_hash)=64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_json TEXT NOT NULL CHECK(json_valid(skill_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed=0),
    created_at TEXT NOT NULL,
    UNIQUE(run_id,group_id)
);

CREATE TABLE knowledge_semantic_admission_group_candidate (
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_admission_run(run_id),
    final_skill_id TEXT NOT NULL REFERENCES knowledge_semantic_admission_skill(final_skill_id),
    candidate_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    PRIMARY KEY(final_skill_id,candidate_id),
    UNIQUE(run_id,candidate_id),
    UNIQUE(final_skill_id,ordinal)
);

CREATE TABLE knowledge_semantic_admission_decision (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_semantic_admission_run(run_id),
    final_skill_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_admission_skill(final_skill_id),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash)=64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT')),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) >= 8),
    decision_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    decision_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(decision_object_hash)=64 AND decision_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed=0),
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_semantic_admission_release (
    release_id TEXT PRIMARY KEY,
    registry_version TEXT NOT NULL UNIQUE CHECK(length(trim(registry_version)) > 0),
    base_run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    admission_run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_admission_run(run_id),
    parent_audited_release_id TEXT NOT NULL,
    parent_audited_object_hash TEXT NOT NULL
        CHECK(length(parent_audited_object_hash)=64
            AND parent_audited_object_hash NOT GLOB '*[^0-9a-f]*'),
    parent_active_skill_count INTEGER NOT NULL CHECK(parent_active_skill_count >= 0),
    raw_candidate_count INTEGER NOT NULL CHECK(raw_candidate_count >= 1),
    exact_group_count INTEGER NOT NULL CHECK(exact_group_count >= 1),
    approved_skill_count INTEGER NOT NULL CHECK(approved_skill_count >= 0),
    rejected_skill_count INTEGER NOT NULL CHECK(rejected_skill_count >= 0),
    active_skill_count INTEGER NOT NULL CHECK(active_skill_count >= 0),
    decision_ids_json TEXT NOT NULL CHECK(json_valid(decision_ids_json)),
    member_ids_json TEXT NOT NULL CHECK(json_valid(member_ids_json)),
    audit_report_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    audit_report_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(audit_report_object_hash)=64
            AND audit_report_object_hash NOT GLOB '*[^0-9a-f]*'),
    release_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    release_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(release_object_hash)=64
            AND release_object_hash NOT GLOB '*[^0-9a-f]*'),
    release_json TEXT NOT NULL CHECK(json_valid(release_json)),
    formal_committee_weight_allowed INTEGER NOT NULL CHECK(formal_committee_weight_allowed=0),
    created_at TEXT NOT NULL,
    CHECK(exact_group_count = approved_skill_count + rejected_skill_count),
    CHECK(active_skill_count = parent_active_skill_count + approved_skill_count)
);

CREATE TABLE knowledge_semantic_admission_member (
    release_id TEXT NOT NULL REFERENCES knowledge_semantic_admission_release(release_id),
    member_ordinal INTEGER NOT NULL CHECK(member_ordinal >= 1),
    final_skill_id TEXT NOT NULL UNIQUE REFERENCES knowledge_semantic_admission_skill(final_skill_id),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash)=64 AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    admission_basis TEXT NOT NULL CHECK(admission_basis='APPROVED'),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    PRIMARY KEY(release_id,final_skill_id),
    UNIQUE(release_id,member_ordinal)
);

CREATE INDEX idx_semantic_admission_run_base
ON knowledge_semantic_admission_run(base_run_id,created_at,run_id);

CREATE INDEX idx_semantic_admission_skill_run
ON knowledge_semantic_admission_skill(run_id,primary_module,final_skill_id);

CREATE INDEX idx_semantic_admission_release_base
ON knowledge_semantic_admission_release(base_run_id,created_at,release_id);

CREATE TRIGGER trg_semantic_admission_run_no_update
BEFORE UPDATE ON knowledge_semantic_admission_run
BEGIN
    SELECT RAISE(ABORT,'semantic admission runs are immutable');
END;

CREATE TRIGGER trg_semantic_admission_run_no_delete
BEFORE DELETE ON knowledge_semantic_admission_run
BEGIN
    SELECT RAISE(ABORT,'semantic admission runs are immutable');
END;

CREATE TRIGGER trg_semantic_admission_skill_no_update
BEFORE UPDATE ON knowledge_semantic_admission_skill
BEGIN
    SELECT RAISE(ABORT,'semantic admission Skills are immutable');
END;

CREATE TRIGGER trg_semantic_admission_skill_no_delete
BEFORE DELETE ON knowledge_semantic_admission_skill
BEGIN
    SELECT RAISE(ABORT,'semantic admission Skills are immutable');
END;

CREATE TRIGGER trg_semantic_admission_mapping_no_update
BEFORE UPDATE ON knowledge_semantic_admission_group_candidate
BEGIN
    SELECT RAISE(ABORT,'semantic admission group membership is immutable');
END;

CREATE TRIGGER trg_semantic_admission_mapping_no_delete
BEFORE DELETE ON knowledge_semantic_admission_group_candidate
BEGIN
    SELECT RAISE(ABORT,'semantic admission group membership is immutable');
END;

CREATE TRIGGER trg_semantic_admission_decision_no_update
BEFORE UPDATE ON knowledge_semantic_admission_decision
BEGIN
    SELECT RAISE(ABORT,'semantic admission decisions are append-only');
END;

CREATE TRIGGER trg_semantic_admission_decision_no_delete
BEFORE DELETE ON knowledge_semantic_admission_decision
BEGIN
    SELECT RAISE(ABORT,'semantic admission decisions are append-only');
END;

CREATE TRIGGER trg_semantic_admission_release_review_closed
BEFORE INSERT ON knowledge_semantic_admission_release
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM knowledge_semantic_admission_skill s
        LEFT JOIN knowledge_semantic_admission_decision d
          ON d.final_skill_id=s.final_skill_id
        WHERE s.run_id=NEW.admission_run_id AND d.decision_id IS NULL
    ) THEN RAISE(ABORT,'semantic admission release requires closed review') END;
END;

CREATE TRIGGER trg_semantic_admission_release_no_update
BEFORE UPDATE ON knowledge_semantic_admission_release
BEGIN
    SELECT RAISE(ABORT,'semantic admission releases are immutable');
END;

CREATE TRIGGER trg_semantic_admission_release_no_delete
BEFORE DELETE ON knowledge_semantic_admission_release
BEGIN
    SELECT RAISE(ABORT,'semantic admission releases are immutable');
END;

CREATE TRIGGER trg_semantic_admission_member_no_update
BEFORE UPDATE ON knowledge_semantic_admission_member
BEGIN
    SELECT RAISE(ABORT,'semantic admission members are immutable');
END;

CREATE TRIGGER trg_semantic_admission_member_no_delete
BEFORE DELETE ON knowledge_semantic_admission_member
BEGIN
    SELECT RAISE(ABORT,'semantic admission members are immutable');
END;
