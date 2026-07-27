from __future__ import annotations

import os

import pytest

from astock.books.visual_repository import BookVisualRepository
from astock.core.state import StateStore
from astock.schemas import BookVisualCoverageStatus, BookVisualRunStage
from astock.settings import ProjectPaths


@pytest.mark.acceptance
def test_private_book_visual_coverage_matches_frozen_real_run() -> None:
    run_id = os.environ.get("ASTOCK_PRIVATE_BOOK_VISUAL_RUN_ID")
    if not run_id:
        pytest.skip(
            "set ASTOCK_PRIVATE_BOOK_VISUAL_RUN_ID after the approved local-only real run"
        )
    paths = ProjectPaths.discover()
    repository = BookVisualRepository(
        StateStore(paths.state_db, paths.root / "migrations")
    )
    run = repository.get_run(run_id)
    assert run is not None
    assert run.stage is BookVisualRunStage.AUDITED
    report = repository.report(run_id)
    assert report is not None
    assert report.coverage_status is BookVisualCoverageStatus.COMPLETE
    assert report.source_pages == 249
    assert report.image_pages == 57
    assert report.image_placements == 74
    assert report.processed_placements == 74
    assert report.image_only_ready_candidate_count == 0
