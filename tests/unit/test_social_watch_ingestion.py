"""Idempotent social-to-monitor research event integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.schemas import FetchStatus, Market, SourceSnapshot
from astock.schemas.continuous_monitoring import (
    MonitorSource,
    MonitorTargetEnrollRequest,
    MonitorTargetReason,
)
from astock.social_sources.models import (
    SocialLead,
    SocialPlatform,
    SocialPlatformCoverage,
    SocialQueryKind,
    SocialSearchResult,
)
from astock.social_sources.monitor import SocialWatchIngestionService

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def _case(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    repo = ContinuousMonitorRepository(state, objects)
    repo.enroll(MonitorTargetEnrollRequest(
        symbol="002475",
        company_id="002475",
        market=Market.XSHE,
        display_name="立讯精密",
        reason=MonitorTargetReason.ANALYZED,
        created_at=NOW,
    ))
    raw = objects.put_json({"text": "立讯精密立案消息"})
    snapshot = SourceSnapshot(
        snapshot_id=f"social-xueqiu-search:raw:{raw.sha256}",
        source_id="social-xueqiu-search",
        object_sha256=raw.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        source_url="https://xueqiu.com/123/98765",
        mime="application/json",
        byte_size=len(objects.get_bytes(raw.sha256)),
        fetch_status=FetchStatus.SUCCEEDED,
    )
    state.register_snapshot(snapshot)
    lead = SocialLead(
        lead_id="xueqiu:consistent",
        platform=SocialPlatform.XUEQIU,
        symbol="002475",
        company_id="002475",
        company_name="立讯精密",
        url="https://xueqiu.com/123/98765",
        title="立讯精密立案消息待核实",
        text="立讯精密公司相关立案消息尚待核实，不能当作事实。",
        published_at=NOW - timedelta(hours=1),
        captured_at=NOW,
        matched_queries=("立讯精密",),
        query_kinds=(SocialQueryKind.COMPANY,),
        is_original=True,
        is_video=False,
        snapshot_id=snapshot.snapshot_id,
    )
    result = SocialSearchResult(
        result_artifact_id="SocialSearchResult:test-watch",
        receipt_id="SocialSearchReceipt:test-watch",
        snapshot_id=snapshot.snapshot_id,
        snapshot_ids=(snapshot.snapshot_id,),
        leads=(lead,),
        coverage=(SocialPlatformCoverage(
            platform=SocialPlatform.XUEQIU,
            start=NOW - timedelta(days=7),
            end=NOW,
            query_count=1,
            completed_query_count=1,
            raw_item_count=1,
            accepted_count=1,
            filtered_time_count=0,
            filtered_content_count=0,
            duplicate_count=0,
            complete=True,
            checked_through=NOW,
            snapshot_ids=(snapshot.snapshot_id,),
        ),),
        complete=True,
    )
    result_ref = objects.put_json(result.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=result.result_artifact_id,
        artifact_type="SocialSearchResult",
        schema_version=result.schema_version,
        object_hash=result_ref.sha256,
        input_hashes=[raw.sha256],
    )
    return state, objects, repo, result


def test_recorded_community_event_and_research_task_are_idempotent(tmp_path: Path):
    state, objects, repo, result = _case(tmp_path)
    service = SocialWatchIngestionService(ROOT, state, objects)
    first = service.ingest(result, at=NOW)
    second = service.ingest(result, at=NOW + timedelta(minutes=10))
    assert first["inserted_events"] == 1
    assert first["inserted_research_tasks"] == 1
    assert second["inserted_events"] == 0
    assert second["inserted_research_tasks"] == 0
    events = repo.list_events(limit=5)
    assert len(events) == 1
    assert events[0]["source"] == MonitorSource.COMMUNITY.value
    assert len(repo.list_tasks(limit=5)) == 1


def test_social_monitor_rejects_missing_evidence(tmp_path: Path):
    state, objects, repo, result = _case(tmp_path)
    altered = result.model_copy(update={
        "leads": (result.leads[0].model_copy(
            update={"snapshot_id": "missing-snapshot"}
        ),)
    })
    service = SocialWatchIngestionService(ROOT, state, objects)
    with pytest.raises(ValueError, match="changed after capture"):
        service.ingest(altered, at=NOW)
    assert not repo.list_events()
