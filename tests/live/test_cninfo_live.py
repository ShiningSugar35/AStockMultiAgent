from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.documents import CninfoDisclosureProvider
from astock.schemas import DisclosureCategory, DisclosureExchange, DisclosureSearchRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("ASTOCK_RUN_LIVE") != "1",
        reason="set ASTOCK_RUN_LIVE=1 for low-frequency external provider probes",
    ),
]


def test_cninfo_annual_report_search_and_download_live(tmp_path: Path, state) -> None:
    objects = ObjectStore(tmp_path / "objects")
    provider = CninfoDisclosureProvider(objects, state)
    request = DisclosureSearchRequest(
        symbol="000001",
        exchange=DisclosureExchange.SZSE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 7, 13),
        category=DisclosureCategory.ANNUAL_REPORT,
        page_size=5,
    )
    batch = provider.search(request)
    assert batch.total_count >= 1
    announcement = next(item for item in batch.announcements if item.symbol == "000001")
    downloaded = provider.download(announcement)
    assert downloaded.snapshot.mime in {"application/pdf", "application/octet-stream"}
    assert objects.get_bytes(downloaded.snapshot.object_sha256).lstrip().startswith(b"%PDF-")
