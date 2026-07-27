CREATE TABLE book_visual_run (
    run_id TEXT PRIMARY KEY,
    source_manifest_id TEXT NOT NULL REFERENCES book_source_manifest(manifest_id),
    source_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    raw_object_hash TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    layout_version TEXT NOT NULL,
    classification_version TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN (
            'INPUT_FROZEN',
            'LAYOUT_ENUMERATED',
            'OCR_COMPLETED',
            'CHARTS_CLASSIFIED',
            'SEMANTIC_MATERIALIZED',
            'AUDITED',
            'FAILED'
        )
    ),
    source_page_count INTEGER NOT NULL CHECK (source_page_count >= 0),
    image_page_count INTEGER NOT NULL CHECK (image_page_count >= 0),
    image_placement_count INTEGER NOT NULL CHECK (image_placement_count >= 0),
    processed_placement_count INTEGER NOT NULL CHECK (processed_placement_count >= 0),
    semantic_run_id TEXT,
    coverage_report_hash TEXT,
    run_object_hash TEXT NOT NULL,
    run_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE book_image_evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES book_visual_run(run_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    placement_index INTEGER NOT NULL CHECK (placement_index >= 1),
    placement_ordinal INTEGER NOT NULL CHECK (placement_ordinal >= 1),
    xref INTEGER CHECK (xref IS NULL OR xref >= 1),
    bbox_json TEXT NOT NULL,
    page_width REAL NOT NULL CHECK (page_width > 0),
    page_height REAL NOT NULL CHECK (page_height > 0),
    image_object_hash TEXT,
    duplicate_of_evidence_id TEXT REFERENCES book_image_evidence(evidence_id),
    evidence_object_hash TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(run_id, page_number, placement_index),
    UNIQUE(run_id, placement_ordinal)
);

CREATE TABLE book_image_evidence_attempt (
    attempt_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES book_image_evidence(evidence_id),
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
    extraction_mode TEXT NOT NULL CHECK (
        extraction_mode IN ('XREF_ORIGINAL', 'BBOX_CLIP_300_DPI')
    ),
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    image_object_hash TEXT,
    error_code TEXT,
    attempt_object_hash TEXT NOT NULL,
    attempt_json TEXT NOT NULL,
    UNIQUE(evidence_id, attempt_ordinal)
);

CREATE TABLE book_image_ocr (
    evidence_id TEXT PRIMARY KEY REFERENCES book_image_evidence(evidence_id),
    run_id TEXT NOT NULL REFERENCES book_visual_run(run_id),
    status TEXT NOT NULL CHECK (
        status IN ('SUCCESS', 'LOW_CONFIDENCE', 'NO_TEXT', 'FAILED')
    ),
    text_object_hash TEXT,
    average_confidence REAL CHECK (
        average_confidence IS NULL
        OR average_confidence BETWEEN 0.0 AND 1.0
    ),
    engine_name TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    result_object_hash TEXT NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE book_layout_atom (
    atom_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES book_visual_run(run_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    page_ordinal INTEGER NOT NULL CHECK (page_ordinal >= 1),
    global_ordinal INTEGER NOT NULL CHECK (global_ordinal >= 1),
    atom_kind TEXT NOT NULL CHECK (
        atom_kind IN ('TEXT_BLOCK', 'IMAGE_EVIDENCE')
    ),
    bbox_json TEXT NOT NULL,
    text_object_hash TEXT,
    evidence_id TEXT REFERENCES book_image_evidence(evidence_id),
    atom_object_hash TEXT NOT NULL,
    atom_json TEXT NOT NULL,
    UNIQUE(run_id, global_ordinal),
    UNIQUE(run_id, page_number, page_ordinal)
);

CREATE TABLE book_chart_unit (
    chart_unit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES book_visual_run(run_id),
    evidence_id TEXT NOT NULL UNIQUE REFERENCES book_image_evidence(evidence_id),
    chart_type TEXT NOT NULL CHECK (
        chart_type IN (
            'CHART',
            'TABLE',
            'DIAGRAM',
            'TEXT_IMAGE',
            'DECORATIVE',
            'UNKNOWN'
        )
    ),
    classification_confidence REAL NOT NULL CHECK (
        classification_confidence BETWEEN 0.0 AND 1.0
    ),
    decorative_excluded INTEGER NOT NULL CHECK (decorative_excluded IN (0, 1)),
    caption_present INTEGER NOT NULL CHECK (caption_present IN (0, 1)),
    review_reason_codes_json TEXT NOT NULL,
    unit_object_hash TEXT NOT NULL,
    unit_json TEXT NOT NULL
);

CREATE TABLE book_visual_semantic_ref (
    ref_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES book_visual_run(run_id),
    chart_unit_id TEXT NOT NULL UNIQUE REFERENCES book_chart_unit(chart_unit_id),
    semantic_run_id TEXT NOT NULL REFERENCES knowledge_semantic_run(run_id),
    paragraph_id TEXT NOT NULL REFERENCES knowledge_paragraph_unit(paragraph_id),
    argument_unit_id TEXT NOT NULL REFERENCES knowledge_argument_unit(argument_unit_id),
    relation_ids_json TEXT NOT NULL,
    ref_object_hash TEXT NOT NULL,
    ref_json TEXT NOT NULL
);

CREATE TABLE book_visual_coverage_report (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES book_visual_run(run_id),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('COMPLETE', 'PARTIAL', 'FAILED')
    ),
    quality_status TEXT NOT NULL CHECK (
        quality_status IN ('PASS', 'REVIEW_REQUIRED', 'FAILED')
    ),
    report_object_hash TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE INDEX idx_book_visual_run_source
ON book_visual_run(source_manifest_id, started_at DESC, run_id DESC);

CREATE INDEX idx_book_image_evidence_run
ON book_image_evidence(run_id, placement_ordinal, evidence_id);

CREATE INDEX idx_book_layout_run
ON book_layout_atom(run_id, global_ordinal, atom_id);

CREATE INDEX idx_book_chart_run
ON book_chart_unit(run_id, chart_type, chart_unit_id);

CREATE INDEX idx_book_visual_semantic_run
ON book_visual_semantic_ref(semantic_run_id, argument_unit_id, paragraph_id);
