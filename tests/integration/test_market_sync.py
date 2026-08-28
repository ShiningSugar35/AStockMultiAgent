from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.market_data.storage import CanonicalMarketStore, ParquetMarketStore
from astock.market_data.sync import MarketSyncService
from astock.schemas import (
    DataProviderCapability,
    MarketDataBatch,
    ProviderStatus,
    ReplayQuality,
    VolumeUnit,
)
from tests.helpers import make_batch


class FakeProvider:
    def __init__(
        self,
        batch: MarketDataBatch | None = None,
        *,
        provider_id: str,
        fail: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.batch = batch
        self.fail = fail
        self.requests = []

    def capability(self) -> DataProviderCapability:
        from astock.providers import EastMoney5mProvider

        capability = object.__new__(EastMoney5mProvider).capability()
        return capability.model_copy(
            update={"provider_id": self.provider_id, "status": ProviderStatus.AVAILABLE}
        )

    def fetch_bars(self, request):
        self.requests.append(request)
        if self.fail:
            raise ProviderError(
                "fixture failure", failure_class=FailureClass.NETWORK, retryable=True
            )
        if self.batch is None:
            raise AssertionError("successful fake providers require a batch")
        return self.batch.model_copy(update={"request": request})


def service(tmp_path: Path, state, providers) -> MarketSyncService:
    return MarketSyncService(
        providers,
        state,
        ParquetMarketStore(tmp_path / "data", "market_observation"),
        CanonicalMarketStore(tmp_path / "data", tmp_path / "manifests"),
    )


def test_repeated_dual_sync_is_idempotent(tmp_path: Path, state) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    sync = service(
        tmp_path,
        state,
        [
            FakeProvider(east, provider_id="eastmoney-5m"),
            FakeProvider(sina, provider_id="sina-5m"),
        ],
    )
    first = sync.sync_5m(east.request)
    second = sync.sync_5m(east.request)
    assert first.canonical_report.replay_quality is ReplayQuality.DUAL_SOURCE_5M_VERIFIED
    assert first.canonical_manifest["content_hash"] == second.canonical_manifest["content_hash"]
    assert set(first.observation_files) == set(second.observation_files)


def test_provider_failure_does_not_replace_previous_canonical(tmp_path: Path, state) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    good = service(
        tmp_path,
        state,
        [FakeProvider(east, provider_id="eastmoney-5m"), FakeProvider(sina, provider_id="sina-5m")],
    )
    good.sync_5m(east.request)
    manifest = tmp_path / "manifests" / "canonical" / "XSHG" / "5m" / "600519.json"
    before = manifest.read_bytes()
    failing = service(
        tmp_path,
        state,
        [
            FakeProvider(provider_id="eastmoney-5m", fail=True),
            FakeProvider(provider_id="sina-5m", fail=True),
        ],
    )
    with pytest.raises(ProviderError):
        failing.sync_5m(east.request)
    assert manifest.read_bytes() == before


def test_one_provider_failure_preserves_existing_canonical(tmp_path: Path, state) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    initial = service(
        tmp_path,
        state,
        [FakeProvider(east, provider_id="eastmoney-5m"), FakeProvider(sina, provider_id="sina-5m")],
    ).sync_5m(east.request)
    manifest_path = tmp_path / "manifests" / "canonical" / "XSHG" / "5m" / "600519.json"
    before = manifest_path.read_bytes()
    degraded = service(
        tmp_path,
        state,
        [
            FakeProvider(east, provider_id="eastmoney-5m"),
            FakeProvider(provider_id="sina-5m", fail=True),
        ],
    ).sync_5m(east.request)
    assert initial.canonical_updated
    assert not degraded.canonical_updated
    assert degraded.canonical_publish_reason == (
        "PRESERVED_PREVIOUS_CANONICAL_DUE_TO_PROVIDER_FAILURE"
    )
    assert manifest_path.read_bytes() == before
    def scope(provider_id: str) -> str:
        return f"{provider_id}:XSHG:600519:5m"
    degraded_checkpoints = {
        provider_id: state.get_checkpoint("market-provider", scope(provider_id))
        for provider_id in ("eastmoney-5m", "sina-5m")
    }
    assert all(
        checkpoint is not None and checkpoint["job_id"] == initial.job_id
        for checkpoint in degraded_checkpoints.values()
    )

    recovered = service(
        tmp_path,
        state,
        [FakeProvider(east, provider_id="eastmoney-5m"), FakeProvider(sina, provider_id="sina-5m")],
    ).sync_5m(east.request)
    assert recovered.canonical_updated
    assert recovered.failures == {}
    assert all(
        state.get_checkpoint("market-provider", scope(provider_id))["job_id"] == recovered.job_id
        for provider_id in ("eastmoney-5m", "sina-5m")
    )


def test_checkpoint_drives_seven_day_overlap_request(tmp_path: Path, state) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    east_provider = FakeProvider(east, provider_id="eastmoney-5m")
    sina_provider = FakeProvider(sina, provider_id="sina-5m")
    sync = service(tmp_path, state, [east_provider, sina_provider])
    first_request = east.request.model_copy(
        update={"requested_start": east.request.requested_start - timedelta(days=30)}
    )
    sync.sync_5m(first_request)
    sync.sync_5m(first_request)
    assert east_provider.requests[1].requested_start > first_request.requested_start
    assert sina_provider.requests[1].requested_start > first_request.requested_start


def test_intraday_breaker_is_scoped_by_provider_and_frequency_capability(
    tmp_path: Path,
    state,
) -> None:
    east = make_batch("eastmoney-5m", volume_unit=VolumeUnit.LOT_100_SHARES)
    sina = make_batch("sina-5m", volume_unit=VolumeUnit.SHARE)
    east_provider = FakeProvider(provider_id="eastmoney-5m", fail=True)
    sina_provider = FakeProvider(sina, provider_id="sina-5m")
    sync = service(tmp_path, state, [east_provider, sina_provider])

    for _ in range(3):
        sync.sync_5m(east.request)

    assert sync.source_breaker.status("eastmoney-5m", "market.raw_5m")["state"] == "OPEN"
    assert sync.source_breaker.status("eastmoney-5m", "market.raw_60m")["state"] == "CLOSED"
    assert len(east_provider.requests) == 3

    degraded = sync.sync_5m(east.request)
    assert len(east_provider.requests) == 3
    assert degraded.failures["eastmoney-5m"] == "CIRCUIT_OPEN:market.raw_5m"
    assert not degraded.canonical_updated
