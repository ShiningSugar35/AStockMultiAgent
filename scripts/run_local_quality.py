"""Run bounded quality tools in the existing owned worker, with honest receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from astock.research.process_worker import (
    KillableResearchWorker,
    WorkerCancelledError,
    WorkerDeadlineError,
)
from astock.storage_retention import path_has_redirect, process_identity

ROOT = Path(__file__).resolve().parents[1]


def fingerprint() -> str:
    digest = hashlib.sha256()
    files = set(ROOT.glob("*.md")) | {
        ROOT / "pyproject.toml", ROOT / "uv.lock", ROOT / ".gitignore",
        ROOT / ".gitattributes",
    }
    for directory in (
        "src", "tests", "configs", "migrations", "planning", "docs", "scripts", ".agents",
        "integrations", "skills",
        # Active audit manifests bind runtime source files and affect validation.
        # Hash that small metadata tree, not vendor code or runtime/object data.
        "third_party/audits",
    ):
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


def configure_console() -> None:
    """Keep Windows legacy console encoding from aborting a valid test receipt."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def _execute_command(
    command: list[str],
    environment: dict[str, str],
    root: str,
    output_path: str,
    result_path: str,
    max_output_bytes: int,
) -> str:
    """This target starts only after KillableResearchWorker establishes ownership."""
    outcome: dict[str, Any] = {
        "child_exit_code": None,
        "output_bytes_observed": 0,
        "output_bytes_retained": 0,
        "output_truncated": False,
        "failure_reason": None,
    }
    try:
        with Path(output_path).open("wb") as log:
            process = subprocess.Popen(
                command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            )
            outcome["command_pid"] = process.pid
            assert process.stdout is not None
            with process.stdout:
                while block := process.stdout.read(64 * 1024):
                    outcome["output_bytes_observed"] += len(block)
                    room = max(0, max_output_bytes - outcome["output_bytes_retained"])
                    retained = block[:room]
                    log.write(retained)
                    log.flush()
                    outcome["output_bytes_retained"] += len(retained)
                    if len(block) > room:
                        outcome["output_truncated"] = True
                        outcome["failure_reason"] = "OUTPUT_LIMIT"
                        break
                else:
                    outcome["child_exit_code"] = process.wait()
            # A budget breach returns immediately. The existing worker's finally
            # closes its job / kills its process group, including this subprocess.
    except Exception as exc:
        outcome["failure_reason"] = "COMMAND_ERROR"
        outcome["error_type"] = type(exc).__name__
    finally:
        _write_json(Path(result_path), outcome)
    return "QualityCommandResult:" + Path(result_path).parent.name


