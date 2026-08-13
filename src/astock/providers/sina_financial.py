"""Sina secondary structured financial-statement backup provider."""

from __future__ import annotations

from datetime import date

from astock.providers.financial_base import (
    FinancialProviderBase,
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.schemas import Market, SourceSnapshot

_SINA_LIVE_FIELD_ALIASES = {
    "BALANCE_SHEET": {
        "TOTASSET": "total_assets",
        "TOTLIAB": "total_liabilities",
        "RIGHAGGR": "total_equity",
        "ACCORECE": "accounts_receivable",
        "INVE": "inventory",
        "PREP": "prepayments",
        "OTHERRECETOT": "other_receivables",
        "OTHERRECE": "other_receivables",
        "TOTSHARE": "shares_outstanding",
    },
    "INCOME_STATEMENT": {
        "NETPROFIT": "net_profit_income",
        "BIZINCO": "revenue",
        "BIZCOST": "operating_cost",
    },
    "CASH_FLOW_STATEMENT": {
        "INICASHBALA": "cash_beginning",
        "FINALCASHBALA": "cash_ending",
        "MANANETR": "net_cash_operating",
        "INVNETCASHFLOW": "net_cash_investing",
        "FINNETCFLOW": "net_cash_financing",
        "CHGEXCHGCHGS": "exchange_effect",
        "NETPROFIT": "net_profit_cash_flow",
    },
}

_SINA_SHARE_FIELDS = {"TOTSHARE"}


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

    def discover_report_periods(
        self,
        company_id: str,
        market: Market,
        *,
        live: bool = False,
    ) -> tuple[list[date], SourceSnapshot]:
        """Return source-declared report dates instead of guessing a disclosure calendar."""

        if not live:
            payload, snapshot = self._recorded_json(company_id, market, "period-discovery")
            responses = _recorded_responses(payload, "sina-financial-raw-fixture-v1")
            dates = sorted(
                {
                    date.fromisoformat(str(row["report_date"])[:10])
                    for row in _sina_rows(responses["BALANCE_SHEET"], "BALANCE_SHEET")
                    if row.get("report_date")
                },
                reverse=True,
            )
            return dates, snapshot
        paper_code = f"{_market_prefix(market)}{company_id}"
        payload, snapshot = self._capture_json(
            self.endpoint,
            params={
                "paperCode": paper_code,
                "source": "fzb",
                "type": 0,
                "page": 1,
                "num": 100,
            },
            request_context={
                "company_id": company_id,
                "market": market.value,
                "purpose": "REPORT_PERIOD_DISCOVERY",
            },
        )
        return _sina_report_dates(payload), snapshot

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
                    _sina_rows(responses[statement], statement), company_id, period_end
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
                    _sina_rows(payload, statement), company_id, period_end
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


def _sina_rows(
    payload: dict[str, object], statement: str
) -> list[dict[str, object]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Sina financial result is malformed")
    status = result.get("status")
    if isinstance(status, dict) and status.get("code") not in {0, "0", None}:
        raise ValueError("Sina financial query failed")
    data = result.get("data")
    if isinstance(data, dict):
        report_list = data.get("report_list")
        if isinstance(report_list, dict):
            rows: list[dict[str, object]] = []
            for report_date, report in report_list.items():
                if not isinstance(report, dict):
                    raise ValueError("Sina financial report entry is malformed")
                scope = _sina_scope(report.get("rType"))
                currency = report.get("rCurrency")
                items = report.get("data")
                if scope is None or currency != "CNY" or not isinstance(items, list):
                    continue
                row: dict[str, object] = {
                    "report_date": _normalize_sina_report_date(str(report_date)),
                    "statement_scope": scope,
                    "currency": "CNY",
                }
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    source_field = str(item.get("item_field") or "")
                    alias = _SINA_LIVE_FIELD_ALIASES.get(statement, {}).get(source_field)
                    if alias is None:
                        continue
                    value = item.get("item_value")
                    if value in {None, ""}:
                        continue
                    row[alias] = _sina_live_value(source_field, value)
                rows.append(row)
            return rows
        data = data.get("data") or report_list
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("Sina financial rows are malformed")
    return [dict(row) for row in data if isinstance(row, dict)]


def _sina_report_dates(payload: dict[str, object]) -> list[date]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Sina financial result is malformed")
    status = result.get("status")
    if isinstance(status, dict) and status.get("code") not in {0, "0", None}:
        raise ValueError("Sina financial query failed")
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("report_date"), list):
        raise ValueError("Sina financial report-date index is malformed")
    dates: list[date] = []
    for item in data["report_date"]:
        if not isinstance(item, dict):
            continue
        raw = item.get("date_value")
        if raw is None:
            continue
        dates.append(date.fromisoformat(_normalize_sina_report_date(str(raw))))
    return sorted(set(dates), reverse=True)


def _sina_scope(value: object) -> str | None:
    text = str(value or "").strip()
    if text.startswith("合并"):
        return "CONSOLIDATED"
    if text.startswith("母公司") or text.startswith("母企"):
        return "PARENT_COMPANY"
    return None


def _normalize_sina_report_date(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 8:
        raise ValueError("Sina report date is malformed")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _sina_live_value(source_field: str, value: object) -> object:
    if source_field in _SINA_SHARE_FIELDS:
        return value
    try:
        from decimal import Decimal

        return str(Decimal(str(value)) / Decimal("10000"))
    except Exception as exc:
        raise ValueError("Sina financial value is malformed") from exc


def _normalize_sina(
    rows: list[dict[str, object]], company_id: str, period_end: date
) -> list[dict[str, object]]:
    normalized = []
    required = {"report_date", "statement_scope", "currency"}
    for row in rows:
        if not required <= row.keys():
            raise ValueError("Sina financial row lacks native identity fields")
        if "symbol" in row and str(row["symbol"]) != company_id:
            continue
        if str(row["report_date"])[:10] != period_end.isoformat():
            continue
        normalized.append(
            {
                **row,
                "company_id": company_id,
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
