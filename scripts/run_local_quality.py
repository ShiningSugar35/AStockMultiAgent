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
from pathlib import Path, PureWindowsPath
from typing import Any

from astock.research.process_worker import (
    KillableResearchWorker,
    WorkerCancelledError,
    WorkerDeadlineError,
)
from astock.storage_retention import path_has_redirect, process_identity

if __package__:
    from scripts.validation_inputs import validation_input_closure
else:
    from validation_inputs import validation_input_closure  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(input_files: list[str] | None = None) -> str:
    digest = hashlib.sha256()
    if input_files is not None:
        for relative in sorted(set(input_files)):
            path = ROOT / relative
            if not path.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("validation input escaped the project")
            digest.update(relative.encode("utf-8"))
            digest.update(
                hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"MISSING"
            )
        return digest.hexdigest()
    files = set(ROOT.glob("*.md")) | {
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "conftest.py",
        ROOT / "pytest.ini",
        ROOT / "setup.cfg",
        ROOT / "tox.ini",
        ROOT / ".gitignore",
        ROOT / ".gitattributes",
        ROOT / ".pre-commit-config.yaml",
    }
    for directory in (
        "src",
        "tests",
        "configs",
        "migrations",
        "planning",
        "docs",
        "scripts",
        ".agents",
        ".github",
        "integrations",
        "skills",
        # Active audit manifests bind runtime source files and affect validation.
        # Hash that small metadata tree, not vendor code or runtime/object data.
        "third_party/audits",
        "third_party/qualifications",
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


def planned_pytest_targets(arguments: list[str], planned_tests: list[str]) -> list[str]:
    """Reject collection inputs omitted from an impact plan before launching pytest."""
    flags = {
        "-q", "-qq", "-v", "-vv", "-x", "-s", "--exitfirst", "--disable-warnings",
        "--strict-markers", "--strict-config", "--collect-only",
    }
    value_options = {"-k", "-m", "--maxfail", "--tb", "--durations"}
    targets: list[str] = []
    iterator = iter(arguments)
    for argument in iterator:
        if argument in flags:
            continue
        if argument in value_options:
            if next(iterator, None) is None:
                raise ValueError("pytest option is missing its value")
            continue
        if argument.partition("=")[0] in value_options and "=" in argument:
            continue
        if argument.startswith("-"):
            raise ValueError("validation plan requires explicitly supported pytest options")
        normalized = argument.replace("\\", "/")
        relative = normalized.split("::", 1)[0]
        if (
            Path(relative).is_absolute()
            or PureWindowsPath(relative).drive
            or ".." in Path(relative).parts
            or not relative.startswith("tests/")
            or not relative.endswith(".py")
            or not (ROOT / relative).is_file()
        ):
            raise ValueError("validation plan requires explicit project-relative pytest files")
        targets.append(normalized)
    planned = {item.replace("\\", "/") for item in planned_tests}
    if not targets or any(
        target not in planned and target.split("::", 1)[0] not in planned for target in targets
    ):
        raise ValueError("pytest files must be explicitly covered by the supplied plan")
    return sorted(set(targets))


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
                command,
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
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
        "TEMP",
        "TMP",
        "TMPDIR",
        "MPLCONFIGDIR",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
        "NPM_CONFIG_CACHE",
        "HYPOTHESIS_STORAGE_DIRECTORY",
        "RUFF_CACHE_DIR",
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
    if path_has_redirect(ROOT, interpreter) or not interpreter.resolve().is_relative_to(
        ROOT.resolve()
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
    validation_plan: str | None = None,
) -> int:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ValueError("quality time and output budgets must be positive and finite")
    if tool == "pyright" and any(
        arg.startswith(("--pythonpath", "--venvpath")) for arg in arguments
    ):
        raise ValueError("the quality runner owns the project Python interpreter binding")
    if tool == "pytest" and any(arg.startswith(("--basetemp", "--junit")) for arg in arguments):
        raise ValueError("pytest temporary and evidence paths are owned by this runner")
    selected_tests: list[str] = []
    if validation_plan is not None:
        if tool != "pytest":
            raise ValueError("a validation plan currently binds explicit pytest inputs only")
        plan_path = (ROOT / validation_plan).resolve()
        if not plan_path.is_relative_to((ROOT / ".ai-bridge").resolve()):
            raise ValueError("validation plan must be generated inside .ai-bridge")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("version") != "validation-plan-v1":
            raise ValueError("unsupported validation plan")
        selected_tests = planned_pytest_targets(arguments, plan["tests"])

    def current_inputs() -> dict[str, Any]:
        if not selected_tests:
            return {"scope": "FULL_WORKSPACE", "files": None}
        return validation_input_closure(ROOT, selected_tests)

    def current_fingerprint(inputs: dict[str, Any]) -> str:
        return (
            fingerprint(inputs["files"])
            if inputs["scope"] == "DEPENDENCY_CLOSURE"
            else fingerprint()
        )

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
    if tool == "imports":
        command = [
            str(
                Path(sys.executable).parent
                / ("lint-imports.exe" if sys.platform == "win32" else "lint-imports")
            )
        ]
    elif tool != "python":
        command += ["-m", tool]
    if tool == "pyright":
        command += ["--pythonpath", type_interpreter]
    command += arguments
    if tool == "pytest":
        command += [
            "--basetemp",
            str(temporary / "pytest"),
            "-p",
            "no:cacheprovider",
            "--junitxml",
            str(directory / "junit.xml"),
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
        inputs_before = current_inputs()
        result["input_closure_before"] = inputs_before
        result["source_hash_before"] = current_fingerprint(inputs_before)
        result["workspace_hash_before"] = (
            fingerprint()
            if inputs_before["scope"] == "DEPENDENCY_CLOSURE"
            else result["source_hash_before"]
        )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "command": command,
                    "cwd": str(ROOT),
                    "output_log": str(output_path.relative_to(ROOT)),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        worker = KillableResearchWorker(
            _execute_command,
            (
                command,
                _environment(temporary),
                str(ROOT),
                str(output_path),
                str(command_result),
                max_output_bytes,
            ),
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
            inputs_after = current_inputs()
            result["input_closure_after"] = inputs_after
            result["source_hash_after"] = current_fingerprint(inputs_after)
            result["workspace_hash_after"] = (
                fingerprint()
                if inputs_after["scope"] == "DEPENDENCY_CLOSURE"
                else result["source_hash_after"]
            )
        except Exception as exc:
            result.update(failure_reason="FINGERPRINT_ERROR", error_type=type(exc).__name__)
        result["source_tree_stable"] = bool(
            result["source_hash_before"]
            and result["source_hash_before"] == result["source_hash_after"]
        )
        result["validation_inputs_stable"] = result["source_tree_stable"]
        result["workspace_tree_stable"] = bool(
            result.get("workspace_hash_before")
            and result.get("workspace_hash_before") == result.get("workspace_hash_after")
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
                status="FAILED",
                returncode=1,
                failure_reason="OUTPUT_READ_ERROR",
                error_type=type(exc).__name__,
            )
        result["output_sha256"] = hashlib.sha256(raw_output).hexdigest()
        result["output_bytes_retained"] = len(raw_output)
        result.setdefault("output_bytes_observed", None)
        result.setdefault("output_truncated", None)
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["duration_seconds"] = time.perf_counter() - started
        owner.update(
            status=result["status"],
            finished_at=result["finished_at"],
            returncode=result["returncode"],
            owned_worker_reaped=cleanup_confirmed,
        )
        for owner_path in (directory / "owner.json", temporary / "owner.json"):
            if not owner_path.parent.exists():
                continue
            try:
                _write_json(owner_path, owner)
            except OSError as exc:
                result.update(
                    status="FAILED",
                    returncode=1,
                    failure_reason="OWNER_RECEIPT_ERROR",
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
    print(raw_output[-64 * 1024 :].decode("utf-8", errors="backslashreplace"), end="", flush=True)
    print(json.dumps(_transport_receipt(result), ensure_ascii=False), flush=True)
    return int(result["returncode"])


def _transport_receipt(result: dict[str, Any]) -> dict[str, Any]:
    """Keep audit read sets in result.json without drowning the command transport."""
    summary = dict(result)
    for key in ("input_closure_before", "input_closure_after"):
        closure = result.get(key)
        if isinstance(closure, dict):
            summary[key] = {
                name: closure.get(name) for name in ("version", "scope", "coverage_proven")
            }
            summary[key].update({
                f"{name}_count": len(value) for name, value in closure.items()
                if isinstance(value, (list, dict))
            })
    summary["result_json"] = str(Path(result["evidence_directory"]) / "result.json")
    return summary


def main() -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--max-output-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--type-environment", choices=("main", "mootdx"), default="main")
    parser.add_argument(
        "--validation-plan", help="Generated plan; unknown reads remain full scope."
    )
    parser.add_argument("tool", choices=("pytest", "ruff", "pyright", "python", "imports"))
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
        kwargs: dict[str, Any] = {}
        if args.validation_plan is not None:
            kwargs["validation_plan"] = args.validation_plan
        return run_quality(
            args.tool,
            arguments,
            timeout_seconds=args.timeout_seconds,
            max_output_bytes=args.max_output_bytes,
            type_environment=args.type_environment,
            **kwargs,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
