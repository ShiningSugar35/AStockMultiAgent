"""SQLite repository for human-reviewed arguments and shadow-only skills."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    ArgumentUnit,
    CandidateSelectionSkill,
    MethodRule,
    ParagraphUnit,
    PositionLifecycleSkill,
    ReviewDecision,
    ReviewedArgumentUnit,
    ReviewedAuthorSkillCoverage,
    ReviewedCoverageReport,
    ReviewedEmbeddingManifest,
    ReviewedSemanticRun,
    ReviewedShadowBundle,
    ReviewedSkillKind,
    ViewpointCard,
)


@dataclass(frozen=True, slots=True)
class SourceParagraphRecord:
    unit: ParagraphUnit
    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SourceVisualRecord:
    source_visual_ref_id: str
    paragraph_id: str
    chart_unit_id: str
    evidence_id: str
    ref_json: str


@dataclass(frozen=True, slots=True)
class SourceMaterial:
    paragraphs: tuple[SourceParagraphRecord, ...]
    arguments: tuple[ArgumentUnit, ...]
    visuals: tuple[SourceVisualRecord, ...]


@dataclass(frozen=True, slots=True)
class ReviewedResult:
    run: ReviewedSemanticRun
    coverage: ReviewedCoverageReport
    shadow_bundle: ReviewedShadowBundle
    statistics: dict[str, int]


class ReviewedKnowledgeRepository:
    """Keep reviewed artifacts separate while retaining exact source lineage."""

    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def source_run_json(self, run_id: str) -> str:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_semantic_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return str(row["run_json"])

    def source_material(self, run_id: str) -> SourceMaterial:
        with self.state.connect() as connection:
            paragraph_rows = connection.execute(
                "SELECT paragraph_id,item_id,unit_json FROM knowledge_paragraph_unit "
                "WHERE run_id=? ORDER BY "
                "json_extract(unit_json,'$.locator.page_number'),ordinal,paragraph_id",
                (run_id,),
            ).fetchall()
            argument_rows = connection.execute(
                "SELECT unit_json FROM knowledge_argument_unit WHERE run_id=? "
                "ORDER BY argument_unit_id",
                (run_id,),
            ).fetchall()
            visual_rows = connection.execute(
                "SELECT r.ref_id,r.paragraph_id,r.chart_unit_id,c.evidence_id,r.ref_json "
                "FROM book_visual_semantic_ref r "
                "JOIN book_chart_unit c ON c.chart_unit_id=r.chart_unit_id "
                "WHERE r.semantic_run_id=? ORDER BY r.paragraph_id,r.chart_unit_id",
                (run_id,),
            ).fetchall()
        paragraphs: list[SourceParagraphRecord] = []
        for row in paragraph_rows:
            unit = ParagraphUnit.model_validate_json(row["unit_json"])
            text = self.object_store.get_bytes(unit.text_object_sha256).decode("utf-8")
            paragraphs.append(
                SourceParagraphRecord(
                    unit=unit,
                    item_id=str(row["item_id"]),
                    text=text,
                )
            )
        return SourceMaterial(
            paragraphs=tuple(paragraphs),
            arguments=tuple(
                ArgumentUnit.model_validate_json(row["unit_json"]) for row in argument_rows
            ),
            visuals=tuple(
                SourceVisualRecord(
                    source_visual_ref_id=str(row["ref_id"]),
                    paragraph_id=str(row["paragraph_id"]),
                    chart_unit_id=str(row["chart_unit_id"]),
                    evidence_id=str(row["evidence_id"]),
                    ref_json=str(row["ref_json"]),
                )
                for row in visual_rows
            ),
        )

    def source_fingerprint(self, run_id: str) -> str:
        tables = (
            ("knowledge_semantic_run", "run_id", "run_id"),
            ("knowledge_semantic_content_item", "run_id", "item_id"),
            ("knowledge_paragraph_unit", "run_id", "paragraph_id"),
            ("knowledge_argument_relation", "run_id", "relation_id"),
            ("knowledge_argument_unit", "run_id", "argument_unit_id"),
            ("knowledge_embedding_manifest", "run_id", "manifest_id"),
            ("knowledge_semantic_candidate", "run_id", "candidate_id"),
        )
        projection: dict[str, list[list[Any]]] = {}
        with self.state.connect() as connection:
            for table, filter_column, order_column in tables:
                columns = [
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                if not columns:
                    projection[table] = []
                    continue
                selected = connection.execute(
                    f"SELECT * FROM {table} WHERE {filter_column}=? ORDER BY {order_column}",
                    (run_id,),
                ).fetchall()
                projection[table] = [[row[column] for column in columns] for row in selected]
        return content_hash(projection)

    def source_embedding_manifest_id(self, run_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT manifest_id FROM knowledge_embedding_manifest WHERE run_id=? "
                "ORDER BY manifest_id LIMIT 1",
                (run_id,),
            ).fetchone()
        return str(row["manifest_id"]) if row else None

    def get_run(self, run_id: str) -> ReviewedSemanticRun | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_reviewed_semantic_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return ReviewedSemanticRun.model_validate_json(row["run_json"]) if row else None

    def save_run(self, run: ReviewedSemanticRun) -> None:
        encoded = _model_json(run)
        object_ref = self.object_store.put_bytes(encoded.encode("utf-8"))
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT source_run_id,author_source_id,review_workbook_hash,"
                "source_pdf_hash,input_manifest_hash,pipeline_version "
                "FROM knowledge_reviewed_semantic_run WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            immutable = (
                run.source_run_id,
                run.author_source_id,
                run.review_workbook_sha256,
                run.source_pdf_sha256,
                run.input_manifest_sha256,
                run.pipeline_version,
            )
            if row is None:
                connection.execute(
                    "INSERT INTO knowledge_reviewed_semantic_run("
                    "run_id,source_run_id,author_source_id,review_workbook_hash,"
                    "source_pdf_hash,input_manifest_hash,pipeline_version,stage,"
                    "review_record_count,reviewed_argument_count,unresolved_count,"
                    "run_object_hash,run_json,started_at,finished_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run.run_id,
                        *immutable,
                        run.stage.value,
                        run.review_record_count,
                        run.reviewed_argument_count,
                        run.unresolved_count,
                        object_ref.sha256,
                        encoded,
                        _utc_text(run.started_at),
                        _utc_text(run.finished_at) if run.finished_at else None,
                    ),
                )
                return
            existing_immutable = tuple(row)
            if existing_immutable != immutable:
                raise ValueError("reviewed run identity collision")
            connection.execute(
                "UPDATE knowledge_reviewed_semantic_run SET "
                "stage=?,review_record_count=?,reviewed_argument_count=?,"
                "unresolved_count=?,run_object_hash=?,run_json=?,finished_at=? "
                "WHERE run_id=?",
                (
                    run.stage.value,
                    run.review_record_count,
                    run.reviewed_argument_count,
                    run.unresolved_count,
                    object_ref.sha256,
                    encoded,
                    _utc_text(run.finished_at) if run.finished_at else None,
                    run.run_id,
                ),
            )

    def save_decisions(self, decisions: Sequence[ReviewDecision]) -> None:
        if not decisions:
            return
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            for decision in decisions:
                encoded = _model_json(decision)
                object_hash = sha256_bytes(encoded.encode("utf-8"))
                _insert_exact(
                    connection,
                    table="knowledge_review_decision",
                    key_column="decision_id",
                    key_value=decision.decision_id,
                    json_column="decision_json",
                    json_value=encoded,
                    columns=(
                        "decision_id",
                        "run_id",
                        "excel_row",
                        "source_argument_unit_id",
                        "verdict",
                        "application_status",
                        "decision_object_hash",
                        "decision_json",
                        "created_at",
                    ),
                    values=(
                        decision.decision_id,
                        decision.run_id,
                        decision.excel_row,
                        decision.source_argument_unit_id,
                        decision.verdict.value,
                        decision.application_status.value,
                        object_hash,
                        encoded,
                        now,
                    ),
                )
                ranges = [
                    range_value for target in decision.targets for range_value in target.ranges
                ]
                for ordinal, range_value in enumerate(ranges, start=1):
                    range_json = _model_json(range_value)
                    _insert_exact_row(
                        connection,
                        table="knowledge_review_decision_candidate_range",
                        where="decision_id=? AND range_ordinal=?",
                        where_values=(decision.decision_id, ordinal),
                        columns=(
                            "decision_id",
                            "range_ordinal",
                            "start_page",
                            "start_paragraph_ordinal",
                            "end_page",
                            "end_paragraph_ordinal",
                            "range_json",
                        ),
                        values=(
                            decision.decision_id,
                            ordinal,
                            range_value.start_page,
                            range_value.start_paragraph_ordinal,
                            range_value.end_page,
                            range_value.end_paragraph_ordinal,
                            range_json,
                        ),
                    )

    def load_decisions(self, run_id: str) -> list[ReviewDecision]:
        with self.state.connect() as connection:
            decision_rows = connection.execute(
                "SELECT decision_json FROM knowledge_review_decision "
                "WHERE run_id=? ORDER BY excel_row",
                (run_id,),
            ).fetchall()
            if not decision_rows:
                return []
        return [ReviewDecision.model_validate_json(row["decision_json"]) for row in decision_rows]

    def load_reviewed_arguments(self, run_id: str) -> tuple[ReviewedArgumentUnit, ...]:
        with self.state.connect() as connection:
            argument_rows = connection.execute(
                "SELECT unit_json FROM knowledge_reviewed_argument_unit "
                "WHERE run_id=? ORDER BY argument_unit_id",
                (run_id,),
            ).fetchall()
        return tuple(
            ReviewedArgumentUnit.model_validate_json(row["unit_json"])
            for row in argument_rows
        )

    def save_arguments(
        self,
        arguments: Sequence[ReviewedArgumentUnit],
        visual_lookup: dict[tuple[str, str], SourceVisualRecord],
    ) -> None:
        if not arguments:
            return
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            for argument in arguments:
                encoded = _model_json(argument)
                object_hash = sha256_bytes(encoded.encode("utf-8"))
                lineage = {
                    "decision_ids": argument.decision_ids,
                    "source_argument_unit_ids": argument.source_argument_unit_ids,
                    "source_snapshot_ids": argument.source_snapshot_ids,
                }
                _insert_exact(
                    connection,
                    table="knowledge_reviewed_argument_unit",
                    key_column="argument_unit_id",
                    key_value=argument.argument_unit_id,
                    json_column="unit_json",
                    json_value=encoded,
                    columns=(
                        "argument_unit_id",
                        "run_id",
                        "decision_id",
                        "author_source_id",
                        "title",
                        "text_object_hash",
                        "status",
                        "topic_relevance",
                        "methodological_completeness",
                        "standalone_distillable",
                        "method_categories_json",
                        "rhetorical_roles_json",
                        "lineage_json",
                        "unit_object_hash",
                        "unit_json",
                        "created_at",
                    ),
                    values=(
                        argument.argument_unit_id,
                        argument.run_id,
                        argument.decision_ids[0],
                        argument.author_source_id,
                        argument.title,
                        argument.text_object_sha256,
                        argument.status.value,
                        argument.topic_relevance,
                        argument.methodological_completeness,
                        int(argument.standalone_distillable),
                        _json([item.value for item in argument.method_categories]),
                        _json([item.value for item in argument.rhetorical_roles]),
                        _json(lineage),
                        object_hash,
                        encoded,
                        now,
                    ),
                )
                for ordinal, decision_id in enumerate(argument.decision_ids, start=1):
                    _insert_exact_row(
                        connection,
                        table="knowledge_reviewed_argument_decision_ref",
                        where="argument_unit_id=? AND ref_ordinal=?",
                        where_values=(argument.argument_unit_id, ordinal),
                        columns=("argument_unit_id", "ref_ordinal", "decision_id"),
                        values=(argument.argument_unit_id, ordinal, decision_id),
                    )
                for ref in argument.paragraph_refs:
                    _insert_exact_row(
                        connection,
                        table="knowledge_reviewed_argument_paragraph_ref",
                        where="argument_unit_id=? AND ref_ordinal=?",
                        where_values=(argument.argument_unit_id, ref.ref_ordinal),
                        columns=(
                            "argument_unit_id",
                            "ref_ordinal",
                            "source_paragraph_id",
                            "item_id",
                            "content_id",
                            "page_number",
                            "paragraph_ordinal",
                            "paragraph_head",
                            "text_object_hash",
                            "rhetorical_role",
                            "source_snapshot_id",
                            "locator_json",
                            "visual_evidence_ids_json",
                            "visual_chart_unit_ids_json",
                        ),
                        values=(
                            argument.argument_unit_id,
                            ref.ref_ordinal,
                            ref.source_paragraph_id,
                            ref.item_id,
                            ref.content_id,
                            ref.page_number,
                            ref.paragraph_ordinal,
                            ref.paragraph_head,
                            ref.text_object_sha256,
                            ref.rhetorical_role.value,
                            ref.source_snapshot_id,
                            _model_json(ref.locator),
                            _json(ref.visual_evidence_ids),
                            _json(ref.visual_chart_unit_ids),
                        ),
                    )
                    for chart_id in ref.visual_chart_unit_ids:
                        source_visual = visual_lookup.get((ref.source_paragraph_id, chart_id))
                        if source_visual is None:
                            raise ValueError("reviewed visual lineage is incomplete")
                        identity = {
                            "run_id": argument.run_id,
                            "argument_unit_id": argument.argument_unit_id,
                            "ref_ordinal": ref.ref_ordinal,
                            "chart_unit_id": chart_id,
                            "evidence_id": source_visual.evidence_id,
                        }
                        visual_ref_id = f"reviewed-visual-ref:{content_hash(identity)}"
                        visual_json = _json(
                            {
                                **identity,
                                "visual_ref_id": visual_ref_id,
                                "source_visual_ref_id": (source_visual.source_visual_ref_id),
                            }
                        )
                        _insert_exact(
                            connection,
                            table="knowledge_reviewed_visual_ref",
                            key_column="visual_ref_id",
                            key_value=visual_ref_id,
                            json_column="ref_json",
                            json_value=visual_json,
                            columns=(
                                "visual_ref_id",
                                "run_id",
                                "argument_unit_id",
                                "ref_ordinal",
                                "source_visual_ref_id",
                                "chart_unit_id",
                                "evidence_id",
                                "ref_object_hash",
                                "ref_json",
                            ),
                            values=(
                                visual_ref_id,
                                argument.run_id,
                                argument.argument_unit_id,
                                ref.ref_ordinal,
                                source_visual.source_visual_ref_id,
                                chart_id,
                                source_visual.evidence_id,
                                sha256_bytes(visual_json.encode("utf-8")),
                                visual_json,
                            ),
                        )
                for relation in argument.relations:
                    relation_json = _model_json(relation)
                    _insert_exact(
                        connection,
                        table="knowledge_reviewed_argument_relation",
                        key_column="relation_id",
                        key_value=relation.relation_id,
                        json_column="relation_json",
                        json_value=relation_json,
                        columns=(
                            "relation_id",
                            "run_id",
                            "argument_unit_id",
                            "source_ref_ordinal",
                            "target_ref_ordinal",
                            "relation_type",
                            "confidence",
                            "relation_json",
                            "created_at",
                        ),
                        values=(
                            relation.relation_id,
                            relation.run_id,
                            relation.argument_unit_id,
                            relation.source_ref_ordinal,
                            relation.target_ref_ordinal,
                            relation.relation_type.value,
                            relation.confidence,
                            relation_json,
                            now,
                        ),
                    )

    def save_embedding(self, manifest: ReviewedEmbeddingManifest) -> None:
        encoded = _model_json(manifest)
        object_ref = self.object_store.put_bytes(encoded.encode("utf-8"))
        with self.state.transaction() as connection:
            _insert_exact(
                connection,
                table="knowledge_reviewed_embedding_manifest",
                key_column="manifest_id",
                key_value=manifest.manifest_id,
                json_column="manifest_json",
                json_value=encoded,
                columns=(
                    "manifest_id",
                    "run_id",
                    "model_id",
                    "model_asset_hash",
                    "tokenizer_asset_hash",
                    "vector_parquet_hash",
                    "score_parquet_hash",
                    "method_vector_parquet_hash",
                    "source_embedding_manifest_id",
                    "manifest_object_hash",
                    "manifest_json",
                    "created_at",
                ),
                values=(
                    manifest.manifest_id,
                    manifest.run_id,
                    manifest.model_id,
                    manifest.model_asset_sha256,
                    manifest.tokenizer_asset_sha256,
                    manifest.vector_parquet_sha256,
                    manifest.score_parquet_sha256,
                    manifest.method_vector_parquet_sha256,
                    manifest.source_embedding_manifest_id,
                    object_ref.sha256,
                    encoded,
                    _utc_text(datetime.now(UTC)),
                ),
            )

    def save_distillation(
        self,
        *,
        cards: Sequence[ViewpointCard],
        rules: Sequence[MethodRule],
        candidate_skills: Sequence[CandidateSelectionSkill],
        lifecycle_skills: Sequence[PositionLifecycleSkill],
        author_coverage: ReviewedAuthorSkillCoverage,
        shadow_bundle: ReviewedShadowBundle,
    ) -> None:
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            for card in cards:
                encoded = _model_json(card)
                object_hash = sha256_bytes(encoded.encode("utf-8"))
                _insert_exact(
                    connection,
                    table="knowledge_viewpoint_card",
                    key_column="card_id",
                    key_value=card.card_id,
                    json_column="card_json",
                    json_value=encoded,
                    columns=(
                        "card_id",
                        "run_id",
                        "proposition",
                        "method_category",
                        "status",
                        "card_object_hash",
                        "card_json",
                        "created_at",
                    ),
                    values=(
                        card.card_id,
                        card.run_id,
                        card.proposition,
                        card.method_category.value,
                        card.status.value,
                        object_hash,
                        encoded,
                        now,
                    ),
                )
                self._save_au_refs(
                    connection,
                    table="knowledge_viewpoint_card_au_ref",
                    owner_column="card_id",
                    owner_id=card.card_id,
                    argument_ids=[item.argument_unit_id for item in card.source_refs],
                )
            for rule in rules:
                encoded = _model_json(rule)
                object_hash = sha256_bytes(encoded.encode("utf-8"))
                _insert_exact(
                    connection,
                    table="knowledge_method_rule",
                    key_column="rule_id",
                    key_value=rule.rule_id,
                    json_column="rule_json",
                    json_value=encoded,
                    columns=(
                        "rule_id",
                        "run_id",
                        "semantic_signature_hash",
                        "decision_question",
                        "status",
                        "rule_object_hash",
                        "rule_json",
                        "created_at",
                    ),
                    values=(
                        rule.rule_id,
                        rule.run_id,
                        rule.semantic_signature_sha256,
                        rule.decision_question,
                        rule.status.value,
                        object_hash,
                        encoded,
                        now,
                    ),
                )
                self._save_au_refs(
                    connection,
                    table="knowledge_method_rule_au_ref",
                    owner_column="rule_id",
                    owner_id=rule.rule_id,
                    argument_ids=[item.argument_unit_id for item in rule.source_refs],
                )
            for kind, skills in (
                (ReviewedSkillKind.CANDIDATE_SELECTION, candidate_skills),
                (ReviewedSkillKind.POSITION_LIFECYCLE, lifecycle_skills),
            ):
                for skill in skills:
                    self._save_skill(connection, kind, skill, now)
            self._save_single_json(
                connection,
                table="knowledge_author_skill_coverage",
                key_column="coverage_id",
                key_value=author_coverage.coverage_id,
                run_id=author_coverage.run_id,
                object_column="coverage_object_hash",
                json_column="coverage_json",
                model=author_coverage,
                extra_columns=("author_source_id",),
                extra_values=(author_coverage.author_source_id,),
                now=now,
            )
            shadow_json = _model_json(shadow_bundle)
            shadow_hash = sha256_bytes(shadow_json.encode("utf-8"))
            _insert_exact(
                connection,
                table="knowledge_reviewed_shadow_bundle",
                key_column="bundle_id",
                key_value=shadow_bundle.bundle_id,
                json_column="bundle_json",
                json_value=shadow_json,
                columns=(
                    "bundle_id",
                    "run_id",
                    "ready_skill_count",
                    "needs_review_skill_count",
                    "formal_committee_weight_allowed",
                    "bundle_object_hash",
                    "bundle_json",
                    "created_at",
                ),
                values=(
                    shadow_bundle.bundle_id,
                    shadow_bundle.run_id,
                    len(shadow_bundle.ready_skill_ids),
                    len(shadow_bundle.needs_user_review_skill_ids),
                    0,
                    shadow_hash,
                    shadow_json,
                    now,
                ),
            )

    def delete_distillation_artifacts(self, run_id: str) -> None:
        with self.state.transaction() as connection:
            table_deletes: list[tuple[str, str | None]] = [
                (
                    "knowledge_viewpoint_card_au_ref",
                    "card_id IN (SELECT card_id FROM knowledge_viewpoint_card WHERE run_id=?)",
                ),
                ("knowledge_viewpoint_card", "run_id=?"),
                (
                    "knowledge_method_rule_au_ref",
                    "rule_id IN (SELECT rule_id FROM knowledge_method_rule WHERE run_id=?)",
                ),
                (
                    "knowledge_reviewed_skill_rule_ref",
                    "skill_id IN (SELECT skill_id FROM knowledge_reviewed_skill WHERE run_id=?)",
                ),
                (
                    "knowledge_reviewed_skill_au_ref",
                    "skill_id IN (SELECT skill_id FROM knowledge_reviewed_skill WHERE run_id=?)",
                ),
                ("knowledge_reviewed_skill", "run_id=?"),
                ("knowledge_method_rule", "run_id=?"),
                ("knowledge_author_skill_coverage", "run_id=?"),
                ("knowledge_reviewed_shadow_bundle", "run_id=?"),
            ]
            for table, predicate in table_deletes:
                query = f"DELETE FROM {table} WHERE {predicate}"
                connection.execute(query, (run_id,))
            connection.execute(
                "DELETE FROM knowledge_reviewed_coverage_report WHERE run_id=?",
                (run_id,),
            )

    def replace_coverage(self, report: ReviewedCoverageReport) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "DELETE FROM knowledge_reviewed_coverage_report WHERE run_id=?",
                (report.run_id,),
            )
        self.save_coverage(report)

    def save_coverage(self, report: ReviewedCoverageReport) -> None:
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            self._save_single_json(
                connection,
                table="knowledge_reviewed_coverage_report",
                key_column="report_id",
                key_value=report.report_id,
                run_id=report.run_id,
                object_column="report_object_hash",
                json_column="report_json",
                model=report,
                extra_columns=("coverage_status",),
                extra_values=(report.coverage_status,),
                now=now,
            )

    def save_checkpoint(
        self,
        *,
        run_id: str,
        stage: str,
        batch_ordinal: int,
        cursor: dict[str, Any],
    ) -> None:
        encoded = _json(cursor)
        with self.state.transaction() as connection:
            _insert_exact_row(
                connection,
                table="knowledge_reviewed_checkpoint",
                where="run_id=? AND stage=? AND batch_ordinal=?",
                where_values=(run_id, stage, batch_ordinal),
                columns=(
                    "run_id",
                    "stage",
                    "batch_ordinal",
                    "cursor_json",
                    "checkpoint_object_hash",
                    "committed_at",
                ),
                values=(
                    run_id,
                    stage,
                    batch_ordinal,
                    encoded,
                    sha256_bytes(encoded.encode("utf-8")),
                    _utc_text(datetime.now(UTC)),
                ),
                compare_columns=(
                    "run_id",
                    "stage",
                    "batch_ordinal",
                    "cursor_json",
                    "checkpoint_object_hash",
                ),
            )

    def result(self, run_id: str) -> ReviewedResult:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        with self.state.connect() as connection:
            coverage_row = connection.execute(
                "SELECT report_json FROM knowledge_reviewed_coverage_report WHERE run_id=?",
                (run_id,),
            ).fetchone()
            bundle_row = connection.execute(
                "SELECT bundle_json FROM knowledge_reviewed_shadow_bundle WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if coverage_row is None or bundle_row is None:
                raise ValueError("reviewed run has no terminal artifacts")
            statistics = self._statistics(connection, run_id)
            coverage = ReviewedCoverageReport.model_validate_json(coverage_row["report_json"])
            statistics.update(coverage.acceptance_statistics)
        return ReviewedResult(
            run=run,
            coverage=coverage,
            shadow_bundle=ReviewedShadowBundle.model_validate_json(bundle_row["bundle_json"]),
            statistics=statistics,
        )

    def shadow_context(self, run_id: str) -> dict[str, object]:
        result = self.result(run_id)
        ready_ids = result.shadow_bundle.ready_skill_ids
        if not ready_ids:
            return {
                "run_id": run_id,
                "formal_committee_weight_allowed": False,
                "skills": [],
                "rules": [],
                "non_ready_rule_count": 0,
            }
        placeholders = ",".join("?" for _ in ready_ids)
        with self.state.connect() as connection:
            skill_rows = connection.execute(
                "SELECT manifest_json, status FROM knowledge_reviewed_skill "
                f"WHERE skill_id IN ({placeholders}) ORDER BY skill_kind,skill_category",
                tuple(ready_ids),
            ).fetchall()
            rule_rows = connection.execute(
                "SELECT DISTINCT r.rule_id, r.status, r.rule_json "
                "FROM knowledge_method_rule r "
                "JOIN knowledge_reviewed_skill_rule_ref sr ON sr.rule_id=r.rule_id "
                f"WHERE sr.skill_id IN ({placeholders}) "
                "ORDER BY r.rule_id",
                tuple(ready_ids),
            ).fetchall()
        skills = []
        for row in skill_rows:
            if str(row["status"]) != "READY_FOR_SHADOW":
                continue
            try:
                manifest = json.loads(row["manifest_json"])
            except json.JSONDecodeError:
                continue
            if manifest.get("status") == "READY_FOR_SHADOW":
                skills.append(manifest)
        rules: list[dict[str, object]] = []
        non_ready_rule_count = 0
        for row in rule_rows:
            if row["status"] != "READY_FOR_SHADOW":
                non_ready_rule_count += 1
                continue
            try:
                rule = MethodRule.model_validate_json(row["rule_json"])
            except Exception:
                non_ready_rule_count += 1
                continue
            rule_payload = rule.model_dump(mode="json")
            if rule_payload.get("status") == "READY_FOR_SHADOW":
                rules.append(rule_payload)
            else:
                non_ready_rule_count += 1
        return {
            "run_id": run_id,
            "formal_committee_weight_allowed": False,
            "skills": skills,
            "rules": rules,
            "non_ready_rule_count": non_ready_rule_count,
        }

    def _save_skill(
        self,
        connection: Any,
        kind: ReviewedSkillKind,
        skill: CandidateSelectionSkill | PositionLifecycleSkill,
        now: str,
    ) -> None:
        encoded = _model_json(skill)
        object_hash = sha256_bytes(encoded.encode("utf-8"))
        _insert_exact(
            connection,
            table="knowledge_reviewed_skill",
            key_column="skill_id",
            key_value=skill.skill_id,
            json_column="manifest_json",
            json_value=encoded,
            columns=(
                "skill_id",
                "run_id",
                "skill_kind",
                "skill_category",
                "coverage_state",
                "status",
                "manifest_object_hash",
                "manifest_json",
                "created_at",
            ),
            values=(
                skill.skill_id,
                skill.run_id,
                kind.value,
                skill.category.value,
                skill.coverage_state.value,
                skill.status.value,
                object_hash,
                encoded,
                now,
            ),
        )
        for ordinal, rule_id in enumerate(skill.rule_ids, start=1):
            _insert_exact_row(
                connection,
                table="knowledge_reviewed_skill_rule_ref",
                where="skill_id=? AND ref_ordinal=?",
                where_values=(skill.skill_id, ordinal),
                columns=("skill_id", "ref_ordinal", "rule_id"),
                values=(skill.skill_id, ordinal, rule_id),
            )
        self._save_au_refs(
            connection,
            table="knowledge_reviewed_skill_au_ref",
            owner_column="skill_id",
            owner_id=skill.skill_id,
            argument_ids=skill.source_argument_unit_ids,
        )

    @staticmethod
    def _save_au_refs(
        connection: Any,
        *,
        table: str,
        owner_column: str,
        owner_id: str,
        argument_ids: Iterable[str],
    ) -> None:
        for ordinal, argument_id in enumerate(argument_ids, start=1):
            _insert_exact_row(
                connection,
                table=table,
                where=f"{owner_column}=? AND ref_ordinal=?",
                where_values=(owner_id, ordinal),
                columns=(owner_column, "ref_ordinal", "argument_unit_id"),
                values=(owner_id, ordinal, argument_id),
            )

    @staticmethod
    def _save_single_json(
        connection: Any,
        *,
        table: str,
        key_column: str,
        key_value: str,
        run_id: str,
        object_column: str,
        json_column: str,
        model: BaseModel,
        extra_columns: tuple[str, ...],
        extra_values: tuple[Any, ...],
        now: str,
    ) -> None:
        encoded = _model_json(model)
        _insert_exact(
            connection,
            table=table,
            key_column=key_column,
            key_value=key_value,
            json_column=json_column,
            json_value=encoded,
            columns=(
                key_column,
                "run_id",
                *extra_columns,
                object_column,
                json_column,
                "created_at",
            ),
            values=(
                key_value,
                run_id,
                *extra_values,
                sha256_bytes(encoded.encode("utf-8")),
                encoded,
                now,
            ),
        )

    @staticmethod
    def _statistics(connection: Any, run_id: str) -> dict[str, int]:
        verdict_rows = connection.execute(
            "SELECT verdict,application_status,decision_json "
            "FROM knowledge_review_decision WHERE run_id=?",
            (run_id,),
        ).fetchall()
        decisions = [
            ReviewDecision.model_validate_json(row["decision_json"]) for row in verdict_rows
        ]
        skill_rows = connection.execute(
            "SELECT skill_kind,status,COUNT(*) AS count "
            "FROM knowledge_reviewed_skill WHERE run_id=? "
            "GROUP BY skill_kind,status",
            (run_id,),
        ).fetchall()
        skill_counts = {
            (str(row["skill_kind"]), str(row["status"])): int(row["count"]) for row in skill_rows
        }
        scalar_queries = {
            "reviewed_argument_count": (
                "SELECT COUNT(*) FROM knowledge_reviewed_argument_unit WHERE run_id=?"
            ),
            "viewpoint_card_count": (
                "SELECT COUNT(*) FROM knowledge_viewpoint_card WHERE run_id=?"
            ),
            "method_rule_count": ("SELECT COUNT(*) FROM knowledge_method_rule WHERE run_id=?"),
            "visual_participation_count": (
                "SELECT COUNT(DISTINCT argument_unit_id) "
                "FROM knowledge_reviewed_visual_ref WHERE run_id=?"
            ),
        }
        result = {
            key: int(connection.execute(sql, (run_id,)).fetchone()[0])
            for key, sql in scalar_queries.items()
        }
        result.update(
            {
                "review_record_count": len(decisions),
                "mapped_record_count": len(decisions),
                "pass_inherited_count": sum(
                    item.verdict.value == "PASS" and item.application_status.value == "APPLIED"
                    for item in decisions
                ),
                "rejected_excluded_count": sum(
                    item.application_status.value == "EXCLUDED" for item in decisions
                ),
                "needs_user_review_count": sum(
                    item.application_status.value == "NEEDS_USER_REVIEW" for item in decisions
                ),
                "candidate_selection_skill_count": sum(
                    count
                    for (kind, _), count in skill_counts.items()
                    if kind == ReviewedSkillKind.CANDIDATE_SELECTION.value
                ),
                "position_lifecycle_skill_count": sum(
                    count
                    for (kind, _), count in skill_counts.items()
                    if kind == ReviewedSkillKind.POSITION_LIFECYCLE.value
                ),
                "ready_for_shadow_count": sum(
                    count
                    for (_, status), count in skill_counts.items()
                    if status == "READY_FOR_SHADOW"
                ),
                "needs_user_review_skill_count": sum(
                    count
                    for (_, status), count in skill_counts.items()
                    if status == "NEEDS_USER_REVIEW"
                ),
            }
        )
        return result


def _model_json(model: BaseModel) -> str:
    return canonical_json_bytes(_stable_projection(model.model_dump(mode="json"))).decode("utf-8")


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _stable_projection(value: Any) -> Any:
    if isinstance(value, dict):
        preserve_created_at = {
            "locator_type",
            "char_start",
            "char_end",
            "source_snapshot_id",
        }.issubset(value)
        return {
            str(key): _stable_projection(child)
            for key, child in value.items()
            if key != "created_at" or preserve_created_at
        }
    if isinstance(value, list):
        return [_stable_projection(child) for child in value]
    return value


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _insert_exact(
    connection: Any,
    *,
    table: str,
    key_column: str,
    key_value: str,
    json_column: str,
    json_value: str,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
) -> None:
    row = connection.execute(
        f"SELECT {json_column} FROM {table} WHERE {key_column}=?",
        (key_value,),
    ).fetchone()
    if row is not None:
        if str(row[json_column]) != json_value:
            raise ValueError(f"{table} identity collision: {key_value}")
        return
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        values,
    )


def _insert_exact_row(
    connection: Any,
    *,
    table: str,
    where: str,
    where_values: tuple[Any, ...],
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    compare_columns: tuple[str, ...] | None = None,
) -> None:
    compared = compare_columns or columns
    row = connection.execute(
        f"SELECT {','.join(compared)} FROM {table} WHERE {where}",
        where_values,
    ).fetchone()
    expected = tuple(values[columns.index(column)] for column in compared)
    if row is not None:
        if tuple(row[column] for column in compared) != expected:
            raise ValueError(f"{table} exact-row collision")
        return
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        values,
    )


__all__ = [
    "ReviewedKnowledgeRepository",
    "ReviewedResult",
    "SourceMaterial",
    "SourceParagraphRecord",
    "SourceVisualRecord",
]
