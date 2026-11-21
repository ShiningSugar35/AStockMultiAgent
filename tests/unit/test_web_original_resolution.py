from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier, Event, Lock

import pymupdf
import pytest

from astock.core.hashing import content_hash
from astock.core.request_budget import RequestBudgetExhausted, current_request_budget
from astock.documents.official_web import OfficialWebDocumentCaptureService
from astock.documents.public_pdf import PublicPdfDownload
from astock.investor_orchestration.run_ownership import ScheduledRunInProgress
from astock.research.web_tool_repository import WebToolOperationRepository, operation_id
from astock.research.web_tools import (
    WebToolExecutionService,
    advance_current_web,
    build_official_resolution_handlers,
    prepare_continuation_web_requests,
)
from astock.schemas.research_acquisition import AcquisitionCapability
from astock.schemas.research_continuation import (
    CurrentResearchContinuationStatus,
    ExternalResearchTaskStatus,
)
from astock.schemas.web_tools import (
    CurrentResearchWebBatch,
    HostWebCapabilities,
    HostWebResult,
    HostWebTool,
    WebOfficialDocumentSelection,
    WebToolHit,
    WebToolRequest,
)
from tests.unit.test_current_research_continuation import NOW, PROJECT_ROOT, _request, _service


def _pending_web(tmp_path, dispatch_id: str | None = None):
    continuation, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    record = continuation.start(_request(request_id="web-original"))
    if dispatch_id is not None:
        task = record.external_tasks[0].model_copy(update={
            "dispatch_id": dispatch_id, "scheduler_run_id": "scheduler:original",
        })
        record = continuation._persist(record.model_copy(update={"external_tasks": [task]}))
    web = WebToolExecutionService(
        PROJECT_ROOT, state, objects, clock=lambda: NOW + timedelta(seconds=30)
    )
    request = WebToolRequest(
        request_id=record.request_id, continuation_id=record.continuation_id,
        task_id=record.external_tasks[0].task_id, operation="SEARCH", query="official report",
        requested_capability="financial.official_document", evidence_scope="company:600519:2025",
        expected_fact="annual financial statements", fresh_after=NOW - timedelta(minutes=1),
    )
    host = HostWebCapabilities(
        host_id="antigravity", invocation_id="i1", observed_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        search=HostWebTool(availability="AVAILABLE", tool_name="search", billing="INCLUDED"),
    )
    view = web.run(request, host)
    return continuation, acquisition, web, record, request, host, view, state, objects


def _host_receipt(view):
    return HostWebResult(
        operation_id=view["operation_id"], dispatch_id=view["dispatch_id"],
        invocation_id="i1", tool_name="search", status="SUCCEEDED",
        observed_at=NOW + timedelta(seconds=30),
        hits=[WebToolHit(
            url="https://www.sse.com.cn/disclosure/listedinfo/report.pdf", title="Annual report",
        )],
    )


def setup(tmp_path):
    continuation, acquisition, web, _record, _request, _host, view, state, objects = _pending_web(
        tmp_path
    )
    response = _host_receipt(view)
    web.complete_host(response)
    selection = WebOfficialDocumentSelection(
        operation_id=view["operation_id"], url=response.hits[0].url,
        title="测试公司2025年年度报告", published_at=NOW - timedelta(days=1),
        document_type="ANNUAL_REPORT", period_end=date(2025, 12, 31),
    )
    return continuation, acquisition, web, selection, state, objects


def test_host_hit_download_capture_and_same_continuation_binding_are_idempotent(
    tmp_path, monkeypatch,
):
    continuation, acquisition, web, selection, state, objects = setup(tmp_path)
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Example annual report source")
        raw = document.tobytes()
    downloads = []
    def fetch(proposal, **kwargs):
        budget = current_request_budget()
        assert budget is not None and budget.remaining_seconds() > 0
        budget.reserve_attempt()
        downloads.append(str(proposal.candidate_url))
        return PublicPdfDownload(raw, selection.url, (selection.url,))
    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", fetch)
    def resolve_once(_):
        try:
            return web.resolve_official_result(selection)
        except ScheduledRunInProgress:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(resolve_once, range(2)))
    resolutions = [item for item in attempts if item is not None]
    assert resolutions and len(downloads) == 1
    resolution = resolutions[0]
    assert resolution.external_operation_id == selection.operation_id
    assert all(item == resolution for item in resolutions)
    assert web.resolve_official_result(selection) == resolution and len(downloads) == 1
    bound = continuation.apply_automatic_resolution(resolution)
    assert bound.external_tasks[0].status is ExternalResearchTaskStatus.EVIDENCE_BOUND
    again = continuation.apply_automatic_resolution(resolution)
    assert again == bound
    resumed = continuation.resume(bound.continuation_id)
    assert resumed.continuation_id == bound.continuation_id and acquisition.calls == 2
    assert not resumed.investor_view_allowed
    capture = state.artifact_record(resolution.capture_artifact_ids[0])
    assert capture and objects.verify(capture["object_hash"])


