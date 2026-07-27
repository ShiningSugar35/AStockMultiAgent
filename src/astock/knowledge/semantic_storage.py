"""Private-safe Parquet storage for semantic vectors and argument scores."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas import (
    BookMethodCategory,
    EmbeddingModelManifest,
    SemanticArgumentScore,
    SemanticEmbeddingContract,
    SemanticEmbeddingView,
    SemanticScreenDecision,
)


@dataclass(frozen=True, slots=True)
class SemanticVectorRecord:
    vector_id: str
    run_id: str
    embedding_manifest_id: str
    view: SemanticEmbeddingView
    entity_id: str
    item_id: str | None
    content_id: str | None
    source_snapshot_ids: tuple[str, ...]
    source_object_sha256s: tuple[str, ...]
    input_object_sha256: str
    vector: tuple[float, ...]
    token_count: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class SemanticArgumentLineage:
    item_id: str
    content_id: str
    source_snapshot_ids: tuple[str, ...]
    source_object_sha256s: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticParquetWrite:
    vectors_path: Path
    scores_path: Path
    vectors_sha256: str
    scores_sha256: str


class ParquetSemanticStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run_directory(
        self,
        author_source_id: str,
        run_id: str,
        embedding_manifest_id: str,
    ) -> Path:
        return (
            self.root
            / "knowledge_semantic"
            / f"author={quote(author_source_id, safe='-_.')}"
            / f"run={quote(run_id, safe='-_.')}"
            / f"embedding={quote(embedding_manifest_id, safe='-_.')}"
        )

    def write(
        self,
        *,
        author_source_id: str,
        run_id: str,
        embedding_manifest_id: str,
        vectors: list[SemanticVectorRecord],
        scores: list[SemanticArgumentScore],
        manifest: EmbeddingModelManifest,
        candidate_paragraph_ids: set[str],
        argument_unit_ids: set[str],
        argument_lineages: dict[str, SemanticArgumentLineage],
    ) -> SemanticParquetWrite:
        if not vectors:
            raise ValueError("semantic Parquet requires at least one vector")
        dimensions = {len(record.vector) for record in vectors}
        if len(dimensions) != 1 or next(iter(dimensions)) < 1:
            raise ValueError("semantic vectors must share one positive dimension")
        if manifest.manifest_id != embedding_manifest_id:
            raise ValueError("semantic manifest identity does not match Parquet")
        if next(iter(dimensions)) != manifest.dimension:
            raise ValueError("semantic vectors do not match manifest dimension")
        _validate_active_contract(
            run_id,
            manifest,
            vectors,
            scores,
            candidate_paragraph_ids,
            argument_unit_ids,
            argument_lineages,
        )
        directory = self.run_directory(author_source_id, run_id, embedding_manifest_id)
        vectors_path = directory / "vectors.parquet"
        scores_path = directory / "scores.parquet"
        vector_schema = pa.schema(
            [
                ("vector_id", pa.string()),
                ("run_id", pa.string()),
                ("embedding_manifest_id", pa.string()),
                ("view", pa.string()),
                ("entity_id", pa.string()),
                ("item_id", pa.string()),
                ("content_id", pa.string()),
                ("source_snapshot_ids", pa.list_(pa.string())),
                ("source_object_sha256s", pa.list_(pa.string())),
                ("input_object_sha256", pa.string()),
                ("vector", pa.list_(pa.float32(), next(iter(dimensions)))),
                ("token_count", pa.int64()),
                ("chunk_count", pa.int64()),
            ]
        )
        vector_rows = [
            {
                "vector_id": record.vector_id,
                "run_id": record.run_id,
                "embedding_manifest_id": record.embedding_manifest_id,
                "view": record.view.value,
                "entity_id": record.entity_id,
                "item_id": record.item_id,
                "content_id": record.content_id,
                "source_snapshot_ids": list(record.source_snapshot_ids),
                "source_object_sha256s": list(record.source_object_sha256s),
                "input_object_sha256": record.input_object_sha256,
                "vector": list(record.vector),
                "token_count": record.token_count,
                "chunk_count": record.chunk_count,
            }
            for record in vectors
        ]
        score_schema = pa.schema(
            [
                ("score_id", pa.string()),
                ("run_id", pa.string()),
                ("argument_unit_id", pa.string()),
                ("embedding_manifest_id", pa.string()),
                ("topic_relevance", pa.float64()),
                ("methodological_completeness", pa.float64()),
                ("category_scores_json", pa.string()),
                ("selected_categories", pa.list_(pa.string())),
                ("decision", pa.string()),
                ("reason_codes", pa.list_(pa.string())),
            ]
        )
        score_rows = [
            {
                "score_id": score.score_id,
                "run_id": score.run_id,
                "argument_unit_id": score.argument_unit_id,
                "embedding_manifest_id": score.embedding_manifest_id,
                "topic_relevance": score.topic_relevance,
                "methodological_completeness": score.methodological_completeness,
                "category_scores_json": canonical_json_bytes(
                    {
                        category.value: value
                        for category, value in score.category_scores.items()
                    }
                ).decode("utf-8"),
                "selected_categories": [
                    category.value for category in score.selected_categories
                ],
                "decision": score.decision.value,
                "reason_codes": score.reason_codes,
            }
            for score in scores
        ]
        _write_exact(
            vectors_path,
            pa.Table.from_pylist(vector_rows, schema=vector_schema),
        )
        _write_exact(
            scores_path,
            pa.Table.from_pylist(score_rows, schema=score_schema),
        )
        return SemanticParquetWrite(
            vectors_path=vectors_path,
            scores_path=scores_path,
            vectors_sha256=sha256_bytes(vectors_path.read_bytes()),
            scores_sha256=sha256_bytes(scores_path.read_bytes()),
        )


def _write_exact(path: Path, table: pa.Table) -> None:
    if path.is_file():
        existing = pq.read_table(path)
        if not existing.equals(table, check_metadata=False):
            raise ValueError(f"semantic Parquet collision: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_active_contract(
    run_id: str,
    manifest: EmbeddingModelManifest,
    vectors: list[SemanticVectorRecord],
    scores: list[SemanticArgumentScore],
    candidate_paragraph_ids: set[str],
    argument_unit_ids: set[str],
    argument_lineages: dict[str, SemanticArgumentLineage],
) -> None:
    if manifest.embedding_contract_version is not (
        SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
    ):
        raise ValueError("active semantic contract must be PARAGRAPH_AUX_ARGUMENT_FINAL_V3")
    required_views = {
        SemanticEmbeddingView.PARAGRAPH_CURRENT,
        SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
        SemanticEmbeddingView.ARGUMENT_UNIT,
        SemanticEmbeddingView.METHOD_PROTOTYPE,
    }
    if any(
        record.run_id != run_id
        or record.embedding_manifest_id != manifest.manifest_id
        for record in vectors
    ):
        raise ValueError("active semantic vectors cross run or manifest boundaries")
    if set(argument_lineages) != argument_unit_ids:
        raise ValueError("AU lineage mapping must match argument units exactly")
    for argument_unit_id, lineage in argument_lineages.items():
        if (
            not argument_unit_id.strip()
            or not lineage.item_id.strip()
            or not lineage.content_id.strip()
            or not _is_sorted_unique_nonempty(lineage.source_snapshot_ids)
            or not _is_sorted_unique_nonempty(lineage.source_object_sha256s)
            or any(
                not _is_sha256(digest)
                for digest in lineage.source_object_sha256s
            )
        ):
            raise ValueError("AU lineage mapping contains invalid provenance")
    if any(
        not _is_sha256(record.input_object_sha256)
        or len(record.source_snapshot_ids) != len(set(record.source_snapshot_ids))
        or len(record.source_object_sha256s) != len(set(record.source_object_sha256s))
        or any(not snapshot_id.strip() for snapshot_id in record.source_snapshot_ids)
        or any(
            not _is_sha256(digest)
            for digest in record.source_object_sha256s
        )
        for record in vectors
    ):
        raise ValueError("active semantic vector provenance hashes are invalid")
    for record in vectors:
        if record.view in {
            SemanticEmbeddingView.PARAGRAPH_CURRENT,
            SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
        } and (
            record.item_id is None
            or record.content_id is None
            or len(record.source_snapshot_ids) != 1
            or len(record.source_object_sha256s) != 1
        ):
            raise ValueError("paragraph vectors must carry one SourceItem snapshot and hash")
        if record.view is SemanticEmbeddingView.ARGUMENT_UNIT:
            lineage = argument_lineages.get(record.entity_id)
            if lineage is None or (
                record.item_id != lineage.item_id
                or record.content_id != lineage.content_id
                or record.source_snapshot_ids != lineage.source_snapshot_ids
                or record.source_object_sha256s != lineage.source_object_sha256s
            ):
                raise ValueError("AU vector does not match exact argument lineage")
        if record.view is SemanticEmbeddingView.METHOD_PROTOTYPE and (
            record.item_id is not None
            or record.content_id is not None
            or record.source_snapshot_ids
            or record.source_object_sha256s
        ):
            raise ValueError("method prototype vectors must not carry source snapshot lineage")
    if {record.view for record in vectors} != required_views:
        raise ValueError("active semantic Parquet has unexpected embedding views")
    vector_keys = [(record.view, record.entity_id) for record in vectors]
    if len(vector_keys) != len(set(vector_keys)):
        raise ValueError("active semantic vectors must be unique per view and entity")
    current_by_id = {
        record.entity_id: record
        for record in vectors
        if record.view is SemanticEmbeddingView.PARAGRAPH_CURRENT
    }
    context_by_id = {
        record.entity_id: record
        for record in vectors
        if record.view is SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT
    }
    if set(current_by_id) != candidate_paragraph_ids:
        raise ValueError("current paragraph vectors do not match candidate paragraphs")
    if set(context_by_id) != candidate_paragraph_ids:
        raise ValueError("local-context vectors do not match candidate paragraphs")
    if any(
        current_by_id[paragraph_id].item_id != context_by_id[paragraph_id].item_id
        or current_by_id[paragraph_id].content_id
        != context_by_id[paragraph_id].content_id
        or current_by_id[paragraph_id].source_snapshot_ids
        != context_by_id[paragraph_id].source_snapshot_ids
        or current_by_id[paragraph_id].source_object_sha256s
        != context_by_id[paragraph_id].source_object_sha256s
        for paragraph_id in candidate_paragraph_ids
    ):
        raise ValueError("paragraph views do not share exact SourceItem provenance")
    expected_prototypes = {
        f"method-prototype:{category.value}" for category in BookMethodCategory
    }
    actual_prototypes = {
        record.entity_id
        for record in vectors
        if record.view is SemanticEmbeddingView.METHOD_PROTOTYPE
    }
    if (
        manifest.method_prototype_count != len(expected_prototypes)
        or actual_prototypes != expected_prototypes
    ):
        raise ValueError("active semantic vectors require exactly 14 method prototypes")
    argument_vector_ids = {
        record.entity_id
        for record in vectors
        if record.view is SemanticEmbeddingView.ARGUMENT_UNIT
    }
    score_ids = [score.argument_unit_id for score in scores]
    if (
        argument_vector_ids != argument_unit_ids
        or set(score_ids) != argument_unit_ids
        or len(score_ids) != len(set(score_ids))
    ):
        raise ValueError("active semantic scores must map one-to-one to AU vectors")
    if len(vectors) != (
        2 * len(candidate_paragraph_ids)
        + len(argument_unit_ids)
        + manifest.method_prototype_count
    ):
        raise ValueError("active semantic vector cardinality is not 2P+A+14")
    for score in scores:
        if (
            score.run_id != run_id
            or score.embedding_manifest_id != manifest.manifest_id
            or set(score.category_scores) != set(BookMethodCategory)
        ):
            raise ValueError("active AU score lineage or category coverage is incomplete")
        if (
            manifest.calibration_manifest_sha256 is None
            and score.decision is SemanticScreenDecision.EXCLUDE_DERIVED
        ):
            raise ValueError("uncalibrated AU scores cannot auto-exclude")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_sorted_unique_nonempty(values: tuple[str, ...]) -> bool:
    return (
        bool(values)
        and values == tuple(sorted(values))
        and len(values) == len(set(values))
        and all(value.strip() for value in values)
    )


__all__ = [
    "ParquetSemanticStore",
    "SemanticArgumentLineage",
    "SemanticParquetWrite",
    "SemanticVectorRecord",
]
