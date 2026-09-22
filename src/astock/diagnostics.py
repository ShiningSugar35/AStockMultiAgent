"""Read-only developer CLI: python -m astock.diagnostics --help.

Uses only the standard library. Never initializes a runtime, migrates state,
loads ML models, accepts caller SQL, or publishes an investment conclusion.
Run from the project root or supply --root.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


class DiagnosticError(ValueError):
    """Only static, value-free codes are supplied by this module."""


_METADATA = "rowid,artifact_id,type,schema_version,object_hash,created_at"
_MAX_OBJECT_BYTES = 16 * 1024 * 1024


def connect_read_only(root: Path) -> sqlite3.Connection:
    database = root / "runtime" / "state.sqlite"
    if not database.is_file():
        raise DiagnosticError("CANONICAL_DATABASE_NOT_FOUND")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _payload(root: Path, record: sqlite3.Row) -> dict[str, Any]:
    digest = record["object_hash"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DiagnosticError("INVALID_REGISTERED_OBJECT_HASH")
    directory = (root / "runtime" / "objects" / "sha256").resolve()
    path = (directory / digest[:2] / digest[2:4] / digest).resolve()
    if not path.is_relative_to(directory):
        raise DiagnosticError("OBJECT_PATH_OUTSIDE_CANONICAL_STORE")
    if path.stat().st_size > _MAX_OBJECT_BYTES:
        raise DiagnosticError("OBJECT_EXCEEDS_DIAGNOSTIC_BUDGET")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise DiagnosticError("OBJECT_HASH_MISMATCH")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise DiagnosticError("OBJECT_IS_NOT_A_RECORD")
    return payload


def summarize(record: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(record)
    for key in ("request_id", "as_of", "status", "coverage_status", "company_id"):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = value[:256]
    for key in ("seeds", "tasks", "candidate_universe", "candidate_rankings", "verified_numbers"):
        value = payload.get(key)
        if isinstance(value, list):
            result[key + "_count"] = len(value)
    if record["type"] == "RecommendationResearchReceipt":
        portfolio = payload.get("portfolio")
        positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
        result["position_count"] = len(positions) if isinstance(positions, list) else None
        result["requires_current_research_refresh"] = "pit_snapshot" in payload
        result["current_request_satisfied"] = "NOT_EVALUATED"
    if record["type"] == "ResearchSeedReport":
        for key in ("universe_coverage_level", "universe_coverage_status",
                    "formal_full_market_coverage_allowed", "market_seed_count",
                    "breadth_seed_count", "long_horizon_value_seed_count", "expert_seed_count"):
            if isinstance(payload.get(key), (str, bool, int)):
                result[key] = payload[key]
        counts = payload.get("selected_industry_counts")
        if isinstance(counts, dict):
            result["selected_industry_count"] = len(counts)
    if record["type"] == "SeedPromotionReport":
        for key in ("selected_seed_count", "promoted_company_count", "blocked_company_count",
                    "reused_candidate_count", "live", "seed_report_artifact_id"):
            if isinstance(payload.get(key), (str, bool, int)):
                result[key] = payload[key]
        tasks = payload.get("tasks", [])
        if isinstance(tasks, list):
            result["next_actions"] = [
                {key: item.get(key) for key in ("company_id", "task_code", "retryable")}
                for item in tasks[:8] if isinstance(item, dict)
            ]
        companies = payload.get("company_results", [])
        if isinstance(companies, list):
            result["company_status_counts"] = dict(Counter(
                str(item.get("status", "UNSPECIFIED"))[:64]
                for item in companies if isinstance(item, dict)
            ))
    for key in ("promotions", "results", "outcomes", "items"):
        items = payload.get(key)
        if isinstance(items, list):
            result[key + "_count"] = len(items)
            result[key + "_status_counts"] = dict(Counter(
                str(item.get("status", "UNSPECIFIED"))[:64]
                for item in items if isinstance(item, dict)
            ))
    return result


def artifacts(connection: sqlite3.Connection, root: Path, *, kind: str, limit: int = 10,
              before_rowid: int | None = None, request_id: str | None = None) -> dict[str, Any]:
    if not 1 <= limit <= 64 or (before_rowid is not None and before_rowid < 1):
        raise DiagnosticError("INVALID_PAGE_BOUNDS")
    rows = connection.execute(
        "SELECT " + _METADATA + " FROM artifact_registry "
        "WHERE type=? AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
        (kind, before_rowid, before_rowid, limit + 1),
    ).fetchall()
    page = rows[:limit]
    results = []
    failures = []
    for row in page:
        try:
            payload = _payload(root, row)
            if request_id is None or payload.get("request_id") == request_id:
                results.append(summarize(row, payload))
        except (ValueError, OSError) as exc:
            failures.append({"artifact_id": row["artifact_id"], "error_class": type(exc).__name__})
    return {"authority": "DIAGNOSTIC_ONLY", "artifacts": results, "failures": failures,
            "scanned": len(page), "request_filter": request_id,
            "next_before_rowid": page[-1]["rowid"] if len(rows) > limit else None,
            "search_exhausted": len(rows) <= limit}


def show_artifact(connection: sqlite3.Connection, root: Path, identity: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT " + _METADATA + " FROM artifact_registry WHERE artifact_id=?", (identity,),
    ).fetchone()
    if row is None:
        raise DiagnosticError("REGISTERED_ARTIFACT_NOT_FOUND")
    return {"authority": "DIAGNOSTIC_ONLY", "artifact": summarize(row, _payload(root, row))}


def table_schema(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    exists = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone()
    if exists is None:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 1024"
        )]
        return {"table": name, "exists": False,
                "similar_tables": difflib.get_close_matches(name, names, n=5)}
    columns = connection.execute(
        'SELECT name,type,"notnull",pk FROM pragma_table_info(?)', (name,),
    ).fetchall()
    return {"table": name, "exists": True, "columns": [
        {"name": row[0], "type": row[1], "not_null": bool(row[2]), "primary_key": bool(row[3])}
        for row in columns
    ]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("artifacts", help="Bounded, resumable artifact summaries.")
    listing.add_argument("--type", required=True, dest="kind")
    listing.add_argument("--limit", type=int, default=10)
    listing.add_argument("--before-rowid", type=int)
    listing.add_argument("--request-id")
    showing = commands.add_parser("artifact", help="Inspect one exact immutable artifact.")
    showing.add_argument("identity")
    schema = commands.add_parser("schema", help="Show one table's columns, not its contents.")
    schema.add_argument("table")
    args = parser.parse_args(argv)
    connection: sqlite3.Connection | None = None
    try:
        root = args.root.resolve()
        connection = connect_read_only(root)
        if args.command == "schema":
            result = table_schema(connection, args.table)
        elif args.command == "artifact":
            result = show_artifact(connection, root, args.identity)
        else:
            result = artifacts(connection, root, kind=args.kind, limit=args.limit,
                               before_rowid=args.before_rowid, request_id=args.request_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.DatabaseError) as exc:
        error = str(exc) if isinstance(exc, DiagnosticError) else type(exc).__name__
        print(json.dumps({"status": "UNAVAILABLE", "error": error}))
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
