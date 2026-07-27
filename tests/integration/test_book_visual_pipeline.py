from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest

from astock.books import PrivatePdfIngestService
from astock.books.visual_repository import BookVisualRepository
from astock.books.visual_semantics import load_book_visual_distillation_config
from astock.books.visuals import BookVisualService
from astock.core.object_store import ObjectStore
from astock.documents.ocr import OcrResult
from astock.schemas import (
    BookLayoutAtomKind,
    BookVisualRunStage,
    ImageExtractionMode,
    ImageExtractionStatus,
    ImageOcrStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SequenceOcr:
    results: list[OcrResult | Exception]
    name: str = "recorded-ocr"
    version: str = "1"
    calls: int = 0

    def recognize(self, image_bytes: bytes) -> OcrResult:
        assert image_bytes
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def _chart_png() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=240, height=100)
    page.draw_line((20, 80), (80, 50), color=(0, 0, 0), width=2)
    page.draw_line((80, 50), (150, 20), color=(0, 0, 0), width=2)
    page.insert_text((20, 95), "profit chart", fontsize=10)
    data = page.get_pixmap(alpha=False).tobytes("png")
    document.close()
    return data


def _private_pdf(path: Path, *, placements: int) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(
        pymupdf.Rect(50, 40, 550, 85),
        "Claim: profit trend supports the method.",
        fontsize=12,
    )
    image = _chart_png()
    xref = page.insert_image(pymupdf.Rect(80, 110, 500, 260), stream=image)
    if placements >= 2:
        page.insert_image(pymupdf.Rect(80, 300, 500, 450), xref=xref)
    for index in range(2, placements):
        top = 470 + (index - 2) * 45
        page.insert_image(pymupdf.Rect(80, top, 220, top + 35), stream=image)
    page.insert_textbox(
        pymupdf.Rect(50, 650, 550, 710),
        "Therefore the profit method conclusion follows.",
        fontsize=12,
    )
    path.write_bytes(document.tobytes())
    document.close()


def _manifest(
    tmp_path: Path,
    state,
    objects: ObjectStore,
    *,
    placements: int,
    suffix: str,
) -> str:
    path = tmp_path / f"book-{suffix}.pdf"
    _private_pdf(path, placements=placements)
    result = PrivatePdfIngestService(objects, state).ingest(
        path,
        source_id=f"book:test:{suffix}",
        display_name=f"Book fixture {suffix}",
        author_source_id="author:test-book",
        file_version="v1",
        ocr_enabled=False,
    )
    return result.manifest.manifest_id


def _service(state, objects: ObjectStore, engine) -> BookVisualService:
    return BookVisualService(
        state,
        objects,
        load_book_visual_distillation_config(
            PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
        ),
        ocr_engine=engine,
    )


def test_native_text_does_not_skip_placement_ocr_and_same_xref_is_not_collapsed(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    manifest_id = _manifest(
        tmp_path,
        state,
        objects,
        placements=2,
        suffix="same-xref",
    )
    engine = SequenceOcr(
        [
            OcrResult("profit chart trend", 0.96),
            OcrResult("profit chart trend", 0.96),
        ]
    )
    service = _service(state, objects, engine)
    execution = service.run(manifest_id)

    assert execution.run.stage is BookVisualRunStage.CHARTS_CLASSIFIED
    assert execution.run.image_placement_count == 2
    assert execution.run.processed_placement_count == 2
    assert engine.calls == 2
    assert len({evidence.xref for evidence in execution.evidences}) == 1
    assert len({evidence.evidence_id for evidence in execution.evidences}) == 2
    assert sum(
        evidence.duplicate_of_evidence_id is not None
        for evidence in execution.evidences
    ) == 1
    assert all(
        attempt.extraction_mode is ImageExtractionMode.XREF_ORIGINAL
        and attempt.status is ImageExtractionStatus.SUCCESS
        for attempt in execution.attempts
    )
    assert [atom.atom_kind for atom in execution.layout_atoms] == [
        BookLayoutAtomKind.TEXT_BLOCK,
        BookLayoutAtomKind.IMAGE_EVIDENCE,
        BookLayoutAtomKind.IMAGE_EVIDENCE,
        BookLayoutAtomKind.TEXT_BLOCK,
    ]

    repeated = service.run(manifest_id)
    assert repeated.run == execution.run
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM book_image_evidence WHERE run_id=?",
            (execution.run.run_id,),
        ).fetchone()[0] == 2
        sqlite_text = "\n".join(
            str(row[0])
            for table, column in (
                ("book_visual_run", "run_json"),
                ("book_image_evidence", "evidence_json"),
                ("book_image_evidence_attempt", "attempt_json"),
                ("book_image_ocr", "result_json"),
                ("book_layout_atom", "atom_json"),
                ("book_chart_unit", "unit_json"),
            )
            for row in connection.execute(f"SELECT {column} FROM {table}").fetchall()
        )
    assert "Claim: profit" not in sqlite_text
    assert "profit chart trend" not in sqlite_text
    assert str(tmp_path) not in sqlite_text


class _FallbackService(BookVisualService):
    def _extract_xref_image(
        self,
        document: pymupdf.Document,
        xref: int,
    ) -> bytes:
        raise RuntimeError("recorded xref extraction failure")


