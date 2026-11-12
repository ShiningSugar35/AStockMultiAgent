"""Recorded browser community search contracts; no live network dependency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from astock.core.errors import ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.social_sources.bridge import SocialCaptureSession
from astock.social_sources.config import SocialSearchConfig
from astock.social_sources.models import (
    SocialCapturedItem,
    SocialCaptureResult,
    SocialParseStatus,
    SocialPlatform,
    SocialQueryCapture,
    SocialQueryKind,
    SocialSearchJob,
    SocialSearchQuery,
    SocialSearchRequest,
    SocialSearchResult,
)
from astock.social_sources.provider import (
    SocialBrowserSearchProvider,
    _build_queries,
    _normalize_item,
    _parse_published_at,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 10, 11, 0, tzinfo=UTC)


def _request() -> SocialSearchRequest:
    return SocialSearchRequest(
        symbol="002475",
        company_id="002475",
        company_names=("立讯精密",),
        industry_keywords=("AI铜连接",),
        management_keywords=("董事长",),
        start=NOW - timedelta(days=7),
        end=NOW,
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        minimum_text_characters=12,
        maximum_text_characters=12000,
        positive_hint_terms=("增长", "增持"),
        negative_hint_terms=("减持", "暴雷"),
    )


def _raw(platform: SocialPlatform, text: str, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "url": (
            "https://xueqiu.com/123/98765"
            if platform is SocialPlatform.XUEQIU
            else "https://www.zhihu.com/question/123/answer/456"
        ),
        "title": "立讯精密近期经营发展",
        "text": text,
        "author": "A",
        "published_at": NOW.isoformat(),
        "is_original": True,
        "is_video": False,
        "content_type": "STATUS",
    }
    payload.update(updates)
    return payload


def _normalize(
    raw: dict[str, object], platform: SocialPlatform,
    kind: SocialQueryKind = SocialQueryKind.COMPANY,
):
    return _normalize_item(
        raw,
        platform=platform,
        request=_request(),
        company_name="立讯精密",
        captured_at=NOW,
        snapshot_id="snapshot:recorded",
        query="立讯精密",
        query_kind=kind,
        config=cast(SocialSearchConfig, _config()),
    )


def test_query_families_and_week_window_filters() -> None:
    request = _request()
    for platform in SocialPlatform:
        queries = _build_queries(request, platform)
        assert len(queries) == 4
        assert {item.kind for item in queries} == set(SocialQueryKind)
        assert all("立讯精密" in query.text or query.text == "002475" for query in queries)
        assert all(query.search_url.startswith("https://") for query in queries)
    assert _normalize(
        _raw(SocialPlatform.XUEQIU, "立讯精密公司业绩增长讨论材料"),
        SocialPlatform.XUEQIU,
    ) is not None
    assert _normalize(
        _raw(SocialPlatform.XUEQIU, "立讯精密公司业绩增长讨论材料", is_original=False),
        SocialPlatform.XUEQIU,
    ) is None
    assert _normalize(
        _raw(SocialPlatform.ZHIHU, "立讯精密近期财务情况分析内容", is_video=True),
        SocialPlatform.ZHIHU,
    ) is None
    assert _normalize(
        _raw(
            SocialPlatform.ZHIHU, "立讯精密近期财务情况分析内容",
            published_at=(NOW - timedelta(days=8)).isoformat(),
        ),
        SocialPlatform.ZHIHU,
    ) is None
    assert _normalize(
        _raw(SocialPlatform.ZHIHU, "立讯精密近期财务情况分析内容", published_at=""),
        SocialPlatform.ZHIHU,
    ) is None


def test_unrelated_search_feed_and_ambiguous_management_are_rejected() -> None:
    assert _normalize(
        _raw(SocialPlatform.ZHIHU, "这是与公司完全无关的随机推荐文本内容",
             title="今日问答讨论随机话题"),
        SocialPlatform.ZHIHU,
    ) is None
    assert _normalize(
        _raw(SocialPlatform.ZHIHU, "董事长职务变化对市场的影响讨论内容",
             title="某企业董事长有何变化"),
        SocialPlatform.ZHIHU,
        SocialQueryKind.MANAGEMENT,
    ) is None
    assert _normalize(
        _raw(SocialPlatform.ZHIHU, "AI铜连接产业链的投资机会与需求分析",
             title="AI铜连接行业调研"),
        SocialPlatform.ZHIHU,
        SocialQueryKind.INDUSTRY,
    ) is not None


def test_id_is_stable_when_title_and_relative_time_change() -> None:
    a = _normalize(
        _raw(SocialPlatform.XUEQIU, "立讯精密公司业绩增长讨论材料",
             published_at=NOW.isoformat()),
        SocialPlatform.XUEQIU,
    )
    b = _normalize(
        _raw(SocialPlatform.XUEQIU, "立讯精密公司业绩增长讨论材料",
             title="立讯精密更新后的标题",
             published_at=(NOW - timedelta(hours=1)).isoformat()),
        SocialPlatform.XUEQIU,
    )
    assert a is not None and b is not None
    assert a.lead_id == b.lead_id
    assert _parse_published_at("8天前", NOW) == NOW - timedelta(days=8)


def test_real_dom_timestamp_recovery_and_precision_boundary() -> None:
    """Real captured classes: Zhihu SearchItem-time, Xueqiu date-and-source."""
    zhihu_html = (
        '<div class="List-item"><span class="ContentItem-action SearchItem-time">'
        '11-07</span></div>'
    )
    lead = _normalize(
        _raw(
            SocialPlatform.ZHIHU,
            "立讯精密公司近期财务与经营趋势讨论",
            published_at="",
            published_raw="",
            raw_excerpt=zhihu_html,
        ),
        SocialPlatform.ZHIHU,
    )
    assert lead is not None
    assert lead.published_precision == "DAY"
    assert lead.published_raw == "11-07"
    assert lead.published_at == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)

    # A day partly outside the seven-day window is not provably in-window.
    boundary = zhihu_html.replace("11-07", "11-03")
    assert _normalize(
        _raw(
            SocialPlatform.ZHIHU,
            "立讯精密公司近期财务与经营趋势讨论",
            published_at="",
            published_raw="",
            raw_excerpt=boundary,
        ),
        SocialPlatform.ZHIHU,
    ) is None

    # Older explicit years and impossible dates must not be guessed current.
    for label in ("2025-03-05", "02-30"):
        html = zhihu_html.replace("11-07", label)
        assert _normalize(
            _raw(
                SocialPlatform.ZHIHU,
                "立讯精密公司近期财务与经营趋势讨论",
                published_at="",
                published_raw="",
                raw_excerpt=html,
            ),
            SocialPlatform.ZHIHU,
        ) is None

    # Retain exact local clock time for relative Xueqiu dates.
    xueqiu_html = (
        '<a class="date-and-source" href="/123/98765">'
        '昨天 14:24<span class="source">· 来自雪球</span></a>'
    )
    post = _normalize(
        _raw(
            SocialPlatform.XUEQIU,
            "立讯精密公司业绩增长讨论材料",
            published_at="",
            published_raw="",
            raw_excerpt=xueqiu_html,
        ),
        SocialPlatform.XUEQIU,
    )
    assert post is not None
    assert post.published_precision == "RELATIVE"
    assert post.published_at == datetime(2026, 11, 9, 6, 24, tzinfo=UTC)
    assert "昨天 14:24" in post.published_raw

    # A post imported from a news feed is not an original Xueqiu opinion.
    repost_source = xueqiu_html.replace("来自雪球", "来自新闻")
    assert _normalize(
        _raw(
            SocialPlatform.XUEQIU,
            "立讯精密公司业绩增长讨论材料",
            published_at="",
            published_raw="",
            raw_excerpt=repost_source,
            is_original=True,
        ),
        SocialPlatform.XUEQIU,
    ) is None


def test_capture_session_rejects_foreign_page_and_unleased_results() -> None:
    query = SocialSearchQuery(
        query_id="xueqiu-1",
        text="立讯精密",
        kind=SocialQueryKind.COMPANY,
        search_url="https://xueqiu.com/k?q=%E7%AB%8B%E8%AE%AF%E7%B2%BE%E5%AF%86&type=status",
    )
    job = SocialSearchJob(
        job_id="job-1", session_id="session-1", platform=SocialPlatform.XUEQIU,
        queries=(query,), start=NOW - timedelta(days=7), end=NOW,
        created_at=NOW,
    )
    session = SocialCaptureSession((job,), lease_seconds=60)
    good_capture = SocialQueryCapture(
        query_id=query.query_id,
        query=query.text,
        query_kind=query.kind,
        page_url=query.search_url,
        captured_at=NOW,
        selector_version="dom-v1",
        parse_status=SocialParseStatus.EMPTY,
        no_results_detected=True,
    )
    payload = SocialCaptureResult(
        job_id=job.job_id, session_id=job.session_id, platform=job.platform,
        captured_at=NOW, query_results=(good_capture,),
    ).model_dump(mode="json")
    with pytest.raises(ValueError, match="not leased"):
        session.submit_result(payload)
    assert session.next_job(wait_seconds=0) is not None
    bad = dict(payload)
    bad["query_results"] = [
        {**good_capture.model_dump(mode="json"), "page_url": "https://xueqiu.com/"}
    ]
    with pytest.raises(ValueError, match="page does not match"):
        session.submit_result(bad)
    session.submit_result(payload)
    assert session.status()["counts"] == {"COMPLETE": 1}


def test_result_snapshots_and_replay_are_canonical(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    provider = object.__new__(SocialBrowserSearchProvider)
    provider.state = state
    provider.objects = objects
    provider.config = cast(SocialSearchConfig, _config())

    request = _request()
    jobs: list[SocialSearchJob] = []
    results: list[SocialCaptureResult] = []
    for platform in SocialPlatform:
        query = _build_queries(request, platform)[0]
        job = SocialSearchJob(
            job_id=f"{platform.value.lower()}-job",
            session_id="recorded-session",
            platform=platform, queries=(query,),
            start=request.start, end=request.end, created_at=NOW,
        )
        item = _raw(platform, "立讯精密公司业绩增长讨论材料")
        capture = SocialQueryCapture(
            query_id=query.query_id,
            query=query.text,
            query_kind=query.kind,
            page_url=query.search_url,
            captured_at=NOW,
            selector_version="dom-v1",
            parse_status=SocialParseStatus.PARSED,
            items=(SocialCapturedItem.model_validate(item),),
            has_more=False,
        )
        results.append(SocialCaptureResult(
            job_id=job.job_id, session_id=job.session_id,
            platform=platform, captured_at=NOW, query_results=(capture,),
        ))
        jobs.append(job)
    result = provider._persist_result(
        request, "recorded-session", tuple(jobs), tuple(results), NOW,
    )
    assert result.complete
    assert len(result.leads) == 2
    record = state.artifact_record(result.result_artifact_id)
    assert record is not None and record["type"] == "SocialSearchResult"
    assert objects.verify(str(record["object_hash"]))
    replay = SocialSearchResult.model_validate_json(
        objects.get_bytes(str(record["object_hash"]))
    )
    assert replay == result
    assert all(state.get_snapshot(key) is not None for key in result.snapshot_ids)

    # A page that yielded an undated, otherwise relevant card cannot prove
    # seven-day completeness even if the DOM parser itself succeeded.
    first_capture = results[0].query_results[0]
    undated = first_capture.items[0].model_copy(
        update={"published_at": "", "published_raw": "未知发布时间"}
    )
    incomplete = results[0].model_copy(
        update={"query_results": (
            first_capture.model_copy(update={"items": (undated,)}),
        )}
    )
    partial = provider._persist_result(
        request, "recorded-partial", tuple(jobs),
        (incomplete, results[1]), NOW,
    )
    assert not partial.complete
    assert partial.coverage[0].undated_count == 1
    assert partial.coverage[0].reason == "UNVERIFIABLE_PUBLISHED_AT"


def test_unpaired_source_fails_fast_without_creating_browser_credentials(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "configs"
    config_root.mkdir()
    (config_root / "social_search.yaml").write_text(
        (ROOT / "configs" / "social_search.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    state = StateStore(tmp_path / "state.sqlite")
    objects = ObjectStore(tmp_path / "objects")
    with pytest.raises(ProviderError, match="pairing is not configured"):
        SocialBrowserSearchProvider(objects, state, tmp_path)
    assert not (tmp_path / "runtime" / "local" / "social_capture.yaml").exists()
