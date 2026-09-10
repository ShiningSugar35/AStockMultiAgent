from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from astock.investor_orchestration.models import (
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ScheduledWindow,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService, policy_from_config
from astock.investor_orchestration.scheduled_preparation import ScheduledSourcePreparationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.monitoring.news import GdeltNewsLeadProvider, NewsSearchReceipt
from astock.schemas import Frequency

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class FakeNews:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search_checked(
        self,
        *,
        names: list[str],
        symbol: str,
        start: datetime,
        end: datetime,
        max_records: int,
    ) -> NewsSearchReceipt:
        self.calls.append(
            {
                "names": names,
                "symbol": symbol,
                "start": start,
                "end": end,
                "max_records": max_records,
            }
        )
        query = GdeltNewsLeadProvider.query_text(names=names, symbol=symbol)
        return NewsSearchReceipt(
            snapshot_id=f"news:{len(self.calls)}",
            query=query,
            start=start,
            end=end,
            checked_at=end + timedelta(milliseconds=100),
            max_records=max_records,
            raw_count=0,
            rejected_count=0,
            duplicate_count=0,
            leads=(),
        )


class FakeDisclosures:
    def __init__(self) -> None:
        self.calls = 0

    def search_all(self, request: Any, *, max_pages: int) -> list[Any]:
        self.calls += 1
        return [SimpleNamespace(request=request, max_pages=max_pages)]


class FakeMacro:
    def __init__(self) -> None:
        self.calls = 0

    def specs(self) -> tuple[str, ...]:
        return ("policy-a", "policy-b")

    def capture_checked(self, spec: str, *, live: bool) -> Any:
        assert live
        self.calls += 1
        return SimpleNamespace(
            capture_artifact_id=f"policy:{spec}",
            release=SimpleNamespace(authority="NBS", release_family=spec),
        )


def _pending(
    tmp_path: Path,
    *,
    window: ScheduledWindow = ScheduledWindow.INTRADAY,
) -> tuple[InvestorOrchestrationStore, ScheduledRunRequest]:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    config = yaml.safe_load(
        (ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    policy = policy_from_config(config)
    scheduler = ScheduledResearchService(store, InvestorSessionPreflightService(store))
    scheduler.register_policy(policy)
    wake = datetime.now(UTC) - timedelta(minutes=1)
    binding = ScheduledTaskBinding(
        binding_id="auto-source-binding",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="RECORDED_AUTO_SOURCE_TEST",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        confirmed_at=wake - timedelta(minutes=1),
        consent_hash="auto-source-consent",
    )
    scheduler.register_binding(binding)
    request = ScheduledRunRequest(
        run_id="auto-source-run",
        binding_id=binding.binding_id,
        schedule_bucket=f"2026-09-09:{window.value}:recorded",
        window=window,
        domains=tuple(ScheduledDomain),
        requested_at=wake,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key="auto-source-run",
    )
    pending = scheduler.run(request)
    assert pending.outcome.value == "DEGRADED"
    assert pending.preflight_receipt_id is None
    return store, request


def _service(
    store: InvestorOrchestrationStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start: datetime,
    max_external_calls: int = 40,
) -> tuple[ScheduledSourcePreparationService, FakeNews, FakeDisclosures, FakeMacro, list[Any]]:
    news = FakeNews()
    disclosures = FakeDisclosures()
    macro = FakeMacro()
    market_calls: list[Any] = []
    service = ScheduledSourcePreparationService(
        store,
        now=Clock(start),
        market_capture=lambda instrument, frequency, as_of: (
            market_calls.append((instrument, frequency, as_of))
            or f"market:{instrument}:{frequency.value}"
        ),
        news_provider=news,
        disclosure_provider=disclosures,
        macro_capture=macro,
        industry_name_resolver=lambda _instrument, _at: "recorded liquor industry",
        company_names_resolver=lambda _instrument, _at: ["recorded issuer"],
        max_external_calls=max_external_calls,
        disclosure_max_pages=2,
    )
    subjects = {domain: ("XSHG:600519",) for domain in ScheduledDomain}
    monkeypatch.setattr(service, "_subjects", lambda _request, _as_of: subjects)
    monkeypatch.setattr(
        service,
        "_register_disclosure_batches",
        lambda _batches: ("disclosure:recorded",),
    )
    counter = {"value": 0}

    def register(request: Any) -> str:
        counter["value"] += 1
        return f"source-report:{request.domain.value}:{counter['value']}"

    monkeypatch.setattr(service.audit, "register", register)
    monkeypatch.setattr(
        service.audit,
        "verify_registered",
        lambda artifact_id: SimpleNamespace(
            all_families_checked=True,
            report_hash=f"hash:{artifact_id}",
            checks=(),
        ),
    )
    return service, news, disclosures, macro, market_calls


@pytest.mark.parametrize(
    ("window", "expected_frequency"),
    [
        (ScheduledWindow.PRE_OPEN, Frequency.H1),
        (ScheduledWindow.INTRADAY, Frequency.M5),
        (ScheduledWindow.POST_CLOSE, Frequency.H1),
    ],
)
def test_pending_bucket_automatically_stages_three_domain_five_family_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: ScheduledWindow,
    expected_frequency: Frequency,
) -> None:
    store, wake_request = _pending(tmp_path, window=window)
    start = wake_request.requested_at + timedelta(seconds=10)
    service, news, disclosures, macro, market_calls = _service(
        store,
        monkeypatch,
        start=start,
    )

    result = service.prepare_pending(
        wake_request.binding_id,
        wake_request.schedule_bucket,
        live=True,
    )

    assert result.request.requested_at > wake_request.requested_at
    assert result.request.source_interval_end == start
    assert result.request.source_interval_start == start - timedelta(days=1)
    assert len(result.source_report_ids) == 3
    assert result.subject_bindings == tuple(
        sorted(
            ((domain, "XSHG:600519") for domain in ScheduledDomain),
            key=lambda item: item[0].value,
        )
    )
    assert result.logical_external_call_budget == 9
    assert len(news.calls) == 2
    assert news.calls[0]["symbol"] == "600519"
    assert news.calls[1]["symbol"] == ""
    assert news.calls[0]["names"] != news.calls[1]["names"]
    assert disclosures.calls == 1
    assert macro.calls == 2
    assert len(market_calls) == 1
    assert result.request.window is window
    assert market_calls[0][1] is expected_frequency
    checkpoint = store.get_scheduled_checkpoint(
        wake_request.binding_id,
        wake_request.schedule_bucket,
    )
    assert checkpoint is not None and checkpoint["status"] == "READY"
    staged = ScheduledRunRequest.model_validate(checkpoint["request"])
    assert staged == result.request
    assert staged.semantic_capability_receipt_id is None
    assert store.completed_schedule_buckets(wake_request.binding_id) == set()

    repeated = service.prepare_pending(
        wake_request.binding_id,
        wake_request.schedule_bucket,
        live=True,
    )
    assert repeated.request == result.request
    assert macro.calls == 2 and len(news.calls) == 2 and disclosures.calls == 1


def test_source_preparation_requires_explicit_live_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request = _pending(tmp_path)
    service, news, disclosures, macro, market_calls = _service(
        store,
        monkeypatch,
        start=request.requested_at + timedelta(seconds=10),
    )
    with pytest.raises(ValueError, match="live opt-in"):
        service.prepare_pending(request.binding_id, request.schedule_bucket, live=False)
    assert news.calls == [] and disclosures.calls == 0 and macro.calls == 0 and market_calls == []


def test_source_preparation_fails_before_network_when_budget_cannot_cover_subjects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request = _pending(tmp_path)
    service, news, disclosures, macro, market_calls = _service(
        store,
        monkeypatch,
        start=request.requested_at + timedelta(seconds=10),
        max_external_calls=8,
    )
    with pytest.raises(ValueError, match="external-call budget"):
        service.prepare_pending(request.binding_id, request.schedule_bucket, live=True)
    assert news.calls == [] and disclosures.calls == 0 and macro.calls == 0 and market_calls == []
    checkpoint = store.get_scheduled_checkpoint(request.binding_id, request.schedule_bucket)
    assert checkpoint is not None and checkpoint["status"] == "PENDING_INPUTS"


def test_subject_drift_during_capture_does_not_stage_partial_source_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request = _pending(tmp_path)
    service, news, disclosures, macro, market_calls = _service(
        store,
        monkeypatch,
        start=request.requested_at + timedelta(seconds=10),
    )
    calls = 0

    def drifting(_request: ScheduledRunRequest, _as_of: datetime):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {domain: ("XSHG:600519",) for domain in ScheduledDomain}
        return {
            **{domain: ("XSHG:600519",) for domain in ScheduledDomain},
            ScheduledDomain.WATCHLIST: ("XSHG:600519", "XSHE:000001"),
        }

    monkeypatch.setattr(service, "_subjects", drifting)
    with pytest.raises(ValueError, match="subject set changed"):
        service.prepare_pending(request.binding_id, request.schedule_bucket, live=True)
    checkpoint = store.get_scheduled_checkpoint(request.binding_id, request.schedule_bucket)
    assert checkpoint is not None and checkpoint["status"] == "PENDING_INPUTS"
    staged = ScheduledRunRequest.model_validate(checkpoint["request"])
    assert staged.source_coverage_artifact_ids == ()
    assert macro.calls == 2 and len(news.calls) == 2 and disclosures.calls == 1
    assert len(market_calls) == 1
