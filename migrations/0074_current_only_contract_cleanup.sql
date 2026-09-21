ALTER TABLE candidate_scan_run
DROP COLUMN formal_historical;

ALTER TABLE frozen_evidence_pack_index
DROP COLUMN formal_historical;

ALTER TABLE frozen_evidence_pack_index
DROP COLUMN allow_approximated;

ALTER TABLE financial_source_release
DROP COLUMN official_pit_id;
