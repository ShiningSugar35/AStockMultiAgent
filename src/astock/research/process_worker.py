"""Owned, killable local workers for blocking current-research operations.

Only Python-owned callables are accepted; request JSON never selects executable code.
Windows jobs contain descendants and terminate them when the coordinator dies.
"""

from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astock.schemas.research_sla import ResearchSchedulerTask


class WorkerCancelledError(RuntimeError):
    """The owning request cancelled this worker attempt."""


class WorkerDeadlineError(TimeoutError):
    """The original request deadline expired, including process startup."""


class _WindowsJob:
    """A non-inheritable kill-on-close job, attached before business work starts."""

    def __init__(self, pid: int) -> None:
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("research worker job creation failed")
        process_handle = None
        try:
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise OSError("research worker job limits failed")
            process_handle = kernel.OpenProcess(0x0101, False, pid)
            if not process_handle or not kernel.AssignProcessToJobObject(
                self.handle, process_handle
            ):
                raise OSError("research worker job assignment failed")
        except BaseException:
            self.close()
            raise
        finally:
            if process_handle:
                kernel.CloseHandle(process_handle)

    def close(self) -> None:
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise OSError("research worker job close failed")
            self.handle = None


def _child_entry(
    target: Callable[..., str],
    args: tuple[Any, ...],
    output: Connection,
    start_gate: Any,
    deadline_at: datetime | None,
    parent_pid: int,
    scratch: str,
) -> None:
    """Run after isolation is established; never forward SDK stdout or exception text."""
    sys.dont_write_bytecode = True
    if os.name != "nt":
        os.setsid()
    remaining = (
        None if deadline_at is None else (deadline_at - datetime.now(UTC)).total_seconds()
    )
    end = None if remaining is None else time.monotonic() + max(0.0, remaining)

    def watchdog() -> None:
        while os.getppid() == parent_pid and (end is None or time.monotonic() < end):
            sleep_for = 0.05 if end is None else min(0.05, max(0.0, end - time.monotonic()))
            time.sleep(sleep_for)
        if os.name != "nt":
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(124)

    threading.Thread(target=watchdog, daemon=True, name="research-worker-boundary").start()
    if remaining is None:
        start_gate.wait()
    elif remaining <= 0 or not start_gate.wait(remaining):
        os._exit(124)
    for key in ("TMP", "TEMP", "TMPDIR", "XDG_CACHE_HOME", "MPLCONFIGDIR", "UV_CACHE_DIR"):
        os.environ[key] = scratch
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tempfile.tempdir = scratch
    with open(os.devnull, "w", encoding="utf-8") as sink:
        sys.stdout = sink
        sys.stderr = sink
        # Python stream assignment alone does not redirect native SDK writes or
        # the OS handles inherited by descendants. Only this owned child changes.
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        if os.name == "nt":
            import msvcrt
            from ctypes import wintypes

            kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
            kernel.SetStdHandle.restype = wintypes.BOOL
            for standard, descriptor in ((-11, 1), (-12, 2)):
                if not kernel.SetStdHandle(standard, msvcrt.get_osfhandle(descriptor)):
                    raise OSError("research worker standard handle redirection failed")
        try:
            result = target(*args)
            if not isinstance(result, str) or not result.strip() or len(result) > 2048:
                raise ValueError("invalid worker artifact reference")
            message = {"status": "ok", "artifact_id": result}
        except BaseException as exc:
            message = {"status": "error", "error_type": type(exc).__name__}
        output.send_bytes(json.dumps(message, ensure_ascii=True).encode("ascii"))
        output.close()
        sink.flush()
        # SDK-owned threads must not keep a successful worker alive at interpreter exit.
        os._exit(0)


