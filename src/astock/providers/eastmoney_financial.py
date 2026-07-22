"""EastMoney secondary structured financial-statement provider."""

from __future__ import annotations

from datetime import date

from astock.providers.financial_base import (
    FinancialProviderBase,
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.schemas import Market


class EastMoneyFinancialProvider(FinancialProviderBase):
    provider_id = "eastmoney-financial"
    fixture_name = "eastmoney_000001_2025.json"
    endpoint = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    _REPORT_NAMES = {
        "BALANCE_SHEET": "RPT_DMSK_FN_BALANCE",
        "INCOME_STATEMENT": "RPT_DMSK_FN_INCOME",
        "CASH_FLOW_STATEMENT": "RPT_DMSK_FN_CASHFLOW",
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
                payload, "eastmoney-financial-raw-fixture-v1"
            )
            tables = {
                statement: _normalize_eastmoney(
                    _eastmoney_rows(responses[statement]),
                    company_id,
                    period_end,
                )
                for statement in self._REPORT_NAMES
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
        for statement, report_name in self._REPORT_NAMES.items():
            try:
                payload, snapshot = self._capture_json(
                    self.endpoint,
                    params={
                        "reportName": report_name,
                        "columns": "ALL",
                        "filter": f'(SECURITY_CODE="{company_id}")',
                        "pageNumber": 1,
                        "pageSize": 200,
                        "sortColumns": "REPORT_DATE",
                        "sortTypes": -1,
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
                tables[statement] = _normalize_eastmoney(
                    _eastmoney_rows(payload), company_id, period_end
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


def _eastmoney_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    if payload.get("success") is False:
        raise ValueError("EastMoney financial query failed")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("EastMoney financial result is malformed")
    rows = result["data"]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("EastMoney financial row is malformed")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _normalize_eastmoney(
    rows: list[dict[str, object]], company_id: str, period_end: date
) -> list[dict[str, object]]:
    normalized = []
    required = {"SECURITY_CODE", "REPORT_DATE", "STATEMENT_SCOPE", "CURRENCY"}
    for row in rows:
        if not required <= row.keys():
            raise ValueError("EastMoney financial row lacks native identity fields")
        if (
            str(row["SECURITY_CODE"]) != company_id
            or str(row["REPORT_DATE"])[:10] != period_end.isoformat()
        ):
            continue
        normalized.append(
            {
                **row,
                "company_id": str(row["SECURITY_CODE"]),
                "period_end": str(row["REPORT_DATE"])[:10],
                "scope": str(row["STATEMENT_SCOPE"]),
                "currency": str(row["CURRENCY"]),
            }
        )
    return normalized


def _recorded_responses(
    payload: dict[str, object], expected_schema: str
) -> dict[str, dict[str, object]]:
    if payload.get("schema_version") != expected_schema:
        raise ValueError("EastMoney recorded financial schema is invalid")
    responses = payload.get("responses")
    if not isinstance(responses, dict) or set(responses) != set(
        EastMoneyFinancialProvider._REPORT_NAMES
    ):
        raise ValueError("EastMoney recorded responses are incomplete")
    if any(not isinstance(value, dict) for value in responses.values()):
        raise ValueError("EastMoney recorded response must be an object")
    return {
        str(key): value for key, value in responses.items() if isinstance(value, dict)
    }


__all__ = ["EastMoneyFinancialProvider"]
