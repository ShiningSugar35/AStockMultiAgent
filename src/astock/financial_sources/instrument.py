"""Fail-closed binding to one verified P5X-2 instrument-master release."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.schemas import (
    DatasetReleaseManifest,
    InstrumentRecord,
    InstrumentType,
    Market,
    ReferenceDatasetKind,
)


@dataclass(frozen=True, slots=True)
class FinancialInstrumentBinding:
    record: InstrumentRecord
    release_id: str
    manifest_artifact_id: str
    manifest_object_hash: str
    content_hash: str
    available_to_system_at: datetime


class FinancialInstrumentResolver:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        reference_parquet_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.parquet = ReferenceParquetStore(reference_parquet_root)

    def resolve(
        self,
        company_id: str,
        market: Market,
        *,
        as_of: datetime,
    ) -> FinancialInstrumentBinding:
        if market is Market.INDEX:
            raise ValueError("financial sources require an explicit stock exchange")
        row = self.state.get_market_reference_release(
            ReferenceDatasetKind.INSTRUMENT_MASTER.value,
            market.value,
            as_of=as_of,
        )
        if row is None:
            row = self.state.get_market_reference_release(
                ReferenceDatasetKind.INSTRUMENT_MASTER.value,
                "ALL",
                as_of=as_of,
            )
        if row is None:
            raise ValueError("financial source instrument release is unavailable at as_of")
        manifest = self._verified_manifest(row)
        if manifest.available_to_system_at > as_of:
            raise ValueError("financial source instrument release is late")
        expected_id = f"{market.value}:{company_id}"
        records = self._read_instruments(manifest)
        matched = [
            item
            for item in records
            if item.instrument_id == expected_id
            and item.market is market
            and item.symbol == company_id
            and item.instrument_type is InstrumentType.STOCK
            and item.available_to_system_at <= as_of
        ]
        if len(matched) != 1:
            raise ValueError("financial source instrument identity is missing or ambiguous")
        artifact_id = f"market-reference:{manifest.release_id}"
        if row["manifest_artifact_id"] != artifact_id:
            raise ValueError("instrument release artifact identity mismatch")
        return FinancialInstrumentBinding(
            record=matched[0],
            release_id=manifest.release_id,
            manifest_artifact_id=artifact_id,
            manifest_object_hash=str(row["manifest_object_hash"]),
            content_hash=manifest.content_hash,
            available_to_system_at=manifest.available_to_system_at,
        )

    def _verified_manifest(self, row: dict[str, Any]) -> DatasetReleaseManifest:
        object_hash = str(row["manifest_object_hash"])
        raw = self.objects.get_bytes(object_hash)
        if sha256_bytes(raw) != object_hash:
            raise ValueError("instrument manifest object hash mismatch")
        manifest = DatasetReleaseManifest.model_validate_json(raw)
        identity = {
            "dataset_kind": manifest.dataset_kind.value,
            "scope_key": manifest.scope_key,
            "provider_id": manifest.provider_id,
            "batch_id": manifest.batch_id,
            "content_hash": manifest.content_hash,
            "previous_release_id": manifest.previous_release_id,
            "available_to_system_at": manifest.available_to_system_at.isoformat(),
        }
        expected_inputs = json.dumps(
            [*manifest.raw_snapshot_ids, manifest.content_hash], separators=(",", ":")
        )
        if (
            manifest.dataset_kind is not ReferenceDatasetKind.INSTRUMENT_MASTER
            or manifest.release_id != content_hash(identity)
            or row["release_id"] != manifest.release_id
            or row["content_hash"] != manifest.content_hash
            or row["manifest_schema_version"] != manifest.schema_version
            or row["artifact_type"] != "DatasetReleaseManifest"
            or row["artifact_schema_version"] != manifest.schema_version
            or row["artifact_object_hash"] != object_hash
            or row["input_hashes_json"] != expected_inputs
            or row["raw_snapshot_ids_json"]
            != canonical_json_bytes(manifest.raw_snapshot_ids).decode("utf-8")
        ):
            raise ValueError("instrument release binding is corrupt")
        for snapshot_id in manifest.raw_snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if (
                snapshot is None
                or snapshot.available_to_system_at > manifest.available_to_system_at
                or not self.objects.verify(snapshot.object_sha256)
            ):
                raise ValueError("instrument release snapshot chain is corrupt")
        for descriptor in [*manifest.observation_files, *manifest.canonical_files]:
            if not self.parquet.verify_descriptor(
                descriptor,
                dataset_kind=manifest.dataset_kind.value,
                scope_key=manifest.scope_key,
                provider_id=manifest.provider_id,
                batch_id=manifest.batch_id,
                available_to_system_at=manifest.available_to_system_at,
                expected_row_count=manifest.coverage.record_count,
            ):
                raise ValueError("instrument release Parquet is corrupt")
        return manifest

    def _read_instruments(
        self, manifest: DatasetReleaseManifest
    ) -> list[InstrumentRecord]:
        descriptor = manifest.canonical_files[0]
        path = (self.parquet.root / descriptor.path).resolve()
        if not path.is_relative_to(self.parquet.root):
            raise ValueError("instrument release path escapes Parquet root")
        try:
            values = pq.ParquetFile(path).read(columns=["record_json"]).column(0).to_pylist()
            return [InstrumentRecord.model_validate_json(value) for value in values]
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("instrument release records are invalid") from exc


__all__ = ["FinancialInstrumentBinding", "FinancialInstrumentResolver"]
