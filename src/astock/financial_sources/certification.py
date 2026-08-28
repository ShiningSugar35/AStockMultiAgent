"""Fail-closed native-PDF financial-number certification for real issuer reports."""

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
    DocumentPage,
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
_UNIT_MULTIPLIERS = {
    FinancialUnit.CNY: Decimal("1"),
    FinancialUnit.THOUSAND_CNY: Decimal("1000"),
    FinancialUnit.TEN_THOUSAND_CNY: Decimal("10000"),
    FinancialUnit.MILLION_CNY: Decimal("1000000"),
    FinancialUnit.HUNDRED_MILLION_CNY: Decimal("100000000"),
    FinancialUnit.SHARES: Decimal("1"),
}
_TABLE_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(?P<title>(?:合并|母公司|公司)?"
    r"(?:资产负债表|利润表|现金流量表))[ \t]*$"
)
_CURRENCY_TOKEN_RE = re.compile(r"币种\s*[:：]\s*人民币")
_UNIT_TOKEN_RE = re.compile(r"单位\s*[:：]\s*(百万元|万元|千元|亿元|元)")
_GENERIC_PERIOD_TOKEN_RE = re.compile(
    r"\d{4}\s*年\s*(?:"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日|"
    r"度|半\s*年\s*度|(?:第?[一三123])\s*季\s*度|"
    r"1\s*[-－—至]\s*(?:3|6|9|12)\s*月"
    r")"
)
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\d.])(?:\([0-9][0-9,]*(?:\.[0-9]+)?\)|"
    r"[−-]?[0-9][0-9,]*(?:\.[0-9]+)?)(?![\d.])"
)
_HEADER_SCAN_CHARS = 1800


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

    def extract_values(
        self,
        report: OfficialFinancialReport,
        period_end: date,
        period_type: FinancialPeriodType,
        mappings: list[FinancialFieldMapping],
    ) -> tuple[list[tuple[FinancialFieldMapping, Decimal, FinancialUnit]], list[str]]:
        """Extract uniquely identified values from an official report without secondary hints."""

        values: list[tuple[FinancialFieldMapping, Decimal, FinancialUnit]] = []
        reasons: list[str] = []
        for mapping in mappings:
            matches = self._exact_matches(report, mapping, period_end, period_type)
            if len(matches) != 1:
                code = "OFFICIAL_VALUE_NOT_FOUND" if not matches else "OFFICIAL_VALUE_AMBIGUOUS"
                reasons.append(f"{code}:{mapping.field_code.value}")
                continue
            _page_id, _char_start, _char_end, value, unit = matches[0]
            values.append((mapping, value, unit))
        return values, list(dict.fromkeys(reasons))

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
            hint_value = hint.reported_value
            if hint_value is None:
                raise ValueError("financial certification hint unexpectedly lost its value")
            if not _values_equivalent(hint_value, hint.unit, value, unit):
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
        statement = _statement_segments(
            report,
            self.objects,
            _HEADINGS[mapping.statement_type],
        )
        if statement is None:
            return []
        segments, heading_page_id, heading_start = statement
        header_text = segments[0][1][segments[0][2] :][: _HEADER_SCAN_CHARS]
        header = _statement_header_identity(
            header_text,
            period_end,
            period_type,
            mapping.statement_type,
        )
        if header is None:
            return []
        statement_unit, period_column_count = header
        field_unit = (
            FinancialUnit.SHARES
            if mapping.unit is FinancialUnit.SHARES
            else statement_unit
        )
        label_re = _field_label_pattern(mapping)
        matches: list[tuple[str, int, int, Decimal, FinancialUnit]] = []
        for page_id, text, segment_start, segment_end in segments:
            segment_text = text[segment_start:segment_end]
            for row_start, row_end, value in _logical_row_values(
                segment_text,
                label_re,
                period_column_count,
            ):
                absolute_start = segment_start + row_start
                absolute_end = segment_start + row_end
                evidence_start = (
                    heading_start
                    if page_id == heading_page_id and heading_start <= absolute_start
                    else absolute_start
                )
                matches.append(
                    (
                        page_id,
                        evidence_start,
                        absolute_end,
                        value,
                        field_unit,
                    )
                )
        return matches


def _statement_segments(
    report: OfficialFinancialReport,
    objects: ObjectStore,
    heading: str,
) -> tuple[list[tuple[str, str, int, int]], str, int] | None:
    pages = sorted(report.pages, key=lambda item: item.page_number)
    native: list[tuple[DocumentPage, str, list[re.Match[str]]]] = []
    targets: list[tuple[int, re.Match[str]]] = []
    for page in pages:
        if page.extraction_method is not PageExtractionMethod.NATIVE_TEXT or page.ocr_applied:
            continue
        text = objects.get_bytes(page.text_object_sha256).decode("utf-8")
        headings = list(_TABLE_HEADING_RE.finditer(text))
        native.append((page, text, headings))
        for match in headings:
            if match.group("title") == heading:
                targets.append((len(native) - 1, match))
    if len(targets) != 1:
        return None
    start_index, target = targets[0]
    segments: list[tuple[str, str, int, int]] = []
    heading_page = native[start_index][0]
    for index in range(start_index, len(native)):
        page, text, headings = native[index]
        if index == start_index:
            segment_start = target.start()
            later = [match.start() for match in headings if match.start() > target.start()]
            segment_end = min(later) if later else len(text)
            segments.append((page.page_id, text, segment_start, segment_end))
            if later:
                break
            continue
        if headings:
            segments.append((page.page_id, text, 0, headings[0].start()))
            break
        segments.append((page.page_id, text, 0, len(text)))
    return segments, heading_page.page_id, target.start()


