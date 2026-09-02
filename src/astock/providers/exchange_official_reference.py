"""Primary-official SSE/SZSE A-share instrument-master adapters."""

from __future__ import annotations

import io
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.providers.http_resilience import HttpClientLike
from astock.schemas import FetchStatus, Market, SourceSnapshot

_SSE_QUERY_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
_SSE_LIST_PAGE = "https://www.sse.com.cn/assortment/stock/list/share/"
_SSE_SQL_ID = "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
_SSE_PAGE_SIZE = 2000
_SSE_MAX_PAGES = 10

_SZSE_DATA_URL = "https://www.szse.cn/api/report/ShowReport/data"
_SZSE_XLSX_URL = "https://www.szse.cn/api/report/ShowReport"
_SZSE_LIST_PAGE = "https://www.szse.cn/market/product/stock/list/index.html"
_SZSE_CATALOG = "1110"
_SZSE_TAB = "tab1"
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class ExchangeOfficialReferenceError(ValueError):
    """Official exchange master could not prove its bounded denominator."""


class _OfficialMasterBase(HttpProviderBase):
    market: Market
    minimum_rows: int

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        fixture_root: Path,
        *,
        client: HttpClientLike | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        super().__init__(object_store, state, client=client, timeout_seconds=timeout_seconds)
        self.fixture_root = fixture_root.resolve()

    def fetch_master(
        self, market: Market, *, live: bool = False
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if market is not self.market:
            raise ExchangeOfficialReferenceError(
                f"{self.provider_id} only supports {self.market.value}"
            )
        if live:
            payload, raws = self._live_master()
            return payload, self._persist_aggregate(payload, raws)
        return self._recorded_master()

    def _recorded_master(self) -> tuple[dict[str, object], SourceSnapshot]:
        path = self.fixture_root / "instrument_master.json"
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExchangeOfficialReferenceError(
                f"Invalid recorded official master fixture: {path}"
            ) from exc
        self._validate(payload)
        ref = self.object_store.put_bytes(raw)
        captured = _captured_at(payload)
        snapshot = SourceSnapshot(
            created_at=captured,
            snapshot_id=f"{self.provider_id}:recorded:{ref.sha256}",
            source_id=self.provider_id,
            object_sha256=ref.sha256,
            fetched_at=captured,
            available_to_system_at=captured,
            source_url=str(payload["source_url"]),
            mime="application/json",
            byte_size=ref.byte_size,
            headers_hash=content_hash({"mode": "RECORDED_OFFICIAL_FREEZE"}),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_OFFICIAL_EXCHANGE",
        )
        self.state.register_snapshot(snapshot)
        return payload, snapshot

    def _persist_aggregate(
        self, payload: dict[str, object], raws: list[SourceSnapshot]
    ) -> SourceSnapshot:
        self._validate(payload)
        ref = self.object_store.put_bytes(canonical_json_bytes(payload))
        observed = max(item.available_to_system_at for item in raws)
        snapshot = SourceSnapshot(
            created_at=observed,
            snapshot_id=f"{self.provider_id}:aggregate:{ref.sha256}",
            source_id=self.provider_id,
            object_sha256=ref.sha256,
            fetched_at=observed,
            available_to_system_at=observed,
            source_url=str(payload["source_url"]),
            mime="application/json",
            byte_size=ref.byte_size,
            headers_hash=content_hash(
                {"raw_snapshot_ids": [item.snapshot_id for item in raws]}
            ),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_OFFICIAL_EXCHANGE",
        )
        self.state.register_snapshot(snapshot)
        return snapshot

    def _validate(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise ExchangeOfficialReferenceError("Official master must be an object")
        request = payload.get("_astock_request")
        rows = payload.get("rows")
        if (
            payload.get("schema_version") != "exchange-official-master-v1"
            or payload.get("_astock_source") != self.provider_id
            or not isinstance(request, dict)
            or request.get("purpose") != "INSTRUMENT_MASTER"
            or request.get("market") != self.market.value
            or payload.get("complete") is not True
            or not isinstance(rows, list)
            or any(not isinstance(item, dict) for item in rows)
        ):
            raise ExchangeOfficialReferenceError("Official master provenance is invalid")
        try:
            total = int(str(payload["total"]))
            denominator = int(str(payload["coverage_denominator"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ExchangeOfficialReferenceError(
                "Official master denominator is invalid"
            ) from exc
        if total != denominator or total != len(rows) or total < self.minimum_rows:
            raise ExchangeOfficialReferenceError(
                "Official master denominator is not proven by its frozen rows"
            )
        codes: list[str] = []
        for item in rows:
            assert isinstance(item, dict)
            code = str(item.get("code") or "")
            name = str(item.get("name") or "").strip()
            if len(code) != 6 or not code.isdigit() or not name:
                raise ExchangeOfficialReferenceError("Official master row is invalid")
            raw_listing = item.get("listing_date")
            if raw_listing is not None:
                try:
                    date.fromisoformat(str(raw_listing))
                except ValueError as exc:
                    raise ExchangeOfficialReferenceError(
                        "Official master listing date is invalid"
                    ) from exc
            codes.append(code)
        if len(codes) != len(set(codes)) or codes != sorted(codes):
            raise ExchangeOfficialReferenceError(
                "Official master must be unique and sorted"
            )
        _captured_at(payload)

    def _live_master(self) -> tuple[dict[str, object], list[SourceSnapshot]]:
        raise NotImplementedError


class SseOfficialReferenceProvider(_OfficialMasterBase):
    provider_id = "sse-official-reference"
    market = Market.XSHG
    minimum_rows = 1500

    def _live_master(self) -> tuple[dict[str, object], list[SourceSnapshot]]:
        page_count: int | None = None
        total: int | None = None
        page_size: int | None = None
        rows: list[dict[str, object]] = []
        raws: list[SourceSnapshot] = []
        for page_no in range(1, _SSE_MAX_PAGES + 1):
            response, _ = self._get(
                _SSE_QUERY_URL,
                params={
                    "sqlId": _SSE_SQL_ID,
                    "COMPANY_STATUS": "2,4,5,7,8",
                    "STOCK_TYPE": "1,8",
                    "type": "inParams",
                    "isPagination": "true",
                    "pageHelp.pageSize": _SSE_PAGE_SIZE,
                    "pageHelp.pageNo": page_no,
                    "pageHelp.beginPage": page_no,
                    "pageHelp.cacheSize": 1,
                },
            )
            snapshot = self._persist_response(response)
            raws.append(snapshot)
            try:
                page_rows, observed_pages, observed_total, observed_size = _sse_page(
                    response.json(), page_no
                )
            except ValueError as exc:
                raise ExchangeOfficialReferenceError(
                    "SSE official response is invalid"
                ) from exc
            if page_count is None:
                page_count, total, page_size = (
                    observed_pages,
                    observed_total,
                    observed_size,
                )
                if page_count > _SSE_MAX_PAGES:
                    raise ExchangeOfficialReferenceError(
                        "SSE pagination exceeds safety bound"
                    )
            elif (page_count, total, page_size) != (
                observed_pages,
                observed_total,
                observed_size,
            ):
                raise ExchangeOfficialReferenceError(
                    "SSE pagination metadata changed during capture"
                )
            rows.extend(_normalize_sse(item) for item in page_rows)
            if page_no == page_count:
                break
        if (
            page_count is None
            or total is None
            or page_size is None
            or len(raws) != page_count
            or len(rows) != total
        ):
            raise ExchangeOfficialReferenceError(
                "SSE terminal pagination proof is incomplete"
            )
        rows.sort(key=lambda item: str(item["code"]))
        payload: dict[str, object] = {
            "schema_version": "exchange-official-master-v1",
            "_astock_source": self.provider_id,
            "_astock_request": {
                "purpose": "INSTRUMENT_MASTER",
                "market": self.market.value,
            },
            "source_url": _SSE_LIST_PAGE,
            "captured_at": max(x.available_to_system_at for x in raws).isoformat(),
            "total": total,
            "coverage_denominator": total,
            "page_count": page_count,
            "page_size": page_size,
            "complete": True,
            "raw_snapshot_ids": [x.snapshot_id for x in raws],
            "rows": rows,
        }
        self._validate(payload)
        return payload, raws


class SzseOfficialReferenceProvider(_OfficialMasterBase):
    provider_id = "szse-official-reference"
    market = Market.XSHE
    minimum_rows = 2000

    def _live_master(self) -> tuple[dict[str, object], list[SourceSnapshot]]:
        params: dict[str, str | int] = {
            "SHOWTYPE": "JSON",
            "CATALOGID": _SZSE_CATALOG,
            "TABKEY": _SZSE_TAB,
        }
        before_response, _ = self._get(_SZSE_DATA_URL, params=params)
        before_snapshot = self._persist_response(before_response)
        before = _szse_metadata(before_response)
        xlsx_response, _ = self._get(
            _SZSE_XLSX_URL,
            params={"SHOWTYPE": "xlsx", "CATALOGID": _SZSE_CATALOG, "TABKEY": _SZSE_TAB},
        )
        xlsx_snapshot = self._persist_response(xlsx_response)
        rows = _szse_xlsx(xlsx_response.content)
        after_response, _ = self._get(_SZSE_DATA_URL, params=params)
        after_snapshot = self._persist_response(after_response)
        after = _szse_metadata(after_response)
        if before != after:
            raise ExchangeOfficialReferenceError(
                "SZSE report metadata changed during capture"
            )
        total, release_label = before
        if len(rows) != total:
            raise ExchangeOfficialReferenceError(
                "SZSE XLSX rows do not match official recordcount"
            )
        rows.sort(key=lambda item: str(item["code"]))
        raws = [before_snapshot, xlsx_snapshot, after_snapshot]
        payload: dict[str, object] = {
            "schema_version": "exchange-official-master-v1",
            "_astock_source": self.provider_id,
            "_astock_request": {
                "purpose": "INSTRUMENT_MASTER",
                "market": self.market.value,
            },
            "source_url": _SZSE_LIST_PAGE,
            "captured_at": max(x.available_to_system_at for x in raws).isoformat(),
            "official_release_label": release_label,
            "total": total,
            "coverage_denominator": total,
            "complete": True,
            "raw_snapshot_ids": [x.snapshot_id for x in raws],
            "rows": rows,
        }
        self._validate(payload)
        return payload, raws


def _sse_page(
    payload: object, expected_page_no: int
) -> tuple[list[dict[str, object]], int, int, int]:
    if not isinstance(payload, dict) or payload.get("isPagination") not in {True, "true"}:
        raise ValueError("missing SSE pagination envelope")
    page = payload.get("pageHelp")
    if not isinstance(page, dict):
        raise ValueError("missing SSE pageHelp")
    page_no = int(str(page["pageNo"]))
    page_count = int(str(page["pageCount"]))
    total = int(str(page["total"]))
    page_size = int(str(page["pageSize"]))
    data = page.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("invalid SSE page rows")
    if page_count < 1 or total < 1 or page_size < 1:
        raise ValueError("invalid SSE pagination values")
    expected_rows = (
        page_size if page_no < page_count else total - page_size * (page_count - 1)
    )
    if (
        page_no != expected_page_no
        or page_count != math.ceil(total / page_size)
        or len(data) != expected_rows
    ):
        raise ValueError("SSE terminal pagination proof failed")
    return [dict(item) for item in data], page_count, total, page_size


def _normalize_sse(row: dict[str, object]) -> dict[str, object]:
    code = str(row.get("A_STOCK_CODE") or row.get("COMPANY_CODE") or "").strip()
    name = str(row.get("SEC_NAME_CN") or row.get("COMPANY_ABBR") or "").strip()
    stock_type = str(row.get("STOCK_TYPE") or "").strip()
    if stock_type not in {"1", "8"} or len(code) != 6 or not code.isdigit() or not name:
        raise ExchangeOfficialReferenceError("SSE official A-share row is invalid")
    raw_listing = str(row.get("LIST_DATE") or "").strip()
    listing_date = None
    if raw_listing and raw_listing != "-":
        if re.fullmatch(r"\d{8}", raw_listing) is None:
            raise ExchangeOfficialReferenceError("SSE listing date is invalid")
        listing_date = date(
            int(raw_listing[:4]), int(raw_listing[4:6]), int(raw_listing[6:8])
        ).isoformat()
    delisted = str(row.get("DELIST_DATE") or "").strip()
    return {
        "code": code,
        "name": name,
        "listing_date": listing_date,
        "tradable": delisted in {"", "-"},
    }


def _szse_metadata(response: httpx.Response) -> tuple[int, str]:
    payload = response.json()
    try:
        first = payload[0]
        metadata = first["metadata"]
        total = int(str(metadata["recordcount"]))
        page_count = int(str(metadata["pagecount"]))
        page_size = int(str(metadata["pagesize"]))
        release_label = str(metadata["subname"]).strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ExchangeOfficialReferenceError("SZSE report metadata is invalid") from exc
    if (
        not isinstance(payload, list)
        or not isinstance(first, dict)
        or not isinstance(metadata, dict)
        or str(metadata.get("catalogid")) != _SZSE_CATALOG
        or str(metadata.get("tabkey")) != _SZSE_TAB
        or total < 1
        or page_size < 1
        or page_count != math.ceil(total / page_size)
        or not release_label
    ):
        raise ExchangeOfficialReferenceError("SZSE completeness metadata is invalid")
    return total, release_label


def _szse_xlsx(content: bytes) -> list[dict[str, object]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise ExchangeOfficialReferenceError("SZSE stock-list XLSX is invalid") from exc
    sheet = root.find(f"{_XLSX_NS}sheetData")
    if sheet is None or len(list(sheet)) < 2:
        raise ExchangeOfficialReferenceError("SZSE stock-list XLSX has no rows")
    rows: list[dict[str, object]] = []
    for row in list(sheet)[1:]:
        row_no = int(str(row.get("r") or "0"))
        values = {
            str(cell.get("r") or ""): "".join(
                node.text or "" for node in cell.iter() if node.tag == f"{_XLSX_NS}t"
            ).strip()
            for cell in list(row)
        }
        code = values.get(f"E{row_no}", "")
        name = values.get(f"F{row_no}", "")
        listing_text = values.get(f"G{row_no}", "")
        if len(code) != 6 or not code.isdigit() or not name:
            raise ExchangeOfficialReferenceError("SZSE official A-share row is invalid")
        try:
            listing_date = date.fromisoformat(listing_text).isoformat() if listing_text else None
        except ValueError as exc:
            raise ExchangeOfficialReferenceError("SZSE listing date is invalid") from exc
        rows.append(
            {
                "code": code,
                "name": name,
                "listing_date": listing_date,
                "tradable": True,
            }
        )
    codes = [str(item["code"]) for item in rows]
    if len(codes) != len(set(codes)):
        raise ExchangeOfficialReferenceError("SZSE XLSX has duplicate A-share codes")
    return rows


def _captured_at(payload: dict[str, object]) -> datetime:
    try:
        captured = datetime.fromisoformat(str(payload["captured_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ExchangeOfficialReferenceError("Official master captured_at is invalid") from exc
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ExchangeOfficialReferenceError("Official master captured_at must be timezone-aware")
    return captured.astimezone(UTC)


__all__ = [
    "ExchangeOfficialReferenceError",
    "SseOfficialReferenceProvider",
    "SzseOfficialReferenceProvider",
]
