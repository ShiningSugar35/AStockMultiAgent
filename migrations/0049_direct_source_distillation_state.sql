CREATE TABLE knowledge_direct_run (
    run_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL UNIQUE
        CHECK(length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'),
    pipeline_version TEXT NOT NULL CHECK(length(trim(pipeline_version)) > 0),
    stage TEXT NOT NULL CHECK(stage IN (
        'INITIALIZED',
        'PACKETS_EXPORTING',
        'BATCHES_IMPORTED',
        'FINALIZED'
    )),
    frozen_source_count INTEGER NOT NULL CHECK(frozen_source_count >= 1),
    frozen_batch_count INTEGER NOT NULL CHECK(frozen_batch_count >= 1),
    manifest_object_hash TEXT NOT NULL
        CHECK(length(manifest_object_hash) = 64
            AND manifest_object_hash NOT GLOB '*[^0-9a-f]*'),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    init_replay_count INTEGER NOT NULL DEFAULT 0 CHECK(init_replay_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT,
    CHECK((stage = 'FINALIZED' AND finalized_at IS NOT NULL)
        OR (stage <> 'FINALIZED' AND finalized_at IS NULL))
);

CREATE TABLE knowledge_direct_source (
    run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    source_file_hash TEXT NOT NULL
        CHECK(length(source_file_hash) = 64
            AND source_file_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, source_id),
    UNIQUE(run_id, source_id, source_file_hash)
);

CREATE TABLE knowledge_direct_chapter_batch (
    run_id TEXT NOT NULL REFERENCES knowledge_direct_run(run_id),
    batch_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chapter_unit_id TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL CHECK(batch_ordinal >= 1),
    stage TEXT NOT NULL CHECK(stage IN ('FROZEN', 'PACKET_EXPORTED', 'IMPORTED')),
    packet_hash TEXT
        CHECK(packet_hash IS NULL OR (
            length(packet_hash) = 64 AND packet_hash NOT GLOB '*[^0-9a-f]*'
        )),
    packet_object_hash TEXT
        CHECK(packet_object_hash IS NULL OR (
            length(packet_object_hash) = 64
            AND packet_object_hash NOT GLOB '*[^0-9a-f]*'
        )),
    batch_text_object_hash TEXT
        CHECK(batch_text_object_hash IS NULL OR (
            length(batch_text_object_hash) = 64
            AND batch_text_object_hash NOT GLOB '*[^0-9a-f]*'
        )),
    import_input_hash TEXT
        CHECK(import_input_hash IS NULL OR (
            length(import_input_hash) = 64
            AND import_input_hash NOT GLOB '*[^0-9a-f]*'
        )),
    import_object_hash TEXT
        CHECK(import_object_hash IS NULL OR (
            length(import_object_hash) = 64
            AND import_object_hash NOT GLOB '*[^0-9a-f]*'
        )),
    imported_candidate_count INTEGER CHECK(
        imported_candidate_count IS NULL OR imported_candidate_count >= 0
    ),
    no_skill_reason TEXT,
    packet_replay_count INTEGER NOT NULL DEFAULT 0 CHECK(packet_replay_count >= 0),
    import_replay_count INTEGER NOT NULL DEFAULT 0 CHECK(import_replay_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    imported_at TEXT,
    PRIMARY KEY(run_id, batch_id),
    UNIQUE(run_id, chapter_unit_id),
    UNIQUE(run_id, batch_ordinal),
    FOREIGN KEY(run_id, source_id)
        REFERENCES knowledge_direct_source(run_id, source_id),
    CHECK(
        (stage = 'FROZEN'
            AND packet_hash IS NULL
            AND packet_object_hash IS NULL
            AND batch_text_object_hash IS NULL
            AND import_input_hash IS NULL
            AND import_object_hash IS NULL
            AND imported_candidate_count IS NULL
            AND imported_at IS NULL)
        OR
        (stage = 'PACKET_EXPORTED'
            AND packet_hash IS NOT NULL
            AND packet_object_hash IS NOT NULL
            AND batch_text_object_hash IS NOT NULL
            AND import_input_hash IS NULL
            AND import_object_hash IS NULL
            AND imported_candidate_count IS NULL
            AND imported_at IS NULL)
        OR
        (stage = 'IMPORTED'
            AND packet_hash IS NOT NULL
            AND packet_object_hash IS NOT NULL
            AND batch_text_object_hash IS NOT NULL
            AND import_input_hash IS NOT NULL
            AND import_object_hash IS NOT NULL
            AND imported_candidate_count IS NOT NULL
            AND imported_at IS NOT NULL)
    ),
    CHECK(
        stage <> 'IMPORTED'
        OR (imported_candidate_count > 0 AND no_skill_reason IS NULL)
        OR (imported_candidate_count = 0 AND length(trim(no_skill_reason)) >= 8)
    )
);

CREATE TABLE knowledge_direct_chapter_fragment (
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    context_role TEXT NOT NULL CHECK(context_role IN (
        'CONTEXT_BEFORE',
        'CURRENT',
        'CONTEXT_AFTER'
    )),
    fragment_ordinal INTEGER NOT NULL CHECK(fragment_ordinal >= 1),
    object_hash TEXT NOT NULL
        CHECK(length(object_hash) = 64 AND object_hash NOT GLOB '*[^0-9a-f]*'),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 1),
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset > start_offset),
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    PRIMARY KEY(run_id, batch_id, fragment_id),
    UNIQUE(run_id, batch_id, fragment_id, object_hash),
    UNIQUE(run_id, batch_id, context_role, fragment_ordinal),
    FOREIGN KEY(run_id, batch_id)
        REFERENCES knowledge_direct_chapter_batch(run_id, batch_id)
);

CREATE TABLE knowledge_direct_chapter_visual_ref (
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    visual_ordinal INTEGER NOT NULL CHECK(visual_ordinal >= 1),
    object_hash TEXT NOT NULL
        CHECK(length(object_hash) = 64 AND object_hash NOT GLOB '*[^0-9a-f]*'),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 1),
    evidence_locator_json TEXT NOT NULL CHECK(json_valid(evidence_locator_json)),
    PRIMARY KEY(run_id, batch_id, evidence_id),
    UNIQUE(run_id, batch_id, evidence_id, object_hash),
    UNIQUE(run_id, batch_id, visual_ordinal),
    FOREIGN KEY(run_id, batch_id)
        REFERENCES knowledge_direct_chapter_batch(run_id, batch_id)
);

