"""Offline evidence probes staged while the existing full-suite tree is frozen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.monitoring import news
from astock.monitoring.news import GdeltNewsLeadProvider


@pytest.fixture
def stores(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("news-scope")
    state = StateStore(root / "state.sqlite")
    state.migrate()
    return state, ObjectStore(root / "objects")


def test_error_response_cannot_be_reported_as_an_empty_success(stores) -> None:
    state, objects = stores
    now = datetime.now(UTC)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"error": "recorded invalid query"})
        )
    ) as client:
        provider = GdeltNewsLeadProvider(objects, state, client=client)
        with pytest.raises(ValueError, match="articles|payload|response"):
            provider.search(
                names=["first-subject"],
                symbol="600519",
                start=now - timedelta(hours=2),
                end=now,
                max_records=20,
            )


def test_identical_body_different_queries_preserve_separate_capture_provenance(stores) -> None:
    state, objects = stores
    now = datetime.now(UTC).replace(microsecond=0)
    body = {
        "articles": [
            {
                "title": "recorded shared lead",
                "url": "https://example.org/lead",
                "seendate": now.strftime("%Y%m%dT%H%M%SZ"),
            }
        ]
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        provider = GdeltNewsLeadProvider(objects, state, client=client)
        first = provider.search(
            names=["issuer-scope"],
            symbol="600519",
            start=now - timedelta(hours=2),
            end=now,
            max_records=20,
        )
        second = provider.search(
            names=["industry-scope"],
            symbol="000001",
            start=now - timedelta(hours=2),
            end=now,
            max_records=20,
        )
    assert first and second
    assert first[0].snapshot_id != second[0].snapshot_id
    first_capture = state.get_snapshot(first[0].snapshot_id)
    second_capture = state.get_snapshot(second[0].snapshot_id)
    assert first_capture is not None and second_capture is not None
    assert first_capture.object_sha256 == second_capture.object_sha256
    assert "issuer-scope" in parse_qs(urlparse(first_capture.source_url).query)["query"][0]
    assert "industry-scope" in parse_qs(urlparse(second_capture.source_url).query)["query"][0]


def test_query_interval_must_be_aware_before_any_network_call(stores) -> None:
    state, objects = stores
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"articles": []})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = GdeltNewsLeadProvider(objects, state, client=client)
        with pytest.raises(ValueError, match="aware|interval|time"):
            provider.search(
                names=["issuer"],
                symbol="600519",
                start=datetime(2026, 9, 7),
                end=datetime(2026, 9, 7, 1),
                max_records=20,
            )
    assert calls == []


def test_valid_empty_response_retains_check_receipt_and_can_be_replayed(stores) -> None:
    state, objects = stores
    at = datetime.now(UTC).replace(microsecond=0)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"articles": []}))
    ) as client:
        provider = news.GdeltNewsLeadProvider(objects, state, client=client)
        result = provider.search_checked(
            names=["recorded issuer"],
            symbol="600519",
            start=at - timedelta(hours=1),
            end=at,
            max_records=20,
        )
    assert result.checked_successfully and result.raw_count == 0 and result.leads == ()
    assert result.end == at and result.checked_at >= at
    assert news.replay_news_search(state, objects, result.snapshot_id) == result


@pytest.mark.parametrize("kind", ["saturated", "invalid_date", "invalid_url"])
def test_truncation_and_rejected_rows_are_not_a_complete_check(stores, kind) -> None:
    state, objects = stores
    at = datetime.now(UTC).replace(microsecond=0)
    item = {
        "title": "recorded lead",
        "url": "https://example.org/lead",
        "seendate": at.strftime("%Y%m%dT%H%M%SZ"),
    }
    limit = 1 if kind == "saturated" else 20
    if kind == "invalid_date":
        item.pop("seendate")
    if kind == "invalid_url":
        item["url"] = "file:///private"
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"articles": [item]}))
    ) as client:
        result = news.GdeltNewsLeadProvider(objects, state, client=client).search_checked(
            names=["recorded"],
            symbol="600519",
            start=at - timedelta(hours=1),
            end=at,
            max_records=limit,
        )
    assert not result.checked_successfully
    assert result.limit_reached if kind == "saturated" else result.rejected_count == 1


def test_redirect_target_does_not_receive_a_request(stores) -> None:
    state, objects = stores
    at = datetime.now(UTC).replace(microsecond=0)
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://untrusted.invalid/destination"})

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        provider = news.GdeltNewsLeadProvider(objects, state, client=client)
        with pytest.raises(httpx.HTTPStatusError):
            provider.search(
                names=["recorded"],
                symbol="600519",
                start=at - timedelta(hours=1),
                end=at,
                max_records=20,
            )
    assert len(calls) == 1
