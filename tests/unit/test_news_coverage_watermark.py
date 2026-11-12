"""Coverage and idempotency regressions for the bounded GDELT news monitor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from astock.monitoring.news import (
    GdeltNewsLeadProvider,
    NewsLead,
    NewsSearchReceipt,
)
from astock.monitoring.service import ContinuousMonitorService
from astock.schemas.continuous_monitoring import (
    ContinuousMonitorTarget,
    MonitorEventType,
    MonitorSeverity,
    MonitorSource,
    MonitorTargetReason,
)
from astock.schemas.reference_data import Market

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def _lead(start: datetime) -> NewsLead:
    return NewsLead(
        title="立讯精密重大合同线索",
        url="https://example.com/article",
        domain="example.com",
        seen_at=start,
        language="zh",
        source_country="CN",
        snapshot_id="sample-snapshot",
    )


def test_saturated_news_window_is_bisected_without_claiming_false_coverage() -> None:
    provider = object.__new__(GdeltNewsLeadProvider)
    intervals: list[tuple[datetime, datetime]] = []

    def capture(*, names, symbol, start, end, max_records):
        del names, symbol
        intervals.append((start, end))
        length = (end - start).total_seconds()
        return NewsSearchReceipt(
            snapshot_id=f"snapshot:{len(intervals)}",
            query="立讯精密",
            start=start,
            end=end,
            checked_at=NOW,
            max_records=max_records,
            raw_count=max_records if length > 2 else 1,
            rejected_count=0,
            duplicate_count=0,
            leads=(_lead(start),),
        )

    provider.search_checked = capture
    start = NOW - timedelta(seconds=10)
    batch = provider.search_covered(
        names=["立讯精密"], symbol="002475", start=start, end=NOW,
        max_records=2, max_requests=32,
    )
    assert batch.complete
    assert batch.checked_through == NOW
    assert batch.request_count > 1
    assert any((end - begin).total_seconds() <= 2 for begin, end in intervals)
    assert len(batch) >= 1


def test_search_budget_exhaustion_retains_leads_but_not_verified_watermark() -> None:
    provider = object.__new__(GdeltNewsLeadProvider)

    def saturated(*, names, symbol, start, end, max_records):
        del names, symbol
        return NewsSearchReceipt(
            snapshot_id="saturated-snapshot",
            query="华海药业",
            start=start,
            end=end,
            checked_at=NOW,
            max_records=max_records,
            raw_count=max_records,
            rejected_count=0,
            duplicate_count=0,
            leads=(_lead(start),),
        )

    provider.search_checked = saturated
    start = NOW - timedelta(minutes=3)
    batch = provider.search_covered(
        names=["华海药业"], symbol="600521", start=start, end=NOW,
        max_records=2, max_requests=1,
    )
    assert not batch.complete
    assert batch.checked_through == start
    assert batch.request_count == 1
    assert len(batch) == 1


def test_repeated_news_lead_keeps_stable_event_identity_across_snapshots() -> None:
    target = ContinuousMonitorTarget(
        target_id="monitor-target:XSHE:002475",
        symbol="002475",
        market=Market.XSHE,
        company_id="002475",
        display_name="立讯精密",
        reasons=[MonitorTargetReason.ANALYZED],
        enrolled_at=NOW,
        updated_at=NOW,
    )
    service = object.__new__(ContinuousMonitorService)
    common: dict[str, Any] = dict(
        event_type=MonitorEventType.NEWS_LEAD,
        severity=MonitorSeverity.WATCH,
        observed_at=NOW,
        available_at=NOW,
        source=MonitorSource.GDELT,
        source_ref="gdelt-lead:stable",
        affected_modules=[],
        requires_research=False,
        dedupe_suffix="gdelt-lead:stable",
        news_lead_only=True,
    )
    first = service._event(target, payload={"title": "消息", "snapshot_id": "s1"}, **common)
    replay = service._event(target, payload={"title": "消息", "snapshot_id": "s2"}, **common)
    assert first.event_id == replay.event_id
    assert first.dedupe_key == replay.dedupe_key
    assert first.payload_hash != replay.payload_hash