CREATE TABLE knowledge_direct_raw_sol_candidate (
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    chapter_unit_id TEXT NOT NULL,
    sol_version_id TEXT NOT NULL CHECK(length(trim(sol_version_id)) > 0),
    primary_module TEXT NOT NULL CHECK(primary_module IN (
        'SOURCING_SCREENING',
        'FUNDAMENTAL_RESEARCH',
        'VALUATION_PRICING',
        'PORTFOLIO_CONSTRUCTION',
        'POSITION_RISK_MANAGEMENT',
        'PSYCHOLOGY_BEHAVIOR'
    )),
    secondary_modules_json TEXT NOT NULL CHECK(json_valid(secondary_modules_json)),
    secondary_module_count INTEGER NOT NULL CHECK(secondary_module_count >= 0),
    status TEXT NOT NULL CHECK(status IN (
        'READY_FOR_SHADOW',
        'NEEDS_USER_REVIEW'
    )),
    skill_name TEXT NOT NULL CHECK(length(trim(skill_name)) > 0),
    decision_question TEXT NOT NULL CHECK(length(trim(decision_question)) > 0),
    core_principle TEXT NOT NULL CHECK(length(trim(core_principle)) > 0),
    applicable_conditions_json TEXT NOT NULL CHECK(json_valid(applicable_conditions_json)),
    reasoning_steps_json TEXT NOT NULL CHECK(json_valid(reasoning_steps_json)),
    reasoning_step_count INTEGER NOT NULL CHECK(reasoning_step_count >= 0),
    required_evidence_json TEXT NOT NULL CHECK(json_valid(required_evidence_json)),
    positive_signals_json TEXT NOT NULL CHECK(json_valid(positive_signals_json)),
    negative_signals_json TEXT NOT NULL CHECK(json_valid(negative_signals_json)),
    invalidation_conditions_json TEXT NOT NULL CHECK(json_valid(invalidation_conditions_json)),
    failure_modes_json TEXT NOT NULL CHECK(json_valid(failure_modes_json)),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    source_ref_count INTEGER NOT NULL CHECK(source_ref_count >= 0),
    visual_ref_count INTEGER NOT NULL CHECK(visual_ref_count >= 0),
    uncertainty_reason TEXT,
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    candidate_object_hash TEXT NOT NULL
        CHECK(length(candidate_object_hash) = 64
            AND candidate_object_hash NOT GLOB '*[^0-9a-f]*'),
    candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, candidate_id),
    UNIQUE(run_id, batch_id, candidate_id),
    UNIQUE(run_id, candidate_object_hash),
    FOREIGN KEY(run_id, batch_id)
        REFERENCES knowledge_direct_chapter_batch(run_id, batch_id),
    CHECK(
        (status = 'READY_FOR_SHADOW'
            AND source_ref_count >= 1
            AND uncertainty_reason IS NULL)
        OR
        (status = 'NEEDS_USER_REVIEW'
            AND length(trim(uncertainty_reason)) >= 8)
    )
);

