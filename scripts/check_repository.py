"""Check only staged or merge-base changed blobs using the same repository hygiene rules."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_ROOTS = {"runtime", "user_state", ".ai-bridge", ".scratchpad", "scratch", ".venv"}
_PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def _git(*arguments: str) -> bytes:
    # Fixed local Git inspection, no hooks, shell, credentials, network or mutations.
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        timeout=15,
        capture_output=True,
    ).stdout


def inspect_blob(path: str, payload: bytes) -> list[str]:
    parts = PurePosixPath(path).parts
    issues: list[str] = []
    name = parts[-1] if parts else ""
    if (not parts or ".." in parts or PurePosixPath(path).is_absolute()
            or ":" in path or "\\" in path or any(ord(char) < 32 for char in path)):
        issues.append("INVALID_PATH")
    if parts and parts[0] in _PRIVATE_ROOTS:
        issues.append("RUNTIME_OR_PRIVATE_PATH")
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        issues.append("CREDENTIAL_FILE")
    if name.endswith((".bak", ".tmp", ".pyc", ".pyo", "~")) or any(
        part in {"__pycache__", "_backup", "_old"} for part in parts
    ):
        issues.append("TEMPORARY_OR_BACKUP_FILE")
    if len(payload) > 5 * 1024 * 1024 and not path.startswith("tests/fixtures/"):
        issues.append("OVERSIZED_BLOB")
    if _PRIVATE_KEY.search(payload):
        issues.append("PRIVATE_KEY_MATERIAL")
    return issues


def _complexities(path: str, payload: bytes) -> dict[str, int]:
    """Ruff is the only complexity analyzer; AST supplies qualified function identities."""
    source = payload.decode("utf-8-sig")
    tree = ast.parse(source)
    names: dict[int, str] = {}

    def visit(node: ast.AST, parents: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parents = (*parents, node.name)
            if not isinstance(node, ast.ClassDef):
                names[node.lineno] = ".".join(parents)
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree)
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=20",
            "--output-format",
            "json",
            "--stdin-filename",
            path,
            "-",
        ],
        input=payload,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError("Ruff complexity inspection failed")
    result = {}
    for issue in json.loads(completed.stdout):
        match = re.search(r"\((\d+) > \d+\)", issue["message"])
        key = names.get(issue["location"]["row"])
        if issue["code"] != "C901" or match is None or key is None:
            raise ValueError("unsupported Ruff complexity diagnostic")
        result[key] = int(match.group(1))
    return result


def complexity_regressions(path: str, before: bytes, after: bytes) -> list[str]:
    """New functions <=20; existing over-limit functions may improve but never increase."""
    old = _complexities(path, before) if before else {}
    new = _complexities(path, after)
    return [
        f"C901:{name}:{value}>{max(20, old.get(name, 20))}"
        for name, value in sorted(new.items())
        if value > max(20, old.get(name, 20))
    ]


def check_changed(*, staged: bool, base: str | None, complexity: bool = False) -> list[str]:
    if staged == (base is not None):
        raise ValueError("choose exactly one of staged or merge-base mode")
    arguments = ["diff", "--name-only", "--diff-filter=ACMR", "-z"]
    if staged:
        baseline = "HEAD"
        arguments += ["--cached"]
    else:
        if base is None or base.startswith("-"):
            raise ValueError("base must identify a commit")
        resolved = _git("rev-parse", "--verify", base + "^{commit}").decode().strip()
        baseline = _git("merge-base", "HEAD", resolved).decode().strip()
        arguments += [baseline, "HEAD"]
    names = _git(*arguments).decode("utf-8").split("\0")
    problems = []
    for path in filter(None, names):
        payload = _git("show", (":" if staged else "HEAD:") + path)
        problems.extend(f"{path}: {issue}" for issue in inspect_blob(path, payload))
        if complexity and path.startswith("src/") and path.endswith(".py"):
            try:
                before = _git("show", baseline + ":" + path)
            except subprocess.CalledProcessError:
                before = b""
            problems.extend(
                f"{path}: {issue}" for issue in complexity_regressions(path, before, payload)
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--base")
    parser.add_argument(
        "--complexity", action="store_true", help="Ruff C901 against exact baseline"
    )
    args = parser.parse_args()
    problems = check_changed(staged=args.staged, base=args.base, complexity=args.complexity)
    for problem in problems:
        print(problem)
    print(f"repository hygiene: {len(problems)} issue(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
