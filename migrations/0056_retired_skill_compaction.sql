CREATE TABLE IF NOT EXISTS knowledge_retired_skill_tombstone (
    source_skill_id TEXT PRIMARY KEY,
    source_skill_object_hash TEXT NOT NULL CHECK(length(source_skill_object_hash)=64),
    skill_origin TEXT NOT NULL CHECK(skill_origin IN ('DIRECT','VISUAL_OVERLAY')),
    audit_run_id TEXT NOT NULL REFERENCES knowledge_skill_audit_run(audit_run_id),
    source_skill_artifact_id TEXT NOT NULL,
    removed_object_bytes INTEGER NOT NULL DEFAULT 0 CHECK(removed_object_bytes >= 0),
    compacted_at TEXT NOT NULL
);

DROP TRIGGER IF EXISTS trg_knowledge_direct_final_skill_frozen_delete;
CREATE TRIGGER trg_knowledge_direct_final_skill_frozen_delete
BEFORE DELETE ON knowledge_direct_final_skill
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
AND NOT EXISTS (
    SELECT 1 FROM knowledge_retired_skill_tombstone tombstone
    WHERE tombstone.source_skill_id = OLD.final_skill_id
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skills are immutable unless explicitly retired');
END;

DROP TRIGGER IF EXISTS trg_knowledge_direct_final_source_ref_frozen_delete;
CREATE TRIGGER trg_knowledge_direct_final_source_ref_frozen_delete
BEFORE DELETE ON knowledge_direct_final_source_ref
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
AND NOT EXISTS (
    SELECT 1 FROM knowledge_retired_skill_tombstone tombstone
    WHERE tombstone.source_skill_id = OLD.final_skill_id
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skill source refs are immutable unless explicitly retired');
END;

DROP TRIGGER IF EXISTS trg_knowledge_skill_member_no_delete;
CREATE TRIGGER trg_knowledge_skill_member_no_delete
BEFORE DELETE ON knowledge_skill_registry_member
WHEN NOT EXISTS (
    SELECT 1 FROM knowledge_retired_skill_tombstone tombstone
    WHERE tombstone.source_skill_id = OLD.final_skill_id
)
BEGIN
    SELECT RAISE(ABORT, 'knowledge skill registry members are immutable unless explicitly retired');
END;

DROP TRIGGER IF EXISTS trg_knowledge_visual_skill_candidate_no_delete;
CREATE TRIGGER trg_knowledge_visual_skill_candidate_no_delete
BEFORE DELETE ON knowledge_visual_skill_candidate
WHEN NOT EXISTS (
    SELECT 1 FROM knowledge_retired_skill_tombstone tombstone
    WHERE tombstone.source_skill_id = OLD.final_skill_id
)
BEGIN
    SELECT RAISE(ABORT, 'visual Skill candidates are immutable unless explicitly retired');
END;

DROP TRIGGER IF EXISTS trg_knowledge_visual_skill_review_no_delete;
CREATE TRIGGER trg_knowledge_visual_skill_review_no_delete
BEFORE DELETE ON knowledge_visual_skill_review_decision
WHEN NOT EXISTS (
    SELECT 1 FROM knowledge_retired_skill_tombstone tombstone
    WHERE tombstone.source_skill_id = OLD.final_skill_id
)
BEGIN
    SELECT RAISE(ABORT, 'visual Skill review decisions are append-only unless their Skill is retired');
END;

DROP TRIGGER IF EXISTS trg_knowledge_visual_skill_member_no_delete;
CREATE TRIGGER trg_knowledge_visual_skill_member_no_delete
BEFORE DELETE ON knowledge_visual_skill_member
WHEN NOT EXISTS (
    SELECT 1 FROM knowledge_retired_skill_tombstone tombstone
    WHERE tombstone.source_skill_id = OLD.final_skill_id
)
BEGIN
    SELECT RAISE(ABORT, 'visual Skill release members are immutable unless explicitly retired');
END;
