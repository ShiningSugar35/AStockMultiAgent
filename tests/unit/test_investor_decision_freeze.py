"""Current requests retain question time while freezing evidence after acquisition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
def acquired(tmp_path: Path):
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    objects = ObjectStore(tmp_path / "objects" / "sha256")
    ledger = LedgerService(state)
    ledger.initialize_account("paper", 100000)
    request = InvestorRequestEnvelope(
        request_id="current-question",
        question_time=datetime.now(UTC),
        user_timezone="Asia/Shanghai",
        raw_text="查看模拟账户资金",
        normalized_intent=RequestIntent.PAPER_STATUS,
        side_effect=SideEffectClass.READ,
        account_id="paper",
        idempotency_key="current-question",
    )
    service = InvestorOrchestrationService(store)
    initial_preflight, _ = service.prepare(request)
    nav = ledger.portfolio_nav("paper", as_of=datetime.now(UTC))
    assert nav.as_of > request.question_time
    reference = objects.put_json(nav.model_dump(mode="json"))
    state.register_artifact(
        artifact_id="PortfolioNAV:after-acquisition",
        artifact_type="PortfolioNAV",
        schema_version=nav.schema_version,
        object_hash=reference.sha256,
        input_hashes=[],
    )
    return service, request, nav, initial_preflight, state, objects


def test_current_freeze_preserves_original_question_and_accepts_new_evidence(acquired) -> None:
    service, request, nav, initial, _, _ = acquired
    frozen = service.freeze_current_request(
        request, artifact_ids=("PortfolioNAV:after-acquisition",)
    )
    assert frozen.question_time == request.question_time
    assert frozen.parent_request_id == request.request_id
    assert frozen.request_id != request.request_id
    assert frozen.decision_time >= nav.as_of
    preflight, _, coverage = service.execute(
        frozen,
        handlers={
            "PAPER": lambda *_: CapabilityExecutionResult(
                artifact_ids=("PortfolioNAV:after-acquisition",)
            )
        },
    )
    assert preflight.as_of == frozen.decision_time
    assert initial.as_of == request.question_time
    assert coverage.coverage_complete, coverage.unresolved_conflicts
    assert service.store.get_preflight_for_request(request.request_id) == initial


def test_freezing_same_acquisition_is_idempotent(acquired) -> None:
    service, request, _, _, _, _ = acquired
    first = service.freeze_current_request(
        request, artifact_ids=("PortfolioNAV:after-acquisition",)
    )
    second = service.freeze_current_request(
        request, artifact_ids=("PortfolioNAV:after-acquisition",)
    )
    assert first == second


def test_unknown_capture_cannot_be_frozen_into_formal_decision(acquired) -> None:
    service, request, _, _, _, _ = acquired
    with pytest.raises(ValueError, match="registered|unavailable"):
        service.freeze_current_request(request, artifact_ids=("never-captured",))


def test_altering_frozen_cutoff_is_detected_before_preflight(acquired) -> None:
    service, request, _, _, _, _ = acquired
    frozen = service.freeze_current_request(
        request, artifact_ids=("PortfolioNAV:after-acquisition",)
    )
    changed = frozen.model_copy(
        update={"decision_time": frozen.decision_time + timedelta(microseconds=1)}
    )
    with pytest.raises(ValueError, match="freeze|frozen"):
        service.prepare(changed)


def test_historical_requests_cannot_opt_into_current_acquisition(acquired) -> None:
    service, request, _, _, _, _ = acquired
    historical = request.model_copy(update={"research_mode": "HISTORICAL"})
    with pytest.raises(ValueError, match="historical|CURRENT"):
        service.freeze_current_request(historical, artifact_ids=("PortfolioNAV:after-acquisition",))
