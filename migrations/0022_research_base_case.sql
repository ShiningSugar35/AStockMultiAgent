CREATE TABLE frozen_evidence_pack_index (
    pack_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    formal_historical INTEGER NOT NULL CHECK (formal_historical IN (0, 1)),
    allow_approximated INTEGER NOT NULL CHECK (allow_approximated IN (0, 1)),
    coverage_status TEXT NOT NULL,
    claim_count INTEGER NOT NULL CHECK (claim_count >= 1),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 1),
    open_conflict_count INTEGER NOT NULL CHECK (open_conflict_count >= 0),
    object_hash TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_frozen_evidence_pack_company
ON frozen_evidence_pack_index(company_id, as_of, created_at, pack_id);

CREATE TABLE base_case_pack_index (
    base_case_id TEXT PRIMARY KEY,
    evidence_pack_id TEXT NOT NULL REFERENCES frozen_evidence_pack_index(pack_id),
    company_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    kernel_version TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    finding_count INTEGER NOT NULL CHECK (finding_count >= 0),
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    gap_count INTEGER NOT NULL CHECK (gap_count >= 0),
    object_hash TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(evidence_pack_id, kernel_version, draft_hash)
);

CREATE INDEX idx_base_case_pack_company
ON base_case_pack_index(company_id, as_of, kernel_version, created_at, base_case_id);
