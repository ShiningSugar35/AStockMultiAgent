"""Fail-closed exact native-PDF financial-number certification."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentPageRepository, DocumentRepository
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.financial_sources.config import FinancialFieldMapping
from astock.financial_sources.official import OfficialFinancialReport
from astock.schemas import (
    EvidenceGrade,
    FactStatus,
    FinancialFact,
    FinancialFieldCode,
    FinancialPeriodType,
    FinancialSourceObservation,
    FinancialStatementScope,
    FinancialStatementType,
    FinancialUnit,
    PageExtractionMethod,
)

_HEADINGS = {
    FinancialStatementType.BALANCE_SHEET: "合并资产负债表",
    FinancialStatementType.INCOME_STATEMENT: "合并利润表",
    FinancialStatementType.CASH_FLOW_STATEMENT: "合并现金流量表",
}
_UNITS = {
    "元": FinancialUnit.CNY,
    "千元": FinancialUnit.THOUSAND_CNY,
    "万元": FinancialUnit.TEN_THOUSAND_CNY,
    "百万元": FinancialUnit.MILLION_CNY,
    "亿元": FinancialUnit.HUNDRED_MILLION_CNY,
}
_TABLE_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(?P<title>(?:合并|母公司|公司)?"
    r"(?:资产负债表|利润表|现金流量表))[ \t]*$"
)
_PERIOD_HEADER_RE = re.compile(
    r"(?m)^[ \t]*(?:\d{4}年\d{1,2}月\d{1,2}日|\d{4}年度|"
    r"\d{4}年1[-－—至]\d{1,2}月)[ \t]*$"
)
_CURRENCY_HEADER_RE = re.compile(r"(?m)^[ \t]*币种：人民币[ \t]*$")
_UNIT_HEADER_RE = re.compile(r"(?m)^[ \t]*单位：(百万元|万元|千元|亿元|元)[ \t]*$")


class FinancialPdfCertifier:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.objects = objects
        self.pages = DocumentPageRepository(state)
        self.evidence = ClaimEvidenceService(
            objects,
            state,
            self.pages,
            DocumentRepository(state),
            EvidenceRepository(state),
        )

    def certify(
        self,
        report: OfficialFinancialReport,
        observations: list[FinancialSourceObservation],
        mappings: list[FinancialFieldMapping],
    ) -> tuple[list[FinancialFact], list[str]]:
        facts: list[FinancialFact] = []
        reasons: list[str] = []
        observed: dict[
            tuple[FinancialStatementType, FinancialFieldCode], FinancialSourceObservation
        ] = {}
        for item in observations:
            if item.statement_scope is not FinancialStatementScope.CONSOLIDATED:
                raise ValueError("Financial PDF certification requires CONSOLIDATED hints")
            if item.reported_value is not None:
                observed.setdefault((item.statement_type, item.field_code), item)
        for mapping in mappings:
            key = (mapping.statement_type, mapping.field_code)
            hint = observed.get(key)
            if hint is None:
                reasons.append(f"SECONDARY_FIELD_MISSING:{mapping.field_code.value}")
                continue
            matches = self._exact_matches(report, mapping, hint.period_end, hint.period_type)
            if len(matches) != 1:
                code = "OFFICIAL_VALUE_NOT_FOUND" if not matches else "OFFICIAL_VALUE_AMBIGUOUS"
                reasons.append(f"{code}:{mapping.field_code.value}")
                continue
            page_id, char_start, char_end, value, unit = matches[0]
            if hint.reported_value != value or hint.unit is not unit:
                reasons.append(f"SECONDARY_VALUE_CONFLICT:{mapping.field_code.value}")
            evidence = self.evidence.create_page_evidence(
                page_id=page_id,
                char_start=char_start,
                char_end=char_end,
                evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
                fact_status=FactStatus.DIRECT,
                entity_ids=[hint.company_id, f"company:{hint.company_id}"],
                valid_from=report.document.published_at,
            )
            identity = {
                "company_id": hint.company_id,
                "period_start": hint.period_start,
                "period_end": hint.period_end,
                "period_type": hint.period_type,
                "duration_semantics": hint.duration_semantics,
                "statement_type": mapping.statement_type,
                "field_code": mapping.field_code,
                "reported_value": value,
                "unit": unit,
                "source_snapshot_id": report.snapshot.snapshot_id,
                "pit_id": report.pit.pit_id,
                "evidence_id": evidence.evidence_id,
            }
            facts.append(
                FinancialFact(
                    created_at=report.snapshot.available_to_system_at,
                    fact_id=f"financial-fact:{content_hash(identity)}",
                    company_id=hint.company_id,
                    period_start=hint.period_start,
                    period_end=hint.period_end,
                    period_type=hint.period_type,
                    duration_semantics=hint.duration_semantics,
                    statement_type=mapping.statement_type,
                    field_code=mapping.field_code,
                    reported_value=value,
                    unit=unit,
                    source_snapshot_id=report.snapshot.snapshot_id,
                    pit_id=report.pit.pit_id,
                    evidence_ids=[evidence.evidence_id],
                )
            )
        return facts, list(dict.fromkeys(reasons))

    def _exact_matches(
        self,
        report: OfficialFinancialReport,
        mapping: FinancialFieldMapping,
        period_end: date,
        period_type: FinancialPeriodType,
    ) -> list[tuple[str, int, int, Decimal, FinancialUnit]]:
        matches = []
        for page in report.pages:
            if page.extraction_method is not PageExtractionMethod.NATIVE_TEXT or page.ocr_applied:
                continue
            text = self.objects.get_bytes(page.text_object_sha256).decode("utf-8")
            heading = _HEADINGS[mapping.statement_type]
            region = _statement_region(text, heading)
            if region is None:
                continue
            region_text, region_start, heading_end = region
            period_column = _period_column(
                period_end, period_type, mapping.statement_type
            )
            period_headers = list(_PERIOD_HEADER_RE.finditer(region_text))
            target_periods = [
                match
                for match in period_headers
                if match.group(0).strip() == period_column
            ]
            if len(period_headers) != 1 or len(target_periods) != 1:
                continue
            currency_headers = list(_CURRENCY_HEADER_RE.finditer(region_text))
            unit_headers = list(_UNIT_HEADER_RE.finditer(region_text))
            if len(currency_headers) != 1 or len(unit_headers) != 1:
                continue
            unit = _UNITS[unit_headers[0].group(1)]
            label = (
                f"{mapping.official_label}（股）"
                if mapping.unit is FinancialUnit.SHARES
                else mapping.official_label
            )
            line_pattern = re.compile(
                rf"(?m)^{re.escape(label)}[ \t]+([^\r\n]+)$"
            )
            line_matches = list(line_pattern.finditer(region_text))
            if len(line_matches) != 1:
                continue
            period_header = target_periods[0]
            currency_header = currency_headers[0]
            unit_header = unit_headers[0]
            value_line = line_matches[0]
            if not (
                heading_end
                <= period_header.start()
                < currency_header.start()
                < unit_header.start()
                < value_line.start()
            ):
                continue
            raw_value = value_line.group(1).strip()
            parsed = _parse_decimal(raw_value)
            if parsed is None:
                continue
            field_unit = FinancialUnit.SHARES if mapping.unit is FinancialUnit.SHARES else unit
            matches.append(
                (
                    page.page_id,
                    region_start,
                    region_start + value_line.end(),
                    parsed,
                    field_unit,
                )
            )
        return matches


def _statement_region(text: str, heading: str) -> tuple[str, int, int] | None:
    headings = list(_TABLE_HEADING_RE.finditer(text))
    targets = [match for match in headings if match.group("title") == heading]
    if len(targets) != 1:
        return None
    target = targets[0]
    end = next(
        (match.start() for match in headings if match.start() > target.start()),
        len(text),
    )
    return text[target.start() : end], target.start(), target.end() - target.start()


def _parse_decimal(value: str) -> Decimal | None:
    normalized = value.replace(",", "").replace(" ", "").replace("−", "-")
    if normalized in {"", "-", "--", "不适用"}:
        return None
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        result = Decimal(normalized)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _period_column(
    period_end: date,
    period_type: FinancialPeriodType,
    statement_type: FinancialStatementType,
) -> str:
    if statement_type is FinancialStatementType.BALANCE_SHEET:
        return f"{period_end.year}年{period_end.month}月{period_end.day}日"
    if period_type is FinancialPeriodType.ANNUAL:
        return f"{period_end.year}年度"
    if period_type is FinancialPeriodType.SEMIANNUAL:
        return f"{period_end.year}年1-6月"
    return f"{period_end.year}年1-{period_end.month}月"


__all__ = ["FinancialPdfCertifier"]
