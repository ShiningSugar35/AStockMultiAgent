from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from astock.core.object_store import ObjectStore
from astock.financial_sources.certification import (
    FinancialPdfCertifier,
    _field_label_pattern,
    _is_rmb_currency_declaration,
    _logical_row_values,
    _statement_header_identity,
    values_equivalent,
)
from astock.financial_sources.config import FinancialFieldMapping
from astock.financial_sources.official import OfficialFinancialReport
from astock.schemas import (
    FinancialFieldCode,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
    PageExtractionMethod,
)


def _mapping(
    field_code: FinancialFieldCode,
    statement_type: FinancialStatementType,
    label: str,
) -> FinancialFieldMapping:
    return FinancialFieldMapping(
        field_code=field_code,
        statement_type=statement_type,
        official_label=label,
        provider_fields={
            "eastmoney-financial": field_code.value,
            "sina-financial": field_code.value.lower(),
        },
        unit=FinancialUnit.TEN_THOUSAND_CNY,
    )


def test_inline_unit_currency_note_column_and_two_periods_are_valid() -> None:
    header = """合并资产负债表
2026 年6 月30 日
编制单位：示例股份有限公司
单位：元币种：人民币
项目 附注七 2026 年6 月30 日 2025 年12 月31 日
流动资产：
"""

    parsed = _statement_header_identity(
        header,
        date(2026, 6, 30),
        FinancialPeriodType.SEMIANNUAL,
        FinancialStatementType.BALANCE_SHEET,
    )

    assert parsed == (FinancialUnit.CNY, 2)


def test_duplicate_unit_is_still_rejected_fail_closed() -> None:
    header = """合并资产负债表
2026 年6 月30 日
单位：元币种：人民币
单位：元
项目 附注七 2026 年6 月30 日 2025 年12 月31 日
"""

    assert (
        _statement_header_identity(
            header,
            date(2026, 6, 30),
            FinancialPeriodType.SEMIANNUAL,
            FinancialStatementType.BALANCE_SHEET,
        )
        is None
    )


def test_split_total_equity_row_extracts_current_period_without_parent_row_confusion() -> None:
    mapping = _mapping(
        FinancialFieldCode.TOTAL_EQUITY,
        FinancialStatementType.BALANCE_SHEET,
        "所有者权益合计",
    )
    text = """归属于母公司所有者权益
55,205,181,437.62 48,389,919,773.01 （或股东权益）合计
所有者权益（或股东权
55,205,181,437.62 48,389,919,773.01 益）合计
负债和所有者权益总计 94,913,197,407.90 90,152,489,325.37
"""

    rows = _logical_row_values(text, _field_label_pattern(mapping), 2)

    assert len(rows) == 1
    assert rows[0][2] == Decimal("55205181437.62")


def test_split_exchange_effect_row_extracts_negative_current_period() -> None:
    mapping = _mapping(
        FinancialFieldCode.EXCHANGE_EFFECT,
        FinancialStatementType.CASH_FLOW_STATEMENT,
        "汇率变动对现金及现金等价物的影响",
    )
    text = """三、筹资活动产生的现金流量：
筹资活动产生的现金流量净额 -5,649,576,915.88 -5,433,130,983.11
四、汇率变动对现金及现金等价物的影
-6,246,512.14 -3,121,346.70 响
五、现金及现金等价物净增加额 1,404,525,672.39 799,048,694.59
"""

    rows = _logical_row_values(text, _field_label_pattern(mapping), 2)

    assert len(rows) == 1
    assert rows[0][2] == Decimal("-6246512.14")


