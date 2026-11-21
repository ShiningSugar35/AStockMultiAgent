"""Native owned-process checks for quality commands and their terminal receipts."""

from __future__ import annotations

import _thread
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from astock.research import process_worker
from astock.storage_retention import process_identity
from scripts import run_local_quality

ROOT = Path(__file__).resolve().parents[2]


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str,
    *, timeout: float = 20.0, output_budget: int = 8192,
) -> tuple[int, dict[str, Any], Path]:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    exit_code = run_local_quality.run_quality(
        "python", ["-c", code], timeout_seconds=timeout, max_output_bytes=output_budget,
    )
    receipts = list((tmp_path / ".ai-bridge" / "quality-runs").glob("*/result.json"))
    assert len(receipts) == 1
    return exit_code, json.loads(receipts[0].read_text(encoding="utf-8")), receipts[0].parent


def _wait_file(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), "owned command did not reach its marker"


def _assert_dead(pid: int) -> None:
    deadline = time.monotonic() + 5
    while process_identity(pid).status != "DEAD" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert process_identity(pid).status == "DEAD"


def _tree_code(marker: Path, *, finish: bool = False) -> str:
    return (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"marker=pathlib.Path({str(marker)!r}); pending=marker.with_suffix('.pending')\n"
        "pending.write_text(json.dumps({'command_pid':os.getpid(),"
        "'grandchild_pid':child.pid}),encoding='utf-8'); pending.replace(marker)\n"
        + ("print('finished')\n" if finish else "time.sleep(60)\n")
    )


def test_success_keeps_compatible_receipt_and_cleans_owned_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, result, directory = _run(tmp_path, monkeypatch, "print('quality-success')")

    assert code == 0
    assert result["status"] == "COMPLETED"
    assert result["child_exit_code"] == 0
    assert result["source_tree_stable"] is True
    assert result["owned_worker_reaped"] is True
    assert result["temporary_cleanup"] == "REMOVED"
    assert "quality-success" in (directory / "output.log").read_text(encoding="utf-8")
    owner = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
    assert owner["schema_version"] == "astock-owned-run-v1"
    assert owner["owner_pid"] == os.getpid()
    assert owner["owner_create_time"] is not None
    assert owner["status"] == "COMPLETED"
    assert not Path(owner["scratch_dir"]).exists()


def test_child_failure_is_not_lost_and_still_cleans_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, result, _ = _run(tmp_path, monkeypatch, "raise SystemExit(17)")

    assert code == result["child_exit_code"] == 17
    assert result["status"] == "FAILED"
    assert result["failure_reason"] == "CHILD_EXIT_NONZERO"
    assert result["temporary_cleanup"] == "REMOVED"


def test_output_without_newlines_is_bounded_and_stops_the_owned_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, result, directory = _run(
        tmp_path, monkeypatch,
        "import os,time; os.write(1,b'x'*20000); time.sleep(60)",
        output_budget=1024,
    )

    assert code != 0
    assert result["failure_reason"] == "OUTPUT_LIMIT"
    assert result["output_truncated"] is True
    assert result["output_bytes_observed"] > 1024
    assert (directory / "output.log").stat().st_size == 1024
    assert result["owned_worker_reaped"] is True
    assert result["temporary_cleanup"] == "REMOVED"
    _assert_dead(result["command_pid"])


def test_source_mutation_cannot_produce_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "changed.py"
    source.parent.mkdir()
    source.write_text("BEFORE = 1\n", encoding="utf-8")
    code, result, _ = _run(
        tmp_path, monkeypatch,
        f"from pathlib import Path; Path({str(source)!r}).write_text('AFTER = 2\\n')",
    )

    assert code == 3
    assert result["child_exit_code"] == 0
    assert result["source_tree_stable"] is False
    assert result["status"] == "FAILED"


def test_timeout_reaps_command_and_descendant_and_records_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "timeout-pids.json"
    code, result, _ = _run(tmp_path, monkeypatch, _tree_code(marker), timeout=8.0)

    assert code == 124
    assert result["status"] == "TIMED_OUT"
    assert result["child_exit_code"] is None
    assert result["temporary_cleanup"] == "REMOVED"
    pids = json.loads(marker.read_text(encoding="utf-8"))
    _assert_dead(pids["command_pid"])
    _assert_dead(pids["grandchild_pid"])


def test_success_also_reaps_a_lingering_owned_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "normal-pids.json"
    code, result, _ = _run(tmp_path, monkeypatch, _tree_code(marker, finish=True))

    assert code == 0
    assert result["owned_worker_reaped"] is True
    _assert_dead(json.loads(marker.read_text(encoding="utf-8"))["grandchild_pid"])


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment boundary")
def test_job_assignment_failure_cannot_start_the_quality_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "must-not-run.txt"

    def fail_job(_pid: int) -> None:
        raise OSError("injected job assignment failure")

    monkeypatch.setattr(process_worker, "_WindowsJob", fail_job)
    code, result, _ = _run(
        tmp_path, monkeypatch,
        f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')",
    )

    assert code != 0
    assert result["status"] == "FAILED"
    assert result["error_type"] == "OSError"
    assert result["owned_worker_reaped"] is True
    assert result["temporary_cleanup"] == "REMOVED"
    assert not marker.exists()