def test_original_url_must_be_bound_to_same_host_result_before_fetch(tmp_path, monkeypatch):
    _continuation, _acquisition, web, selection, _state, _objects = setup(tmp_path)
    calls = []
    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf",
                        lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="bound to"):
        web.resolve_official_result(selection.model_copy(update={
            "url": "https://www.sse.com.cn/disclosure/listedinfo/different.pdf"}))
    assert calls == []


def test_stale_dispatch_selection_cannot_start_original_download(tmp_path, monkeypatch):
    _continuation, _acquisition, web, selection, _state, _objects = setup(tmp_path)
    downloads = []
    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf",
                        lambda *args, **kwargs: downloads.append(args))
    with pytest.raises(ValueError, match="dispatch"):
        web.resolve_official_result(
            selection.model_copy(update={"dispatch_id": "stale-generation"})
        )
    assert downloads == []


def test_frozen_raw_after_interruption_finishes_locally_after_request_budget(tmp_path, monkeypatch):
    continuation, acquisition, web, selection, _state, _objects = setup(tmp_path)
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Frozen annual original")
        raw = document.tobytes()
    downloads = []
    def fetch(proposal, **kwargs):
        budget = current_request_budget()
        assert budget is not None
        budget.reserve_attempt()
        downloads.append(str(proposal.candidate_url))
        return PublicPdfDownload(raw, selection.url, (selection.url,))
    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", fetch)
    original_capture = OfficialWebDocumentCaptureService.capture
    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted after raw receipt")
    monkeypatch.setattr(OfficialWebDocumentCaptureService, "capture", interrupted)
    with pytest.raises(RuntimeError, match="after raw"):
        web.resolve_official_result(selection)
    monkeypatch.setattr(OfficialWebDocumentCaptureService, "capture", original_capture)
    operation = web.repository.get(selection.operation_id)
    assert operation is not None
    record = continuation.get(operation["request"]["continuation_id"])
    assert record is not None
    after_deadline = record.deadline_at + timedelta(seconds=1)
    continuation.clock = web.clock = lambda: after_deadline
    terminal = continuation.resume(record.continuation_id)
    assert terminal.status.value == "BUDGET_EXHAUSTED"
    resolution = web.resolve_official_result(selection)
    assert len(downloads) == 1
    bound = continuation.apply_automatic_resolution(resolution)
    resumed = continuation.resume(bound.continuation_id)
    assert resumed.continuation_id == record.continuation_id
    assert acquisition.network_modes == [True, False]
    assert not resumed.investor_view_allowed


def test_unknown_branch_allows_independent_web_but_not_unknown_or_dependent_task(tmp_path):
    continuation, _, web, record, request, host, _view, _, _ = _pending_web(
        tmp_path, "scheduler:annual:gen:0"
    )
    unknown = record.external_tasks[0].model_copy(update={
        "last_failure_code": "EXTERNAL_COMPLETION_UNKNOWN",
    })
    independent = unknown.model_copy(update={
        "task_id": unknown.task_id + ":interim",
        "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        "dispatch_id": "scheduler:interim:gen:0", "scheduler_run_id": "scheduler:interim",
        "last_failure_code": None,
    })
    record = continuation._persist(record.model_copy(update={
        "status": CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        "external_tasks": [unknown, independent],
    }))
    independent_request = request.model_copy(update={"task_id": independent.task_id})
    result = web.run(independent_request, host)
    assert result["status"] == "WAITING_HOST"
    cursor = web.repository.get(result["operation_id"])
    assert cursor is not None
    assert cursor["request"]["continuation_dispatch_id"] == independent.dispatch_id
    with pytest.raises(ValueError, match="cannot be redispatched"):
        web.run(request, host)
    with pytest.raises(ValueError, match="dispatch generation"):
        web.run(independent_request.model_copy(update={
            "continuation_dispatch_id": "scheduler:interim:gen:old",
        }), host)
    dependency = unknown.model_copy(update={
        "capability": AcquisitionCapability.INSTRUMENT_IDENTITY,
    })
    continuation._persist(record.model_copy(update={"external_tasks": [dependency, independent]}))
    with pytest.raises(ValueError, match="independent public evidence"):
        web.run(independent_request, host)


