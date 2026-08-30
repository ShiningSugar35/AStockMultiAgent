from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

import httpx
import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers.bse_official_reference import (
    BseOfficialCaptureError,
    BseOfficialReferenceProvider,
)
from astock.schemas import Market

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "reference" / "bse"
CALLBACK = "astockBseCallback"


def _row(code: str, name: str | None = None) -> dict[str, object]:
    return {
        "xxzqdm": code,
        "xxzqjc": name or f"测试{code}",
        "xxfcbj": "2",
        "xxzqjb": "T",
        "xxtpbz": "F",
        "xxgprq": "20240530",
        "xxhyzl": "通用设备制造业",
        "xxssdq": "北京市",
        "xxisin": f"CNE{code}",
    }


def _page(
    number: int,
    *,
    total: int,
    size: int,
    rows: list[dict[str, object]],
    total_pages: int | None = None,
    first: bool | None = None,
    last: bool | None = None,
) -> dict[str, object]:
    pages = total_pages if total_pages is not None else (total + size - 1) // size
    return {
        "content": rows,
        "firstPage": number == 0 if first is None else first,
        "lastPage": number == pages - 1 if last is None else last,
        "number": number,
        "numberOfElements": len(rows),
        "size": size,
        "totalElements": total,
        "totalPages": pages,
    }


def _client(pages: dict[int, dict[str, object]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        page_number = int(form["page"][0])
        callback = form["callback"][0]
        assert callback == CALLBACK
        body = f"{callback}({json.dumps([pages[page_number]], ensure_ascii=False)})"
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "application/javascript; charset=utf-8"},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(
    state: StateStore,
    objects: ObjectStore,
    pages: dict[int, dict[str, object]],
) -> BseOfficialReferenceProvider:
    provider = BseOfficialReferenceProvider(
        objects,
        state,
        FIXTURE_ROOT,
        client=_client(pages),
    )
    provider.page_workers = 2
    return provider


def test_recorded_bse_master_and_identity_are_complete(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    provider = BseOfficialReferenceProvider(object_store, state, FIXTURE_ROOT)

    payload, snapshot = provider.fetch_master(Market.BJSE)
    identity, identity_snapshot = provider.fetch_identity("920000", Market.BJSE)

    rows = cast(list[dict[str, object]], payload["rows"])
    assert payload["total"] == 2
    assert payload["coverage_denominator"] == 2
    assert payload["complete"] is True
    assert [item["code"] for item in rows] == ["920000", "920002"]
    assert identity["code"] == "920000"
    assert identity["provider_symbol"] == "bj920000"
    assert object_store.verify(snapshot.object_sha256)
    assert object_store.verify(identity_snapshot.object_sha256)


def test_live_bse_master_freezes_every_page_and_proves_terminal_coverage(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    pages = {
        0: _page(0, total=3, size=2, rows=[_row("920000"), _row("920001")]),
        1: _page(1, total=3, size=2, rows=[_row("920002")]),
    }

    payload, snapshot = _provider(state, object_store, pages).fetch_master(
        Market.BJSE,
        live=True,
    )

    rows = cast(list[dict[str, object]], payload["rows"])
    assert payload["total"] == 3
    assert payload["coverage_denominator"] == 3
    assert payload["page_count"] == 2
    assert payload["complete"] is True
    assert [item["code"] for item in rows] == [
        "920000",
        "920001",
        "920002",
    ]
    page_snapshot_ids = payload["page_snapshot_ids"]
    assert isinstance(page_snapshot_ids, list)
    assert len(page_snapshot_ids) == 2
    assert all(state.get_snapshot(item) is not None for item in page_snapshot_ids)
    assert all(
        object_store.verify(state.get_snapshot(item).object_sha256)  # type: ignore[union-attr]
        for item in page_snapshot_ids
    )
    assert object_store.verify(snapshot.object_sha256)


def test_bse_master_fails_closed_on_total_drift_or_duplicate_symbol(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    total_drift = {
        0: _page(0, total=3, size=2, rows=[_row("920000"), _row("920001")]),
        1: _page(
            1,
            total=4,
            size=2,
            rows=[_row("920002"), _row("920003")],
        ),
    }
    with pytest.raises(ValueError, match="total changed"):
        _provider(state, object_store, total_drift).fetch_master(Market.BJSE, live=True)

    duplicates = {
        0: _page(0, total=3, size=2, rows=[_row("920000"), _row("920001")]),
        1: _page(1, total=3, size=2, rows=[_row("920001")]),
    }
    with pytest.raises(ValueError, match="duplicate"):
        _provider(state, object_store, duplicates).fetch_master(Market.BJSE, live=True)


def test_bse_page_contradiction_preserves_raw_failure_snapshot(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    pages = {
        0: _page(
            0,
            total=3,
            size=2,
            rows=[_row("920000"), _row("920001")],
            last=True,
        )
    }

    with pytest.raises(BseOfficialCaptureError, match="terminal marker") as captured:
        _provider(state, object_store, pages).fetch_master(Market.BJSE, live=True)

    snapshot = captured.value.snapshot
    assert snapshot is not None
    assert state.get_snapshot(snapshot.snapshot_id) is not None
    assert object_store.verify(snapshot.object_sha256)


def test_bse_official_route_is_bjse_scoped(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = MarketReferenceService(
        state,
        ObjectStore(tmp_path / "objects"),
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )

    bjse = service._capability_route(
        "instrument.master",
        live=True,
        formal_use=True,
        require_complete=True,
        market=Market.BJSE,
    )
    xshg = service._capability_route(
        "instrument.master",
        live=True,
        formal_use=True,
        require_complete=True,
        market=Market.XSHG,
    )

    assert bjse[0].provider_id == "bse-official-reference"
    assert all(item.provider_id != "bse-official-reference" for item in xshg)
