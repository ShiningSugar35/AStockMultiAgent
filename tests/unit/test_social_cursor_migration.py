"""Versioned community cursor migration keeps historical data and FK integrity."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_community_cursor_migration_preserves_existing_source_state(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "cursor.sqlite")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE continuous_monitor_target (
            target_id TEXT PRIMARY KEY
        );
        CREATE TABLE continuous_monitor_source_cursor (
            target_id TEXT NOT NULL REFERENCES continuous_monitor_target(target_id),
            source TEXT NOT NULL CHECK (
                source IN ('MARKET_60M','CNINFO','GDELT','CATALYST','SCHEDULE','PAPER')
            ),
            cursor TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
            retry_after TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(target_id, source)
        );
        INSERT INTO continuous_monitor_target VALUES ('monitor-target:XSHE:002475');
        INSERT INTO continuous_monitor_source_cursor
        VALUES ('monitor-target:XSHE:002475','GDELT','2026-11-10T00:00:00Z',
                2,'2026-11-10T01:00:00Z','2026-11-10T00:00:00Z');
        """
    )
    script = (ROOT / "migrations/0079_continuous_monitor_community_cursor.sql").read_text(
        encoding="utf-8"
    )
    connection.executescript(script)
    historical = connection.execute(
        "SELECT source,cursor,failure_count,retry_after,updated_at "
        "FROM continuous_monitor_source_cursor WHERE source='GDELT'"
    ).fetchone()
    assert historical == (
        "GDELT", "2026-11-10T00:00:00Z", 2,
        "2026-11-10T01:00:00Z", "2026-11-10T00:00:00Z",
    )
    connection.execute(
        "INSERT INTO continuous_monitor_source_cursor "
        "(target_id,source,cursor,updated_at) VALUES (?,?,?,?)",
        ("monitor-target:XSHE:002475", "COMMUNITY", None, "2026-11-11T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO continuous_monitor_source_cursor "
            "(target_id,source,cursor,updated_at) VALUES (?,?,?,?)",
            ("monitor-target:XSHE:002475", "UNKNOWN", None, "2026-11-11T00:00:00Z"),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
