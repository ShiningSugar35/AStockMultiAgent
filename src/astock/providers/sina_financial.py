"""Sina secondary structured financial-statement backup provider."""

from __future__ import annotations

from datetime import date

from astock.providers.financial_base import (
    FinancialProviderBase,
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.schemas import Market


class SinaFinancialProvider(FinancialProviderBase):
    provider_id = "sina-financial"
    fixture_name = "sina_000001_2025.json"
    endpoint = (
        "https://quotes.sina.cn/cn/api/openapi.php/"
        "CompanyFinanceService.getFinanceReport2022"
    )
    _SOURCE_NAMES = {
        "BALANCE_SHEET": "fzb",
        "INCOME_STATEMENT": "lrb",
        "CASH_FLOW_STATEMENT": "llb",
    }

    def fetch(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        *,
        live: bool = False,
    ) -> FinancialProviderPayload:
        if not live:
            payload, snapshot = self._recorded_json(
                company_id, market, period_end.isoformat()
            )
            responses = _recorded_responses(
                payload, "sina-financial-raw-fixture-v1"
            )
            tables = {
                statement: _normalize_sina(
                    _sina_rows(responses[statement]), company_id, period_end
                )
                for statement in self._SOURCE_NAMES
            }
            return FinancialProviderPayload(
                self.provider_id,
                company_id,
                market,
                period_end.isoformat(),
                tables,
                {statement: snapshot for statement in tables},
                {
                    statement: snapshot.source_id.rsplit(":", maxsplit=1)[-1]
                    for statement in tables
                },
            )
        tables: dict[str, list[dict[str, object]]] = {}
        snapshots = {}
        captured = []
        paper_code = f"{_market_prefix(market)}{company_id}"
        for statement, source in self._SOURCE_NAMES.items():
            try:
                payload, snapshot = self._capture_json(
                    self.endpoint,
                    params={
                        "paperCode": paper_code,
                        "source": source,
                        "type": 0,
                        "page": 1,
                        "num": 100,
                    },
                    request_context={
                        "company_id": company_id,
                        "market": market.value,
                        "period_end": period_end.isoformat(),
                        "statement": statement,
                    },
                )
            except FinancialRawCaptureError as exc:
                raise FinancialRawCaptureError(
                    exc.failure_code, [*captured, *exc.snapshots]
                ) from exc
            captured.append(snapshot)
            try:
                tables[statement] = _normalize_sina(
                    _sina_rows(payload), company_id, period_end
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise FinancialRawCaptureError(
                    "FINANCIAL_RAW_NORMALIZATION_FAILED", list(captured)
                ) from exc
            snapshots[statement] = snapshot
        return FinancialProviderPayload(
            self.provider_id,
            company_id,
            market,
            period_end.isoformat(),
            tables,
            snapshots,
            {
                statement: snapshot.source_id.rsplit(":", maxsplit=1)[-1]
                for statement, snapshot in snapshots.items()
            },
        )


def _sina_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Sina financial result is malformed")
    data = result.get("data")
    if isinstance(data, dict):
        data = data.get("data") or data.get("report_list")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("Sina financial rows are malformed")
    return [dict(row) for row in data if isinstance(row, dict)]


def _normalize_sina(
    rows: list[dict[str, object]], company_id: str, period_end: date
) -> list[dict[str, object]]:
    normalized = []
    required = {"symbol", "report_date", "statement_scope", "currency"}
    for row in rows:
        if not required <= row.keys():
            raise ValueError("Sina financial row lacks native identity fields")
        if (
            str(row["symbol"]) != company_id
            or str(row["report_date"])[:10] != period_end.isoformat()
        ):
            continue
        normalized.append(
            {
                **row,
                "company_id": str(row["symbol"]),
                "period_end": str(row["report_date"])[:10],
                "scope": str(row["statement_scope"]),
                "currency": str(row["currency"]),
            }
        )
    return normalized


def _recorded_responses(
    payload: dict[str, object], expected_schema: str
) -> dict[str, dict[str, object]]:
    if payload.get("schema_version") != expected_schema:
        raise ValueError("Sina recorded financial schema is invalid")
    responses = payload.get("responses")
    if not isinstance(responses, dict) or set(responses) != set(
        SinaFinancialProvider._SOURCE_NAMES
    ):
        raise ValueError("Sina recorded responses are incomplete")
    if any(not isinstance(value, dict) for value in responses.values()):
        raise ValueError("Sina recorded response must be an object")
    return {
        str(key): value for key, value in responses.items() if isinstance(value, dict)
    }


def _market_prefix(market: Market) -> str:
    try:
        return {
            Market.XSHG: "sh",
            Market.XSHE: "sz",
            Market.BJSE: "bj",
        }[market]
    except KeyError as exc:
        raise ValueError("Financial statements require an explicit stock exchange") from exc


__all__ = ["SinaFinancialProvider"]
