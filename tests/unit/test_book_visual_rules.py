from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.books.visual_semantics import load_book_visual_distillation_config
from astock.books.visuals import BookVisualService
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    BookVisualRun,
    BookVisualRunStage,
    ChartUnitType,
    ImageEvidence,
    ImageOcrResult,
    ImageOcrStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 26, tzinfo=UTC)
HASH = "a" * 64


def _evidence(identifier: str, bbox: tuple[float, float, float, float]) -> ImageEvidence:
    return ImageEvidence(
        evidence_id=identifier,
        run_id="run:test",
        page_number=3,
        placement_index=1 if identifier.endswith("small") else 2,
        placement_ordinal=1 if identifier.endswith("small") else 2,
        bbox=bbox,
        page_width=1000,
        page_height=1000,
        attempt_ids=[f"attempt:{identifier}"],
        image_object_sha256=HASH,
        created_at=NOW,
    )


def _no_text(identifier: str, text_hash: str) -> ImageOcrResult:
    return ImageOcrResult(
        evidence_id=identifier,
        run_id="run:test",
        status=ImageOcrStatus.NO_TEXT,
        text_object_sha256=text_hash,
        engine_name="recorded",
        engine_version="1",
        reason_codes=["OCR_NO_TEXT"],
        created_at=NOW,
    )


def test_decorative_exclusion_is_strict_and_uncertain_visuals_remain_unknown(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    service = BookVisualService(
        state,
        objects,
        load_book_visual_distillation_config(
            PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
        ),
    )
    small = _evidence("image:small", (0.0, 0.0, 90.0, 90.0))
    uncertain = _evidence("image:uncertain", (0.0, 0.0, 150.0, 150.0))
    failed_cover = _evidence(
        "image:failed-cover",
        (0.0, 0.0, 800.0, 800.0),
    ).model_copy(update={"page_number": 1, "placement_index": 3, "placement_ordinal": 3})
    empty_text = objects.put_bytes(b"")
    run = BookVisualRun(
        run_id="run:test",
        source_manifest_id="manifest:test",
        source_id="source:test",
        source_snapshot_id="snapshot:test",
        raw_object_sha256=HASH,
        pipeline_version="pipeline",
        layout_version="layout",
        classification_version="classification",
        stage=BookVisualRunStage.OCR_COMPLETED,
        input_hashes=[HASH],
        source_page_count=3,
        image_page_count=1,
        image_placement_count=3,
        processed_placement_count=3,
        started_at=NOW,
        created_at=NOW,
    )
    units = service._classify(  # noqa: SLF001 - frozen-rule contract test
        [small, uncertain, failed_cover],
        [
            _no_text(small.evidence_id, empty_text.sha256),
            _no_text(uncertain.evidence_id, empty_text.sha256),
            ImageOcrResult(
                evidence_id=failed_cover.evidence_id,
                run_id="run:test",
                status=ImageOcrStatus.FAILED,
                engine_name="recorded",
                engine_version="1",
                reason_codes=["OCR_ENGINE_FAILED"],
                created_at=NOW,
            ),
        ],
        [],
        run,
    )
    assert units[0].chart_type is ChartUnitType.DECORATIVE
    assert units[0].decorative_excluded
    assert units[1].chart_type is ChartUnitType.UNKNOWN
    assert not units[1].decorative_excluded
    assert "UNKNOWN_CLASSIFICATION" in units[1].review_reason_codes
    assert units[2].chart_type is ChartUnitType.UNKNOWN
    assert "OCR_FAILED" in units[2].review_reason_codes