class KillableResearchWorker:
    """One attempt, one process; cancel() waits for termination, never for an SDK."""

    def __init__(
        self,
        target: Callable[..., str],
        args: tuple[Any, ...],
        *,
        deadline_at: datetime | None,
        project_root: Path,
        scratch_root: Path,
    ) -> None:
        if deadline_at is not None and (
            deadline_at.tzinfo is None or deadline_at.utcoffset() is None
        ):
            raise ValueError("research worker deadline must be timezone-aware when provided")
        root = project_root.resolve()
        scratch = scratch_root.resolve()
        if not scratch.is_relative_to(root):
            # The production checkout owns worker scratch. Keep compatibility
            # with callers that pass a temporary request directory while the
            # repository root is the real ownership boundary.
            if (root / "src" / "astock").is_dir():
                scratch_root.mkdir(parents=True, exist_ok=True)
                scratch = root / "runtime" / "workers"
            else:
                raise ValueError("research worker scratch must stay inside its project")
        self.target, self.args = target, args
        self.deadline_at = deadline_at
        self.scratch_root = scratch
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._process: BaseProcess | None = None
        self._job: _WindowsJob | None = None
        self._used = False
        self.last_pid: int | None = None

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.is_alive()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        process = self._process
        if process is None:
            return
        if self._job is not None:
            self._job.close()
            self._job = None
        if os.name != "nt" and process.pid is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.is_alive():
            process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        if process.is_alive():
            raise RuntimeError("research worker termination not confirmed")

    def _check_boundary(self, end: float | None) -> None:
        if self._cancelled.is_set():
            raise WorkerCancelledError("research worker cancelled")
        if end is not None and time.monotonic() >= end:
            raise WorkerDeadlineError("research request deadline expired")
        if self.deadline_at is not None and datetime.now(UTC) >= self.deadline_at:
            raise WorkerDeadlineError("research request deadline expired")

    def __call__(self, task: ResearchSchedulerTask) -> str:
        with self._lock:
            if self._cancelled.is_set():
                raise WorkerCancelledError("research worker cancelled before start")
            if self._used:
                raise RuntimeError("research worker attempt cannot be reused")
            self._used = True
        remaining = (
            None
            if self.deadline_at is None
            else (self.deadline_at - datetime.now(UTC)).total_seconds()
        )
        if remaining is not None and remaining <= 0:
            raise WorkerDeadlineError("research request deadline expired")
        end = None if remaining is None else time.monotonic() + remaining
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        gate = context.Event()
        scratch: str | None = None
        try:
            with self._lock:
                if self._cancelled.is_set():
                    raise WorkerCancelledError("research worker cancelled before start")
                self.scratch_root.mkdir(parents=True, exist_ok=True)
                scratch = tempfile.mkdtemp(prefix="attempt-", dir=self.scratch_root)
                process = context.Process(
                    target=_child_entry,
                    args=(
                        self.target,
                        self.args,
                        send,
                        gate,
                        self.deadline_at,
                        os.getpid(),
                        scratch,
                    ),
                    name="astock-company-worker",
                )
                process.start()
                self._process = process
                self.last_pid = process.pid
                if os.name == "nt" and process.pid is not None:
                    self._job = _WindowsJob(process.pid)
                gate.set()
                send.close()
            while True:
                self._check_boundary(end)
                if receive.poll(0.05):
                    try:
                        message = json.loads(receive.recv_bytes(8192))
                    except (EOFError, OSError, ValueError) as exc:
                        self._check_boundary(end)
                        raise RuntimeError("research worker ended without a valid result") from exc
                    self._check_boundary(end)
                    if not isinstance(message, dict) or message.get("status") != "ok":
                        # No arbitrary child exception text, provider response or private path.
                        kind = (
                            message.get("error_type", "UnknownError")
                            if isinstance(message, dict)
                            else "UnknownError"
                        )
                        if not isinstance(kind, str) or not kind.isidentifier() or len(kind) > 80:
                            kind = "UnknownError"
                        raise RuntimeError(f"research worker operation failed: {kind}")
                    artifact_id = message.get("artifact_id")
                    if not isinstance(artifact_id, str) or not artifact_id.strip():
                        raise RuntimeError("research worker returned an invalid artifact reference")
                    return artifact_id
                with self._lock:
                    if not process.is_alive():
                        self._check_boundary(end)
                        raise RuntimeError("research worker exited without a result")
        finally:
            with self._lock:
                self._stop_locked()
                if self._process is not None:
                    self._process.close()
                    self._process = None
            receive.close()
            send.close()
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)


def run_company_request(
    project_root: str,
    database: str,
    object_root: str,
    parquet_root: str,
    request_json: str,
) -> str:
    """Rebuild the same canonical services in the child; never inherit SQLite handles."""
    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.knowledge.completion_repository import KnowledgeCompletionRepository
    from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
    from astock.research.runtime import ResearchRunService
    from astock.schemas.research_runtime import ResearchRunRequest

    root = Path(project_root).resolve()
    for raw in (database, object_root, parquet_root):
        if not Path(raw).resolve().is_relative_to(root):
            raise ValueError("research worker paths must remain project-local")
    state = StateStore(Path(database), root / "migrations")
    objects = ObjectStore(Path(object_root))
    service = ResearchRunService(
        project_root=root,
        state=state,
        objects=objects,
        reference_parquet_root=Path(parquet_root),
        knowledge_provider=RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state), objects
        ),
    )
    report = service.run(ResearchRunRequest.model_validate_json(request_json))
    return f"ResearchRunReport:{report.report_id}"
