"""Community source scheduling stays bounded and separate from market/paper work."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.monitoring.config import load_continuous_monitor_config
from astock.monitoring.service import ContinuousMonitorService
from astock.schemas.continuous_monitoring import (
    MonitorSource,
    MonitorTargetEnrollRequest,
    MonitorTargetReason,
)
from astock.schemas.reference_data import Market
from astock.settings import ProjectPaths

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> ContinuousMonitorService:
    paths = ProjectPaths(
        root=ROOT,
        runtime=tmp_path,
        objects=tmp_path / "objects" / "sha256",
        parquet=tmp_path / "data" / "parquet",
        manifests=tmp_path / "manifests",
        state_db=tmp_path / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, ROOT / "migrations")
    state.migrate()
    service = ContinuousMonitorService(
        paths, state, ObjectStore(paths.objects),
        load_continuous_monitor_config(ROOT / "configs" / "continuous_monitor.yaml"),
    )
    for symbol, market in (("002475", Market.XSHE), ("600521", Market.XSHG)):
        service.repo.enroll(MonitorTargetEnrollRequest(
            symbol=symbol, market=market, company_id=symbol,
            display_name=symbol, reason=MonitorTargetReason.ANALYZED,
            created_at=NOW,
        ))
    return service


def test_live_monitor_scans_at_most_one_community_target_after_primary_work(
    tmp_path: Path, monkeypatch,
) -> None:
    service = _service(tmp_path)
    calls = []
    monkeypatch.setattr(service, "sync_paper_targets", lambda **_kwargs: [])
    for name in (
        "_process_market", "_process_disclosures", "_process_news",
        "_process_catalysts", "_process_schedule", "_process_paper",
    ):
        def primary(target, now, live, *, lease_guard=None, _name=name):
            del live, lease_guard
            calls.append((_name, target.symbol))
            return 0, 0, now.isoformat()

        monkeypatch.setattr(service, name, primary)
    import astock.social_sources.config as social_config
    monkeypatch.setattr(
        social_config, "load_social_search_config",
        lambda *_args, **_kwargs: SimpleNamespace(enabled=True),
    )

    def community(target, now, *, lease_guard=None):
        del lease_guard
        calls.append(("community", target.symbol))
        return 0, 0, now.isoformat()

    monkeypatch.setattr(service, "_process_community", community)
    report = service.run_cycle(owner_id="community-budget-test", live=True, now=NOW)
    community_calls = [item for item in calls if item[0] == "community"]
    assert len(community_calls) == 1
    assert report.source_success[MonitorSource.COMMUNITY] == 1
    assert all(item[0] != "community" for item in calls[:-1])
    assert sum(1 for item in calls if item[0] != "community") == 12


def test_unpaired_community_does_not_block_other_monitor_sources(
    tmp_path: Path, monkeypatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "sync_paper_targets", lambda **_kwargs: [])
    for name in (
        "_process_market", "_process_disclosures", "_process_news",
        "_process_catalysts", "_process_schedule", "_process_paper",
    ):
        monkeypatch.setattr(
            service, name,
            lambda _target, now, _live, *, lease_guard=None: (0, 0, now.isoformat()),
        )
    import astock.social_sources.config as social_config
    def not_paired(*_args, **_kwargs):
        raise ValueError("not yet paired")

    monkeypatch.setattr(social_config, "load_social_search_config", not_paired)
    report = service.run_cycle(owner_id="community-disabled-test", live=True, now=NOW)
    assert MonitorSource.COMMUNITY not in report.source_success
    assert report.source_failure.get(MonitorSource.COMMUNITY, 0) == 0
    assert report.source_success[MonitorSource.MARKET_60M] == 2