@pytest.mark.parametrize(
    ("field_code", "label", "text", "expected"),
    [
        (
            FinancialFieldCode.NET_CASH_OPERATING,
            "经营活动产生的现金流量净额",
            "经营活动产生的现金流\n 2,128,504,212.49 2,032,230,237.01\n量净额\n",
            Decimal("2128504212.49"),
        ),
        (
            FinancialFieldCode.NET_CASH_INVESTING,
            "投资活动产生的现金流量净额",
            "投资活动产生的现金流\n -1,390,656,105.54 -669,335,589.16\n量净额\n",
            Decimal("-1390656105.54"),
        ),
        (
            FinancialFieldCode.NET_CASH_FINANCING,
            "筹资活动产生的现金流量净额",
            "筹资活动产生的现金流\n -516,861,272.54 480,383,029.69\n量净额\n",
            Decimal("-516861272.54"),
        ),
        (
            FinancialFieldCode.EXCHANGE_EFFECT,
            "汇率变动对现金及现金等价物的影响",
            "四、汇率变动对现金及现金等\n -163,212,077.26 129,993,370.57\n价物的影响\n",
            Decimal("-163212077.26"),
        ),
        (
            FinancialFieldCode.CASH_BEGINNING,
            "期初现金及现金等价物余额",
            "加：期初现金及现金等价物\n 9,104,158,718.59 7,130,887,670.48\n余额\n",
            Decimal("9104158718.59"),
        ),
        (
            FinancialFieldCode.CASH_ENDING,
            "期末现金及现金等价物余额",
            "六、期末现金及现金等价物余\n 9,161,933,475.74 9,104,158,718.59\n额\n",
            Decimal("9161933475.74"),
        ),
    ],
)
def test_split_cash_flow_rows_extract_current_period(
    field_code: FinancialFieldCode,
    label: str,
    text: str,
    expected: Decimal,
) -> None:
    mapping = _mapping(field_code, FinancialStatementType.CASH_FLOW_STATEMENT, label)

    rows = _logical_row_values(text, _field_label_pattern(mapping), 2)

    assert len(rows) == 1
    assert rows[0][2] == expected


def test_equivalent_monetary_values_compare_after_unit_normalization() -> None:
    assert values_equivalent(
        Decimal("5520518.143762"),
        FinancialUnit.TEN_THOUSAND_CNY,
        Decimal("55205181437.62"),
        FinancialUnit.CNY,
    )
    assert not values_equivalent(
        Decimal("5520518.143762"),
        FinancialUnit.TEN_THOUSAND_CNY,
        Decimal("55205181437.63"),
        FinancialUnit.CNY,
    )

def test_sse_audit_type_is_not_part_of_currency() -> None:
    header = """合并资产负债表
2026 年9 月30 日
编制单位：真实发行人
单位：元币种：人民币审计类型：未经审计
项目 2026 年9 月30 日 2025 年12 月31 日
"""
    assert _statement_header_identity(
        header, date(2026, 9, 30), FinancialPeriodType.QUARTERLY,
        FinancialStatementType.BALANCE_SHEET,
    ) == (FinancialUnit.CNY, 2)
    assert _statement_header_identity(
        header.replace("人民币", "美元"), date(2026, 9, 30),
        FinancialPeriodType.QUARTERLY, FinancialStatementType.BALANCE_SHEET,
    ) is None
    assert _is_rmb_currency_declaration("人民币审计类型：未经审计")
    assert not _is_rmb_currency_declaration("人民币其他币种：美元")


def test_sse_income_heading_and_currency_across_two_pages() -> None:
    pages = [
        ("p1", "发行人2026年第三季度报告\n合并利润表\n2026 年1—9 月\n编制单位：真实发行人\n"),
        ("p2", "发行人2026年第三季度报告\n单位：元币种：人民币审计类型：未经审计\n"
         "2026 年前三季度     2025 年前三季度\n"
         "项目\n营业收入 7,425,159,497.23 6,408,881,906.05\n"),
    ]
    raw = {key: body.encode("utf-8") for key, body in pages}
    objects = SimpleNamespace(get_bytes=lambda key: raw[key])
    pdf_pages = [
        SimpleNamespace(page_id=key, page_number=index,
                        text_object_sha256=key,
                        extraction_method=PageExtractionMethod.NATIVE_TEXT,
                        ocr_applied=False)
        for index, (key, _) in enumerate(pages, 1)
    ]
    certifier = FinancialPdfCertifier.__new__(FinancialPdfCertifier)
    certifier.objects = cast(ObjectStore, objects)
    matches = certifier._exact_matches(
        cast(OfficialFinancialReport, SimpleNamespace(pages=pdf_pages)),
        _mapping(FinancialFieldCode.REVENUE, FinancialStatementType.INCOME_STATEMENT,
                 "营业收入"),
        date(2026, 9, 30), FinancialPeriodType.QUARTERLY,
    )
    assert len(matches) == 1
    assert matches[0][3] == Decimal("7425159497.23")
    assert matches[0][4] is FinancialUnit.CNY
