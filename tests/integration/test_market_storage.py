from __future__ import annotations

import json
from pathlib import Path

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
    manifest = canonical.publish(batch, report, source_batch_ids=[batch.batch_id, other.batch_id])
    manifest_path = tmp_path / "manifests" / "canonical" / "XSHG" / "5m" / "600519.json"
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["replay_quality"] == ReplayQuality.DUAL_SOURCE_5M_VERIFIED.value
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
