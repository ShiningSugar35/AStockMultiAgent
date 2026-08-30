from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import HttpUrl

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import OfficialWebDocumentCaptureService
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.research.continuation import CurrentResearchContinuationService
from astock.research.team import ResearchTeamService
from astock.schemas import AgentSourceProposal, DocumentType, SourceClass
from astock.schemas.financial import (
    FinancialCoverageStatus,
    FinancialIndustryProfile,
    FinancialIntegrityEvidencePack,
    FinancialRiskLevel,
)
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    ExternalAuthority,
    ExternalResearchNeed,
)
from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuationRequest,
    CurrentResearchContinuationStatus,
    CurrentResearchEvidenceBinding,
    ExternalResearchTaskStatus,
)
from astock.schemas.research_team import (
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTaskRole,
    ResearchTeamTaskState,
)
from astock.schemas.runs import RunStatus
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 29, 2, 0, tzinfo=UTC)
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class FakeAcquisition:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        gap_rounds: list[bool],
    ) -> None:
        self.state = state
        self.objects = objects
        self.gap_rounds = list(gap_rounds)
        self.calls = 0

    def acquire(
        self,
        company_id: str,
        market: Market,
        *,
        lookback_days: int | None = None,
        planner_plan_artifact_id: str | None = None,
        reuse_report_artifact_id: str | None = None,
        trusted_identity_capture_ids: tuple[str, ...] = (),
    ) -> CurrentResearchAcquisitionReport:
        del (
            lookback_days,
            planner_plan_artifact_id,
            reuse_report_artifact_id,
            trusted_identity_capture_ids,
        )
        index = min(self.calls, len(self.gap_rounds) - 1)
        gapped = self.gap_rounds[index]
        self.calls += 1
        observed = NOW + timedelta(seconds=self.calls)
        needs = []
        if gapped:
            needs = [
                ExternalResearchNeed(
                    capability=AcquisitionCapability.FINANCIAL_ANNUAL,
                    research_question=(f"从发行人官网或交易所取得 {company_id} 的最新年度报告。"),
                    preferred_authorities=[
                        ExternalAuthority.EXCHANGE_OFFICIAL,
                        ExternalAuthority.ISSUER_IR,
                    ],
                    created_at=observed,
                )
            ]
        report = CurrentResearchAcquisitionReport(
            report_id=f"current-research-acquisition:fake:{self.calls}",
            company_id=company_id,
            market=market,
            started_at=observed,
            decision_as_of=observed,
            status=(
                CurrentResearchAcquisitionStatus.NEEDS_EXTERNAL_RESEARCH
                if gapped
                else CurrentResearchAcquisitionStatus.READY
            ),
            attempts=[],
            external_research_needs=needs,
            created_at=observed,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=report.report_id,
            artifact_type="CurrentResearchAcquisitionReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        return report


def _runtime(tmp_path: Path) -> tuple[ProjectPaths, StateStore, ObjectStore]:
    runtime = tmp_path / "runtime"
    paths = ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    return paths, state, ObjectStore(paths.objects)


def _service(
    tmp_path: Path,
    *,
    gap_rounds: list[bool],
    clock: datetime = NOW + timedelta(seconds=30),
) -> tuple[
    CurrentResearchContinuationService,
    FakeAcquisition,
    StateStore,
    ObjectStore,
]:
    paths, state, objects = _runtime(tmp_path)
    acquisition = FakeAcquisition(state, objects, gap_rounds=gap_rounds)
    team = ResearchTeamService(project_root=PROJECT_ROOT, state=state, objects=objects)
    service = CurrentResearchContinuationService(
        paths,
        state,
        objects,
        acquisition=cast(CurrentResearchAcquisitionService, acquisition),
        team=team,
        clock=lambda: clock,
    )
    return service, acquisition, state, objects


def _request(
    *, request_id: str = "request-1", max_rounds: int = 3
) -> CurrentResearchContinuationRequest:
    return CurrentResearchContinuationRequest(
        request_id=request_id,
        company_id="600519",
        market=Market.XSHG,
        automatic_resolution_budget_seconds=1800,
        max_automatic_rounds=max_rounds,
        created_at=NOW,
    )


def _capture_annual_report(state: StateStore, objects: ObjectStore, company_id: str) -> str:
    proposal = AgentSourceProposal(
        requested_capability="financial.official_document",
        query="official annual report recovery",
        candidate_url=HttpUrl("https://www.sse.com.cn/disclosure/listedinfo/example.pdf"),
        expected_fact="latest annual financial facts",
        preferred_source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        formal_use=True,
        require_complete=False,
        reason="same-request automatic evidence recovery",
    )
    capture = OfficialWebDocumentCaptureService(state, objects).capture(
        proposal,
        PDF,
        title="测试公司2025年年度报告",
        company_ids=[company_id],
        published_at=NOW - timedelta(days=1),
        period_end=date(2025, 12, 31),
        document_type=DocumentType.ANNUAL_REPORT,
        observed_at=NOW,
    )
    return f"OfficialWebDocumentCapture:{capture.capture_id}"


def test_exchange_official_capture_can_resolve_identity_when_structured_sources_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, state, objects = _runtime(tmp_path)
    acquisition = CurrentResearchAcquisitionService(paths, state, objects, clock=lambda: NOW)
    capture_artifact_id = _capture_annual_report(state, objects, "600519")
    capture_id = capture_artifact_id.removeprefix("OfficialWebDocumentCapture:")
    failed = AcquisitionAttempt(
        capability=AcquisitionCapability.INSTRUMENT_IDENTITY,
        status=AcquisitionAttemptStatus.FAILED,
        provider_path=["eastmoney-reference"],
        fallback_used=True,
        record_count=0,
        latency_ms=5,
        internal_reason_codes=["TARGET_INSTRUMENT_IDENTITY_NOT_FOUND"],
        source_snapshot_ids=[],
        created_at=NOW,
    )
    monkeypatch.setattr(acquisition, "_reference_attempt", lambda *_args, **_kwargs: failed)

    resolved = acquisition._identity_attempt(
        "600519",
        Market.XSHG,
        trusted_identity_capture_ids=(capture_id,),
    )
    rejected = acquisition._identity_attempt(
        "600519",
        Market.XSHE,
        trusted_identity_capture_ids=(capture_id,),
    )
    stale_acquisition = CurrentResearchAcquisitionService(
        paths,
        state,
        objects,
        clock=lambda: NOW + timedelta(days=181),
    )
    monkeypatch.setattr(
        stale_acquisition,
        "_reference_attempt",
        lambda *_args, **_kwargs: failed,
    )
    stale = stale_acquisition._identity_attempt(
        "600519",
        Market.XSHG,
        trusted_identity_capture_ids=(capture_id,),
    )

    assert resolved.status is AcquisitionAttemptStatus.SUCCEEDED
    assert resolved.record_count == 1
    assert "sse-official-web" in resolved.provider_path
    assert "OFFICIAL_EXCHANGE_DOCUMENT_IDENTITY_FALLBACK" in resolved.internal_reason_codes
    assert rejected == failed
    assert stale == failed


def _register_generic_artifact(
    state: StateStore,
    objects: ObjectStore,
    artifact_id: str,
) -> None:
    ref = objects.put_json({"artifact_id": artifact_id, "test": True})
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="TestResearchOutput",
        schema_version="1.0",
        object_hash=ref.sha256,
        input_hashes=[],
    )


