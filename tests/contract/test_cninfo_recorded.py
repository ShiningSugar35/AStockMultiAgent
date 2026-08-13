from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from astock.core.errors import FailureClass, ProviderError
from astock.core.object_store import ObjectStore
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