@pytest.mark.parametrize("late", [False, True])
def test_unknown_matching_host_completion_continues_original_with_same_budget(
    tmp_path, monkeypatch, late,
):
    continuation, acquisition, web, record, _, _, view, _, _ = _pending_web(
        tmp_path, "scheduler:annual:gen:0"
    )
    task = record.external_tasks[0].model_copy(update={
        "last_failure_code": "EXTERNAL_COMPLETION_UNKNOWN",
    })
    continuation._persist(record.model_copy(update={
        "status": CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        "external_tasks": [task],
    }))
    if late:
        web.clock = lambda: record.deadline_at + timedelta(seconds=1)
    response = _host_receipt(view)
    assert web.complete_host(response)["status"] == "SUCCEEDED"
    assert web.complete_host(response)["status"] == "SUCCEEDED"
    with pytest.raises(ValueError, match="does not match"):
        web.complete_host(response.model_copy(update={"hits": [WebToolHit(
            url=response.hits[0].url, title="conflicting callback",
        )]}))
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Recovered annual original")
        raw = document.tobytes()
    downloads = []
    def fetch(proposal, **kwargs):
        budget = current_request_budget()
        assert budget is not None
        budget.reserve_attempt()
        downloads.append(str(proposal.candidate_url))
        return PublicPdfDownload(raw, response.hits[0].url, (response.hits[0].url,))
    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", fetch)
    selection = WebOfficialDocumentSelection(
        operation_id=view["operation_id"], dispatch_id=task.dispatch_id,
        url=response.hits[0].url, title="测试公司2025年年度报告",
        published_at=NOW - timedelta(days=1), document_type="ANNUAL_REPORT",
        period_end=date(2025, 12, 31),
    )
    if late:
        with pytest.raises(RequestBudgetExhausted):
            web.resolve_official_result(selection)
        assert downloads == []
    else:
        resolution = web.resolve_official_result(selection)
        assert resolution.dispatch_id == task.dispatch_id
        assert resolution.external_operation_id == view["operation_id"]
        bound = continuation.apply_automatic_resolution(resolution)
        assert bound.external_tasks[0].status is ExternalResearchTaskStatus.EVIDENCE_BOUND
        resumed = continuation.resume(bound.continuation_id)
        assert resumed.continuation_id == record.continuation_id and acquisition.calls == 2
        assert len(downloads) == 1 and not resumed.investor_view_allowed


@pytest.mark.parametrize("old_dispatch", [None, "scheduler:annual:gen:0"])
def test_old_web_generation_cannot_be_rebound_by_callback_or_new_selection(
    tmp_path, monkeypatch, old_dispatch,
):
    continuation, _, web, record, request, host, view, _, _ = _pending_web(
        tmp_path, old_dispatch
    )
    expected_request = request.model_copy(update={"continuation_dispatch_id": old_dispatch})
    assert view["operation_id"] == operation_id(expected_request)
    if old_dispatch is None:
        assert view["operation_id"] == content_hash(request.model_dump(
            mode="json", exclude={"continuation_dispatch_id"},
        ))
    response = _host_receipt(view)
    web.complete_host(response)
    task = record.external_tasks[0].model_copy(update={
        "dispatch_id": "scheduler:annual:gen:1", "scheduler_run_id": "scheduler:new-generation",
    })
    continuation._persist(record.model_copy(update={"external_tasks": [task]}))
    with pytest.raises(ValueError, match="dispatch generation"):
        web.complete_host(response)
    downloads = []
    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf",
                        lambda *args, **kwargs: downloads.append(args))
    selection = WebOfficialDocumentSelection(
        operation_id=view["operation_id"], dispatch_id=task.dispatch_id,
        url=response.hits[0].url, title="测试公司2025年年度报告",
        published_at=NOW - timedelta(days=1), document_type="ANNUAL_REPORT",
        period_end=date(2025, 12, 31),
    )
    with pytest.raises(ValueError, match="continuation dispatch"):
        web.resolve_official_result(selection)
    assert downloads == []
    new_view = web.run(request, host)
    assert new_view["operation_id"] != view["operation_id"]
    assert new_view["status"] == "WAITING_HOST"