def _register_financial_pack(
    state: StateStore,
    objects: ObjectStore,
    artifact_id: str,
) -> None:
    pack = FinancialIntegrityEvidencePack(
        audit_run_id=artifact_id,
        request_hash="f" * 64,
        status=RunStatus.SUCCEEDED,
        coverage_status=FinancialCoverageStatus.COMPLETE,
        company_id="600519",
        as_of=NOW,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        periods=[],
        input_fact_ids=[],
        source_snapshot_ids=[],
        pit_ids=[],
        verified_numbers=[],
        recalculated_metrics=[],
        rule_findings=[],
        evidence_gaps=[],
        risk_level=FinancialRiskLevel.LOW,
        rule_versions={},
        model_versions={},
        capability_status={},
        created_at=NOW,
    )
    ref = objects.put_json(pack.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="FinancialIntegrityEvidencePack",
        schema_version=pack.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )


def _complete_company_team(
    service: CurrentResearchContinuationService,
    state: StateStore,
    objects: ObjectStore,
    plan_id: str,
) -> None:
    plan = service.team.get_plan(plan_id)
    assert plan is not None
    for task in plan.tasks:
        if task.task_id == "recommendation-gate":
            continue
        member_artifact_id = f"test-member:{plan_id}:{task.task_id}"
        if task.role is ResearchTaskRole.FINANCIAL_INTEGRITY:
            _register_financial_pack(state, objects, member_artifact_id)
        else:
            _register_generic_artifact(state, objects, member_artifact_id)
        role_output = service.team.register_role_output(
            ResearchRoleOutput(
                plan_id=plan_id,
                task_id=task.task_id,
                output_contract=task.output_contract,
                member_artifact_ids=[member_artifact_id],
                evidence_ids=[],
                readiness_check_results={check: True for check in task.readiness_checks},
                summary=f"completed {task.task_id}",
                created_at=NOW,
            )
        )
        context = f"context:{task.task_id}"
        if task.task_id == "bull-case":
            context = "context:independent-bull"
        elif task.task_id == "bear-case":
            context = "context:independent-bear"
        service.team.register_role_result(
            ResearchRoleResult(
                plan_id=plan_id,
                task_id=task.task_id,
                state=ResearchTeamTaskState.COMPLETE,
                independent_context_id=context,
                output_artifact_ids=[str(role_output["artifact_id"])],
                evidence_ids=[],
                created_at=NOW,
            )
        )


