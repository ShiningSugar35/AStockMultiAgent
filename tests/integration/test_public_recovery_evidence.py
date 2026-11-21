"""Bounded actual response evidence controls closure; budgets and caller booleans do not."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from astock.core.hashing import content_hash
from astock.investor_orchestration.closure import (
    InvestmentClosureState,
    InvestmentRequestClosurePolicy,
)
from astock.investor_orchestration.gateway import InvestorAnswerGateway
from astock.investor_orchestration.service import InvestorOrchestrationService
from astock.research.recovery_evidence import build_public_recovery_coverage
from astock.research.web_tools import WebToolExecutionService
from astock.schemas.evidence import FetchStatus, SourceSnapshot
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CapabilityScheduleStep,
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    CurrentResearchSchedule,
)
from astock.schemas.research_continuation import CurrentResearchContinuationStatus
from astock.schemas.web_tools import (
    HostWebCapabilities,
    HostWebResult,
    HostWebTool,
    WebToolHit,
    WebToolRequest,
)
from tests.unit.test_current_research_continuation import NOW, PROJECT_ROOT, _request, _service
from tests.unit.test_investment_request_closure import _coverage, _plan
from tests.unit.test_investment_request_closure import _request as investor_request


def _registered(state, objects, identity, kind, payload, inputs=()):
    ref = objects.put_json(payload)
    state.register_artifact(
        artifact_id=identity, artifact_type=kind, schema_version=payload["schema_version"],
        object_hash=ref.sha256, input_hashes=list(inputs),
    )
    return ref.sha256


def recorded_route(
    tmp_path, *, omitted_provider=False, provider_failure=None, native_failure=False,
):
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    record = service.start(_request(request_id="registered-bounded-recovery"))
    at = NOW + timedelta(seconds=31)
    raw = objects.put_json({
        "code": 0, "success": not native_failure,
        "data": {"fields": ["value"], "items": []},
    })
    snapshot = SourceSnapshot(
        snapshot_id="empty-recorded-response:" + raw.sha256, source_id="recorded-json-source",
        object_sha256=raw.sha256, byte_size=raw.byte_size, fetched_at=at,
        available_to_system_at=at, created_at=at, fetch_status=FetchStatus.SUCCEEDED,
        mime="application/json",
    )
    state.register_snapshot(snapshot)
    providers = ["recorded-json-source", *(["other-source"] if omitted_provider else [])]
    step = CapabilityScheduleStep(
        capability=AcquisitionCapability.FINANCIAL_ANNUAL, stage=0, core=True,
        provider_candidates=providers,
        preferred_authorities=record.external_tasks[0].preferred_authorities,
        created_at=at,
    )
    schedule = CurrentResearchSchedule(
        schedule_id="recorded-schedule:" + content_hash(providers), policy_version="recorded",
        policy_hash=service.policy.policy_hash, company_id=record.company_id, market=record.market,
        lookback_days=120, max_workers=1, automatic_resolution_budget_seconds=1800,
        steps=[step], created_at=at,
    )
    schedule_hash = _registered(
        state, objects, schedule.schedule_id, "CurrentResearchSchedule",
        schedule.model_dump(mode="json"),
    )
    attempt = AcquisitionAttempt(
        capability=AcquisitionCapability.FINANCIAL_ANNUAL,
        status=(AcquisitionAttemptStatus.FAILED if provider_failure == "PROVIDER_FAILED"
                else AcquisitionAttemptStatus.PARTIAL),
        provider_path=["recorded-json-source"], record_count=0, latency_ms=1,
        internal_reason_codes=[provider_failure or "NO_RECORDS"],
        source_snapshot_ids=[snapshot.snapshot_id], created_at=at,
    )
    report = CurrentResearchAcquisitionReport(
        report_id="recorded-acquisition:bounded-empty", company_id=record.company_id,
        market=record.market, started_at=at, decision_as_of=at,
        status=CurrentResearchAcquisitionStatus.NEEDS_EXTERNAL_RESEARCH,
        policy_version=schedule.policy_version, policy_hash=schedule.policy_hash,
        schedule_artifact_id=schedule.schedule_id, attempts=[attempt], created_at=at,
    )
    _registered(state, objects, report.report_id, "CurrentResearchAcquisitionReport",
                report.model_dump(mode="json"), (schedule_hash, raw.sha256))
    record = service._persist(record.model_copy(update={
        "acquisition_report_artifact_ids": [
            *record.acquisition_report_artifact_ids, report.report_id,
        ],
        "current_acquisition_report_artifact_id": report.report_id,
    }))
    task = record.external_tasks[0]
    web = WebToolExecutionService(
        PROJECT_ROOT, state, objects, clock=lambda: NOW + timedelta(seconds=32),
    )
    request = WebToolRequest(
        request_id=record.request_id, continuation_id=record.continuation_id, task_id=task.task_id,
        operation="SEARCH", query=task.research_question,
        requested_capability="financial.official_document",
        evidence_scope=f"{record.company_id}:{task.capability.value}:{task.task_id}",
        expected_fact=task.research_question, fresh_after=record.started_at,
    )
    host = HostWebCapabilities(
        host_id="recorded-host", invocation_id="bounded-query", observed_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        search=HostWebTool(availability="AVAILABLE", tool_name="search", billing="INCLUDED"),
    )
    pending = web.run(request, host)
    service.clock = lambda: NOW + timedelta(seconds=33)
    return service, acquisition, web, record, pending, snapshot


def finish_web(web, pending, *, status: Literal["SUCCEEDED", "FAILED"] = "SUCCEEDED", hit=False):
    return web.complete_host(HostWebResult(
        operation_id=pending["operation_id"], dispatch_id=pending["dispatch_id"],
        invocation_id="bounded-query", tool_name="search", status=status,
        observed_at=NOW + timedelta(seconds=32),
        hits=(
            [WebToolHit(url="https://www.sse.com.cn/disclosure/listedinfo/annual.pdf")]
            if hit else []
        ),
    ))


def test_actual_empty_provider_and_host_callback_stop_only_the_bounded_gap(tmp_path, monkeypatch):
    service, acquisition, web, record, pending, _ = recorded_route(tmp_path)
    finish_web(web, pending)
    before = acquisition.calls
    result = service.resume(record.continuation_id)
    assert result.status is CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE
    assert acquisition.calls == before
    assert not result.automatic_budget_exhausted and result.recovery_stop_reason is None
    proof = result.public_recovery_coverage
    assert proof and proof.all_applicable_routes_completed_empty()
    assert {route.route_kind for route in proof.routes} == {"PROVIDER", "WEB_QUERY"}
    assert all(route.classification == "NO_NEW_EVIDENCE" for route in proof.routes)
    assert not proof.whole_public_web_exhausted and not proof.conclusion_authority_allowed
    assert all(route.evidence_artifact_ids and route.source_object_hashes for route in proof.routes)

    request = investor_request().model_copy(update={
        "request_id": record.request_id, "entity_ids": ("XSHG:600519",),
    })
    plan = _plan(request)
    coverage = _coverage(request, plan, missing={"FINANCIAL_INTEGRITY"})
    facade = object.__new__(InvestorOrchestrationService)
    facade.gateway = cast(InvestorAnswerGateway, SimpleNamespace(verifier=SimpleNamespace(
        state=service.state, objects=service.objects,
    )))
    monkeypatch.setattr(
        "astock.investor_orchestration.service.RecommendationFulfillmentService.assess",
        lambda *_: None,
    )
    decision = facade.closure_status(request, plan, coverage)
    assert decision.state is InvestmentClosureState.PUBLIC_DATA_UNAVAILABLE
    assert decision.automatic_resolution_exhausted and decision.public_recovery_evidence
    assert decision.investment_conclusion_blocked and not decision.investor_view_allowed
    wider = request.model_copy(update={"entity_ids": ("XSHG:600519", "XSHE:000001")})
    assert facade.closure_status(wider, _plan(wider), _coverage(
        wider, _plan(wider), missing={"FINANCIAL_INTEGRITY"},
    )).state is InvestmentClosureState.CONTINUE_AUTOMATICALLY


@pytest.mark.parametrize("failure", [
    "unattempted", "host_failed", "unknown", "leads", "corrupt",
    "native_failure", "provider_failed",
])
def test_uncovered_or_unverified_route_never_means_public_absence(tmp_path, failure):
    service, _, web, record, pending, snapshot = recorded_route(
        tmp_path, omitted_provider=failure == "unattempted",
        native_failure=failure == "native_failure",
        provider_failure="PROVIDER_FAILED" if failure == "provider_failed" else None,
    )
    if failure != "unknown":
        finish_web(web, pending, status="FAILED" if failure == "host_failed" else "SUCCEEDED",
                   hit=failure == "leads")
    if failure == "corrupt":
        service.objects.path_for(snapshot.object_sha256).write_bytes(b"corrupt response")
    current = service.get(record.continuation_id)
    assert current
    proof = build_public_recovery_coverage(service.state, service.objects, current)
    assert not proof.all_applicable_routes_completed_empty()
    assert any(route.classification != "NO_NEW_EVIDENCE" for route in proof.routes)
    assert all(route.uncovered_impact for route in proof.routes)


def test_budget_limit_retains_actual_route_coverage_without_claiming_source_exhaustion(tmp_path):
    service, _, _, record, _, _ = recorded_route(tmp_path, provider_failure="TIME_BUDGET_EXHAUSTED")
    service.clock = lambda: record.deadline_at + timedelta(seconds=1)
    stopped = service.resume(record.continuation_id)
    assert stopped.status is CurrentResearchContinuationStatus.BUDGET_EXHAUSTED
    assert stopped.automatic_budget_exhausted and stopped.public_recovery_coverage
    assert not stopped.public_recovery_coverage.all_applicable_routes_completed_empty()
    assert not stopped.investor_view_allowed


def test_boolean_and_foreign_request_cannot_grant_terminal_source_absence(tmp_path):
    request = investor_request().model_copy(update={"entity_ids": ("XSHG:600519",)})
    plan = _plan(request)
    coverage = _coverage(request, plan, missing={"FINANCIAL_INTEGRITY"})
    result = InvestmentRequestClosurePolicy.evaluate(
        request, plan, coverage, automatic_resolution_exhausted=True,
    )
    assert result.state is InvestmentClosureState.CONTINUE_AUTOMATICALLY
    service, _, web, record, pending, _ = recorded_route(tmp_path)
    finish_web(web, pending)
    proof = build_public_recovery_coverage(service.state, service.objects, record)
    with pytest.raises(ValueError, match="another request"):
        InvestmentRequestClosurePolicy.evaluate(
            request, plan, coverage, public_recovery_evidence=(proof,),
        )


def test_old_generation_empty_callback_cannot_cover_new_external_attempt(tmp_path):
    service, _, web, record, pending, _ = recorded_route(tmp_path)
    finish_web(web, pending)
    task = record.external_tasks[0].model_copy(update={
        "dispatch_id": "replacement-scheduler:gen:2",
        "scheduler_run_id": "replacement-scheduler",
    })
    current = record.model_copy(update={"external_tasks": [task]})
    proof = build_public_recovery_coverage(service.state, service.objects, current)
    search = next(route for route in proof.routes if route.route_kind == "WEB_QUERY")
    assert search.classification == "NOT_ATTEMPTED" and search.attempt_count == 0
    assert not proof.all_applicable_routes_completed_empty()
