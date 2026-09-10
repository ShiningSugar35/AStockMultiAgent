"""Isolated regressions from the September 8 progress supervision review."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from astock.investor_orchestration.models import RequestIntent, SideEffectClass
from astock.investor_orchestration.scenarios import BusinessScenario
from astock.investor_orchestration.store import InvestorOrchestrationStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def controlled_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "supervision_controlled_live",
        ROOT / "scripts/run_investor_orchestration_controlled_live.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deny_network(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("offline smoke must not perform a network request")


def test_controlled_script_reuses_confirmation_on_same_and_next_day(
    controlled_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "controlled.sqlite"
    base = datetime.now(UTC) - timedelta(days=2)

    class RecordedClock(datetime):
        current = base

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(controlled_script, "datetime", RecordedClock)
    monkeypatch.setattr(httpx.Client, "send", _deny_network)
    monkeypatch.setattr("sys.argv", ["controlled-live", "--database", str(database)])
    outputs = []
    for seconds in (0, 1, 86400):
        RecordedClock.current = base + timedelta(seconds=seconds)
        controlled_script.main()
        outputs.append(json.loads(capsys.readouterr().out))
    first, repeated, next_day = [item["scheduled_receipt"] for item in outputs]
    assert first["receipt_id"] == repeated["receipt_id"]
    assert first["receipt_id"] != next_day["receipt_id"]
    assert len({item["binding_id"] for item in (first, repeated, next_day)}) == 1
    binding = InvestorOrchestrationStore(database).get_binding(first["binding_id"])
    assert binding is not None and binding.confirmed_at == base
    for result in outputs:
        checks = {item["check_type"]: item["status"] for item in result["checks"]}
        assert checks == {"CURRENT_MACRO": "NOT_RUN", "SCHEDULED_RESEARCH": "BLOCKED"}
        assert result["scheduled_receipt"]["capability_receipt_id"] is None
        assert result["feature_activation_changed"] is False


def test_controlled_script_does_not_accept_other_immutable_binding_changes(
    controlled_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "controlled-conflict.sqlite"
    monkeypatch.setattr(httpx.Client, "send", _deny_network)
    monkeypatch.setattr("sys.argv", ["controlled-live", "--database", str(database)])
    controlled_script.main()
    initial = json.loads(capsys.readouterr().out)
    binding_id = initial["scheduled_receipt"]["binding_id"]
    original_get = InvestorOrchestrationStore.get_binding

    def conflicting_get(self: InvestorOrchestrationStore, selected_id: str) -> Any:
        binding = original_get(self, selected_id)
        if binding is not None and selected_id == binding_id:
            return binding.model_copy(update={"schedule_expression": "DIFFERENT_CONTRACT"})
        return binding

    monkeypatch.setattr(InvestorOrchestrationStore, "get_binding", conflicting_get)
    with pytest.raises(ValueError, match="immutable fields"):
        controlled_script.main()


@pytest.mark.parametrize(
    ("left", "right"),
    [("required", "conditional"), ("required", "prohibited"), ("conditional", "prohibited")],
)
def test_scenario_capability_partitions_cannot_overlap(left: str, right: str) -> None:
    body: dict[str, Any] = {
        "id": 1,
        "title": "isolated contract probe",
        "intent": RequestIntent.RESEARCH,
        "required": ("REQUEST_TIME",),
        "conditional": (),
        "prohibited": (),
        "allowed_side_effects": (SideEffectClass.READ,),
        "side_effect_description": "read only",
        "acceptance": "all capability classes are disjoint",
        "source_chain": "recorded contract",
    }
    body[left] = ("FINANCIAL_INTEGRITY",)
    body[right] = ("FINANCIAL_INTEGRITY",)
    with pytest.raises(ValueError, match="overlap"):
        BusinessScenario.model_validate(body)