CREATE TABLE knowledge_direct_candidate_source_ref (
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    source_id TEXT NOT NULL,
    source_file_hash TEXT NOT NULL,
    chapter_unit_id TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    fragment_object_hash TEXT NOT NULL
        CHECK(length(fragment_object_hash) = 64
            AND fragment_object_hash NOT GLOB '*[^0-9a-f]*'),
    source_object_hash TEXT NOT NULL
        CHECK(length(source_object_hash) = 64
            AND source_object_hash NOT GLOB '*[^0-9a-f]*'),
    slice_hash TEXT NOT NULL
        CHECK(length(slice_hash) = 64 AND slice_hash NOT GLOB '*[^0-9a-f]*'),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 1),
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset > start_offset),
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    original_locator TEXT NOT NULL CHECK(length(trim(original_locator)) > 0),
    paragraph_head TEXT NOT NULL CHECK(length(trim(paragraph_head)) > 0),
    visual_evidence_ids_json TEXT NOT NULL CHECK(json_valid(visual_evidence_ids_json)),
    PRIMARY KEY(run_id, candidate_id, ref_ordinal),
    UNIQUE(run_id, candidate_id, source_object_hash, original_locator),
    FOREIGN KEY(run_id, batch_id, candidate_id)
        REFERENCES knowledge_direct_raw_sol_candidate(run_id, batch_id, candidate_id),
    FOREIGN KEY(run_id, batch_id, fragment_id)
        REFERENCES knowledge_direct_chapter_fragment(run_id, batch_id, fragment_id),
    FOREIGN KEY(run_id, batch_id, fragment_id, fragment_object_hash)
        REFERENCES knowledge_direct_chapter_fragment(
            run_id,
            batch_id,
            fragment_id,
            object_hash
        ),
    FOREIGN KEY(run_id, source_id, source_file_hash)
        REFERENCES knowledge_direct_source(run_id, source_id, source_file_hash),
    CHECK(source_object_hash = slice_hash)
);

CREATE TABLE knowledge_direct_candidate_visual_ref (
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    evidence_id TEXT NOT NULL,
    object_hash TEXT NOT NULL
        CHECK(length(object_hash) = 64 AND object_hash NOT GLOB '*[^0-9a-f]*'),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 1),
    evidence_locator_json TEXT NOT NULL CHECK(json_valid(evidence_locator_json)),
    PRIMARY KEY(run_id, candidate_id, ref_ordinal),
    UNIQUE(run_id, candidate_id, evidence_id),
    FOREIGN KEY(run_id, batch_id, candidate_id)
        REFERENCES knowledge_direct_raw_sol_candidate(run_id, batch_id, candidate_id),
    FOREIGN KEY(run_id, batch_id, evidence_id)
        REFERENCES knowledge_direct_chapter_visual_ref(run_id, batch_id, evidence_id),
    FOREIGN KEY(run_id, batch_id, evidence_id, object_hash)
        REFERENCES knowledge_direct_chapter_visual_ref(
            run_id,
            batch_id,
            evidence_id,
            object_hash
        )
);

CREATE TABLE knowledge_direct_sol_confirmed_dedup_manifest (
    run_id TEXT NOT NULL UNIQUE REFERENCES knowledge_direct_run(run_id),
    manifest_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE
        CHECK(length(manifest_hash) = 64 AND manifest_hash NOT GLOB '*[^0-9a-f]*'),
    manifest_object_hash TEXT NOT NULL
        CHECK(length(manifest_object_hash) = 64
            AND manifest_object_hash NOT GLOB '*[^0-9a-f]*'),
    embedding_usage TEXT NOT NULL CHECK(
        embedding_usage = 'POST_GENERATION_ASSIST_ONLY'
    ),
    sol_confirmed INTEGER NOT NULL CHECK(sol_confirmed = 1),
    sol_version TEXT NOT NULL CHECK(length(trim(sol_version)) > 0),
    sol_version_hash TEXT NOT NULL
        CHECK(length(sol_version_hash) = 64
            AND sol_version_hash NOT GLOB '*[^0-9a-f]*'),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    finalize_replay_count INTEGER NOT NULL DEFAULT 0 CHECK(finalize_replay_count >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, manifest_id)
);

