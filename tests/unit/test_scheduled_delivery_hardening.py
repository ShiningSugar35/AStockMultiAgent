"""Real SQLite outbox/restart tests with recorded analyzer and receiver boundaries.

No production accounts or external notification endpoint are used. These tests
prove persistence/authorization behavior, not semantic research or live delivery.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
import yaml

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.investor_orchestration.models import (
    MaterialChangeDigest,
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ScheduledWindow,
)
from astock.investor_orchestration.notification_delivery import DurableNotificationOutbox
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import ScheduledResearchService, policy_from_config
from astock.investor_orchestration.scheduled_input_coverage import ScheduledRunSourceCoverage
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
from astock.schemas.market import Market

ROOT = Path(__file__).resolve().parents[2]


def _message(outbox: DurableNotificationOutbox, key: str) -> dict[str, Any]:
    result = outbox.get(key)
    assert result is not None, "the required persisted notification is missing"
    return result


class RecordedAnalyzer:
    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[Any] = []

    def __call__(
        self,
        *,
        domain: ScheduledDomain,
        instrument_id: str,
        window: ScheduledWindow,
        preflight: Any,
        run_id: str,
    ) -> MaterialChangeDigest:
        self.calls += 1
        self.seen.append(preflight)
        return MaterialChangeDigest(
            digest_id=f"recorded-digest-{run_id}-{domain.value}",
            run_id=run_id,
            domain=domain,
            instrument_id=instrument_id,
            as_of=preflight.as_of,
            severity="CRITICAL",
            change_summary="已收到新的重大事项公告，需要核验对经营的影响。",
            impact_summary="请复核原持仓依据，所有真实交易均由用户自行决定。",
            action="EXIT_REVIEW",
            source_artifact_ids=("recorded-analyzer-boundary",),
        )


class RecordedReceiver:
    def __init__(self, *, fail_first: bool = False, delay: float = 0) -> None:
        self.fail_first = fail_first
        self.delay = delay
        self.calls = 0
        self.keys: list[str] = []
        self._lock = threading.Lock()

    def publish(self, *, notification_key: str, title: str, body: str, metadata: Any) -> str:
        with self._lock:
            self.calls += 1
            self.keys.append(notification_key)
            fail = self.fail_first and self.calls == 1
        if fail:
            raise RuntimeError("recorded receiver temporarily unavailable")
        if self.delay:
            time.sleep(self.delay)
        return f"recorded-delivery:{notification_key}"


def _prime_delivery_gate(service: ScheduledResearchService, request: ScheduledRunRequest) -> None:
    """Isolate delivery semantics from the independently tested source/semantic gates."""
    service.source_audit.bind_run = Mock(
        return_value=ScheduledRunSourceCoverage(
            report_ids=request.source_coverage_artifact_ids,
            expected_subject_count=1,
            verified_subject_count=1,
            all_families_checked=True,
            degradation_reasons=(),
            checked_through=request.source_interval_end,
        )
    )
    service.source_audit.register_run_coverage = Mock(
        return_value=SimpleNamespace(
            receipt_id="recorded-delivery-capability",
            semantic_coverage_complete=True,
        )
    )


@pytest.fixture
def context(
    tmp_path: Path,
) -> tuple[InvestorOrchestrationStore, ScheduledRunRequest, RecordedAnalyzer]:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    state = StateStore(store.path)
    account = ExternalAccountRepository(state, ObjectStore(tmp_path / "objects"))
    at = datetime.now(UTC) - timedelta(seconds=5)
    account.create_account(account_id="actual", display_name="test actual", created_at=at)
    account.append_drafts(
        [
            ExternalAccountEventDraft(
                account_id="actual",
                event_type=ExternalAccountEventType.TRADE,
                occurred_at=at,
                available_to_system_at=at,
                created_at=at,
                market=Market.XSHG,
                symbol="600519",
                side="BUY",
                quantity=100,
                price_cny=Decimal("10"),
                idempotency_key="recorded-actual-trade",
            )
        ]
    )
    policy = policy_from_config(
        yaml.safe_load(
            (ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
        )
    )
    analyzer = RecordedAnalyzer()
    service = ScheduledResearchService(
        store, InvestorSessionPreflightService(store), analyzer=analyzer
    )
    service.register_policy(policy)
    service.register_binding(
        ScheduledTaskBinding(
            binding_id="recorded-binding",
            creation_mode="LOCAL_ONLY",
            execution_surface="LOCAL_DAEMON",
            schedule_expression="MANUAL_RECORDED_TEST",
            timezone="Asia/Shanghai",
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            confirmed_at=at,
            consent_hash="recorded-test-consent",
        )
    )
    requested_at = datetime.now(UTC)
    request = ScheduledRunRequest(
        run_id="recorded-run",
        binding_id="recorded-binding",
        schedule_bucket="recorded-bucket",
        window=ScheduledWindow.POST_CLOSE,
        domains=(ScheduledDomain.ACTUAL_HOLDING,),
        requested_at=requested_at,
        source_revision_set={},
        policy_hash=policy.policy_hash,
        idempotency_key="recorded-bucket",
        source_coverage_artifact_ids=("recorded-delivery-source",),
        source_interval_start=requested_at - timedelta(hours=1),
        source_interval_end=requested_at - timedelta(seconds=1),
    )
    return store, request, analyzer


def test_receiver_failure_retries_after_restart_without_reanalyzing(context: Any) -> None:
    store, request, analyzer = context
    receiver = RecordedReceiver(fail_first=True)
    service = ScheduledResearchService(
        store, InvestorSessionPreflightService(store), analyzer=analyzer, notification_sink=receiver
    )
    _prime_delivery_gate(service, request)
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        service.run(request)
    persisted = store.get_scheduled_receipt(idempotency_key=request.idempotency_key)
    assert persisted is not None and persisted.notification_required
    key = f"scheduled:{request.binding_id}:{request.schedule_bucket}"
    assert _message(service.outbox, key)["status"] == "PENDING"
    restarted_store = InvestorOrchestrationStore(store.path)
    forbidden_analyzer = Mock(side_effect=AssertionError("must not repeat semantic work"))
    restarted = ScheduledResearchService(
        restarted_store,
        InvestorSessionPreflightService(restarted_store),
        analyzer=forbidden_analyzer,
        notification_sink=receiver,
    )
    assert restarted.run(request) == persisted
    assert _message(restarted.outbox, key)["status"] == "SENT"
    assert _message(restarted.outbox, key)["attempts"] == 2
    assert receiver.calls == 2 and len(set(receiver.keys)) == 1
    assert analyzer.calls == 1
    forbidden_analyzer.assert_not_called()
    restarted.run(request)
    assert receiver.calls == 2
    assert restarted.outbox.list_pending() == ()


def test_outbox_failure_rolls_back_run_receipt_and_digest(context: Any) -> None:
    store, request, analyzer = context
    service = ScheduledResearchService(
        store, InvestorSessionPreflightService(store), analyzer=analyzer
    )
    _prime_delivery_gate(service, request)
    with patch.object(service.outbox, "publish", side_effect=RuntimeError("recorded disk failure")):
        with pytest.raises(RuntimeError, match="disk failure"):
            service.run(request)
    assert store.get_scheduled_receipt(idempotency_key=request.idempotency_key) is None
    assert service.outbox.list_pending() == ()
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM material_change_digests").fetchone()[0] == 0
    receipt = service.run(request)
    assert receipt.notification_required
    assert len(service.outbox.list_pending()) == 1


def test_queueing_locally_is_not_marked_as_external_delivery(context: Any) -> None:
    store, request, analyzer = context
    outbox = DurableNotificationOutbox(store)
    service = ScheduledResearchService(
        store, InvestorSessionPreflightService(store), analyzer=analyzer, notification_sink=outbox
    )
    _prime_delivery_gate(service, request)
    receipt = service.run(request)
    assert receipt.notification_required
    pending = outbox.list_pending()
    assert len(pending) == 1 and pending[0]["status"] == "PENDING"
    assert pending[0]["attempts"] == 0


def test_outbox_rejects_idempotency_key_content_conflict(context: Any) -> None:
    store, _, _ = context
    outbox = DurableNotificationOutbox(store)
    first = outbox.publish(notification_key="same-key", title="提醒", body="原始内容", metadata={})
    assert (
        outbox.publish(notification_key="same-key", title="提醒", body="原始内容", metadata={})
        == first
    )
    with pytest.raises(ValueError, match="different content"):
        outbox.publish(notification_key="same-key", title="提醒", body="改写内容", metadata={})
    assert _message(outbox, "same-key")["body"] == "原始内容"


def test_only_one_receiver_call_owns_a_concurrent_delivery(context: Any) -> None:
    store, _, _ = context
    DurableNotificationOutbox(store).publish(
        notification_key="parallel", title="提醒", body="复核", metadata={}
    )
    receiver = RecordedReceiver(delay=0.05)

    def attempt(_: int) -> str | None:
        local = DurableNotificationOutbox(InvestorOrchestrationStore(store.path))
        return local.deliver("parallel", receiver)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(attempt, range(8)))
    assert receiver.calls == 1
    assert _message(DurableNotificationOutbox(store), "parallel")["status"] == "SENT"


def test_abandoned_delivery_lease_is_recoverable(context: Any) -> None:
    store, _, _ = context
    outbox = DurableNotificationOutbox(store)
    notification_id = outbox.publish(
        notification_key="abandoned", title="提醒", body="复核", metadata={}
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE scheduled_notification_delivery SET status='SENDING',owner_id='crashed',"
            "claim_expires_at=? WHERE notification_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), notification_id),
        )
    receiver = RecordedReceiver()
    assert outbox.deliver("abandoned", receiver) is not None
    assert receiver.calls == 1 and _message(outbox, "abandoned")["status"] == "SENT"


def test_cloud_binding_cannot_read_local_account_state(context: Any) -> None:
    store, request, analyzer = context
    binding = store.get_binding(request.binding_id)
    preflight_stub = Mock()
    service = ScheduledResearchService(store, preflight_stub, analyzer=analyzer)
    service.register_binding(
        binding.model_copy(update={"binding_id": "cloud", "execution_surface": "WEB_CLOUD"})
    )
    cloud_request = request.model_copy(
        update={
            "run_id": "cloud-run",
            "binding_id": "cloud",
            "idempotency_key": "cloud-bucket",
        }
    )
    receipt = service.run(cloud_request)
    assert receipt.outcome.value == "BLOCKED"
    preflight_stub.build.assert_not_called()
    assert analyzer.calls == 0


def test_no_consent_cannot_start_scheduled_account_analysis(context: Any) -> None:
    store, request, analyzer = context
    binding = store.get_binding(request.binding_id)
    preflight_stub = Mock()
    service = ScheduledResearchService(store, preflight_stub, analyzer=analyzer)
    service.register_binding(
        binding.model_copy(
            update={"binding_id": "unapproved", "confirmed_at": None, "consent_hash": None}
        )
    )
    unapproved = request.model_copy(
        update={
            "run_id": "unapproved-run",
            "binding_id": "unapproved",
            "idempotency_key": "unapproved",
        }
    )
    receipt = service.run(unapproved)
    assert receipt.outcome.value == "BLOCKED"
    preflight_stub.build.assert_not_called()
    assert analyzer.calls == 0


def test_minimum_semantic_context_does_not_disclose_account_cash_or_cost(context: Any) -> None:
    store, request, analyzer = context
    service = ScheduledResearchService(
        store, InvestorSessionPreflightService(store), analyzer=analyzer
    )
    _prime_delivery_gate(service, request)
    service.run(request)
    seen = analyzer.seen[0]
    assert not hasattr(seen.context, "actual"), "raw actual account context was disclosed"
    assert not hasattr(seen.context, "paper"), "raw paper account context was disclosed"


def test_schedule_owns_the_bucket_before_semantic_work(context: Any) -> None:
    store, request, analyzer = context
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    counter = threading.Lock()

    def slow_analysis(**kwargs: Any) -> MaterialChangeDigest:
        nonlocal calls
        with counter:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(10), "the test failed to release its recorded analyzer"
        return analyzer(**kwargs)

    first_store = InvestorOrchestrationStore(store.path)
    second_store = InvestorOrchestrationStore(store.path)
    first = ScheduledResearchService(
        first_store, InvestorSessionPreflightService(first_store), analyzer=slow_analysis
    )
    second = ScheduledResearchService(
        second_store, InvestorSessionPreflightService(second_store), analyzer=slow_analysis
    )
    _prime_delivery_gate(first, request)
    _prime_delivery_gate(second, request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.run, request)
        try:
            assert entered.wait(10), "first worker did not reach the recorded analysis boundary"
            with pytest.raises(RuntimeError, match="already in progress"):
                second.run(request)
        finally:
            release.set()
        receipt = future.result(timeout=10)
    assert calls == 1
    assert second.run(request) == receipt
    assert calls == 1


def test_missing_analysis_result_cannot_certify_no_change_or_advance_watermark(
    context: Any,
) -> None:
    store, request, _ = context
    service = ScheduledResearchService(
        store, InvestorSessionPreflightService(store), analyzer=lambda **_: None
    )
    _prime_delivery_gate(service, request)
    receipt = service.run(request)
    assert receipt.outcome.value == "DEGRADED"
    assert receipt.next_watermark is None
    assert receipt.degradation_reasons
