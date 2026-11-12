-- Expand only the source-cursor enum; historical cursor values remain unchanged.
-- No event, order, account, or research object is rewritten.
CREATE TABLE continuous_monitor_source_cursor_next (
    target_id TEXT NOT NULL REFERENCES continuous_monitor_target(target_id),
    source TEXT NOT NULL CHECK (
        source IN ('MARKET_60M','CNINFO','GDELT','COMMUNITY','CATALYST','SCHEDULE','PAPER')
    ),
    cursor TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    retry_after TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(target_id, source)
);

INSERT INTO continuous_monitor_source_cursor_next (
    target_id,source,cursor,failure_count,retry_after,updated_at
)
SELECT target_id,source,cursor,failure_count,retry_after,updated_at
FROM continuous_monitor_source_cursor;

DROP TABLE continuous_monitor_source_cursor;

ALTER TABLE continuous_monitor_source_cursor_next
RENAME TO continuous_monitor_source_cursor;
