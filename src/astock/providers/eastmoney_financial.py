"""EastMoney secondary structured financial-statement provider."""

from __future__ import annotations

from datetime import date

from astock.providers.dialects import ProviderDialect
from astock.providers.financial_base import (
    FinancialProviderBase,
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.schemas import Market


class EastMoneyFinancialProvider(FinancialProviderBase):
    provider_id = "eastmoney-financial"
    fixture_name = "eastmoney_000001_2025.json"

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
                payload,
                "eastmoney-financial-raw-fixture-v1",
                set(self.dialect.statement_sources),
            )
            tables = {
                statement: _normalize_eastmoney(
                    _eastmoney_rows(responses[statement], self.dialect),
                    company_id,
                    period_end,
                    self.dialect,
                )
                for statement in self.dialect.statement_sources
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
        for statement, report_name in self.dialect.statement_sources.items():
            try:
                payload, snapshot = self._capture_json(
                    self.dialect.endpoint,
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
            if self.dialect.response_shape != "eastmoney-data-list-v1":
                raise FinancialRawCaptureError(
                    "FINANCIAL_DIALECT_UNRECOGNIZED", list(captured)
                )
            try:
                self.dialect.value_at(payload, "rows")
            except ValueError as exc:
                raise FinancialRawCaptureError(
                    "FINANCIAL_DIALECT_UNRECOGNIZED", list(captured)
                ) from exc
            try:
                tables[statement] = _normalize_eastmoney(
                    _eastmoney_rows(payload, self.dialect), company_id, period_end, self.dialect
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


def _eastmoney_rows(
    payload: dict[str, object], dialect: ProviderDialect
) -> list[dict[str, object]]:
    try:
        success = dialect.value_at(payload, "success")
        rows = dialect.value_at(payload, "rows")
    except ValueError as exc:
        raise ValueError("EastMoney financial dialect is unrecognized") from exc
    if success is False:
        raise ValueError("EastMoney financial query failed")
    if not isinstance(rows, list):
        raise ValueError("EastMoney financial result is malformed")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("EastMoney financial row is malformed")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _normalize_eastmoney(
    rows: list[dict[str, object]],
    company_id: str,
    period_end: date,
    dialect: ProviderDialect,
) -> list[dict[str, object]]:
    normalized = []
    if len(dialect.identity_fields) < 2 or not dialect.scope_field or not dialect.currency_field:
        raise ValueError("EastMoney dialect lacks identity/scope/currency fields")
    company_field, period_field = dialect.identity_fields[:2]
    required = {company_field, period_field, dialect.scope_field, dialect.currency_field}
    for row in rows:
        if not required <= row.keys():
            raise ValueError("EastMoney financial row lacks dialect identity fields")
        currency = str(row[dialect.currency_field])
        if dialect.allowed_currencies and currency not in dialect.allowed_currencies:
            continue
        if (
            str(row[company_field]) != company_id
            or str(row[period_field])[:10] != period_end.isoformat()
        ):
            continue
        normalized.append(
            {
                **row,
                "company_id": str(row[company_field]),
                "period_end": str(row[period_field])[:10],
                "scope": str(row[dialect.scope_field]),
                "currency": currency,
            }
        )
    return normalized


def _recorded_responses(
    payload: dict[str, object], expected_schema: str, expected_statements: set[str]
) -> dict[str, dict[str, object]]:
    if payload.get("schema_version") != expected_schema:
        raise ValueError("EastMoney recorded financial schema is invalid")
    responses = payload.get("responses")
    if not isinstance(responses, dict) or set(responses) != expected_statements:
        raise ValueError("EastMoney recorded responses are incomplete")
    if any(not isinstance(value, dict) for value in responses.values()):
        raise ValueError("EastMoney recorded response must be an object")
    return {
        str(key): value for key, value in responses.items() if isinstance(value, dict)
    }


__all__ = ["EastMoneyFinancialProvider"]
