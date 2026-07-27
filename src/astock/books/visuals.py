"""Placement-level private-book visual extraction, OCR, and audit service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pymupdf

from astock.books.repository import BookRepository
from astock.books.visual_repository import BookVisualRepository
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.ocr import OcrEngine, RapidOcrEngine
from astock.schemas import (
    ArgumentUnitStatus,
    BookLayoutAtom,
    BookLayoutAtomKind,
    BookSourceManifest,
    BookVisualCoverageReport,
    BookVisualCoverageStatus,
    BookVisualDistillationConfig,
    BookVisualPlan,
    BookVisualQualityStatus,
    BookVisualRun,
    BookVisualRunStage,
    ChartUnit,
    ChartUnitType,
    ImageEvidence,
    ImageEvidenceAttempt,
    ImageExtractionMode,
    ImageExtractionStatus,
    ImageOcrResult,
    ImageOcrStatus,
    ParagraphUnitKind,
)


@dataclass(frozen=True, slots=True)
class BookVisualExecution:
    run: BookVisualRun
    evidences: tuple[ImageEvidence, ...]
    attempts: tuple[ImageEvidenceAttempt, ...]
    ocr_results: tuple[ImageOcrResult, ...]
    layout_atoms: tuple[BookLayoutAtom, ...]
    chart_units: tuple[ChartUnit, ...]


@dataclass(frozen=True, slots=True)
class _EnumeratedLayout:
    evidences: list[ImageEvidence]
    attempts: list[ImageEvidenceAttempt]
    atoms: list[BookLayoutAtom]
    image_page_count: int


class BookVisualService:
    """Internal, resumable service; intentionally not exposed through the public CLI."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        config: BookVisualDistillationConfig,
        *,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.config = config
        self.books = BookRepository(state)
        self.repository = BookVisualRepository(state)
        self._ocr_engine = ocr_engine

    def plan(self, source_manifest_id: str) -> BookVisualPlan:
        manifest = self._manifest(source_manifest_id)
        pdf_bytes = self.object_store.get_bytes(manifest.raw_object_sha256)
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            source_pages = document.page_count
            self._validate_page_count(manifest.source_page_count, source_pages)
            image_pages = 0
            image_placements = 0
            for page in document:
                placements = page.get_image_info(xrefs=True, hashes=True)
                if placements:
                    image_pages += 1
                    image_placements += len(placements)
        return BookVisualPlan(
            source_manifest_id=manifest.manifest_id,
            run_id=self._run_id(manifest),
            source_pages=source_pages,
            image_pages=image_pages,
            image_placements=image_placements,
            input_hashes=self._input_hashes(manifest),
            created_at=manifest.created_at,
        )

    def run(self, source_manifest_id: str) -> BookVisualExecution:
        manifest = self._manifest(source_manifest_id)
        run_id = self._run_id(manifest)
        existing = self.repository.get_run(run_id)
        if existing is not None and existing.stage in {
            BookVisualRunStage.CHARTS_CLASSIFIED,
            BookVisualRunStage.SEMANTIC_MATERIALIZED,
            BookVisualRunStage.AUDITED,
        }:
            return self._execution(existing)
        if existing is not None and existing.stage is BookVisualRunStage.FAILED:
            raise ValueError("failed book visual runs are fail-closed")
        if existing is None:
            plan = self.plan(source_manifest_id)
            frozen = BookVisualRun(
                run_id=plan.run_id,
                source_manifest_id=manifest.manifest_id,
                source_id=manifest.source_id,
                source_snapshot_id=manifest.snapshot_id,
                raw_object_sha256=manifest.raw_object_sha256,
                pipeline_version=self.config.pipeline_version,
                layout_version=self.config.layout_version,
                classification_version=self.config.classification_version,
                stage=BookVisualRunStage.INPUT_FROZEN,
                input_hashes=plan.input_hashes,
                source_page_count=plan.source_pages,
                image_page_count=plan.image_pages,
                image_placement_count=plan.image_placements,
                processed_placement_count=0,
                started_at=manifest.created_at,
                created_at=manifest.created_at,
            )
            frozen = self._object_store_run(frozen)
            self.repository.save_run(frozen)
        else:
            self._verify_existing_run(existing, manifest)
            frozen = existing

        if _stage_before(frozen.stage, BookVisualRunStage.LAYOUT_ENUMERATED):
            pdf_bytes = self.object_store.get_bytes(manifest.raw_object_sha256)
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
                self._validate_page_count(
                    frozen.source_page_count,
                    document.page_count,
                )
                layout = self._enumerate_layout(document, frozen)
            if (
                layout.image_page_count != frozen.image_page_count
                or len(layout.evidences) != frozen.image_placement_count
            ):
                raise ValueError("book visual planned layout changed before registration")
            layout_run = self._object_store_run(
                frozen.model_copy(
                    update={
                        "stage": BookVisualRunStage.LAYOUT_ENUMERATED,
                        "image_page_count": layout.image_page_count,
                        "image_placement_count": len(layout.evidences),
                    }
                )
            )
            self.repository.register_layout(
                layout_run,
                layout.evidences,
                layout.attempts,
                layout.atoms,
            )
        else:
            layout_run = frozen
            layout = self._recover_layout(layout_run)

        if _stage_before(layout_run.stage, BookVisualRunStage.OCR_COMPLETED):
            ocr_results = self._run_ocr(layout.evidences, layout_run)
            ocr_run = self._object_store_run(
                layout_run.model_copy(
                    update={
                        "stage": BookVisualRunStage.OCR_COMPLETED,
                        "processed_placement_count": len(ocr_results),
                    }
                )
            )
            self.repository.register_ocr(ocr_run, ocr_results)
        else:
            ocr_run = layout_run
            ocr_results = self._recover_ocr(ocr_run, layout.evidences)
        chart_units = self._classify(
            layout.evidences,
            ocr_results,
            layout.atoms,
            ocr_run,
        )
        classified_run = self._object_store_run(
            ocr_run.model_copy(update={"stage": BookVisualRunStage.CHARTS_CLASSIFIED})
        )
        self.repository.register_charts(classified_run, chart_units)
        return BookVisualExecution(
            run=classified_run,
            evidences=tuple(layout.evidences),
            attempts=tuple(layout.attempts),
            ocr_results=tuple(ocr_results),
            layout_atoms=tuple(layout.atoms),
            chart_units=tuple(chart_units),
        )

    def status(self, source_manifest_id: str) -> dict[str, object]:
        run = self.repository.latest_run(source_manifest_id)
        if run is None:
            return {"status": "NOT_RUN", "run": None, "report": None}
        return {
            "status": run.stage.value,
            "run": run,
            "report": self.repository.report(run.run_id),
        }

    def audit(self, run_id: str) -> BookVisualCoverageReport:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.stage is BookVisualRunStage.AUDITED:
            report = self.repository.report(run_id)
            if report is None or report.report_object_sha256 is None:
                raise ValueError("audited book visual run is missing its report")
            self.object_store.get_bytes(report.report_object_sha256)
            return report
        if run.stage is not BookVisualRunStage.SEMANTIC_MATERIALIZED:
            raise ValueError("book visual audit requires semantic materialization")

        evidences = self.repository.evidences(run_id)
        ocr_results = self.repository.ocr_results(run_id)
        units = self.repository.chart_units(run_id)
        refs = self.repository.semantic_refs(run_id)
        if len(refs) != sum(not unit.decorative_excluded for unit in units):
            raise ValueError("book visual audit found incomplete semantic lineage")
        classifications = {
            unit_type: sum(unit.chart_type is unit_type for unit in units)
            for unit_type in ChartUnitType
        }
        ocr_failed = sum(result.status is ImageOcrStatus.FAILED for result in ocr_results)
        low_confidence = sum(
            result.status is ImageOcrStatus.LOW_CONFIDENCE for result in ocr_results
        )
        no_text = sum(result.status is ImageOcrStatus.NO_TEXT for result in ocr_results)
        complete = (
            len(evidences)
            == len(ocr_results)
            == len(units)
            == run.image_placement_count
            == run.processed_placement_count
        )
        coverage_status = (
            BookVisualCoverageStatus.COMPLETE
            if complete
            else BookVisualCoverageStatus.PARTIAL
        )
        review_required = (
            not complete
            or ocr_failed > 0
            or low_confidence > 0
            or no_text > 0
            or classifications[ChartUnitType.UNKNOWN] > 0
            or any(unit.review_reason_codes for unit in units)
        )
        quality_status = (
            BookVisualQualityStatus.REVIEW_REQUIRED
            if review_required
            else BookVisualQualityStatus.PASS
        )
        affected_argument_ids = {ref.argument_unit_id for ref in refs}
        image_only_ready = self._image_only_ready_count(
            run.semantic_run_id,
            affected_argument_ids,
        )
        audited_at = datetime.now(UTC)
        report = BookVisualCoverageReport(
            report_id=f"book-visual-coverage:{content_hash({'run_id':run_id,'stage':'AUDITED'})}",
            run_id=run_id,
            coverage_status=coverage_status,
            quality_status=quality_status,
            source_pages=run.source_page_count,
            image_pages=len({evidence.page_number for evidence in evidences}),
            image_placements=run.image_placement_count,
            processed_placements=len(units),
            ocr_failed=ocr_failed,
            low_confidence=low_confidence,
            no_text=no_text,
            duplicate=sum(
                evidence.duplicate_of_evidence_id is not None for evidence in evidences
            ),
            classification_counts=classifications,
            affected_argument_unit_count=len(affected_argument_ids),
            image_only_ready_candidate_count=image_only_ready,
            created_at=audited_at,
        )
        report_object = self.object_store.put_json(
            report.model_dump(mode="json", exclude={"report_object_sha256"})
        )
        report = report.model_copy(
            update={"report_object_sha256": report_object.sha256}
        )
        audited = self._object_store_run(
            run.model_copy(
                update={
                    "stage": BookVisualRunStage.AUDITED,
                    "coverage_report_object_sha256": report_object.sha256,
                    "finished_at": audited_at,
                }
            )
        )
        self.repository.register_audit(audited, report)
        return report

    def _enumerate_layout(
        self,
        document: pymupdf.Document,
        run: BookVisualRun,
    ) -> _EnumeratedLayout:
        evidences: list[ImageEvidence] = []
        attempts: list[ImageEvidenceAttempt] = []
        pending_atoms: list[
            tuple[int, tuple[float, float, int, int, int], BookLayoutAtom]
        ] = []
        duplicate_by_hash: dict[str, str] = {}
        placement_ordinal = 0
        image_pages = 0

        for page_index in range(document.page_count):
            page = document[page_index]
            page_number = page_index + 1
            infos = sorted(
                enumerate(
                    page.get_image_info(xrefs=True, hashes=True),
                    start=1,
                ),
                key=lambda entry: (*_placement_sort_key(entry[1]), entry[0]),
            )
            if infos:
                image_pages += 1
            text_blocks = self._native_text_blocks(page)
            for text_index, (bbox, text) in enumerate(text_blocks, start=1):
                text_object = self.object_store.put_bytes(text.encode("utf-8"))
                atom_id = "book-layout-atom:" + content_hash(
                    {
                        "run_id": run.run_id,
                        "page_number": page_number,
                        "kind": BookLayoutAtomKind.TEXT_BLOCK.value,
                        "bbox": bbox,
                        "text_object_sha256": text_object.sha256,
                        "text_index": text_index,
                    }
                )
                atom = BookLayoutAtom(
                    atom_id=atom_id,
                    run_id=run.run_id,
                    page_number=page_number,
                    page_ordinal=1,
                    global_ordinal=1,
                    atom_kind=BookLayoutAtomKind.TEXT_BLOCK,
                    bbox=bbox,
                    text_object_sha256=text_object.sha256,
                    created_at=run.started_at,
                )
                atom = self._object_store_atom(atom)
                pending_atoms.append(
                    (
                        page_number,
                        (bbox[1], bbox[0], 0, 0, text_index),
                        atom,
                    )
                )

            for placement_index, info in infos:
                placement_ordinal += 1
                bbox = _bbox(info["bbox"])
                raw_xref = int(info.get("xref") or 0)
                xref = raw_xref if raw_xref > 0 else None
                evidence_id = "image-evidence:" + content_hash(
                    {
                        "run_id": run.run_id,
                        "page_number": page_number,
                        "bbox": bbox,
                        "xref": xref,
                        "placement_index": placement_index,
                    }
                )
                placement_attempts, image_hash = self._extract_placement(
                    document,
                    page,
                    evidence_id,
                    xref,
                    bbox,
                    run,
                )
                attempts.extend(placement_attempts)
                duplicate = (
                    duplicate_by_hash.get(image_hash) if image_hash is not None else None
                )
                if image_hash is not None and duplicate is None:
                    duplicate_by_hash[image_hash] = evidence_id
                evidence = ImageEvidence(
                    evidence_id=evidence_id,
                    run_id=run.run_id,
                    page_number=page_number,
                    placement_index=placement_index,
                    placement_ordinal=placement_ordinal,
                    xref=xref,
                    bbox=bbox,
                    page_width=float(page.rect.width),
                    page_height=float(page.rect.height),
                    attempt_ids=[attempt.attempt_id for attempt in placement_attempts],
                    image_object_sha256=image_hash,
                    duplicate_of_evidence_id=duplicate,
                    created_at=run.started_at,
                )
                evidence = self._object_store_evidence(evidence)
                evidences.append(evidence)
                atom_id = "book-layout-atom:" + content_hash(
                    {
                        "run_id": run.run_id,
                        "page_number": page_number,
                        "kind": BookLayoutAtomKind.IMAGE_EVIDENCE.value,
                        "evidence_id": evidence_id,
                    }
                )
                atom = BookLayoutAtom(
                    atom_id=atom_id,
                    run_id=run.run_id,
                    page_number=page_number,
                    page_ordinal=1,
                    global_ordinal=1,
                    atom_kind=BookLayoutAtomKind.IMAGE_EVIDENCE,
                    bbox=bbox,
                    evidence_id=evidence_id,
                    created_at=run.started_at,
                )
                atom = self._object_store_atom(atom)
                pending_atoms.append(
                    (
                        page_number,
                        (
                            bbox[1],
                            bbox[0],
                            1,
                            xref or 0,
                            placement_index,
                        ),
                        atom,
                    )
                )

        atoms: list[BookLayoutAtom] = []
        global_ordinal = 0
        for page_number in range(1, document.page_count + 1):
            page_atoms = sorted(
                [entry for entry in pending_atoms if entry[0] == page_number],
                key=lambda entry: entry[1],
            )
            for page_ordinal, (_, _, atom) in enumerate(page_atoms, start=1):
                global_ordinal += 1
                finalized = atom.model_copy(
                    update={
                        "page_ordinal": page_ordinal,
                        "global_ordinal": global_ordinal,
                        "atom_object_sha256": None,
                    }
                )
                atoms.append(self._object_store_atom(finalized))
        return _EnumeratedLayout(
            evidences=evidences,
            attempts=attempts,
            atoms=atoms,
            image_page_count=image_pages,
        )

    def _extract_placement(
        self,
        document: pymupdf.Document,
        page: pymupdf.Page,
        evidence_id: str,
        xref: int | None,
        bbox: tuple[float, float, float, float],
        run: BookVisualRun,
    ) -> tuple[list[ImageEvidenceAttempt], str | None]:
        attempts: list[ImageEvidenceAttempt] = []
        if xref is not None:
            try:
                image_bytes = self._extract_xref_image(document, xref)
                if not image_bytes:
                    raise ValueError("empty xref image")
            except Exception:
                attempts.append(
                    self._attempt(
                        evidence_id,
                        len(attempts) + 1,
                        ImageExtractionMode.XREF_ORIGINAL,
                        None,
                        "XREF_EXTRACTION_FAILED",
                        run,
                    )
                )
            else:
                image = self.object_store.put_bytes(image_bytes)
                attempts.append(
                    self._attempt(
                        evidence_id,
                        len(attempts) + 1,
                        ImageExtractionMode.XREF_ORIGINAL,
                        image.sha256,
                        None,
                        run,
                    )
                )
                return attempts, image.sha256

        try:
            image_bytes = self._clip_image(page, bbox)
            if not image_bytes:
                raise ValueError("empty clipped image")
        except Exception:
            attempts.append(
                self._attempt(
                    evidence_id,
                    len(attempts) + 1,
                    ImageExtractionMode.BBOX_CLIP_300_DPI,
                    None,
                    "CLIP_EXTRACTION_FAILED",
                    run,
                )
            )
            return attempts, None
        image = self.object_store.put_bytes(image_bytes)
        attempts.append(
            self._attempt(
                evidence_id,
                len(attempts) + 1,
                ImageExtractionMode.BBOX_CLIP_300_DPI,
                image.sha256,
                None,
                run,
            )
        )
        return attempts, image.sha256

    def _extract_xref_image(
        self,
        document: pymupdf.Document,
        xref: int,
    ) -> bytes:
        return bytes(document.extract_image(xref)["image"])

    def _clip_image(
        self,
        page: pymupdf.Page,
        bbox: tuple[float, float, float, float],
    ) -> bytes:
        pixmap = page.get_pixmap(
            clip=pymupdf.Rect(bbox),
            dpi=self.config.clip_fallback_dpi,
            alpha=False,
        )
        return pixmap.tobytes("png")

    def _attempt(
        self,
        evidence_id: str,
        ordinal: int,
        mode: ImageExtractionMode,
        image_hash: str | None,
        error_code: str | None,
        run: BookVisualRun,
    ) -> ImageEvidenceAttempt:
        attempt_id = "image-evidence-attempt:" + content_hash(
            {
                "evidence_id": evidence_id,
                "attempt_ordinal": ordinal,
                "extraction_mode": mode.value,
            }
        )
        attempt = ImageEvidenceAttempt(
            attempt_id=attempt_id,
            evidence_id=evidence_id,
            attempt_ordinal=ordinal,
            extraction_mode=mode,
            status=(
                ImageExtractionStatus.SUCCESS
                if image_hash is not None
                else ImageExtractionStatus.FAILED
            ),
            image_object_sha256=image_hash,
            error_code=error_code,
            created_at=run.started_at,
        )
        stored = self.object_store.put_json(
            attempt.model_dump(mode="json", exclude={"attempt_object_sha256"})
        )
        return attempt.model_copy(update={"attempt_object_sha256": stored.sha256})

    def _run_ocr(
        self,
        evidences: list[ImageEvidence],
        run: BookVisualRun,
    ) -> list[ImageOcrResult]:
        engine = self._get_ocr_engine()
        results: list[ImageOcrResult] = []
        for evidence in evidences:
            if evidence.image_object_sha256 is None:
                result = ImageOcrResult(
                    evidence_id=evidence.evidence_id,
                    run_id=run.run_id,
                    status=ImageOcrStatus.FAILED,
                    engine_name=engine.name,
                    engine_version=engine.version,
                    reason_codes=["IMAGE_EXTRACTION_FAILED", "OCR_NOT_EXECUTABLE"],
                    created_at=run.started_at,
                )
            else:
                image_bytes = self.object_store.get_bytes(evidence.image_object_sha256)
                try:
                    raw = engine.recognize(image_bytes)
                except Exception:
                    result = ImageOcrResult(
                        evidence_id=evidence.evidence_id,
                        run_id=run.run_id,
                        status=ImageOcrStatus.FAILED,
                        engine_name=engine.name,
                        engine_version=engine.version,
                        reason_codes=["OCR_ENGINE_FAILED"],
                        created_at=run.started_at,
                    )
                else:
                    text = _normalize_text(raw.text)
                    text_object = self.object_store.put_bytes(text.encode("utf-8"))
                    visible_chars = _visible_char_count(text)
                    if visible_chars < self.config.minimum_visible_ocr_chars:
                        status = ImageOcrStatus.NO_TEXT
                        reasons = ["OCR_NO_TEXT"]
                    elif (
                        raw.average_confidence is None
                        or raw.average_confidence < self.config.low_confidence_threshold
                    ):
                        status = ImageOcrStatus.LOW_CONFIDENCE
                        reasons = ["OCR_LOW_CONFIDENCE"]
                    else:
                        status = ImageOcrStatus.SUCCESS
                        reasons = ["OCR_COMPLETED"]
                    result = ImageOcrResult(
                        evidence_id=evidence.evidence_id,
                        run_id=run.run_id,
                        status=status,
                        text_object_sha256=text_object.sha256,
                        average_confidence=raw.average_confidence,
                        engine_name=engine.name,
                        engine_version=engine.version,
                        reason_codes=reasons,
                        created_at=run.started_at,
                    )
            stored = self.object_store.put_json(
                result.model_dump(mode="json", exclude={"result_object_sha256"})
            )
            results.append(
                result.model_copy(update={"result_object_sha256": stored.sha256})
            )
        return results

    def _classify(
        self,
        evidences: list[ImageEvidence],
        ocr_results: list[ImageOcrResult],
        atoms: list[BookLayoutAtom],
        run: BookVisualRun,
    ) -> list[ChartUnit]:
        result_by_evidence = {result.evidence_id: result for result in ocr_results}
        text_atoms_by_page: dict[int, list[tuple[BookLayoutAtom, str]]] = {}
        for atom in atoms:
            if (
                atom.atom_kind is BookLayoutAtomKind.TEXT_BLOCK
                and atom.text_object_sha256 is not None
            ):
                text_atoms_by_page.setdefault(atom.page_number, []).append(
                    (
                        atom,
                        self.object_store.get_bytes(atom.text_object_sha256).decode("utf-8"),
                    )
                )
        units: list[ChartUnit] = []
        for evidence in evidences:
            result = result_by_evidence[evidence.evidence_id]
            ocr_text = (
                self.object_store.get_bytes(result.text_object_sha256).decode("utf-8")
                if result.text_object_sha256 is not None
                else ""
            )
            nearby = [
                text
                for atom, text in text_atoms_by_page.get(evidence.page_number, [])
                if _vertical_distance(atom.bbox, evidence.bbox)
                <= self.config.caption_margin_points
            ]
            caption_present = any(_caption_signal(text) for text in nearby)
            combined = "\n".join([ocr_text, *nearby]).casefold()
            area_ratio = _area_ratio(evidence)
            method_or_chart_signal = _has_any(
                combined,
                [
                    *self.config.method_signal_terms,
                    *self.config.chart_terms,
                    *self.config.table_terms,
                    *self.config.diagram_terms,
                ],
            )
            strict_cover = (
                evidence.page_number <= 2
                and area_ratio >= self.config.cover_minimum_area_ratio
                and result.status is ImageOcrStatus.SUCCESS
                and not method_or_chart_signal
            )
            strict_small = (
                area_ratio < self.config.decorative_maximum_area_ratio
                and result.status is ImageOcrStatus.NO_TEXT
                and _visible_char_count(ocr_text)
                < self.config.minimum_visible_ocr_chars
                and not caption_present
            )
            if strict_cover or strict_small:
                chart_type = ChartUnitType.DECORATIVE
                confidence = 0.98
                reasons: list[str] = []
            elif _has_any(combined, self.config.table_terms):
                chart_type = ChartUnitType.TABLE
                confidence = 0.88
                reasons = []
            elif _has_any(combined, self.config.chart_terms):
                chart_type = ChartUnitType.CHART
                confidence = 0.88
                reasons = []
            elif _has_any(combined, self.config.diagram_terms):
                chart_type = ChartUnitType.DIAGRAM
                confidence = 0.84
                reasons = []
            elif _visible_char_count(ocr_text) >= self.config.minimum_visible_ocr_chars:
                chart_type = ChartUnitType.TEXT_IMAGE
                confidence = 0.72
                reasons = []
            else:
                chart_type = ChartUnitType.UNKNOWN
                confidence = 0.0
                reasons = ["UNKNOWN_CLASSIFICATION"]
            if chart_type is not ChartUnitType.DECORATIVE:
                if result.status is ImageOcrStatus.FAILED:
                    reasons.append("OCR_FAILED")
                elif result.status is ImageOcrStatus.LOW_CONFIDENCE:
                    reasons.append("OCR_LOW_CONFIDENCE")
                elif result.status is ImageOcrStatus.NO_TEXT:
                    reasons.append("OCR_NO_TEXT")
            unit = ChartUnit(
                chart_unit_id="chart-unit:"
                + content_hash(
                    {
                        "run_id": run.run_id,
                        "evidence_id": evidence.evidence_id,
                        "classification_version": self.config.classification_version,
                    }
                ),
                run_id=run.run_id,
                evidence_id=evidence.evidence_id,
                chart_type=chart_type,
                classification_confidence=confidence,
                decorative_excluded=chart_type is ChartUnitType.DECORATIVE,
                caption_present=caption_present,
                review_reason_codes=sorted(set(reasons)),
                created_at=run.started_at,
            )
            stored = self.object_store.put_json(
                unit.model_dump(mode="json", exclude={"unit_object_sha256"})
            )
            units.append(unit.model_copy(update={"unit_object_sha256": stored.sha256}))
        return units

    def _native_text_blocks(
        self,
        page: pymupdf.Page,
    ) -> list[tuple[tuple[float, float, float, float], str]]:
        blocks: list[tuple[tuple[float, float, float, float], str]] = []
        for raw in page.get_text("blocks"):
            if len(raw) > 6 and int(raw[6]) != 0:
                continue
            bbox = _bbox(raw[:4])
            text = _normalize_text(str(raw[4]))
            if _visible_char_count(text) == 0:
                continue
            blocks.append((bbox, text))
        return sorted(blocks, key=lambda item: (item[0][1], item[0][0], item[0][3], item[0][2]))

    def _object_store_run(self, run: BookVisualRun) -> BookVisualRun:
        base = run.model_copy(update={"run_object_sha256": None})
        stored = self.object_store.put_json(
            base.model_dump(mode="json", exclude={"run_object_sha256"})
        )
        return base.model_copy(update={"run_object_sha256": stored.sha256})

    def _object_store_evidence(self, evidence: ImageEvidence) -> ImageEvidence:
        stored = self.object_store.put_json(
            evidence.model_dump(mode="json", exclude={"evidence_object_sha256"})
        )
        return evidence.model_copy(update={"evidence_object_sha256": stored.sha256})

    def _object_store_atom(self, atom: BookLayoutAtom) -> BookLayoutAtom:
        base = atom.model_copy(update={"atom_object_sha256": None})
        stored = self.object_store.put_json(
            base.model_dump(mode="json", exclude={"atom_object_sha256"})
        )
        return base.model_copy(update={"atom_object_sha256": stored.sha256})

    def _manifest(self, source_manifest_id: str) -> BookSourceManifest:
        manifest = self.books.get_manifest(source_manifest_id)
        if manifest is None:
            raise ValueError(f"unknown private-book manifest: {source_manifest_id}")
        if not self.object_store.verify(manifest.raw_object_sha256):
            raise ValueError("private-book raw object is unavailable or corrupt")
        return manifest

    def _run_id(self, manifest: BookSourceManifest) -> str:
        return "book-visual-run:" + content_hash(
            {
                "source_manifest_id": manifest.manifest_id,
                "source_snapshot_id": manifest.snapshot_id,
                "raw_object_sha256": manifest.raw_object_sha256,
                "pipeline_version": self.config.pipeline_version,
                "layout_version": self.config.layout_version,
                "classification_version": self.config.classification_version,
            }
        )

    def _input_hashes(self, manifest: BookSourceManifest) -> list[str]:
        return sorted(
            {
                manifest.raw_object_sha256,
                manifest.snapshot_id,
                content_hash(self.config.model_dump(mode="json")),
            }
        )

    def _validate_page_count(self, declared: int, actual: int) -> None:
        if declared != actual:
            raise ValueError(
                f"private-book manifest page count mismatch: declared={declared}, actual={actual}"
            )

    def _get_ocr_engine(self) -> OcrEngine:
        if self._ocr_engine is None:
            self._ocr_engine = RapidOcrEngine()
        return self._ocr_engine

    def _execution(self, run: BookVisualRun) -> BookVisualExecution:
        layout = self._recover_layout(run)
        ocr_results = self._recover_ocr(run, layout.evidences)
        chart_units = self._recover_charts(run, layout.evidences)
        return BookVisualExecution(
            run=run,
            evidences=tuple(layout.evidences),
            attempts=tuple(layout.attempts),
            ocr_results=tuple(ocr_results),
            layout_atoms=tuple(layout.atoms),
            chart_units=tuple(chart_units),
        )

    def _recover_layout(self, run: BookVisualRun) -> _EnumeratedLayout:
        evidences = self.repository.evidences(run.run_id)
        attempts = self.repository.attempts(run.run_id)
        atoms = self.repository.layout_atoms(run.run_id)
        if len(evidences) != run.image_placement_count:
            raise ValueError("persisted book visual layout is incomplete")
        if len({evidence.page_number for evidence in evidences}) != run.image_page_count:
            raise ValueError("persisted book visual image-page count changed")
        attempts_by_evidence: dict[str, list[ImageEvidenceAttempt]] = {}
        for attempt in attempts:
            attempts_by_evidence.setdefault(attempt.evidence_id, []).append(attempt)
            self._verify_model_object(attempt, "attempt_object_sha256")
            if attempt.image_object_sha256 is not None:
                self.object_store.get_bytes(attempt.image_object_sha256)
        evidence_ids = {evidence.evidence_id for evidence in evidences}
        for evidence in evidences:
            self._verify_model_object(evidence, "evidence_object_sha256")
            if evidence.image_object_sha256 is not None:
                self.object_store.get_bytes(evidence.image_object_sha256)
            if evidence.attempt_ids != [
                attempt.attempt_id
                for attempt in attempts_by_evidence.get(evidence.evidence_id, [])
            ]:
                raise ValueError("persisted image evidence attempt lineage changed")
        image_atom_ids = [
            atom.evidence_id for atom in atoms if atom.evidence_id is not None
        ]
        if len(image_atom_ids) != len(evidence_ids) or set(image_atom_ids) != evidence_ids:
            raise ValueError("persisted layout atoms do not cover every image placement")
        for atom in atoms:
            self._verify_model_object(atom, "atom_object_sha256")
            if atom.text_object_sha256 is not None:
                self.object_store.get_bytes(atom.text_object_sha256)
        return _EnumeratedLayout(
            evidences=evidences,
            attempts=attempts,
            atoms=atoms,
            image_page_count=run.image_page_count,
        )

    def _recover_ocr(
        self,
        run: BookVisualRun,
        evidences: list[ImageEvidence],
    ) -> list[ImageOcrResult]:
        results = self.repository.ocr_results(run.run_id)
        by_evidence = {result.evidence_id: result for result in results}
        if (
            len(results) != run.processed_placement_count
            or len(by_evidence) != len(evidences)
            or set(by_evidence)
            != {evidence.evidence_id for evidence in evidences}
        ):
            raise ValueError("persisted OCR results do not cover every image placement")
        ordered = [by_evidence[evidence.evidence_id] for evidence in evidences]
        for result in ordered:
            self._verify_model_object(result, "result_object_sha256")
            if result.text_object_sha256 is not None:
                self.object_store.get_bytes(result.text_object_sha256)
        return ordered

    def _recover_charts(
        self,
        run: BookVisualRun,
        evidences: list[ImageEvidence],
    ) -> list[ChartUnit]:
        units = self.repository.chart_units(run.run_id)
        by_evidence = {unit.evidence_id: unit for unit in units}
        if (
            len(units) != run.processed_placement_count
            or len(by_evidence) != len(evidences)
            or set(by_evidence)
            != {evidence.evidence_id for evidence in evidences}
        ):
            raise ValueError("persisted chart units do not cover every image placement")
        ordered = [by_evidence[evidence.evidence_id] for evidence in evidences]
        for unit in ordered:
            self._verify_model_object(unit, "unit_object_sha256")
        return ordered

    def _verify_model_object(self, model: object, hash_field: str) -> None:
        object_hash = getattr(model, hash_field)
        if not isinstance(object_hash, str):
            raise ValueError(f"persisted {type(model).__name__} has no object hash")
        payload = model.model_dump(mode="json", exclude={hash_field})  # type: ignore[attr-defined]
        if self.object_store.get_bytes(object_hash) != canonical_json_bytes(payload):
            raise ValueError(f"persisted {type(model).__name__} object changed")

    def _verify_existing_run(
        self,
        run: BookVisualRun,
        manifest: BookSourceManifest,
    ) -> None:
        expected_inputs = self._input_hashes(manifest)
        if (
            run.source_manifest_id != manifest.manifest_id
            or run.source_id != manifest.source_id
            or run.source_snapshot_id != manifest.snapshot_id
            or run.raw_object_sha256 != manifest.raw_object_sha256
            or run.pipeline_version != self.config.pipeline_version
            or run.layout_version != self.config.layout_version
            or run.classification_version != self.config.classification_version
            or run.input_hashes != expected_inputs
        ):
            raise ValueError("persisted book visual run input contract changed")
        self._verify_model_object(run, "run_object_sha256")

    def _image_only_ready_count(
        self,
        semantic_run_id: str | None,
        argument_ids: set[str],
    ) -> int:
        if semantic_run_id is None or not argument_ids:
            return 0
        from astock.knowledge.semantic_repository import SemanticFunnelRepository

        semantic = SemanticFunnelRepository(self.state)
        arguments = {
            argument.argument_unit_id: argument
            for argument in semantic.argument_units(semantic_run_id)
        }
        paragraphs = {
            paragraph.paragraph_id: paragraph
            for group in semantic.paragraph_groups(semantic_run_id).values()
            for paragraph in group
        }
        return sum(
            argument.status is ArgumentUnitStatus.READY
            and all(
                paragraphs[paragraph_id].paragraph_kind
                is ParagraphUnitKind.VISUAL_EVIDENCE
                for paragraph_id in argument.paragraph_ids
            )
            for argument_id in argument_ids
            if (argument := arguments.get(argument_id)) is not None
        )


