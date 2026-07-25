"""Immutable provider observations and protected canonical Parquet manifests."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.core.atomic import atomic_write_bytes
from astock.core.errors import DataQualityError, FailureClass
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.market_data.quality import normalize_volume_to_shares
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    BarRequest,
    DataQualityReport,
    Frequency,
    Market,
    MarketBar,
    MarketDataBatch,
    QualityStatus,
    ReplayQuality,
    TimestampSemantics,
    VolumeUnit,
)

_PARQUET_SCHEMA = pa.schema(
    [
        ("observation_id", pa.string()),
        ("provider_id", pa.string()),
        ("symbol", pa.string()),
        ("market", pa.string()),
        ("frequency", pa.string()),
        ("timestamp", pa.timestamp("us", tz="Asia/Shanghai")),
        ("timestamp_semantics", pa.string()),
        ("open", pa.decimal128(20, 4)),
        ("high", pa.decimal128(20, 4)),
        ("low", pa.decimal128(20, 4)),
        ("close", pa.decimal128(20, 4)),
        ("volume", pa.decimal128(24, 4)),
        ("volume_unit", pa.string()),
        ("amount", pa.decimal128(24, 4)),
        ("amount_unit", pa.string()),
        ("adjustment_mode", pa.string()),
        ("raw_snapshot_id", pa.string()),
        ("batch_id", pa.string()),
    ]
)


class ParquetMarketStore:
    def __init__(self, root: Path, dataset_name: str) -> None:
        self.root = root.resolve()
        self.dataset_name = dataset_name

    def write_batch(self, batch: MarketDataBatch) -> list[Path]:
        grouped: dict[int, list[MarketBar]] = defaultdict(list)
        for bar in batch.bars:
            grouped[bar.timestamp.year].append(bar)
        paths: list[Path] = []
        for year, bars in sorted(grouped.items()):
            path = (
                self.root
                / self.dataset_name
                / f"provider={_safe_partition_value(batch.provider_id)}"
                / f"market={_safe_partition_value(batch.request.market.value)}"
                / f"frequency={_safe_partition_value(batch.request.frequency.value)}"
                / f"year={year}"
                / f"symbol={_safe_partition_value(batch.request.symbol)}"
                / f"{batch.batch_id}.parquet"
            )
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                rows = [_bar_row(bar, batch) for bar in bars]
                table = pa.Table.from_pylist(rows, schema=_PARQUET_SCHEMA)
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                try:
                    pq.write_table(table, temporary, compression="zstd")
                    with temporary.open("rb+") as handle:
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            paths.append(path)
        return paths


class CanonicalMarketStore:
    def __init__(self, data_root: Path, manifest_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.writer = ParquetMarketStore(self.data_root, "market_canonical")
        self.manifest_root = manifest_root.resolve()

    def manifest_path(self, request: BarRequest) -> Path:
        return canonical_manifest_path(
            self.manifest_root, request.market, request.frequency, request.symbol
        )

    def load_manifest(self, request: BarRequest) -> dict[str, object] | None:
        path = self.manifest_path(request)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise DataQualityError(
                "Canonical manifest is not a JSON object",
                failure_class=FailureClass.DATA_QUALITY,
                details={"manifest": str(path)},
            )
        return value

    def read_bars(self, request: BarRequest) -> list[MarketBar]:
        manifest = self.load_manifest(request)
        return self._read_manifest_bars(manifest) if manifest else []

    def publish(
        self,
        selected: MarketDataBatch,
        report: DataQualityReport,
        *,
        source_batch_ids: list[str],
    ) -> dict[str, object]:
        if (
            report.quality_status == QualityStatus.FAIL
            or report.replay_quality == ReplayQuality.UNREPLAYABLE
        ):
            raise DataQualityError(
                "Cannot publish a failed market batch as canonical",
                failure_class=FailureClass.DATA_QUALITY,
                details={"report_id": report.report_id},
            )
        previous_manifest = self.load_manifest(selected.request)
        previous_bars = self._read_manifest_bars(previous_manifest) if previous_manifest else []
        merged_by_timestamp = {bar.timestamp: bar for bar in previous_bars}
        merged_by_timestamp.update({bar.timestamp: _canonical_bar(bar) for bar in selected.bars})
        canonical_bars = [merged_by_timestamp[key] for key in sorted(merged_by_timestamp)]
        previous_source_batch_ids: list[str] = []
        if previous_manifest is not None:
            raw_previous_ids = previous_manifest.get("source_batch_ids", [])
            if not isinstance(raw_previous_ids, list) or not all(
                isinstance(value, str) for value in raw_previous_ids
            ):
                raise DataQualityError(
                    "Canonical manifest source_batch_ids are invalid",
                    failure_class=FailureClass.DATA_QUALITY,
                )
            previous_source_batch_ids = raw_previous_ids
        all_source_batch_ids = list(
            dict.fromkeys(
                [
                    *previous_source_batch_ids,
                    *source_batch_ids,
                ]
            )
        )
        canonical_batch_id = content_hash(
            {
                "selected_provider": selected.provider_id,
                "report_id": report.report_id,
                "source_batch_ids": all_source_batch_ids,
                "bars": [bar.observation_id for bar in canonical_bars],
            }
        )
        canonical_batch = selected.model_copy(
            update={
                "batch_id": canonical_batch_id,
                "provider_id": f"canonical:{selected.provider_id}",
                "bars": canonical_bars,
                "bar_count": len(canonical_bars),
                "actual_start": canonical_bars[0].timestamp if canonical_bars else None,
                "actual_end": canonical_bars[-1].timestamp if canonical_bars else None,
                "cursor": canonical_bars[-1].timestamp.isoformat() if canonical_bars else None,
            }
        )
        files = self.writer.write_batch(canonical_batch)
        relative_files = [str(path.relative_to(self.data_root)) for path in files]
        manifest: dict[str, object] = {
            "schema_version": "1.2",
            "market": selected.request.market.value,
            "instrument_type": selected.request.instrument_type.value,
            "symbol": selected.request.symbol,
            "frequency": selected.request.frequency.value,
            "adjustment_mode": selected.request.adjustment_mode.value,
            "canonical_batch_id": canonical_batch_id,
            "source_batch_ids": all_source_batch_ids,
            "selected_provider": selected.provider_id,
            "quality_report_id": report.report_id,
            "replay_quality": report.replay_quality.value,
            "quality_status": report.quality_status.value,
            "quality_metrics": report.cross_source_diffs,
            "actual_start": canonical_batch.actual_start.isoformat()
            if canonical_batch.actual_start
            else None,
            "actual_end": canonical_batch.actual_end.isoformat()
            if canonical_batch.actual_end
            else None,
            "bar_count": canonical_batch.bar_count,
            "files": relative_files,
            "file_hashes": {
                relative_path: sha256_bytes(path.read_bytes())
                for relative_path, path in zip(relative_files, files, strict=True)
            },
        }
        manifest["content_hash"] = content_hash(manifest)
        path = self.manifest_path(selected.request)
        atomic_write_bytes(path, canonical_json_bytes(manifest))
        return manifest

    def _read_manifest_bars(self, manifest: dict[str, object] | None) -> list[MarketBar]:
        if manifest is None:
            return []
        manifest_without_hash = dict(manifest)
        stored_manifest_hash = manifest_without_hash.pop("content_hash", None)
        if stored_manifest_hash != content_hash(manifest_without_hash):
            raise DataQualityError(
                "Canonical manifest content hash mismatch",
                failure_class=FailureClass.DATA_QUALITY,
            )
        raw_files = manifest.get("files", [])
        raw_hashes = manifest.get("file_hashes")
        if (
            not isinstance(raw_files, list)
            or not all(isinstance(item, str) for item in raw_files)
            or not isinstance(raw_hashes, dict)
            or set(raw_hashes) != set(raw_files)
        ):
            raise DataQualityError(
                "Canonical manifest files and content hashes are invalid",
                failure_class=FailureClass.DATA_QUALITY,
            )
        bars: dict[datetime, MarketBar] = {}
        for raw_path in raw_files:
            path = Path(raw_path)
            if not path.is_absolute():
                path = self.data_root / path
            path = path.resolve()
            if not path.is_relative_to(self.data_root) or not path.is_file():
                raise DataQualityError(
                    "Canonical Parquet path is outside the store or missing",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"file": str(path)},
                )
            expected_hash = raw_hashes[raw_path]
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or sha256_bytes(path.read_bytes()) != expected_hash
            ):
                raise DataQualityError(
                    "Canonical Parquet content hash mismatch",
                    failure_class=FailureClass.DATA_QUALITY,
                    details={"file": str(path)},
                )
            for row in pq.ParquetFile(path).read().to_pylist():
                bar = _market_bar_from_row(row)
                bars[bar.timestamp] = bar
        return [bars[key] for key in sorted(bars)]


def _bar_row(bar: MarketBar, batch: MarketDataBatch) -> dict[str, object]:
    return {
        "observation_id": bar.observation_id,
        "provider_id": bar.provider_id,
        "symbol": bar.symbol,
        "market": bar.market.value,
        "frequency": bar.frequency.value,
        "timestamp": bar.timestamp,
        "timestamp_semantics": bar.timestamp_semantics.value,
        "open": _quantize(bar.open),
        "high": _quantize(bar.high),
        "low": _quantize(bar.low),
        "close": _quantize(bar.close),
        "volume": _quantize(bar.volume),
        "volume_unit": bar.volume_unit.value,
        "amount": _quantize(bar.amount) if bar.amount is not None else None,
        "amount_unit": bar.amount_unit.value,
        "adjustment_mode": bar.adjustment_mode.value,
        "raw_snapshot_id": batch.raw_snapshot_id,
        "batch_id": batch.batch_id,
    }


def _canonical_bar(bar: MarketBar) -> MarketBar:
    normalized_volume = normalize_volume_to_shares(bar)
    payload = bar.model_dump(mode="json")
    payload.update(
        {
            "observation_id": content_hash(
                {"source_observation": bar.observation_id, "normalized_volume": normalized_volume}
            ),
            "provider_id": f"canonical:{bar.provider_id}",
            "volume": normalized_volume,
            "volume_unit": VolumeUnit.SHARE,
        }
    )
    return MarketBar.model_validate(payload)


def _market_bar_from_row(row: dict[str, object]) -> MarketBar:
    timestamp = row["timestamp"]
    if not isinstance(timestamp, datetime):
        raise DataQualityError(
            "Canonical Parquet timestamp has an invalid type",
            failure_class=FailureClass.DATA_QUALITY,
        )
    return MarketBar(
        observation_id=str(row["observation_id"]),
        provider_id=str(row["provider_id"]),
        symbol=str(row["symbol"]),
        market=Market(str(row["market"])),
        frequency=Frequency(str(row["frequency"])),
        timestamp=timestamp,
        timestamp_semantics=TimestampSemantics(str(row["timestamp_semantics"])),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
        volume_unit=VolumeUnit(str(row["volume_unit"])),
        amount=Decimal(str(row["amount"])) if row.get("amount") is not None else None,
        amount_unit=AmountUnit(str(row["amount_unit"])),
        adjustment_mode=AdjustmentMode(str(row["adjustment_mode"])),
    )


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _safe_partition_value(value: str) -> str:
    encoded = quote(value, safe="-_.")
    if not encoded or encoded in {".", ".."}:
        raise ValueError(f"Unsafe Parquet partition value: {value!r}")
    return encoded


def canonical_manifest_path(
    manifest_root: Path,
    market: Market | str,
    frequency: Frequency | str,
    symbol: str,
) -> Path:
    market_value = market.value if isinstance(market, Market) else str(market)
    frequency_value = frequency.value if isinstance(frequency, Frequency) else str(frequency)
    return (
        manifest_root.resolve()
        / "canonical"
        / _safe_partition_value(market_value)
        / _safe_partition_value(frequency_value)
        / f"{_safe_partition_value(symbol)}.json"
    )
