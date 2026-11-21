"""Durable same-request continuation for current company research."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import local
from typing import Literal

import httpx

from astock.core.hashing import content_hash
from astock.core.logging import emit_operational_event
from astock.core.object_store import ObjectStore
from astock.core.request_budget import RequestBudgetExhausted, RequestExecutionBudget
from astock.core.state import StateStore
from astock.documents.repository import DocumentRepository
from astock.investor_orchestration.run_ownership import schedule_run_ownership
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.research.policy import load_default_current_research_policy
from astock.research.sla_runtime import CancellableTaskHandler, CurrentResearchSlaService
from astock.research.team import ResearchTeamService
from astock.schemas.operational import OperationalSeverity
from astock.schemas.research_acquisition import (
    AcquisitionCapability,
    CurrentResearchAcquisitionReport,
    ExternalResearchNeed,
    ManualResearchAction,
)
from astock.schemas.research_continuation import (
    CurrentResearchAutomaticResolution,
    CurrentResearchContinuation,
    CurrentResearchContinuationRequest,
    CurrentResearchContinuationStatus,
    CurrentResearchEvidenceBinding,
    CurrentResearchExternalTask,
    ExternalResearchTaskStatus,
)
from astock.schemas.research_sla import (
    ResearchSchedulerTask,
    ResearchSchedulerTaskStatus,
    ResearchTaskCategory,
)
from astock.schemas.research_team import (
    FullResearchInputReadinessRequest,
    ResearchTeamPlan,
    ResearchTeamTask,
)
from astock.schemas.source_access import OfficialWebDocumentCapture
from astock.settings import ProjectPaths

CurrentResearchExternalResolver = Callable[
    [CurrentResearchContinuation, CurrentResearchExternalTask],
    CurrentResearchAutomaticResolution,
]
CurrentResearchTeamExecutor = Callable[
    [CurrentResearchContinuation, ResearchTeamPlan, tuple[ResearchTeamTask, ...]],
    None,
]


class CurrentResearchContinuationService:
    """Own the automatic evidence-to-team-to-gate chain for one investor request."""

    def __init__(
        self,
        paths: ProjectPaths,
        state: StateStore,
        objects: ObjectStore,
        *,
        acquisition: CurrentResearchAcquisitionService | None = None,
        team: ResearchTeamService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.state = state
        self.objects = objects
        self.clock = clock or (lambda: datetime.now(UTC))
        self.policy = load_default_current_research_policy(paths.root)
        self.acquisition = acquisition or CurrentResearchAcquisitionService(paths, state, objects)
        self.team = team or ResearchTeamService(
            project_root=paths.root,
            state=state,
            objects=objects,
        )
        self.documents = DocumentRepository(state)
        self._coordinator_thread = local()

    def start(self, request: CurrentResearchContinuationRequest) -> CurrentResearchContinuation:
        with self._coordinator(self._continuation_id(request)):
            return self._start(request)

    def _start(self, request: CurrentResearchContinuationRequest) -> CurrentResearchContinuation:
        continuation_id = self._continuation_id(request)
        existing = self.get(continuation_id)
        if existing is not None:
            return existing

        if (
            request.automatic_resolution_budget_seconds
            > self.policy.automatic_resolution_budget_seconds
        ):
            raise ValueError("requested recovery budget exceeds the active policy limit")
        if request.max_transport_attempts > self.policy.max_transport_attempts:
            raise ValueError("requested transport attempts exceed the active policy limit")
        started_at = self.clock()
        request_ref = self.objects.put_json(request.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"CurrentResearchContinuationRequest:{continuation_id}",
            artifact_type="CurrentResearchContinuationRequest",
            schema_version=request.schema_version,
            object_hash=request_ref.sha256,
            input_hashes=[],
        )
        deadline_at = started_at + timedelta(seconds=request.automatic_resolution_budget_seconds)
        budget = RequestExecutionBudget(
            self.state,
            request.request_id,
            deadline_at,
            request.max_transport_attempts,
            clock=self.clock,
        )
        with budget.bind():
            report = self.acquisition.acquire(
                request.company_id,
                request.market,
                lookback_days=request.lookback_days,
                planner_plan_artifact_id=request.planner_plan_artifact_id,
            )
        tasks = [
            self._task_from_need(continuation_id, need, automatic_round=1)
            for need in report.external_research_needs
        ]
        team_plan_id: str | None = None
        status = CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
        if not tasks:
            plan = self.team.create_company_plan(
                company_id=request.company_id,
                acquisition_report_artifact_id=report.report_id,
                as_of=report.decision_as_of,
            )
            team_plan_id = plan.plan_id
            status = CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED
        record = CurrentResearchContinuation(
            continuation_id=continuation_id,
            request_id=request.request_id,
            company_id=request.company_id,
            market=request.market,
            lookback_days=request.lookback_days,
            planner_plan_artifact_id=request.planner_plan_artifact_id,
            started_at=started_at,
            deadline_at=started_at + timedelta(seconds=request.automatic_resolution_budget_seconds),
            status=status,
            automatic_resolution_budget_seconds=request.automatic_resolution_budget_seconds,
            max_automatic_rounds=request.max_automatic_rounds,
            max_transport_attempts=request.max_transport_attempts,
            automatic_rounds_completed=1,
            acquisition_report_artifact_ids=[report.report_id],
            current_acquisition_report_artifact_id=report.report_id,
            external_tasks=sorted(tasks, key=lambda item: item.task_id),
            team_plan_id=team_plan_id,
            created_at=request.created_at,
        )
        return self._persist(record)

    def run_to_terminal(
        self,
        continuation_id: str,
        *,
        resolve_external: CurrentResearchExternalResolver,
        execute_team: CurrentResearchTeamExecutor,
        external_handlers: Mapping[str, CancellableTaskHandler] | None = None,
    ) -> CurrentResearchContinuation:
        with self._coordinator(continuation_id):
            return self._run_to_terminal(
                continuation_id,
                resolve_external=resolve_external,
                execute_team=execute_team,
                external_handlers=external_handlers,
            )

    def _run_to_terminal(
        self,
        continuation_id: str,
        *,
        resolve_external: CurrentResearchExternalResolver,
        execute_team: CurrentResearchTeamExecutor,
        external_handlers: Mapping[str, CancellableTaskHandler] | None = None,
    ) -> CurrentResearchContinuation:
        """Drive automatic evidence and ready team stages in one caller request.

        Network search and role synthesis remain Agent-owned callbacks. This
        deterministic loop owns budgets, frozen result lineage, readiness gates,
        recovery, and the rule that an internal gap is never an investor answer.
        """

        record = self._require(continuation_id)
        if record.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN:
            if external_handlers is not None:
                record = self.resolve_external_wave(record.continuation_id, external_handlers)
            record = self.resume(record.continuation_id)
        while record.status in {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED,
        }:
            if record.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED:
                record = self._advance_external_evidence(
                    record, resolve_external, external_handlers
                )
                continue

            if record.team_plan_id is None:
                raise ValueError("team continuation is missing its plan")
            advanced = self.advance_team(record.continuation_id)
            if advanced != record:
                record = advanced
                continue
            plan = self.team.get_plan(record.team_plan_id)
            if plan is None:
                raise ValueError("company research-team plan is unavailable")
            team_status = self.team.status(plan.plan_id)
            ready_ids = self._status_task_ids(team_status, "ready_tasks")
            ready_by_id = {
                task.task_id: task for task in plan.tasks if task.required_for_recommendation
            }
            ready_tasks = tuple(
                ready_by_id[task_id] for task_id in ready_ids if task_id in ready_by_id
            )
            if not ready_tasks:
                raise RuntimeError(
                    "company research-team has no runnable required task and is not terminal"
                )
            completed_before = self._status_task_ids(
                team_status,
                "completed_tasks",
            )
            execute_team(record, plan, ready_tasks)
            completed_after = self._status_task_ids(
                self.team.status(plan.plan_id),
                "completed_tasks",
            )
            if len(completed_after) <= len(completed_before):
                raise RuntimeError("company research-team executor made no durable progress")
            record = self._require(record.continuation_id)
        return record

    def _advance_external_evidence(
        self,
        record: CurrentResearchContinuation,
        resolve_external: CurrentResearchExternalResolver,
        external_handlers: Mapping[str, CancellableTaskHandler] | None,
    ) -> CurrentResearchContinuation:
        if any(
            task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND
            for task in record.external_tasks
        ):
            return self.resume(record.continuation_id)
        if self._has_unknown_completion(record):
            if external_handlers is not None:
                record = self.resolve_external_wave(record.continuation_id, external_handlers)
                return self.resume(record.continuation_id)
            return self._persist(
                record.model_copy(
                    update={"status": CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN}
                )
            )
        if self.clock() >= record.deadline_at:
            return self._finish_recovery(record, "TIME_BUDGET_EXHAUSTED")
        if self._budget(record).remaining_attempts() == 0:
            return self._finish_recovery(record, "ATTEMPT_BUDGET_EXHAUSTED")
        unresolved = self._pending_public_tasks(record)
        if not unresolved:
            return self.resume(record.continuation_id)
        if external_handlers is not None:
            record = self.resolve_external_wave(record.continuation_id, external_handlers)
            return self.resume(record.continuation_id)
        for task in unresolved:
            if self.clock() >= record.deadline_at:
                break
            try:
                with self._budget(record).bind():
                    result = resolve_external(record, task)
            except RequestBudgetExhausted:
                return self._finish_recovery(record, "ATTEMPT_BUDGET_EXHAUSTED")
            except (
                TimeoutError,
                ConnectionError,
                httpx.TimeoutException,
                httpx.NetworkError,
            ):
                result = CurrentResearchAutomaticResolution(
                    continuation_id=record.continuation_id,
                    task_id=task.task_id,
                    failure_code="AUTOMATIC_RESOLVER_TRANSIENT_TRANSPORT",
                    created_at=self.clock(),
                )
            if result.task_id != task.task_id:
                raise ValueError("automatic resolver result does not match the requested task")
            record = self._apply_automatic_resolution(record, result)
            if record.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT:
                return record
        return self.resume(record.continuation_id)

    def apply_automatic_resolution(
        self, result: CurrentResearchAutomaticResolution
    ) -> CurrentResearchContinuation:
        with self._coordinator(result.continuation_id):
            return self._accept_automatic_resolution(result)

    def _accept_automatic_resolution(
        self,
        result: CurrentResearchAutomaticResolution,
    ) -> CurrentResearchContinuation:
        """Validate and persist one Agent-owned automatic resolution result."""

        record = self._require(result.continuation_id)
        task = next(
            (item for item in record.external_tasks if item.task_id == result.task_id), None
        )
        if task is not None and result.dispatch_id != task.dispatch_id:
            raise ValueError("automatic resolution belongs to another dispatch generation")
        if task is not None and task.resolution_hash == self._resolution_hash(result):
            return record
        active = record.status in {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        }
        late_captured_result = bool(result.capture_artifact_ids) and record.status in {
            CurrentResearchContinuationStatus.BUDGET_EXHAUSTED,
            CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE,
            CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
        }
        if not active and not late_captured_result:
            raise ValueError("automatic resolution is valid only for an active evidence gap")
        return self._apply_automatic_resolution(record, result)

    def _apply_automatic_resolution(
        self,
        record: CurrentResearchContinuation,
        result: CurrentResearchAutomaticResolution,
    ) -> CurrentResearchContinuation:
        if result.continuation_id != record.continuation_id:
            raise ValueError("automatic resolution belongs to another continuation")
        task = next(
            (item for item in record.external_tasks if item.task_id == result.task_id),
            None,
        )
        if task is None:
            raise ValueError("automatic resolution references an unknown task")
        if result.dispatch_id != task.dispatch_id:
            raise ValueError("automatic resolution belongs to another dispatch generation")
        result_hash = self._resolution_hash(result)
        if task.resolution_hash == result_hash:
            return record
        if task.resolution_hash is not None and (
            task.external_operation_id is not None or result.external_operation_id is not None
        ):
            raise ValueError("automatic resolution cannot replace a completed operation")
        if task.external_operation_id is not None and (
            task.external_operation_id != result.external_operation_id
        ):
            raise ValueError("automatic resolution belongs to another external operation")
        if task.status is ExternalResearchTaskStatus.RESOLVED:
            raise ValueError("automatic resolution cannot rewrite a resolved task")

        capture_hashes = [
            self._validate_official_capture(record, task, artifact_id)
            for artifact_id in result.capture_artifact_ids
        ]
        result_ref = self.objects.put_json(result.model_dump(mode="json"))
        artifact_id = (
            "CurrentResearchAutomaticResolution:"
            f"{record.continuation_id}:{result.task_id}:"
            f"round-{task.automatic_rounds_attempted}:{result_ref.sha256}"
        )
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="CurrentResearchAutomaticResolution",
            schema_version=result.schema_version,
            object_hash=result_ref.sha256,
            input_hashes=sorted(capture_hashes),
        )
        history = list(record.automatic_resolution_artifact_ids)
        if artifact_id not in history:
            history.append(artifact_id)
        updated_tasks = [
            item.model_copy(
                update={
                    "last_failure_code": result.failure_code,
                    "private_material_required": result.private_material_required,
                    "resolution_hash": result_hash,
                    "external_operation_id": result.external_operation_id,
                    "status": (
                        ExternalResearchTaskStatus.EVIDENCE_BOUND
                        if result.capture_artifact_ids
                        else item.status
                    ),
                    "capture_artifact_ids": sorted(
                        {
                            *item.capture_artifact_ids,
                            *result.capture_artifact_ids,
                        }
                    ),
                }
            )
            if item.task_id == task.task_id
            else item
            for item in record.external_tasks
        ]
        record = self._persist(
            record.model_copy(
                update={
                    "status": CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
                    "automatic_budget_exhausted": False,
                    "recovery_stop_reason": None,
                    "private_material_required": False,
                    "manual_actions": [],
                    "automatic_resolution_artifact_ids": history,
                    "external_tasks": updated_tasks,
                }
            )
        )
        if result.private_material_required:
            if not self._pending_public_tasks(record) and not any(
                item.status is ExternalResearchTaskStatus.EVIDENCE_BOUND
                for item in record.external_tasks
            ):
                return self._finish_private_material(record)
            return record
        return record

    @contextmanager
    def _coordinator(self, continuation_id: str) -> Iterator[None]:
        """Serialize mutations across processes; nested calls on this thread reuse ownership."""
        active = getattr(self._coordinator_thread, "active", frozenset())
        if continuation_id in active:
            yield
            return
        with schedule_run_ownership(self.state.path, "research-continuation", continuation_id):
            self._coordinator_thread.active = active | {continuation_id}
            try:
                yield
            finally:
                self._coordinator_thread.active = active

    def _budget(self, record: CurrentResearchContinuation) -> RequestExecutionBudget:
        return RequestExecutionBudget(
            self.state,
            record.request_id,
            record.deadline_at,
            min(record.max_transport_attempts, self.policy.max_transport_attempts),
            clock=self.clock,
        )

    @staticmethod
    def _resolution_hash(result: CurrentResearchAutomaticResolution) -> str:
        return content_hash(result.model_dump(mode="json", exclude={"created_at"}))

    def prepare_external_wave(self, continuation_id: str) -> CurrentResearchContinuation:
        """Freeze existing scheduler generations before genuine host Search/Fetch."""
        with self._coordinator(continuation_id):
            record = self._require(continuation_id)
            if record.status not in {
                CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
                CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
            }:
                return record
            scheduler = CurrentResearchSlaService(self.state, self.objects, clock=self.clock)
            return self._bind_external_wave(record, scheduler)

    def record_external_operation(
        self,
        continuation_id: str,
        task_id: str,
        dispatch_id: str,
        operation_id: str,
        *,
        completion_unknown: bool = False,
    ) -> CurrentResearchContinuation:
        """Bind an adapter-verified durable operation, never a merely prepared task."""
        with self._coordinator(continuation_id):
            record, task, dispatched = self._external_operation_task(
                continuation_id, task_id, dispatch_id, operation_id
            )
            # Search completion cannot settle a later PDF worker using the same operation.
            completion_unknown = completion_unknown or (
                task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
                and dispatched.dispatch_attempted is not False
            )
            updated = task.model_copy(update={
                "external_operation_id": operation_id,
                "last_failure_code": (
                    "EXTERNAL_COMPLETION_UNKNOWN" if completion_unknown else None
                ),
            })
            return self._persist_external_operation(record, updated)

    def release_external_operation(
        self, continuation_id: str, task_id: str, dispatch_id: str, operation_id: str,
    ) -> CurrentResearchContinuation:
        """Release only an adapter-verified known failure/cancellation of this operation."""
        with self._coordinator(continuation_id):
            record, task, dispatched = self._external_operation_task(
                continuation_id, task_id, dispatch_id, operation_id
            )
            if task.external_operation_id is None:
                return record
            if (
                task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
                and dispatched.dispatch_attempted is not False
            ):
                raise ValueError("dispatched original completion must be reconciled before release")
            updated = task.model_copy(update={
                "external_operation_id": None,
                "last_failure_code": "EXTERNAL_OPERATION_FAILED",
            })
            return self._persist_external_operation(record, updated)

    def _external_operation_task(
        self, continuation_id: str, task_id: str, dispatch_id: str, operation_id: str,
    ) -> tuple[CurrentResearchContinuation, CurrentResearchExternalTask, ResearchSchedulerTask]:
        record = self._require(continuation_id)
        if record.status not in {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        }:
            raise ValueError("external operation requires an active continuation")
        task = next((item for item in record.external_tasks if item.task_id == task_id), None)
        if (
            task is None or task.status is not ExternalResearchTaskStatus.PENDING
            or task.resolution_hash is not None or not operation_id.strip()
        ):
            raise ValueError("external operation requires an unresolved task")
        if not task.scheduler_run_id or not dispatch_id or task.dispatch_id != dispatch_id:
            raise ValueError("external operation belongs to another dispatch generation")
        scheduler = CurrentResearchSlaService(self.state, self.objects, clock=self.clock)
        dispatched = next(
            (item for item in scheduler.tasks(task.scheduler_run_id) if item.node_id == task_id),
            None,
        )
        if dispatched is None or external_dispatch_id(dispatched) != dispatch_id:
            raise ValueError("external operation has no matching scheduler generation")
        if task.external_operation_id not in {None, operation_id}:
            raise ValueError("external operation cannot replace another operation")
        return record, task, dispatched

    def _persist_external_operation(
        self, record: CurrentResearchContinuation, task: CurrentResearchExternalTask,
    ) -> CurrentResearchContinuation:
        updated = record.model_copy(update={"external_tasks": [
            task if item.task_id == task.task_id else item for item in record.external_tasks
        ]})
        return self._persist(updated.model_copy(update={"status": (
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
            if self._has_unknown_completion(updated)
            else CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
        )}))

    def resolve_external_wave(
        self,
        continuation_id: str,
        handlers: Mapping[str, CancellableTaskHandler],
        *,
        selected_task_ids: Collection[str] | None = None,
    ) -> CurrentResearchContinuation:
        with self._coordinator(continuation_id):
            return self._resolve_external_wave(
                continuation_id, handlers, selected_task_ids=selected_task_ids
            )

    def _resolve_external_wave(
        self,
        continuation_id: str,
        handlers: Mapping[str, CancellableTaskHandler],
        *,
        selected_task_ids: Collection[str] | None = None,
    ) -> CurrentResearchContinuation:
        """Reconcile frozen outcomes, then dispatch only independent unstarted gaps."""
        record = self._require(continuation_id)
        if record.status not in {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        }:
            return record
        self._validate_external_selection(record, handlers, selected_task_ids)
        scheduler = CurrentResearchSlaService(self.state, self.objects, clock=self.clock)
        record = self._bind_external_wave(record, scheduler)
        run_ids = sorted(
            {task.scheduler_run_id for task in record.external_tasks if task.scheduler_run_id}
        )
        budget = self._budget(record)
        for run_id in run_ids:
            tasks = scheduler.tasks(run_id)
            runnable = self._runnable_external_tasks(record, tasks, selected_task_ids)
            if runnable and budget.remaining_seconds() > 0 and budget.remaining_attempts() > 0:
                if any(
                    not isinstance(handlers.get(task.node_id), CancellableTaskHandler)
                    for task in runnable
                ):
                    raise ValueError("external wave requires a cancellable handler for ready gaps")
                scheduler.execute(
                    run_id,
                    {
                        task.node_id: _BudgetedExternalHandler(handlers[task.node_id], budget)
                        for task in runnable
                    },
                    resource_limits={ResearchTaskCategory.NETWORK: self.policy.max_workers},
                    recover_unowned=False,
                    selected_node_ids={task.node_id for task in runnable},
                )
            record = self._reconcile_external_wave(record, scheduler.tasks(run_id))
        if self._has_unknown_completion(record):
            return self._persist(
                record.model_copy(
                    update={
                        "status": CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
                        "automatic_budget_exhausted": False,
                        "recovery_stop_reason": None,
                    }
                )
            )
        if self.clock() >= record.deadline_at:
            return self._finish_recovery(record, "TIME_BUDGET_EXHAUSTED")
        if budget.remaining_attempts() == 0:
            return self._finish_recovery(record, "ATTEMPT_BUDGET_EXHAUSTED")
        return record

    @staticmethod
    def _validate_external_selection(
        record: CurrentResearchContinuation,
        handlers: Mapping[str, CancellableTaskHandler],
        selected_task_ids: Collection[str] | None,
    ) -> None:
        if selected_task_ids is None:
            return
        selected = set(selected_task_ids)
        known = {task.task_id for task in record.external_tasks}
        if not selected or not selected.issubset(known) or selected != set(handlers):
            raise ValueError("selected external tasks must match existing task handlers")

    @staticmethod
    def _runnable_external_tasks(
        record: CurrentResearchContinuation,
        tasks: tuple[ResearchSchedulerTask, ...],
        selected_task_ids: Collection[str] | None,
    ) -> list[ResearchSchedulerTask]:
        current = {
            task.task_id: (task.scheduler_run_id, task.dispatch_id)
            for task in record.external_tasks
        }
        return [
            task for task in tasks
            if task.status in {
                ResearchSchedulerTaskStatus.READY, ResearchSchedulerTaskStatus.PENDING,
            }
            and current.get(task.node_id) == (task.run_id, external_dispatch_id(task))
            and (selected_task_ids is None or task.node_id in selected_task_ids)
        ]

    def _waiting_external_task_ids(self, record: CurrentResearchContinuation) -> set[str]:
        run_ids = {task.scheduler_run_id for task in record.external_tasks if task.scheduler_run_id}
        scheduler = CurrentResearchSlaService(self.state, self.objects, clock=self.clock)
        scheduled = {
            (task.run_id, task.node_id): task
            for run_id in run_ids for task in scheduler.tasks(run_id)
        }
        waiting: set[str] = set()
        for task in record.external_tasks:
            dispatched = scheduled.get((task.scheduler_run_id or "", task.task_id))
            if (
                dispatched is not None
                and task.status is ExternalResearchTaskStatus.PENDING
                and task.resolution_hash is None
                and task.external_operation_id is not None
                and task.dispatch_id == external_dispatch_id(dispatched)
            ):
                waiting.add(task.task_id)
        return waiting

    def _bind_external_wave(
        self,
        record: CurrentResearchContinuation,
        scheduler: CurrentResearchSlaService,
    ) -> CurrentResearchContinuation:
        fresh = [task for task in self._pending_public_tasks(record) if not task.scheduler_run_id]
        if not fresh:
            return record
        # Frozen acquisition and resolution lineage distinguish retries after local
        # rejection without replacing a sibling's still-active host generation.
        wave_key = (
            f"{record.continuation_id}:round-{record.automatic_rounds_completed}:"
            + content_hash({
                "tasks": sorted(task.task_id for task in fresh),
                "acquisition": record.current_acquisition_report_artifact_id,
                "resolutions": record.automatic_resolution_artifact_ids,
            })
        )
        run_id = "research-scheduler:" + content_hash({"request_id": wave_key})
        run = scheduler.get_run(run_id)
        if run is None:
            if self.clock() >= record.deadline_at or self._budget(record).remaining_attempts() == 0:
                return record
            run = scheduler.create_run(
                request_id=wave_key,
                task_specs=[
                    (
                        task.task_id,
                        record.company_id,
                        ResearchTaskCategory.NETWORK,
                        [],
                        content_hash(
                            {
                                "capability": task.capability,
                                "question": task.research_question,
                                "company_id": record.company_id,
                                "lookback_days": record.lookback_days,
                                "acquisition": record.current_acquisition_report_artifact_id,
                            }
                        ),
                    )
                    for task in fresh
                ],
                budget_seconds=None,
                deadline_at=record.deadline_at,
            )
        if run.deadline_at != record.deadline_at:
            raise ValueError("external wave cannot change the original request deadline")
        dispatched = {task.node_id: task for task in scheduler.tasks(run.run_id)}
        fresh_ids = {task.task_id for task in fresh}
        if any(
            task_id not in dispatched
            or dispatched[task_id].status not in {
                ResearchSchedulerTaskStatus.PENDING, ResearchSchedulerTaskStatus.READY,
            }
            or dispatched[task_id].dispatch_attempted is not False
            for task_id in fresh_ids
        ):
            raise ValueError("external retry requires a fresh acquisition or resolution generation")
        # Commit the scheduler generation before the first external side effect.
        return self._persist(
            record.model_copy(
                update={
                    "external_tasks": [
                        task.model_copy(
                            update={
                                "dispatch_id": external_dispatch_id(dispatched[task.task_id]),
                                "scheduler_run_id": run.run_id,
                            }
                        )
                        if task.task_id in fresh_ids
                        else task
                        for task in record.external_tasks
                    ]
                }
            )
        )

    def _reconcile_external_wave(
        self,
        record: CurrentResearchContinuation,
        dispatched_tasks: tuple[ResearchSchedulerTask, ...],
    ) -> CurrentResearchContinuation:
        for dispatched in dispatched_tasks:
            task = next(
                (item for item in record.external_tasks if item.task_id == dispatched.node_id),
                None,
            )
            if task is None or task.scheduler_run_id != dispatched.run_id:
                continue
            if task.dispatch_id != external_dispatch_id(dispatched):
                raise ValueError("external wave generation differs from its frozen dispatch")
            if task.resolution_hash is not None:
                continue
            if dispatched.status is ResearchSchedulerTaskStatus.COMPLETED:
                record = self._apply_frozen_external_result(record, dispatched)
                continue
            if task.external_operation_id is not None and not dispatched.dispatch_attempted:
                # Preparing/cancelling the local PDF slot does not complete the host operation.
                continue
            if dispatched.error_code == "HANDLER_FAILED:RequestBudgetExhausted":
                failure = "REQUEST_BUDGET_EXHAUSTED"
            elif dispatched.dispatch_attempted is False or dispatched.status in {
                ResearchSchedulerTaskStatus.PENDING,
                ResearchSchedulerTaskStatus.READY,
            }:
                failure = "EXTERNAL_NOT_DISPATCHED"
            else:
                failure = "EXTERNAL_COMPLETION_UNKNOWN"
            record = record.model_copy(
                update={
                    "external_tasks": [
                        item.model_copy(update={"last_failure_code": failure})
                        if item.task_id == task.task_id
                        else item
                        for item in record.external_tasks
                    ]
                }
            )
        return self._persist(record)

    def _apply_frozen_external_result(
        self,
        record: CurrentResearchContinuation,
        dispatched: ResearchSchedulerTask,
    ) -> CurrentResearchContinuation:
        artifact_id = dispatched.result_artifact_id or ""
        prefix = "CurrentResearchAutomaticResolution:"
        if not artifact_id.startswith(prefix):
            raise ValueError("external worker must return a frozen automatic resolution")
        result_hash = artifact_id.removeprefix(prefix)
        if not self.objects.verify(result_hash):
            raise ValueError("external resolution object is absent or corrupted")
        result = CurrentResearchAutomaticResolution.model_validate_json(
            self.objects.get_bytes(result_hash)
        )
        if result.task_id != dispatched.node_id:
            raise ValueError("external resolution task identity differs from its dispatch")
        return self._apply_automatic_resolution(record, result)

    @staticmethod
    def _has_unknown_completion(record: CurrentResearchContinuation) -> bool:
        return any(
            task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
            and task.resolution_hash is None
            for task in record.external_tasks
        )

    def _validate_official_capture(
        self,
        record: CurrentResearchContinuation,
        task: CurrentResearchExternalTask,
        artifact_id: str,
    ) -> str:
        artifact_record = self.state.artifact_record(
            self._official_capture_artifact_id(artifact_id)
        )
        if (
            artifact_record is None
            or str(artifact_record["type"]) != "OfficialWebDocumentCapture"
            or not self.objects.verify(str(artifact_record["object_hash"]))
        ):
            raise ValueError("automatic resolution requires registered official Web captures")
        try:
            capture = OfficialWebDocumentCapture.model_validate_json(
                self.objects.get_bytes(str(artifact_record["object_hash"]))
            )
        except ValueError as exc:
            raise ValueError(
                "automatic resolution requires registered official Web captures"
            ) from exc
        document = self.documents.get_model(capture.document_id)
        if document is None or record.company_id not in document.company_ids:
            raise ValueError("official Web capture is not bound to the continuation company")
        if capture.requested_capability not in self._capture_capabilities(task.capability):
            raise ValueError("official Web capture capability does not match the continuation task")
        return str(artifact_record["object_hash"])

    @staticmethod
    def _status_task_ids(status: dict[str, object], key: str) -> tuple[str, ...]:
        raw = status.get(key)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError(f"company research-team status has invalid {key}")
        return tuple(raw)

    def bind_external_evidence(
        self, binding: CurrentResearchEvidenceBinding
    ) -> CurrentResearchContinuation:
        with self._coordinator(binding.continuation_id):
            return self._bind_external_evidence(binding)

    def _bind_external_evidence(
        self,
        binding: CurrentResearchEvidenceBinding,
    ) -> CurrentResearchContinuation:
        record = self._require(binding.continuation_id)
        resumable_gap = record.status in {
            CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
            CurrentResearchContinuationStatus.BUDGET_EXHAUSTED,
            CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE,
            CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
        }
        if (
            record.status is not CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
            and not resumable_gap
        ):
            raise ValueError("external evidence can only be bound to an active evidence gap")
        task = next(
            (item for item in record.external_tasks if item.task_id == binding.task_id),
            None,
        )
        if task is None:
            raise ValueError("continuation evidence binding references an unknown task")
        if task.status is ExternalResearchTaskStatus.RESOLVED:
            raise ValueError("resolved continuation task cannot accept new evidence")

        self._validate_official_capture(
            record,
            task,
            binding.capture_artifact_id,
        )

        updated_tasks = []
        for item in record.external_tasks:
            if item.task_id != task.task_id:
                updated_tasks.append(item)
                continue
            captures = sorted({*item.capture_artifact_ids, binding.capture_artifact_id})
            updated_tasks.append(
                item.model_copy(
                    update={
                        "status": ExternalResearchTaskStatus.EVIDENCE_BOUND,
                        "capture_artifact_ids": captures,
                        "last_failure_code": None,
                        "private_material_required": False,
                    }
                )
            )
        update: dict[str, object] = {
            "external_tasks": sorted(updated_tasks, key=lambda item: item.task_id)
        }
        if resumable_gap:
            update.update(
                {
                    "status": CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
                    "manual_actions": [],
                    "private_material_required": False,
                    "automatic_budget_exhausted": False,
                    "recovery_stop_reason": None,
                }
            )
        updated = record.model_copy(update=update)
        return self._persist(updated)

    def resume(self, continuation_id: str) -> CurrentResearchContinuation:
        with self._coordinator(continuation_id):
            return self._resume(continuation_id)

    def _resume(self, continuation_id: str) -> CurrentResearchContinuation:
        record = self._require(continuation_id)
        bound_evidence_available = any(
            task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND and task.capture_artifact_ids
            for task in record.external_tasks
        )
        waiting_external = self._waiting_external_task_ids(record)
        blocked_capabilities = frozenset(
            task.capability for task in record.external_tasks
            if task.task_id in waiting_external
            or (
                task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
                and task.resolution_hash is None
            )
        )
        runnable = any(
            task.capability not in blocked_capabilities
            for task in self._pending_public_tasks(record)
        )
        if record.status is not CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED:
            if record.status not in {
                CurrentResearchContinuationStatus.BUDGET_EXHAUSTED,
                CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN,
            } or not (
                bound_evidence_available
                or (
                    record.status is CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN
                    and runnable
                )
            ):
                return record
            record = record.model_copy(
                update={
                    "status": CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
                    "automatic_budget_exhausted": False,
                    "recovery_stop_reason": None,
                }
            )
        deadline_exhausted = self.clock() >= record.deadline_at
        rounds_exhausted = record.automatic_rounds_completed >= record.max_automatic_rounds
        budget = self._budget(record)
        budget_exhausted = (
            deadline_exhausted or rounds_exhausted or budget.remaining_attempts() == 0
        )
        stop_reason: Literal["TIME_BUDGET_EXHAUSTED", "ATTEMPT_BUDGET_EXHAUSTED"] = (
            "TIME_BUDGET_EXHAUSTED" if deadline_exhausted else "ATTEMPT_BUDGET_EXHAUSTED"
        )
        if not bound_evidence_available and not blocked_capabilities and not budget_exhausted:
            record = self._with_public_recovery_coverage(record)
            proof = record.public_recovery_coverage
            if proof is not None and proof.all_applicable_routes_completed_empty():
                if any(task.private_material_required for task in record.external_tasks):
                    return self._finish_private_material(record)
                return self._persist(record.model_copy(update={
                    "status": CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE,
                }))
        if budget_exhausted and not bound_evidence_available:
            return self._finish_recovery(record, stop_reason)
        if not bound_evidence_available and not runnable:
            return record if blocked_capabilities else self._finish_private_material(record)

        trusted_identity_capture_ids = tuple(
            sorted(
                {
                    capture_id
                    for task in record.external_tasks
                    for capture_id in task.capture_artifact_ids
                }
            )
        )
        allow_network = not budget_exhausted and (runnable or not blocked_capabilities)
        with budget.bind():
            report = self.acquisition.acquire(
                record.company_id,
                record.market,
                lookback_days=record.lookback_days,
                planner_plan_artifact_id=record.planner_plan_artifact_id,
                reuse_report_artifact_id=record.current_acquisition_report_artifact_id,
                trusted_identity_capture_ids=trusted_identity_capture_ids,
                allow_network=allow_network,
                network_blocked_capabilities=blocked_capabilities,
            )
        # Budget limits stop new network attempts, not consumption of frozen
        # evidence. The existing acquisition pipeline reconciles locally after
        # either limit, without spending another resolver round or using fixtures.
        next_round = (
            record.automatic_rounds_completed
            if not allow_network
            else record.automatic_rounds_completed + 1
        )
        tasks = self._reconcile_tasks(record, report, automatic_round=next_round)
        history = list(dict.fromkeys([*record.acquisition_report_artifact_ids, report.report_id]))
        if tasks:
            updated = record.model_copy(
                update={
                    "automatic_rounds_completed": next_round,
                    "acquisition_report_artifact_ids": history,
                    "current_acquisition_report_artifact_id": report.report_id,
                    "external_tasks": tasks,
                }
            )
            if self._has_unknown_completion(updated):
                updated = updated.model_copy(
                    update={"status": CurrentResearchContinuationStatus.EXTERNAL_COMPLETION_UNKNOWN}
                )
            updated = self._persist(updated)
            if self._has_unknown_completion(updated):
                return updated
            if budget_exhausted:
                return self._finish_recovery(updated, stop_reason)
            return (
                updated
                if self._pending_public_tasks(updated)
                else self._finish_private_material(updated)
            )

        plan = self.team.create_company_plan(
            company_id=record.company_id,
            acquisition_report_artifact_id=report.report_id,
            as_of=report.decision_as_of,
        )
        updated = record.model_copy(
            update={
                "status": CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED,
                "automatic_rounds_completed": next_round,
                "acquisition_report_artifact_ids": history,
                "current_acquisition_report_artifact_id": report.report_id,
                "external_tasks": self._resolved_task_history(record.external_tasks),
                "team_plan_id": plan.plan_id,
            }
        )
        return self._persist(updated)

    def advance_team(self, continuation_id: str) -> CurrentResearchContinuation:
        with self._coordinator(continuation_id):
            return self._advance_team(continuation_id)

    def _advance_team(self, continuation_id: str) -> CurrentResearchContinuation:
        record = self._require(continuation_id)
        if record.status is not CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED:
            return record
        if record.team_plan_id is None:
            raise ValueError("team continuation is missing its plan")
        plan = self.team.get_plan(record.team_plan_id)
        if plan is None:
            raise ValueError("company research-team plan is unavailable")
        status = self.team.status(plan.plan_id)
        raw_completed = status.get("completed_tasks")
        if not isinstance(raw_completed, list) or any(
            not isinstance(item, str) for item in raw_completed
        ):
            raise ValueError("company research-team status has invalid completed tasks")
        completed = set(raw_completed)
        required_task_ids = {
            task.task_id for task in plan.tasks if task.required_for_recommendation
        }
        if not required_task_ids.issubset(completed):
            return record

        readiness = self.team.evaluate_readiness(
            FullResearchInputReadinessRequest(
                plan_id=plan.plan_id,
                checks={},
                created_at=self.clock(),
            )
        )
        readiness_artifact_id = f"FullResearchInputReadinessReport:{readiness.report_id}"
        ready = readiness.full_research_input_ready
        updated = record.model_copy(
            update={
                "status": (
                    CurrentResearchContinuationStatus.READY_FOR_INVESTOR_VIEW
                    if ready
                    else CurrentResearchContinuationStatus.OBSERVATION_ONLY_FOR_INVESTOR_VIEW
                ),
                "readiness_report_artifact_id": readiness_artifact_id,
                "investor_view_allowed": True,
                "full_research_input_ready": ready,
            }
        )
        return self._persist(updated)

    @staticmethod
    def _project_legacy_budget_stop(payload: object) -> object:
        """Read old ambiguous gap states without rewriting history or granting proof."""
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == "current-research-continuation-v1"
            and payload.get("status") == "PUBLIC_DATA_UNAVAILABLE"
            and payload.get("automatic_budget_exhausted") is True
            and payload.get("public_recovery_coverage") is None
        ):
            return {
                **payload, "status": "BUDGET_EXHAUSTED",
                "recovery_stop_reason": (
                    payload.get("recovery_stop_reason") or "LEGACY_BUDGET_EXHAUSTED"
                ),
            }
        return payload

    def get(self, continuation_id: str) -> CurrentResearchContinuation | None:
        checkpoint = self.state.get_checkpoint("current-research-continuation", continuation_id)
        if checkpoint is None or not checkpoint.get("object_hash"):
            return None
        object_hash = str(checkpoint["object_hash"])
        if not self.objects.verify(object_hash):
            return None
        payload = json.loads(self.objects.get_bytes(object_hash))
        return CurrentResearchContinuation.model_validate(self._project_legacy_budget_stop(payload))

    def status(self, continuation_id: str) -> dict[str, object]:
        record = self.get(continuation_id)
        if record is None:
            return {"status": "NOT_FOUND", "continuation_id": continuation_id}
        team_status: dict[str, object] | None = None
        if record.team_plan_id is not None:
            team_status = self.team.status(record.team_plan_id)
        return {
            "status": record.status.value,
            "continuation": record.model_dump(mode="json"),
            "team_status": team_status,
            "same_request_continuation_required": record.status
            in {
                CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
                CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED,
            },
            "investment_conclusion_blocked": not record.investor_view_allowed,
            "user_assistance_request_allowed": (
                record.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
                and record.private_material_required
            ),
            "public_data_unavailable": (
                record.status is CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE
            ),
            "automatic_budget_exhausted": record.automatic_budget_exhausted,
            "recovery_stop_reason": record.recovery_stop_reason,
            "public_recovery_coverage": (
                record.public_recovery_coverage.model_dump(mode="json")
                if record.public_recovery_coverage is not None else None
            ),
            "investor_view_allowed": record.investor_view_allowed,
            "full_research_input_ready": record.full_research_input_ready,
        }

    def _require(self, continuation_id: str) -> CurrentResearchContinuation:
        record = self.get(continuation_id)
        if record is None:
            raise ValueError("unknown current research continuation")
        if (
            record.automatic_resolution_budget_seconds
            > self.policy.automatic_resolution_budget_seconds
        ):
            raise ValueError("stored continuation exceeds the active recovery budget")
        return record

    @staticmethod
    def _continuation_id(request: CurrentResearchContinuationRequest) -> str:
        identity = request.model_dump(mode="json", exclude={"created_at"})
        # Preserve the original request identity for pre-budget v1 checkpoints.
        if request.max_transport_attempts == 64:
            identity.pop("max_transport_attempts")
        return "current-research-continuation:" + content_hash(identity)

    @staticmethod
    def _official_capture_artifact_id(capture_id: str) -> str:
        return (
            capture_id
            if capture_id.startswith("OfficialWebDocumentCapture:")
            else f"OfficialWebDocumentCapture:{capture_id}"
        )

    @staticmethod
    def _capture_capabilities(capability: AcquisitionCapability) -> frozenset[str]:
        if capability in {
            AcquisitionCapability.FINANCIAL_ANNUAL,
            AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
        }:
            return frozenset({"financial.official_document"})
        if capability in {
            AcquisitionCapability.INSTRUMENT_IDENTITY,
            AcquisitionCapability.CORPORATE_ACTIONS,
        }:
            return frozenset({"disclosure.document"})
        return frozenset()

    @staticmethod
    def _task_from_need(
        continuation_id: str,
        need: ExternalResearchNeed,
        *,
        automatic_round: int,
    ) -> CurrentResearchExternalTask:
        task_id = "current-research-external-task:" + content_hash(
            {
                "continuation_id": continuation_id,
                "capability": need.capability.value,
                "research_question": need.research_question,
            }
        )
        return CurrentResearchExternalTask(
            task_id=task_id,
            capability=need.capability,
            research_question=need.research_question,
            preferred_authorities=sorted(need.preferred_authorities, key=lambda item: item.value),
            automatic_rounds_attempted=automatic_round,
            created_at=need.created_at,
        )

    def _reconcile_tasks(
        self,
        record: CurrentResearchContinuation,
        report: CurrentResearchAcquisitionReport,
        *,
        automatic_round: int,
    ) -> list[CurrentResearchExternalTask]:
        needs = {item.capability: item for item in report.external_research_needs}
        existing = {item.capability: item for item in record.external_tasks}
        waiting_ids = self._waiting_external_task_ids(record)
        tasks: list[CurrentResearchExternalTask] = []
        for capability, task in existing.items():
            need = needs.pop(capability, None)
            if need is None:
                tasks.append(
                    task.model_copy(update={"status": ExternalResearchTaskStatus.RESOLVED})
                )
                continue
            if (
                task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN"
                and task.resolution_hash is None
            ):
                # Local consumption of a sibling cannot reset this in-flight generation.
                tasks.append(task)
                continue
            if task.task_id in waiting_ids:
                # Only a real matching external operation retains the old generation.
                tasks.append(task)
                continue
            tasks.append(
                task.model_copy(
                    update={
                        "status": ExternalResearchTaskStatus.PENDING,
                        "research_question": need.research_question,
                        "preferred_authorities": sorted(
                            need.preferred_authorities,
                            key=lambda item: item.value,
                        ),
                        "automatic_rounds_attempted": automatic_round,
                        "resolution_hash": None,
                        "external_operation_id": None,
                        "dispatch_id": None,
                        "scheduler_run_id": None,
                        "last_failure_code": (
                            task.last_failure_code
                            if task.private_material_required
                            else (
                                "BOUND_EVIDENCE_NOT_ACCEPTED_BY_ACQUISITION"
                                if task.capture_artifact_ids
                                else "AUTOMATIC_SOURCE_STILL_UNAVAILABLE"
                            )
                        ),
                    }
                )
            )
        for need in needs.values():
            tasks.append(
                self._task_from_need(
                    record.continuation_id,
                    need,
                    automatic_round=automatic_round,
                )
            )
        return sorted(
            [item for item in tasks if item.status is not ExternalResearchTaskStatus.RESOLVED],
            key=lambda item: item.task_id,
        )

    @staticmethod
    def _resolved_task_history(
        tasks: list[CurrentResearchExternalTask],
    ) -> list[CurrentResearchExternalTask]:
        return sorted(
            [
                item.model_copy(
                    update={
                        "status": ExternalResearchTaskStatus.RESOLVED,
                        "private_material_required": False,
                    }
                )
                for item in tasks
            ],
            key=lambda item: item.task_id,
        )

    def _pending_public_tasks(
        self,
        record: CurrentResearchContinuation,
    ) -> tuple[CurrentResearchExternalTask, ...]:
        unresolved = {
            task.capability
            for task in record.external_tasks
            if task.status is not ExternalResearchTaskStatus.RESOLVED
        }
        return tuple(
            task
            for task in record.external_tasks
            if task.status is ExternalResearchTaskStatus.PENDING
            and not task.private_material_required
            and not unresolved.intersection(self.policy.capabilities[task.capability].dependencies)
        )

    def _private_actions(
        self,
        record: CurrentResearchContinuation,
    ) -> list[ManualResearchAction]:
        return [
            ManualResearchAction(
                capability=task.capability,
                instruction=task.research_question,
                why_needed=(
                    "This capability requires identified private material or authorization; "
                    "independent public evidence has been processed separately."
                ),
                created_at=self.clock(),
            )
            for task in record.external_tasks
            if task.private_material_required
            and task.status is not ExternalResearchTaskStatus.RESOLVED
        ]

    def _finish_private_material(
        self,
        record: CurrentResearchContinuation,
    ) -> CurrentResearchContinuation:
        actions = self._private_actions(record)
        if not actions:
            raise ValueError("private-input closure requires an identified private gap")
        return self._persist(
            record.model_copy(
                update={
                    "status": CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
                    "manual_actions": actions,
                    "automatic_budget_exhausted": False,
                    "recovery_stop_reason": None,
                    "private_material_required": True,
                }
            )
        )

    def _with_public_recovery_coverage(
        self, record: CurrentResearchContinuation,
    ) -> CurrentResearchContinuation:
        from astock.research.recovery_evidence import build_public_recovery_coverage

        try:
            proof = build_public_recovery_coverage(self.state, self.objects, record)
        except (OSError, ValueError, KeyError, TypeError):
            # Missing receipts cannot justify stopping independent automatic work.
            proof = None
        return record.model_copy(update={"public_recovery_coverage": proof})

    def _finish_recovery(
        self,
        record: CurrentResearchContinuation,
        reason: Literal["TIME_BUDGET_EXHAUSTED", "ATTEMPT_BUDGET_EXHAUSTED"],
    ) -> CurrentResearchContinuation:
        record = self._with_public_recovery_coverage(record)
        # A local resource limit is not evidence that public sources are exhausted.
        # Keep the original task/capture lineage so later evidence can resume it.
        actions = self._private_actions(record)
        return self._persist(
            record.model_copy(
                update={
                    "status": (
                        CurrentResearchContinuationStatus.NEEDS_USER_INPUT
                        if actions
                        else CurrentResearchContinuationStatus.BUDGET_EXHAUSTED
                    ),
                    "manual_actions": actions,
                    "automatic_budget_exhausted": True,
                    "recovery_stop_reason": reason,
                    "private_material_required": bool(actions),
                }
            )
        )

    def _persist(
        self,
        record: CurrentResearchContinuation,
    ) -> CurrentResearchContinuation:
        record = CurrentResearchContinuation.model_validate(record.model_dump())
        previous = self.state.get_checkpoint(
            "current-research-continuation",
            record.continuation_id,
        )
        ref = self.objects.put_json(record.model_dump(mode="json"))
        if previous is not None and str(previous.get("object_hash") or "") == ref.sha256:
            return record
        artifact_id = f"CurrentResearchContinuation:{record.continuation_id}:{ref.sha256}"
        input_hashes: set[str] = set()
        request_record = self.state.artifact_record(
            f"CurrentResearchContinuationRequest:{record.continuation_id}"
        )
        if request_record is not None:
            input_hashes.add(str(request_record["object_hash"]))
        if previous is not None and previous.get("object_hash"):
            input_hashes.add(str(previous["object_hash"]))
        for report_id in record.acquisition_report_artifact_ids:
            report_record = self.state.artifact_record(report_id)
            if report_record is not None:
                input_hashes.add(str(report_record["object_hash"]))
        for resolution_id in record.automatic_resolution_artifact_ids:
            resolution_record = self.state.artifact_record(resolution_id)
            if resolution_record is not None:
                input_hashes.add(str(resolution_record["object_hash"]))
        if record.public_recovery_coverage is not None:
            input_hashes.update(record.public_recovery_coverage.source_object_hashes)
        for task in record.external_tasks:
            for capture_artifact_id in task.capture_artifact_ids:
                capture_record = self.state.artifact_record(
                    self._official_capture_artifact_id(capture_artifact_id)
                )
                if capture_record is not None:
                    input_hashes.add(str(capture_record["object_hash"]))
        if record.team_plan_id is not None:
            plan_record = self.state.artifact_record(f"ResearchTeamPlan:{record.team_plan_id}")
            if plan_record is not None:
                input_hashes.add(str(plan_record["object_hash"]))
        if record.readiness_report_artifact_id is not None:
            readiness_record = self.state.artifact_record(record.readiness_report_artifact_id)
            if readiness_record is not None:
                input_hashes.add(str(readiness_record["object_hash"]))
        input_hashes.discard(ref.sha256)
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="CurrentResearchContinuation",
            schema_version=record.schema_version,
            object_hash=ref.sha256,
            input_hashes=sorted(input_hashes),
        )
        self.state.set_checkpoint(
            scope_type="current-research-continuation",
            scope_key=record.continuation_id,
            cursor={
                "artifact_id": artifact_id,
                "status": record.status.value,
                "request_id": record.request_id,
                "company_id": record.company_id,
                "market": record.market.value,
                "current_acquisition_report_artifact_id": (
                    record.current_acquisition_report_artifact_id
                ),
                "team_plan_id": record.team_plan_id,
                "investor_view_allowed": record.investor_view_allowed,
            },
            status=record.status.value,
            object_hash=ref.sha256,
        )
        emit_operational_event(
            component="current_research_continuation",
            event="current_research_continuation_checkpointed",
            severity=(
                OperationalSeverity.WARNING
                if record.status
                in {
                    CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
                    CurrentResearchContinuationStatus.FAILED,
                    CurrentResearchContinuationStatus.PUBLIC_DATA_UNAVAILABLE,
                    CurrentResearchContinuationStatus.BUDGET_EXHAUSTED,
                }
                else OperationalSeverity.INFO
            ),
            run_id=record.continuation_id,
            request_id=record.request_id,
            context={
                "company_id": record.company_id,
                "market": record.market.value,
                "status": record.status.value,
                "automatic_resolution_budget_seconds": (record.automatic_resolution_budget_seconds),
                "automatic_rounds_completed": record.automatic_rounds_completed,
                "pending_external_task_count": sum(
                    item.status is ExternalResearchTaskStatus.PENDING
                    for item in record.external_tasks
                ),
                "automatic_budget_exhausted": record.automatic_budget_exhausted,
                "recovery_stop_reason": record.recovery_stop_reason,
                "investor_view_allowed": record.investor_view_allowed,
                "full_research_input_ready": record.full_research_input_ready,
            },
        )
        return record


def external_dispatch_id(task: ResearchSchedulerTask) -> str:
    """Identity of this existing scheduler task generation, frozen before transport."""
    return f"{task.task_id}:gen:{task.generation}"


class _BudgetedExternalHandler:
    def __init__(self, handler: CancellableTaskHandler, budget: RequestExecutionBudget) -> None:
        self.handler = handler
        self.budget = budget

    def __call__(self, task: ResearchSchedulerTask) -> str:
        with self.budget.bind():
            return self.handler(task)

    def cancel(self) -> None:
        self.handler.cancel()


__all__ = [
    "CurrentResearchContinuationService",
    "CurrentResearchExternalResolver",
    "CurrentResearchTeamExecutor",
    "external_dispatch_id",
]
