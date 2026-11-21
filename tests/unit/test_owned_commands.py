"""The command adapter uses the existing killable owner for real CLI/SDK children."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from astock.research.process_worker import WorkerDeadlineError, run_owned_command
from astock.storage_retention import process_identity

ROOT = Path(__file__).resolve().parents[2]


def test_owned_command_preserves_nonzero_exit_and_removes_private_temp() -> None:
    result = run_owned_command(
        [
            sys.executable,
            "-B",
            "-c",
            "import json,os,sys; print(json.dumps({'pid':os.getpid(),'temp':os.environ['TEMP']})); "
            "sys.exit(7)",
        ],
        project_root=ROOT,
        timeout_seconds=30,
    )
    observed = json.loads(result.stdout)
    assert result.returncode == 7
    assert result.owned_worker_reaped
    assert process_identity(observed["pid"]).status == "DEAD"
    assert Path(observed["temp"]).is_relative_to(ROOT)
    assert not Path(observed["temp"]).exists()


def test_owned_command_output_flood_is_bounded_and_reaped() -> None:
    with pytest.raises(RuntimeError, match="output budget exceeded"):
        run_owned_command(
            [sys.executable, "-B", "-c", "import sys; sys.stdout.write('x' * 1000000)"],
            project_root=ROOT,
            timeout_seconds=30,
            max_output_bytes=128,
        )


def test_owned_command_deadline_reaps_real_cli_child(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    with pytest.raises(WorkerDeadlineError):
        run_owned_command(
            [
                sys.executable,
                "-B",
                "-c",
                "import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)",
                str(marker),
            ],
            project_root=ROOT,
            timeout_seconds=5,
        )
    assert marker.exists()
    assert process_identity(int(marker.read_text(encoding="utf-8"))).status == "DEAD"