def _owned_original_batch(tmp_path, *, expired=False, bad_first_pdf=False):
    """Real host receipts and raw objects; the production worker never needs the network."""
    observed = datetime.now(UTC) - timedelta(seconds=120 if expired else 0)
    continuation, acquisition, state, objects = _service(
        tmp_path, gap_rounds=[True, True] if expired else [True, False], clock=observed,
    )
    record = continuation.start(_request(
        request_id="owned-original-batch", budget_seconds=60 if expired else 1800,
    ))
    annual = record.external_tasks[0]
    interim = annual.model_copy(update={
        "task_id": annual.task_id + ":interim",
        "capability": AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        "research_question": "取得600519的2026年第三季度报告。",
    })
    continuation._persist(record.model_copy(update={"external_tasks": [annual, interim]}))
    record = continuation.prepare_external_wave(record.continuation_id)
    web = WebToolExecutionService(PROJECT_ROOT, state, objects, clock=lambda: observed)
    host = HostWebCapabilities(
        host_id="test-host", invocation_id="owned-batch", observed_at=observed,
        expires_at=observed + timedelta(minutes=5),
        search=HostWebTool(availability="AVAILABLE", tool_name="search", billing="INCLUDED"),
    )
    responses, originals = [], []
    requests = prepare_continuation_web_requests(record, root=PROJECT_ROOT)
    for index, request in enumerate(requests):
        view = web.run(request, host)
        url = f"https://www.sse.com.cn/disclosure/listedinfo/report-{index}.pdf"
        response = HostWebResult(
            operation_id=view["operation_id"], dispatch_id=view["dispatch_id"],
            invocation_id=host.invocation_id, tool_name="search", status="SUCCEEDED",
            observed_at=observed, hits=[WebToolHit(url=url, title="Official report")],
        )
        web.complete_host(response)
        is_annual = request.task_id == annual.task_id
        selection = WebOfficialDocumentSelection(
            operation_id=view["operation_id"], dispatch_id=request.continuation_dispatch_id,
            url=url, title="600519 2025年年度报告" if is_annual else "600519 2026年第三季度报告",
            published_at=observed - timedelta(days=1),
            document_type="ANNUAL_REPORT" if is_annual else "QUARTERLY_REPORT",
            period_end=date(2025, 12, 31) if is_annual else date(2026, 9, 30),
        )
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Immutable original financial report.")
            raw = pdf.tobytes()
        if index == 0 and bad_first_pdf:
            raw = b"poisoned frozen bytes without a PDF header"
        raw_ref = objects.put_bytes(raw)
        receipt = objects.put_json({
            "raw_object_hash": raw_ref.sha256, "observed_at": observed.isoformat(),
            "original_url": url, "final_url": url, "redirect_chain": [url],
        })
        cursor = web.repository.get(selection.operation_id)
        assert cursor is not None
        state.register_artifact(
            artifact_id=web._official_raw_id(cursor, url), artifact_type="WebOfficialRaw",
            schema_version="web-official-raw-v1", object_hash=receipt.sha256,
            input_hashes=[raw_ref.sha256],
        )
        responses.append(response)
        originals.append(selection)
    if expired:
        continuation.clock = lambda: datetime.now(UTC)
    return continuation, acquisition, web, record, responses, originals, state, objects


def test_production_factory_uses_parallel_owned_workers_and_same_request_resume(
    tmp_path, monkeypatch,
):
    from astock.research import web_tools as module

    continuation, acquisition, _web, record, responses, originals, state, objects = (
        _owned_original_batch(tmp_path)
    )
    rendezvous, lock = Barrier(2), Lock()
    workers = []
    original_call = module._OfficialOriginalHandler.__call__

    def observed_call(self, task):
        with lock:
            workers.append(self.worker)
        rendezvous.wait(timeout=10)
        return original_call(self, task)

    monkeypatch.setattr(module._OfficialOriginalHandler, "__call__", observed_call)
    batch = CurrentResearchWebBatch(
        continuation_id=record.continuation_id, host_results=responses, originals=originals,
    )
    result = advance_current_web(
        continuation.paths, state, objects, record.continuation_id, batch,
        continuation=continuation,
    )
    assert result["status"] == "TEAM_RESEARCH_REQUIRED"
    assert result["continuation_id"] == record.continuation_id
    assert len(workers) == 2 and all(worker.last_pid is not None for worker in workers)
    assert all(worker.cleanup_confirmed and not worker.alive for worker in workers)
    assert acquisition.calls == 2 and not result["investor_view_allowed"]
    assert all(task["capture_artifact_ids"] for task in result["external_tasks"])
    again = advance_current_web(
        continuation.paths, state, objects, record.continuation_id, batch,
        continuation=continuation,
    )
    assert again["continuation_id"] == result["continuation_id"]
    assert len(workers) == 2 and acquisition.calls == 2
    already = [item for item in again["web_operations"] if item["status"] == "ALREADY_APPLIED"]
    assert len(already) == 2


