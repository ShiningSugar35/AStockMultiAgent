"""SQLite metadata repository for private knowledge distillation."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.state import StateStore
from astock.schemas import (
    AuthorDistillationReport,
    BookCleaningReport,
    BookMethodCoverageReport,
    DistillationReviewQueue,
    DistillationRun,
    DistillationRunStatus,
    DistillationUnit,
)


class DistillationRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_run(self, run_id: str) -> DistillationRun | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_distillation_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return DistillationRun.model_validate_json(row["run_json"]) if row else None

    def register_run(self, run: DistillationRun) -> DistillationRun:
        if run.status is not DistillationRunStatus.RUNNING:
            raise ValueError("new distillation runs must start as RUNNING")
        run_json = canonical_json_bytes(run.model_dump(mode="json")).decode("utf-8")
        input_set_hash = content_hash(sorted(run.input_hashes))
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT author_source_id,classification_rule_version,input_set_hash,run_json "
                "FROM knowledge_distillation_run WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            if row is not None:
                expected = (
                    run.author_source_id,
                    run.classification_rule_version,
                    input_set_hash,
                )
                if tuple(row[:3]) != expected:
                    raise ValueError(f"distillation run collision: {run.run_id}")
                return DistillationRun.model_validate_json(row["run_json"])
            connection.execute(
                "INSERT INTO knowledge_distillation_run("
                "run_id,author_source_id,classification_rule_version,status,input_set_hash,"
                "input_source_item_count,produced_unit_count,run_json,started_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.author_source_id,
                    run.classification_rule_version,
                    run.status.value,
                    input_set_hash,
                    run.input_source_item_count,
                    run.produced_unit_count,
                    run_json,
                    run.started_at.isoformat(),
                    None,
                ),
            )
        return run

    def complete_run(self, run: DistillationRun) -> DistillationRun:
        if run.status is not DistillationRunStatus.COMPLETE:
            raise ValueError("only complete distillation runs can be finalized")
        run_json = canonical_json_bytes(run.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT run_json,status FROM knowledge_distillation_run WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run.run_id)
            if row["status"] == DistillationRunStatus.COMPLETE.value:
                existing = DistillationRun.model_validate_json(row["run_json"])
                if existing != run:
                    raise ValueError(f"completed distillation run collision: {run.run_id}")
                return existing
            connection.execute(
                "UPDATE knowledge_distillation_run SET status=?,input_source_item_count=?,"
                "produced_unit_count=?,run_json=?,finished_at=? WHERE run_id=?",
                (
                    run.status.value,
                    run.input_source_item_count,
                    run.produced_unit_count,
                    run_json,
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.run_id,
                ),
            )
        return run

    def register_units(self, units: list[DistillationUnit]) -> None:
        with self.state.transaction() as connection:
            for unit in units:
                unit_json = canonical_json_bytes(unit.model_dump(mode="json")).decode("utf-8")
                row = connection.execute(
                    "SELECT unit_json FROM knowledge_distillation_unit WHERE unit_id=?",
                    (unit.unit_id,),
                ).fetchone()
                if row is not None:
                    if str(row["unit_json"]) != unit_json:
                        raise ValueError(f"distillation unit collision: {unit.unit_id}")
                    continue
                connection.execute(
                    "INSERT INTO knowledge_distillation_unit("
                    "unit_id,run_id,author_source_id,source_id,source_snapshot_id,"
                    "source_unit_id,source_item_ordinal,segment_ordinal,normalized_text_hash,"
                    "duplicate_of_unit_id,decision,classification_rule_version,unit_json,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        unit.unit_id,
                        unit.run_id,
                        unit.author_source_id,
                        unit.source_id,
                        unit.locator.source_snapshot_id,
                        unit.locator.source_unit_id,
                        unit.source_item_ordinal,
                        unit.segment_ordinal,
                        unit.normalized_text_sha256,
                        unit.duplicate_of_unit_id,
                        unit.decision.value,
                        unit.classification_rule_version,
                        unit_json,
                        unit.created_at.isoformat(),
                    ),
                )

    def units_for_run(self, run_id: str) -> list[DistillationUnit]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT unit_json FROM knowledge_distillation_unit WHERE run_id=? "
                "ORDER BY source_item_ordinal,segment_ordinal,unit_id",
                (run_id,),
            ).fetchall()
        return [DistillationUnit.model_validate_json(row["unit_json"]) for row in rows]

    def register_author_report(
        self,
        report: AuthorDistillationReport,
        *,
        object_hash: str,
    ) -> None:
        report_json = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT report_json,report_object_hash FROM author_distillation_report "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["report_json"]) != report_json
                    or row["report_object_hash"] != object_hash
                ):
                    raise ValueError(f"author distillation report collision: {report.report_id}")
                return
            connection.execute(
                "INSERT INTO author_distillation_report("
                "report_id,run_id,author_source_id,coverage_status,human_review_status,"
                "report_object_hash,report_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.run_id,
                    report.author_source_id,
                    report.coverage_status.value,
                    report.human_review_status.value,
                    object_hash,
                    report_json,
                    report.created_at.isoformat(),
                ),
            )

    def latest_author_report(self, author_source_id: str) -> AuthorDistillationReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM author_distillation_report WHERE author_source_id=? "
                "ORDER BY created_at DESC,report_id DESC LIMIT 1",
                (author_source_id,),
            ).fetchone()
        return AuthorDistillationReport.model_validate_json(row["report_json"]) if row else None

    def author_report_for_run(self, run_id: str) -> AuthorDistillationReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM author_distillation_report WHERE run_id=? "
                "ORDER BY created_at DESC,report_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return AuthorDistillationReport.model_validate_json(row["report_json"]) if row else None

    def review_queue_object_hash_for_run(self, run_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT queue_object_hash FROM distillation_review_queue WHERE run_id=? "
                "ORDER BY created_at DESC,queue_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return str(row["queue_object_hash"]) if row else None

    def book_report_ids_for_run(self, run_id: str) -> tuple[list[str], list[str]]:
        with self.state.connect() as connection:
            cleaning = connection.execute(
                "SELECT report_id FROM book_cleaning_report WHERE run_id=? ORDER BY report_id",
                (run_id,),
            ).fetchall()
            coverage = connection.execute(
                "SELECT report_id FROM book_method_coverage_report WHERE run_id=? "
                "ORDER BY report_id",
                (run_id,),
            ).fetchall()
        return (
            [str(row["report_id"]) for row in cleaning],
            [str(row["report_id"]) for row in coverage],
        )

    def latest_review_queue_summary(self, author_source_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT queue_id,run_id,candidate_count,human_review_status,"
                "queue_object_hash,created_at FROM distillation_review_queue "
                "WHERE author_source_id=? ORDER BY created_at DESC,queue_id DESC LIMIT 1",
                (author_source_id,),
            ).fetchone()
        return dict(row) if row else None

    def register_review_queue(
        self,
        queue: DistillationReviewQueue,
        *,
        object_hash: str,
    ) -> None:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT queue_object_hash,candidate_count,human_review_status "
                "FROM distillation_review_queue WHERE queue_id=?",
                (queue.queue_id,),
            ).fetchone()
            expected = (
                object_hash,
                len(queue.unit_ids),
                queue.human_review_status.value,
            )
            if row is not None:
                if tuple(row) != expected:
                    raise ValueError(f"distillation review queue collision: {queue.queue_id}")
                return
            connection.execute(
                "INSERT INTO distillation_review_queue("
                "queue_id,run_id,author_source_id,candidate_count,human_review_status,"
                "queue_object_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    queue.queue_id,
                    queue.run_id,
                    queue.author_source_id,
                    len(queue.unit_ids),
                    queue.human_review_status.value,
                    object_hash,
                    queue.created_at.isoformat(),
                ),
            )

    def register_book_cleaning_report(
        self,
        report: BookCleaningReport,
        *,
        run_id: str,
        object_hash: str,
    ) -> None:
        self._register_book_report(
            table="book_cleaning_report",
            report_id=report.report_id,
            manifest_id=report.manifest_id,
            run_id=run_id,
            processing_status=report.processing_status.value,
            human_review_status=report.human_review_status.value,
            object_hash=object_hash,
            report_json=canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8"),
            created_at=report.created_at.isoformat(),
        )

    def register_book_method_coverage_report(
        self,
        report: BookMethodCoverageReport,
        *,
        run_id: str,
        object_hash: str,
    ) -> None:
        self._register_book_report(
            table="book_method_coverage_report",
            report_id=report.report_id,
            manifest_id=report.manifest_id,
            run_id=run_id,
            processing_status=report.processing_status.value,
            human_review_status=report.human_review_status.value,
            object_hash=object_hash,
            report_json=canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8"),
            created_at=report.created_at.isoformat(),
        )

    def _register_book_report(
        self,
        *,
        table: str,
        report_id: str,
        manifest_id: str,
        run_id: str,
        processing_status: str,
        human_review_status: str,
        object_hash: str,
        report_json: str,
        created_at: str,
    ) -> None:
        if table not in {"book_cleaning_report", "book_method_coverage_report"}:
            raise ValueError("unsupported book report table")
        with self.state.transaction() as connection:
            row = connection.execute(
                f"SELECT report_json,report_object_hash FROM {table} WHERE report_id=?",
                (report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["report_json"]) != report_json
                    or row["report_object_hash"] != object_hash
                ):
                    raise ValueError(f"book report collision: {report_id}")
                return
            connection.execute(
                f"INSERT INTO {table}(report_id,manifest_id,run_id,processing_status,"
                "human_review_status,report_object_hash,report_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    report_id,
                    manifest_id,
                    run_id,
                    processing_status,
                    human_review_status,
                    object_hash,
                    report_json,
                    created_at,
                ),
            )


__all__ = ["DistillationRepository"]
