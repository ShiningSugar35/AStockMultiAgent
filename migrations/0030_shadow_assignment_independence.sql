CREATE UNIQUE INDEX ux_shadow_assignment_event
ON shadow_assignment_index(study_id, event_id);

CREATE UNIQUE INDEX ux_shadow_assignment_protocol
ON shadow_assignment_index(study_id, trade_protocol_id);
