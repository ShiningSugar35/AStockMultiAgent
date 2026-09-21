"""Real spawned processes: bounded lifetime, private output and owned descendants."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from astock.research.process_worker import (
    KillableResearchWorker,
    WorkerCancelledError,
    WorkerDeadlineError,
)
from astock.schemas.research_sla import ResearchSchedulerTask

ROOT = Path(__file__).resolve().parents[2]
TASK = cast(ResearchSchedulerTask, None)


def _success(marker: str) -> str:
    print("SDK output must not leak into the CLI JSON")
    Path(marker).write_text(
        json.dumps({"pid": os.getpid(), "temp": os.environ["TEMP"]}), encoding="utf-8"
    )
    threading.Thread(target=lambda: time.sleep(60), daemon=False).start()
    return "ResearchRunReport:recorded-test"


def _blocking(marker: str) -> str:
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(60)
    Path(marker + ".late").write_text("unexpected late write", encoding="utf-8")
    return "ResearchRunReport:late"


def _spawn_descendant(marker: str) -> str:
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]+'.ready').write_text('ready'); "
            "time.sleep(2); pathlib.Path(sys.argv[1]+'.late').write_text('late')",
            marker,
        ]
    )
    Path(marker).write_text(str(child.pid), encoding="utf-8")
    time.sleep(60)
    return "ResearchRunReport:late"


def _failure() -> str:
    raise ValueError("private provider response must stay in the child")


def _wait_marker(marker: Path) -> None:
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "worker did not reach its blocking operation"


def _worker(tmp_path: Path, target, args=(), seconds=20) -> KillableResearchWorker:
    return KillableResearchWorker(
        target,
        args,
        deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
        project_root=ROOT,
        scratch_root=tmp_path / "workers",
    )


def test_spawned_success_is_reaped_and_temp_is_project_local(tmp_path: Path) -> None:
    marker = tmp_path / "success.json"
    worker = _worker(tmp_path, _success, (str(marker),))
    started = time.monotonic()
    assert worker(TASK) == "ResearchRunReport:recorded-test"
    assert time.monotonic() - started < 15
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["pid"] != os.getpid()
    assert Path(record["temp"]).is_relative_to(ROOT)
    assert not worker.alive
    assert not list((tmp_path / "workers").iterdir())


def test_deadline_kills_real_blocking_process(tmp_path: Path) -> None:
    marker = tmp_path / "deadline.pid"
    worker = _worker(tmp_path, _blocking, (str(marker),), seconds=8)
    started = time.monotonic()
    with pytest.raises(WorkerDeadlineError):
        worker(TASK)
    assert marker.exists()
    assert time.monotonic() - started < 11
    assert not worker.alive
    assert not Path(str(marker) + ".late").exists()


def test_cancel_running_worker_does_not_wait_for_blocking_operation(tmp_path: Path) -> None:
    marker = tmp_path / "cancel.pid"
    worker = _worker(tmp_path, _blocking, (str(marker),))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker, TASK)
        try:
            _wait_marker(marker)
            started = time.monotonic()
            worker.cancel()
            assert time.monotonic() - started < 3
            assert not worker.alive
            with pytest.raises(WorkerCancelledError):
                future.result(timeout=3)
        finally:
            worker.cancel()
    assert not Path(str(marker) + ".late").exists()


def test_cancel_also_terminates_owned_sdk_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "descendant.pid"
    worker = _worker(tmp_path, _spawn_descendant, (str(marker),))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker, TASK)
        try:
            _wait_marker(marker)
            _wait_marker(Path(str(marker) + ".ready"))
            worker.cancel()
            with pytest.raises(WorkerCancelledError):
                future.result(timeout=3)
        finally:
            worker.cancel()
    time.sleep(2.2)
    assert not Path(str(marker) + ".late").exists()
    assert not worker.alive


def test_cancel_before_start_creates_no_process(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _success, (str(tmp_path / "never"),))
    worker.cancel()
    with pytest.raises(WorkerCancelledError):
        worker(TASK)
    assert worker.last_pid is None
    assert not (tmp_path / "never").exists()


def test_expired_deadline_is_not_reset(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _success, (str(tmp_path / "never"),), seconds=-1)
    with pytest.raises(WorkerDeadlineError):
        worker(TASK)
    assert worker.last_pid is None


def test_worker_error_does_not_return_private_exception_text(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _failure)
    with pytest.raises(RuntimeError, match="operation failed") as caught:
        worker(TASK)
    assert "private provider response" not in str(caught.value)
    assert not worker.alive


def test_scratch_outside_project_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside its project"):
        KillableResearchWorker(
            _failure,
            (),
            deadline_at=datetime.now(UTC) + timedelta(seconds=10),
            project_root=tmp_path,
            scratch_root=tmp_path.parent / "outside-not-created",
        )


def test_duplicate_call_cannot_kill_first_attempt(tmp_path: Path) -> None:
    marker = tmp_path / "original.pid"
    worker = _worker(tmp_path, _blocking, (str(marker),))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker, TASK)
        try:
            _wait_marker(marker)
            with pytest.raises(RuntimeError, match="reused"):
                worker(TASK)
            assert worker.alive
        finally:
            worker.cancel()
        with pytest.raises(WorkerCancelledError):
            future.result(timeout=3)


def _coordinator(marker: str) -> None:
    worker = _worker(Path(marker).parent, _spawn_descendant, (marker,), seconds=30)
    worker(TASK)


@pytest.mark.skipif(os.name != "nt", reason="Windows job handle ownership contract")
def test_coordinator_death_kills_worker_and_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "orphan.pid"
    coordinator = multiprocessing.get_context("spawn").Process(
        target=_coordinator,
        args=(str(marker),),
    )
    coordinator.start()
    try:
        _wait_marker(marker)
        _wait_marker(Path(str(marker) + ".ready"))
        coordinator.terminate()
        coordinator.join(3)
        assert not coordinator.is_alive()
        time.sleep(2.2)
        assert not Path(str(marker) + ".late").exists()
    finally:
        if coordinator.is_alive():
            coordinator.kill()
            coordinator.join(3)
        coordinator.close()


def test_cancelling_worker_preserves_unrelated_process(tmp_path: Path) -> None:
    marker = tmp_path / "owned.pid"
    unrelated_marker = tmp_path / "unrelated.txt"
    unrelated = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('ok')",
            str(unrelated_marker),
        ]
    )
    worker = _worker(tmp_path, _blocking, (str(marker),))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker, TASK)
            try:
                _wait_marker(marker)
                worker.cancel()
                with pytest.raises(WorkerCancelledError):
                    future.result(timeout=3)
            finally:
                worker.cancel()
        assert unrelated.wait(timeout=5) == 0
        assert unrelated_marker.read_text(encoding="utf-8") == "ok"
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
            unrelated.wait(timeout=3)


def _native_sdk_output() -> str:
    sentinel = "OWNED_SDK_PRIVATE_OUTPUT_TEST_SENTINEL"
    os.write(1, (sentinel + ":native-stdout\n").encode())
    os.write(2, (sentinel + ":native-stderr\n").encode())
    subprocess.run([sys.executable, "-B", "-c", f"print({sentinel!r})"], check=True)
    return "ResearchRunReport:private-output-test"


def test_native_and_descendant_output_is_not_forwarded(tmp_path: Path, capfd) -> None:
    worker = _worker(tmp_path, _native_sdk_output)
    assert worker(TASK) == "ResearchRunReport:private-output-test"
    observed = capfd.readouterr()
    assert "OWNED_SDK_PRIVATE_OUTPUT_TEST_SENTINEL" not in observed.out
    assert "OWNED_SDK_PRIVATE_OUTPUT_TEST_SENTINEL" not in observed.err
    assert not worker.alive
