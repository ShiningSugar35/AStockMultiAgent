from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.monitoring.config import load_continuous_monitor_config
from astock.monitoring.news import GdeltNewsLeadProvider
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.monitoring.service import (
    ContinuousMonitorService,
    calculate_monitor_metrics,
    rule_triggered,
)
from astock.schemas.adaptation import ResearchModule
from astock.schemas.continuous_monitoring import (
    MonitorComparison,
    MonitorEvent,
    MonitorEventType,
    MonitorMetric,
    MonitorRuleAction,
    MonitorRuleRequest,
    MonitorSeverity,
    MonitorSource,
    MonitorTargetEnrollRequest,
    MonitorTargetReason,
    MonitorTaskStatus,
)
from astock.schemas.market import (
    AdjustmentMode,
    AmountUnit,
    Frequency,
    Market,
    MarketBar,
    TimestampSemantics,
    VolumeUnit,
)
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)


def _repo(tmp_path: Path) -> ContinuousMonitorRepository:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    assert "0059" in state.migrate()
    return ContinuousMonitorRepository(state, ObjectStore(tmp_path / "objects"))


def _target(repo: ContinuousMonitorRepository):
    return repo.enroll(
        MonitorTargetEnrollRequest(
            symbol="600519",
            market=Market.XSHG,
            company_id="600519",
            display_name="贵州茅台",
            reason=MonitorTargetReason.ANALYZED,
            aliases=["茅台"],
            created_at=NOW,
        )
    )


def test_continuous_monitor_config_keeps_hard_safety_gates() -> None:
    config = load_continuous_monitor_config(PROJECT_ROOT / "configs" / "continuous_monitor.yaml")
    assert config.wake_interval_seconds == 60
    assert config.cadence(MonitorSource.MARKET_60M) == 900
    assert config.cadence(MonitorSource.PAPER) == 900
    assert config.news.max_records_per_target == 20


