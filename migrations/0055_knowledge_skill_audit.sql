CREATE TABLE IF NOT EXISTS knowledge_skill_audit_run (
    audit_run_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    source_registry_release_id TEXT NOT NULL,
    source_registry_object_hash TEXT NOT NULL CHECK (length(source_registry_object_hash)=64),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash)=64),
    evidence_catalog_hash TEXT NOT NULL CHECK (length(evidence_catalog_hash)=64),
    expected_skill_count INTEGER NOT NULL CHECK (expected_skill_count > 0),
    decision_count INTEGER NOT NULL CHECK (decision_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('PLANNED','DECISIONS_COMPLETE','PUBLISHED')),
    run_artifact_id TEXT NOT NULL,
    run_object_hash TEXT NOT NULL CHECK (length(run_object_hash)=64),
    run_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_skill_audit_decision (
    decision_id TEXT PRIMARY KEY,
    audit_run_id TEXT NOT NULL REFERENCES knowledge_skill_audit_run(audit_run_id),
    source_skill_id TEXT NOT NULL,
    source_skill_object_hash TEXT NOT NULL CHECK (length(source_skill_object_hash)=64),
    source_skill_artifact_id TEXT NOT NULL,
    skill_origin TEXT NOT NULL CHECK (skill_origin IN ('DIRECT','VISUAL_OVERLAY')),
    verdict TEXT NOT NULL CHECK (verdict IN ('KEEP','KEEP_SCOPED','REVISE','RETIRE')),
    premise_scope TEXT NOT NULL,
    risk_codes_json TEXT NOT NULL,
    conflict_groups_json TEXT NOT NULL,
    external_evidence_ids_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    replacement_skill_id TEXT,
    replacement_skill_object_hash TEXT CHECK (replacement_skill_object_hash IS NULL OR length(replacement_skill_object_hash)=64),
    replacement_skill_artifact_id TEXT,
    decision_object_hash TEXT NOT NULL CHECK (length(decision_object_hash)=64),
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(audit_run_id, source_skill_id),
    CHECK (
        (verdict='REVISE' AND replacement_skill_id IS NOT NULL AND replacement_skill_object_hash IS NOT NULL AND replacement_skill_artifact_id IS NOT NULL)
        OR
        (verdict<>'REVISE' AND replacement_skill_id IS NULL AND replacement_skill_object_hash IS NULL AND replacement_skill_artifact_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS knowledge_skill_audit_decision_run_idx
ON knowledge_skill_audit_decision(audit_run_id, verdict, source_skill_id);

CREATE TABLE IF NOT EXISTS knowledge_skill_audited_registry_release (
    release_id TEXT PRIMARY KEY,
    audit_run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_skill_audit_run(audit_run_id),
    source_run_id TEXT NOT NULL,
    source_registry_release_id TEXT NOT NULL,
    source_registry_object_hash TEXT NOT NULL CHECK (length(source_registry_object_hash)=64),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash)=64),
    evidence_catalog_hash TEXT NOT NULL CHECK (length(evidence_catalog_hash)=64),
    source_skill_count INTEGER NOT NULL CHECK (source_skill_count > 0),
    decision_count INTEGER NOT NULL CHECK (decision_count > 0),
    keep_count INTEGER NOT NULL CHECK (keep_count >= 0),
    keep_scoped_count INTEGER NOT NULL CHECK (keep_scoped_count >= 0),
    revise_count INTEGER NOT NULL CHECK (revise_count >= 0),
    retire_count INTEGER NOT NULL CHECK (retire_count >= 0),
    curated_count INTEGER NOT NULL CHECK (curated_count >= 0),
    active_skill_count INTEGER NOT NULL CHECK (active_skill_count >= 0),
    release_artifact_id TEXT NOT NULL,
    release_object_hash TEXT NOT NULL CHECK (length(release_object_hash)=64),
    release_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (decision_count=source_skill_count),
    CHECK (keep_count + keep_scoped_count + revise_count + retire_count = source_skill_count),
    CHECK (active_skill_count = keep_count + keep_scoped_count + revise_count + curated_count)
);

CREATE TABLE IF NOT EXISTS knowledge_skill_audited_registry_member (
    release_id TEXT NOT NULL REFERENCES knowledge_skill_audited_registry_release(release_id),
    member_ordinal INTEGER NOT NULL CHECK (member_ordinal >= 1),
    effective_skill_id TEXT NOT NULL,
    effective_skill_object_hash TEXT NOT NULL CHECK (length(effective_skill_object_hash)=64),
    effective_skill_artifact_id TEXT NOT NULL,
    source_skill_id TEXT,
    decision_id TEXT REFERENCES knowledge_skill_audit_decision(decision_id),
    skill_origin TEXT NOT NULL CHECK (skill_origin IN ('DIRECT','VISUAL_OVERLAY','CURATED','REVISED')),
    admission_basis TEXT NOT NULL,
    source_hashes_json TEXT NOT NULL,
    selection_row_json TEXT NOT NULL,
    PRIMARY KEY(release_id, effective_skill_id),
    UNIQUE(release_id, member_ordinal)
);

CREATE INDEX IF NOT EXISTS knowledge_skill_audited_registry_member_source_idx
ON knowledge_skill_audited_registry_member(release_id, source_skill_id);
