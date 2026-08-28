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
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.schemas import (
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _request() -> DisclosureSearchRequest:
    return DisclosureSearchRequest(
        symbol="000001",
        exchange=DisclosureExchange.SZSE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 7, 13),
        category=DisclosureCategory.ANNUAL_REPORT,
    )


def _provider(tmp_path: Path, handler: object) -> CninfoDisclosureProvider:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return CninfoDisclosureProvider(objects, state, client=client)


def _discovery() -> bytes:
    return json.dumps(
        {
            "totalAnnouncement": 1,
            "totalpages": 1,
            "announcements": [{"secCode": "000001", "orgId": "gssz0000001"}],
        }
    ).encode()


def _announcement(announcement_id: str) -> dict[str, object]:
    return {
        "secCode": "000001",
        "secName": "平安银行",
        "orgId": "gssz0000001",
        "announcementId": announcement_id,
        "announcementTitle": "公告1",
        "announcementTime": 1773331200000,
        "adjunctUrl": f"finalpage/2026-03-13/{announcement_id}.PDF",
    }


def test_search_all_fails_closed_when_has_more_survives_exhausted_total(
    tmp_path: Path,
) -> None:
    exact = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalRecordNum": 1,
            "totalpages": 1,
            "hasMore": True,
            "announcements": [_announcement("terminal-conflict-1")],
        }
    ).encode()

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        return httpx.Response(
            200,
            content=exact if form.get("stock") else _discovery(),
            request=http_request,
        )

    cninfo = _provider(tmp_path, handler)
    with pytest.raises(ProviderError, match="hasMore after total_count") as captured:
        cninfo.search_all(_request())
    assert captured.value.failure_class is FailureClass.DATA_QUALITY


def test_search_all_fails_closed_when_terminal_flag_contradicts_total_pages(
    tmp_path: Path,
) -> None:
    exact = json.dumps(
        {
            "totalAnnouncement": 1,
            "totalRecordNum": 1,
            "totalpages": 2,
            "hasMore": False,
            "announcements": [_announcement("terminal-pages-conflict-1")],
        }
    ).encode()

    def handler(http_request: httpx.Request) -> httpx.Response:
        form = parse_qs(http_request.content.decode())
        return httpx.Response(
            200,
            content=exact if form.get("stock") else _discovery(),
            request=http_request,
        )

    cninfo = _provider(tmp_path, handler)
    with pytest.raises(ProviderError, match="terminal proof contradicts") as captured:
        cninfo.search_all(_request())
    assert captured.value.failure_class is FailureClass.DATA_QUALITY
