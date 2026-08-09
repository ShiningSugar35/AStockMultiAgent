CREATE UNIQUE INDEX idx_knowledge_direct_final_review_identity
ON knowledge_direct_final_skill(run_id, final_skill_id, skill_object_hash);

CREATE TRIGGER trg_knowledge_direct_final_skill_frozen_insert
BEFORE INSERT ON knowledge_direct_final_skill
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = NEW.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skills are immutable');
END;

CREATE TRIGGER trg_knowledge_direct_final_skill_frozen_update
BEFORE UPDATE ON knowledge_direct_final_skill
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skills are immutable');
END;

CREATE TRIGGER trg_knowledge_direct_final_skill_frozen_delete
BEFORE DELETE ON knowledge_direct_final_skill
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skills are immutable');
END;

CREATE TRIGGER trg_knowledge_direct_final_source_ref_frozen_insert
BEFORE INSERT ON knowledge_direct_final_source_ref
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = NEW.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skill source refs are immutable');
END;

CREATE TRIGGER trg_knowledge_direct_final_source_ref_frozen_update
BEFORE UPDATE ON knowledge_direct_final_source_ref
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skill source refs are immutable');
END;

CREATE TRIGGER trg_knowledge_direct_final_source_ref_frozen_delete
BEFORE DELETE ON knowledge_direct_final_source_ref
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct Skill source refs are immutable');
END;

CREATE TRIGGER trg_knowledge_direct_shadow_bundle_frozen_insert
BEFORE INSERT ON knowledge_direct_shadow_bundle
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = NEW.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct ShadowBundle is immutable');
END;

CREATE TRIGGER trg_knowledge_direct_shadow_bundle_frozen_update
BEFORE UPDATE ON knowledge_direct_shadow_bundle
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct ShadowBundle is immutable');
END;

CREATE TRIGGER trg_knowledge_direct_shadow_bundle_frozen_delete
BEFORE DELETE ON knowledge_direct_shadow_bundle
WHEN EXISTS (
    SELECT 1 FROM knowledge_direct_run
    WHERE run_id = OLD.run_id AND stage = 'FINALIZED'
)
BEGIN
    SELECT RAISE(ABORT, 'finalized direct ShadowBundle is immutable');
END;

CREATE TABLE knowledge_direct_review_decision (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64
            AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision TEXT NOT NULL CHECK(decision IN ('APPROVE', 'REJECT')),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    decided_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason)) >= 8),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    decision_artifact_id TEXT NOT NULL UNIQUE
        REFERENCES artifact_registry(artifact_id),
    decision_object_hash TEXT NOT NULL
        CHECK(length(decision_object_hash) = 64
            AND decision_object_hash NOT GLOB '*[^0-9a-f]*'),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, final_skill_id),
    FOREIGN KEY(run_id, final_skill_id, skill_object_hash)
        REFERENCES knowledge_direct_final_skill(
            run_id,
            final_skill_id,
            skill_object_hash
        )
);

CREATE TRIGGER trg_knowledge_direct_review_target
BEFORE INSERT ON knowledge_direct_review_decision
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM knowledge_direct_final_skill skill
        JOIN knowledge_direct_run run ON run.run_id = skill.run_id
        WHERE skill.run_id = NEW.run_id
          AND skill.final_skill_id = NEW.final_skill_id
          AND skill.skill_object_hash = NEW.skill_object_hash
          AND skill.status = 'NEEDS_USER_REVIEW'
          AND run.stage = 'FINALIZED'
    ) THEN RAISE(ABORT, 'direct review target is not a finalized NEEDS_USER_REVIEW skill') END;
END;

CREATE TRIGGER trg_knowledge_direct_review_no_update
BEFORE UPDATE ON knowledge_direct_review_decision
BEGIN
    SELECT RAISE(ABORT, 'knowledge direct review decisions are append-only');
END;

CREATE TRIGGER trg_knowledge_direct_review_no_delete
BEFORE DELETE ON knowledge_direct_review_decision
BEGIN
    SELECT RAISE(ABORT, 'knowledge direct review decisions are append-only');
END;

