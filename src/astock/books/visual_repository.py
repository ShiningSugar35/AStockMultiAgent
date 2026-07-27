"""Private-safe SQLite metadata repository for book visual semantics."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas import (
    BookLayoutAtom,
    BookVisualCoverageReport,
    BookVisualRun,
    BookVisualRunStage,
    BookVisualSemanticRef,
    ChartUnit,
    ImageEvidence,
    ImageEvidenceAttempt,
    ImageOcrResult,
)

_STAGE_ORDER = {
    BookVisualRunStage.INPUT_FROZEN: 0,
    BookVisualRunStage.LAYOUT_ENUMERATED: 1,
    BookVisualRunStage.OCR_COMPLETED: 2,
    BookVisualRunStage.CHARTS_CLASSIFIED: 3,
    BookVisualRunStage.SEMANTIC_MATERIALIZED: 4,
    BookVisualRunStage.AUDITED: 5,
    BookVisualRunStage.FAILED: 99,
}


class BookVisualRepository:
    """Persist hashes, coordinates, status, confidence, and lineage only."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_run(self, run_id: str) -> BookVisualRun | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM book_visual_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return BookVisualRun.model_validate_json(row["run_json"]) if row else None

    def latest_run(self, source_manifest_id: str) -> BookVisualRun | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM book_visual_run WHERE source_manifest_id=? "
                "ORDER BY started_at DESC,run_id DESC LIMIT 1",
                (source_manifest_id,),
            ).fetchone()
        return BookVisualRun.model_validate_json(row["run_json"]) if row else None

    def save_run(self, run: BookVisualRun) -> BookVisualRun:
        with self.state.transaction() as connection:
            return self._save_run_row(connection, run)

    def register_layout(
        self,
        run: BookVisualRun,
        evidences: Sequence[ImageEvidence],
        attempts: Sequence[ImageEvidenceAttempt],
        atoms: Sequence[BookLayoutAtom],
    ) -> None:
        if run.stage is not BookVisualRunStage.LAYOUT_ENUMERATED:
            raise ValueError("layout registration requires LAYOUT_ENUMERATED")
        evidence_list = list(evidences)
        attempt_list = list(attempts)
        atom_list = list(atoms)
        _validate_layout_projection(run, evidence_list, attempt_list, atom_list)
        with self.state.transaction() as connection:
            for evidence in evidence_list:
                self._insert_evidence(connection, evidence)
            for attempt in attempt_list:
                self._insert_attempt(connection, attempt)
            for atom in atom_list:
                self._insert_layout_atom(connection, atom)
            self._save_run_row(connection, run)

    def register_ocr(
        self,
        run: BookVisualRun,
        results: Sequence[ImageOcrResult],
    ) -> None:
        if run.stage is not BookVisualRunStage.OCR_COMPLETED:
            raise ValueError("OCR registration requires OCR_COMPLETED")
        result_list = list(results)
        if len(result_list) != run.processed_placement_count:
            raise ValueError("OCR results must cover every processed placement")
        with self.state.transaction() as connection:
            expected_evidence_ids = {
                str(row["evidence_id"])
                for row in connection.execute(
                    "SELECT evidence_id FROM book_image_evidence WHERE run_id=?",
                    (run.run_id,),
                ).fetchall()
            }
            if {result.evidence_id for result in result_list} != expected_evidence_ids:
                raise ValueError("OCR results do not match enumerated placements")
            for result in result_list:
                if result.run_id != run.run_id:
                    raise ValueError("OCR result belongs to another run")
                self._insert_ocr(connection, result)
            self._save_run_row(connection, run)

    def register_charts(
        self,
        run: BookVisualRun,
        units: Sequence[ChartUnit],
    ) -> None:
        if run.stage is not BookVisualRunStage.CHARTS_CLASSIFIED:
            raise ValueError("chart registration requires CHARTS_CLASSIFIED")
        unit_list = list(units)
        if len(unit_list) != run.processed_placement_count:
            raise ValueError("chart units must partition processed placements")
        with self.state.transaction() as connection:
            expected_evidence_ids = {
                str(row["evidence_id"])
                for row in connection.execute(
                    "SELECT evidence_id FROM book_image_ocr WHERE run_id=?",
                    (run.run_id,),
                ).fetchall()
            }
            if {unit.evidence_id for unit in unit_list} != expected_evidence_ids:
                raise ValueError("chart units do not match completed OCR placements")
            for unit in unit_list:
                if unit.run_id != run.run_id:
                    raise ValueError("chart unit belongs to another run")
                self._insert_chart(connection, unit)
            self._save_run_row(connection, run)

    def register_semantic_refs(
        self,
        run: BookVisualRun,
        refs: Sequence[BookVisualSemanticRef],
    ) -> None:
        if run.stage is not BookVisualRunStage.SEMANTIC_MATERIALIZED:
            raise ValueError("semantic ref registration requires SEMANTIC_MATERIALIZED")
        if run.semantic_run_id is None:
            raise ValueError("semantic run id is missing")
        ref_list = list(refs)
        with self.state.transaction() as connection:
            for ref in ref_list:
                if ref.run_id != run.run_id or ref.semantic_run_id != run.semantic_run_id:
                    raise ValueError("book visual semantic ref provenance mismatch")
                self._insert_semantic_ref(connection, ref)
            expected = connection.execute(
                "SELECT COUNT(*) FROM book_chart_unit WHERE run_id=? "
                "AND decorative_excluded=0",
                (run.run_id,),
            ).fetchone()[0]
            actual = connection.execute(
                "SELECT COUNT(*) FROM book_visual_semantic_ref WHERE run_id=?",
                (run.run_id,),
            ).fetchone()[0]
            if int(actual) != int(expected):
                raise ValueError("every non-decorative visual requires semantic lineage")
            self._save_run_row(connection, run)

    def register_audit(
        self,
        run: BookVisualRun,
        report: BookVisualCoverageReport,
    ) -> None:
        if run.stage is not BookVisualRunStage.AUDITED:
            raise ValueError("audit registration requires AUDITED")
        if report.run_id != run.run_id:
            raise ValueError("coverage report belongs to another run")
        with self.state.transaction() as connection:
            self._insert_report(connection, report)
            self._save_run_row(connection, run)

    def evidences(self, run_id: str) -> list[ImageEvidence]:
        return self._models(
            "SELECT evidence_json FROM book_image_evidence WHERE run_id=? "
            "ORDER BY placement_ordinal,evidence_id",
            run_id,
            "evidence_json",
            ImageEvidence,
        )

    def attempts(self, run_id: str) -> list[ImageEvidenceAttempt]:
        return self._models(
            "SELECT a.attempt_json FROM book_image_evidence_attempt a "
            "JOIN book_image_evidence e ON e.evidence_id=a.evidence_id "
            "WHERE e.run_id=? ORDER BY e.placement_ordinal,a.attempt_ordinal,a.attempt_id",
            run_id,
            "attempt_json",
            ImageEvidenceAttempt,
        )

    def ocr_results(self, run_id: str) -> list[ImageOcrResult]:
        return self._models(
            "SELECT result_json FROM book_image_ocr WHERE run_id=? ORDER BY evidence_id",
            run_id,
            "result_json",
            ImageOcrResult,
        )

    def layout_atoms(self, run_id: str) -> list[BookLayoutAtom]:
        return self._models(
            "SELECT atom_json FROM book_layout_atom WHERE run_id=? "
            "ORDER BY global_ordinal,atom_id",
            run_id,
            "atom_json",
            BookLayoutAtom,
        )

    def chart_units(self, run_id: str) -> list[ChartUnit]:
        return self._models(
            "SELECT unit_json FROM book_chart_unit WHERE run_id=? ORDER BY chart_unit_id",
            run_id,
            "unit_json",
            ChartUnit,
        )

    def semantic_refs(self, run_id: str) -> list[BookVisualSemanticRef]:
        return self._models(
            "SELECT ref_json FROM book_visual_semantic_ref WHERE run_id=? ORDER BY ref_id",
            run_id,
            "ref_json",
            BookVisualSemanticRef,
        )

    def report(self, run_id: str) -> BookVisualCoverageReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM book_visual_coverage_report WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return BookVisualCoverageReport.model_validate_json(row["report_json"]) if row else None

    def _models(
        self,
        sql: str,
        run_id: str,
        field: str,
        model: type,
    ) -> list:
        with self.state.connect() as connection:
            rows = connection.execute(sql, (run_id,)).fetchall()
        return [model.model_validate_json(row[field]) for row in rows]

    def _save_run_row(
        self,
        connection: sqlite3.Connection,
        run: BookVisualRun,
    ) -> BookVisualRun:
        if run.run_object_sha256 is None:
            raise ValueError("book visual run must be ObjectStore-first")
        encoded = _model_json(run)
        row = connection.execute(
            "SELECT run_json FROM book_visual_run WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
        values = (
            run.source_manifest_id,
            run.source_id,
            run.source_snapshot_id,
            run.raw_object_sha256,
            run.pipeline_version,
            run.layout_version,
            run.classification_version,
            run.stage.value,
            run.source_page_count,
            run.image_page_count,
            run.image_placement_count,
            run.processed_placement_count,
            run.semantic_run_id,
            run.coverage_report_object_sha256,
            run.run_object_sha256,
            encoded,
            run.started_at.isoformat(),
            run.finished_at.isoformat() if run.finished_at else None,
        )
        if row is None:
            connection.execute(
                "INSERT INTO book_visual_run("
                "run_id,source_manifest_id,source_id,source_snapshot_id,raw_object_hash,"
                "pipeline_version,layout_version,classification_version,stage,"
                "source_page_count,image_page_count,image_placement_count,"
                "processed_placement_count,semantic_run_id,coverage_report_hash,"
                "run_object_hash,run_json,started_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run.run_id, *values),
            )
            return run
        existing = BookVisualRun.model_validate_json(row["run_json"])
        _validate_same_run(existing, run)
        if _STAGE_ORDER[run.stage] < _STAGE_ORDER[existing.stage]:
            raise ValueError("book visual run stage cannot move backwards")
        if run.stage is existing.stage and existing != run:
            raise ValueError(f"book visual run stage collision: {run.run_id}")
        if run.stage is existing.stage:
            return existing
        connection.execute(
            "UPDATE book_visual_run SET stage=?,image_page_count=?,"
            "image_placement_count=?,processed_placement_count=?,semantic_run_id=?,"
            "coverage_report_hash=?,run_object_hash=?,run_json=?,finished_at=? "
            "WHERE run_id=?",
            (
                run.stage.value,
                run.image_page_count,
                run.image_placement_count,
                run.processed_placement_count,
                run.semantic_run_id,
                run.coverage_report_object_sha256,
                run.run_object_sha256,
                encoded,
                run.finished_at.isoformat() if run.finished_at else None,
                run.run_id,
            ),
        )
        return run

    def _insert_evidence(
        self,
        connection: sqlite3.Connection,
        evidence: ImageEvidence,
    ) -> None:
        if evidence.evidence_object_sha256 is None:
            raise ValueError("image evidence must be ObjectStore-first")
        _insert_exact(
            connection,
            table="book_image_evidence",
            key="evidence_id",
            identifier=evidence.evidence_id,
            json_column="evidence_json",
            encoded=_model_json(evidence),
            columns=(
                "evidence_id",
                "run_id",
                "page_number",
                "placement_index",
                "placement_ordinal",
                "xref",
                "bbox_json",
                "page_width",
                "page_height",
                "image_object_hash",
                "duplicate_of_evidence_id",
                "evidence_object_hash",
                "evidence_json",
            ),
            values=(
                evidence.evidence_id,
                evidence.run_id,
                evidence.page_number,
                evidence.placement_index,
                evidence.placement_ordinal,
                evidence.xref,
                _json(evidence.bbox),
                evidence.page_width,
                evidence.page_height,
                evidence.image_object_sha256,
                evidence.duplicate_of_evidence_id,
                evidence.evidence_object_sha256,
                _model_json(evidence),
            ),
        )

    def _insert_attempt(
        self,
        connection: sqlite3.Connection,
        attempt: ImageEvidenceAttempt,
    ) -> None:
        if attempt.attempt_object_sha256 is None:
            raise ValueError("image attempt must be ObjectStore-first")
        encoded = _model_json(attempt)
        _insert_exact(
            connection,
            table="book_image_evidence_attempt",
            key="attempt_id",
            identifier=attempt.attempt_id,
            json_column="attempt_json",
            encoded=encoded,
            columns=(
                "attempt_id",
                "evidence_id",
                "attempt_ordinal",
                "extraction_mode",
                "status",
                "image_object_hash",
                "error_code",
                "attempt_object_hash",
                "attempt_json",
            ),
            values=(
                attempt.attempt_id,
                attempt.evidence_id,
                attempt.attempt_ordinal,
                attempt.extraction_mode.value,
                attempt.status.value,
                attempt.image_object_sha256,
                attempt.error_code,
                attempt.attempt_object_sha256,
                encoded,
            ),
        )

    def _insert_ocr(self, connection: sqlite3.Connection, result: ImageOcrResult) -> None:
        if result.result_object_sha256 is None:
            raise ValueError("OCR result must be ObjectStore-first")
        encoded = _model_json(result)
        _insert_exact(
            connection,
            table="book_image_ocr",
            key="evidence_id",
            identifier=result.evidence_id,
            json_column="result_json",
            encoded=encoded,
            columns=(
                "evidence_id",
                "run_id",
                "status",
                "text_object_hash",
                "average_confidence",
                "engine_name",
                "engine_version",
                "reason_codes_json",
                "result_object_hash",
                "result_json",
            ),
            values=(
                result.evidence_id,
                result.run_id,
                result.status.value,
                result.text_object_sha256,
                result.average_confidence,
                result.engine_name,
                result.engine_version,
                _json(result.reason_codes),
                result.result_object_sha256,
                encoded,
            ),
        )

    def _insert_layout_atom(
        self,
        connection: sqlite3.Connection,
        atom: BookLayoutAtom,
    ) -> None:
        if atom.atom_object_sha256 is None:
            raise ValueError("layout atom must be ObjectStore-first")
        encoded = _model_json(atom)
        _insert_exact(
            connection,
            table="book_layout_atom",
            key="atom_id",
            identifier=atom.atom_id,
            json_column="atom_json",
            encoded=encoded,
            columns=(
                "atom_id",
                "run_id",
                "page_number",
                "page_ordinal",
                "global_ordinal",
                "atom_kind",
                "bbox_json",
                "text_object_hash",
                "evidence_id",
                "atom_object_hash",
                "atom_json",
            ),
            values=(
                atom.atom_id,
                atom.run_id,
                atom.page_number,
                atom.page_ordinal,
                atom.global_ordinal,
                atom.atom_kind.value,
                _json(atom.bbox),
                atom.text_object_sha256,
                atom.evidence_id,
                atom.atom_object_sha256,
                encoded,
            ),
        )

    def _insert_chart(self, connection: sqlite3.Connection, unit: ChartUnit) -> None:
        if unit.unit_object_sha256 is None:
            raise ValueError("chart unit must be ObjectStore-first")
        encoded = _model_json(unit)
        _insert_exact(
            connection,
            table="book_chart_unit",
            key="chart_unit_id",
            identifier=unit.chart_unit_id,
            json_column="unit_json",
            encoded=encoded,
            columns=(
                "chart_unit_id",
                "run_id",
                "evidence_id",
                "chart_type",
                "classification_confidence",
                "decorative_excluded",
                "caption_present",
                "review_reason_codes_json",
                "unit_object_hash",
                "unit_json",
            ),
            values=(
                unit.chart_unit_id,
                unit.run_id,
                unit.evidence_id,
                unit.chart_type.value,
                unit.classification_confidence,
                int(unit.decorative_excluded),
                int(unit.caption_present),
                _json(unit.review_reason_codes),
                unit.unit_object_sha256,
                encoded,
            ),
        )

    def _insert_semantic_ref(
        self,
        connection: sqlite3.Connection,
        ref: BookVisualSemanticRef,
    ) -> None:
        if ref.ref_object_sha256 is None:
            raise ValueError("semantic ref must be ObjectStore-first")
        encoded = _model_json(ref)
        _insert_exact(
            connection,
            table="book_visual_semantic_ref",
            key="ref_id",
            identifier=ref.ref_id,
            json_column="ref_json",
            encoded=encoded,
            columns=(
                "ref_id",
                "run_id",
                "chart_unit_id",
                "semantic_run_id",
                "paragraph_id",
                "argument_unit_id",
                "relation_ids_json",
                "ref_object_hash",
                "ref_json",
            ),
            values=(
                ref.ref_id,
                ref.run_id,
                ref.chart_unit_id,
                ref.semantic_run_id,
                ref.paragraph_id,
                ref.argument_unit_id,
                _json(ref.relation_ids),
                ref.ref_object_sha256,
                encoded,
            ),
        )

    def _insert_report(
        self,
        connection: sqlite3.Connection,
        report: BookVisualCoverageReport,
    ) -> None:
        if report.report_object_sha256 is None:
            raise ValueError("coverage report must be ObjectStore-first")
        encoded = _model_json(report)
        _insert_exact(
            connection,
            table="book_visual_coverage_report",
            key="report_id",
            identifier=report.report_id,
            json_column="report_json",
            encoded=encoded,
            columns=(
                "report_id",
                "run_id",
                "coverage_status",
                "quality_status",
                "report_object_hash",
                "report_json",
            ),
            values=(
                report.report_id,
                report.run_id,
                report.coverage_status.value,
                report.quality_status.value,
                report.report_object_sha256,
                encoded,
            ),
        )


def _validate_same_run(existing: BookVisualRun, incoming: BookVisualRun) -> None:
    immutable = (
        "run_id",
        "source_manifest_id",
        "source_id",
        "source_snapshot_id",
        "raw_object_sha256",
        "pipeline_version",
        "layout_version",
        "classification_version",
        "input_hashes",
        "source_page_count",
        "started_at",
    )
    if any(getattr(existing, field) != getattr(incoming, field) for field in immutable):
        raise ValueError(f"book visual run identity collision: {incoming.run_id}")


def _validate_layout_projection(
    run: BookVisualRun,
    evidences: list[ImageEvidence],
    attempts: list[ImageEvidenceAttempt],
    atoms: list[BookLayoutAtom],
) -> None:
    if len(evidences) != run.image_placement_count:
        raise ValueError("layout evidence count does not match placement count")
    evidence_ids = {evidence.evidence_id for evidence in evidences}
    if len(evidence_ids) != len(evidences):
        raise ValueError("image evidence ids must be unique")
    if [evidence.placement_ordinal for evidence in evidences] != list(
        range(1, len(evidences) + 1)
    ):
        raise ValueError("image evidence placement ordinals must be contiguous")
    if any(evidence.run_id != run.run_id for evidence in evidences):
        raise ValueError("image evidence belongs to another run")
    attempt_ids = {attempt.attempt_id for attempt in attempts}
    if len(attempt_ids) != len(attempts):
        raise ValueError("image attempt ids must be unique")
    if any(attempt.evidence_id not in evidence_ids for attempt in attempts):
        raise ValueError("image attempt refers to unknown evidence")
    attempts_by_evidence = {
        evidence_id: [
            attempt.attempt_id
            for attempt in attempts
            if attempt.evidence_id == evidence_id
        ]
        for evidence_id in evidence_ids
    }
    if any(
        evidence.attempt_ids != attempts_by_evidence[evidence.evidence_id]
        for evidence in evidences
    ):
        raise ValueError("image evidence attempt projection is inconsistent")
    if any(atom.run_id != run.run_id for atom in atoms):
        raise ValueError("layout atom belongs to another run")
    atom_evidence_ids = {
        atom.evidence_id for atom in atoms if atom.evidence_id is not None
    }
    image_atom_count = sum(atom.evidence_id is not None for atom in atoms)
    if atom_evidence_ids != evidence_ids or image_atom_count != len(evidences):
        raise ValueError("every image placement requires exactly one layout atom")
    if [atom.global_ordinal for atom in atoms] != list(range(1, len(atoms) + 1)):
        raise ValueError("layout global ordinals must be contiguous")
    for page_number in {atom.page_number for atom in atoms}:
        page_ordinals = [
            atom.page_ordinal for atom in atoms if atom.page_number == page_number
        ]
        if page_ordinals != list(range(1, len(page_ordinals) + 1)):
            raise ValueError("layout page ordinals must be contiguous")


def _insert_exact(
    connection: sqlite3.Connection,
    *,
    table: str,
    key: str,
    identifier: str,
    json_column: str,
    encoded: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    row = connection.execute(
        f"SELECT {json_column} FROM {table} WHERE {key}=?",
        (identifier,),
    ).fetchone()
    if row is not None:
        if str(row[json_column]) != encoded:
            raise ValueError(f"{table} identity collision: {identifier}")
        return
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        values,
    )


def _model_json(value: object) -> str:
    return canonical_json_bytes(value.model_dump(mode="json")).decode("utf-8")  # type: ignore[attr-defined]


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


__all__ = ["BookVisualRepository"]