def test_continuation_cli_is_discoverable() -> None:
    commands = {command.name for command in app.registered_commands if command.name}
    assert {
        "research-current-continuation-schema",
        "research-current-continuation-start",
        "research-current-continuation-resolve",
        "research-current-continuation-bind",
        "research-current-continuation-resume",
        "research-current-continuation-advance",
        "research-current-continuation-status",
    } <= commands


def test_same_request_automatically_continues_from_evidence_to_team_and_gate(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(tmp_path, gap_rounds=[True, False])
    started = service.start(_request())

    assert started.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    assert not started.manual_actions
    assert not started.investor_view_allowed
    assert len(started.external_tasks) == 1
    task = started.external_tasks[0]
    capture_artifact_id = _capture_annual_report(state, objects, started.company_id)

    bound = service.bind_external_evidence(
        CurrentResearchEvidenceBinding(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            capture_artifact_id=capture_artifact_id,
            created_at=NOW,
        )
    )
    assert bound.external_tasks[0].status is ExternalResearchTaskStatus.EVIDENCE_BOUND
    assert bound.external_tasks[0].capture_artifact_ids == [capture_artifact_id]

    continued = service.resume(started.continuation_id)
    assert acquisition.calls == 2
    assert continued.status is CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    assert continued.team_plan_id is not None
    assert not continued.investor_view_allowed
    assert all(
        item.status is ExternalResearchTaskStatus.RESOLVED for item in continued.external_tasks
    )

    _complete_company_team(service, state, objects, continued.team_plan_id)
    ready = service.advance_team(continued.continuation_id)

    assert ready.status is CurrentResearchContinuationStatus.READY_FOR_INVESTOR_VIEW
    assert ready.investor_view_allowed
    assert ready.readiness_report_artifact_id is not None
    assert not ready.broker_execution_allowed
    persisted = service.get(ready.continuation_id)
    assert persisted == ready


def test_start_is_idempotent_for_the_same_request(tmp_path: Path) -> None:
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True])
    request = _request(request_id="stable-request")

    first = service.start(request)
    second = service.start(request)

    assert first == second
    assert acquisition.calls == 1


