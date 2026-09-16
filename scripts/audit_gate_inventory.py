"""Reproducible gate-site inventory; a scan is not a complete semantic audit.

Includes Python exception/assert/control branches, schema constraints, config,
workflows and skills. False positives remain visible and never relax a runtime
check. Output must stay in .ai-bridge; no runtime objects or user state are read.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GATE = re.compile(
    r"needs?_info|needs_user_input|fail.?closed|blocked|unavailable|reject|"
    r"required|gate|veto|exhaust|not_admitted|strict|threshold|minimum|maximum|pytest|pyright|ruff",
    re.IGNORECASE,
)
SQL_GATE = re.compile(r"\b(check|not\s+null|unique|foreign\s+key|references)\b", re.IGNORECASE)
SCOPES = (
    "src", "configs", ".agents", "docs/workflows", "scripts", "migrations", ".github/workflows",
)


def _semantic_review_status(
    *,
    path: str,
    file_sha256: str,
    review_index: dict[str, Any] | None,
) -> tuple[str, str | None]:
    if review_index is None:
        return "UNREVIEWED_SITE", None
    if review_index.get("version") != "gate-semantic-review-index-v1":
        return "INVALID_REVIEW_INDEX", None
    files = review_index.get("files")
    if not isinstance(files, dict):
        return "INVALID_REVIEW_INDEX", None
    review = files.get(path)
    if not isinstance(review, dict):
        return "UNREVIEWED_SITE", None
    review_id = review.get("review_id")
    review_sha256 = review.get("file_sha256")
    if not isinstance(review_id, str) or not review_id:
        return "INVALID_REVIEW_INDEX", None
    if review_sha256 != file_sha256:
        return "STALE_FILE_REVIEW", review_id
    if review_index.get("findings_adjudicated") is not True:
        return "FILE_REVIEWED_FINDINGS_OPEN", review_id
    return "FILE_SEMANTIC_REVIEWED", review_id


def inventory(
    root: Path,
    *,
    semantic_review_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = {root / "AGENTS.md", root / "pyproject.toml"}
    errors: list[dict[str, str]] = []

    def walk_error(error: OSError) -> None:
        path = Path(error.filename) if error.filename else root
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else "."
        errors.append({"path": relative, "error": type(error).__name__})

    for scope in SCOPES:
        base = root / scope
        if not base.exists():
            continue
        for current, directories, names in os.walk(base, followlinks=False, onerror=walk_error):
            directory = Path(current)
            directories[:] = [
                name for name in directories
                if name != "__pycache__" and not (directory / name).is_symlink()
                and not (directory / name).is_junction()
            ]
            files.update(
                directory / name for name in names
                if Path(name).suffix in {".py", ".yaml", ".yml", ".md", ".sql", ".toml"}
                and not (directory / name).is_symlink()
            )
    sites: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    for path in sorted(files):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not path.resolve().is_relative_to(root.resolve()):
            errors.append({"path": relative, "error": "outside-root target skipped"})
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append({"path": relative, "error": type(exc).__name__})
            continue
        file_hashes[relative] = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            errors.append({"path": relative, "error": "invalid UTF-8"})
            continue
        candidates: dict[int, set[str]] = {}
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                errors.append({"path": relative, "error": "Python parse error"})
                continue
            for node in ast.walk(tree):
                kind = None
                if isinstance(node, (ast.Raise, ast.Assert)):
                    kind = type(node).__name__
                elif isinstance(node, (ast.If, ast.IfExp)):
                    kind = "Conditional"  # Include non-keyword control-flow gates too.
                elif isinstance(node, ast.ExceptHandler):
                    kind = "ExceptionHandler"
                elif isinstance(node, ast.Match):
                    kind = "PatternMatch"
                elif isinstance(node, ast.Call):
                    name = ast.unparse(node.func).rsplit(".", 1)[-1]
                    if name in {"Field", "field_validator", "model_validator"}:
                        kind = "SchemaConstraint"
                if kind:
                    line_no = getattr(node, "lineno", None)
                    if isinstance(line_no, int):
                        candidates.setdefault(line_no, set()).add(kind)
        for line_no, line in enumerate(text.splitlines(), 1):
            if path.suffix == ".sql" and SQL_GATE.search(line):
                candidates.setdefault(line_no, set()).add("SQLConstraint")
            if path.suffix in {".yml", ".yaml"} and re.match(r"\s*(?:-\s+)?if\s*:", line):
                candidates.setdefault(line_no, set()).add("ConditionalPolicy")
            if GATE.search(line):
                candidates.setdefault(line_no, set()).add("PolicyOrStatus")
        review_status, review_id = _semantic_review_status(
            path=relative,
            file_sha256=file_hashes[relative],
            review_index=semantic_review_index,
        )
        for line, kinds in sorted(candidates.items()):
            sites.append({
                "path": relative, "line": line, "kinds": sorted(kinds),
                "review_status": review_status,
                "semantic_review_id": review_id,
                "file_sha256": file_hashes[relative],
            })
    groups = Counter(
        site["path"].split("/")[2]
        if site["path"].startswith("src/astock/") else site["path"].split("/")[0]
        for site in sites
    )
    candidate_files = {site["path"] for site in sites}
    covered_files = {
        site["path"] for site in sites
        if site["review_status"] == "FILE_SEMANTIC_REVIEWED"
    }
    covered_sites = sum(
        site["review_status"] == "FILE_SEMANTIC_REVIEWED" for site in sites
    )
    semantic_complete = bool(
        semantic_review_index is not None
        and not errors
        and candidate_files == covered_files
        and covered_sites == len(sites)
        and semantic_review_index.get("findings_adjudicated") is True
    )
    return {
        "version": "gate-inventory-v1",
        "scanned_file_count": len(file_hashes),
        "candidate_file_count": len(candidate_files),
        "candidate_site_count": len(sites),
        "by_domain": dict(sorted(groups.items())),
        "semantic_audit_complete": semantic_complete,
        "semantic_review_coverage": {
            "review_index_version": (
                semantic_review_index.get("version") if semantic_review_index else None
            ),
            "candidate_file_count": len(candidate_files),
            "reviewed_file_count": len(covered_files),
            "candidate_site_count": len(sites),
            "reviewed_site_count": covered_sites,
            "unreviewed_file_count": len(candidate_files - covered_files),
            "findings_adjudicated": bool(
                semantic_review_index
                and semantic_review_index.get("findings_adjudicated") is True
            ),
        },
        "limitations": [
            "Candidates include false positives; no check is automatically waived.",
            (
                "Exact-hash whole-file semantic review covers candidate sites but is not "
                "dynamic path proof."
            ),
            (
                "External services and compound invariants still require domain tests and "
                "controlled live evidence."
            ),
            (
                "Domain dispositions and verified fixes remain in current architecture "
                "and review evidence."
            ),
        ],
        "scan_errors": errors,
        "file_hashes": file_hashes,
        "sites": sites,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".ai-bridge/wp27/gate-inventory.json")
    parser.add_argument(
        "--semantic-review-index",
        help="Optional project-local exact-hash semantic review index JSON.",
    )
    args = parser.parse_args()
    destination = (ROOT / args.output).resolve()
    if not destination.is_relative_to(ROOT / ".ai-bridge"):
        raise ValueError("gate inventories must remain inside project .ai-bridge")
    review_index = None
    if args.semantic_review_index:
        review_path = (ROOT / args.semantic_review_index).resolve()
        if not review_path.is_relative_to(ROOT):
            raise ValueError("semantic review index must remain inside the project")
        review_index = json.loads(review_path.read_text(encoding="utf-8"))
        if review_index.get("version") != "gate-semantic-review-index-v1":
            raise ValueError("unsupported semantic review index version")
    result = inventory(ROOT, semantic_review_index=review_index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({key: result[key] for key in (
        "scanned_file_count", "candidate_file_count", "candidate_site_count", "by_domain",
        "semantic_audit_complete", "semantic_review_coverage", "scan_errors",
    )}, ensure_ascii=False))
    return 1 if result["scan_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