CREATE TABLE knowledge_direct_final_skill (
    run_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'READY_FOR_SHADOW',
        'NEEDS_USER_REVIEW'
    )),
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
    secondary_module_count INTEGER NOT NULL CHECK(secondary_module_count >= 0),
    decision_question TEXT NOT NULL CHECK(length(trim(decision_question)) > 0),
    core_principle TEXT NOT NULL CHECK(length(trim(core_principle)) > 0),
    applicable_conditions_json TEXT NOT NULL CHECK(json_valid(applicable_conditions_json)),
    reasoning_steps_json TEXT NOT NULL CHECK(json_valid(reasoning_steps_json)),
    reasoning_step_count INTEGER NOT NULL CHECK(reasoning_step_count >= 0),
    required_evidence_json TEXT NOT NULL CHECK(json_valid(required_evidence_json)),
    positive_signals_json TEXT NOT NULL CHECK(json_valid(positive_signals_json)),
    negative_signals_json TEXT NOT NULL CHECK(json_valid(negative_signals_json)),
    invalidation_conditions_json TEXT NOT NULL CHECK(json_valid(invalidation_conditions_json)),
    failure_modes_json TEXT NOT NULL CHECK(json_valid(failure_modes_json)),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    module_count INTEGER NOT NULL CHECK(module_count >= 1),
    contribution_count INTEGER NOT NULL CHECK(contribution_count >= 1),
    source_ref_count INTEGER NOT NULL CHECK(source_ref_count >= 0),
    visual_ref_count INTEGER NOT NULL CHECK(visual_ref_count >= 0),
    uncertainty_reason TEXT,
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    skill_object_hash TEXT NOT NULL
        CHECK(length(skill_object_hash) = 64
            AND skill_object_hash NOT GLOB '*[^0-9a-f]*'),
    skill_json TEXT NOT NULL CHECK(json_valid(skill_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, final_skill_id),
    UNIQUE(run_id, skill_object_hash),
    FOREIGN KEY(run_id, manifest_id)
        REFERENCES knowledge_direct_sol_confirmed_dedup_manifest(run_id, manifest_id),
    CHECK(
        (status = 'READY_FOR_SHADOW'
            AND source_ref_count >= 1
            AND uncertainty_reason IS NULL)
        OR
        (status = 'NEEDS_USER_REVIEW'
            AND length(trim(uncertainty_reason)) >= 8)
    )
);

CREATE TABLE knowledge_direct_final_skill_module (
    run_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    module_ordinal INTEGER NOT NULL CHECK(module_ordinal >= 1),
    module_role TEXT NOT NULL CHECK(module_role IN ('PRIMARY', 'SECONDARY')),
    module TEXT NOT NULL CHECK(module IN (
        'SOURCING_SCREENING',
        'FUNDAMENTAL_RESEARCH',
        'VALUATION_PRICING',
        'PORTFOLIO_CONSTRUCTION',
        'POSITION_RISK_MANAGEMENT',
        'PSYCHOLOGY_BEHAVIOR'
    )),
    PRIMARY KEY(run_id, final_skill_id, module_role, module_ordinal),
    UNIQUE(run_id, final_skill_id, module),
    FOREIGN KEY(run_id, final_skill_id)
        REFERENCES knowledge_direct_final_skill(run_id, final_skill_id)
);

CREATE TABLE knowledge_direct_final_to_candidate_contribution (
    run_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    contribution_ordinal INTEGER NOT NULL CHECK(contribution_ordinal >= 1),
    candidate_id TEXT NOT NULL,
    PRIMARY KEY(run_id, final_skill_id, contribution_ordinal),
    UNIQUE(run_id, candidate_id),
    FOREIGN KEY(run_id, final_skill_id)
        REFERENCES knowledge_direct_final_skill(run_id, final_skill_id),
    FOREIGN KEY(run_id, candidate_id)
        REFERENCES knowledge_direct_raw_sol_candidate(run_id, candidate_id)
);

CREATE UNIQUE INDEX idx_knowledge_direct_final_one_primary_module
ON knowledge_direct_final_skill_module(run_id, final_skill_id)
WHERE module_role = 'PRIMARY';

