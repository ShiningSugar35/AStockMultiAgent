"""A scheduler terminal result must stop its owned blocking worker first."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.schemas.research_sla import ResearchSchedulerTask, ResearchTaskCategory

ROOT = Path(__file__).resolve().parents[2]


class BlockingProbe:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.late_write = False

    def __call__(self, task: ResearchSchedulerTask) -> str:
        if not self.cancelled.wait(2.5):
            self.late_write = True
        self.finished.set()
        return "probe:unpublished"

    def cancel(self) -> None:
        self.cancelled.set()
        assert self.finished.wait(1)


def test_scheduler_stops_cancellable_handler_before_returning(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    service = CurrentResearchSlaService(state, ObjectStore(tmp_path / "objects"))
    run = service.create_run(
        request_id="owned-worker-cancel",
        task_specs=[("blocking", None, ResearchTaskCategory.NETWORK, (), "input")],
        budget_seconds=1,
    )
    worker = BlockingProbe()
    try:
        ended = service.execute(run.run_id, {"blocking": worker})
        assert worker.cancelled.is_set(), "scheduler returned with an owned worker still running"
        assert worker.finished.is_set()
        assert not worker.late_write
        assert ended.deadline_at == run.deadline_at
        assert datetime.now(UTC) >= run.deadline_at
        assert all(task.result_artifact_id is None for task in service.tasks(run.run_id))
    finally:
        worker.cancelled.set()
        worker.finished.wait(3)


def test_completed_handler_is_not_cancelled(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    service = CurrentResearchSlaService(state, ObjectStore(tmp_path / "objects"))
    run = service.create_run(
        request_id="successful-worker-no-cancel",
        task_specs=[("fast", None, ResearchTaskCategory.NETWORK, (), "input")],
        budget_seconds=10,
    )
    cancelled = []

    class FastWorker:
        def __call__(self, task: ResearchSchedulerTask) -> str:
            time.sleep(0.01)
            return "probe:accepted"

        def cancel(self) -> None:
            cancelled.append(True)

    service.execute(run.run_id, {"fast": FastWorker()})
    assert not cancelled
    assert service.tasks(run.run_id)[0].result_artifact_id == "probe:accepted"
