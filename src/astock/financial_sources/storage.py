"""Immutable, self-describing Parquet for financial source observations and facts."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.schemas import (
    FinancialFact,
    FinancialSourceFileDescriptor,
    FinancialSourceObservation,
)

_SCHEMA = pa.schema(
    [
        ("record_id", pa.string()),
        ("record_kind", pa.string()),
        ("company_id", pa.string()),
        ("period_end", pa.date32()),
        ("available_to_system_at", pa.timestamp("us", tz="UTC")),
        ("record_json", pa.binary()),
    ]
)


class FinancialSourceParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write_observations(
        self,
        company_id: str,
        period_end: date,
        available_at: datetime,
        records: list[FinancialSourceObservation],
    ) -> tuple[Path, str, FinancialSourceFileDescriptor]:
        return self._write(
            "financial_source_observation",
            company_id,
            period_end,
            available_at,
            records,
            "SOURCE_OBSERVATION",
        )

    def write_facts(
        self,
        company_id: str,
        period_end: date,
        available_at: datetime,
        records: list[FinancialFact],
    ) -> tuple[Path, str, FinancialSourceFileDescriptor]:
        return self._write(
            "financial_certified_fact",
            company_id,
            period_end,
            available_at,
            records,
            "CERTIFIED_FACT",
        )

    def verify_descriptor(
        self,
        descriptor: FinancialSourceFileDescriptor,
        *,
        record_kind: str,
        company_id: str,
        period_end: date,
        available_at: datetime,
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
            ):
                return False
            rows = parquet.read().to_pylist()
            if any(
                row["record_kind"] != record_kind
                or row["company_id"] != company_id
                or row["period_end"] != period_end
                or row["available_to_system_at"] != available_at
                for row in rows
            ):
                return False
            payloads = [json.loads(row["record_json"]) for row in rows]
            return _logical_hash(payloads) == descriptor.logical_content_hash
        except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            return False

    def read_facts(self, descriptor: FinancialSourceFileDescriptor) -> list[FinancialFact]:
        path = (self.root / descriptor.path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Financial fact path escapes Parquet root")
        return [
            FinancialFact.model_validate_json(raw)
            for raw in pq.ParquetFile(path).read().column("record_json").to_pylist()
        ]

    def read_observations(
        self, descriptor: FinancialSourceFileDescriptor
    ) -> list[FinancialSourceObservation]:
        path = (self.root / descriptor.path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Financial observation path escapes Parquet root")
        return [
            FinancialSourceObservation.model_validate_json(raw)
            for raw in pq.ParquetFile(path).read().column("record_json").to_pylist()
        ]

    def _write(
        self,
        dataset: str,
        company_id: str,
        period_end: date,
        available_at: datetime,
        records: list[FinancialSourceObservation] | list[FinancialFact],
        record_kind: str,
    ) -> tuple[Path, str, FinancialSourceFileDescriptor]:
        if not records:
            raise ValueError("Cannot persist an empty financial source file")
        payloads = [item.model_dump(mode="json", exclude={"created_at"}) for item in records]
        logical_hash = _logical_hash(payloads)
        storage_identity = sha256_bytes(
            canonical_json_bytes(
                {
                    "logical_content_hash": logical_hash,
                    "available_to_system_at": available_at,
                }
            )
        )
        path = (
            self.root
            / dataset
            / f"company={_safe(company_id)}"
            / f"period={period_end.isoformat()}"
            / f"{storage_identity}.parquet"
        )
        if not path.is_file():
            rows = []
            for item, payload in zip(records, payloads, strict=True):
                record_id = (
                    item.observation_id
                    if isinstance(item, FinancialSourceObservation)
                    else item.fact_id
                )
                rows.append(
                    {
                        "record_id": str(record_id),
                        "record_kind": record_kind,
                        "company_id": company_id,
                        "period_end": period_end,
                        "available_to_system_at": available_at,
                        "record_json": canonical_json_bytes(payload),
                    }
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                pq.write_table(
                    pa.Table.from_pylist(rows, schema=_SCHEMA),
                    temporary,
                    compression="zstd",
                )
                with temporary.open("rb+") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        descriptor = self._describe(path, logical_hash, available_at)
        if not self.verify_descriptor(
            descriptor,
            record_kind=record_kind,
            company_id=company_id,
            period_end=period_end,
            available_at=available_at,
        ):
            raise ValueError("Financial Parquet verification failed")
        return path, logical_hash, descriptor

    def _describe(
        self, path: Path, logical_hash: str, created_at: datetime
    ) -> FinancialSourceFileDescriptor:
        parquet = pq.ParquetFile(path)
        return FinancialSourceFileDescriptor(
            created_at=created_at,
            path=path.resolve().relative_to(self.root).as_posix(),
            sha256=sha256_bytes(path.read_bytes()),
            schema_fingerprint=_schema_fingerprint(parquet.schema_arrow),
            row_count=parquet.metadata.num_rows,
            logical_content_hash=logical_hash,
        )


def _logical_hash(payloads: list[dict[str, object]]) -> str:
    return content_hash(payloads)


def _schema_fingerprint(schema: pa.Schema) -> str:
    return sha256_bytes(schema.serialize().to_pybytes())


def _safe(value: str) -> str:
    encoded = quote(value, safe="-_.")
    if not encoded or encoded in {".", ".."}:
        raise ValueError("Unsafe financial Parquet partition")
    return encoded


__all__ = ["FinancialSourceParquetStore"]
