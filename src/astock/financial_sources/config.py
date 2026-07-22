"""Strict YAML configuration for the financial-source pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from astock.schemas import FinancialFieldCode, FinancialStatementType, FinancialUnit


@dataclass(frozen=True, slots=True)
class FinancialFieldMapping:
    field_code: FinancialFieldCode
    statement_type: FinancialStatementType
    official_label: str
    eastmoney_field: str
    sina_field: str
    unit: FinancialUnit

    def provider_field(self, provider_id: str) -> str:
        if provider_id == "eastmoney-financial":
            return self.eastmoney_field
        if provider_id == "sina-financial":
            return self.sina_field
        raise ValueError("Unknown financial provider mapping")


@dataclass(frozen=True, slots=True)
class FinancialSourceConfig:
    primary_provider: str
    backup_provider: str
    provider_fixtures: dict[str, Path]
    official_reports_fixture: Path
    required_statements: tuple[FinancialStatementType, ...]
    allowed_official_publishers: tuple[str, ...]


def load_financial_field_mappings(path: Path) -> list[FinancialFieldMapping]:
    payload = _load_yaml(path)
    if payload.get("schema_version") != "financial-field-mappings-v1":
        raise ValueError("Unsupported financial field mapping version")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("Financial field mappings are empty")
    mappings: list[FinancialFieldMapping] = []
    for raw_code, raw in fields.items():
        if not isinstance(raw, dict):
            raise ValueError("Financial field mapping must be an object")
        mappings.append(
            FinancialFieldMapping(
                field_code=FinancialFieldCode(str(raw_code)),
                statement_type=FinancialStatementType(str(raw["statement_type"])),
                official_label=str(raw["official_label"]),
                eastmoney_field=str(raw["eastmoney_field"]),
                sina_field=str(raw["sina_field"]),
                unit=FinancialUnit(str(raw["unit"])),
            )
        )
    if len({item.field_code for item in mappings}) != len(mappings):
        raise ValueError("Financial field codes must be unique")
    return mappings


def load_financial_source_config(path: Path) -> FinancialSourceConfig:
    payload = _load_yaml(path)
    if payload.get("schema_version") != "financial-sources-v1":
        raise ValueError("Unsupported financial source config version")
    root = path.parent.parent.resolve()
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("Financial source providers are missing")
    provider_fixtures: dict[str, Path] = {}
    for provider_id in (str(payload["primary_provider"]), str(payload["backup_provider"])):
        raw = providers.get(provider_id)
        if not isinstance(raw, dict) or raw.get("officiality") != "SECONDARY_STRUCTURED":
            raise ValueError("Financial providers must be SECONDARY_STRUCTURED")
        fixture = (root / str(raw["recorded_fixture"])).resolve()
        if not fixture.is_relative_to(root):
            raise ValueError("Financial provider fixture escapes project root")
        provider_fixtures[provider_id] = fixture
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
    return FinancialSourceConfig(
        primary_provider=str(payload["primary_provider"]),
        backup_provider=str(payload["backup_provider"]),
        provider_fixtures=provider_fixtures,
        official_reports_fixture=official,
        required_statements=required,
        allowed_official_publishers=tuple(str(item) for item in raw_publishers),
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
