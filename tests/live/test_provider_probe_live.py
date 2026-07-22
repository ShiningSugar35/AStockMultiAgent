from __future__ import annotations

import os
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers import ProviderProbeService, load_provider_registry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("ASTOCK_RUN_LIVE") != "1", reason="explicit live opt-in"),
]


@pytest.mark.parametrize(
    "provider_id",
    [
        "eastmoney-5m",
        "sina-5m",
        "cninfo-disclosures",
        "eastmoney-financial",
        "sina-financial",
    ],
)
def test_explicit_live_provider_probe(tmp_path: Path, provider_id: str) -> None:
    state = StateStore(tmp_path / "state.sqlite", Path("migrations"))
    state.migrate()
    service = ProviderProbeService(
        project_root=Path.cwd(),
        registry=load_provider_registry(Path("configs/provider_registry.yaml")),
        state=state,
        objects=ObjectStore(tmp_path / "objects"),
    )

    result = service.probe(provider_id, live=True, probe_key="live-smoke-v1")

    assert result.probe_mode == "LIVE"
    assert result.last_probe_at is not None
    assert result.report_object_hash is not None