def test_xref_failure_uses_300_dpi_clip_fallback(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    manifest_id = _manifest(
        tmp_path,
        state,
        objects,
        placements=1,
        suffix="clip-fallback",
    )
    config = load_book_visual_distillation_config(
        PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
    )
    service = _FallbackService(
        state,
        objects,
        config,
        ocr_engine=SequenceOcr([OcrResult("profit chart", 0.95)]),
    )
    execution = service.run(manifest_id)
    assert [attempt.extraction_mode for attempt in execution.attempts] == [
        ImageExtractionMode.XREF_ORIGINAL,
        ImageExtractionMode.BBOX_CLIP_300_DPI,
    ]
    assert [attempt.status for attempt in execution.attempts] == [
        ImageExtractionStatus.FAILED,
        ImageExtractionStatus.SUCCESS,
    ]
    assert config.clip_fallback_dpi == 300


def test_ocr_success_failure_low_confidence_and_no_text_are_distinct(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    manifest_id = _manifest(
        tmp_path,
        state,
        objects,
        placements=4,
        suffix="ocr-states",
    )
    engine = SequenceOcr(
        [
            OcrResult("profit chart", 0.96),
            RuntimeError("recorded OCR failure"),
            OcrResult("profit chart", 0.40),
            OcrResult("", None),
        ]
    )
    execution = _service(state, objects, engine).run(manifest_id)
    assert [result.status for result in execution.ocr_results] == [
        ImageOcrStatus.SUCCESS,
        ImageOcrStatus.FAILED,
        ImageOcrStatus.LOW_CONFIDENCE,
        ImageOcrStatus.NO_TEXT,
    ]


class _CrashAfterLayoutService(BookVisualService):
    def _run_ocr(self, evidences, run):
        raise RuntimeError("recorded crash after atomic layout registration")


def test_crash_after_layout_resumes_without_duplicate_rows(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    manifest_id = _manifest(
        tmp_path,
        state,
        objects,
        placements=1,
        suffix="resume",
    )
    config = load_book_visual_distillation_config(
        PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
    )
    crashing = _CrashAfterLayoutService(
        state,
        objects,
        config,
        ocr_engine=SequenceOcr([]),
    )
    with pytest.raises(RuntimeError, match="recorded crash"):
        crashing.run(manifest_id)
    repository = BookVisualRepository(state)
    interrupted = repository.latest_run(manifest_id)
    assert interrupted is not None
    assert interrupted.stage is BookVisualRunStage.LAYOUT_ENUMERATED

    resumed = _service(
        state,
        objects,
        SequenceOcr([OcrResult("profit chart", 0.95)]),
    ).run(manifest_id)
    assert resumed.run.stage is BookVisualRunStage.CHARTS_CLASSIFIED
    assert len(repository.evidences(resumed.run.run_id)) == 1
    assert len(repository.ocr_results(resumed.run.run_id)) == 1


class _CrashAfterOcrService(BookVisualService):
    def _classify(self, evidences, ocr_results, atoms, run):
        raise RuntimeError("recorded crash after atomic OCR registration")


def test_ocr_completed_resume_reuses_persisted_results_without_calling_engine(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    manifest_id = _manifest(
        tmp_path,
        state,
        objects,
        placements=1,
        suffix="resume-after-ocr",
    )
    config = load_book_visual_distillation_config(
        PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
    )
    first_engine = SequenceOcr([OcrResult("profit chart", 0.97)])
    crashing = _CrashAfterOcrService(
        state,
        objects,
        config,
        ocr_engine=first_engine,
    )
    with pytest.raises(RuntimeError, match="after atomic OCR"):
        crashing.run(manifest_id)
    repository = BookVisualRepository(state)
    interrupted = repository.latest_run(manifest_id)
    assert interrupted is not None
    assert interrupted.stage is BookVisualRunStage.OCR_COMPLETED
    before_ocr = repository.ocr_results(interrupted.run_id)
    assert before_ocr[0].status is ImageOcrStatus.SUCCESS
    before_hashes = {
        "evidence": [
            evidence.evidence_object_sha256
            for evidence in repository.evidences(interrupted.run_id)
        ],
        "attempt": [
            attempt.attempt_object_sha256
            for attempt in repository.attempts(interrupted.run_id)
        ],
        "layout": [
            atom.atom_object_sha256
            for atom in repository.layout_atoms(interrupted.run_id)
        ],
        "ocr_result": [result.result_object_sha256 for result in before_ocr],
        "ocr_text": [result.text_object_sha256 for result in before_ocr],
    }

    failing_engine = SequenceOcr([RuntimeError("must never be called")])
    resumed = BookVisualService(
        state,
        objects,
        config,
        ocr_engine=failing_engine,
    ).run(manifest_id)
    assert failing_engine.calls == 0
    assert resumed.ocr_results == tuple(before_ocr)
    assert resumed.ocr_results[0].status is ImageOcrStatus.SUCCESS
    assert "OCR_FAILED" not in resumed.chart_units[0].review_reason_codes
    persisted_ocr = repository.ocr_results(interrupted.run_id)
    assert persisted_ocr == before_ocr
    after_hashes = {
        "evidence": [
            evidence.evidence_object_sha256
            for evidence in repository.evidences(interrupted.run_id)
        ],
        "attempt": [
            attempt.attempt_object_sha256
            for attempt in repository.attempts(interrupted.run_id)
        ],
        "layout": [
            atom.atom_object_sha256
            for atom in repository.layout_atoms(interrupted.run_id)
        ],
        "ocr_result": [result.result_object_sha256 for result in persisted_ocr],
        "ocr_text": [result.text_object_sha256 for result in persisted_ocr],
    }
    assert after_hashes == before_hashes
