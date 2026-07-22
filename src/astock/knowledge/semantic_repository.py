"""SQLite metadata repository for the argument-aware semantic funnel."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.knowledge.semantic_funnel import ParagraphizedContent
from astock.schemas import (
    ArgumentRelation,
    ArgumentUnit,
    EmbeddingModelManifest,
    ParagraphUnit,
    SemanticFunnelRun,
    SemanticLlmBatch,
    SemanticLlmBatchStatus,
    SemanticRunStage,
    SemanticSkillCandidate,
)


@dataclass(frozen=True, slots=True)
class SemanticEmbeddingRegistration:
    manifest: EmbeddingModelManifest
    vector_parquet_sha256: str
    score_parquet_sha256: str
    manifest_object_sha256: str

_STAGE_ORDER = {
    SemanticRunStage.PLANNED: 0,
    SemanticRunStage.INPUT_FROZEN: 1,
    SemanticRunStage.PARAGRAPHIZED: 2,
    SemanticRunStage.KEYWORD_SCREENED: 3,
    SemanticRunStage.ARGUMENT_UNITS_BUILT: 4,
    SemanticRunStage.EMBEDDING_READY: 5,
    SemanticRunStage.EMBEDDING_SCREENED: 6,
    SemanticRunStage.DEEPSEEK_PACKET_READY: 7,
    SemanticRunStage.DEEPSEEK_RESULT_STAGED: 8,
    SemanticRunStage.IMPORT_VALIDATED: 9,
    SemanticRunStage.CANDIDATES_GENERATED: 10,
    SemanticRunStage.AUDITED: 11,
    SemanticRunStage.PENDING_HUMAN_REVIEW: 12,
    SemanticRunStage.FAILED: 99,
}


class SemanticFunnelRepository:
    """Persist only private-safe metadata after immutable objects exist."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_run(self, run_id: str) -> SemanticFunnelRun | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_semantic_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return SemanticFunnelRun.model_validate_json(row["run_json"]) if row else None

    def latest_run(self, author_source_id: str) -> SemanticFunnelRun | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_semantic_run "
                "WHERE author_source_id=? ORDER BY started_at DESC,run_id DESC LIMIT 1",
                (author_source_id,),
            ).fetchone()
        return SemanticFunnelRun.model_validate_json(row["run_json"]) if row else None

    def latest_completed_run(self, author_source_id: str) -> SemanticFunnelRun | None:
        """Return the latest run with a durable argument-unit stage.

        A newer interrupted run must not hide the last usable result in status
        output.  FAILED and pre-materialization stages are intentionally omitted.
        """

        excluded = (
            SemanticRunStage.PLANNED,
            SemanticRunStage.INPUT_FROZEN,
            SemanticRunStage.PARAGRAPHIZED,
            SemanticRunStage.KEYWORD_SCREENED,
            SemanticRunStage.FAILED,
        )
        placeholders = ",".join("?" for _ in excluded)
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_semantic_run "
                f"WHERE author_source_id=? AND stage NOT IN ({placeholders}) "
                "ORDER BY started_at DESC,run_id DESC LIMIT 1",
                (author_source_id, *(stage.value for stage in excluded)),
            ).fetchone()
        return SemanticFunnelRun.model_validate_json(row["run_json"]) if row else None

    def save_run(self, run: SemanticFunnelRun) -> SemanticFunnelRun:
        encoded = _model_json(run)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_semantic_run WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO knowledge_semantic_run("
                    "run_id,author_source_id,input_manifest_hash,pipeline_version,stage,"
                    "run_json,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run.run_id,
                        run.author_source_id,
                        run.input_manifest_sha256,
                        run.pipeline_version,
                        run.stage.value,
                        encoded,
                        _utc_text(run.started_at),
                        _utc_text(run.finished_at) if run.finished_at else None,
                    ),
                )
                return run
            existing = SemanticFunnelRun.model_validate_json(row["run_json"])
            _validate_same_run_contract(existing, run)
            if (
                run.stage is not SemanticRunStage.FAILED
                and existing.stage is not SemanticRunStage.FAILED
                and _STAGE_ORDER[run.stage] < _STAGE_ORDER[existing.stage]
            ):
                raise ValueError("semantic run stage cannot move backwards")
            connection.execute(
                "UPDATE knowledge_semantic_run SET stage=?,run_json=?,finished_at=? "
                "WHERE run_id=?",
                (
                    run.stage.value,
                    encoded,
                    _utc_text(run.finished_at) if run.finished_at else None,
                    run.run_id,
                ),
            )
        return run

    def register_paragraphized(
        self,
        run: SemanticFunnelRun,
        contents: Sequence[ParagraphizedContent],
    ) -> None:
        """Atomically register one already-materialized semantic stage."""

        if run.stage is not SemanticRunStage.ARGUMENT_UNITS_BUILT:
            raise ValueError("paragraphized registration requires ARGUMENT_UNITS_BUILT")
        expected_items = len(contents)
        expected_paragraphs = sum(len(content.paragraphs) for content in contents)
        expected_arguments = sum(len(content.argument_units) for content in contents)
        if (
            run.content_item_count,
            run.paragraph_count,
            run.argument_unit_count,
        ) != (expected_items, expected_paragraphs, expected_arguments):
            raise ValueError("semantic run counters do not match its materialized contents")
        if len({content.item.item_id for content in contents}) != len(contents):
            raise ValueError("semantic run content item ids must be unique")
        _validate_paragraphized_contract(run, contents)
        encoded_run = _model_json(run)
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT run_json FROM knowledge_semantic_run WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO knowledge_semantic_run("
                    "run_id,author_source_id,input_manifest_hash,pipeline_version,stage,"
                    "run_json,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run.run_id,
                        run.author_source_id,
                        run.input_manifest_sha256,
                        run.pipeline_version,
                        run.stage.value,
                        encoded_run,
                        _utc_text(run.started_at),
                        _utc_text(run.finished_at) if run.finished_at else None,
                    ),
                )
            else:
                existing = SemanticFunnelRun.model_validate_json(row["run_json"])
                _validate_same_run_contract(existing, run)
                if existing.stage not in {
                    SemanticRunStage.INPUT_FROZEN,
                    SemanticRunStage.PARAGRAPHIZED,
                    SemanticRunStage.KEYWORD_SCREENED,
                    SemanticRunStage.ARGUMENT_UNITS_BUILT,
                }:
                    raise ValueError("semantic run is outside the argument build stage")
                connection.execute(
                    "UPDATE knowledge_semantic_run SET stage=?,run_json=?,finished_at=? "
                    "WHERE run_id=?",
                    (
                        run.stage.value,
                        encoded_run,
                        _utc_text(run.finished_at) if run.finished_at else None,
                        run.run_id,
                    ),
                )
            for content in contents:
                if content.item.run_id != run.run_id:
                    raise ValueError("semantic item belongs to another run")
                item_json = _model_json(content.item)
                _insert_exact(
                    connection,
                    table="knowledge_semantic_content_item",
                    key_name="item_id",
                    key_value=content.item.item_id,
                    json_name="item_json",
                    json_value=item_json,
                    columns=(
                        "item_id",
                        "run_id",
                        "author_source_id",
                        "content_type",
                        "content_id",
                        "content_version_id",
                        "source_snapshot_id",
                        "source_object_hash",
                        "normalized_object_hash",
                        "paragraph_count",
                        "item_json",
                        "created_at",
                    ),
                    values=(
                        content.item.item_id,
                        run.run_id,
                        content.item.author_source_id,
                        content.item.content_type,
                        content.item.content_id,
                        content.item.content_version_id,
                        content.item.source_snapshot_id,
                        content.item.source_object_sha256,
                        content.item.normalized_object_sha256,
                        len(content.paragraphs),
                        item_json,
                        now,
                    ),
                )
                for paragraph in content.paragraphs:
                    paragraph_json = _model_json(paragraph)
                    _insert_exact(
                        connection,
                        table="knowledge_paragraph_unit",
                        key_name="paragraph_id",
                        key_value=paragraph.paragraph_id,
                        json_name="unit_json",
                        json_value=paragraph_json,
                        columns=(
                            "paragraph_id",
                            "run_id",
                            "item_id",
                            "author_source_id",
                            "content_id",
                            "ordinal",
                            "text_object_hash",
                            "primary_role",
                            "standalone_distillable",
                            "merge_action",
                            "unit_json",
                            "created_at",
                        ),
                        values=(
                            paragraph.paragraph_id,
                            run.run_id,
                            content.item.item_id,
                            paragraph.author_source_id,
                            paragraph.content_id,
                            paragraph.ordinal,
                            paragraph.text_object_sha256,
                            paragraph.primary_role.value,
                            int(paragraph.standalone_distillable),
                            paragraph.merge_action.value,
                            paragraph_json,
                            now,
                        ),
                    )
                screen_json = _model_json(content.screen)
                _insert_exact(
                    connection,
                    table="knowledge_keyword_screen",
                    key_name="screen_id",
                    key_value=content.screen.screen_id,
                    json_name="screen_json",
                    json_value=screen_json,
                    columns=(
                        "screen_id",
                        "run_id",
                        "item_id",
                        "rule_version",
                        "decision",
                        "result_object_hash",
                        "screen_json",
                        "created_at",
                    ),
                    values=(
                        content.screen.screen_id,
                        run.run_id,
                        content.item.item_id,
                        content.screen.keyword_rule_version,
                        content.screen.decision.value,
                        content.screen.result_object_sha256,
                        screen_json,
                        now,
                    ),
                )
                for relation in content.relations:
                    relation_json = _model_json(relation)
                    _insert_exact(
                        connection,
                        table="knowledge_argument_relation",
                        key_name="relation_id",
                        key_value=relation.relation_id,
                        json_name="relation_json",
                        json_value=relation_json,
                        columns=(
                            "relation_id",
                            "run_id",
                            "item_id",
                            "source_paragraph_id",
                            "target_paragraph_id",
                            "relation_type",
                            "relation_json",
                            "created_at",
                        ),
                        values=(
                            relation.relation_id,
                            run.run_id,
                            content.item.item_id,
                            relation.source_paragraph_id,
                            relation.target_paragraph_id,
                            relation.relation_type.value,
                            relation_json,
                            now,
                        ),
                    )
                paragraphs_by_id = {
                    paragraph.paragraph_id: paragraph for paragraph in content.paragraphs
                }
                for argument in content.argument_units:
                    argument_json = _model_json(argument)
                    _insert_exact(
                        connection,
                        table="knowledge_argument_unit",
                        key_name="argument_unit_id",
                        key_value=argument.argument_unit_id,
                        json_name="unit_json",
                        json_value=argument_json,
                        columns=(
                            "argument_unit_id",
                            "run_id",
                            "item_id",
                            "author_source_id",
                            "content_id",
                            "start_ordinal",
                            "end_ordinal",
                            "text_object_hash",
                            "status",
                            "topic_relevance",
                            "methodological_completeness",
                            "unit_json",
                            "created_at",
                        ),
                        values=(
                            argument.argument_unit_id,
                            run.run_id,
                            content.item.item_id,
                            argument.author_source_id,
                            argument.content_id,
                            argument.start_ordinal,
                            argument.end_ordinal,
                            argument.text_object_sha256,
                            argument.status.value,
                            argument.topic_relevance,
                            argument.methodological_completeness,
                            argument_json,
                            now,
                        ),
                    )
                    for ordinal, paragraph_id in enumerate(argument.paragraph_ids, start=1):
                        paragraph = paragraphs_by_id[paragraph_id]
                        connection.execute(
                            "INSERT INTO knowledge_argument_unit_paragraph_ref("
                            "argument_unit_id,ordinal,paragraph_id,rhetorical_role) "
                            "VALUES(?,?,?,?) ON CONFLICT(argument_unit_id,ordinal) DO "
                            "UPDATE SET paragraph_id=excluded.paragraph_id,"
                            "rhetorical_role=excluded.rhetorical_role",
                            (
                                argument.argument_unit_id,
                                ordinal,
                                paragraph_id,
                                paragraph.primary_role.value,
                            ),
                        )

    def counts(self, run_id: str) -> dict[str, int]:
        with self.state.connect() as connection:
            item_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_semantic_content_item WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            paragraph_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_paragraph_unit WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            argument_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_argument_unit WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        return {
            "content_item_count": int(item_count),
            "paragraph_count": int(paragraph_count),
            "argument_unit_count": int(argument_count),
        }

    def summary(self, run_id: str) -> dict[str, int]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM knowledge_argument_unit "
                "WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
            screen_rows = connection.execute(
                "SELECT decision,COUNT(*) AS count FROM knowledge_keyword_screen "
                "WHERE run_id=? GROUP BY decision",
                (run_id,),
            ).fetchall()
        arguments = {str(row["status"]): int(row["count"]) for row in rows}
        screens = {str(row["decision"]): int(row["count"]) for row in screen_rows}
        return {
            "candidate_item_count": screens.get("CANDIDATE", 0),
            "excluded_item_count": screens.get("EXCLUDED_DERIVED", 0),
            "ready_argument_count": arguments.get("READY", 0),
            "review_argument_count": arguments.get("NEEDS_REVIEW", 0),
            "excluded_argument_count": arguments.get("DERIVED_EXCLUDED", 0),
        }

    def paragraph_groups(
        self,
        run_id: str,
        *,
        candidate_only: bool = False,
    ) -> dict[str, list[ParagraphUnit]]:
        with self.state.connect() as connection:
            if candidate_only:
                rows = connection.execute(
                    "SELECT p.item_id,p.unit_json FROM knowledge_paragraph_unit p "
                    "JOIN knowledge_keyword_screen s "
                    "ON s.run_id=p.run_id AND s.item_id=p.item_id "
                    "WHERE p.run_id=? AND s.decision='CANDIDATE' "
                    "ORDER BY p.item_id,p.ordinal",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT item_id,unit_json FROM knowledge_paragraph_unit "
                    "WHERE run_id=? ORDER BY item_id,ordinal",
                    (run_id,),
                ).fetchall()
        groups: dict[str, list[ParagraphUnit]] = {}
        for row in rows:
            groups.setdefault(str(row["item_id"]), []).append(
                ParagraphUnit.model_validate_json(row["unit_json"])
            )
        return groups

    def argument_units(self, run_id: str) -> list[ArgumentUnit]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT unit_json FROM knowledge_argument_unit WHERE run_id=? "
                "ORDER BY item_id,start_ordinal,argument_unit_id",
                (run_id,),
            ).fetchall()
        return [ArgumentUnit.model_validate_json(row["unit_json"]) for row in rows]

    def argument_relations(self, run_id: str) -> list[ArgumentRelation]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT relation_json FROM knowledge_argument_relation WHERE run_id=? "
                "ORDER BY relation_id",
                (run_id,),
            ).fetchall()
        return [ArgumentRelation.model_validate_json(row["relation_json"]) for row in rows]

    def embedding_registration(
        self,
        run_id: str,
    ) -> SemanticEmbeddingRegistration | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT manifest_json,vector_parquet_hash,score_parquet_hash,"
                "manifest_object_hash FROM knowledge_embedding_manifest "
                "WHERE run_id=? ORDER BY created_at DESC,manifest_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return SemanticEmbeddingRegistration(
            manifest=EmbeddingModelManifest.model_validate_json(row["manifest_json"]),
            vector_parquet_sha256=str(row["vector_parquet_hash"]),
            score_parquet_sha256=str(row["score_parquet_hash"]),
            manifest_object_sha256=str(row["manifest_object_hash"]),
        )

    def register_embedding(
        self,
        run: SemanticFunnelRun,
        manifest: EmbeddingModelManifest,
        *,
        vector_parquet_hash: str,
        score_parquet_hash: str,
        manifest_object_hash: str,
    ) -> None:
        if run.stage not in {
            SemanticRunStage.ARGUMENT_UNITS_BUILT,
            SemanticRunStage.EMBEDDING_READY,
            SemanticRunStage.EMBEDDING_SCREENED,
        }:
            raise ValueError("semantic run cannot register embeddings at its current stage")
        manifest_json = _model_json(manifest)
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT manifest_json,vector_parquet_hash,score_parquet_hash,"
                "manifest_object_hash FROM knowledge_embedding_manifest "
                "WHERE manifest_id=?",
                (manifest.manifest_id,),
            ).fetchone()
            expected = (
                manifest_json,
                vector_parquet_hash,
                score_parquet_hash,
                manifest_object_hash,
            )
            if row is not None:
                actual = (
                    str(row["manifest_json"]),
                    str(row["vector_parquet_hash"]),
                    str(row["score_parquet_hash"]),
                    str(row["manifest_object_hash"]),
                )
                if actual != expected:
                    raise ValueError(f"semantic embedding collision: {manifest.manifest_id}")
            else:
                connection.execute(
                    "INSERT INTO knowledge_embedding_manifest("
                    "manifest_id,run_id,model_id,model_asset_hash,tokenizer_asset_hash,"
                    "vector_parquet_hash,score_parquet_hash,manifest_object_hash,"
                    "manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        manifest.manifest_id,
                        run.run_id,
                        manifest.model_id,
                        manifest.model_asset_sha256,
                        manifest.tokenizer_asset_sha256,
                        vector_parquet_hash,
                        score_parquet_hash,
                        manifest_object_hash,
                        manifest_json,
                        now,
                    ),
                )
            _advance_run_in_transaction(
                connection,
                run.run_id,
                SemanticRunStage.EMBEDDING_SCREENED,
            )

    def get_llm_batch(self, batch_id: str) -> SemanticLlmBatch | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT batch_json FROM knowledge_llm_batch WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        return SemanticLlmBatch.model_validate_json(row["batch_json"]) if row else None

    def register_llm_batch(self, batch: SemanticLlmBatch) -> None:
        batch_json = _model_json(batch)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT batch_json FROM knowledge_llm_batch WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
            if row is not None:
                existing = SemanticLlmBatch.model_validate_json(row["batch_json"])
                if existing == batch:
                    return
                if (
                    existing.run_id != batch.run_id
                    or existing.prompt_sha256 != batch.prompt_sha256
                    or existing.result_schema_sha256 != batch.result_schema_sha256
                    or existing.input_manifest_sha256 != batch.input_manifest_sha256
                    or existing.packet_object_sha256 != batch.packet_object_sha256
                ):
                    raise ValueError(f"semantic LLM batch collision: {batch.batch_id}")
                allowed = {
                    SemanticLlmBatchStatus.PACKET_READY: {
                        SemanticLlmBatchStatus.RESULT_STAGED,
                        SemanticLlmBatchStatus.REJECTED,
                    },
                    SemanticLlmBatchStatus.RESULT_STAGED: {
                        SemanticLlmBatchStatus.IMPORTED,
                        SemanticLlmBatchStatus.REJECTED,
                    },
                    SemanticLlmBatchStatus.IMPORTED: set(),
                    SemanticLlmBatchStatus.REJECTED: set(),
                }
                if batch.status not in allowed[existing.status]:
                    raise ValueError("semantic LLM batch status cannot move backwards")
                if (
                    existing.response_object_sha256 is not None
                    and existing.response_object_sha256 != batch.response_object_sha256
                ):
                    raise ValueError("semantic LLM batch response is immutable once staged")
                if existing.status is batch.status:
                    if (
                        existing.response_object_sha256 == batch.response_object_sha256
                        and existing.imported_result_count == batch.imported_result_count
                    ):
                        return
                    raise ValueError("semantic LLM batch same-stage metadata changed")
                if batch.status in {
                    SemanticLlmBatchStatus.RESULT_STAGED,
                    SemanticLlmBatchStatus.IMPORTED,
                } and (
                    batch.response_object_sha256 is None
                    or batch.imported_result_count != batch.exported_argument_count
                ):
                    raise ValueError("semantic LLM batch result set is incomplete")
                connection.execute(
                    "UPDATE knowledge_llm_batch SET response_object_hash=?,status=?,"
                    "batch_json=?,updated_at=? WHERE batch_id=?",
                    (
                        batch.response_object_sha256,
                        batch.status.value,
                        batch_json,
                        _utc_text(batch.updated_at),
                        batch.batch_id,
                    ),
                )
                return
            connection.execute(
                "INSERT INTO knowledge_llm_batch("
                "batch_id,run_id,provider,model_id,prompt_hash,schema_hash,"
                "input_manifest_hash,packet_object_hash,response_object_hash,status,"
                "batch_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch.batch_id,
                    batch.run_id,
                    batch.provider,
                    batch.model_id,
                    batch.prompt_sha256,
                    batch.result_schema_sha256,
                    batch.input_manifest_sha256,
                    batch.packet_object_sha256,
                    batch.response_object_sha256,
                    batch.status.value,
                    batch_json,
                    _utc_text(batch.created_at),
                    _utc_text(batch.updated_at),
                ),
            )

    def import_candidates(
        self,
        batch: SemanticLlmBatch,
        candidates: Sequence[SemanticSkillCandidate],
    ) -> None:
        if batch.status.value != "IMPORTED":
            raise ValueError("semantic candidate import requires an IMPORTED batch")
        now = _utc_text(datetime.now(UTC))
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT batch_json FROM knowledge_llm_batch WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(batch.batch_id)
            existing_batch = SemanticLlmBatch.model_validate_json(row["batch_json"])
            if (
                existing_batch.run_id != batch.run_id
                or existing_batch.packet_object_sha256 != batch.packet_object_sha256
                or existing_batch.response_object_sha256 != batch.response_object_sha256
            ):
                raise ValueError("semantic candidate batch contract changed")
            if (
                existing_batch.status is not SemanticLlmBatchStatus.RESULT_STAGED
                or existing_batch.response_object_sha256 is None
                or existing_batch.imported_result_count
                != existing_batch.exported_argument_count
                or batch.imported_result_count != batch.exported_argument_count
            ):
                raise ValueError("semantic candidate import requires a complete staged batch")
            for candidate in candidates:
                if candidate.run_id != batch.run_id:
                    raise ValueError("semantic candidate belongs to another run")
                if (
                    candidate.llm_batch_id != batch.batch_id
                    or candidate.llm_response_object_sha256
                    != batch.response_object_sha256
                ):
                    raise ValueError("semantic candidate LLM provenance does not match batch")
                placeholders = ",".join("?" for _ in candidate.argument_unit_ids)
                rows = connection.execute(
                    "SELECT argument_unit_id,author_source_id FROM knowledge_argument_unit "
                    f"WHERE run_id=? AND argument_unit_id IN ({placeholders})",
                    (batch.run_id, *candidate.argument_unit_ids),
                ).fetchall()
                if {str(item["argument_unit_id"]) for item in rows} != set(
                    candidate.argument_unit_ids
                ) or any(
                    str(item["author_source_id"]) != candidate.author_source_id
                    for item in rows
                ):
                    raise ValueError(
                        "semantic candidate contains cross-run or cross-author AU refs"
                    )
                candidate_json = _model_json(candidate)
                _insert_exact(
                    connection,
                    table="knowledge_semantic_candidate",
                    key_name="candidate_id",
                    key_value=candidate.candidate_id,
                    json_name="candidate_json",
                    json_value=candidate_json,
                    columns=(
                        "candidate_id",
                        "run_id",
                        "author_source_id",
                        "payload_object_hash",
                        "evaluation_status",
                        "approval_status",
                        "llm_batch_id",
                        "llm_response_object_hash",
                        "candidate_json",
                        "created_at",
                    ),
                    values=(
                        candidate.candidate_id,
                        candidate.run_id,
                        candidate.author_source_id,
                        candidate.payload_object_sha256,
                        candidate.evaluation_status.value,
                        candidate.approval_status.value,
                        candidate.llm_batch_id,
                        candidate.llm_response_object_sha256,
                        candidate_json,
                        now,
                    ),
                )
                for ordinal, argument_unit_id in enumerate(
                    candidate.argument_unit_ids,
                    start=1,
                ):
                    connection.execute(
                        "INSERT INTO knowledge_semantic_candidate_au_ref("
                        "candidate_id,ordinal,argument_unit_id) VALUES(?,?,?) "
                        "ON CONFLICT(candidate_id,ordinal) DO UPDATE SET "
                        "argument_unit_id=excluded.argument_unit_id",
                        (candidate.candidate_id, ordinal, argument_unit_id),
                    )
            batch_json = _model_json(batch)
            connection.execute(
                "UPDATE knowledge_llm_batch SET response_object_hash=?,status=?,"
                "batch_json=?,updated_at=? WHERE batch_id=?",
                (
                    batch.response_object_sha256,
                    batch.status.value,
                    batch_json,
                    _utc_text(batch.updated_at),
                    batch.batch_id,
                ),
            )
            _advance_run_in_transaction(
                connection,
                batch.run_id,
                SemanticRunStage.CANDIDATES_GENERATED,
            )

    def finalize_imported_batch(self, batch: SemanticLlmBatch) -> int:
        """Repair an interrupted post-import run transition idempotently."""

        if batch.status is not SemanticLlmBatchStatus.IMPORTED:
            raise ValueError("only imported semantic batches can be finalized")
        with self.state.transaction() as connection:
            batch_row = connection.execute(
                "SELECT batch_json FROM knowledge_llm_batch WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
            if batch_row is None:
                raise KeyError(batch.batch_id)
            persisted = SemanticLlmBatch.model_validate_json(batch_row["batch_json"])
            if (
                persisted != batch
                or persisted.status is not SemanticLlmBatchStatus.IMPORTED
                or persisted.response_object_sha256 is None
                or persisted.imported_result_count != persisted.exported_argument_count
            ):
                raise ValueError("persisted semantic batch is not a complete import")
            _advance_run_in_transaction(
                connection,
                batch.run_id,
                SemanticRunStage.CANDIDATES_GENERATED,
            )
            row = connection.execute(
                "SELECT COUNT(*) FROM knowledge_semantic_candidate WHERE llm_batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        return int(row[0]) if row else 0


def _insert_exact(
    connection: object,
    *,
    table: str,
    key_name: str,
    key_value: str,
    json_name: str,
    json_value: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    cursor = connection  # sqlite3.Connection; kept structural for narrow repository use.
    row = cursor.execute(  # type: ignore[attr-defined]
        f"SELECT {json_name} FROM {table} WHERE {key_name}=?",
        (key_value,),
    ).fetchone()
    if row is not None:
        if str(row[json_name]) != json_value:
            raise ValueError(f"semantic metadata collision: {key_value}")
        return
    placeholders = ",".join("?" for _ in columns)
    cursor.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        values,
    )


def _advance_run_in_transaction(
    connection: object,
    run_id: str,
    target_stage: SemanticRunStage,
) -> None:
    cursor = connection  # sqlite3.Connection; structural use kept private.
    row = cursor.execute(  # type: ignore[attr-defined]
        "SELECT run_json FROM knowledge_semantic_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(run_id)
    current = SemanticFunnelRun.model_validate_json(row["run_json"])
    if current.stage is SemanticRunStage.FAILED:
        raise ValueError("failed semantic run cannot be advanced")
    if _STAGE_ORDER[current.stage] >= _STAGE_ORDER[target_stage]:
        return
    advanced = current.model_copy(update={"stage": target_stage})
    cursor.execute(  # type: ignore[attr-defined]
        "UPDATE knowledge_semantic_run SET stage=?,run_json=? WHERE run_id=?",
        (target_stage.value, _model_json(advanced), run_id),
    )


def _validate_paragraphized_contract(
    run: SemanticFunnelRun,
    contents: Sequence[ParagraphizedContent],
) -> None:
    all_paragraph_ids: set[str] = set()
    all_relation_ids: set[str] = set()
    all_argument_ids: set[str] = set()
    for content in contents:
        item = content.item
        if item.run_id != run.run_id or item.author_source_id != run.author_source_id:
            raise ValueError("semantic item run or author does not match its run")
        paragraphs = list(content.paragraphs)
        paragraph_ids = [paragraph.paragraph_id for paragraph in paragraphs]
        if item.paragraph_ids != paragraph_ids:
            raise ValueError("semantic item paragraph projection is inconsistent")
        if [paragraph.ordinal for paragraph in paragraphs] != list(
            range(1, len(paragraphs) + 1)
        ):
            raise ValueError("semantic item paragraph ordinals must be contiguous")
        if all_paragraph_ids.intersection(paragraph_ids):
            raise ValueError("semantic paragraph cannot belong to multiple items")
        all_paragraph_ids.update(paragraph_ids)
        paragraph_by_id = {paragraph.paragraph_id: paragraph for paragraph in paragraphs}
        for paragraph in paragraphs:
            if (
                paragraph.run_id != run.run_id
                or paragraph.author_source_id != run.author_source_id
                or paragraph.content_type != item.content_type
                or paragraph.content_id != item.content_id
                or paragraph.content_version_id != item.content_version_id
                or paragraph.locator.content_id != item.content_id
                or paragraph.locator.source_snapshot_id != item.source_snapshot_id
            ):
                raise ValueError("semantic paragraph projection is inconsistent")
        screen = content.screen
        if (
            screen.run_id != run.run_id
            or screen.item_id != item.item_id
            or screen.keyword_rule_version != run.keyword_rule_version
            or not set(screen.matched_paragraph_ids).issubset(paragraph_by_id)
        ):
            raise ValueError("semantic keyword screen projection is inconsistent")
        relation_by_id = {relation.relation_id: relation for relation in content.relations}
        if len(relation_by_id) != len(content.relations):
            raise ValueError("semantic relation ids must be globally unique")
        if all_relation_ids.intersection(relation_by_id):
            raise ValueError("semantic relation cannot belong to multiple items")
        all_relation_ids.update(relation_by_id)
        for relation in content.relations:
            if (
                relation.run_id != run.run_id
                or relation.content_id != item.content_id
                or relation.relation_rule_version != run.relation_rule_version
                or relation.source_paragraph_id not in paragraph_by_id
                or relation.target_paragraph_id not in paragraph_by_id
            ):
                raise ValueError("semantic relation crosses its item contract")
        flattened: list[str] = []
        for argument in content.argument_units:
            if argument.argument_unit_id in all_argument_ids:
                raise ValueError("semantic argument id belongs to multiple items")
            all_argument_ids.add(argument.argument_unit_id)
            if (
                argument.run_id != run.run_id
                or argument.author_source_id != run.author_source_id
                or argument.content_type != item.content_type
                or argument.content_id != item.content_id
                or argument.source_snapshot_ids != [item.source_snapshot_id]
                or argument.builder_version != run.argument_builder_version
                or not set(argument.paragraph_ids).issubset(paragraph_by_id)
                or not set(argument.relation_ids).issubset(relation_by_id)
            ):
                raise ValueError("semantic argument projection is inconsistent")
            actual_ordinals = [
                paragraph_by_id[paragraph_id].ordinal
                for paragraph_id in argument.paragraph_ids
            ]
            if actual_ordinals != list(
                range(argument.start_ordinal, argument.end_ordinal + 1)
            ):
                raise ValueError("semantic argument paragraph range is inconsistent")
            for relation_id in argument.relation_ids:
                relation = relation_by_id[relation_id]
                if not {
                    relation.source_paragraph_id,
                    relation.target_paragraph_id,
                }.issubset(argument.paragraph_ids):
                    raise ValueError("semantic argument relation crosses its boundary")
            flattened.extend(argument.paragraph_ids)
        if screen.decision.value == "CANDIDATE" and flattened != paragraph_ids:
            raise ValueError("candidate item arguments must partition its paragraphs")
        if screen.decision.value == "EXCLUDED_DERIVED" and content.argument_units:
            raise ValueError("keyword-excluded item cannot contain arguments")


def _validate_same_run_contract(
    existing: SemanticFunnelRun,
    incoming: SemanticFunnelRun,
) -> None:
    immutable_fields = (
        "run_id",
        "author_source_id",
        "input_hashes",
        "input_manifest_sha256",
        "pipeline_version",
        "paragraphizer_version",
        "role_rule_version",
        "relation_rule_version",
        "argument_builder_version",
        "keyword_rule_version",
        "rule_config_sha256",
        "started_at",
    )
    if any(getattr(existing, field) != getattr(incoming, field) for field in immutable_fields):
        raise ValueError(f"semantic run collision: {incoming.run_id}")


def _model_json(value: object) -> str:
    return canonical_json_bytes(value.model_dump(mode="json")).decode("utf-8")  # type: ignore[attr-defined]


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


__all__ = ["SemanticEmbeddingRegistration", "SemanticFunnelRepository"]
