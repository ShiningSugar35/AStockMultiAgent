from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from astock.providers import load_provider_registry
from astock.schemas import ProviderRegistry


def test_project_provider_registry_is_strict_and_declares_only_implemented_providers() -> None:
    registry = load_provider_registry(Path("configs/provider_registry.yaml"))

    assert [item.provider_id for item in registry.providers] == [
        "eastmoney-5m",
        "sina-5m",
        "cninfo-disclosures",
        "baostock-reference",
        "eastmoney-reference",
    ]
    assert "corporate_actions.ledger_ready" in registry.capability_gaps
    assert "financial.structured" in registry.capability_gaps
    assert all((Path.cwd() / item.recorded_fixture).is_file() for item in registry.providers)


def test_registry_rejects_unknown_keys_duplicate_ids_and_implemented_gap() -> None:
    payload = yaml.safe_load(Path("configs/provider_registry.yaml").read_text(encoding="utf-8"))
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ProviderRegistry.model_validate(payload)

    payload.pop("unexpected")
    payload["providers"].append(dict(payload["providers"][0]))
    with pytest.raises(ValidationError, match="provider_id values must be unique"):
        ProviderRegistry.model_validate(payload)

    payload["providers"].pop()
    payload["capability_gaps"].append("market.raw_5m")
    with pytest.raises(ValidationError, match="implemented capabilities"):
        ProviderRegistry.model_validate(payload)


def test_registry_rejects_fixture_path_traversal() -> None:
    payload = yaml.safe_load(Path("configs/provider_registry.yaml").read_text(encoding="utf-8"))
    payload["providers"][0]["recorded_fixture"] = "../private/cookie.json"
    with pytest.raises(ValidationError, match="traverse"):
        ProviderRegistry.model_validate(payload)
