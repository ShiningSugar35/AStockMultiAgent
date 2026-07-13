CREATE TABLE evidence_record (
    evidence_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot_index(snapshot_id),
    page_id TEXT NOT NULL REFERENCES document_page(page_id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    excerpt_object_hash TEXT NOT NULL,
    excerpt_sha256 TEXT NOT NULL,
    available_to_system_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(page_id, excerpt_sha256, evidence_id)
);

CREATE TABLE claim_record (
    claim_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    as_of TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL,
    claim_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

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

CREATE TABLE evidence_conflict (
    conflict_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claim_record(claim_id),
    evidence_ids_json TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    resolution_status TEXT NOT NULL,
    resolution_note TEXT,
    conflict_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_evidence_document_page
ON evidence_record(document_id, page_number);

CREATE INDEX idx_claim_subject_asof
ON claim_record(subject_id, as_of);

CREATE INDEX idx_claim_evidence_evidence
ON claim_evidence_link(evidence_id, claim_id);
