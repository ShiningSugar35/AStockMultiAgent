"""Beijing Stock Exchange official instrument-master provider.

The BSE stock-list page exposes a bounded JSONP endpoint with explicit page,
total-page, total-element, and terminal metadata.  This adapter freezes every
raw page before validating an aggregate master and deliberately exposes no
market-price capability: official identity/coverage and secondary quote data
remain separate concerns.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.providers.http_resilience import HttpClientLike
from astock.schemas import Market, SourceSnapshot

_CALLBACK = "astockBseCallback"
_JSONP_PATTERN = re.compile(rf"^{_CALLBACK}\((.*)\)$", re.DOTALL)
_DATE_PATTERN = re.compile(r"^\d{8}$")


class BseOfficialCaptureError(ValueError):
    """Schema/coverage failure with the latest frozen raw snapshot attached."""

    def __init__(self, message: str, *, snapshot: SourceSnapshot | None = None) -> None:
        super().__init__(message)
        self.snapshot = snapshot


class BseOfficialReferenceProvider(HttpProviderBase):
    """Fetch a complete BJSE listed-stock master from the exchange-owned site."""

    provider_id = "bse-official-reference"
    endpoint = "https://www.bseinfo.net/nqxxController/nqxxCnzq.do"
    listing_page = "https://www.bseinfo.net/nq/listedcompany.html"
    max_pages = 50
    # Keep exchange acquisition inside the project-wide low-resource network
    # concurrency contract. The first page is fetched synchronously and the
    # remaining bounded pagination never fans out beyond two remote requests.
    page_workers = 2

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        fixture_root: Path,
        *,
        client: HttpClientLike | None = None,
    ) -> None:
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_master(
        self,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if market is not Market.BJSE:
            raise ValueError("BSE official master supports BJSE only")
        if not live:
            return self._recorded_master()

        first_page, first_snapshot = self._fetch_page(0)
        total_pages = _validated_page_count(first_page, 0, self.max_pages)
        page_results: dict[int, tuple[dict[str, object], SourceSnapshot]] = {
            0: (first_page, first_snapshot)
        }
        if total_pages > 1:
            workers = min(self.page_workers, total_pages - 1)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._fetch_page, page): page for page in range(1, total_pages)
                }
                for future in as_completed(futures):
                    page = futures[future]
                    page_results[page] = future.result()

        ordered = [page_results[index] for index in range(total_pages)]
        payload = _aggregate_pages([item[0] for item in ordered])
        payload["page_snapshot_ids"] = [item[1].snapshot_id for item in ordered]
        payload["created_at"] = datetime.now(UTC).isoformat()
        aggregate_response = httpx.Response(
            200,
            content=canonical_json_bytes(payload),
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("POST", self.endpoint),
        )
        aggregate_snapshot = self._persist_response(aggregate_response)
        return payload, aggregate_snapshot

    def fetch_identity(
        self,
        symbol: str,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if market is not Market.BJSE:
            raise ValueError("BSE official identity supports BJSE only")
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("BSE official identity requires one six-digit symbol")
        if not live:
            master, snapshot = self._recorded_master()
            rows = master.get("rows")
            assert isinstance(rows, list)
            matched = [
                item for item in rows if isinstance(item, dict) and item.get("code") == symbol
            ]
            if len(matched) != 1:
                raise ValueError("Recorded BSE master lacks the requested identity")
            payload = dict(matched[0])
            payload["provider_symbol"] = f"bj{symbol}"
            payload["_astock_source"] = "BSE_OFFICIAL_LIST"
            payload["_astock_request"] = {
                "market": Market.BJSE.value,
                "symbol": symbol,
                "purpose": "INSTRUMENT_IDENTITY_EXACT",
            }
            return payload, snapshot

        page, snapshot = self._fetch_page(0, symbol=symbol)
        _validated_page_count(page, 0, self.max_pages)
        content = page.get("content")
        if not isinstance(content, list):
            raise BseOfficialCaptureError("BSE identity content is malformed", snapshot=snapshot)
        normalized = [_normalize_row(item, snapshot=snapshot) for item in content]
        matched = [item for item in normalized if item["code"] == symbol]
        if len(matched) != 1 or _required_int(page, "totalElements") != 1:
            raise BseOfficialCaptureError(
                "BSE identity endpoint did not return exactly one requested stock",
                snapshot=snapshot,
            )
        payload = dict(matched[0])
        payload["provider_symbol"] = f"bj{symbol}"
        payload["_astock_source"] = "BSE_OFFICIAL_LIST"
        payload["_astock_request"] = {
            "market": Market.BJSE.value,
            "symbol": symbol,
            "purpose": "INSTRUMENT_IDENTITY_EXACT",
        }
        return payload, snapshot

    def _fetch_page(
        self,
        page: int,
        *,
        symbol: str = "",
    ) -> tuple[dict[str, object], SourceSnapshot]:
        response, _ = self._request(
            "POST",
            self.endpoint,
            data={
                "page": str(page),
                "typejb": "T",
                "xxfcbj[]": "2",
                "xxzqdm": symbol,
                "sortfield": "xxzqdm",
                "sorttype": "asc",
                "callback": _CALLBACK,
            },
        )
        snapshot = self._persist_response(response)
        try:
            payload = _decode_jsonp_page(response.text)
            _validated_page_count(payload, page, self.max_pages)
        except (TypeError, ValueError) as exc:
            raise BseOfficialCaptureError(str(exc), snapshot=snapshot) from exc
        return payload, snapshot

    def _recorded_master(self) -> tuple[dict[str, object], SourceSnapshot]:
        path = (self.fixture_root / "instrument_master.json").resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise ValueError("Missing recorded BSE official instrument-master fixture")
        content = path.read_bytes()
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Recorded BSE official fixture is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Recorded BSE official fixture root must be an object")
        _validate_aggregate(payload)
        response = httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", f"recorded://{path.as_posix()}"),
        )
        return payload, self._persist_response(response)


def _decode_jsonp_page(text: str) -> dict[str, object]:
    matched = _JSONP_PATTERN.fullmatch(text.strip())
    if matched is None:
        raise ValueError("BSE official response is not the expected JSONP callback")
    try:
        root = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("BSE official JSONP payload is invalid JSON") from exc
    if not isinstance(root, list) or len(root) != 1 or not isinstance(root[0], dict):
        raise ValueError("BSE official JSONP root must contain exactly one page object")
    return root[0]


def _required_int(payload: dict[str, object], key: str) -> int:
    raw = payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError(f"BSE official integer field is malformed: {key}")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"BSE official integer field is malformed: {key}") from exc


def _validated_page_count(page: dict[str, object], expected_page: int, max_pages: int) -> int:
    content = page.get("content")
    if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
        raise ValueError("BSE official page content is malformed")
    number = _required_int(page, "number")
    number_of_elements = _required_int(page, "numberOfElements")
    size = _required_int(page, "size")
    total_elements = _required_int(page, "totalElements")
    total_pages = _required_int(page, "totalPages")
    if number != expected_page:
        raise ValueError("BSE official page number does not match the request")
    if size <= 0 or number_of_elements != len(content):
        raise ValueError("BSE official page size metadata is inconsistent")
    expected_total_pages = 0 if total_elements == 0 else (total_elements + size - 1) // size
    if total_elements < 0 or total_pages != expected_total_pages:
        raise ValueError("BSE official total-page metadata is inconsistent")
    if total_pages <= 0 or total_pages > max_pages:
        raise ValueError("BSE official pagination exceeds the configured bound")
    if bool(page.get("firstPage")) != (number == 0):
        raise ValueError("BSE official first-page marker is contradictory")
    if bool(page.get("lastPage")) != (number == total_pages - 1):
        raise ValueError("BSE official terminal marker is contradictory")
    if number < total_pages - 1 and len(content) != size:
        raise ValueError("BSE official non-terminal page is truncated")
    if number == total_pages - 1 and len(content) != total_elements - size * number:
        raise ValueError("BSE official terminal page length is inconsistent")
    return total_pages


def _aggregate_pages(pages: list[dict[str, object]]) -> dict[str, object]:
    if not pages:
        raise ValueError("BSE official aggregation requires at least one page")
    total_pages = _validated_page_count(pages[0], 0, BseOfficialReferenceProvider.max_pages)
    if len(pages) != total_pages:
        raise ValueError("BSE official aggregation is missing one or more pages")
    total_elements = _required_int(pages[0], "totalElements")
    size = _required_int(pages[0], "size")
    normalized: list[dict[str, object]] = []
    for expected_page, page in enumerate(pages):
        _validated_page_count(page, expected_page, BseOfficialReferenceProvider.max_pages)
        if (
            _required_int(page, "totalElements") != total_elements
            or _required_int(page, "totalPages") != total_pages
        ):
            raise ValueError("BSE official total changed during pagination")
        if _required_int(page, "size") != size:
            raise ValueError("BSE official page size changed during pagination")
        content = page["content"]
        assert isinstance(content, list)
        normalized.extend(_normalize_row(item) for item in content)
    codes = [str(item["code"]) for item in normalized]
    if len(normalized) != total_elements:
        raise ValueError("BSE official aggregate row count does not match totalElements")
    if len(codes) != len(set(codes)):
        raise ValueError("BSE official aggregate contains duplicate securities")
    if codes != sorted(codes):
        raise ValueError("BSE official aggregate is not sorted by security code")
    payload: dict[str, object] = {
        "schema_version": "bse-official-master-v1",
        "_astock_source": "BSE_OFFICIAL_LIST",
        "_astock_request": {
            "market": Market.BJSE.value,
            "purpose": "INSTRUMENT_MASTER",
        },
        "rows": normalized,
        "total": total_elements,
        "coverage_denominator": total_elements,
        "page_count": total_pages,
        "complete": True,
    }
    _validate_aggregate(payload)
    return payload


def _normalize_row(
    raw: Any,
    *,
    snapshot: SourceSnapshot | None = None,
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise BseOfficialCaptureError("BSE official row is not an object", snapshot=snapshot)
    code = str(raw.get("xxzqdm") or "").strip()
    name = str(raw.get("xxzqjc") or "").strip()
    if len(code) != 6 or not code.isdigit() or not name:
        raise BseOfficialCaptureError("BSE official row identity is malformed", snapshot=snapshot)
    if str(raw.get("xxfcbj") or "") != "2" or str(raw.get("xxzqjb") or "") != "T":
        raise BseOfficialCaptureError(
            "BSE official row crossed the listed-stock scope",
            snapshot=snapshot,
        )
    listing_date = _optional_compact_date(raw.get("xxgprq") or raw.get("fxssrq"))
    return {
        "code": code,
        "name": name,
        "listing_date": listing_date.isoformat() if listing_date is not None else None,
        "tradable": str(raw.get("xxtpbz") or "F").upper() != "T",
        "industry": str(raw.get("xxhyzl") or "").strip() or None,
        "region": str(raw.get("xxssdq") or "").strip() or None,
        "isin": str(raw.get("xxisin") or "").strip() or None,
    }


def _validate_aggregate(payload: dict[str, object]) -> None:
    if payload.get("schema_version") != "bse-official-master-v1":
        raise ValueError("Unsupported BSE official aggregate schema")
    if payload.get("_astock_source") != "BSE_OFFICIAL_LIST":
        raise ValueError("BSE official aggregate provenance mismatch")
    request = payload.get("_astock_request")
    if not isinstance(request, dict) or request.get("market") != Market.BJSE.value:
        raise ValueError("BSE official aggregate request boundary mismatch")
    rows = payload.get("rows")
    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
        raise ValueError("BSE official aggregate rows are malformed")
    total = _required_int(payload, "total")
    denominator = _required_int(payload, "coverage_denominator")
    codes = [str(item.get("code") or "") for item in rows]
    if (
        payload.get("complete") is not True
        or total <= 0
        or denominator != total
        or len(rows) != total
        or len(codes) != len(set(codes))
        or codes != sorted(codes)
        or any(len(code) != 6 or not code.isdigit() for code in codes)
    ):
        raise ValueError("BSE official aggregate completeness contract failed")


def _optional_compact_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _DATE_PATTERN.fullmatch(text):
        raise ValueError("BSE official listing date is malformed")
    return datetime.strptime(text, "%Y%m%d").date()


__all__ = ["BseOfficialCaptureError", "BseOfficialReferenceProvider"]