def test_interrupt_cancels_the_command_tree_and_records_a_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "cancel-pids.json"
    finished = threading.Event()

    def interrupt_when_started() -> None:
        deadline = time.monotonic() + 15
        while not finished.is_set() and time.monotonic() < deadline:
            if marker.exists():
                _thread.interrupt_main()
                return
            finished.wait(0.02)

    interrupter = threading.Thread(target=interrupt_when_started, daemon=True)
    interrupter.start()
    try:
        code, result, _ = _run(tmp_path, monkeypatch, _tree_code(marker))
    finally:
        finished.set()
        interrupter.join(2)

    assert code == 130
    assert result["status"] == "CANCELLED"
    assert result["owned_worker_reaped"] is True
    assert result["temporary_cleanup"] == "REMOVED"
    pids = json.loads(marker.read_text(encoding="utf-8"))
    _assert_dead(pids["command_pid"])
    _assert_dead(pids["grandchild_pid"])


@pytest.mark.skipif(os.name != "nt", reason="Windows kill-on-close coordinator ownership")
def test_runner_coordinator_death_reaps_the_owned_tree(tmp_path: Path) -> None:
    marker = tmp_path / "runner-pids.json"
    runner = subprocess.Popen(
        [
            sys.executable, "-B", str(ROOT / "scripts" / "run_local_quality.py"),
            "--timeout-seconds", "30", "python", "-c", _tree_code(marker),
        ],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert runner.stdout is not None
        first_line = json.loads(runner.stdout.readline())
        directory = ROOT / ".ai-bridge" / "quality-runs" / first_line["run_id"]
        _wait_file(marker)
        pids = json.loads(marker.read_text(encoding="utf-8"))
        runner.terminate()
        runner.communicate(timeout=10)
        _assert_dead(pids["command_pid"])
        _assert_dead(pids["grandchild_pid"])
        # Hard termination cannot run Python finally. GC can identify this dead
        # owner; the test must not fabricate a successful runner receipt.
        assert not (directory / "result.json").exists()
        owner = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
        assert owner["status"] == "RUNNING"
        scratch = Path(owner["scratch_dir"])
        assert scratch.is_relative_to(ROOT / ".scratchpad")
        assert scratch.name == "quality-" + first_line["run_id"]
        shutil.rmtree(scratch)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)


def test_redirected_cache_is_rejected_before_starting_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "must-not-run.txt"
    original = run_local_quality.path_has_redirect

    def redirected_cache(root: Path, path: Path) -> bool:
        cache = root / ".ai-bridge" / "quality-cache"
        return path == cache or cache in path.parents or original(root, path)

    monkeypatch.setattr(run_local_quality, "path_has_redirect", redirected_cache)
    code, result, _ = _run(
        tmp_path, monkeypatch,
        f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')",
    )

    assert code != 0
    assert result["status"] == "FAILED"
    assert result["error_type"] == "ValueError"
    assert result["worker_pid"] is None
    assert result["temporary_cleanup"] == "REMOVED"
    assert not marker.exists()


def test_quality_environment_bounds_blas_without_changing_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    variables = (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
    )
    for variable in variables:
        monkeypatch.setenv(variable, "12")

    environment = run_local_quality._environment(tmp_path / "temporary")

    assert all(environment[variable] == "1" for variable in variables)
    assert all(os.environ[variable] == "12" for variable in variables)

def test_type_environment_selects_only_the_canonical_sdk_and_cli_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    interpreter = tmp_path / "runtime" / "tool-environments" / "mootdx" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture")
    assert run_local_quality._type_interpreter("pyright", "mootdx") == str(interpreter.resolve())
    assert run_local_quality._type_interpreter("pyright", "main") == sys.executable
    captured: dict[str, Any] = {}

    def capture_run(tool: str, arguments: list[str], **options: Any) -> int:
        captured.update(tool=tool, arguments=arguments, **options)
        return 0

    monkeypatch.setattr(run_local_quality, "run_quality", capture_run)
    monkeypatch.setattr(
        sys, "argv",
        ["run_local_quality.py", "--type-environment", "mootdx", "pyright", "worker.py"],
    )
    assert run_local_quality.main() == 0
    assert captured["tool"] == "pyright"
    assert captured["arguments"] == ["worker.py"]
    assert captured["type_environment"] == "mootdx"


@pytest.mark.parametrize("boundary", ["missing", "redirect", "escape"])
def test_type_environment_rejects_missing_or_escaping_sdk_before_any_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    interpreter = tmp_path / "runtime" / "tool-environments" / "mootdx" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if boundary != "missing":
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b"fixture")
    if boundary == "redirect":
        monkeypatch.setattr(run_local_quality, "path_has_redirect", lambda _root, _path: True)
    if boundary == "escape":
        original_resolve = Path.resolve

        def escaped_resolve(path: Path, strict: bool = False) -> Path:
            if path == interpreter:
                return tmp_path.parent / "outside-python.exe"
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", escaped_resolve)
    with pytest.raises(ValueError, match="mootdx interpreter"):
        run_local_quality.run_quality(
            "pyright", [], timeout_seconds=10, max_output_bytes=1024,
            type_environment="mootdx",
        )
    assert not (tmp_path / ".ai-bridge").exists()


@pytest.mark.parametrize(
    ("tool", "type_environment", "arguments"),
    [
        ("python", "mootdx", []),
        ("pytest", "mootdx", []),
        ("ruff", "mootdx", []),
        ("pyright", "unknown", []),
        ("pyright", "main", ["--pythonpath", "outside-python.exe"]),
        ("pyright", "mootdx", ["--pythonpath=outside-python.exe"]),
        ("pyright", "main", ["--venvpath", "outside"]),
        ("pyright", "mootdx", ["--venvpath=outside"]),
    ],
)
def test_type_environment_rejects_non_pyright_or_arbitrary_path_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tool: str, type_environment: str, arguments: list[str],
) -> None:
    monkeypatch.setattr(run_local_quality, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        run_local_quality.run_quality(
            tool, arguments, timeout_seconds=10, max_output_bytes=1024,
            type_environment=type_environment,
        )
    assert not (tmp_path / ".ai-bridge").exists()