CREATE TABLE knowledge_direct_final_source_ref (
    run_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    batch_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_file_hash TEXT NOT NULL,
    chapter_unit_id TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    fragment_object_hash TEXT NOT NULL
        CHECK(length(fragment_object_hash) = 64
            AND fragment_object_hash NOT GLOB '*[^0-9a-f]*'),
    source_object_hash TEXT NOT NULL
        CHECK(length(source_object_hash) = 64
            AND source_object_hash NOT GLOB '*[^0-9a-f]*'),
    slice_hash TEXT NOT NULL
        CHECK(length(slice_hash) = 64 AND slice_hash NOT GLOB '*[^0-9a-f]*'),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 1),
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset > start_offset),
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    original_locator TEXT NOT NULL CHECK(length(trim(original_locator)) > 0),
    paragraph_head TEXT NOT NULL CHECK(length(trim(paragraph_head)) > 0),
    visual_evidence_ids_json TEXT NOT NULL CHECK(json_valid(visual_evidence_ids_json)),
    PRIMARY KEY(run_id, final_skill_id, ref_ordinal),
    UNIQUE(run_id, final_skill_id, source_object_hash, original_locator),
    FOREIGN KEY(run_id, final_skill_id)
        REFERENCES knowledge_direct_final_skill(run_id, final_skill_id),
    FOREIGN KEY(run_id, batch_id, fragment_id)
        REFERENCES knowledge_direct_chapter_fragment(run_id, batch_id, fragment_id),
    FOREIGN KEY(run_id, batch_id, fragment_id, fragment_object_hash)
        REFERENCES knowledge_direct_chapter_fragment(
            run_id,
            batch_id,
            fragment_id,
            object_hash
        ),
    FOREIGN KEY(run_id, source_id, source_file_hash)
        REFERENCES knowledge_direct_source(run_id, source_id, source_file_hash),
    CHECK(source_object_hash = slice_hash)
);

CREATE TABLE knowledge_direct_final_visual_ref (
    run_id TEXT NOT NULL,
    final_skill_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL CHECK(ref_ordinal >= 1),
    batch_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    object_hash TEXT NOT NULL
        CHECK(length(object_hash) = 64 AND object_hash NOT GLOB '*[^0-9a-f]*'),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('PDF', 'DOCX')),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 1),
    evidence_locator_json TEXT NOT NULL CHECK(json_valid(evidence_locator_json)),
    PRIMARY KEY(run_id, final_skill_id, ref_ordinal),
    UNIQUE(run_id, final_skill_id, evidence_id, object_hash),
    FOREIGN KEY(run_id, final_skill_id)
        REFERENCES knowledge_direct_final_skill(run_id, final_skill_id),
    FOREIGN KEY(run_id, batch_id, evidence_id)
        REFERENCES knowledge_direct_chapter_visual_ref(run_id, batch_id, evidence_id),
    FOREIGN KEY(run_id, batch_id, evidence_id, object_hash)
        REFERENCES knowledge_direct_chapter_visual_ref(
            run_id,
            batch_id,
            evidence_id,
            object_hash
        )
);

CREATE TABLE knowledge_direct_shadow_bundle (
    run_id TEXT PRIMARY KEY REFERENCES knowledge_direct_run(run_id),
    manifest_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL UNIQUE,
    all_skill_ids_json TEXT NOT NULL CHECK(json_valid(all_skill_ids_json)),
    shadow_skill_ids_json TEXT NOT NULL CHECK(json_valid(shadow_skill_ids_json)),
    all_skill_count INTEGER NOT NULL CHECK(all_skill_count >= 0),
    shadow_skill_count INTEGER NOT NULL CHECK(shadow_skill_count >= 0),
    non_ready_skill_count INTEGER NOT NULL CHECK(non_ready_skill_count >= 0),
    formal_committee_weight_allowed INTEGER NOT NULL
        CHECK(formal_committee_weight_allowed = 0),
    bundle_object_hash TEXT NOT NULL
        CHECK(length(bundle_object_hash) = 64
            AND bundle_object_hash NOT GLOB '*[^0-9a-f]*'),
    bundle_json TEXT NOT NULL CHECK(json_valid(bundle_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id, manifest_id)
        REFERENCES knowledge_direct_sol_confirmed_dedup_manifest(run_id, manifest_id),
    CHECK(all_skill_count = shadow_skill_count + non_ready_skill_count)
);

CREATE INDEX idx_knowledge_direct_batch_stage
ON knowledge_direct_chapter_batch(run_id, stage, batch_ordinal);

CREATE INDEX idx_knowledge_direct_candidate_status
ON knowledge_direct_raw_sol_candidate(run_id, status, primary_module, candidate_id);

CREATE INDEX idx_knowledge_direct_final_status
ON knowledge_direct_final_skill(run_id, status, final_skill_id);
