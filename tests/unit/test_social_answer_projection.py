"""Public investment answers incorporate verified social leads as non-authoritative context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.investor_orchestration.answer_projection import _community_projection
from astock.social_sources.models import (
    SocialLead,
    SocialPlatform,
    SocialPlatformCoverage,
    SocialSearchResult,
)


def _result(*, complete: bool, leads: bool) -> SocialSearchResult:
    now = datetime(2026, 11, 11, 2, 0, tzinfo=UTC)
    coverage = tuple(
        SocialPlatformCoverage(
            platform=platform,
            start=now - timedelta(days=7),
            end=now,
            query_count=1,
            completed_query_count=1 if complete else 0,
            raw_item_count=int(leads),
            accepted_count=int(leads),
            filtered_time_count=0,
            filtered_content_count=0,
            duplicate_count=0,
            complete=complete,
            checked_through=now if complete else now - timedelta(days=7),
            snapshot_ids=("snapshot:source",),
        )
        for platform in (SocialPlatform.XUEQIU, SocialPlatform.ZHIHU)
    )
    items = ()
    if leads:
        items = (
            SocialLead(
                lead_id="xueqiu:original-1",
                platform=SocialPlatform.XUEQIU,
                symbol="002475",
                company_id="002475",
                company_name="立讯精密",
                url="https://xueqiu.com/12345/98765",
                title="立讯精密经营风险需要进一步核查",
                text="市场讨论存在分歧，尚未作为正式事实确认",
                published_at=now - timedelta(days=1),
                captured_at=now,
                snapshot_id="snapshot:source",
                matched_queries=("立讯精密",),
                sentiment_hint="NEGATIVE_HINT",
            ),
        )
    return SocialSearchResult(
        result_artifact_id="SocialSearchResult:fixture",
        receipt_id="SocialSearchReceipt:fixture",
        snapshot_id="snapshot:result",
        snapshot_ids=("snapshot:source", "snapshot:result"),
        leads=items,
        coverage=coverage,
        complete=complete,
    )


def test_social_projection_preserves_platform_source_and_review_boundary() -> None:
    reasons, risks = _community_projection((_result(complete=True, leads=True),))
    assert any("近七日" in text for text in reasons)
    assert any("雪球" in text and "https://xueqiu.com/" in text for text in reasons)
    assert any("尚未作为公司事实核实" in text for text in reasons)
    assert any("不可单独" in text for text in risks)


def test_day_precision_uses_beijing_date_not_previous_utc_day() -> None:
    recorded = _result(complete=False, leads=True)
    day = recorded.leads[0].model_copy(
        update={
            "published_at": datetime(2026, 11, 6, 16, tzinfo=UTC),
            "published_precision": "DAY",
            "published_raw": "11-07",
        }
    )
    reasons, _ = _community_projection((
        recorded.model_copy(update={"leads": (day,)}),
    ))
    assert any("2026-11-07，日期精度仅到日" in value for value in reasons)
    assert not any("2026-11-06" in value for value in reasons)


def test_partial_social_coverage_is_not_reported_as_no_risks() -> None:
    reasons, risks = _community_projection((_result(complete=False, leads=False),))
    assert reasons == ()
    assert any("尚未覆盖完整窗口" in value for value in risks)
    assert not any("已完成" in value for value in reasons)


def test_completed_empty_search_is_not_proof_of_no_adverse_news() -> None:
    reasons, _ = _community_projection((_result(complete=True, leads=False),))
    assert any("不代表不存在" in value for value in reasons)


def test_unexpected_social_output_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="unexpected output schema"):
        _community_projection((object(),))  # type: ignore[arg-type]
