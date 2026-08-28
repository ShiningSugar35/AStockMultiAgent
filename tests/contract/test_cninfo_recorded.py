from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
from astock.core.source_resilience import SourceFailureClass
from astock.core.state import StateStore
from astock.documents.cninfo import CninfoDisclosureProvider, cninfo_org_id
from astock.schemas import (
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "documents" / "cninfo_annual_000001.json"
PDF_BYTES = b"%PDF-1.7\n% recorded contract fixture\n%%EOF\n"


def request() -> DisclosureSearchRequest:
    return DisclosureSearchRequest(
        symbol="000001",
        exchange=DisclosureExchange.SZSE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 7, 13),
        category=DisclosureCategory.ANNUAL_REPORT,
    )


def provider(tmp_path: Path, handler) -> tuple[CninfoDisclosureProvider, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CninfoDisclosureProvider(objects, state, client=client), state, objects


def test_recorded_search_and_download_are_snapshotted(tmp_path: Path) -> None:
    raw_index = FIXTURE.read_bytes()

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "POST":
            form = parse_qs(http_request.content.decode())
            if form.get("stock") is None:
                assert form["searchkey"] == ["000001"]
            else:
                assert form["stock"] == ["000001,gssz0000001"]
                assert form["category"] == ["category_ndbg_szsh;"]
            return httpx.Response(
                200,
                content=raw_index,
                headers={"content-type": "application/json"},
                request=http_request,
            )
        assert str(http_request.url) == (
            "https://static.cninfo.com.cn/finalpage/2026-03-21/1225022887.PDF"
        )
        return httpx.Response(
            200,
            content=PDF_BYTES,
            headers={"content-type": "application/pdf"},
            request=http_request,
        )

    cninfo, state, objects = provider(tmp_path, handler)
    batch = cninfo.search(request())
    assert batch.total_count == 1
    assert batch.total_pages == 1
    assert batch.announcements[0].title == "2025年年度报告"
    assert batch.announcements[0].document_id == "cninfo:1225022887"
    assert objects.verify(batch.raw_snapshot_id.rsplit(":", 1)[-1])

    downloaded = cninfo.download(batch.announcements[0])
    assert downloaded.document.company_ids == ["000001"]
    assert downloaded.document.rights_status == "PUBLIC_DISCLOSURE"
    assert objects.get_bytes(downloaded.snapshot.object_sha256) == PDF_BYTES
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_snapshot_detail").fetchone()[0] == 3


def test_cninfo_rate_limit_opens_capability_breaker_with_retry_after(tmp_path: Path) -> None:
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            request=http_request,
            extensions={"astock_retry_after_seconds": 600},
        )

    cninfo, state, _ = provider(tmp_path, handler)
    with pytest.raises(ProviderError) as captured:
        cninfo.search(request())

    assert calls == 1
    assert captured.value.failure_class is FailureClass.RATE_LIMITED
    status = cninfo.source_breaker.status("cninfo-disclosures", "disclosure.discover")
    assert status["state"] == "OPEN"
    assert status["failure_count"] == 1
    assert status["retry_after_at"] is not None
    with state.connect() as connection:
        row = connection.execute(
            "SELECT last_failure_class FROM source_circuit_breaker "
            "WHERE source_id=? AND capability=?",
            ("cninfo-disclosures", "disclosure.discover"),
        ).fetchone()
    assert row is not None and row["last_failure_class"] == "RATE_LIMITED"


def test_search_breaker_does_not_block_exhaustive_enumeration_capability(tmp_path: Path) -> None:
    discovery = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "000001", "orgId": "gssz0000001"}],
        }
    ).encode()
    exact = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalRecordNum": 1,
            "totalpages": 1,
            "hasMore": False,
            "announcements": [
                {
                    "secCode": "000001",
                    "secName": "平安银行",
                    "orgId": "gssz0000001",
                    "announcementId": "enumerate-isolated-1",
                    "announcementTitle": "公告1",
                    "announcementTime": 1773331200000,
                    "adjunctUrl": "finalpage/2026-03-13/enumerate-isolated-1.PDF",
                }
            ],
        }
    ).encode()

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        return httpx.Response(
            200,
            content=exact if form.get("stock") else discovery,
            request=http_request,
        )

    cninfo, _, _ = provider(tmp_path, handler)
    cninfo.source_breaker.record_failure(
        "cninfo-disclosures",
        "disclosure.discover",
        SourceFailureClass.RATE_LIMITED,
        retry_after_seconds=600,
    )

    batches = cninfo.search_all(request())

    assert sum(len(item.announcements) for item in batches) == 1
    search_status = cninfo.source_breaker.status(
        "cninfo-disclosures", "disclosure.discover"
    )
    enumerate_status = cninfo.source_breaker.status(
        "cninfo-disclosures", "disclosure.enumerate"
    )
    assert search_status["state"] == "OPEN"
    assert enumerate_status["state"] == "CLOSED"