def test_manual_escalation_occurs_only_after_bounded_automatic_rounds(tmp_path: Path) -> None:
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True, True])
    started = service.start(_request(request_id="manual-last", max_rounds=2))

    assert started.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    assert not started.manual_actions
    first_failure = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=started.external_tasks[0].task_id,
            failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
        )
    )
    second_round = service.resume(first_failure.continuation_id)
    assert second_round.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    second_failure = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=second_round.continuation_id,
            task_id=second_round.external_tasks[0].task_id,
            failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
        )
    )
    escalated = service.resume(second_failure.continuation_id)

    assert acquisition.calls == 2
    assert escalated.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
    assert escalated.automatic_budget_exhausted
    assert escalated.manual_actions
    assert not escalated.investor_view_allowed


def test_capture_from_another_company_is_rejected(tmp_path: Path) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request(request_id="wrong-company"))
    capture_artifact_id = _capture_annual_report(state, objects, "600000")

    with pytest.raises(ValueError, match="not bound to the continuation company"):
        service.bind_external_evidence(
            CurrentResearchEvidenceBinding(
                continuation_id=started.continuation_id,
                task_id=started.external_tasks[0].task_id,
                capture_artifact_id=capture_artifact_id,
                created_at=NOW,
            )
        )


def test_automatic_resolution_accepts_capture_id_returned_by_ingest_cli(tmp_path: Path) -> None:
    service, _, state, objects = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request(request_id="raw-capture-id"))
    task = started.external_tasks[0]
    artifact_id = _capture_annual_report(state, objects, started.company_id)
    capture_id = artifact_id.removeprefix("OfficialWebDocumentCapture:")

    updated = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            capture_artifact_ids=[capture_id],
        )
    )

    updated_task = next(item for item in updated.external_tasks if item.task_id == task.task_id)
    assert updated_task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND
    assert updated_task.capture_artifact_ids == [capture_id]
    assert len(updated.automatic_resolution_artifact_ids) == 1
    capture_record = state.artifact_record(artifact_id)
    checkpoint = state.get_checkpoint("current-research-continuation", updated.continuation_id)
    assert capture_record is not None
    assert checkpoint is not None
    continuation_artifact = state.artifact_record(str(checkpoint["cursor"]["artifact_id"]))
    assert continuation_artifact is not None
    assert str(capture_record["object_hash"]) in continuation_artifact["input_hashes"]


def test_team_advance_stays_closed_until_all_required_tasks_complete(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, gap_rounds=[False])
    started = service.start(_request(request_id="team-not-done"))

    assert started.status is CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    unchanged = service.advance_team(started.continuation_id)

    assert unchanged == started
    assert not unchanged.investor_view_allowed