CREATE TABLE knowledge_skill_registry_release (
    release_id TEXT PRIMARY KEY,
    registry_version TEXT NOT NULL UNIQUE CHECK(length(trim(registry_version)) > 0),
    run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_direct_run(run_id),
    total_skill_count INTEGER NOT NULL CHECK(total_skill_count >= 0),
    ready_skill_count INTEGER NOT NULL CHECK(ready_skill_count >= 0),
    approved_skill_count INTEGER NOT NULL CHECK(approved_skill_count >= 0),
    rejected_skill_count INTEGER NOT NULL CHECK(rejected_skill_count >= 0),
    admitted_skill_count INTEGER NOT NULL CHECK(admitted_skill_count >= 0),
    decision_ids_json TEXT NOT NULL CHECK(json_valid(decision_ids_json)),
    member_ids_json TEXT NOT NULL CHECK(json_valid(member_ids_json)),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    release_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    release_object_hash TEXT NOT NULL
        CHECK(length(release_object_hash) = 64
            AND release_object_hash NOT GLOB '*[^0-9a-f]*'),
    release_json TEXT NOT NULL CHECK(json_valid(release_json)),
    created_at TEXT NOT NULL,
    CHECK(admitted_skill_count = ready_skill_count + approved_skill_count),
    CHECK(total_skill_count = admitted_skill_count + rejected_skill_count)
);

CREATE TABLE knowledge_skill_registry_member (
    release_id TEXT NOT NULL REFERENCES knowledge_skill_registry_release(release_id),
    member_ordinal INTEGER NOT NULL CHECK(member_ordinal >= 1),
    run_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64
            AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_artifact_id TEXT NOT NULL REFERENCES artifact_registry(artifact_id),
    admission_basis TEXT NOT NULL CHECK(admission_basis IN ('READY', 'APPROVED')),
    source_hashes_json TEXT NOT NULL CHECK(json_valid(source_hashes_json)),
    PRIMARY KEY(release_id, final_skill_id),
    UNIQUE(release_id, member_ordinal),
    UNIQUE(release_id, skill_artifact_id),
    FOREIGN KEY(run_id, final_skill_id, skill_object_hash)
        REFERENCES knowledge_direct_final_skill(
            run_id,
            final_skill_id,
            skill_object_hash
        )
);

CREATE TRIGGER trg_knowledge_skill_release_review_closed
BEFORE INSERT ON knowledge_skill_registry_release
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM knowledge_direct_final_skill skill
        LEFT JOIN knowledge_direct_review_decision decision
          ON decision.run_id = skill.run_id
         AND decision.final_skill_id = skill.final_skill_id
        WHERE skill.run_id = NEW.run_id
          AND skill.status = 'NEEDS_USER_REVIEW'
          AND decision.decision_id IS NULL
    ) THEN RAISE(ABORT, 'knowledge registry release requires closed direct review') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_direct_run
        WHERE run_id = NEW.run_id AND stage = 'FINALIZED'
    ) THEN RAISE(ABORT, 'knowledge registry release requires finalized direct run') END;
    SELECT CASE WHEN NEW.total_skill_count != (
        SELECT COUNT(*) FROM knowledge_direct_final_skill WHERE run_id = NEW.run_id
    ) THEN RAISE(ABORT, 'knowledge registry total count drift') END;
    SELECT CASE WHEN NEW.ready_skill_count != (
        SELECT COUNT(*) FROM knowledge_direct_final_skill
        WHERE run_id = NEW.run_id AND status = 'READY_FOR_SHADOW'
    ) THEN RAISE(ABORT, 'knowledge registry ready count drift') END;
    SELECT CASE WHEN NEW.approved_skill_count != (
        SELECT COUNT(*) FROM knowledge_direct_review_decision
        WHERE run_id = NEW.run_id AND decision = 'APPROVE'
    ) THEN RAISE(ABORT, 'knowledge registry approve count drift') END;
    SELECT CASE WHEN NEW.rejected_skill_count != (
        SELECT COUNT(*) FROM knowledge_direct_review_decision
        WHERE run_id = NEW.run_id AND decision = 'REJECT'
    ) THEN RAISE(ABORT, 'knowledge registry reject count drift') END;
