"""Audited official-label aliases: compatibility without vague row matching.

Aliases must be explicit in the versioned mapping. They do not override issuer,
statement header, reporting period, currency, or exact-row uniqueness gates.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from astock.core.object_store import ObjectStore
from astock.financial_sources.certification import (
    FinancialPdfCertifier,
    _field_label_pattern,
    _logical_row_values,
)
from astock.financial_sources.config import (
    FinancialFieldMapping,
    load_financial_field_mappings,
)
from astock.financial_sources.official import OfficialFinancialReport
from astock.schemas import (
    FinancialFieldCode,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
    PageExtractionMethod,
)

ROOT = Path(__file__).resolve().parents[2]


def _mapping() -> FinancialFieldMapping:
    return FinancialFieldMapping(
        field_code=FinancialFieldCode.TOTAL_ASSETS,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        official_label="资产总计", official_label_aliases=("资产总额",),
        provider_fields={"sina-financial": "total_assets"},
        unit=FinancialUnit.TEN_THOUSAND_CNY,
    )


def _source(text: str):
    objects = SimpleNamespace(get_bytes=lambda _: text.encode("utf-8"))
    page = SimpleNamespace(
        page_id="page-1", page_number=1, text_object_sha256="object-1",
        extraction_method=PageExtractionMethod.NATIVE_TEXT, ocr_applied=False,
    )
    report = cast(OfficialFinancialReport, SimpleNamespace(pages=[page]))
    certifier = FinancialPdfCertifier.__new__(FinancialPdfCertifier)
    certifier.objects = cast(ObjectStore, objects)
    return certifier, report


def test_exact_approved_alias_is_recognized_without_free_text_fuzzy_match() -> None:
    label = _field_label_pattern(_mapping())
    rows = _logical_row_values("资产总额 1,000.00 900.00\n", label, 2)
    assert len(rows) == 1 and rows[0][2] == Decimal("1000.00")
    assert not _logical_row_values("资产总产量 1,000.00 900.00\n", label, 2)


def test_unreviewed_alias_is_still_rejected() -> None:
    strict = replace(_mapping(), official_label_aliases=())
    rows = _logical_row_values("资产总额 1,000.00 900.00\n", _field_label_pattern(strict), 2)
    assert rows == []


def test_duplicate_alias_rows_cannot_be_automatically_certified() -> None:
    text = (
        "合并资产负债表\n2026年9月30日\n"
        "编制单位：发行人\n单位：元币种：人民币\n"
        "2026年9月30日 2025年12月31日\n"
        "资产总计 1,000.00 900.00\n"
        "资产总额 1,100.00 950.00\n"
    )
    certifier, report = _source(text)
    matches = certifier._exact_matches(
        report, _mapping(), date(2026, 9, 30), FinancialPeriodType.QUARTERLY,
    )
    assert len(matches) == 2  # The caller rejects !=1. No convenient value is chosen.


def test_wrong_currency_rejected_even_with_approved_alias() -> None:
    text = (
        "合并资产负债表\n2026年9月30日\n"
        "编制单位：发行人\n单位：元币种：美元\n"
        "2026年9月30日 2025年12月31日\n"
        "资产总额 1,000.00 900.00\n"
    )
    certifier, report = _source(text)
    assert certifier._exact_matches(
        report, _mapping(), date(2026, 9, 30), FinancialPeriodType.QUARTERLY,
    ) == []


def test_alias_mapping_loader_accepts_reviewed_unique_labels(tmp_path: Path) -> None:
    conf = tmp_path / "mapping.yaml"
    conf.write_text(
        'schema_version: financial-field-mappings-v2\nfields:\n'
        '  TOTAL_ASSETS:\n    statement_type: BALANCE_SHEET\n'
        '    official_label: 资产总计\n'
        '    official_label_aliases: [资产总额]\n'
        '    provider_fields: {sina-financial: total_assets}\n'
        '    unit: CNY\n',
        encoding="utf-8",
    )
    mapping = load_financial_field_mappings(conf)[0]
    assert mapping.official_label_aliases == ("资产总额",)


@pytest.mark.parametrize(
    "aliases",
    [
        "[资产总额, 资产总额]",
        "[资产总计]",
        '[资产总额, ""]',
        '"资产总额"',
    ],
)
def test_alias_mapping_rejects_duplicate_canonical_or_non_list(
    tmp_path: Path, aliases: str,
) -> None:
    conf = tmp_path / "mapping.yaml"
    conf.write_text(
        'schema_version: financial-field-mappings-v2\nfields:\n'
        '  TOTAL_ASSETS:\n    statement_type: BALANCE_SHEET\n'
        '    official_label: 资产总计\n'
        f'    official_label_aliases: {aliases}\n'
        '    provider_fields: {sina-financial: total_assets}\n'
        '    unit: CNY\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="alias"):
        load_financial_field_mappings(conf)


def test_dividend_exact_payment_row_cannot_take_generic_label_alias(
    tmp_path: Path,
) -> None:
    conf = tmp_path / "mapping.yaml"
    conf.write_text(
        'schema_version: financial-field-mappings-v2\nfields:\n'
        '  CASH_DIVIDENDS_PAID:\n'
        '    statement_type: CASH_FLOW_STATEMENT\n'
        '    official_label: 向本公司普通股股东支付的现金股利\n'
        '    official_label_aliases: [分配股利利润和偿付利息支付的现金]\n'
        '    provider_fields: {}\n    optional: true\n    unit: CNY\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="alias"):
        load_financial_field_mappings(conf)


def test_same_statement_alias_cannot_shadow_another_field(tmp_path: Path) -> None:
    conf = tmp_path / "mapping.yaml"
    conf.write_text(
        "schema_version: financial-field-mappings-v2\nfields:\n"
        "  TOTAL_ASSETS:\n"
        "    statement_type: BALANCE_SHEET\n"
        "    official_label: 资产总计\n"
        "    official_label_aliases: [资产 总额]\n"
        "    provider_fields: {sina-financial: total_assets}\n    unit: CNY\n"
        "  TOTAL_LIABILITIES:\n"
        "    statement_type: BALANCE_SHEET\n"
        "    official_label: 资产总额\n"
        "    provider_fields: {sina-financial: total_liabilities}\n    unit: CNY\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overlap"):
        load_financial_field_mappings(conf)


def test_existing_production_mappings_remain_strict_until_aliases_are_reviewed() -> None:
    mappings = load_financial_field_mappings(
        ROOT / "configs" / "financial_field_mappings.yaml"
    )
    assert mappings and all(not m.official_label_aliases for m in mappings)