def test_run_to_terminal_drives_evidence_team_and_gate_in_one_call(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(
        tmp_path,
        gap_rounds=[True, False],
    )
    started = service.start(_request(request_id="one-call-terminal"))
    resolver_calls: list[str] = []
    team_calls: list[tuple[str, ...]] = []

    def resolve_external(record, task):
        resolver_calls.append(task.task_id)
        capture_artifact_id = _capture_annual_report(
            state,
            objects,
            record.company_id,
        )
        return CurrentResearchAutomaticResolution(
            continuation_id=record.continuation_id,
            task_id=task.task_id,
            capture_artifact_ids=[capture_artifact_id],
        )

    def execute_team(record, plan, ready_tasks) -> None:
        del record
        team_calls.append(tuple(task.task_id for task in ready_tasks))
        _complete_company_team(service, state, objects, plan.plan_id)

    final = service.run_to_terminal(
        started.continuation_id,
        resolve_external=resolve_external,
        execute_team=execute_team,
    )

    assert acquisition.calls == 2
    assert resolver_calls == [started.external_tasks[0].task_id]
    assert team_calls
    assert final.status is CurrentResearchContinuationStatus.READY_FOR_INVESTOR_VIEW
    assert final.investor_view_allowed
    assert final.formal_recommendation_allowed
    assert len(final.automatic_resolution_artifact_ids) == 1
    resolution_artifact = state.artifact_record(final.automatic_resolution_artifact_ids[0])
    assert resolution_artifact is not None
    assert resolution_artifact["type"] == "CurrentResearchAutomaticResolution"
    report = service.status(final.continuation_id)
    assert report["same_request_continuation_required"] is False
    assert report["investment_conclusion_blocked"] is False
    assert report["broker_execution_allowed"] is False


def test_run_to_terminal_consumes_bound_evidence_after_restart_without_recalling_resolver(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(
        tmp_path,
        gap_rounds=[True, False],
    )
    started = service.start(_request(request_id="bound-evidence-restart"))
    task = started.external_tasks[0]
    capture_artifact_id = _capture_annual_report(state, objects, started.company_id)
    bound = service.bind_external_evidence(
        CurrentResearchEvidenceBinding(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            capture_artifact_id=capture_artifact_id,
            created_at=NOW,
        )
    )
    resolver_calls: list[str] = []

    def resolve_external(record, external_task):
        del record
        resolver_calls.append(external_task.task_id)
        raise AssertionError("already-bound evidence must be consumed before resolver reuse")

    def execute_team(record, plan, ready_tasks) -> None:
        del record, ready_tasks
        _complete_company_team(service, state, objects, plan.plan_id)

    final = service.run_to_terminal(
        bound.continuation_id,
        resolve_external=resolve_external,
        execute_team=execute_team,
    )

    assert resolver_calls == []
    assert acquisition.calls == 2
    assert final.status is CurrentResearchContinuationStatus.READY_FOR_INVESTOR_VIEW
    assert final.investor_view_allowed
    assert final.formal_recommendation_allowed


def test_run_to_terminal_uses_the_configured_final_automatic_round_before_escalating(
    tmp_path: Path,
) -> None:
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request(request_id="one-round-budget", max_rounds=1))
    resolver_calls: list[str] = []

    def resolve_external(record, task):
        resolver_calls.append(task.task_id)
        return CurrentResearchAutomaticResolution(
            continuation_id=record.continuation_id,
            task_id=task.task_id,
            failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
        )

    def execute_team(record, plan, ready_tasks) -> None:
        del record, plan, ready_tasks
        raise AssertionError("team execution must not start while the evidence gap remains")

    final = service.run_to_terminal(
        started.continuation_id,
        resolve_external=resolve_external,
        execute_team=execute_team,
    )

    assert acquisition.calls == 1
    assert resolver_calls == [started.external_tasks[0].task_id]
    assert final.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
    assert final.automatic_budget_exhausted is True
    assert len(final.automatic_resolution_artifact_ids) == 1


def test_final_automatic_round_consumes_successful_bound_evidence_before_team(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(
        tmp_path,
        gap_rounds=[True, False],
    )
    started = service.start(_request(request_id="final-round-success", max_rounds=1))
    task = started.external_tasks[0]
    capture_artifact_id = _capture_annual_report(state, objects, started.company_id)

    bound = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            capture_artifact_ids=[capture_artifact_id],
        )
    )
    resumed = service.resume(bound.continuation_id)

    assert acquisition.calls == 2
    assert resumed.status is CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    assert resumed.automatic_rounds_completed == 1
    assert resumed.automatic_budget_exhausted is False
    assert not resumed.manual_actions
    assert all(
        item.status is ExternalResearchTaskStatus.RESOLVED for item in resumed.external_tasks
    )


def test_final_automatic_round_does_not_gain_an_extra_resolver_round_when_bound_evidence_fails(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(
        tmp_path,
        gap_rounds=[True, True],
    )
    started = service.start(_request(request_id="final-round-still-gapped", max_rounds=1))
    task = started.external_tasks[0]
    capture_artifact_id = _capture_annual_report(state, objects, started.company_id)

    bound = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            capture_artifact_ids=[capture_artifact_id],
        )
    )
    escalated = service.resume(bound.continuation_id)

    assert acquisition.calls == 2
    assert escalated.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
    assert escalated.automatic_rounds_completed == 1
    assert escalated.automatic_budget_exhausted is True
    assert escalated.manual_actions
    assert not escalated.investor_view_allowed