def _environment(temporary: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "MPLCONFIGDIR": str(temporary / "matplotlib"),
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
            # Bound numerical pools inside quality commands and their descendants.
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    for key in (
        "TEMP", "TMP", "TMPDIR", "MPLCONFIGDIR", "UV_CACHE_DIR", "XDG_CACHE_HOME",
        "NPM_CONFIG_CACHE", "HYPOTHESIS_STORAGE_DIRECTORY", "RUFF_CACHE_DIR",
        "PYRIGHT_PYTHON_CACHE_DIR",
    ):
        path = Path(environment[key])
        if path_has_redirect(ROOT, path) or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("quality cache paths must remain project-local without redirects")
    return environment


def _type_interpreter(tool: str, type_environment: str) -> str:
    if type_environment == "main":
        return sys.executable
    if type_environment != "mootdx":
        raise ValueError("unknown quality type environment")
    if tool != "pyright":
        raise ValueError("the mootdx type environment is only valid for pyright")
    # This is the sole SDK location used by integrations/mootdx/setup.py and its
    # provider. Do not import the provider or accept a user-supplied Python path.
    environment = ROOT / "runtime" / "tool-environments" / "mootdx"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (
        path_has_redirect(ROOT, interpreter)
        or not interpreter.resolve().is_relative_to(ROOT.resolve())
    ):
        raise ValueError("the mootdx interpreter must remain project-local without redirects")
    if not interpreter.is_file():
        raise ValueError("the mootdx interpreter does not exist; rebuild its isolated environment")
    return str(interpreter.resolve())


def run_quality(
    tool: str,
    arguments: list[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    type_environment: str = "main",
) -> int:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ValueError("quality time and output budgets must be positive and finite")
    if tool == "pyright" and any(
        arg.startswith(("--pythonpath", "--venvpath")) for arg in arguments
    ):
        raise ValueError("the quality runner owns the project Python interpreter binding")
    if tool == "pytest" and any(
        arg.startswith(("--basetemp", "--junit")) for arg in arguments
    ):
        raise ValueError("pytest temporary and evidence paths are owned by this runner")
    type_interpreter = _type_interpreter(tool, type_environment)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = ROOT / ".ai-bridge" / "quality-runs" / run_id
    temporary = ROOT / ".scratchpad" / f"quality-{run_id}"
    for path in (directory, temporary):
        if path_has_redirect(ROOT, path) or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("quality outputs must remain inside the project without redirects")
    directory.mkdir(parents=True, exist_ok=False)
    temporary.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, "-B"]
    if tool != "python":
        command += ["-m", tool]
    if tool == "pyright":
        command += ["--pythonpath", type_interpreter]
    command += arguments
    if tool == "pytest":
        command += [
            "--basetemp", str(temporary / "pytest"), "-p", "no:cacheprovider",
            "--junitxml", str(directory / "junit.xml"),
        ]
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    owner: dict[str, Any] = {
        "schema_version": "astock-owned-run-v1",
        "run_id": run_id,
        "owner_pid": os.getpid(),
        "owner_create_time": None,
        "started_at": started_at.isoformat(),
        "status": "RUNNING",
        "scratch_dir": str(temporary),
        "project_root": str(ROOT.resolve()),
    }
    result: dict[str, Any] = {
        "run_id": run_id,
        "command": command,
        "type_environment": type_environment,
        "type_python": type_interpreter if tool == "pyright" else None,
        "cwd": str(ROOT),
        "started_at": started_at.isoformat(),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "child_exit_code": None,
        "status": "FAILED",
        "returncode": 1,
        "failure_reason": None,
        "source_hash_before": None,
        "source_hash_after": None,
        "source_tree_stable": False,
        "evidence_directory": str(directory.relative_to(ROOT)),
    }
    worker: KillableResearchWorker | None = None
    output_path = directory / "output.log"
    command_result = directory / "command_result.json"
    try:
        owner["owner_create_time"] = process_identity(os.getpid()).creation_time
        _write_json(temporary / "owner.json", owner)
        _write_json(directory / "owner.json", owner)
        result["source_hash_before"] = fingerprint()
        print(
            json.dumps(
                {"run_id": run_id, "command": command, "cwd": str(ROOT),
                 "output_log": str(output_path.relative_to(ROOT))},
                ensure_ascii=False,
            ),
            flush=True,
        )
        worker = KillableResearchWorker(
            _execute_command,
            (command, _environment(temporary), str(ROOT), str(output_path),
             str(command_result), max_output_bytes),
            deadline_at=started_at + timedelta(seconds=timeout_seconds),
            project_root=ROOT,
            scratch_root=temporary / "worker",
        )
        reference = worker()
        if reference != "QualityCommandResult:" + run_id:
            raise RuntimeError("quality worker result identity mismatch")
        outcome = json.loads(command_result.read_text(encoding="utf-8"))
        result.update(outcome)
        child_code = outcome["child_exit_code"]
        if child_code == 0 and not outcome["failure_reason"]:
            result.update(status="COMPLETED", returncode=0)
        else:
            result["returncode"] = child_code if isinstance(child_code, int) and child_code else 1
            result["failure_reason"] = outcome["failure_reason"] or "CHILD_EXIT_NONZERO"
    except WorkerDeadlineError:
        result.update(status="TIMED_OUT", returncode=124, failure_reason="TIME_BUDGET")
    except (KeyboardInterrupt, WorkerCancelledError):
        result.update(status="CANCELLED", returncode=130, failure_reason="CANCELLED")
    except Exception as exc:
        result.update(failure_reason="RUNNER_ERROR", error_type=type(exc).__name__)
    finally:
        cleanup_confirmed = worker is None or worker.cleanup_confirmed
        result["owned_worker_reaped"] = cleanup_confirmed
        result["worker_pid"] = None if worker is None else worker.last_pid
        try:
            result["source_hash_after"] = fingerprint()
        except Exception as exc:
            result.update(failure_reason="FINGERPRINT_ERROR", error_type=type(exc).__name__)
        result["source_tree_stable"] = bool(
            result["source_hash_before"]
            and result["source_hash_before"] == result["source_hash_after"]
        )
        if result["status"] == "COMPLETED" and not result["source_tree_stable"]:
            result.update(status="FAILED", returncode=3, failure_reason="SOURCE_TREE_CHANGED")
        if not cleanup_confirmed:
            result.update(status="FAILED", returncode=1, failure_reason="TERMINATION_UNCONFIRMED")
        try:
            raw_output = output_path.read_bytes() if output_path.exists() else b""
        except OSError as exc:
            raw_output = b""
            result.update(
                status="FAILED", returncode=1, failure_reason="OUTPUT_READ_ERROR",
                error_type=type(exc).__name__,
            )
        result["output_sha256"] = hashlib.sha256(raw_output).hexdigest()
        result["output_bytes_retained"] = len(raw_output)
        result.setdefault("output_bytes_observed", None)
        result.setdefault("output_truncated", None)
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["duration_seconds"] = time.perf_counter() - started
        owner.update(
            status=result["status"], finished_at=result["finished_at"],
            returncode=result["returncode"], owned_worker_reaped=cleanup_confirmed,
        )
        for owner_path in (directory / "owner.json", temporary / "owner.json"):
            if not owner_path.parent.exists():
                continue
            try:
                _write_json(owner_path, owner)
            except OSError as exc:
                result.update(
                    status="FAILED", returncode=1, failure_reason="OWNER_RECEIPT_ERROR",
                    error_type=type(exc).__name__,
                )
        result["temporary_cleanup"] = "RETAINED_TERMINATION_UNCONFIRMED"
        if cleanup_confirmed and not temporary.exists():
            result["temporary_cleanup"] = "ALREADY_REMOVED"
        elif cleanup_confirmed and not path_has_redirect(ROOT, temporary):
            try:
                shutil.rmtree(temporary)
                result["temporary_cleanup"] = "REMOVED"
            except OSError as exc:
                result["temporary_cleanup"] = f"DEFERRED:{type(exc).__name__}"
        _write_json(directory / "result.json", result)
    # Complete logs are bounded and retained. The transport gets only a bounded
    # tail, including normal pytest/Ruff summaries; unbroken lines cannot grow RAM.
    print(raw_output[-64 * 1024:].decode("utf-8", errors="backslashreplace"), end="", flush=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(result["returncode"])


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--max-output-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--type-environment", choices=("main", "mootdx"), default="main")
    parser.add_argument("tool", choices=("pytest", "ruff", "pyright", "python"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]

    def cancel(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    previous = {}
    for name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, cancel)
    try:
        return run_quality(
            args.tool, arguments, timeout_seconds=args.timeout_seconds,
            max_output_bytes=args.max_output_bytes, type_environment=args.type_environment,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
