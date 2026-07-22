ALTER TABLE knowledge_semantic_candidate ADD COLUMN llm_batch_id TEXT
REFERENCES knowledge_llm_batch(batch_id);

ALTER TABLE knowledge_semantic_candidate ADD COLUMN llm_response_object_hash TEXT;

CREATE INDEX idx_knowledge_semantic_candidate_batch
ON knowledge_semantic_candidate(llm_batch_id, candidate_id);
