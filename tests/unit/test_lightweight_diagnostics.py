"""The diagnostic CLI must not initialize or mutate canonical state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from astock.diagnostics import (
    artifacts,
    connect_read_only,
    main,
    show_artifact,
    table_schema,
)


@pytest.fixture
def diagnostic_root(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with sqlite3.connect(runtime / "state.sqlite") as connection:
        connection.execute(
            "CREATE TABLE artifact_registry (artifact_id TEXT PRIMARY KEY, type TEXT, "
            "schema_version TEXT, object_hash TEXT, created_at TEXT)"
        )
        for number in range(5):
            payload = {"request_id": "current" if number == 1 else "other",
                       "as_of": "2026-09-17T00:00:00Z", "candidate_universe": ["000001"],
                       "portfolio": {"positions": []}, "pit_snapshot": {}}
            raw = json.dumps(payload).encode()
            digest = hashlib.sha256(raw).hexdigest()
            path = runtime / "objects" / "sha256" / digest[:2] / digest[2:4] / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            connection.execute("INSERT INTO artifact_registry VALUES (?,?,?,?,?)", (
                f"receipt:{number}", "RecommendationResearchReceipt", "test-v1", digest,
                "2026-09-17T00:00:00Z",
            ))
    return tmp_path


def test_missing_database_is_not_created(tmp_path: Path, capsys) -> None:
    assert main(["--root", str(tmp_path), "schema", "artifact_registry"]) == 2
    assert "CANONICAL_DATABASE_NOT_FOUND" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_read_only_schema_and_sql_input_remain_read_only(diagnostic_root: Path) -> None:
    database = diagnostic_root / "runtime" / "state.sqlite"
    before = database.read_bytes()
    connection = connect_read_only(diagnostic_root)
    try:
        schema = table_schema(connection, "artifact_registry")
        assert schema["exists"]
        assert "artifact_id" in {column["name"] for column in schema["columns"]}
        assert not table_schema(connection, "artifact_registry' OR 1=1 --")["exists"]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_write (n INTEGER)")
    finally:
        connection.close()
    assert database.read_bytes() == before


def test_request_filter_never_substitutes_another_request_and_has_a_cursor(diagnostic_root: Path):
    connection = connect_read_only(diagnostic_root)
    try:
        first = artifacts(connection, diagnostic_root, kind="RecommendationResearchReceipt",
                          limit=2, request_id="current")
        assert first["artifacts"] == []
        assert not first["search_exhausted"]
        second = artifacts(connection, diagnostic_root, kind="RecommendationResearchReceipt",
                           limit=2, request_id="current", before_rowid=first["next_before_rowid"])
        assert [row["artifact_id"] for row in second["artifacts"]] == ["receipt:1"]
        assert second["artifacts"][0]["requires_current_research_refresh"]
        assert second["artifacts"][0]["current_request_satisfied"] == "NOT_EVALUATED"
        assert second["artifacts"][0]["position_count"] == 0
        assert first["authority"] == "DIAGNOSTIC_ONLY"
    finally:
        connection.close()


@pytest.mark.parametrize("limit", [0, -1, 65, 1000000])
def test_unbounded_pages_rejected(diagnostic_root: Path, limit: int):
    connection = connect_read_only(diagnostic_root)
    try:
        with pytest.raises(ValueError, match="INVALID_PAGE_BOUNDS"):
            artifacts(
                connection, diagnostic_root, kind="RecommendationResearchReceipt", limit=limit
            )
    finally:
        connection.close()


def test_object_tamper_is_never_summarized_as_verified(diagnostic_root: Path):
    connection = connect_read_only(diagnostic_root)
    try:
        result = show_artifact(connection, diagnostic_root, "receipt:0")
        digest = result["artifact"]["object_hash"]
        path = diagnostic_root / "runtime/objects/sha256" / digest[:2] / digest[2:4] / digest
        path.write_bytes(b"not the original immutable object")
        with pytest.raises(ValueError, match="OBJECT_HASH_MISMATCH"):
            show_artifact(connection, diagnostic_root, "receipt:0")
    finally:
        connection.close()


def test_diagnostics_and_anomaly_import_do_not_load_ml_models():
    code = (
        "import sys; import astock.diagnostics; "
        "assert 'astock.cli' not in sys.modules; "
        "import astock.financial_integrity.anomaly; "
        "assert 'pyod' not in sys.modules; assert 'sklearn' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
