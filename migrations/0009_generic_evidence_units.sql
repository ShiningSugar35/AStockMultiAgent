CREATE TABLE evidence_record_v2 (
    evidence_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    source_unit_type TEXT NOT NULL CHECK (source_unit_type IN ('PAGE', 'BLOCK')),
    source_unit_index INTEGER NOT NULL CHECK (source_unit_index >= 1),
    page_id TEXT REFERENCES document_page(page_id),
    block_id TEXT REFERENCES document_block(block_id),
    excerpt_object_hash TEXT NOT NULL,
    excerpt_sha256 TEXT NOT NULL,
    available_to_system_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (source_unit_type = 'PAGE' AND page_id IS NOT NULL AND block_id IS NULL)
        OR
        (source_unit_type = 'BLOCK' AND block_id IS NOT NULL AND page_id IS NULL)
    ),
    UNIQUE(source_unit_type, page_id, block_id, excerpt_sha256, evidence_id)
);

INSERT INTO evidence_record_v2(
    evidence_id,document_id,snapshot_id,source_unit_type,source_unit_index,page_id,block_id,
    excerpt_object_hash,excerpt_sha256,available_to_system_at,evidence_json,created_at
)
SELECT
    evidence_id,document_id,snapshot_id,'PAGE',page_number,page_id,NULL,
    excerpt_object_hash,excerpt_sha256,available_to_system_at,evidence_json,created_at
FROM evidence_record;

CREATE TABLE claim_evidence_link_backup AS
SELECT claim_id,evidence_id,relation,weight,reviewer_status,link_json,created_at
FROM claim_evidence_link;

DROP TABLE claim_evidence_link;
DROP TABLE evidence_record;
ALTER TABLE evidence_record_v2 RENAME TO evidence_record;

CREATE TABLE claim_evidence_link (
    claim_id TEXT NOT NULL REFERENCES claim_record(claim_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_record(evidence_id),
    relation TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    reviewer_status TEXT NOT NULL,
    link_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(claim_id, evidence_id, relation)
);

INSERT INTO claim_evidence_link(
    claim_id,evidence_id,relation,weight,reviewer_status,link_json,created_at
)
SELECT claim_id,evidence_id,relation,weight,reviewer_status,link_json,created_at
FROM claim_evidence_link_backup;

DROP TABLE claim_evidence_link_backup;

CREATE INDEX idx_evidence_document_unit
ON evidence_record(document_id, source_unit_type, source_unit_index);

CREATE INDEX idx_claim_evidence_evidence
ON claim_evidence_link(evidence_id, claim_id);