def test_expired_batch_consumes_good_frozen_raw_after_bad_sibling_without_network(
    tmp_path, monkeypatch,
):
    continuation, acquisition, _web, record, _responses, originals, state, objects = (
        _owned_original_batch(tmp_path, expired=True, bad_first_pdf=True)
    )
    def forbidden_dispatch(*args, **kwargs):
        raise AssertionError("expired local recovery cannot dispatch a host or paid operation")

    monkeypatch.setattr(WebToolExecutionService, "run", forbidden_dispatch)
    result = advance_current_web(
        continuation.paths, state, objects, record.continuation_id,
        CurrentResearchWebBatch(continuation_id=record.continuation_id, originals=originals),
        continuation=continuation,
    )
    assert any(item["status"] == "ORIGINAL_FAILED" for item in result["web_operations"])
    current = continuation.get(record.continuation_id)
    assert current is not None and len(current.automatic_resolution_artifact_ids) == 1
    assert acquisition.network_modes == [True, False]
    assert result["status"] == "BUDGET_EXHAUSTED" and not result["investor_view_allowed"]
    from astock.core.request_budget import RequestExecutionBudget
    budget = RequestExecutionBudget(
        state, record.request_id, record.deadline_at, record.max_transport_attempts,
    )
    assert budget.remaining_attempts() == record.max_transport_attempts


def test_local_only_original_without_frozen_bytes_never_downloads(tmp_path, monkeypatch):
    _continuation, _acquisition, web, selection, _state, _objects = setup(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("local-only recovery must not access DNS or HTTP")

    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", forbidden)
    with pytest.raises(ValueError, match="Local-only original"):
        web.resolve_official_result(selection, allow_network=False)


def test_production_original_handler_rejects_wrong_generation_before_worker_start(tmp_path):
    continuation, _acquisition, _web, record, _responses, originals, state, objects = (
        _owned_original_batch(tmp_path)
    )
    from astock.research.sla_runtime import CurrentResearchSlaService

    handlers = build_official_resolution_handlers(
        continuation.paths, state, objects, record, originals,
    )
    scheduler = CurrentResearchSlaService(state, objects, clock=continuation.clock)
    run_id = record.external_tasks[0].scheduler_run_id
    assert run_id is not None
    task = scheduler.tasks(run_id)[0]
    with pytest.raises(ValueError, match="scheduler generation"):
        handlers[task.node_id](task.model_copy(update={"generation": task.generation + 1}))
    handlers[task.node_id].cancel()
    from astock.research.process_worker import WorkerCancelledError
    with pytest.raises(WorkerCancelledError):
        handlers[task.node_id](task)


def test_host_batch_checks_all_continuation_bindings_before_publishing(tmp_path):
    _continuation, _acquisition, web, record, _request, _host, view, _state, _objects = (
        _pending_web(tmp_path)
    )
    first = _host_receipt(view)
    unknown = first.model_copy(update={"operation_id": "f" * 64})
    with pytest.raises(ValueError, match="registered operation"):
        web.complete_continuation_results(record.continuation_id, [first, unknown])
    stored = web.repository.get(view["operation_id"])
    assert stored is not None and stored["status"] == "WAITING_HOST"


def _prepared_default_web(tmp_path):
    continuation, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, True])
    record = continuation.start(_request(request_id="claim-before-bind"))
    record = continuation.prepare_external_wave(record.continuation_id)
    request = prepare_continuation_web_requests(record, root=PROJECT_ROOT)[0]
    web = WebToolExecutionService(PROJECT_ROOT, state, objects, clock=continuation.clock)
    host = HostWebCapabilities(
        host_id="test-host", invocation_id="claim-window", observed_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        search=HostWebTool(availability="AVAILABLE", tool_name="search", billing="INCLUDED"),
    )
    return continuation, acquisition, web, record, request, host, state, objects