def _stage_before(current: BookVisualRunStage, target: BookVisualRunStage) -> bool:
    order = {
        BookVisualRunStage.INPUT_FROZEN: 0,
        BookVisualRunStage.LAYOUT_ENUMERATED: 1,
        BookVisualRunStage.OCR_COMPLETED: 2,
        BookVisualRunStage.CHARTS_CLASSIFIED: 3,
        BookVisualRunStage.SEMANTIC_MATERIALIZED: 4,
        BookVisualRunStage.AUDITED: 5,
        BookVisualRunStage.FAILED: 99,
    }
    return order[current] < order[target]


def _placement_sort_key(info: dict[str, Any]) -> tuple[float, float, int]:
    bbox = _bbox(info["bbox"])
    return (bbox[1], bbox[0], int(info.get("xref") or 0))


def _bbox(raw: Any) -> tuple[float, float, float, float]:
    values = tuple(round(float(value), 6) for value in raw)
    if len(values) != 4:
        raise ValueError("PDF bbox must contain four coordinates")
    return values  # type: ignore[return-value]


def _normalize_text(text: str) -> str:
    return "\n".join(
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if line.strip()
    ).strip()


def _visible_char_count(text: str) -> int:
    return sum(not char.isspace() for char in text)


def _caption_signal(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    return any(
        token in folded
        for token in ("figure", "fig.", "table", "chart", "图", "表")
    )


def _vertical_distance(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    if left[3] < right[1]:
        return right[1] - left[3]
    if right[3] < left[1]:
        return left[1] - right[3]
    return 0.0


def _area_ratio(evidence: ImageEvidence) -> float:
    x0, y0, x1, y1 = evidence.bbox
    return ((x1 - x0) * (y1 - y0)) / (evidence.page_width * evidence.page_height)


def _has_any(text: str, terms: list[str]) -> bool:
    return any(term.casefold() in text for term in terms)


__all__ = ["BookVisualExecution", "BookVisualService"]
