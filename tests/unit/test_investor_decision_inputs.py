"""Current-decision freeze tests using actual isolated ledger outputs.

No live acquisition, external notification or economic handler is invoked here.
The tests consume the canonical DecisionFreezeService, not a second clock model.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.capabilities import CapabilityExecutionResult
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading.ledger import LedgerService


@pytest.fixture
def context(tmp_path: Path):
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(tmp_path / "objects" / "sha256")
    ledger = LedgerService(state, objects)
    ledger.initialize_account("paper-freeze", 1234500)
    request_id = f"source-request:{uuid.uuid4()}"
    request = InvestorRequestEnvelope(
        request_id=request_id,
        idempotency_key=request_id,
        question_time=datetime.now(UTC),
        user_timezone="Asia/Shanghai",
        raw_text="查看模拟账户资金",
        normalized_intent=RequestIntent.PAPER_STATUS,
        side_effect=SideEffectClass.READ,
        account_id="paper-freeze",
    )
    # Separate Windows wall-clock ticks; this is not a performance measurement.
    time.sleep(0.02)
    nav = ledger.portfolio_nav("paper-freeze", as_of=datetime.now(UTC))
    assert nav.as_of > request.question_time
    ref = objects.put_json(nav.model_dump(mode="json"))
    artifact_id = f"PortfolioNAV:{uuid.uuid4()}"
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="PortfolioNAV",
        schema_version=nav.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return store, state, request, artifact_id, nav


def test_post_question_output_requires_authenticated_current_freeze(context) -> None:
    store, _, request, artifact_id, nav = context
    service = InvestorOrchestrationService(store)
    _, _, original_coverage = service.execute(
        request,
        handlers={
            "PAPER": lambda *_: CapabilityExecutionResult(artifact_ids=(artifact_id,)),
        },
    )
    assert not original_coverage.coverage_complete
    frozen = service.freeze_current_request(request, artifact_ids=(artifact_id,))
    assert frozen.question_time == request.question_time
    assert frozen.parent_request_id == request.request_id
    assert frozen.decision_time is not None and frozen.decision_time >= nav.as_of
    assert frozen.request_id != request.request_id
    preflight, _, coverage = service.execute_registered_inputs(frozen, {"PAPER": (artifact_id,)})
    assert preflight.as_of == frozen.evidence_cutoff
    assert coverage.coverage_complete, coverage.unresolved_conflicts
    assert coverage.request_id == frozen.request_id


def test_frozen_input_replay_is_stable_and_has_no_economic_writes(context) -> None:
    store, state, request, artifact_id, _ = context
    service = InvestorOrchestrationService(store)
    with store.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM journal ORDER BY seq")]
    first = service.freeze_current_request(request, artifact_ids=(artifact_id,))
    second = service.freeze_current_request(request, artifact_ids=(artifact_id,))
    assert first == second
    executed = service.execute_registered_inputs(first, {"PAPER": (artifact_id,)})
    restarted = InvestorOrchestrationService(InvestorOrchestrationStore(store.path))
    resumed = restarted.execute_registered_inputs(second, {"PAPER": (artifact_id,)})
    assert resumed == executed
    with store.connect() as connection:
        after = [tuple(row) for row in connection.execute("SELECT * FROM journal ORDER BY seq")]
    assert after == before
    assert state.integrity_check() == "ok"


def test_current_freeze_cannot_authorize_economic_writes(context) -> None:
    store, _, request, artifact_id, _ = context
    service = InvestorOrchestrationService(store)
    for intent, permission in (
        (RequestIntent.PAPER_CONFIRM, SideEffectClass.PT_CONFIRM),
        (RequestIntent.ACCOUNT_FACT_WRITE, SideEffectClass.EA_WRITE),
    ):
        changed = request.model_copy(
            update={"normalized_intent": intent, "side_effect": permission}
        )
        with pytest.raises(ValueError, match="read-only|economic"):
            service.freeze_current_request(changed, artifact_ids=(artifact_id,))
        with pytest.raises(ValueError, match="read-only|economic"):
            service.execute_registered_inputs(changed, {"PAPER": (artifact_id,)})


def test_source_request_identity_cannot_be_rebound_to_different_text(context) -> None:
    store, _, request, artifact_id, _ = context
    service = InvestorOrchestrationService(store)
    service.freeze_current_request(request, artifact_ids=(artifact_id,))
    altered = request.model_copy(update={"raw_text": "另一个未经批准的请求"})
    with pytest.raises(ValueError, match="collision|identity|different"):
        service.freeze_current_request(altered, artifact_ids=(artifact_id,))


def test_current_inputs_reject_missing_sources_and_builtin_overrides(context) -> None:
    store, _, request, artifact_id, _ = context
    service = InvestorOrchestrationService(store)
    with pytest.raises(ValueError, match="registered|unavailable"):
        service.freeze_current_request(request, artifact_ids=("unregistered-result",))
    frozen = service.freeze_current_request(request, artifact_ids=(artifact_id,))
    with pytest.raises(ValueError, match="built-in|boundary"):
        service.execute_registered_inputs(frozen, {"SESSION_PREFLIGHT": (artifact_id,)})


def test_valid_but_unfrozen_artifact_cannot_replace_a_frozen_input(context) -> None:
    store, state, request, artifact_id, nav = context
    # The second identity is otherwise valid and available before the freeze.
    original = state.artifact_record(artifact_id)
    assert original is not None
    state.register_artifact(
        artifact_id="PortfolioNAV:not-in-freeze",
        artifact_type="PortfolioNAV",
        schema_version=nav.schema_version,
        object_hash=original["object_hash"],
        input_hashes=[],
    )
    service = InvestorOrchestrationService(store)
    frozen = service.freeze_current_request(request, artifact_ids=(artifact_id,))
    _, _, coverage = service.execute_registered_inputs(
        frozen, {"PAPER": ("PortfolioNAV:not-in-freeze",)}
    )
    assert not coverage.coverage_complete
    assert any("not bound to the frozen" in reason for reason in coverage.unresolved_conflicts)


def test_historical_request_cannot_be_advanced_by_current_freeze(context) -> None:
    store, _, request, artifact_id, _ = context
    historical = request.model_copy(update={"research_mode": "HISTORICAL"})
    with pytest.raises(ValueError, match="CURRENT"):
        InvestorOrchestrationService(store).freeze_current_request(
            historical, artifact_ids=(artifact_id,)
        )


def test_registered_execution_rejects_extra_prohibited_inputs_before_issuing_coverage(
    context,
) -> None:
    store, _, request, artifact_id, _ = context
    service = InvestorOrchestrationService(store)
    frozen = service.freeze_current_request(request, artifact_ids=(artifact_id,))
    with pytest.raises(ValueError, match="prohibited|not selected"):
        service.execute_registered_inputs(
            frozen,
            {
                "PAPER": (artifact_id,),
                "EXTERNAL_ACCOUNT": (artifact_id,),
            },
        )
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM capability_coverage_receipts WHERE request_id=?",
                (frozen.request_id,),
            ).fetchone()[0]
            == 0
        )
