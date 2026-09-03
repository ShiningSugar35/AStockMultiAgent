"""Recoverable single-instance daemon for the continuous investment monitor."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from astock.monitoring.config import ContinuousMonitorConfig
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.monitoring.service import ContinuousMonitorService


class _DaemonLeaseLost(RuntimeError):
    """Raised when the durable singleton lease is no longer owned by this daemon."""


def _run_cycle_with_lease_heartbeat(
    service: ContinuousMonitorService,
    repository: ContinuousMonitorRepository,
    *,
    owner_id: str,
    live: bool,
    cycle_started: datetime,
    heartbeat_seconds: int,
):
    """Run one synchronous cycle while renewing the durable daemon lease."""

    stop_heartbeat = Event()
    lease_lost = Event()

    def renew_lease() -> None:
        while not stop_heartbeat.wait(heartbeat_seconds):
            try:
                if not repository.heartbeat_daemon(owner_id, at=datetime.now(UTC)):
                    lease_lost.set()
                    return
            except Exception:
                # StateStore already applies a bounded SQLite busy timeout. Once a
                # renewal attempt still fails, do not keep running toward lease expiry:
                # mark the lease unsafe and let the daemon fail closed after the
                # current bounded cycle returns.
                lease_lost.set()
                return

    heartbeat_thread = Thread(
        target=renew_lease,
        name="continuous-monitor-lease-heartbeat",
        daemon=True,
    )
    def assert_lease_owned() -> None:
        if lease_lost.is_set():
            raise _DaemonLeaseLost(
                "continuous monitor daemon lease ownership was lost during cycle"
            )

    heartbeat_thread.start()
    try:
        report = service.run_cycle(
            owner_id=owner_id,
            live=live,
            now=cycle_started,
            lease_guard=assert_lease_owned,
        )
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=max(5.0, heartbeat_seconds + 1.0))
    if heartbeat_thread.is_alive():
        raise RuntimeError("continuous monitor lease heartbeat thread did not stop")
    if lease_lost.is_set():
        raise _DaemonLeaseLost("continuous monitor daemon lease ownership was lost during cycle")
    return report


def run_daemon(
    service: ContinuousMonitorService,
    repository: ContinuousMonitorRepository,
    config: ContinuousMonitorConfig,
    *,
    live: bool,
    interval_seconds: int | None = None,
    owner_id: str | None = None,
) -> int:
    owner = owner_id or f"continuous-monitor:{uuid4().hex}"
    interval = interval_seconds or config.wake_interval_seconds
    now = datetime.now(UTC)
    if not repository.acquire_daemon(
        owner_id=owner,
        pid=os.getpid(),
        at=now,
        lease_seconds=config.daemon.lease_seconds,
        details={"live": live, "interval_seconds": interval},
    ):
        return 3
    failed = False
    try:
        while True:
            if repository.daemon_stop_requested(owner):
                break
            cycle_started = datetime.now(UTC)
            try:
                report = _run_cycle_with_lease_heartbeat(
                    service,
                    repository,
                    owner_id=owner,
                    live=live,
                    cycle_started=cycle_started,
                    heartbeat_seconds=config.daemon.heartbeat_seconds,
                )
                if not repository.heartbeat_daemon(
                    owner,
                    at=datetime.now(UTC),
                    last_run_id=report.run_id,
                ):
                    raise _DaemonLeaseLost("continuous monitor daemon lease ownership was lost")
            except _DaemonLeaseLost:
                failed = True
                return 4
            except Exception:
                failed = True
                if not repository.heartbeat_daemon(owner, at=datetime.now(UTC)):
                    return 4
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                if repository.daemon_stop_requested(owner):
                    return 0
                if not repository.heartbeat_daemon(owner, at=datetime.now(UTC)):
                    failed = True
                    return 4
                time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        return 0
    finally:
        repository.release_daemon(owner, at=datetime.now(UTC), failed=failed)


def spawn_daemon(
    project_root: Path,
    *,
    live: bool,
    interval_seconds: int,
    owner_id: str | None = None,
) -> int:
    command = [
        sys.executable,
        "-m",
        "astock",
        "continuous-monitor-daemon",
        "--interval-seconds",
        str(interval_seconds),
    ]
    if live:
        command.append("--live")
    if owner_id is not None:
        command.extend(["--owner-id", owner_id])
    env = os.environ.copy()
    env["ASTOCK_PROJECT_ROOT"] = str(project_root.resolve())
    if os.name == "nt":
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(project_root.resolve()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            ),
        )
    else:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(project_root.resolve()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    return int(process.pid)


__all__ = ["run_daemon", "spawn_daemon"]
