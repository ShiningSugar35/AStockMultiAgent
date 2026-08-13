"""Versioned provider dialect contracts for schema-drift-tolerant adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ProviderDialect:
    provider_id: str
    dialect_version: str
    endpoint: str
    response_shape: str
    transport_profile: str
    response_paths: dict[str, str]
    statement_sources: dict[str, str]
    field_aliases: dict[str, dict[str, str]]
    share_fields: frozenset[str]
    scope_prefixes: dict[str, tuple[str, ...]]
    currency_field: str | None
    allowed_currencies: frozenset[str]
    report_type_field: str | None
    report_items_key: str | None
    item_field_key: str | None
    item_value_key: str | None
    identity_fields: tuple[str, ...]
    scope_field: str | None
    native_monetary_unit: str
    monetary_scale_to_internal: Decimal

    def source_for(self, statement: str) -> str:
        try:
            return self.statement_sources[statement]
        except KeyError as exc:
            raise ValueError(
                f"Dialect {self.dialect_version} does not support statement {statement}"
            ) from exc

    def alias_for(self, statement: str, source_field: str) -> str | None:
        return self.field_aliases.get(statement, {}).get(source_field)

    def scope_from(self, value: object) -> str | None:
        text = str(value or "").strip()
        for scope, prefixes in self.scope_prefixes.items():
            if any(text.startswith(prefix) for prefix in prefixes):
                return scope
        return None

    def value_at(self, payload: object, path_name: str) -> object:
        try:
            path = self.response_paths[path_name]
        except KeyError as exc:
            raise ValueError(
                f"Dialect {self.dialect_version} has no response path {path_name}"
            ) from exc
        current: object = payload
        for segment in path.split("."):
            if not isinstance(current, dict) or segment not in current:
                raise ValueError(
                    f"Dialect {self.dialect_version} response path not found: {path}"
                )
            current = current[segment]
        return current


def load_provider_dialects(path: Path) -> dict[str, ProviderDialect]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid provider dialect configuration: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "provider-dialects-v1":
        raise ValueError("Unsupported provider dialect configuration")
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ValueError("Provider dialect configuration is empty")
    result: dict[str, ProviderDialect] = {}
    for provider_id, raw in raw_providers.items():
        if not isinstance(raw, dict):
            raise ValueError("Provider dialect must be an object")
        raw_sources = raw.get("statement_sources", {})
        raw_paths = raw.get("response_paths", {})
        raw_aliases = raw.get("field_aliases", {})
        raw_scopes = raw.get("scope_prefixes", {})
        if not isinstance(raw_sources, dict) or not raw_sources:
            raise ValueError("Financial provider dialect requires statement_sources")
        if not isinstance(raw_paths, dict) or not raw_paths:
            raise ValueError("Provider dialect requires response_paths")
        if not isinstance(raw_aliases, dict) or not isinstance(raw_scopes, dict):
            raise ValueError("Provider dialect aliases/scopes must be objects")
        aliases: dict[str, dict[str, str]] = {}
        for statement, values in raw_aliases.items():
            if not isinstance(values, dict):
                raise ValueError("Provider field aliases must be an object")
            aliases[str(statement)] = {
                str(source): str(target) for source, target in values.items()
            }
        scopes: dict[str, tuple[str, ...]] = {}
        for scope, values in raw_scopes.items():
            if not isinstance(values, list) or not values:
                raise ValueError("Provider scope prefixes must be a non-empty list")
            scopes[str(scope)] = tuple(str(item) for item in values)
        identity_fields_raw = raw.get("identity_fields", [])
        if not isinstance(identity_fields_raw, list):
            raise ValueError("Provider identity_fields must be a list")
        allowed_raw = raw.get("allowed_currencies", [])
        if not isinstance(allowed_raw, list):
            raise ValueError("Provider allowed_currencies must be a list")
        share_raw = raw.get("share_fields", [])
        if not isinstance(share_raw, list):
            raise ValueError("Provider share_fields must be a list")
        dialect = ProviderDialect(
            provider_id=str(provider_id),
            dialect_version=str(raw["dialect_version"]),
            endpoint=str(raw["endpoint"]),
            response_shape=str(raw["response_shape"]),
            transport_profile=str(raw["transport_profile"]),
            response_paths={str(key): str(value) for key, value in raw_paths.items()},
            statement_sources={str(key): str(value) for key, value in raw_sources.items()},
            field_aliases=aliases,
            share_fields=frozenset(str(item) for item in share_raw),
            scope_prefixes=scopes,
            currency_field=str(raw["currency_field"]) if raw.get("currency_field") else None,
            allowed_currencies=frozenset(str(item) for item in allowed_raw),
            report_type_field=(
                str(raw["report_type_field"]) if raw.get("report_type_field") else None
            ),
            report_items_key=(
                str(raw["report_items_key"]) if raw.get("report_items_key") else None
            ),
            item_field_key=str(raw["item_field_key"]) if raw.get("item_field_key") else None,
            item_value_key=str(raw["item_value_key"]) if raw.get("item_value_key") else None,
            identity_fields=tuple(str(item) for item in identity_fields_raw),
            scope_field=str(raw["scope_field"]) if raw.get("scope_field") else None,
            native_monetary_unit=str(raw["native_monetary_unit"]),
            monetary_scale_to_internal=Decimal(str(raw["monetary_scale_to_internal"])),
        )
        if not dialect.dialect_version or not dialect.endpoint.startswith("https://"):
            raise ValueError("Provider dialect identity/endpoint is invalid")
        if dialect.monetary_scale_to_internal <= 0:
            raise ValueError("Provider dialect monetary scale must be positive")
        result[dialect.provider_id] = dialect
    return result


__all__ = ["ProviderDialect", "load_provider_dialects"]