END;

CREATE TRIGGER trg_knowledge_skill_member_admission
BEFORE INSERT ON knowledge_skill_registry_member
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_skill_registry_release
        WHERE release_id = NEW.release_id AND run_id = NEW.run_id
    ) THEN RAISE(ABORT, 'knowledge registry member run/release mismatch') END;
    SELECT CASE WHEN NEW.admission_basis = 'READY' AND NOT EXISTS (
        SELECT 1 FROM knowledge_direct_final_skill
        WHERE run_id = NEW.run_id
          AND final_skill_id = NEW.final_skill_id
          AND skill_object_hash = NEW.skill_object_hash
          AND status = 'READY_FOR_SHADOW'
    ) THEN RAISE(ABORT, 'READY registry member is not a ready direct skill') END;
    SELECT CASE WHEN NEW.admission_basis = 'APPROVED' AND NOT EXISTS (
        SELECT 1
        FROM knowledge_direct_review_decision
        WHERE run_id = NEW.run_id
          AND final_skill_id = NEW.final_skill_id
          AND skill_object_hash = NEW.skill_object_hash
          AND decision = 'APPROVE'
    ) THEN RAISE(ABORT, 'APPROVED registry member lacks an approve decision') END;
END;

CREATE TRIGGER trg_knowledge_skill_release_no_update
BEFORE UPDATE ON knowledge_skill_registry_release
BEGIN
    SELECT RAISE(ABORT, 'knowledge skill registry releases are immutable');
END;

CREATE TRIGGER trg_knowledge_skill_release_no_delete
BEFORE DELETE ON knowledge_skill_registry_release
BEGIN
    SELECT RAISE(ABORT, 'knowledge skill registry releases are immutable');
END;

CREATE TRIGGER trg_knowledge_skill_member_no_update
BEFORE UPDATE ON knowledge_skill_registry_member
BEGIN
    SELECT RAISE(ABORT, 'knowledge skill registry members are immutable');
END;

CREATE TRIGGER trg_knowledge_skill_member_no_delete
BEFORE DELETE ON knowledge_skill_registry_member
BEGIN
    SELECT RAISE(ABORT, 'knowledge skill registry members are immutable');
END;

