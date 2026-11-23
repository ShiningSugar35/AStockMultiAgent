"""Strict YAML configuration for the financial-source pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from astock.schemas import FinancialFieldCode, FinancialStatementType, FinancialUnit, Market


@dataclass(frozen=True, slots=True)
class FinancialFieldMapping:
    field_code: FinancialFieldCode
    statement_type: FinancialStatementType
    official_label: str
    provider_fields: dict[str, str]
    unit: FinancialUnit
    provider_units: dict[str, FinancialUnit] = field(default_factory=dict)
    optional: bool = False
    official_label_aliases: tuple[str, ...] = ()  # Only reviewed, exact report-row synonyms.

    def provider_unit(self, provider_id: str) -> FinancialUnit:
        return self.provider_units.get(provider_id, self.unit)

    def provider_field(self, provider_id: str) -> str:
        try:
            return self.provider_fields[provider_id]
        except KeyError as exc:
            raise ValueError(f"Unknown financial provider mapping: {provider_id}") from exc


@dataclass(frozen=True, slots=True)
class FinancialSourceConfig:
    provider_order: tuple[str, ...]
    official_reports_fixture: Path
    required_statements: tuple[FinancialStatementType, ...]
    allowed_official_publishers: tuple[str, ...]
    official_market_coverage: dict[Market, str]


def load_financial_field_mappings(path: Path) -> list[FinancialFieldMapping]:
    payload = _load_yaml(path)
    if payload.get("schema_version") != "financial-field-mappings-v2":
        raise ValueError("Unsupported financial field mapping version")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("Financial field mappings are empty")
    mappings: list[FinancialFieldMapping] = []
    for raw_code, raw in fields.items():
        if not isinstance(raw, dict):
            raise ValueError("Financial field mapping must be an object")
        optional = raw.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError("Financial field optional flag must be boolean")
        official_label = raw.get("official_label")
        if not isinstance(official_label, str) or not official_label.strip():
            raise ValueError("Financial official label must be non-empty text")
        raw_aliases = raw.get("official_label_aliases", [])
        if not isinstance(raw_aliases, list) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in raw_aliases
        ):
            raise ValueError("Official financial label aliases must be a list of non-empty labels")
        aliases = tuple(raw_aliases)
        if len(aliases) != len(set(aliases)) or official_label in aliases:
            raise ValueError("Official financial label aliases must be unique and noncanonical")
        if raw_code == FinancialFieldCode.CASH_DIVIDENDS_PAID.value and aliases:
            raise ValueError("Cash dividend official label aliases are not supported")
        provider_fields = raw.get("provider_fields")
        if not isinstance(provider_fields, dict) or (not provider_fields and not optional):
            raise ValueError("Required financial field provider mappings must be non-empty")
        normalized_fields = {
            str(provider_id): str(field_name)
            for provider_id, field_name in provider_fields.items()
            if str(provider_id).strip() and str(field_name).strip()
        }
        if len(normalized_fields) != len(provider_fields):
            raise ValueError("Financial field provider mappings must be non-empty")
        raw_units = raw.get("provider_units", {})
        if not isinstance(raw_units, dict) or set(raw_units) - set(normalized_fields):
            raise ValueError("Financial unit overrides require a mapped provider")
        provider_units = {
            str(provider): FinancialUnit(str(unit)) for provider, unit in raw_units.items()
        }
        mappings.append(
            FinancialFieldMapping(
                field_code=FinancialFieldCode(str(raw_code)),
                statement_type=FinancialStatementType(str(raw["statement_type"])),
                official_label=official_label,
                provider_fields=normalized_fields,
                unit=FinancialUnit(str(raw["unit"])),
                provider_units=provider_units,
                optional=optional,
                official_label_aliases=aliases,
            )
        )
    if len({item.field_code for item in mappings}) != len(mappings):
        raise ValueError("Financial field codes must be unique")
    # An approved alias must never collide with a different financial field in
    # the same table, including a canonical label with whitespace differences.
    labels: dict[tuple[FinancialStatementType, str], FinancialFieldCode] = {}
    for mapping in mappings:
        for label in (mapping.official_label, *mapping.official_label_aliases):
            key = (mapping.statement_type, "".join(label.split()))
            previous = labels.get(key)
            if previous is not None and previous is not mapping.field_code:
                raise ValueError("Official financial label aliases overlap across field codes")
            labels[key] = mapping.field_code
    return mappings


def load_financial_source_config(path: Path) -> FinancialSourceConfig:
    payload = _load_yaml(path)
    if payload.get("schema_version") != "financial-sources-v2":
        raise ValueError("Unsupported financial source config version")
    root = path.parent.parent.resolve()
    raw_order = payload.get("provider_order")
    if not isinstance(raw_order, list) or not raw_order:
        raise ValueError("Financial source provider_order must be a non-empty list")
    provider_order = tuple(str(item) for item in raw_order)
    if len(provider_order) != len(set(provider_order)):
        raise ValueError("Financial source provider_order must be unique")
    official = (root / str(payload["official_reports_fixture"])).resolve()
    if not official.is_relative_to(root):
        raise ValueError("Official financial fixture escapes project root")
    raw_required = payload.get("required_statements")
    if not isinstance(raw_required, list):
        raise ValueError("Financial required statements must be a list")
    required = tuple(FinancialStatementType(str(item)) for item in raw_required)
    if set(required) != {
        FinancialStatementType.BALANCE_SHEET,
        FinancialStatementType.INCOME_STATEMENT,
        FinancialStatementType.CASH_FLOW_STATEMENT,
    }:
        raise ValueError("Financial sources require exactly three statements")
    raw_publishers = payload.get("allowed_official_publishers")
    if not isinstance(raw_publishers, list) or not raw_publishers:
        raise ValueError("Allowed official publishers must be a non-empty list")
    raw_coverage = payload.get("official_market_coverage")
    if not isinstance(raw_coverage, dict) or not raw_coverage:
        raise ValueError("Financial official market coverage must be a non-empty object")
    official_market_coverage: dict[Market, str] = {}
    for raw_market, raw_status in raw_coverage.items():
        market = Market(str(raw_market))
        status = str(raw_status)
        if market is Market.INDEX or status not in {"AVAILABLE", "PARTIAL", "UNAVAILABLE"}:
            raise ValueError("Financial official market coverage is invalid")
        official_market_coverage[market] = status
    return FinancialSourceConfig(
        provider_order=provider_order,
        official_reports_fixture=official,
        required_statements=required,
        allowed_official_publishers=tuple(str(item) for item in raw_publishers),
        official_market_coverage=official_market_coverage,
    )


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Financial source configuration is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Financial source configuration root must be an object")
    return payload


__all__ = [
    "FinancialFieldMapping",
    "FinancialSourceConfig",
    "load_financial_field_mappings",
    "load_financial_source_config",
]
