CREATE UNIQUE INDEX idx_shadow_assignment_formal_memo
ON shadow_assignment_index(study_id, research_memo_id)
WHERE prospective_eligible = 1 AND research_memo_id IS NOT NULL;

CREATE UNIQUE INDEX idx_shadow_assignment_formal_decision
ON shadow_assignment_index(study_id, decision_id)
WHERE prospective_eligible = 1 AND decision_id IS NOT NULL;
