"""Compaction for immutable one-row knowledge Parquet indices."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

_COMPACT_FILE = "_compact-v1.parquet"
_COMPACT_IDS = "_compacted_ids.txt"
_SUPPORTED_DATASETS = ("knowledge_comments", "knowledge_content")


@dataclass(frozen=True, slots=True)
class ParquetCompactionResult:
    dataset: str
    partition_count: int
    rows: int
    files_before: int
    files_after: int
    bytes_before: int
    bytes_after: int

    @property
    def bytes_saved(self) -> int:
        return self.bytes_before - self.bytes_after


class ParquetKnowledgeCompactor:
    """Merge tiny immutable record Parquets into one verified file per logical partition."""

    def __init__(self, parquet_root: Path) -> None:
        self.parquet_root = parquet_root.resolve()

    def compact_all(self) -> dict[str, object]:
        results = [self.compact_dataset(dataset) for dataset in _SUPPORTED_DATASETS]
        audit = self.audit()
        if audit["status"] != "PASS":
            raise RuntimeError("knowledge Parquet compaction audit failed")
        return {
            "status": "COMPACTED",
            "datasets": [
                {
                    "dataset": item.dataset,
                    "partition_count": item.partition_count,
                    "rows": item.rows,
                    "files_before": item.files_before,
                    "files_after": item.files_after,
                    "bytes_before": item.bytes_before,
                    "bytes_after": item.bytes_after,
                    "bytes_saved": item.bytes_saved,
                }
                for item in results
            ],
            "bytes_saved": sum(item.bytes_saved for item in results),
            "audit": audit,
        }

    def compact_dataset(self, dataset: str) -> ParquetCompactionResult:
        if dataset not in _SUPPORTED_DATASETS:
            raise ValueError(f"unsupported knowledge Parquet dataset: {dataset}")
        root = self.parquet_root / dataset
        if not root.exists():
            return ParquetCompactionResult(dataset, 0, 0, 0, 0, 0, 0)
        before_files, before_bytes = _parquet_stats(root)
        partitions = self._partitions(root)
        total_rows = 0
        compacted = 0
        for partition in partitions:
            rows, changed = self._compact_partition(partition)
            total_rows += rows
            compacted += int(changed)
        after_files, after_bytes = _parquet_stats(root)
        return ParquetCompactionResult(
            dataset=dataset,
            partition_count=compacted,
            rows=total_rows,
            files_before=before_files,
            files_after=after_files,
            bytes_before=before_bytes,
            bytes_after=after_bytes,
        )

    def audit(self) -> dict[str, object]:
        failures: list[str] = []
        partitions = 0
        rows = 0
        for dataset in _SUPPORTED_DATASETS:
            root = self.parquet_root / dataset
            if not root.exists():
                continue
            for partition in self._partitions(root):
                compact = partition / _COMPACT_FILE
                ids_file = partition / _COMPACT_IDS
                if not compact.exists() and not ids_file.exists():
                    continue
                partitions += 1
                if not compact.is_file() or not ids_file.is_file():
                    failures.append(str(partition))
                    continue
                expected = _read_ids(ids_file)
                try:
                    table = pq.ParquetFile(compact).read(columns=["version_id"])
                except (OSError, ValueError):
                    failures.append(str(partition))
                    continue
                actual = [str(value) for value in table.column("version_id").to_pylist()]
                if len(actual) != len(set(actual)) or set(actual) != expected:
                    failures.append(str(partition))
                    continue
                duplicate_deltas = [
                    path
                    for path in partition.glob("*.parquet")
                    if path.name != _COMPACT_FILE and path.stem in expected
                ]
                if duplicate_deltas:
                    failures.append(str(partition))
                    continue
                rows += len(actual)
        return {
            "status": "PASS" if not failures else "FAIL",
            "partition_count": partitions,
            "rows": rows,
            "failures": failures,
        }

    @staticmethod
    def _partitions(root: Path) -> list[Path]:
        result: list[Path] = []
        for directory in root.rglob("*"):
            if not directory.is_dir():
                continue
            if any(directory.glob("*.parquet")) or (directory / _COMPACT_IDS).is_file():
                result.append(directory)
        return sorted(result)

    def _compact_partition(self, partition: Path) -> tuple[int, bool]:
        compact_path = partition / _COMPACT_FILE
        ids_path = partition / _COMPACT_IDS
        sources = sorted(partition.glob("*.parquet"))
        delta_sources = [path for path in sources if path.name != _COMPACT_FILE]
        if compact_path.is_file() and not delta_sources:
            return int(pq.ParquetFile(compact_path).metadata.num_rows), False
        if not sources:
            return 0, False

        rows_by_id: dict[str, dict[str, object]] = {}
        schema: pa.Schema | None = None
        for source in sources:
            with pq.ParquetFile(source) as parquet:
                source_schema = parquet.schema_arrow
                if schema is None:
                    schema = source_schema
                elif not schema.equals(source_schema, check_metadata=False):
                    schema = pa.unify_schemas(
                        [schema, source_schema],
                        promote_options="permissive",
                    )
                for batch in parquet.iter_batches(batch_size=4096):
                    for row in pa.Table.from_batches([batch]).to_pylist():
                        version_id = str(row["version_id"])
                        existing = rows_by_id.get(version_id)
                        if existing is not None and existing != row:
                            raise RuntimeError(
                                "knowledge Parquet version collision in "
                                f"{partition}: {version_id}"
                            )
                        rows_by_id[version_id] = row
        if schema is None:
            return 0, False
        ordered_ids = sorted(rows_by_id)
        compacted = pa.Table.from_pylist(
            [rows_by_id[version_id] for version_id in ordered_ids], schema=schema
        )

        temp_compact = partition / f".{_COMPACT_FILE}.{uuid4().hex}.tmp"
        temp_ids = partition / f".{_COMPACT_IDS}.{uuid4().hex}.tmp"
        pq.write_table(compacted, temp_compact, compression="zstd", row_group_size=32768)
        temp_ids.write_text("\n".join(ordered_ids) + "\n", encoding="utf-8")
        _fsync(temp_compact)
        _fsync(temp_ids)
        if int(pq.ParquetFile(temp_compact).metadata.num_rows) != len(ordered_ids):
            temp_compact.unlink(missing_ok=True)
            temp_ids.unlink(missing_ok=True)
            raise RuntimeError(f"knowledge Parquet row-count mismatch in {partition}")
        if _read_ids(temp_ids) != set(ordered_ids):
            temp_compact.unlink(missing_ok=True)
            temp_ids.unlink(missing_ok=True)
            raise RuntimeError(f"knowledge Parquet id-index mismatch in {partition}")

        os.replace(temp_compact, compact_path)
        os.replace(temp_ids, ids_path)
        _read_ids.cache_clear()
        for source in delta_sources:
            source.unlink(missing_ok=True)
        return len(ordered_ids), True


def compacted_record_path(record_path: Path, version_id: str) -> Path | None:
    """Return the compact shard when a record id is already represented there."""

    ids_path = record_path.parent / _COMPACT_IDS
    compact_path = record_path.parent / _COMPACT_FILE
    if not ids_path.is_file() or not compact_path.is_file():
        return None
    if version_id in _read_ids(ids_path):
        return compact_path
    return None


@lru_cache(maxsize=256)
def _read_ids(path: Path) -> frozenset[str]:
    return frozenset(line for line in path.read_text(encoding="utf-8").splitlines() if line)


def _parquet_stats(root: Path) -> tuple[int, int]:
    files = list(root.rglob("*.parquet"))
    return len(files), sum(path.stat().st_size for path in files)


def _fsync(path: Path) -> None:
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ParquetCompactionResult",
    "ParquetKnowledgeCompactor",
    "compacted_record_path",
    "sha256_file",
]
