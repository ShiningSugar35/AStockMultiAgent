"""Tushare three-statement hints using the shared query, quota and raw contracts."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from astock.core.errors import ProviderError
from astock.core.hashing import content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.financial_base import (
    FinancialProviderBase,
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.providers.tushare_structured import (
    Endpoint,
    TushareQueryRequest,
    TushareStructuredProvider,
    _decode,
    _rows,
)
from astock.schemas import Market, SourceSnapshot

STATEMENT_ENDPOINTS: dict[str, Endpoint] = {
    "BALANCE_SHEET": "balancesheet",
    "INCOME_STATEMENT": "income",
    "CASH_FLOW_STATEMENT": "cashflow",
}
_METADATA = frozenset({
    "ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
    "comp_type", "end_type", "update_flag",
})


class TushareFinancialProvider(FinancialProviderBase):
    """Normalize latest consolidated cumulative hints; official certification is separate."""

    provider_id = "tushare-financial"
    # Offline operation consumes admitted local evidence, never an implicit test fixture.
    recorded_supported = False

    def __init__(
        self, objects: ObjectStore, state: StateStore, *,
        project_root: Path | None = None, client: httpx.Client | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        self.objects, self.state = objects, state
        self.structured = TushareStructuredProvider(
            objects, state, project_root=project_root, client=client,
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        self.structured.close()

    def fetch(
        self, company_id: str, market: Market, period_end: date, *, live: bool = False,
    ) -> FinancialProviderPayload:
        if not live:
            raise FinancialRawCaptureError("FINANCIAL_RECORDED_UNAVAILABLE", [])
        suffix = {Market.XSHG: "SH", Market.XSHE: "SZ", Market.BJSE: "BJ"}.get(market)
        if suffix is None:
            raise ValueError("Financial statements require a stock exchange")
        tables: dict[str, list[dict[str, object]]] = {}
        snapshots: dict[str, SourceSnapshot] = {}
        captured: list[SourceSnapshot] = []
        for statement, endpoint in STATEMENT_ENDPOINTS.items():
            request = TushareQueryRequest.model_validate({
                "endpoint": endpoint,
                "params": {
                    "ts_code": f"{company_id}.{suffix}",
                    "period": period_end.strftime("%Y%m%d"),
                    "report_type": "1",
                },
                "max_rows": 200,
            })
            try:
                result = self.structured.query(request)
                upstream = self.state.get_snapshot(result["source_snapshot_id"])
                if upstream is None or not self.objects.verify(upstream.object_sha256):
                    raise ValueError("Tushare financial raw object is unavailable")
                if result["request"] != request.identity():
                    raise ValueError("Tushare financial query identity mismatch")
                raw = self.objects.get_bytes(upstream.object_sha256)
                snapshot = self._persist(
                    raw, source_url=upstream.source_url or "https://api.tushare.pro",
                    content_type=upstream.mime, observed_at=upstream.available_to_system_at,
                    request={
                        "query": request.identity(),
                        "query_fingerprint": result["request_fingerprint"],
                        "upstream_snapshot_id": upstream.snapshot_id,
                        "capture_artifact_id": result["artifact_id"],
                    },
                )
                captured.append(snapshot)
                tables[statement] = normalize_financial_rows(
                    result["rows"], company_id, market, period_end,
                    capture_artifact_id=result["artifact_id"],
                    raw_snapshot_id=upstream.snapshot_id,
                )
                snapshots[statement] = snapshot
            except ProviderError as exc:
                exc.details["financial_snapshot_ids"] = [
                    item.snapshot_id for item in captured
                ]
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise FinancialRawCaptureError(
                    "TUSHARE_FINANCIAL_MAPPING_FAILED", captured,
                ) from exc
        return FinancialProviderPayload(
            self.provider_id, company_id, market, period_end.isoformat(), tables, snapshots,
            {
                statement: snapshot.source_id.rsplit(":", maxsplit=1)[-1]
                for statement, snapshot in snapshots.items()
            },
        )


def normalize_financial_rows(
    rows: list[dict[str, Any]], company_id: str, market: Market, period_end: date, *,
    capture_artifact_id: str, raw_snapshot_id: str,
) -> list[dict[str, object]]:
    """Select one CURRENT revision without merging parent/single-quarter/adjusted data.

    Every returned version remains in the original snapshot and capture artifact. A date
    has no invented midnight timestamp: observation availability stays at actual capture.
    """
    suffix = {Market.XSHG: "SH", Market.XSHE: "SZ", Market.BJSE: "BJ"}.get(market)
    code = f"{company_id}.{suffix}"
    versions: dict[str, tuple[dict[str, Any], date | None, date | None]] = {}
    for row in rows:
        if (
            row.get("ts_code") != code
            or row.get("end_date") != period_end.strftime("%Y%m%d")
            or str(row.get("report_type")) != "1"
        ):
            raise ValueError("Tushare consolidated cumulative identity mismatch")
        announced = _announcement(row.get("ann_date"), period_end)
        actual = _announcement(row.get("f_ann_date"), period_end)
        if row.get("update_flag") is not None and str(row["update_flag"]) not in {"0", "1"}:
            raise ValueError("Unknown Tushare financial update flag")
        if str(row.get("comp_type")) not in {"1", "2", "3", "4", "7"}:
            raise ValueError("Unknown Tushare financial company type")
        versions[content_hash(row)] = (row, announced, actual)
    if not versions:
        return []

    # Official cashflow doc44 defines update_flag=1 as latest. Publication dates
    # cannot overrule that native revision marker or supply an invented date.
    candidates = list(versions.items())
    flagged = [
        (digest, item) for digest, item in candidates
        if str(item[0].get("update_flag")) == "1"
    ]
    if flagged:
        candidates = flagged
    if len(candidates) > 1 and any(
        actual is None and announced is None for _, (_, announced, actual) in candidates
    ):
        raise ValueError("Tushare competing financial revision date is unknown")

    def order(item: tuple[dict[str, Any], date | None, date | None]) -> date:
        _, announced, actual = item
        return actual or announced or date.min

    latest = max(order(item) for _, item in candidates)
    selected = [(digest, item) for digest, item in candidates if order(item) == latest]
    if len(selected) != 1:
        raise ValueError("Tushare latest financial revision is ambiguous")
    digest, (row, announced, actual) = selected[0]
    normalized: dict[str, object] = {
        key: _number(value) for key, value in row.items() if key not in _METADATA
    }
    normalized.update({
        "company_id": company_id, "period_end": period_end.isoformat(),
        "scope": "CONSOLIDATED", "currency": "CNY",
        "provider_report_version": {
            "announced_on": announced.isoformat() if announced else None,
            "actual_announced_on": actual.isoformat() if actual else None,
            "report_type": "1", "company_type": str(row["comp_type"]),
            "update_flag": str(row["update_flag"]) if row.get("update_flag") is not None else None,
            "source_row_hash": digest,
            "capture_artifact_id": capture_artifact_id,
            "raw_snapshot_id": raw_snapshot_id,
            "returned_version_count": len(versions),
            "selection_basis": "LATEST_CONSOLIDATED_CUMULATIVE",
            "publication_precision": "DATE_ONLY" if (actual or announced) else "UNKNOWN",
        },
    })
    return [normalized]


def _announcement(value: Any, period_end: date) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) != 8 or not value.isdecimal():
        raise ValueError("Tushare announcement must be YYYYMMDD")
    parsed = datetime.strptime(value, "%Y%m%d").date()
    if parsed < period_end:
        raise ValueError("Tushare announcement precedes its report period")
    return parsed


def _number(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Tushare financial value must be numeric or null")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Tushare financial value must be numeric or null") from exc
    if not result.is_finite():
        raise ValueError("Tushare financial value must be finite")
    return str(result)


def validate_recorded_financial(
    payload: dict[str, Any], target: dict[str, str | int],
) -> tuple[bool, int]:
    """Replay native bytes through the same boundary, without credentials or networking."""
    captures = payload.get("captures")
    if not isinstance(captures, dict) or set(captures) != set(STATEMENT_ENDPOINTS):
        return False, 0
    count = 0
    try:
        company_id, market = str(target["symbol"]), Market(str(target["market"]))
        period_end = date.fromisoformat(str(target["period_end"]))
        for statement, endpoint in STATEMENT_ENDPOINTS.items():
            capture = captures[statement]
            body = capture["raw_response"].encode("utf-8")
            if capture["source_snapshot_id"] != f"tushare-structured:{sha256_bytes(body)}":
                return False, 0
            request = TushareQueryRequest(
                endpoint=endpoint,
                params={
                    "ts_code": (
                        f"{company_id}.{dict(XSHG='SH', XSHE='SZ', BJSE='BJ')[market.value]}"
                    ),
                    "period": period_end.strftime("%Y%m%d"), "report_type": "1",
                },
                max_rows=200,
            )
            rows = _rows(_decode(body, 200, None), request, [])
            normalized = normalize_financial_rows(
                rows, company_id, market, period_end,
                capture_artifact_id=capture["capture_artifact_id"],
                raw_snapshot_id=capture["source_snapshot_id"],
            )
            if not normalized:
                return False, 0
            count += len(rows)
    except (KeyError, TypeError, ValueError, AttributeError, ProviderError):
        return False, 0
    return True, count
