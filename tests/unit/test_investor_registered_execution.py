"""Canonical artifact execution and two-time current research regressions.

Local SQLite/ObjectStore/ledger are real; no external provider or model is called.
These are interface regressions, not a replacement for the 68 business E2E cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.capabilities import CapabilityPlanner
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier, output_model
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.ledger import LedgerService


@pytest.fixture(scope="module")
def environment(tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp("registered-execution")
    store = InvestorOrchestrationStore(directory / "state.sqlite")
    store.initialize()
    objects = ObjectStore(directory / "objects" / "sha256")
    ledger = LedgerService(StateStore(store.path), objects)
    ledger.initialize_account("paper", 100_000)
    return store, objects, ledger


def request(**updates: Any) -> InvestorRequestEnvelope:
    identity = f"registered-test-{uuid4()}"
    values: dict[str, Any] = {
        "request_id": identity,
        "idempotency_key": identity,
        "question_time": datetime.now(UTC),
        "user_timezone": "Asia/Shanghai",
        "raw_text": "核对模拟账户现金",
        "normalized_intent": RequestIntent.PAPER_STATUS,
        "side_effect": SideEffectClass.READ,
        "account_id": "paper",
    }
    values.update(updates)
    return InvestorRequestEnvelope(**values)


def register(environment: Any, value: BaseModel) -> str:
    store, objects, _ = environment
    identity = f"registered-test-artifact:{uuid4()}"
    reference = objects.put_json(value.model_dump(mode="json"))
    StateStore(store.path).register_artifact(
        artifact_id=identity,
        artifact_type=type(value).__name__,
        schema_version=str(getattr(value, "schema_version", "1")),
        object_hash=reference.sha256,
        input_hashes=[],
    )
    return identity


def test_each_planned_external_output_has_a_real_canonical_model(environment: Any) -> None:
    store, _, _ = environment
    service = InvestorOrchestrationService(store)
    exempt = {
        "REQUEST_TIME",
        "ENTITY_IDENTITY",
        "SESSION_PREFLIGHT",
        "MARKET_REGIME",
        "SUBJECT_REGISTRY",
        "RESPONSE_GATEWAY",
    }
    sides = {
        RequestIntent.ACCOUNT_FACT_WRITE: SideEffectClass.EA_WRITE,
        RequestIntent.PAPER_PREPARE: SideEffectClass.PT_PREPARE,
        RequestIntent.PAPER_CONFIRM: SideEffectClass.PT_CONFIRM,
    }
    missing = set()
    for intent in RequestIntent:
        current = request(
            normalized_intent=intent, side_effect=sides.get(intent, SideEffectClass.READ)
        )
        preflight = service.preflight_service.build(current)
        plan = CapabilityPlanner().plan(current, preflight)
        for node in plan.nodes:
            if node.capability_id in exempt or node.requirement.value == "PROHIBITED":
                continue
            try:
                output_model(node.output_schema)
            except ValueError:
                missing.add(node.output_schema)
    assert not missing, f"unconnected canonical contracts: {sorted(missing)}"


def test_registered_request_id_cannot_be_rebound_to_different_user_content(
    environment: Any,
) -> None:
    store, _, _ = environment
    service = InvestorOrchestrationService(store)
    original = request()
    service.prepare(original)
    with pytest.raises(ValueError, match="collision|different|request"):
        service.prepare(original.model_copy(update={"raw_text": "另一条未经确认的指令"}))


def test_nested_future_availability_cannot_hide_behind_an_old_top_level_timestamp() -> None:
    at = datetime.now(UTC)
    with pytest.raises(ValueError, match="frozen|available|future"):
        RegisteredOutputVerifier._check_time(
            {
                "as_of": at.isoformat(),
                "created_at": at.isoformat(),
                "market_price_anchor": {
                    "available_to_system_at": (at + timedelta(days=1)).isoformat(),
                },
            },
            at,
        )


def test_completed_status_does_not_override_partial_financial_coverage(environment: Any) -> None:
    from astock.schemas.financial import FinancialIntegrityEvidencePack

    store, _, _ = environment
    at = datetime.now(UTC)
    # This is intentionally a valid-schema but incomplete financial result.
    value = FinancialIntegrityEvidencePack.model_validate(
        {
            "created_at": at,
            "audit_run_id": "partial-financial",
            "request_hash": "a" * 64,
            "status": "SUCCEEDED",
            "coverage_status": "PARTIAL",
            "company_id": "600519",
            "as_of": at,
            "industry_profile": "GENERAL_INDUSTRIAL",
            "periods": [],
            "input_fact_ids": [],
            "source_snapshot_ids": [],
            "pit_ids": [],
            "verified_numbers": [],
            "recalculated_metrics": [],
            "rule_findings": [],
            "risk_level": "LOW",
            "rule_versions": {},
            "model_versions": {},
            "capability_status": {},
        }
    )
    identity = register(environment, value)
    current = request(entity_ids=("XSHG:600519",), question_time=at)
    preflight = InvestorOrchestrationService(store).preflight_service.build(current)
    from astock.investor_orchestration.models import CapabilityNode, CapabilityRequirement

    node = CapabilityNode(
        capability_id="FINANCIAL_INTEGRITY",
        output_schema="FinancialIntegrityEvidencePack",
        requirement=CapabilityRequirement.REQUIRED,
    )
    with pytest.raises(ValueError, match="completion|coverage|domain"):
        RegisteredOutputVerifier(store).verify(node, (identity,), current, preflight)


def test_current_execution_accepts_data_observed_after_the_original_question(
    environment: Any,
) -> None:
    store, _, ledger = environment
    original = request()
    # Separate the Windows wall-clock ticks; evidence must actually be later.
    import time

    time.sleep(0.02)
    nav = ledger.portfolio_nav("paper", as_of=datetime.now(UTC))
    assert nav.as_of > original.question_time
    identity = register(environment, nav)
    service = InvestorOrchestrationService(store)
    result = service.execute_current_registered(original, artifacts={"PAPER": (identity,)})
    assert result.original_request.request_id == original.request_id
    assert result.original_request.question_time == original.question_time
    assert result.decision_request.request_id != original.request_id
    assert result.decision_request.question_time == original.question_time
    assert result.decision_request.evidence_cutoff >= nav.as_of
    assert result.preflight.as_of == result.decision_request.evidence_cutoff
    assert result.coverage.coverage_complete
    assert result.coverage.outputs_verified
    assert result.original_request.raw_text == result.decision_request.raw_text


def test_historical_execution_does_not_silently_adopt_current_time(environment: Any) -> None:
    store, _, ledger = environment
    original = request()
    # Separate the Windows wall-clock ticks; evidence must actually be later.
    import time

    time.sleep(0.02)
    nav = ledger.portfolio_nav("paper", as_of=datetime.now(UTC))
    assert nav.as_of > original.question_time
    identity = register(environment, nav)
    service = InvestorOrchestrationService(store)
    _, _, coverage = service.execute_registered(original, artifacts={"PAPER": (identity,)})
    assert not coverage.coverage_complete
    with pytest.raises(ValueError, match="historical|current|mode"):
        service.execute_current_registered(
            original.model_copy(update={"metadata": {"analysis_mode": "HISTORICAL"}}),
            artifacts={"PAPER": (identity,)},
        )


def test_current_freeze_cannot_upgrade_a_read_into_economic_writes(environment: Any) -> None:
    store, _, _ = environment
    service = InvestorOrchestrationService(store)
    original = request(
        normalized_intent=RequestIntent.ACCOUNT_FACT_WRITE, side_effect=SideEffectClass.EA_WRITE
    )
    with pytest.raises(ValueError, match="read|economic|write"):
        service.execute_current_registered(original, artifacts={})