def test_monitor_target_enroll_merges_reasons_and_aliases(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _target(repo)
    second = repo.enroll(
        MonitorTargetEnrollRequest(
            symbol="600519",
            market=Market.XSHG,
            company_id="600519",
            display_name="贵州茅台",
            reason=MonitorTargetReason.RECOMMENDED,
            aliases=["Kweichow Moutai"],
            created_at=NOW + timedelta(minutes=1),
        )
    )
    assert first.target_id == second.target_id
    assert second.reasons == [MonitorTargetReason.ANALYZED, MonitorTargetReason.RECOMMENDED]
    assert second.aliases == ["Kweichow Moutai", "茅台"]
    assert len(repo.active_targets(limit=10)) == 1


def test_event_and_task_are_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = _target(repo)
    payload: dict[str, object] = {"title": "公司发布重大合同公告"}
    event = MonitorEvent(
        event_id="monitor-event:test",
        target_id=target.target_id,
        company_id=target.company_id,
        event_type=MonitorEventType.OFFICIAL_DISCLOSURE,
        severity=MonitorSeverity.MATERIAL,
        observed_at=NOW,
        available_at=NOW,
        source=MonitorSource.CNINFO,
        source_ref="announcement-1",
        payload=payload,
        payload_hash=content_hash(payload),
        dedupe_key="dedupe-1",
        affected_modules=[ResearchModule.BASE_CASE, ResearchModule.EVIDENCE],
        requires_research=True,
    )
    stored, inserted = repo.record_event(event)
    assert inserted is True
    duplicate, duplicate_inserted = repo.record_event(event)
    assert duplicate_inserted is False
    assert duplicate.event_id == stored.event_id
    task, task_inserted = repo.ensure_task(stored)
    assert task_inserted is True
    assert task is not None
    again, again_inserted = repo.ensure_task(stored)
    assert again_inserted is False
    assert again is not None and again.task_id == task.task_id
    assert repo.update_task_status(task.task_id, status=MonitorTaskStatus.CLAIMED, at=NOW)
    assert repo.update_task_status(task.task_id, status=MonitorTaskStatus.COMPLETED, at=NOW)
    assert repo.list_tasks(pending_only=True) == []


def test_research_task_claim_is_leased_and_owner_bound(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = _target(repo)
    payload: dict[str, object] = {"title": "业绩预告需要复核"}
    event = MonitorEvent(
        event_id="monitor-event:claim-test",
        target_id=target.target_id,
        company_id=target.company_id,
        event_type=MonitorEventType.OFFICIAL_DISCLOSURE,
        severity=MonitorSeverity.MATERIAL,
        observed_at=NOW,
        available_at=NOW,
        source=MonitorSource.CNINFO,
        source_ref="announcement-claim",
        payload=payload,
        payload_hash=content_hash(payload),
        dedupe_key="dedupe-claim",
        affected_modules=[ResearchModule.BASE_CASE, ResearchModule.EVIDENCE],
        requires_research=True,
    )
    stored, _ = repo.record_event(event)
    task, _ = repo.ensure_task(stored)
    assert task is not None
    claimed = repo.claim_next_task(owner_id="agent-a", at=NOW, lease_seconds=120)
    assert claimed is not None and claimed["task_id"] == task.task_id
    assert claimed["claimed_by"] == "agent-a"
    assert repo.claim_next_task(owner_id="agent-b", at=NOW + timedelta(seconds=60)) is None
    reclaimed = repo.claim_next_task(owner_id="agent-b", at=NOW + timedelta(seconds=121))
    assert reclaimed is not None and reclaimed["task_id"] == task.task_id
    assert not repo.finish_task(
        task.task_id, owner_id="agent-a", succeeded=True, at=NOW + timedelta(seconds=122)
    )
    assert repo.finish_task(
        task.task_id, owner_id="agent-b", succeeded=True, at=NOW + timedelta(seconds=122)
    )


def test_cycle_isolates_one_source_failure_and_continues_other_sources(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    paths = ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    service = ContinuousMonitorService(
        paths,
        state,
        objects,
        load_continuous_monitor_config(PROJECT_ROOT / "configs" / "continuous_monitor.yaml"),
    )
    service.repo.enroll(
        MonitorTargetEnrollRequest(
            symbol="600519",
            market=Market.XSHG,
            company_id="600519",
            display_name="贵州茅台",
            reason=MonitorTargetReason.ANALYZED,
            aliases=["茅台"],
            created_at=NOW,
        )
    )
    monkeypatch.setattr(service, "sync_paper_targets", lambda at: [])

    def fail_market(*args, **kwargs):
        raise RuntimeError("market source unavailable")

    def succeed(*args, **kwargs):
        return 0, 0, NOW.isoformat()

    monkeypatch.setattr(service, "_process_market", fail_market)
    monkeypatch.setattr(service, "_process_disclosures", succeed)
    monkeypatch.setattr(service, "_process_news", succeed)
    monkeypatch.setattr(service, "_process_catalysts", succeed)
    monkeypatch.setattr(service, "_process_schedule", succeed)
    monkeypatch.setattr(service, "_process_paper", succeed)

    report = service.run_cycle(live=True, owner_id="test-owner", now=NOW)
    assert report.status.value == "PARTIAL"
    assert report.source_failure[MonitorSource.MARKET_60M] == 1
    assert report.source_success[MonitorSource.CNINFO] == 1
    assert report.source_success[MonitorSource.GDELT] == 1
    assert report.source_success[MonitorSource.PAPER] == 1
    assert any("MARKET_60M" in finding for finding in report.findings)


def test_cycle_isolates_gdelt_failure_and_keeps_other_sources_running(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    paths = ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    service = ContinuousMonitorService(
        paths,
        state,
        objects,
        load_continuous_monitor_config(PROJECT_ROOT / "configs" / "continuous_monitor.yaml"),
    )
    service.repo.enroll(
        MonitorTargetEnrollRequest(
            symbol="600519",
            market=Market.XSHG,
            company_id="600519",
            display_name="贵州茅台",
            reason=MonitorTargetReason.ANALYZED,
            aliases=["茅台"],
            created_at=NOW,
        )
    )
    monkeypatch.setattr(service, "sync_paper_targets", lambda at: [])

    def fail_news(*args, **kwargs):
        raise RuntimeError("gdelt source unavailable")

    def succeed(*args, **kwargs):
        return 0, 0, NOW.isoformat()

    monkeypatch.setattr(service, "_process_market", succeed)
    monkeypatch.setattr(service, "_process_disclosures", succeed)
    monkeypatch.setattr(service, "_process_news", fail_news)
    monkeypatch.setattr(service, "_process_catalysts", succeed)
    monkeypatch.setattr(service, "_process_schedule", succeed)
    monkeypatch.setattr(service, "_process_paper", succeed)

    report = service.run_cycle(live=True, owner_id="test-owner", now=NOW)
    assert report.status.value == "PARTIAL"
    assert report.source_failure[MonitorSource.GDELT] == 1
    assert report.source_success[MonitorSource.MARKET_60M] == 1
    assert report.source_success[MonitorSource.CNINFO] == 1
    assert report.source_success[MonitorSource.PAPER] == 1
    assert any("GDELT" in finding for finding in report.findings)


def test_daemon_lease_rejects_parallel_owner_and_recovers_stale_lease(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert repo.acquire_daemon(
        owner_id="owner-a",
        pid=101,
        at=NOW,
        lease_seconds=180,
    )
    assert not repo.acquire_daemon(
        owner_id="owner-b",
        pid=202,
        at=NOW + timedelta(seconds=60),
        lease_seconds=180,
    )
    assert repo.acquire_daemon(
        owner_id="owner-b",
        pid=202,
        at=NOW + timedelta(seconds=181),
        lease_seconds=180,
    )
    status = repo.daemon_status()
    assert status.owner_id == "owner-b"
    assert status.pid == 202


def test_rule_evaluator_is_typed_and_honors_cooldown(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = _target(repo)
    request = MonitorRuleRequest(
        target_id=target.target_id,
        metric=MonitorMetric.LAST_PRICE,
        comparison=MonitorComparison.LE,
        threshold=1400.0,
        action=MonitorRuleAction.ENTER_PAPER_CANDIDATE,
        severity=MonitorSeverity.MATERIAL,
        cooldown_seconds=3600,
        affected_modules=[ResearchModule.COMMITTEE, ResearchModule.TRADING_CLASSIFICATION],
        created_at=NOW,
    )
    rule = repo.add_rule(request)
    assert rule_triggered(rule, value=1399.0, now=NOW)
    cooled = rule.model_copy(update={"last_triggered_at": NOW})
    assert not rule_triggered(cooled, value=1399.0, now=NOW + timedelta(minutes=10))
    assert rule_triggered(cooled, value=1399.0, now=NOW + timedelta(hours=2))


def test_monitor_metrics_cover_price_return_drawdown_and_volume() -> None:
    bars = [_bar(index, close=100 + index, volume=1000 + index * 100) for index in range(25)]
    metrics = calculate_monitor_metrics(
        bars,
        high_watermark=130.0,
        last_review_at=NOW - timedelta(days=10),
        now=NOW,
        one_day_bars=4,
        five_day_bars=20,
        volume_ratio_window=20,
    )
    assert metrics[MonitorMetric.LAST_PRICE] == 124.0
    assert metrics[MonitorMetric.RETURN_1D] == 124 / 120 - 1
    assert metrics[MonitorMetric.RETURN_5D] == 124 / 104 - 1
    assert metrics[MonitorMetric.DRAWDOWN_FROM_WATCH_HIGH] == 124 / 130 - 1
    assert metrics[MonitorMetric.VOLUME_RATIO] is not None
    assert metrics[MonitorMetric.DAYS_SINCE_REVIEW] == 10.0


def test_gdelt_adapter_persists_only_news_leads(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.gdeltproject.org"
        return httpx.Response(
            200,
            request=request,
            json={
                "articles": [
                    {
                        "url": "https://example.com/news/1",
                        "title": "贵州茅台发布重大合同线索",
                        "seendate": "20260822T075500Z",
                        "domain": "example.com",
                        "language": "Chinese",
                        "sourcecountry": "China",
                    }
                ]
            },
        )

    provider = GdeltNewsLeadProvider(
        objects,
        state,
        endpoint="https://api.gdeltproject.org/api/v2/doc/doc",
        timeout_seconds=5,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    leads = provider.search(
        names=["贵州茅台"],
        symbol="600519",
        start=NOW - timedelta(hours=1),
        end=NOW,
        max_records=20,
    )
    assert len(leads) == 1
    assert leads[0].title == "贵州茅台发布重大合同线索"
    with state.connect() as connection:
        snapshot = connection.execute(
            "SELECT source_id FROM source_snapshot_index WHERE snapshot_id=?",
            (leads[0].snapshot_id,),
        ).fetchone()
    assert snapshot is not None and snapshot["source_id"] == "gdelt-news-leads:index"


def _bar(index: int, *, close: int, volume: int) -> MarketBar:
    timestamp = NOW - timedelta(hours=24 - index)
    price = Decimal(close)
    return MarketBar(
        observation_id=f"bar-{index}",
        provider_id="test",
        symbol="600519",
        market=Market.XSHG,
        frequency=Frequency.H1,
        timestamp=timestamp,
        timestamp_semantics=TimestampSemantics.BAR_END,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
        volume_unit=VolumeUnit.SHARE,
        amount=price * Decimal(volume),
        amount_unit=AmountUnit.CNY,
        adjustment_mode=AdjustmentMode.NONE,
    )
