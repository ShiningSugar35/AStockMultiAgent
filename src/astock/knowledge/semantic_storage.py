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
from astock.schemas import SemanticArgumentScore, SemanticEmbeddingView


@dataclass(frozen=True, slots=True)
class SemanticVectorRecord:
    vector_id: str
    run_id: str
    embedding_manifest_id: str
    view: SemanticEmbeddingView
    entity_id: str
    item_id: str | None
    source_snapshot_ids: tuple[str, ...]
    input_object_sha256: str
    vector: tuple[float, ...]
    token_count: int
    chunk_count: int


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
    ) -> SemanticParquetWrite:
        if not vectors:
            raise ValueError("semantic Parquet requires at least one vector")
        dimensions = {len(record.vector) for record in vectors}
        if len(dimensions) != 1 or next(iter(dimensions)) < 1:
            raise ValueError("semantic vectors must share one positive dimension")
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
                ("source_snapshot_ids", pa.list_(pa.string())),
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
                "source_snapshot_ids": list(record.source_snapshot_ids),
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


__all__ = [
    "ParquetSemanticStore",
    "SemanticParquetWrite",
    "SemanticVectorRecord",
]
