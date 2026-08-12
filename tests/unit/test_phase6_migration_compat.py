from __future__ import annotations

import shutil
from pathlib import Path

from astock.core.state import StateStore, _migration_checksum

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AT = "2026-07-27T01:00:00+00:00"


def _copy_migrations(target: Path, start: int, end: int) -> None:
    for version in range(start, end + 1):
        source = next((PROJECT_ROOT / "migrations").glob(f"{version:04d}_*.sql"))
        shutil.copy(source, target / source.name)


def _seed_decision(state: StateStore, suffix: str, verdict: str = "PAPER_ELIGIBLE") -> str:
    assessment_id = f"assessment-{suffix}"
    bundle_id = f"bundle-{suffix}"
    decision_id = f"decision-{suffix}"
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO committee_assessment_index VALUES(?,?,?,?,?,?,?,?)",
            (
                assessment_id,
                "300750",
                "NEW_CANDIDATE",
                _AT,
                1,
                f"assessment-object-{suffix}",
                f"assessment-input-{suffix}",
                _AT,
            ),
        )
        connection.execute(
            "INSERT INTO committee_bundle_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bundle_id,
                assessment_id,
                None,
                "300750",
                "NEW_CANDIDATE",
                _AT,
                "rules-v1",
                "engine-v1",
                2,
                f"bundle-object-{suffix}",
                f"bundle-input-{suffix}",
                _AT,
            ),
        )
        connection.execute(
            "INSERT INTO committee_decision_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                bundle_id,
                "300750",
                "NEW_CANDIDATE",
                _AT,
                "rules-v1",
                "engine-v1",
                verdict,
                0,
                0,
                None,
                f"decision-object-{suffix}",
                f"decision-input-{suffix}",
                _AT,
            ),
        )
    return decision_id


def test_known_legacy_0044_runtime_reconciles_through_current_0047(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _copy_migrations(migrations, 1, 43)
    compatibility_dir = migrations / "compat"
    compatibility_dir.mkdir()
    legacy_0044 = PROJECT_ROOT / "migrations" / "compat" / "0044_phase6_decision_close_legacy.sql"
    shutil.copy(legacy_0044, compatibility_dir / legacy_0044.name)

    state = StateStore(tmp_path / "state.sqlite", migrations)
    assert state.migrate()[-1] == "0043"
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO committee_rule_index VALUES(?,?,?,?,?,?,?)",
            ("rules-v1", "rule-set-v1", "engine-v1", _AT, "rule-object", "rule-input", _AT),
        )
    legacy_decision = _seed_decision(state, "legacy")
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO committee_trade_protocol_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-protocol-v1",
                legacy_decision,
                "300750",
                "PAPER_ELIGIBLE",
                "ACTIVE",
                "legacy-strategy",
                _AT,
                1,
                0,
                0,
                "legacy-object",
                "legacy-input",
                _AT,
            ),
        )

    shutil.copy(legacy_0044, migrations / "0044_phase6_decision_close.sql")
    assert state.migrate() == ["0044"]
    with state.connect() as connection:
        old_checksum = str(
            connection.execute(
                "SELECT checksum FROM schema_migration WHERE version='0044'"
            ).fetchone()["checksum"]
        )
    assert old_checksum == _migration_checksum(legacy_0044.read_text(encoding="utf-8"))

    approved_decision = _seed_decision(state, "approved")
    watch_decision = _seed_decision(state, "watch", verdict="PAPER_HOLD")
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO committee_trade_protocol_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "paper-only-protocol-v2",
                approved_decision,
                "300750",
                "PAPER_ELIGIBLE",
                "ACTIVE",
                "paper-only-strategy",
                _AT,
                1,
                0,
                1,
                1,
                "approved-object",
                "approved-input",
                _AT,
            ),
        )
        connection.execute(
            "INSERT INTO committee_trade_protocol_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "watch-protocol-v2",
                watch_decision,
                "300750",
                "PAPER_HOLD",
                "BLOCKED",
                "watch-strategy",
                _AT,
                1,
                0,
                0,
                0,
                "watch-object",
                "watch-input",
                _AT,
            ),
        )

    current_0044 = PROJECT_ROOT / "migrations" / "0044_phase6_decision_close.sql"
    shutil.copy(current_0044, migrations / current_0044.name)
    _copy_migrations(migrations, 45, 47)

    assert state.migrate() == ["0045", "0046", "0047"]
    assert state.migrate() == []
    with state.connect() as connection:
        checksums = {
            str(row["version"]): str(row["checksum"])
            for row in connection.execute(
                "SELECT version,checksum FROM schema_migration WHERE version IN ('0044','0047')"
            ).fetchall()
        }
        legacy_ids = [
            str(row["protocol_id"])
            for row in connection.execute(
                "SELECT protocol_id FROM committee_trade_protocol_index_legacy ORDER BY protocol_id"
            ).fetchall()
        ]
        active_ids = [
            str(row["protocol_id"])
            for row in connection.execute(
                "SELECT protocol_id FROM committee_trade_protocol_index ORDER BY protocol_id"
            ).fetchall()
        ]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='paper_confirmation_key_binding'"
        ).fetchone()
    current_0047 = PROJECT_ROOT / "migrations" / "0047_phase6_migration_identity_recovery.sql"
    assert checksums["0044"] == _migration_checksum(current_0044.read_text(encoding="utf-8"))
    assert checksums["0047"] == _migration_checksum(current_0047.read_text(encoding="utf-8"))
    assert legacy_ids == ["legacy-protocol-v1", "watch-protocol-v2"]
    assert active_ids == ["paper-only-protocol-v2"]
    assert state.integrity_check() == "ok"
