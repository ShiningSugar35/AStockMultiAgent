"""Exercise real host-process ownership and release after abnormal worker exit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from astock.investor_orchestration.run_ownership import (
    ScheduledRunInProgress,
    schedule_run_ownership,
)

_CHILD = """
import os
import sys
from pathlib import Path
from astock.investor_orchestration.run_ownership import (
    ScheduledRunInProgress, schedule_run_ownership,
)
try:
    with schedule_run_ownership(Path(sys.argv[1]), 'binding', 'bucket'):
        os._exit(0)
except ScheduledRunInProgress:
    sys.exit(23)
"""


def test_other_process_is_excluded_and_abnormal_exit_releases_ownership(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    command = [sys.executable, "-B", "-c", _CHILD, str(database)]
    with schedule_run_ownership(database, "binding", "bucket"):
        child = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        assert child.returncode == 23, child.stderr
    child = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert child.returncode == 0, child.stderr
    with schedule_run_ownership(database, "binding", "bucket"):
        pass
    assert not database.exists(), "coordination must not invent an economic state database"


def test_different_buckets_or_bindings_do_not_block_each_other(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with schedule_run_ownership(database, "binding", "bucket"):
        with schedule_run_ownership(database, "binding", "next-bucket"):
            with schedule_run_ownership(database, "another-binding", "bucket"):
                pass
        with pytest.raises(ScheduledRunInProgress):
            with schedule_run_ownership(database, "binding", "bucket"):
                pytest.fail("the second owner entered the same critical section")


def test_ownership_is_released_on_analyzer_exception(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with pytest.raises(ValueError, match="recorded failure"):
        with schedule_run_ownership(database, "binding", "bucket"):
            raise ValueError("recorded failure")
    with schedule_run_ownership(database, "binding", "bucket"):
        pass
