from __future__ import annotations

import shutil
from pathlib import Path

from astock.core.state import StateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AT = "2026-07-27T01:00:00+00:00"


def test_phase6_migration_archives_legacy_protocol_without_reauthorizing_it(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for version in range(1, 44):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, migrations / source.name)
    state = StateStore(tmp_path / "state.sqlite", migrations)
    assert state.migrate()[-1] == "0043"

    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO committee_rule_index VALUES(?,?,?,?,?,?,?)",
            ("rules-v1", "rule-set-v1", "engine-v1", _AT, "1" * 64, "2" * 64, _AT),
        )
        connection.execute(
            "INSERT INTO committee_assessment_index VALUES(?,?,?,?,?,?,?,?)",
            (
                "assessment-v1",
                "300750",
                "NEW_CANDIDATE",
                _AT,
                1,
                "3" * 64,
                "4" * 64,
                _AT,
            ),
        )
        connection.execute(
            "INSERT INTO committee_bundle_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "bundle-v1",
                "assessment-v1",
                None,
                "300750",
                "NEW_CANDIDATE",
                _AT,
                "rules-v1",
                "engine-v1",
                2,
                "5" * 64,
                "6" * 64,
                _AT,
            ),
        )
        connection.execute(
            "INSERT INTO committee_decision_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "decision-v1",
                "bundle-v1",
                "300750",
                "NEW_CANDIDATE",
                _AT,
                "rules-v1",
                "engine-v1",
                "PAPER_ELIGIBLE",
                0,
                0,
                None,
                "7" * 64,
                "8" * 64,
                _AT,
            ),
        )
        connection.execute(
            "INSERT INTO committee_trade_protocol_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-protocol-v1",
                "decision-v1",
                "300750",
                "PAPER_ELIGIBLE",
                "ACTIVE",
                "legacy-strategy",
                _AT,
                1,
                0,
                0,
                "9" * 64,
                "a" * 64,
                _AT,
            ),
        )

    migration = PROJECT_ROOT / "migrations" / "0044_phase6_decision_close.sql"
    shutil.copy(migration, migrations / migration.name)
    assert state.migrate() == ["0044"]
    with state.transaction() as connection:
        legacy = connection.execute(
            "SELECT protocol_id FROM committee_trade_protocol_index_legacy"
        ).fetchall()
        active = connection.execute(
            "SELECT protocol_id FROM committee_trade_protocol_index"
        ).fetchall()
        assert [str(row["protocol_id"]) for row in legacy] == ["legacy-protocol-v1"]
        assert active == []
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='paper_confirmation_key_binding'"
        ).fetchone()
        connection.execute(
            "INSERT INTO committee_trade_protocol_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "paper-only-protocol-v2",
                "decision-v1",
                "300750",
                "PAPER_ELIGIBLE",
                "ACTIVE",
                "paper-only-strategy",
                _AT,
                1,
                0,
                1,
                1,
                "b" * 64,
                "c" * 64,
                _AT,
            ),
        )
    assert state.integrity_check() == "ok"