def test_zero_totalpages_is_derived_from_total_count(tmp_path: Path) -> None:
    discovery = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "000001", "orgId": "gssz0000001"}],
        }
    ).encode()
    announcements = [
        {
            "secCode": "000001",
            "secName": "平安银行",
            "orgId": "gssz0000001",
            "announcementId": f"zero-pages-{index}",
            "announcementTitle": f"公告{index}",
            "announcementTime": 1773331200000 + index,
            "adjunctUrl": f"finalpage/2026-03-13/zero-pages-{index}.PDF",
        }
        for index in range(9)
    ]
    exact = json.dumps(
        {
            "totalAnnouncement": 9,
            "totalRecordNum": 9,
            "totalpages": 0,
            "hasMore": False,
            "announcements": announcements,
        }
    ).encode()

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        body = exact if form.get("stock") else discovery
        return httpx.Response(200, content=body, request=http_request)

    cninfo, _, _ = provider(tmp_path, handler)
    batch = cninfo.search(request())

    assert batch.total_count == 9
    assert batch.total_pages == 1
    assert not batch.has_more
    assert len(batch.announcements) == 9


def test_search_all_ignores_broken_totalpages_and_follows_has_more(tmp_path: Path) -> None:
    discovery = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "000001", "orgId": "gssz0000001"}],
        }
    ).encode()

    def announcement(index: int) -> dict[str, object]:
        return {
            "secCode": "000001",
            "secName": "平安银行",
            "orgId": "gssz0000001",
            "announcementId": f"multi-{index}",
            "announcementTitle": f"公告{index}",
            "announcementTime": 1773331200000 + index,
            "adjunctUrl": f"finalpage/2026-03-13/multi-{index}.PDF",
        }

    first_page = json.dumps(
        {
            "totalAnnouncement": 40,
            "totalRecordNum": 40,
            "totalpages": 1,
            "hasMore": True,
            "announcements": [announcement(index) for index in range(30)],
        }
    ).encode()
    second_page = json.dumps(
        {
            "totalAnnouncement": 40,
            "totalRecordNum": 40,
            "totalpages": 1,
            "hasMore": False,
            "announcements": [announcement(index) for index in range(30, 40)],
        }
    ).encode()
    requested_pages: list[int] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        if form.get("stock") is None:
            return httpx.Response(200, content=discovery, request=http_request)
        assert form["pageSize"] == ["30"]
        page = int(form["pageNum"][0])
        requested_pages.append(page)
        body = first_page if page == 1 else second_page
        return httpx.Response(200, content=body, request=http_request)

    cninfo, _, _ = provider(tmp_path, handler)
    batches = cninfo.search_all(
        request().model_copy(update={"category": DisclosureCategory.ALL, "page_size": 100})
    )

    assert requested_pages == [1, 2]
    assert [item.total_pages for item in batches] == [2, 2]
    assert [item.has_more for item in batches] == [True, False]
    assert sum(len(item.announcements) for item in batches) == 40
    assert len({item.announcement_id for batch in batches for item in batch.announcements}) == 40


def test_search_all_fails_fast_on_repeated_page_fingerprint(tmp_path: Path) -> None:
    discovery = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "000001", "orgId": "gssz0000001"}],
        }
    ).encode()
    repeated = json.dumps(
        {
            "totalAnnouncement": 40,
            "totalpages": 1,
            "hasMore": True,
            "announcements": [
                {
                    "secCode": "000001",
                    "secName": "平安银行",
                    "orgId": "gssz0000001",
                    "announcementId": f"repeat-{index}",
                    "announcementTitle": f"公告{index}",
                    "announcementTime": 1773331200000 + index,
                    "adjunctUrl": f"finalpage/2026-03-13/repeat-{index}.PDF",
                }
                for index in range(30)
            ],
        }
    ).encode()
    exact_calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal exact_calls
        form = parse_qs(http_request.content.decode())
        if form.get("stock") is None:
            return httpx.Response(200, content=discovery, request=http_request)
        exact_calls += 1
        return httpx.Response(200, content=repeated, request=http_request)

    cninfo, _, _ = provider(tmp_path, handler)
    with pytest.raises(ProviderError, match="repeated a page"):
        cninfo.search_all(request().model_copy(update={"page_size": 100}))
    assert exact_calls == 2