@pytest.mark.parametrize(
    "operation_status", ["WAITING_HOST", "SUCCEEDED", "PAID_RUNNING", "UNKNOWN"],
)
def test_default_resume_recovers_claim_before_coordinator_binding_without_redispatch(
    tmp_path, monkeypatch, operation_status,
):
    continuation, acquisition, web, record, request, host, state, objects = (
        _prepared_default_web(tmp_path)
    )
    if operation_status in {"WAITING_HOST", "SUCCEEDED"}:
        view = web.run(request, host)
        if operation_status == "SUCCEEDED":
            view = web.complete_host(HostWebResult(
                operation_id=view["operation_id"], dispatch_id=view["dispatch_id"],
                invocation_id=host.invocation_id, tool_name="search", status="SUCCEEDED",
                observed_at=continuation.clock(), hits=[WebToolHit(
                    url="https://www.sse.com.cn/disclosure/listedinfo/report.pdf",
                    title="Official report",
                )],
            ))
    else:
        cursor, claimed = web.repository.claim(
            request, source="deepseek-tools", host=None,
            policy=web.policy, now=continuation.clock(),
        )
        assert claimed
        if operation_status == "UNKNOWN":
            monkeypatch.setattr(
                WebToolOperationRepository, "_owner_gone", staticmethod(lambda _cursor: True),
            )
            cursor = web.repository.reconcile_owner(cursor["operation_id"], continuation.clock())
        view = web._view(cursor)
    assert record.external_tasks[0].external_operation_id is None
    budget_key = "request:" + content_hash(record.request_id)
    before_budget = state.get_checkpoint("web-tool-budget", budget_key)
    before_operation = web.repository.get(view["operation_id"])

    def forbidden_dispatch(*args, **kwargs):
        raise AssertionError("existing cursor recovery must not rerun Web or transport")

    monkeypatch.setattr(WebToolExecutionService, "run", forbidden_dispatch)
    for _ in range(2):
        result = advance_current_web(
            continuation.paths, state, objects, record.continuation_id,
            continuation=continuation, resume_unbound=True,
        )
        current = continuation.get(record.continuation_id)
        assert current is not None
        task = current.external_tasks[0]
        assert task.external_operation_id == view["operation_id"]
        assert task.dispatch_id == record.external_tasks[0].dispatch_id
        assert task.scheduler_run_id == record.external_tasks[0].scheduler_run_id
        assert current.deadline_at == record.deadline_at and acquisition.calls == 1
        recovered = [item for item in result["web_operations"]
                     if item.get("operation_id") == view["operation_id"]]
        assert len(recovered) == 1 and recovered[0]["status"] == operation_status
    assert state.get_checkpoint("web-tool-budget", budget_key) == before_budget
    assert web.repository.get(view["operation_id"]) == before_operation
    if operation_status in {"PAID_RUNNING", "UNKNOWN"}:
        assert before_budget is not None
        assert before_budget["cursor"]["reserved_or_charged"] == 1
        current = continuation.get(record.continuation_id)
        assert current is not None
        assert current.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN


@pytest.mark.parametrize("wrong_binding", ["request_id", "continuation_dispatch_id"])
def test_default_cursor_recovery_rejects_other_request_or_generation_before_dispatch(
    tmp_path, monkeypatch, wrong_binding,
):
    continuation, acquisition, web, record, request, host, state, objects = (
        _prepared_default_web(tmp_path)
    )
    wrong = request.model_copy(update={wrong_binding: "another-request-or-generation"})
    cursor, claimed = web.repository.claim(
        wrong, source="host-included-web", host=host.model_dump(mode="json"),
        policy=web.policy, now=continuation.clock(),
    )
    assert claimed
    task = record.external_tasks[0].model_copy(update={
        "external_operation_id": cursor["operation_id"],
    })
    continuation._persist(record.model_copy(update={"external_tasks": [task]}))
    before = state.get_checkpoint("current-research-continuation", record.continuation_id)

    def forbidden_dispatch(*args, **kwargs):
        raise AssertionError("conflicting cursor must be rejected before any dispatch")

    monkeypatch.setattr(WebToolExecutionService, "run", forbidden_dispatch)
    with pytest.raises(ValueError, match="request or generation"):
        advance_current_web(
            continuation.paths, state, objects, record.continuation_id,
            continuation=continuation, resume_unbound=True,
        )
    assert state.get_checkpoint("current-research-continuation", record.continuation_id) == before
    assert web.repository.get(cursor["operation_id"]) == cursor and acquisition.calls == 1


