"""Recoverable single-instance daemon for the continuous investment monitor."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from astock.monitoring.config import ContinuousMonitorConfig
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.monitoring.service import ContinuousMonitorService


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
                report = service.run_cycle(owner_id=owner, live=live, now=cycle_started)
                repository.heartbeat_daemon(owner, at=datetime.now(UTC), last_run_id=report.run_id)
            except Exception:
                failed = True
                repository.heartbeat_daemon(owner, at=datetime.now(UTC))
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                if repository.daemon_stop_requested(owner):
                    return 0
                repository.heartbeat_daemon(owner, at=datetime.now(UTC))
                time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        return 0
    finally:
        repository.release_daemon(owner, at=datetime.now(UTC), failed=failed)


def spawn_daemon(
    project_root: Path,
    *,
    live: bool,
    interval_seconds: int,
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
