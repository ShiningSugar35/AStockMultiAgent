"""Provider self-probe operations driven by registry metadata rather than provider-id switches."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from astock.core.hashing import canonical_json_bytes
from astock.schemas import (
    BarRequest,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    Frequency,
    InstrumentType,
    Market,
    ProviderDefinition,
)

if TYPE_CHECKING:
    from astock.providers.runtime import ProviderFactory


@dataclass(frozen=True, slots=True)
class SelfProbeResult:
    content: bytes
    latency_ms: int
    record_count: int
    checked_capabilities: tuple[str, ...]
    quality: bool
    content_type: str = "application/json"
    status_code: int = 200


class ProviderSelfProbeRunner:
    """Execute a registry-declared probe operation using only adapter public capabilities."""

    def __init__(self, factory: ProviderFactory) -> None:
        self.factory = factory

    def run(self, definition: ProviderDefinition) -> SelfProbeResult:
        started = time.perf_counter()
        provider = self.factory.create(definition.provider_id)
        operation = definition.probe_operation
        if operation == "market-bars":
            record_count, quality = self._market_bars(provider, definition.probe_target)
        elif operation == "reference-identity":
            record_count, quality = self._reference_identity(provider, definition.probe_target)
        elif operation == "reference-master":
            record_count, quality = self._reference_master(provider, definition.probe_target)
        elif operation == "financial-period":
            record_count, quality = self._financial_period(provider, definition.probe_target)
        elif operation == "disclosure-search":
            record_count, quality = self._disclosure_search(provider, definition.probe_target)
        elif operation == "news-lead":
            record_count, quality = self._news_lead(provider, definition.probe_target)
        elif operation == "macro-indicator":
            record_count, quality = self._macro_indicator(provider, definition.probe_target)
        else:
            raise ValueError(f"Unknown provider self-probe operation: {operation}")
        checked = tuple(_checked_capabilities(definition))
        envelope = {
            "schema_version": "provider-self-probe-v1",
            "provider_id": definition.provider_id,
            "operation": operation,
            "record_count": record_count,
            "quality": quality,
            "checked_capabilities": list(checked),
        }
        return SelfProbeResult(
            content=canonical_json_bytes(envelope),
            latency_ms=round((time.perf_counter() - started) * 1000),
            record_count=record_count,
            checked_capabilities=checked,
            quality=quality,
        )

    @staticmethod
    def _market_bars(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        fetch = getattr(provider, "fetch_bars", None)
        if not callable(fetch):
            return 0, False
        market = Market(str(target["market"]))
        symbol = str(target["symbol"])
        now = datetime.now(UTC).astimezone()
        request = BarRequest(
            symbol=symbol,
            market=market,
            instrument_type=(
                InstrumentType.INDEX if market is Market.INDEX else InstrumentType.STOCK
            ),
            frequency=Frequency.M5,
            requested_start=now - timedelta(days=7),
            requested_end=now,
        )
        batch = fetch(request)
        count = int(getattr(batch, "bar_count", 0))
        return count, count > 0

    @staticmethod
    def _reference_identity(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        market = Market(str(target["market"]))
        symbol = str(target["symbol"])
        exact = getattr(provider, "fetch_identity", None)
        if callable(exact):
            result = exact(symbol, market, live=True)
            if not isinstance(result, tuple) or len(result) != 2:
                return 0, False
            return _generic_payload_count(result[0])
        fetch = getattr(provider, "fetch", None)
        if callable(fetch):
            result = fetch(
                "instrument.master",
                {"symbol": symbol, "market": market.value},
                live=True,
            )
            if not isinstance(result, tuple) or len(result) != 2:
                return 0, False
            envelope = result[0]
            rows = getattr(envelope, "rows", None)
            fields = getattr(envelope, "fields", None)
            if not isinstance(rows, list) or not rows or not isinstance(fields, list):
                return 0, False
            return len(rows), True
        return 0, False

    @staticmethod
    def _reference_master(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        fetch = getattr(provider, "fetch_master", None)
        if not callable(fetch):
            return 0, False
        result = fetch(Market(str(target["market"])), live=True)
        if not isinstance(result, tuple) or len(result) != 2:
            return 0, False
        payload = result[0]
        if not isinstance(payload, dict):
            return 0, False
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return 0, False
        try:
            total = int(str(payload["total"]))
            denominator = int(str(payload["coverage_denominator"]))
        except (KeyError, TypeError, ValueError):
            return len(rows), False
        quality = (
            payload.get("complete") is True
            and total == denominator
            and total == len(rows)
            and bool(rows)
        )
        return len(rows), quality

    @staticmethod
    def _financial_period(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        fetch = getattr(provider, "fetch", None)
        if not callable(fetch):
            return 0, False
        payload = fetch(
            str(target["symbol"]),
            Market(str(target["market"])),
            date.fromisoformat(str(target["period_end"])),
            live=True,
        )
        tables = getattr(payload, "tables", None)
        if not isinstance(tables, dict):
            return 0, False
        count = sum(len(rows) for rows in tables.values() if isinstance(rows, list))
        return count, count > 0

    @staticmethod
    def _disclosure_search(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        search = getattr(provider, "search", None)
        if not callable(search):
            return 0, False
        today = date.today()
        request = DisclosureSearchRequest(
            symbol=str(target["symbol"]),
            exchange=DisclosureExchange(str(target["exchange"])),
            start_date=today - timedelta(days=730),
            end_date=today,
            category=DisclosureCategory(str(target.get("category", "ALL"))),
            page_size=1,
        )
        batch = search(request)
        announcements = getattr(batch, "announcements", None)
        total = getattr(batch, "total_count", None)
        if not isinstance(announcements, list) or not isinstance(total, int):
            return 0, False
        # A valid zero-result official index still proves the search endpoint/schema is healthy.
        return len(announcements), True

    @staticmethod
    def _news_lead(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        search = getattr(provider, "search", None)
        if not callable(search):
            return 0, False
        now = datetime.now(UTC)
        leads = search(
            names=[str(target.get("name") or target.get("symbol") or "")],
            symbol=str(target["symbol"]),
            start=now - timedelta(days=1),
            end=now,
            max_records=1,
        )
        return (len(leads), isinstance(leads, list)) if isinstance(leads, list) else (0, False)

    @staticmethod
    def _macro_indicator(provider: object, target: dict[str, str | int]) -> tuple[int, bool]:
        fetch = getattr(provider, "fetch_indicator", None)
        if not callable(fetch):
            return 0, False
        result = fetch(str(target["indicator_code"]), live=True)
        if not isinstance(result, tuple) or len(result) != 2:
            return 0, False
        quality, count = _recorded_macro_indicator(result[0])
        return count, quality


def validate_recorded_probe_payload(
    definition: ProviderDefinition,
    payload: object,
) -> tuple[bool, int]:
    """Validate recorded probe fixtures by declared operation, never by provider id."""

    generic = _generic_self_probe(payload, definition)
    if generic is not None:
        return generic
    operation = definition.probe_operation
    if operation == "market-bars":
        return _recorded_market_bars(payload)
    if operation == "reference-identity":
        return _recorded_reference_identity(payload)
    if operation == "reference-master":
        return _recorded_reference_master(payload)
    if operation == "financial-period":
        return _recorded_financial(payload)
    if operation == "disclosure-search":
        return _recorded_disclosure(payload)
    if operation == "news-lead":
        return _generic_self_probe(payload, definition) or (False, 0)
    if operation == "macro-indicator":
        return _recorded_macro_indicator(payload)
    return False, 0


def checked_capabilities(definition: ProviderDefinition) -> list[str]:
    return _checked_capabilities(definition)


def _checked_capabilities(definition: ProviderDefinition) -> list[str]:
    operation = definition.probe_operation
    capabilities = definition.capabilities
    if operation == "market-bars":
        selected = [item for item in capabilities if item.startswith("market.raw_")]
    elif operation == "reference-identity":
        selected = [item for item in capabilities if item == "instrument.identity"]
    elif operation == "reference-master":
        selected = [item for item in capabilities if item == "instrument.master"]
    elif operation == "financial-period":
        selected = [
            item
            for item in capabilities
            if item.startswith("financial.") and item != "financial.report_period_index"
        ]
    elif operation == "disclosure-search":
        selected = [item for item in capabilities if item == "disclosure.discover"]
    elif operation == "news-lead":
        selected = [item for item in capabilities if item == "news.discovery.lead"]
    elif operation == "macro-indicator":
        selected = [item for item in capabilities if item.startswith("macro.")]
    else:
        selected = []
    return list(dict.fromkeys(selected))


def _generic_self_probe(
    payload: object, definition: ProviderDefinition
) -> tuple[bool, int] | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != "provider-self-probe-v1":
        return None
    if payload.get("provider_id") != definition.provider_id:
        return False, 0
    if payload.get("operation") != definition.probe_operation:
        return False, 0
    quality = payload.get("quality") is True
    count = payload.get("record_count")
    if not isinstance(count, int) or count < 0:
        return False, 0
    return quality, count


def _recorded_market_bars(payload: object) -> tuple[bool, int]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        rows = payload["data"].get("klines")
        if isinstance(rows, list):
            quality = bool(rows) and all(
                isinstance(row, str) and len(row.split(",")) >= 7 for row in rows
            )
            return quality, len(rows)
    if isinstance(payload, list):
        required = {"day", "open", "high", "low", "close", "volume"}
        quality = bool(payload) and all(
            isinstance(row, dict) and required <= row.keys() for row in payload
        )
        return quality, len(payload)
    return False, 0


def _recorded_reference_identity(payload: object) -> tuple[bool, int]:
    if not isinstance(payload, dict):
        return False, 0
    fields = payload.get("fields")
    rows = payload.get("rows")
    if isinstance(fields, list) and isinstance(rows, list):
        quality = bool(rows) and all(
            isinstance(row, list) and len(row) == len(fields) for row in rows
        )
        return quality, len(rows)
    data = payload.get("data")
    if isinstance(data, dict):
        diff = data.get("diff")
        if isinstance(diff, dict):
            diff = list(diff.values())
        if isinstance(diff, list):
            quality = bool(diff) and all(
                isinstance(row, dict) and {"f12", "f13", "f14"} <= row.keys() for row in diff
            )
            return quality, len(diff)
        if any(key in data for key in ("f57", "f58")):
            return True, 1
    return False, 0


def _recorded_reference_master(payload: object) -> tuple[bool, int]:
    if not isinstance(payload, dict):
        return False, 0
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return False, 0
    try:
        total = int(str(payload["total"]))
        denominator = int(str(payload["coverage_denominator"]))
    except (KeyError, TypeError, ValueError):
        return False, len(rows)
    quality = (
        payload.get("complete") is True
        and total == denominator
        and total == len(rows)
        and bool(rows)
    )
    return quality, len(rows)


def _recorded_financial(payload: object) -> tuple[bool, int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("responses"), dict):
        return False, 0
    responses = payload["responses"]
    count = 0
    valid_tables = 0
    for response in responses.values():
        if not isinstance(response, dict):
            continue
        result = response.get("result")
        if not isinstance(result, dict):
            continue
        data: object = result.get("data")
        if isinstance(data, dict):
            data = data.get("data") or data.get("report_list")
        if isinstance(data, dict):
            rows = list(data.values())
        else:
            rows = data
        if isinstance(rows, list) and rows:
            count += len(rows)
            valid_tables += 1
    return valid_tables >= 3, count


def _recorded_disclosure(payload: object) -> tuple[bool, int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("announcements"), list):
        return False, 0
    rows = payload["announcements"]
    return True, len(rows)


def _recorded_macro_indicator(payload: object) -> tuple[bool, int]:
    if not isinstance(payload, dict):
        return False, 0
    schema_version = payload.get("schema_version")
    request = payload.get("_astock_request")
    data_points = payload.get("data_points")
    revision_version = payload.get("revision_version")
    quality = bool(
        isinstance(schema_version, str)
        and schema_version.startswith("macro-release-v")
        and isinstance(request, dict)
        and request.get("purpose") == "MACRO_ECONOMIC_RELEASE"
        and request.get("indicator_code") == payload.get("indicator_code")
        and isinstance(payload.get("publication_date"), str)
        and isinstance(payload.get("available_to_system_at"), str)
        and isinstance(revision_version, int)
        and not isinstance(revision_version, bool)
        and revision_version > 0
        and isinstance(data_points, list)
        and data_points
        and all(
            isinstance(point, dict)
            and isinstance(point.get("period"), str)
            and bool(point.get("period"))
            and isinstance(point.get("unit"), str)
            and bool(point.get("unit"))
            and ("value" in point or "price" in point)
            for point in data_points
        )
    )
    return quality, len(data_points) if isinstance(data_points, list) else 0


def _generic_payload_count(payload: object) -> tuple[int, bool]:
    if not isinstance(payload, dict):
        return 0, False
    data = payload.get("data")
    if isinstance(data, dict):
        if data.get("f57") or data.get("f58"):
            return 1, True
        diff = data.get("diff")
        if isinstance(diff, (list, dict)):
            return len(diff), bool(diff)
    return 1, bool(payload)


__all__ = [
    "ProviderSelfProbeRunner",
    "SelfProbeResult",
    "checked_capabilities",
    "validate_recorded_probe_payload",
]
