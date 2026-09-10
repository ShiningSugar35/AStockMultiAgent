"""Run a quality gate with project-local temporary files and honest exit evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fingerprint() -> str:
    digest = hashlib.sha256()
    files = set(ROOT.glob("*.md")) | {ROOT / "pyproject.toml", ROOT / "uv.lock"}
    for directory in ("src", "tests", "configs", "migrations", "planning", "docs", "scripts"):
        for path in (ROOT / directory).rglob("*"):
            if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in path.parts):
                continue
            if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                files.add(path)
    for path in sorted(files):
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("pytest", "ruff", "pyright", "python"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = ROOT / ".ai-bridge" / "quality-runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    temporary = directory / "tmp"
    temporary.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "UV_CACHE_DIR": str(ROOT / ".ai-bridge" / "quality-cache" / "uv"),
            "XDG_CACHE_HOME": str(ROOT / ".ai-bridge" / "quality-cache"),
            "NPM_CONFIG_CACHE": str(ROOT / ".ai-bridge" / "quality-cache" / "npm"),
            "HYPOTHESIS_STORAGE_DIRECTORY": str(
                ROOT / ".ai-bridge" / "quality-cache" / "hypothesis"
            ),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "RUFF_CACHE_DIR": str(ROOT / ".ai-bridge" / "quality-cache" / "ruff"),
            "PYRIGHT_PYTHON_CACHE_DIR": str(ROOT / ".ai-bridge" / "quality-cache" / "pyright"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    command = [sys.executable, "-B"]
    if args.tool != "python":
        command += ["-m", args.tool]
    if args.tool == "pyright":
        if any(arg.startswith(("--pythonpath", "--venvpath")) for arg in arguments):
            raise SystemExit("the quality runner owns the project Python interpreter binding")
        command += ["--pythonpath", sys.executable]
    command += arguments
    if args.tool == "pytest":
        if any(arg.startswith("--basetemp") or arg.startswith("--junit") for arg in arguments):
            raise SystemExit("pytest temporary and evidence paths are owned by this runner")
        command += [
            "--basetemp",
            str(temporary / "pytest"),
            "-p",
            "no:cacheprovider",
            "--junitxml",
            str(directory / "junit.xml"),
        ]
    before = fingerprint()
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    print(
        json.dumps({"run_id": run_id, "command": command, "cwd": str(ROOT)}, ensure_ascii=False),
        flush=True,
    )
    with (directory / "output.log").open("w", encoding="utf-8", newline="\n") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
        child_exit_code = process.wait()
    after = fingerprint()
    result = {
        "run_id": run_id,
        "command": command,
        "cwd": str(ROOT),
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": time.perf_counter() - started,
        "child_exit_code": child_exit_code,
        "source_hash_before": before,
        "source_hash_after": after,
        "source_tree_stable": before == after,
        "output_sha256": hashlib.sha256((directory / "output.log").read_bytes()).hexdigest(),
        "evidence_directory": str(directory.relative_to(ROOT)),
    }
    (directory / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return child_exit_code if child_exit_code else (0 if before == after else 3)


if __name__ == "__main__":
    raise SystemExit(main())