def test_default_composition_holds_request_ownership_across_claim_and_binding(
    tmp_path, monkeypatch,
):
    continuation, acquisition, _web, record, _request_value, host, state, objects = (
        _prepared_default_web(tmp_path)
    )
    entered, release = Event(), Event()
    original_claim = WebToolOperationRepository.claim

    def paused_claim(self, request, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original_claim(self, request, **kwargs)

    monkeypatch.setattr(WebToolOperationRepository, "claim", paused_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            advance_current_web, continuation.paths, state, objects, record.continuation_id,
            CurrentResearchWebBatch(
                continuation_id=record.continuation_id, host_capabilities=host,
            ),
            continuation=continuation,
        )
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(ScheduledRunInProgress):
                advance_current_web(
                    continuation.paths, state, objects, record.continuation_id,
                    continuation=continuation, resume_unbound=True,
                )
        finally:
            release.set()
        result = first.result(timeout=10)
    current = continuation.get(record.continuation_id)
    assert current is not None and acquisition.calls == 1
    assert current.external_tasks[0].dispatch_id == record.external_tasks[0].dispatch_id
    operation = result["web_operations"][0]
    assert operation["status"] == "WAITING_HOST"
    assert current.external_tasks[0].external_operation_id == operation["operation_id"]


def test_default_web_preparation_preserves_an_existing_non_web_operation(tmp_path, monkeypatch):
    continuation, acquisition, web, record, _request_value, host, state, objects = (
        _prepared_default_web(tmp_path)
    )
    task = record.external_tasks[0]
    assert task.dispatch_id is not None
    external_id = "e" * 64
    bound = continuation.record_external_operation(
        record.continuation_id, task.task_id, task.dispatch_id, external_id,
    )
    assert web.repository.get(external_id) is None

    def forbidden_dispatch(*args, **kwargs):
        raise AssertionError("a different adapter already owns this evidence gap")

    monkeypatch.setattr(WebToolExecutionService, "run", forbidden_dispatch)
    result = advance_current_web(
        continuation.paths, state, objects, record.continuation_id,
        CurrentResearchWebBatch(continuation_id=record.continuation_id, host_capabilities=host),
        continuation=continuation, resume_unbound=True,
    )
    current = continuation.get(record.continuation_id)
    assert current is not None and current.external_tasks == bound.external_tasks
    assert result["web_operations"] == [] and acquisition.calls == 1
    assert web.repository.get(external_id) is None



def _second_original_consumer(continuation, web, selection):
    old = web.repository.get(selection.operation_id)
    assert old is not None
    original = WebToolRequest.model_validate(old["request"])
    record = continuation.start(_request(request_id="another-original-consumer"))
    request = original.model_copy(update={
        "request_id": record.request_id, "continuation_id": record.continuation_id,
        "task_id": record.external_tasks[0].task_id, "continuation_dispatch_id": None,
    })
    view = web.run(request, HostWebCapabilities.model_validate(old["host"]))
    web.complete_host(_host_receipt(view))
    return selection.model_copy(update={"operation_id": view["operation_id"]}), record


@pytest.mark.parametrize("concurrent", [False, True])
def test_canonical_pdf_fetch_is_shared_but_each_request_revalidates_and_binds_itself(
    tmp_path, monkeypatch, concurrent,
):
    continuation, _, web, first_selection, state, objects = setup(tmp_path)
    second_selection, second_record = _second_original_consumer(
        continuation, web, first_selection,
    )
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "A shared original, separate consumer receipts")
        raw = document.tobytes()
    downloads = []
    entered, release = Event(), Event()

    def fetch(proposal, **kwargs):
        kwargs["before_hop"](str(proposal.candidate_url))
        budget = current_request_budget()
        assert budget is not None
        budget.reserve_attempt()
        downloads.append(str(proposal.candidate_url))
        entered.set()
        assert not concurrent or release.wait(10)
        return PublicPdfDownload(raw, first_selection.url, (first_selection.url,))

    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", fetch)
    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as executor:
            owner = executor.submit(web.resolve_official_result, first_selection)
            try:
                assert entered.wait(10)
                follower = executor.submit(web.resolve_official_result, second_selection)
            finally:
                release.set()
            first, second = owner.result(timeout=10), follower.result(timeout=10)
    else:
        first = web.resolve_official_result(first_selection)
        second = web.resolve_official_result(second_selection)
    assert len(downloads) == 1
    assert first.continuation_id != second.continuation_id == second_record.continuation_id
    assert first.external_operation_id != second.external_operation_id
    for resolution in (first, second):
        bound = continuation.apply_automatic_resolution(resolution)
        assert bound.external_tasks[0].status is ExternalResearchTaskStatus.EVIDENCE_BOUND
        capture = state.artifact_record(resolution.capture_artifact_ids[0])
        assert capture is not None and objects.verify(capture["object_hash"])


