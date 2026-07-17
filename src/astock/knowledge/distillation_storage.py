"""Private-safe Parquet metadata index for distillation units."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes
from astock.schemas import DistillationUnit

_DISTILLATION_SCHEMA = pa.schema(
    [
        ("unit_id", pa.string()),
        ("run_id", pa.string()),
        ("author_source_id", pa.string()),
        ("source_id", pa.string()),
        ("source_snapshot_id", pa.string()),
        ("source_unit_id", pa.string()),
        ("locator_type", pa.string()),
        ("source_item_ordinal", pa.int64()),
        ("segment_ordinal", pa.int64()),
        ("page_number", pa.int64()),
        ("block_index", pa.int64()),
        ("content_id", pa.string()),
        ("comment_id", pa.string()),
        ("char_start", pa.int64()),
        ("char_end", pa.int64()),
        ("source_object_sha256", pa.string()),
        ("normalized_text_sha256", pa.string()),
        ("normalized_char_count", pa.int64()),
        ("duplicate_of_unit_id", pa.string()),
        ("content_classes", pa.list_(pa.string())),
        ("method_categories", pa.list_(pa.string())),
        ("decision", pa.string()),
        ("reason_codes", pa.list_(pa.string())),
        ("score_json", pa.string()),
        ("classification_rule_version", pa.string()),
    ]
)


class ParquetDistillationStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for_run(self, author_source_id: str, run_id: str) -> Path:
        return (
            self.root
            / "knowledge_distillation"
            / f"author={quote(author_source_id, safe='-_.')}"
            / f"run={quote(run_id, safe='-_.')}"
            / "units.parquet"
        )

    def write_run(self, author_source_id: str, run_id: str, units: list[DistillationUnit]) -> Path:
        path = self.path_for_run(author_source_id, run_id)
        expected_ids = [unit.unit_id for unit in units]
        if path.exists():
            existing_ids = [
                str(row["unit_id"])
                for row in pq.ParquetFile(path).read(columns=["unit_id"]).to_pylist()
            ]
            if existing_ids != expected_ids:
                raise ValueError(f"distillation Parquet collision: {run_id}")
            return path
        rows = [
            {
                "unit_id": unit.unit_id,
                "run_id": unit.run_id,
                "author_source_id": unit.author_source_id,
                "source_id": unit.source_id,
                "source_snapshot_id": unit.locator.source_snapshot_id,
                "source_unit_id": unit.locator.source_unit_id,
                "locator_type": unit.locator.locator_type.value,
                "source_item_ordinal": unit.source_item_ordinal,
                "segment_ordinal": unit.segment_ordinal,
                "page_number": unit.locator.page_number,
                "block_index": unit.locator.block_index,
                "content_id": unit.locator.content_id,
                "comment_id": unit.locator.comment_id,
                "char_start": unit.locator.char_start,
                "char_end": unit.locator.char_end,
                "source_object_sha256": unit.locator.source_object_sha256,
                "normalized_text_sha256": unit.normalized_text_sha256,
                "normalized_char_count": unit.normalized_char_count,
                "duplicate_of_unit_id": unit.duplicate_of_unit_id,
                "content_classes": [item.value for item in unit.content_classes],
                "method_categories": [item.value for item in unit.method_categories],
                "decision": unit.decision.value,
                "reason_codes": unit.reason_codes,
                "score_json": canonical_json_bytes(unit.score_by_content_class).decode("utf-8"),
                "classification_rule_version": unit.classification_rule_version,
            }
            for unit in units
        ]
        table = pa.Table.from_pylist(rows, schema=_DISTILLATION_SCHEMA)
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
        return path

    def unit_hash_index(self, author_source_id: str, run_id: str) -> dict[str, str]:
        path = self.path_for_run(author_source_id, run_id)
        if not path.is_file():
            return {}
        rows = pq.ParquetFile(path).read(
            columns=["unit_id", "normalized_text_sha256"]
        ).to_pylist()
        return {
            str(row["unit_id"]): str(row["normalized_text_sha256"])
            for row in rows
        }


__all__ = ["ParquetDistillationStore"]
