from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
import pytest

from astock.core.errors import DataQualityError
from astock.market_data.quality import cross_validate_batches, validate_batch
from astock.market_data.storage import CanonicalMarketStore, ParquetMarketStore
from astock.schemas import QualityStatus, ReplayQuality, VolumeUnit
from tests.helpers import make_batch


def test_observation_and_canonical_parquet_are_idempotent(tmp_path: Path) -> None:
    batch = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    other = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    observation = ParquetMarketStore(tmp_path / "data", "market_observation")
    first_paths = observation.write_batch(batch)
    second_paths = observation.write_batch(batch)
    assert first_paths == second_paths
    assert pq.read_table(first_paths[0]).num_rows == 48

    canonical = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    report = cross_validate_batches(batch, other)
    manifest = canonical.publish(
        batch,
        report,
        source_batch_ids=[batch.batch_id, other.batch_id],
        source_snapshot_ids=[
            batch.raw_snapshot_id,
            other.raw_snapshot_id,
        ],
    )
    manifest_path = tmp_path / "manifests" / "canonical" / "XSHG" / "5m" / "600519.json"
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["replay_quality"] == ReplayQuality.DUAL_SOURCE_5M_VERIFIED.value
    assert persisted["source_snapshot_ids"] == [
        batch.raw_snapshot_id,
        other.raw_snapshot_id,
    ]
    assert persisted["content_hash"] == manifest["content_hash"]
    canonical_table = pq.read_table(tmp_path / "data" / Path(persisted["files"][0]))
    assert canonical_table.column("volume_unit")[0].as_py() == VolumeUnit.SHARE.value


def test_failed_batch_cannot_replace_existing_manifest(tmp_path: Path) -> None:
    batch = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    other = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    canonical = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    good = cross_validate_batches(batch, other)
    canonical.publish(batch, good, source_batch_ids=[batch.batch_id, other.batch_id])
    manifest_path = tmp_path / "manifests" / "canonical" / "XSHG" / "5m" / "600519.json"
    before = manifest_path.read_bytes()

    failed = validate_batch(make_batch("bad", bad_ohlc=True))
    assert failed.quality_status is QualityStatus.FAIL
    with pytest.raises(DataQualityError):
        canonical.publish(batch, failed, source_batch_ids=[batch.batch_id])
    assert manifest_path.read_bytes() == before


def _shift_batch_year(batch, *, days: int):
    bars = [
        bar.model_copy(update={"timestamp": bar.timestamp + timedelta(days=days)})
        for bar in batch.bars
    ]
    request = batch.request.model_copy(
        update={
            "requested_start": batch.request.requested_start + timedelta(days=days),
            "requested_end": batch.request.requested_end + timedelta(days=days),
        }
    )
    return batch.model_copy(
        update={
            "request": request,
            "requested_start": request.requested_start,
            "requested_end": request.requested_end,
            "actual_start": bars[0].timestamp,
            "actual_end": bars[-1].timestamp,
            "bars": bars,
            "cursor": bars[-1].timestamp.isoformat(),
        }
    )


def test_canonical_publish_reuses_unaffected_years_and_gc_prunes_only_orphans(
    tmp_path: Path,
) -> None:
    canonical = CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests")
    batch_2026 = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    other_2026 = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    batch_2025 = _shift_batch_year(batch_2026, days=-365)
    other_2025 = _shift_batch_year(other_2026, days=-365)

    first = canonical.publish(
        batch_2025,
        cross_validate_batches(batch_2025, other_2025),
        source_batch_ids=[batch_2025.batch_id, other_2025.batch_id],
    )
    first_files = cast(list[str], first["files"])
    first_2025_file = next(path for path in first_files if "year=2025" in path)

    second = canonical.publish(
        batch_2026,
        cross_validate_batches(batch_2026, other_2026),
        source_batch_ids=[batch_2026.batch_id, other_2026.batch_id],
    )

    second_files = cast(list[str], second["files"])
    assert second["schema_version"] == "1.4"
    assert len(second_files) == 2
    assert first_2025_file in second_files
    assert second["bar_count"] == 96
    assert len(canonical.read_bars(batch_2026.request)) == 96
    assert canonical.prune_orphaned_files()["orphaned_file_count"] == 0

    refreshed = make_batch("eastmoney-refresh", volume_unit=VolumeUnit.LOT_100_SHARES)
    refreshed_other = make_batch("sina-refresh", volume_unit=VolumeUnit.SHARE)
    third = canonical.publish(
        refreshed,
        cross_validate_batches(refreshed, refreshed_other),
        source_batch_ids=[refreshed.batch_id, refreshed_other.batch_id],
    )

    third_files = cast(list[str], third["files"])
    assert first_2025_file in third_files
    plan = canonical.prune_orphaned_files()
    assert plan["orphaned_file_count"] == 1
    assert cast(int, plan["orphaned_bytes"]) > 0
    pruned = canonical.prune_orphaned_files(confirm=True)
    assert pruned["deleted_file_count"] == 1
    assert canonical.prune_orphaned_files()["orphaned_file_count"] == 0
    assert len(canonical.read_bars(refreshed.request)) == 96
