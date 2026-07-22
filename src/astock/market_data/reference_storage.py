"""Immutable Parquet persistence for raw reference observations and canonical releases."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.schemas import ReferenceBatch, ReferenceFileDescriptor

_SCHEMA = pa.schema(
    [
        ("record_id", pa.string()),
        ("record_type", pa.string()),
        ("dataset_kind", pa.string()),
        ("scope_key", pa.string()),
        ("provider_id", pa.string()),
        ("batch_id", pa.string()),
        ("available_to_system_at", pa.timestamp("us", tz="UTC")),
        ("record_json", pa.binary()),
    ]
)


class ReferenceParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write_observation(self, batch: ReferenceBatch) -> Path:
        path = self._path("market_reference_observation", batch, batch.batch_id)
        return self._write(path, batch)

    def write_canonical(self, batch: ReferenceBatch) -> tuple[Path, str]:
        records_hash = _batch_content_hash(batch)
        path = self._path("market_reference_canonical", batch, batch.batch_id)
        return self._write(path, batch), records_hash

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def describe(
        self,
        path: Path,
        *,
        logical_content_hash: str,
        created_at: datetime,
    ) -> ReferenceFileDescriptor:
        resolved = path.resolve()
        parquet = pq.ParquetFile(resolved)
        try:
            raw_records = parquet.read(columns=["record_json"]).column(0).to_pylist()
            records = [json.loads(raw) for raw in raw_records]
        except (TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Reference Parquet record_json is invalid") from exc
        actual_logical_hash = _logical_records_hash(records)
        if actual_logical_hash != logical_content_hash:
            raise ValueError("Reference Parquet logical content does not match the batch")
        return ReferenceFileDescriptor(
            created_at=created_at,
            path=self.relative(resolved),
            sha256=sha256_bytes(resolved.read_bytes()),
            schema_fingerprint=_schema_fingerprint(parquet.schema_arrow),
            row_count=parquet.metadata.num_rows,
            logical_content_hash=actual_logical_hash,
        )

    def verify_descriptor(
        self,
        descriptor: ReferenceFileDescriptor,
        *,
        dataset_kind: str,
        scope_key: str,
        provider_id: str,
        batch_id: str,
        available_to_system_at: object,
        expected_row_count: int,
    ) -> bool:
        path = (self.root / descriptor.path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        try:
            raw = path.read_bytes()
            parquet = pq.ParquetFile(path)
            if (
                sha256_bytes(raw) != descriptor.sha256
                or not parquet.schema_arrow.equals(_SCHEMA, check_metadata=True)
                or _schema_fingerprint(parquet.schema_arrow) != descriptor.schema_fingerprint
                or parquet.metadata.num_rows != descriptor.row_count
                or descriptor.row_count != expected_row_count
            ):
                return False
            table = parquet.read()
            rows = table.to_pylist()
            expected_metadata = {
                "dataset_kind": dataset_kind,
                "scope_key": scope_key,
                "provider_id": provider_id,
                "batch_id": batch_id,
            }
            for row in rows:
                if any(row[key] != value for key, value in expected_metadata.items()):
                    return False
                if row["available_to_system_at"] != available_to_system_at:
                    return False
            records = [json.loads(row["record_json"]) for row in rows]
            return _logical_records_hash(records) == descriptor.logical_content_hash
        except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            return False

    def _path(self, dataset: str, batch: ReferenceBatch, identity: str) -> Path:
        return (
            self.root
            / dataset
            / f"kind={_safe(batch.dataset_kind.value)}"
            / f"scope={_safe(batch.scope_key)}"
            / f"{identity}.parquet"
        )

    def _write(self, path: Path, batch: ReferenceBatch) -> Path:
        if path.is_file():
            descriptor = self.describe(
                path,
                logical_content_hash=_batch_content_hash(batch),
                created_at=batch.available_to_system_at,
            )
            if not self.verify_descriptor(
                descriptor,
                dataset_kind=batch.dataset_kind.value,
                scope_key=batch.scope_key,
                provider_id=batch.provider_id,
                batch_id=batch.batch_id,
                available_to_system_at=batch.available_to_system_at,
                expected_row_count=len(batch.records),
            ):
                raise ValueError(f"Existing reference Parquet has another identity: {path}")
            return path
        rows = []
        for record in batch.records:
            payload = record.model_dump(mode="python", exclude={"created_at"})
            record_type = str(payload.get("record_type", type(record).__name__))
            record_id = str(
                payload.get("observation_id")
                or payload.get("instrument_id")
                or content_hash(payload)
            )
            if record_type == "trading_session":
                record_id = f"{payload['exchange']}:{payload['session_date']}"
            rows.append(
                {
                    "record_id": record_id,
                    "record_type": record_type,
                    "dataset_kind": batch.dataset_kind.value,
                    "scope_key": batch.scope_key,
                    "provider_id": batch.provider_id,
                    "batch_id": batch.batch_id,
                    "available_to_system_at": batch.available_to_system_at,
                    "record_json": canonical_json_bytes(payload),
                }
            )
        # EMPTY batches are deliberately not published as canonical releases.
        if not rows:
            raise ValueError("Cannot persist an empty reference batch")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            table = pa.Table.from_pylist(rows, schema=_SCHEMA)
            pq.write_table(table, temporary, compression="zstd")
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = self.describe(
            path,
            logical_content_hash=_batch_content_hash(batch),
            created_at=batch.available_to_system_at,
        )
        if not self.verify_descriptor(
            descriptor,
            dataset_kind=batch.dataset_kind.value,
            scope_key=batch.scope_key,
            provider_id=batch.provider_id,
            batch_id=batch.batch_id,
            available_to_system_at=batch.available_to_system_at,
            expected_row_count=len(batch.records),
        ):
            raise ValueError("New reference Parquet failed post-create verification")
        return path


def _safe(value: str) -> str:
    encoded = quote(value, safe="-_.")
    if not encoded or encoded in {".", ".."}:
        raise ValueError(f"Unsafe reference partition: {value!r}")
    return encoded


def _batch_content_hash(batch: ReferenceBatch) -> str:
    records = [item.model_dump(mode="json", exclude={"created_at"}) for item in batch.records]
    return _logical_records_hash(records)


def _logical_records_hash(records: list[dict[str, object]]) -> str:
    logical = []
    for record in records:
        item = dict(record)
        item.pop("source_snapshot_id", None)
        item.pop("available_to_system_at", None)
        logical.append(item)
    return content_hash(logical)


def _schema_fingerprint(schema: pa.Schema) -> str:
    return sha256_bytes(schema.serialize().to_pybytes())


__all__ = ["ReferenceParquetStore"]
