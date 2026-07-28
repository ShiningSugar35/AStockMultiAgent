"""Deterministic private Parquet storage for reviewed semantic artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, sha256_bytes


@dataclass(frozen=True, slots=True)
class ReviewedVectorRow:
    entity_id: str
    entity_kind: str
    input_object_sha256: str
    vector: tuple[float, ...]
    token_count: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class ReviewedScoreRow:
    argument_unit_id: str
    topic_relevance: float
    methodological_completeness: float
    category_scores: dict[str, float]
    selected_categories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewedParquetWrite:
    vectors_path: Path
    scores_path: Path
    method_vectors_path: Path
    vectors_sha256: str
    scores_sha256: str
    method_vectors_sha256: str


class ReviewedParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write(
        self,
        *,
        author_source_id: str,
        run_id: str,
        manifest_id: str,
        vectors: list[ReviewedVectorRow],
        scores: list[ReviewedScoreRow],
        method_vectors: list[ReviewedVectorRow],
    ) -> ReviewedParquetWrite:
        if not vectors or not scores or not method_vectors:
            raise ValueError("reviewed semantic Parquet inputs cannot be empty")
        dimensions = {len(row.vector) for row in (*vectors, *method_vectors)}
        if len(dimensions) != 1 or next(iter(dimensions)) < 1:
            raise ValueError("reviewed semantic vectors must share one dimension")
        dimension = next(iter(dimensions))
        directory = (
            self.root
            / "knowledge_reviewed"
            / f"author={quote(author_source_id, safe='-_.')}"
            / f"run={quote(run_id, safe='-_.')}"
            / f"embedding={quote(manifest_id, safe='-_.')}"
        )
        vectors_path = directory / "argument-vectors.parquet"
        scores_path = directory / "argument-scores.parquet"
        method_vectors_path = directory / "method-vectors.parquet"
        vector_schema = pa.schema(
            [
                ("run_id", pa.string()),
                ("manifest_id", pa.string()),
                ("entity_id", pa.string()),
                ("entity_kind", pa.string()),
                ("input_object_sha256", pa.string()),
                ("vector", pa.list_(pa.float32(), dimension)),
                ("token_count", pa.int64()),
                ("chunk_count", pa.int64()),
            ]
        )
        _write_exact(
            vectors_path,
            pa.Table.from_pylist(
                [_vector_projection(run_id, manifest_id, row) for row in vectors],
                schema=vector_schema,
            ),
        )
        _write_exact(
            method_vectors_path,
            pa.Table.from_pylist(
                [_vector_projection(run_id, manifest_id, row) for row in method_vectors],
                schema=vector_schema,
            ),
        )
        score_schema = pa.schema(
            [
                ("run_id", pa.string()),
                ("manifest_id", pa.string()),
                ("argument_unit_id", pa.string()),
                ("topic_relevance", pa.float64()),
                ("methodological_completeness", pa.float64()),
                ("category_scores_json", pa.string()),
                ("selected_categories", pa.list_(pa.string())),
            ]
        )
        _write_exact(
            scores_path,
            pa.Table.from_pylist(
                [
                    {
                        "run_id": run_id,
                        "manifest_id": manifest_id,
                        "argument_unit_id": row.argument_unit_id,
                        "topic_relevance": row.topic_relevance,
                        "methodological_completeness": (row.methodological_completeness),
                        "category_scores_json": canonical_json_bytes(row.category_scores).decode(
                            "utf-8"
                        ),
                        "selected_categories": list(row.selected_categories),
                    }
                    for row in scores
                ],
                schema=score_schema,
            ),
        )
        return ReviewedParquetWrite(
            vectors_path=vectors_path,
            scores_path=scores_path,
            method_vectors_path=method_vectors_path,
            vectors_sha256=sha256_bytes(vectors_path.read_bytes()),
            scores_sha256=sha256_bytes(scores_path.read_bytes()),
            method_vectors_sha256=sha256_bytes(method_vectors_path.read_bytes()),
        )


def _vector_projection(
    run_id: str,
    manifest_id: str,
    row: ReviewedVectorRow,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "manifest_id": manifest_id,
        "entity_id": row.entity_id,
        "entity_kind": row.entity_kind,
        "input_object_sha256": row.input_object_sha256,
        "vector": list(row.vector),
        "token_count": row.token_count,
        "chunk_count": row.chunk_count,
    }


def _write_exact(path: Path, table: pa.Table) -> None:
    if path.is_file():
        existing = pq.read_table(path)
        if not existing.equals(table, check_metadata=False):
            raise ValueError(f"reviewed semantic Parquet collision: {path}")
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
    "ReviewedParquetStore",
    "ReviewedParquetWrite",
    "ReviewedScoreRow",
    "ReviewedVectorRow",
]