def _statement_header_identity(
    header_text: str,
    period_end: date,
    period_type: FinancialPeriodType,
    statement_type: FinancialStatementType,
) -> tuple[FinancialUnit, int] | None:
    currency = list(_CURRENCY_TOKEN_RE.finditer(header_text))
    units = list(_UNIT_TOKEN_RE.finditer(header_text))
    if len(currency) != 1 or len(units) != 1:
        return None
    period_patterns = _target_period_patterns(period_end, period_type, statement_type)
    target_matches: list[re.Match[str]] = []
    for pattern in period_patterns:
        found = list(pattern.finditer(header_text))
        if found:
            target_matches = found
            break
    if not target_matches:
        return None
    period_column_count = 1
    for target in target_matches:
        line_start = header_text.rfind("\n", 0, target.start()) + 1
        line_end = header_text.find("\n", target.end())
        if line_end < 0:
            line_end = len(header_text)
        line = header_text[line_start:line_end]
        period_column_count = max(
            period_column_count,
            len(_GENERIC_PERIOD_TOKEN_RE.findall(line)),
        )
    return _UNITS[units[0].group(1)], period_column_count


def _target_period_patterns(
    period_end: date,
    period_type: FinancialPeriodType,
    statement_type: FinancialStatementType,
) -> list[re.Pattern[str]]:
    year = period_end.year
    if statement_type is FinancialStatementType.BALANCE_SHEET:
        return [
            re.compile(
                rf"{year}\s*年\s*{period_end.month}\s*月\s*{period_end.day}\s*日"
            )
        ]
    if period_type is FinancialPeriodType.ANNUAL:
        return [re.compile(rf"{year}\s*年\s*度")]
    if period_type is FinancialPeriodType.SEMIANNUAL:
        return [
            re.compile(rf"{year}\s*年\s*半\s*年\s*度"),
            re.compile(rf"{year}\s*年\s*1\s*[-－—至]\s*6\s*月"),
        ]
    quarter = "1" if period_end.month == 3 else "3"
    chinese = "一" if period_end.month == 3 else "三"
    return [
        re.compile(rf"{year}\s*年\s*(?:第\s*)?{chinese}\s*季\s*度"),
        re.compile(rf"{year}\s*年\s*第?{quarter}\s*季\s*度"),
        re.compile(rf"{year}\s*年\s*1\s*[-－—至]\s*{period_end.month}\s*月"),
    ]


def _label_pattern(label: str) -> re.Pattern[str]:
    escaped = r"\s*".join(re.escape(character) for character in label)
    return re.compile(rf"(?<![\u4e00-\u9fff]){escaped}(?![\u4e00-\u9fff])")


def _field_label_pattern(mapping: FinancialFieldMapping) -> re.Pattern[str]:
    if mapping.field_code is FinancialFieldCode.TOTAL_EQUITY:
        return re.compile(
            r"(?<![\u4e00-\u9fff])"
            r"所\s*有\s*者\s*权\s*益"
            r"(?:\s*[（(]\s*或\s*股\s*东\s*权"
            r"[\s\d,().（）−\-]*"
            r"益\s*[）)])?"
            r"\s*合\s*计"
            r"(?![\u4e00-\u9fff])"
        )
    interleaved_cash_flow_fields = {
        FinancialFieldCode.NET_CASH_OPERATING,
        FinancialFieldCode.NET_CASH_INVESTING,
        FinancialFieldCode.NET_CASH_FINANCING,
        FinancialFieldCode.EXCHANGE_EFFECT,
        FinancialFieldCode.CASH_BEGINNING,
        FinancialFieldCode.CASH_ENDING,
    }
    if mapping.field_code in interleaved_cash_flow_fields:
        label_re = r"[\s\d,().（）−\-]*".join(
            re.escape(character) for character in mapping.official_label
        )
        return re.compile(
            rf"(?<![\u4e00-\u9fff]){label_re}(?![\u4e00-\u9fff])"
        )
    return _label_pattern(mapping.official_label)


def _logical_row_values(
    segment_text: str,
    label_re: re.Pattern[str],
    period_column_count: int,
) -> list[tuple[int, int, Decimal]]:
    lines = list(re.finditer(r"(?m)^.*(?:\n|$)", segment_text))
    result: list[tuple[int, int, Decimal]] = []
    for start_index, first_line in enumerate(lines):
        first_line_end = first_line.end()
        for size in (1, 2, 3):
            end_index = start_index + size - 1
            if end_index >= len(lines):
                break
            row_start = first_line.start()
            row_end = lines[end_index].end()
            window = segment_text[row_start:row_end]
            label_match = label_re.search(window)
            if label_match is None:
                continue
            first_relative_end = first_line_end - row_start
            if label_match.start() >= first_relative_end:
                continue
            numeric = [
                parsed
                for token in _NUMBER_TOKEN_RE.findall(window)
                if (parsed := _parse_decimal(token)) is not None
            ]
            if len(numeric) < period_column_count:
                continue
            result.append((row_start, row_end, numeric[-period_column_count]))
            break
    return result


def _values_equivalent(
    left: Decimal,
    left_unit: FinancialUnit,
    right: Decimal,
    right_unit: FinancialUnit,
) -> bool:
    left_multiplier = _UNIT_MULTIPLIERS.get(left_unit)
    right_multiplier = _UNIT_MULTIPLIERS.get(right_unit)
    if left_multiplier is None or right_multiplier is None:
        return left == right and left_unit is right_unit
    return left * left_multiplier == right * right_multiplier


def _parse_decimal(value: str) -> Decimal | None:
    normalized = (
        value.replace(",", "")
        .replace(" ", "")
        .replace("−", "-")
        .replace("—", "-")
    )
    if normalized in {"", "-", "--", "不适用"}:
        return None
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        result = Decimal(normalized)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


__all__ = ["FinancialPdfCertifier"]
