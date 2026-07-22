"""Immutable Parquet facts for candidate signals and scan membership."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.schemas.candidates import CandidateFileDescriptor, CandidateRecord, CandidateSignal

_SCHEMA = pa.schema(
    [
        ("record_id", pa.string()),
        ("record_kind", pa.string()),
        ("scan_id", pa.string()),
        ("company_id", pa.string()),
        ("as_of", pa.timestamp("us", tz="UTC")),
        ("record_json", pa.binary()),
    ]
)


class CandidateParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write_signals(
        self,
        scan_id: str,
        as_of: datetime,
        records: list[CandidateSignal],
    ) -> CandidateFileDescriptor:
        return self._write("candidate_signal", scan_id, as_of, records)

    def write_members(
        self,
        scan_id: str,
        as_of: datetime,
        records: list[CandidateRecord],
    ) -> CandidateFileDescriptor:
        return self._write("candidate_member", scan_id, as_of, records)

    def verify(
        self,
        descriptor: CandidateFileDescriptor,
        *,
        record_kind: str,
        scan_id: str,
        as_of: datetime,
    ) -> bool:
        path = (self.root / descriptor.path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        try:
            parquet = pq.ParquetFile(path)
            if (
                sha256_bytes(path.read_bytes()) != descriptor.sha256
                or not parquet.schema_arrow.equals(_SCHEMA, check_metadata=True)
                or _schema_fingerprint(parquet.schema_arrow) != descriptor.schema_fingerprint
                or parquet.metadata.num_rows != descriptor.row_count
            ):
                return False
            rows = parquet.read().to_pylist()
            if any(
                row["record_kind"] != record_kind
                or row["scan_id"] != scan_id
                or row["as_of"] != as_of
                for row in rows
            ):
                return False
            payloads = [json.loads(row["record_json"]) for row in rows]
            return content_hash(payloads) == descriptor.logical_content_hash
        except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            return False

    def read_signals(self, descriptor: CandidateFileDescriptor) -> list[CandidateSignal]:
        return [
            CandidateSignal.model_validate_json(raw)
            for raw in self._read_json(descriptor)
        ]

    def read_members(self, descriptor: CandidateFileDescriptor) -> list[CandidateRecord]:
        return [
            CandidateRecord.model_validate_json(raw)
            for raw in self._read_json(descriptor)
        ]

    def _read_json(self, descriptor: CandidateFileDescriptor) -> list[bytes]:
        path = (self.root / descriptor.path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Candidate Parquet path escapes root")
        return pq.ParquetFile(path).read().column("record_json").to_pylist()

    def _write(
        self,
        record_kind: str,
        scan_id: str,
        as_of: datetime,
        records: list[CandidateSignal] | list[CandidateRecord],
    ) -> CandidateFileDescriptor:
        payloads = [item.model_dump(mode="json", exclude={"created_at"}) for item in records]
        logical_hash = content_hash(payloads)
        path = self.root / record_kind / f"scan={scan_id}" / f"{logical_hash}.parquet"
        if not path.is_file():
            rows = []
            for item, payload in zip(records, payloads, strict=True):
                record_id = (
                    item.signal_id
                    if isinstance(item, CandidateSignal)
                    else item.candidate_version_id
                )
                rows.append(
                    {
                        "record_id": record_id,
                        "record_kind": record_kind,
                        "scan_id": scan_id,
                        "company_id": item.company_id,
                        "as_of": as_of,
                        "record_json": canonical_json_bytes(payload),
                    }
                )
            table = pa.Table.from_pylist(rows, schema=_SCHEMA)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
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
        parquet = pq.ParquetFile(path)
        descriptor = CandidateFileDescriptor(
            created_at=as_of,
            path=path.resolve().relative_to(self.root).as_posix(),
            sha256=sha256_bytes(path.read_bytes()),
            schema_fingerprint=_schema_fingerprint(parquet.schema_arrow),
            row_count=parquet.metadata.num_rows,
            logical_content_hash=logical_hash,
        )
        if not self.verify(
            descriptor,
            record_kind=record_kind,
            scan_id=scan_id,
            as_of=as_of,
        ):
            raise ValueError("Candidate Parquet verification failed")
        return descriptor


def _schema_fingerprint(schema: pa.Schema) -> str:
    return sha256_bytes(schema.serialize().to_pybytes())


__all__ = ["CandidateParquetStore"]
