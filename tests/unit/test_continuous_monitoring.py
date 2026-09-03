from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
import typer
from typer.testing import CliRunner

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.monitoring import cli as monitor_cli
from astock.monitoring import daemon as monitor_daemon
from astock.monitoring import repository as monitor_repository
from astock.monitoring.cli import _daemon_start_matches, register_continuous_monitor_commands
from astock.monitoring.config import load_continuous_monitor_config
from astock.monitoring.news import GdeltNewsLeadProvider, NewsLead
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.monitoring.service import (
    ContinuousMonitorService,
    calculate_monitor_metrics,
    rule_triggered,
)
from astock.schemas.adaptation import ResearchModule
from astock.schemas.continuous_monitoring import (
    MonitorComparison,
    MonitorDaemonState,
    MonitorDaemonStatus,
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
    object_count = sum(1 for path in (tmp_path / "objects").rglob("*") if path.is_file())
    again, again_inserted = repo.ensure_task(stored)
    assert again_inserted is False
    assert again is not None and again.task_id == task.task_id
    assert sum(1 for path in (tmp_path / "objects").rglob("*") if path.is_file()) == object_count
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
    monkeypatch.setattr(service, "sync_paper_targets", lambda at, lease_guard=None: [])

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
    monkeypatch.setattr(service, "sync_paper_targets", lambda at, lease_guard=None: [])

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


def test_cycle_lease_guard_aborts_before_recording_or_next_source(
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
    target = service.repo.enroll(
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
    monkeypatch.setattr(service, "sync_paper_targets", lambda at, lease_guard=None: [])
    calls: list[MonitorSource] = []
    lost = False

    def market(*args, **kwargs):
        nonlocal lost
        calls.append(MonitorSource.MARKET_60M)
        lost = True
        return 0, 0, NOW.isoformat()

    def later(*args, **kwargs):
        calls.append(MonitorSource.CNINFO)
        return 0, 0, NOW.isoformat()

    def guard() -> None:
        if lost:
            raise RuntimeError("simulated daemon lease loss")

    monkeypatch.setattr(service, "_process_market", market)
    monkeypatch.setattr(service, "_process_disclosures", later)

    with pytest.raises(RuntimeError, match="simulated daemon lease loss"):
        service.run_cycle(
            live=True,
            owner_id="test-owner",
            now=NOW,
            lease_guard=guard,
        )

    assert calls == [MonitorSource.MARKET_60M]
    assert service.repo.cursor(target.target_id, MonitorSource.MARKET_60M) is None
    assert service.repo.recent_runs() == []


def test_real_news_handler_fences_monitor_writes_after_lease_loss(
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
    target = service.repo.enroll(
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
    lost = False

    def search(**kwargs):
        nonlocal lost
        del kwargs
        lost = True
        return [
            NewsLead(
                title="material lead",
                url="https://example.invalid/lead",
                domain="example.invalid",
                seen_at=NOW,
                language="zh",
                source_country="CN",
                snapshot_id="snapshot:lease-fence",
            )
        ]

    def guard() -> None:
        if lost:
            raise RuntimeError("simulated daemon lease loss after acquisition")

    monkeypatch.setattr(service.news, "search", search)
    with pytest.raises(RuntimeError, match="lease loss after acquisition"):
        service._process_news(target, NOW, True, lease_guard=guard)

    with state.connect() as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM continuous_monitor_event WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0]
        task_count = connection.execute(
            "SELECT COUNT(*) FROM continuous_monitor_task WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0]
    assert event_count == 0
    assert task_count == 0


def test_existing_research_event_reconciles_missing_task_after_crash(
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
    target = service.repo.enroll(
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
    lead = NewsLead(
        title="贵州茅台重大合同",
        url="https://example.invalid/reconcile",
        domain="example.invalid",
        seen_at=NOW,
        language="zh",
        source_country="CN",
        snapshot_id="snapshot:reconcile",
    )
    monkeypatch.setattr(service.news, "search", lambda **kwargs: [lead])
    original_ensure_task = service.repo.ensure_task
    fail_once = True

    def flaky_ensure_task(event):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated crash between event and task")
        return original_ensure_task(event)

    monkeypatch.setattr(service.repo, "ensure_task", flaky_ensure_task)
    with pytest.raises(RuntimeError, match="between event and task"):
        service._process_news(target, NOW, True)

    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM continuous_monitor_event WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM continuous_monitor_task WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0] == 0

    events, tasks, _ = service._process_news(target, NOW, True)
    assert events == 0
    assert tasks == 1
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM continuous_monitor_task WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0] == 1


def test_daemon_lease_rejects_parallel_owner_and_recovers_stale_lease(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(monitor_repository, "_process_is_alive", lambda pid: False)
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


def test_stale_lease_cannot_be_stolen_while_recorded_daemon_pid_is_alive(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert repo.acquire_daemon(
        owner_id="owner-a",
        pid=os.getpid(),
        at=NOW,
        lease_seconds=2,
    )

    assert not repo.acquire_daemon(
        owner_id="owner-b",
        pid=os.getpid() + 1,
        at=NOW + timedelta(seconds=3),
        lease_seconds=2,
    )
    status = repo.daemon_status()
    assert status.owner_id == "owner-a"
    assert status.pid == os.getpid()


def test_active_lease_with_missing_heartbeat_still_fences_live_daemon_pid(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert repo.acquire_daemon(
        owner_id="owner-a",
        pid=os.getpid(),
        at=NOW,
        lease_seconds=2,
    )
    with repo.state.transaction() as connection:
        connection.execute(
            "UPDATE continuous_monitor_daemon SET heartbeat_at=NULL "
            "WHERE singleton_id='default'"
        )

    assert not repo.acquire_daemon(
        owner_id="owner-b",
        pid=os.getpid() + 1,
        at=NOW + timedelta(seconds=3),
        lease_seconds=2,
    )
    assert repo.daemon_status().owner_id == "owner-a"


def test_daemon_start_handshake_uses_owner_identity_not_launcher_pid() -> None:
    status = SimpleNamespace(
        state=SimpleNamespace(value="RUNNING"),
        owner_id="continuous-monitor:test-owner",
        pid=33952,
    )

    assert _daemon_start_matches(status, "continuous-monitor:test-owner")
    assert not _daemon_start_matches(status, "continuous-monitor:other-owner")


def test_continuous_monitor_start_wires_owner_to_spawn_and_accepts_pid_mismatch(
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
    captured: dict[str, object] = {}

    def fake_spawn(
        project_root: Path,
        *,
        live: bool,
        interval_seconds: int,
        owner_id: str | None = None,
    ) -> int:
        captured.update(
            project_root=project_root,
            live=live,
            interval_seconds=interval_seconds,
            owner_id=owner_id,
        )
        return 111

    def fake_status(self) -> MonitorDaemonStatus:
        del self
        owner_id = captured.get("owner_id")
        if isinstance(owner_id, str):
            return MonitorDaemonStatus(
                owner_id=owner_id,
                pid=222,
                state=MonitorDaemonState.RUNNING,
                started_at=NOW,
                heartbeat_at=NOW,
                updated_at=NOW,
            )
        return MonitorDaemonStatus(state=MonitorDaemonState.STOPPED, updated_at=NOW)

    monkeypatch.setattr(monitor_cli, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(ContinuousMonitorRepository, "daemon_status", fake_status)
    app = typer.Typer()
    emitted: list[object] = []
    register_continuous_monitor_commands(app, lambda: (paths, state, objects), emitted.append)

    result = CliRunner().invoke(
        app,
        ["continuous-monitor-start", "--live", "--interval-seconds", "60"],
    )

    assert result.exit_code == 0, result.output
    assert captured["project_root"] == PROJECT_ROOT
    assert captured["live"] is True
    assert captured["interval_seconds"] == 60
    owner_id = captured["owner_id"]
    assert isinstance(owner_id, str) and owner_id.startswith("continuous-monitor:")
    assert isinstance(emitted[-1], dict)
    assert emitted[-1]["status"] == "STARTED"
    assert emitted[-1]["pid"] == 222


def test_spawn_daemon_threads_owner_id_into_subprocess_command(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class _Process:
        pid = 111

    def fake_popen(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(monitor_daemon.subprocess, "Popen", fake_popen)

    pid = monitor_daemon.spawn_daemon(
        tmp_path,
        live=True,
        interval_seconds=60,
        owner_id="continuous-monitor:test-owner",
    )

    assert pid == 111
    command = captured["command"]
    assert isinstance(command, list)
    owner_index = command.index("--owner-id")
    assert command[owner_index + 1] == "continuous-monitor:test-owner"
    assert "--live" in command
    assert "continuous-monitor-daemon" in command
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["env"]["ASTOCK_PROJECT_ROOT"] == str(tmp_path.resolve())


def test_long_cycle_renews_lease_and_blocks_second_daemon_owner(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base_config = load_continuous_monitor_config(
        PROJECT_ROOT / "configs" / "continuous_monitor.yaml"
    )
    config = replace(
        base_config,
        daemon=replace(base_config.daemon, lease_seconds=2, heartbeat_seconds=1),
    )
    cycle_entered = Event()
    release_cycle = Event()
    exit_codes: list[int] = []

    class _BlockingService:
        def run_cycle(
            self,
            *,
            owner_id: str,
            live: bool,
            now: datetime,
            lease_guard=None,
        ):
            assert owner_id == "owner-a"
            assert live is True
            assert now.tzinfo is not None
            assert lease_guard is not None
            lease_guard()
            cycle_entered.set()
            assert release_cycle.wait(timeout=8.0)
            lease_guard()
            return SimpleNamespace(run_id="monitor-run:long-cycle")

    def daemon_target() -> None:
        exit_codes.append(
            monitor_daemon.run_daemon(
                cast(ContinuousMonitorService, _BlockingService()),
                repo,
                config,
                live=True,
                interval_seconds=1,
                owner_id="owner-a",
            )
        )

    daemon_thread = Thread(target=daemon_target, daemon=True)
    daemon_thread.start()
    assert cycle_entered.wait(timeout=2.0)
    initial = repo.daemon_status()
    assert initial.heartbeat_at is not None

    time.sleep(2.2)
    running = repo.daemon_status()
    assert running.state is MonitorDaemonState.RUNNING
    assert running.heartbeat_at is not None and running.heartbeat_at > initial.heartbeat_at
    assert not repo.acquire_daemon(
        owner_id="owner-b",
        pid=222,
        at=datetime.now(UTC),
        lease_seconds=2,
    )

    assert repo.request_daemon_stop(at=datetime.now(UTC))
    stopping = repo.daemon_status()
    assert stopping.state is MonitorDaemonState.STOPPING
    assert stopping.heartbeat_at is not None
    time.sleep(2.2)
    stopping_later = repo.daemon_status()
    assert stopping_later.state is MonitorDaemonState.STOPPING
    assert (
        stopping_later.heartbeat_at is not None
        and stopping_later.heartbeat_at > stopping.heartbeat_at
    )
    assert not repo.acquire_daemon(
        owner_id="owner-b",
        pid=222,
        at=datetime.now(UTC),
        lease_seconds=2,
    )

    release_cycle.set()
    daemon_thread.join(timeout=3.0)
    assert not daemon_thread.is_alive()
    assert exit_codes == [0]
    final = repo.daemon_status()
    assert final.state is MonitorDaemonState.STOPPED
    assert final.last_run_id == "monitor-run:long-cycle"


def test_heartbeat_exception_fails_closed_before_next_cycle(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    base_config = load_continuous_monitor_config(
        PROJECT_ROOT / "configs" / "continuous_monitor.yaml"
    )
    config = replace(
        base_config,
        daemon=replace(base_config.daemon, lease_seconds=2, heartbeat_seconds=1),
    )
    cycle_entered = Event()
    heartbeat_attempted = Event()
    release_cycle = Event()
    cycle_count = 0
    exit_codes: list[int] = []

    class _BlockingService:
        def run_cycle(
            self,
            *,
            owner_id: str,
            live: bool,
            now: datetime,
            lease_guard=None,
        ):
            nonlocal cycle_count
            cycle_count += 1
            assert lease_guard is not None
            cycle_entered.set()
            while not release_cycle.wait(timeout=0.05):
                lease_guard()
            lease_guard()
            return SimpleNamespace(run_id=f"monitor-run:lease-loss:{cycle_count}")

    def failed_heartbeat(owner_id: str, *, at: datetime, last_run_id: str | None = None) -> bool:
        del owner_id, at, last_run_id
        heartbeat_attempted.set()
        raise RuntimeError("simulated durable lease renewal failure")

    monkeypatch.setattr(repo, "heartbeat_daemon", failed_heartbeat)

    daemon_thread = Thread(
        target=lambda: exit_codes.append(
            monitor_daemon.run_daemon(
                cast(ContinuousMonitorService, _BlockingService()),
                repo,
                config,
                live=True,
                interval_seconds=1,
                owner_id="owner-a",
            )
        ),
        daemon=True,
    )
    daemon_thread.start()
    assert cycle_entered.wait(timeout=2.0)
    assert heartbeat_attempted.wait(timeout=2.0)
    release_cycle.set()
    daemon_thread.join(timeout=3.0)

    assert not daemon_thread.is_alive()
    assert exit_codes == [4]
    assert cycle_count == 1
    assert repo.daemon_status().state is MonitorDaemonState.FAILED


def test_cycle_repairs_persisted_event_task_and_rule_half_commit(
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
    target = service.repo.enroll(
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
    rule = service.repo.add_rule(
        MonitorRuleRequest(
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
    )
    payload: dict[str, object] = {
        "rule_id": rule.rule_id,
        "metric": rule.metric.value,
        "value": 1399.0,
    }
    event = MonitorEvent(
        event_id="monitor-event:half-commit",
        target_id=target.target_id,
        company_id=target.company_id,
        event_type=MonitorEventType.PRICE_TRIGGER,
        severity=MonitorSeverity.MATERIAL,
        observed_at=NOW,
        available_at=NOW,
        source=MonitorSource.MARKET_60M,
        source_ref=rule.rule_id,
        payload=payload,
        payload_hash=content_hash(payload),
        dedupe_key="half-commit-rule-event",
        affected_modules=[ResearchModule.COMMITTEE, ResearchModule.TRADING_CLASSIFICATION],
        requires_research=True,
    )
    _, inserted = service.repo.record_event(event)
    assert inserted is True
    assert service.repo.list_tasks() == []
    assert service.repo.rules(target.target_id)[0].last_triggered_at is None

    monkeypatch.setattr(service, "sync_paper_targets", lambda at, lease_guard=None: [])
    monkeypatch.setattr(service.repo, "active_targets", lambda limit: [])
    first = service.run_cycle(owner_id="repair-owner", live=False, now=NOW + timedelta(minutes=1))

    tasks = service.repo.list_tasks()
    assert first.task_count == 1
    assert "RECOVERED_RULE_TRIGGER_STATE:1" in first.findings
    assert "RECOVERED_RESEARCH_TASKS:1" in first.findings
    assert len(tasks) == 1 and tasks[0]["event_id"] == event.event_id
    repaired_rule = service.repo.rules(target.target_id)[0]
    assert repaired_rule.last_triggered_at == event.available_at

    second = service.run_cycle(owner_id="repair-owner", live=False, now=NOW + timedelta(minutes=2))
    assert second.task_count == 0
    assert not any(item.startswith("RECOVERED_") for item in second.findings)
    assert len(service.repo.list_tasks()) == 1


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