def test_shared_raw_never_inherits_another_requests_admission_or_period(tmp_path, monkeypatch):
    continuation, _, web, first_selection, _, _ = setup(tmp_path)
    second_selection, _ = _second_original_consumer(continuation, web, first_selection)
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Annual original")
        raw = document.tobytes()
    downloads = []

    def fetch(proposal, **kwargs):
        downloads.append(str(proposal.candidate_url))
        return PublicPdfDownload(raw, first_selection.url, (first_selection.url,))

    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", fetch)
    web.resolve_official_result(first_selection)

    def rejected(*args, **kwargs):
        raise ValueError("current parser or admission policy rejects this consumer")

    monkeypatch.setattr(OfficialWebDocumentCaptureService, "capture", rejected)
    with pytest.raises(ValueError, match="current parser"):
        web.resolve_official_result(second_selection)
    assert len(downloads) == 1
    # Even with identical bytes/URL, a different period cannot reuse the previous raw identity.
    third_record = continuation.start(_request(request_id="different-period-consumer"))
    old = web.repository.get(first_selection.operation_id)
    assert old is not None
    third_request = WebToolRequest.model_validate(old["request"]).model_copy(update={
        "request_id": third_record.request_id, "continuation_id": third_record.continuation_id,
        "task_id": third_record.external_tasks[0].task_id,
    })
    view = web.run(third_request, HostWebCapabilities.model_validate(old["host"]))
    web.complete_host(_host_receipt(view))
    different_period = first_selection.model_copy(update={
        "operation_id": view["operation_id"], "period_end": date(2024, 12, 31),
    })
    with pytest.raises(ValueError, match="current parser"):
        web.resolve_official_result(different_period)
    assert len(downloads) == 2


def test_shared_raw_freeze_before_per_operation_receipt_recovers_locally_after_deadline(
    tmp_path, monkeypatch,
):
    continuation, _, web, selection, state, _ = setup(tmp_path)
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Original frozen before consumer receipt")
        raw = document.tobytes()
    downloads = []

    def fetch(proposal, **kwargs):
        downloads.append(str(proposal.candidate_url))
        return PublicPdfDownload(raw, selection.url, (selection.url,))

    register = state.register_artifact

    def interrupted(**kwargs):
        if kwargs["artifact_type"] == "WebOfficialRaw":
            raise RuntimeError("before operation raw receipt")
        return register(**kwargs)

    monkeypatch.setattr("astock.documents.public_pdf.fetch_official_pdf", fetch)
    monkeypatch.setattr(state, "register_artifact", interrupted)
    with pytest.raises(RuntimeError, match="operation raw receipt"):
        web.resolve_official_result(selection)
    monkeypatch.setattr(state, "register_artifact", register)
    operation = web.repository.get(selection.operation_id)
    assert operation is not None
    record = continuation.get(operation["request"]["continuation_id"])
    assert record is not None
    web.clock = continuation.clock = lambda: record.deadline_at + timedelta(seconds=1)
    assert continuation.resume(record.continuation_id).status.value == "BUDGET_EXHAUSTED"
    assert web.has_frozen_original(selection)
    resolution = web.resolve_official_result(selection, allow_network=False)
    assert resolution.continuation_id == record.continuation_id and len(downloads) == 1


@pytest.mark.parametrize("empty", [False, True])
def test_default_advance_releases_only_verified_empty_host_query_without_paid_retry(
    tmp_path, monkeypatch, empty,
):
    continuation, acquisition, web, record, request, host, state, objects = (
        _prepared_default_web(tmp_path)
    )

    def no_paid(*args, **kwargs):
        raise AssertionError("included host completion cannot spend a paid fallback")

    monkeypatch.setattr(
        "astock.providers.deepseek_tools.DeepSeekSearchFetchClient.execute", no_paid,
    )
    prepared = advance_current_web(
        continuation.paths, state, objects, record.continuation_id,
        CurrentResearchWebBatch(
            continuation_id=record.continuation_id, host_capabilities=host,
        ), continuation=continuation,
    )
    view = next(item for item in prepared["web_operations"]
                if item.get("operation_id") == operation_id(request))
    response = HostWebResult(
        operation_id=view["operation_id"], dispatch_id=view["dispatch_id"],
        invocation_id=host.invocation_id, tool_name="search", status="SUCCEEDED",
        observed_at=continuation.clock(),
        hits=[] if empty else [WebToolHit(url="https://example.com/unverified-report")],
    )
    result = advance_current_web(
        continuation.paths, state, objects, record.continuation_id,
        CurrentResearchWebBatch(
            continuation_id=record.continuation_id, host_results=[response],
        ), continuation=continuation, resume_unbound=True,
    )
    current = continuation.get(record.continuation_id)
    assert current is not None
    if empty:
        assert acquisition.calls == 2 and not acquisition.blocked_modes[-1]
        assert all(task.external_operation_id is None for task in current.external_tasks)
    else:
        assert acquisition.calls == 1
        assert current.external_tasks[0].external_operation_id == view["operation_id"]
    assert result["continuation_id"] == record.continuation_id
    assert state.get_checkpoint("web-tool-budget", "day:" + NOW.date().isoformat()) is None