def test_run_to_terminal_requests_private_material_without_exhausting_budget(
    tmp_path: Path,
) -> None:
    service, acquisition, _, _ = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request(request_id="private-material"))

    def resolve_external(record, task):
        return CurrentResearchAutomaticResolution(
            continuation_id=record.continuation_id,
            task_id=task.task_id,
            failure_code="PRIVATE_IR_MATERIAL_REQUIRED",
            private_material_required=True,
        )

    def execute_team(record, plan, ready_tasks) -> None:
        del record, plan, ready_tasks
        raise AssertionError("team execution must not start before private evidence exists")

    final = service.run_to_terminal(
        started.continuation_id,
        resolve_external=resolve_external,
        execute_team=execute_team,
    )

    assert acquisition.calls == 1
    assert final.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
    assert final.private_material_required
    assert not final.automatic_budget_exhausted
    assert len(final.manual_actions) == 1
    assert not final.investor_view_allowed
    assert not final.broker_execution_allowed


def test_automatic_resolution_rejects_unregistered_capture_before_lineage_write(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _service(tmp_path, gap_rounds=[True])
    started = service.start(_request(request_id="unregistered-capture"))
    task = started.external_tasks[0]

    with pytest.raises(ValueError, match="registered official Web captures"):
        service.apply_automatic_resolution(
            CurrentResearchAutomaticResolution(
                continuation_id=started.continuation_id,
                task_id=task.task_id,
                capture_artifact_ids=["OfficialWebDocumentCapture:missing"],
            )
        )

    after = cast(dict[str, object], service.status(started.continuation_id)["continuation"])
    external_tasks = cast(list[dict[str, object]], after["external_tasks"])
    assert after["automatic_resolution_artifact_ids"] == []
    assert external_tasks[0]["capture_artifact_ids"] == []
    assert after["status"] == "AUTO_RESOLUTION_REQUIRED"


def test_private_material_can_resume_the_same_continuation_after_user_binding(
    tmp_path: Path,
) -> None:
    service, acquisition, state, objects = _service(
        tmp_path,
        gap_rounds=[True, False],
    )
    started = service.start(_request(request_id="private-material-resume"))
    capture_artifact_id = _capture_annual_report(state, objects, started.company_id)
    task = started.external_tasks[0]

    interrupted = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            failure_code="PRIVATE_FORMAL_MATERIAL_REQUIRED",
            private_material_required=True,
        )
    )
    assert interrupted.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
    assert interrupted.private_material_required is True
    assert interrupted.automatic_budget_exhausted is False
    assert len(interrupted.manual_actions) == 1

    rebound = service.bind_external_evidence(
        CurrentResearchEvidenceBinding(
            continuation_id=started.continuation_id,
            task_id=task.task_id,
            capture_artifact_id=capture_artifact_id,
            created_at=NOW,
        )
    )
    assert rebound.continuation_id == started.continuation_id
    assert rebound.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    assert rebound.private_material_required is False
    assert rebound.manual_actions == []

    resumed = service.resume(started.continuation_id)
    assert resumed.continuation_id == started.continuation_id
    assert resumed.status is CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
    assert resumed.team_plan_id is not None
    assert len(resumed.automatic_resolution_artifact_ids) == 1
    assert acquisition.calls == 2


def test_public_source_failure_escalates_only_after_automatic_budget_exhaustion(
    tmp_path: Path,
) -> None:
    service, acquisition, _, _ = _service(
        tmp_path,
        gap_rounds=[True, True],
    )
    started = service.start(_request(request_id="public-budget", max_rounds=2))

    first_failure = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=started.external_tasks[0].task_id,
            failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
        )
    )
    assert first_failure.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    assert first_failure.manual_actions == []

    first_retry = service.resume(started.continuation_id)
    assert first_retry.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
    assert first_retry.automatic_budget_exhausted is False
    assert first_retry.manual_actions == []

    second_failure = service.apply_automatic_resolution(
        CurrentResearchAutomaticResolution(
            continuation_id=started.continuation_id,
            task_id=first_retry.external_tasks[0].task_id,
            failure_code="PUBLIC_SOURCE_TEMPORARY_FAILURE",
        )
    )
    exhausted = service.resume(second_failure.continuation_id)

    assert exhausted.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
    assert exhausted.automatic_budget_exhausted is True
    assert exhausted.private_material_required is False
    assert len(exhausted.manual_actions) == 1
    assert len(exhausted.automatic_resolution_artifact_ids) == 2
    assert acquisition.calls == 2