CREATE TABLE knowledge_zhihu_visual_asset (
    asset_id TEXT PRIMARY KEY,
    image_object_hash TEXT NOT NULL UNIQUE
        CHECK(length(image_object_hash) = 64
            AND image_object_hash NOT GLOB '*[^0-9a-f]*'),
    image_mime TEXT NOT NULL CHECK(image_mime IN (
        'image/gif',
        'image/jpeg',
        'image/png',
        'image/webp'
    )),
    byte_size INTEGER NOT NULL CHECK(byte_size > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_zhihu_visual_placement (
    placement_id TEXT PRIMARY KEY,
    source_snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    source_item_id TEXT NOT NULL CHECK(length(trim(source_item_id)) > 0),
    author_source_id TEXT NOT NULL CHECK(length(trim(author_source_id)) > 0),
    content_id TEXT NOT NULL CHECK(length(trim(content_id)) > 0),
    asset_id TEXT NOT NULL REFERENCES knowledge_zhihu_visual_asset(asset_id),
    url_hash TEXT NOT NULL
        CHECK(length(url_hash) = 64 AND url_hash NOT GLOB '*[^0-9a-f]*'),
    host_fingerprint TEXT NOT NULL
        CHECK(length(host_fingerprint) = 64
            AND host_fingerprint NOT GLOB '*[^0-9a-f]*'),
    path_fingerprint TEXT NOT NULL
        CHECK(length(path_fingerprint) = 64
            AND path_fingerprint NOT GLOB '*[^0-9a-f]*'),
    redirect_chain_hash TEXT NOT NULL
        CHECK(length(redirect_chain_hash) = 64
            AND redirect_chain_hash NOT GLOB '*[^0-9a-f]*'),
    redirect_count INTEGER NOT NULL CHECK(redirect_count >= 0),
    dom_path TEXT NOT NULL CHECK(length(trim(dom_path)) > 0),
    image_ordinal INTEGER NOT NULL CHECK(image_ordinal >= 1),
    standalone INTEGER NOT NULL CHECK(standalone = 0),
    merge_policy TEXT NOT NULL CHECK(merge_policy = 'MERGE_WITH_BOTH'),
    created_at TEXT NOT NULL,
    UNIQUE(source_snapshot_id, source_item_id, dom_path, image_ordinal)
);

CREATE TABLE knowledge_zhihu_visual_ocr_attempt (
    placement_id TEXT PRIMARY KEY
        REFERENCES knowledge_zhihu_visual_placement(placement_id),
    attempt_status TEXT NOT NULL CHECK(attempt_status IN (
        'SUCCEEDED',
        'NO_TEXT',
        'FAILED'
    )),
    engine_version TEXT NOT NULL CHECK(length(trim(engine_version)) > 0),
    ocr_text_object_hash TEXT
        CHECK(ocr_text_object_hash IS NULL OR (
            length(ocr_text_object_hash) = 64
            AND ocr_text_object_hash NOT GLOB '*[^0-9a-f]*'
        )),
    confidence REAL CHECK(confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    failure_reason TEXT,
    ocr_record_object_hash TEXT NOT NULL
        CHECK(length(ocr_record_object_hash) = 64
            AND ocr_record_object_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    CHECK(
        (attempt_status = 'SUCCEEDED'
            AND ocr_text_object_hash IS NOT NULL
            AND confidence IS NOT NULL
            AND failure_reason IS NULL)
        OR
        (attempt_status IN ('NO_TEXT', 'FAILED')
            AND ocr_text_object_hash IS NULL
            AND length(trim(failure_reason)) >= 8)
    )
);

CREATE TABLE knowledge_zhihu_visual_classification (
    placement_id TEXT PRIMARY KEY
        REFERENCES knowledge_zhihu_visual_placement(placement_id),
    visual_type TEXT NOT NULL CHECK(visual_type IN (
        'CHART',
        'TABLE',
        'DIAGRAM',
        'SCREENSHOT',
        'DECORATIVE',
        'OTHER'
    )),
    classifier_version TEXT NOT NULL CHECK(length(trim(classifier_version)) > 0),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    classification_object_hash TEXT NOT NULL
        CHECK(length(classification_object_hash) = 64
            AND classification_object_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_zhihu_visual_context (
    placement_id TEXT NOT NULL
        REFERENCES knowledge_zhihu_visual_placement(placement_id),
    context_role TEXT NOT NULL CHECK(context_role IN ('PRECEDING', 'FOLLOWING')),
    paragraph_id TEXT NOT NULL CHECK(length(trim(paragraph_id)) > 0),
    paragraph_ordinal INTEGER NOT NULL CHECK(paragraph_ordinal >= 1),
    text_object_hash TEXT NOT NULL
        CHECK(length(text_object_hash) = 64
            AND text_object_hash NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(placement_id, context_role),
    UNIQUE(placement_id, paragraph_id)
);

CREATE TABLE knowledge_zhihu_visual_argument_rebuild (
    placement_id TEXT NOT NULL
        REFERENCES knowledge_zhihu_visual_placement(placement_id),
    argument_unit_id TEXT NOT NULL CHECK(length(trim(argument_unit_id)) > 0),
    previous_argument_object_hash TEXT NOT NULL
        CHECK(length(previous_argument_object_hash) = 64
            AND previous_argument_object_hash NOT GLOB '*[^0-9a-f]*'),
    rebuilt_argument_object_hash TEXT
        CHECK(rebuilt_argument_object_hash IS NULL OR (
            length(rebuilt_argument_object_hash) = 64
            AND rebuilt_argument_object_hash NOT GLOB '*[^0-9a-f]*'
        )),
    rebuild_status TEXT NOT NULL CHECK(rebuild_status IN ('READY', 'NEEDS_REVIEW')),
    reason TEXT,
    rebuild_record_object_hash TEXT NOT NULL
        CHECK(length(rebuild_record_object_hash) = 64
            AND rebuild_record_object_hash NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(placement_id, argument_unit_id),
    CHECK(
        (rebuild_status = 'READY'
            AND rebuilt_argument_object_hash IS NOT NULL
            AND reason IS NULL)
        OR
        (rebuild_status = 'NEEDS_REVIEW' AND length(trim(reason)) >= 8)
    )
);

CREATE TABLE knowledge_zhihu_visual_packet (
    packet_id TEXT PRIMARY KEY,
    placement_id TEXT NOT NULL UNIQUE
        REFERENCES knowledge_zhihu_visual_placement(placement_id),
    packet_status TEXT NOT NULL CHECK(packet_status IN ('READY', 'NEEDS_REVIEW')),
    reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
    stages_json TEXT NOT NULL CHECK(json_valid(stages_json)),
    packet_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_registry(artifact_id),
    packet_object_hash TEXT NOT NULL
        CHECK(length(packet_object_hash) = 64
            AND packet_object_hash NOT GLOB '*[^0-9a-f]*'),
    packet_json TEXT NOT NULL CHECK(json_valid(packet_json)),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_knowledge_zhihu_visual_source
ON knowledge_zhihu_visual_placement(
    author_source_id,
    source_item_id,
    image_ordinal
);

CREATE INDEX idx_knowledge_zhihu_visual_packet_status
ON knowledge_zhihu_visual_packet(packet_status, placement_id);

CREATE TRIGGER trg_knowledge_zhihu_visual_packet_complete
BEFORE INSERT ON knowledge_zhihu_visual_packet
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_zhihu_visual_ocr_attempt
        WHERE placement_id = NEW.placement_id
    ) THEN RAISE(ABORT, 'Zhihu visual packet requires an OCR attempt') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_zhihu_visual_classification
        WHERE placement_id = NEW.placement_id
    ) THEN RAISE(ABORT, 'Zhihu visual packet requires a classification') END;
    SELECT CASE WHEN (
        SELECT COUNT(*) FROM knowledge_zhihu_visual_context
        WHERE placement_id = NEW.placement_id
    ) != 2 THEN RAISE(ABORT, 'Zhihu visual packet requires both context sides') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_zhihu_visual_argument_rebuild
        WHERE placement_id = NEW.placement_id
    ) THEN RAISE(ABORT, 'Zhihu visual packet requires an affected AU rebuild') END;
    SELECT CASE WHEN NEW.packet_status = 'READY' AND EXISTS (
        SELECT 1 FROM knowledge_zhihu_visual_argument_rebuild
        WHERE placement_id = NEW.placement_id AND rebuild_status != 'READY'
    ) THEN RAISE(ABORT, 'READY Zhihu visual packet has unresolved AU rebuilds') END;
    SELECT CASE WHEN NEW.packet_status = 'READY' AND EXISTS (
        SELECT 1 FROM knowledge_zhihu_visual_ocr_attempt
        WHERE placement_id = NEW.placement_id AND attempt_status = 'FAILED'
    ) THEN RAISE(ABORT, 'READY Zhihu visual packet cannot contain failed OCR') END;
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_asset_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_asset
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual assets are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_asset_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_asset
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual assets are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_placement_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_placement
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual placements are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_placement_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_placement
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual placements are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_ocr_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_ocr_attempt
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual OCR attempts are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_ocr_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_ocr_attempt
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual OCR attempts are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_classification_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_classification
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual classifications are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_classification_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_classification
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual classifications are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_context_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_context
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual contexts are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_context_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_context
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual contexts are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_rebuild_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_argument_rebuild
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual AU rebuilds are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_rebuild_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_argument_rebuild
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual AU rebuilds are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_packet_no_update
BEFORE UPDATE ON knowledge_zhihu_visual_packet
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual packets are immutable');
END;

CREATE TRIGGER trg_knowledge_zhihu_visual_packet_no_delete
BEFORE DELETE ON knowledge_zhihu_visual_packet
BEGIN
    SELECT RAISE(ABORT, 'Zhihu visual packets are immutable');
END;