def test_search_all_fails_closed_on_truncated_total_count(tmp_path: Path) -> None:
    discovery = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "000001", "orgId": "gssz0000001"}],
        }
    ).encode()
    exact = json.dumps(
        {
            "totalAnnouncement": 2,
            "totalRecordNum": 2,
            "hasMore": False,
            "announcements": [
                {
                    "secCode": "000001",
                    "secName": "平安银行",
                    "orgId": "gssz0000001",
                    "announcementId": "truncated-1",
                    "announcementTitle": "公告1",
                    "announcementTime": 1773331200000,
                    "adjunctUrl": "finalpage/2026-03-13/truncated-1.PDF",
                }
            ],
        }
    ).encode()

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        return httpx.Response(
            200,
            content=exact if form.get("stock") else discovery,
            request=http_request,
        )

    cninfo, _, _ = provider(tmp_path, handler)
    with pytest.raises(ProviderError, match="terminated before total_count") as captured:
        cninfo.search_all(request())
    assert captured.value.failure_class is FailureClass.DATA_QUALITY


def test_zero_result_resolves_current_cninfo_org_id_and_retries_exact_query(
    tmp_path: Path,
) -> None:
    request_600989 = DisclosureSearchRequest(
        symbol="600989",
        exchange=DisclosureExchange.SSE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 8, 13),
        category=DisclosureCategory.ANNUAL_REPORT,
        keyword="2025年年度报告",
    )
    discovery = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "600989", "orgId": "9900019573"}],
        }
    ).encode()
    annual = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [
                {
                    "secCode": "600989",
                    "secName": "宝丰能源",
                    "orgId": "9900019573",
                    "announcementId": "annual-600989",
                    "announcementTitle": "宁夏宝丰能源集团股份有限公司2025年年度报告",
                    "announcementTime": 1773331200000,
                    "adjunctUrl": "finalpage/2026-03-13/annual-600989.PDF",
                }
            ],
        }
    ).encode()
    calls: list[dict[str, list[str]]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        calls.append(form)
        if form.get("stock") == ["600989,9900019573"]:
            body = annual
        else:
            assert form.get("stock") is None
            assert form["searchkey"] == ["600989"]
            body = discovery
        return httpx.Response(200, content=body, request=http_request)

    cninfo, state, _ = provider(tmp_path, handler)
    batch = cninfo.search(request_600989)

    assert len(calls) == 2
    assert calls[0]["column"] == ["sse"]
    assert calls[0].get("stock") is None
    assert calls[1]["stock"] == ["600989,9900019573"]
    assert batch.total_count == 1
    assert batch.announcements[0].symbol == "600989"
    assert batch.announcements[0].org_id == "9900019573"
    assert len(batch.resolution_snapshot_ids) == 1
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_snapshot_detail").fetchone()[0] == 2

    calls.clear()
    cached = cninfo.search(request_600989)
    assert cached.total_count == 1
    assert len(calls) == 1
    assert calls[0]["stock"] == ["600989,9900019573"]


def test_non_object_json_root_is_snapshotted_and_rejected_as_invalid_response(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"[]", request=http_request)

    cninfo, state, objects = provider(tmp_path, handler)

    with pytest.raises(ProviderError) as captured:
        cninfo.search(request())

    assert calls == 1
    assert captured.value.failure_class is FailureClass.INVALID_RESPONSE
    with state.connect() as connection:
        snapshots = connection.execute(
            "SELECT object_hash FROM source_snapshot_index ORDER BY snapshot_id"
        ).fetchall()
    assert len(snapshots) == 1
    assert all(objects.verify(str(row["object_hash"])) for row in snapshots)


def test_org_id_mapping_is_deterministic() -> None:
    assert cninfo_org_id("000001", DisclosureExchange.SZSE) == "gssz0000001"
    assert cninfo_org_id("600519", DisclosureExchange.SSE) == "gssh0600519"


def test_non_official_download_url_is_rejected_before_network(tmp_path: Path) -> None:
    raw_index = FIXTURE.read_bytes()

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_index, request=http_request)

    cninfo, _, _ = provider(tmp_path, handler)
    announcement = cninfo.search(request()).announcements[0].model_copy(
        update={"source_url": "https://example.com/steal.pdf"}
    )
    with pytest.raises(ProviderError) as error:
        cninfo.download(announcement)
    assert error.value.failure_class is FailureClass.POLICY_REJECTED


def test_recorded_network_disconnect_is_retryable(tmp_path: Path) -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recorded disconnect", request=http_request)

    cninfo, _, _ = provider(tmp_path, handler)
    with pytest.raises(ProviderError) as error:
        cninfo.search(request())
    assert error.value.failure_class is FailureClass.NETWORK
    assert error.value.retryable
