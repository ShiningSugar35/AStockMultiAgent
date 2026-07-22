CREATE TABLE collection_gap_state_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX idx_collection_gap_state_scope_time
ON collection_gap_state_event(scope_id, occurred_at, event_id);

CREATE TABLE collection_gap_temporal_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    reliable_from TEXT NOT NULL
);

INSERT INTO collection_gap_temporal_meta(singleton, reliable_from)
VALUES (1, '9999-12-31T23:59:59.999999+00:00');

INSERT INTO collection_gap_state_event(
    gap_id,
    scope_id,
    cursor_json,
    status,
    occurred_at
)
SELECT
    gap_id,
    scope_id,
    cursor_json,
    status,
    strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM collection_gap
ORDER BY gap_id;

UPDATE collection_gap_temporal_meta
SET reliable_from = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE singleton = 1;

CREATE TRIGGER collection_gap_state_after_insert
AFTER INSERT ON collection_gap
BEGIN
    INSERT INTO collection_gap_state_event(
        gap_id,
        scope_id,
        cursor_json,
        status,
        occurred_at
    ) VALUES (
        NEW.gap_id,
        NEW.scope_id,
        NEW.cursor_json,
        NEW.status,
        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    );
END;

CREATE TRIGGER collection_gap_state_after_status_update
AFTER UPDATE OF status ON collection_gap
WHEN OLD.status IS NOT NEW.status
BEGIN
    INSERT INTO collection_gap_state_event(
        gap_id,
        scope_id,
        cursor_json,
        status,
        occurred_at
    ) VALUES (
        NEW.gap_id,
        NEW.scope_id,
        NEW.cursor_json,
        NEW.status,
        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    );
END;
