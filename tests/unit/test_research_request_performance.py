from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.observability import AgentObservabilityService
from astock.schemas.agent_observability import (
    ResearchRequestCacheMode,
    ResearchRequestSourceGroup,
    ResearchRequestTrace,
    ResearchRequestTraceStatus,
    ResearchTraceSpan,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path) -> AgentObservabilityService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return AgentObservabilityService(
        state,
        ObjectStore(tmp_path / "objects"),
        project_root=PROJECT_ROOT,
        manifest_root=PROJECT_ROOT,
    )


def _trace(index: int, minutes: int, *, failure: bool = False) -> ResearchRequestTrace:
    started = datetime(2026, 9, 17, 0, 0, tzinfo=UTC) + timedelta(seconds=index)
    ready = started + timedelta(minutes=minutes)
    spans = [
        ResearchTraceSpan(
            span_id=f"backend:{index}",
            name="backend fetch and compute",
            category="BACKEND",
            started_at=started,
            finished_at=min(ready, started + timedelta(minutes=10)),
            provider_calls=2,
            cache_hits=1,
            cache_misses=1,
        ),
        ResearchTraceSpan(
            span_id=f"llm:{index}",
            name="LLM synthesis",
            category="LLM",
            started_at=min(ready, started + timedelta(minutes=5)),
            finished_at=min(ready, started + timedelta(minutes=15)),
        ),
        ResearchTraceSpan(
            span_id=f"ready:{index}",
            name="ready scheduler",
            category="SCHEDULER_READY",
            started_at=started,
            finished_at=min(ready, started + timedelta(minutes=1)),
        ),
    ]
    return ResearchRequestTrace(
        trace_id="AUTO",
        request_id=f"request:{index}",
        status=(
            ResearchRequestTraceStatus.FAILED
            if failure
            else ResearchRequestTraceStatus.COMPLETED
        ),
        source_group=(
            ResearchRequestSourceGroup.FAILURE
            if failure
            else ResearchRequestSourceGroup.NORMAL
        ),
        cache_mode=ResearchRequestCacheMode.MIXED,
        started_at=started,
        answer_ready_at=ready,
        useful_output=not failure,
        spans=spans,
        finding_codes=["CONTROLLED_FAILURE"] if failure else [],
        created_at=ready,
    )


def test_research_request_percentile_contract_is_35_40_45_minutes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # Nearest-rank percentiles over 30 normal samples:
    # P50 -> item 15 = 35m, P75 -> item 23 = 40m, P90 -> item 27 = 45m.
    durations = [35] * 15 + [40] * 8 + [45] * 4 + [46] * 3
    for index, minutes in enumerate(durations):
        service.register_request_trace(_trace(index, minutes))
    # Failure cases are disclosed but do not contaminate normal-request percentiles.
    service.register_request_trace(_trace(100, 90, failure=True))
    service.register_request_trace(_trace(101, 120, failure=True))

    summary = service.request_performance(lookback_days=0)

    assert summary.sample_count == 32
    assert summary.normal_sample_count == 30
    assert summary.failure_sample_count == 2
    assert summary.p50_wall_time_ms == 35 * 60_000
    assert summary.p75_wall_time_ms == 40 * 60_000
    assert summary.p90_wall_time_ms == 45 * 60_000
    assert summary.threshold_p50_ms == 35 * 60_000
    assert summary.threshold_p75_ms == 40 * 60_000
    assert summary.threshold_p90_ms == 45 * 60_000
    assert summary.threshold_pass
    assert summary.on_time_rate_45m == pytest.approx(27 / 30)
    assert summary.useful_delivery_rate == pytest.approx(30 / 32)
    assert summary.provider_call_count == 64
    assert summary.cache_hit_count == 32
    assert summary.cache_miss_count == 32
    assert summary.backend_llm_overlap_ms > 0
    assert summary.avoidable_scheduler_idle_rate == 0.0
    assert "INSUFFICIENT_SAMPLE_FOR_RELEASE_PERCENTILES" not in summary.finding_codes
    assert "RESEARCH_SLA_PERCENTILE_EXCEEDED" not in summary.finding_codes


def test_research_request_percentile_contract_fails_when_tail_exceeds_45_minutes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    durations = [35] * 15 + [40] * 7 + [46] * 8
    for index, minutes in enumerate(durations):
        service.register_request_trace(_trace(index, minutes))

    summary = service.request_performance(lookback_days=0)

    assert summary.p50_wall_time_ms == 35 * 60_000
    assert summary.p75_wall_time_ms == 46 * 60_000
    assert summary.p90_wall_time_ms == 46 * 60_000
    assert not summary.threshold_pass
    assert "RESEARCH_SLA_PERCENTILE_EXCEEDED" in summary.finding_codes
