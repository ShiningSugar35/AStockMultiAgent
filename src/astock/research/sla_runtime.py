"""Bounded current-research scheduler, incremental target pool, and LLM takeover handoff."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable
from uuid import uuid4

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.run_ownership import schedule_run_ownership
from astock.schemas.research_sla import (
    IncrementalRefreshPlan,
    LlmTakeoverPacket,
    LlmTakeoverResult,
    LlmTakeoverResultStatus,
    ResearchSchedulerRun,
    ResearchSchedulerRunStatus,
    ResearchSchedulerTask,
    ResearchSchedulerTaskStatus,
    ResearchTargetRecord,
    ResearchTargetState,
    ResearchTaskCategory,
)

Clock = Callable[[], datetime]
TaskHandler = Callable[[ResearchSchedulerTask], str]
TakeoverResultValidator = Callable[[str], None]


@runtime_checkable
class CancellableTaskHandler(Protocol):
    """An owned blocking handler must confirm termination before cancel returns."""

    def __call__(self, task: ResearchSchedulerTask) -> str: ...

    def cancel(self) -> None: ...


_DEFAULT_LIMITS: dict[ResearchTaskCategory, int] = {
    ResearchTaskCategory.NETWORK: 4,
    ResearchTaskCategory.CPU: 2,
    ResearchTaskCategory.LLM: 1,
    ResearchTaskCategory.WRITE: 1,
}


class CurrentResearchSlaService:
    """Own durable target state and a deadline-aware ready-task graph.

    The tables are latest-state projections. Every mutation is also materialized in
    the ObjectStore, so a crash can resume from SQLite without making SQLite a second
    source of research facts.
    """

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ targets
    def upsert_target(
        self,
        *,
        instrument_id: str,
        company_id: str,
        state: ResearchTargetState,
        source_reason: str,
        dependency_fingerprint: str,
        module_versions: Mapping[str, str],
        triggers: Sequence[str] = (),
        source_artifact_id: str | None = None,
        latest_result_artifact_id: str | None = None,
        priority: int = 0,
        invalidation_reason: str | None = None,
        last_review_at: datetime | None = None,
        next_review_at: datetime | None = None,
    ) -> ResearchTargetRecord:
        with schedule_run_ownership(self.state.path, "research-target", instrument_id):
            now = self.clock()
            existing = self.target(instrument_id)
            normalized_module_versions = dict(sorted(module_versions.items()))
            normalized_triggers = sorted(set(triggers))
            if existing is not None and (
                existing.company_id == company_id
                and existing.state is state
                and existing.source_reason == source_reason
                and existing.source_artifact_id == source_artifact_id
                and existing.latest_result_artifact_id == latest_result_artifact_id
                and existing.dependency_fingerprint == dependency_fingerprint
                and existing.module_versions == normalized_module_versions
                and existing.triggers == normalized_triggers
                and existing.priority == priority
                and existing.invalidation_reason == invalidation_reason
                and existing.last_review_at == last_review_at
                and existing.next_review_at == next_review_at
            ):
                # A previous attempt may have committed the projection before sync failed.
                self._sync_subject_registry(existing)
                return existing
            created_at = existing.created_at if existing is not None else now
            target_id = (
                existing.target_id
                if existing is not None
                else f"research-target:{content_hash({'instrument_id': instrument_id})}"
            )
            record = ResearchTargetRecord(
                target_id=target_id,
                instrument_id=instrument_id,
                company_id=company_id,
                state=state,
                source_reason=source_reason,
                source_artifact_id=source_artifact_id,
                latest_result_artifact_id=latest_result_artifact_id,
                dependency_fingerprint=dependency_fingerprint,
                module_versions=dict(sorted(module_versions.items())),
                triggers=sorted(set(triggers)),
                priority=priority,
                invalidation_reason=invalidation_reason,
                last_review_at=last_review_at,
                next_review_at=next_review_at,
                created_at=created_at,
                updated_at=now,
            )
            ref = self.objects.put_json(record.model_dump(mode="json"))
            with self.state.transaction() as connection:
                row = connection.execute(
                    "SELECT target_id,company_id FROM research_target_state WHERE instrument_id=?",
                    (instrument_id,),
                ).fetchone()
                if row is not None and str(row["target_id"]) != target_id:
                    raise ValueError("research target instrument identity collision")
                if row is not None and str(row["company_id"]) != company_id:
                    raise ValueError("research target cannot change company identity")
                connection.execute(
                    "INSERT INTO research_target_state("
                    "target_id,instrument_id,company_id,state,source_reason,source_artifact_id,"
                    "latest_result_artifact_id,dependency_fingerprint,module_versions_json,triggers_json,"
                    "priority,invalidation_reason,last_review_at,next_review_at,object_hash,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_id) DO UPDATE SET "
                    "state=excluded.state,source_reason=excluded.source_reason,"
                    "source_artifact_id=excluded.source_artifact_id,"
                    "latest_result_artifact_id=excluded.latest_result_artifact_id,"
                    "dependency_fingerprint=excluded.dependency_fingerprint,"
                    "module_versions_json=excluded.module_versions_json,triggers_json=excluded.triggers_json,"
                    "priority=excluded.priority,invalidation_reason=excluded.invalidation_reason,"
                    "last_review_at=excluded.last_review_at,next_review_at=excluded.next_review_at,"
                    "object_hash=excluded.object_hash,updated_at=excluded.updated_at",
                    (
                        target_id,
                        instrument_id,
                        company_id,
                        record.state.value,
                        source_reason,
                        source_artifact_id,
                        latest_result_artifact_id,
                        dependency_fingerprint,
                        json.dumps(record.module_versions, ensure_ascii=False, sort_keys=True),
                        json.dumps(record.triggers, ensure_ascii=False),
                        priority,
                        invalidation_reason,
                        last_review_at.isoformat() if last_review_at else None,
                        next_review_at.isoformat() if next_review_at else None,
                        ref.sha256,
                        created_at.isoformat(),
                        now.isoformat(),
                    ),
                )
            self._sync_subject_registry(record)
            return record

    def _sync_subject_registry(self, record: ResearchTargetRecord) -> None:
        """Mirror the target projection into the existing canonical subject event lane."""

        from astock.investor_orchestration.models import PortfolioLane, SubjectEventKind
        from astock.investor_orchestration.store import InvestorOrchestrationStore
        from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

        registry = ResearchSubjectRegistryService(InvestorOrchestrationStore(self.state.path))
        if record.state is ResearchTargetState.EXITED:
            event_type = SubjectEventKind.WATCHLIST_REMOVED
        else:
            event_type = (
                SubjectEventKind.WATCHLIST_ADDED
                if record.created_at == record.updated_at
                else SubjectEventKind.WATCHLIST_UPDATED
            )
        registry.append(
            instrument_id=record.instrument_id,
            event_type=event_type,
            lane=PortfolioLane.WATCHLIST,
            artifact_id=record.latest_result_artifact_id or record.source_artifact_id,
            reason=record.source_reason,
            available_at=record.updated_at,
            metadata={
                "target_id": record.target_id,
                "company_id": record.company_id,
                "target_state": record.state.value,
                "dependency_fingerprint": record.dependency_fingerprint,
                "priority": record.priority,
                "module_versions": record.module_versions,
                "triggers": record.triggers,
                "invalidation_reason": record.invalidation_reason,
                "last_review_at": record.last_review_at.isoformat()
                if record.last_review_at
                else None,
                "next_review_at": record.next_review_at.isoformat()
                if record.next_review_at
                else None,
            },
            idempotency_key=content_hash(record.model_dump(mode="json")),
        )

    def target(self, instrument_id: str) -> ResearchTargetRecord | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM research_target_state WHERE instrument_id=?",
                (instrument_id,),
            ).fetchone()
        if row is None:
            return None
        return ResearchTargetRecord.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )

    def active_targets(self, *, limit: int = 100) -> tuple[ResearchTargetRecord, ...]:
        if limit <= 0:
            raise ValueError("target limit must be positive")
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT object_hash FROM research_target_state "
                "WHERE state!='EXITED' ORDER BY priority DESC,"
                "COALESCE(next_review_at,''),target_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            ResearchTargetRecord.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        )

    def incremental_plan(
        self,
        target: ResearchTargetRecord,
        *,
        current_versions: Mapping[str, str],
        dependencies: Mapping[str, Sequence[str]],
    ) -> IncrementalRefreshPlan:
        current = dict(current_versions)
        changed = {
            key
            for key in set(target.module_versions) | set(current)
            if target.module_versions.get(key) != current.get(key)
        }
        reverse: dict[str, set[str]] = {}
        for module, parents in dependencies.items():
            for parent in parents:
                reverse.setdefault(parent, set()).add(module)
        rerun = set(changed)
        frontier = list(changed)
        while frontier:
            parent = frontier.pop()
            for child in reverse.get(parent, ()):
                if child not in rerun:
                    rerun.add(child)
                    frontier.append(child)
        known = set(target.module_versions) | set(current) | set(dependencies)
        unchanged = known - rerun
        reasons = [f"VERSION_CHANGED:{item}" for item in sorted(changed)]
        if not changed:
            reasons.append("NO_MATERIAL_DEPENDENCY_CHANGE")
        return IncrementalRefreshPlan(
            target_id=target.target_id,
            changed_modules=sorted(changed),
            rerun_modules=sorted(rerun),
            unchanged_modules=sorted(unchanged),
            reason_codes=sorted(reasons),
        )

    # --------------------------------------------------------------- scheduler
    def create_run(
        self,
        *,
        request_id: str,
        task_specs: Sequence[tuple[str, str | None, ResearchTaskCategory, Sequence[str], str]],
        budget_seconds: int = 2700,
        legacy_batch_caller: str | None = None,
        legacy_batch_explicit: bool = True,
    ) -> ResearchSchedulerRun:
        if type(budget_seconds) is not int or budget_seconds <= 0:
            raise ValueError("research budget must be a positive integer")
        if legacy_batch_caller is not None and not legacy_batch_caller.strip():
            raise ValueError("legacy batch caller identity must not be blank")
        now = self.clock()
        run_id = f"research-scheduler:{content_hash({'request_id': request_id})}"
        run = ResearchSchedulerRun(
            run_id=run_id,
            request_id=request_id,
            status=ResearchSchedulerRunStatus.RUNNING,
            generation=1,
            deadline_at=now + timedelta(seconds=budget_seconds),
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        run_ref = self.objects.put_json(run.model_dump(mode="json"))
        identities: dict[tuple[str, str | None], str] = {}
        for node_id, candidate_id, _category, _dependencies, input_fingerprint in task_specs:
            key = (node_id, candidate_id)
            if key in identities:
                raise ValueError("research scheduler node/candidate identity must be unique")
            task_identity = content_hash(
                {
                    "run": run_id,
                    "node": node_id,
                    "candidate": candidate_id,
                    "input": input_fingerprint,
                }
            )
            identities[key] = f"research-task:{task_identity}"
        tasks: list[ResearchSchedulerTask] = []
        for node_id, candidate_id, category, dependencies, input_fingerprint in task_specs:
            resolved_dependencies: list[str] = []
            for dependency_node in dependencies:
                dependency_id = identities.get((dependency_node, candidate_id)) or identities.get(
                    (dependency_node, None)
                )
                if dependency_id is not None:
                    resolved_dependencies.append(dependency_id)
                    continue
                # A shared reducer without a shared dependency consumes every
                # candidate-specific producer of that node, in deterministic order.
                candidate_dependencies = sorted(
                    task_id
                    for (producer, candidate), task_id in identities.items()
                    if candidate_id is None
                    and producer == dependency_node
                    and candidate is not None
                )
                if not candidate_dependencies:
                    raise ValueError(
                        f"research task has unknown dependency node: {dependency_node}"
                    )
                resolved_dependencies.extend(candidate_dependencies)
            task_id = identities[(node_id, candidate_id)]
            tasks.append(
                ResearchSchedulerTask(
                    task_id=task_id,
                    run_id=run_id,
                    node_id=node_id,
                    candidate_id=candidate_id,
                    category=category,
                    status=(
                        ResearchSchedulerTaskStatus.READY
                        if not resolved_dependencies
                        else ResearchSchedulerTaskStatus.PENDING
                    ),
                    dependencies=sorted(set(resolved_dependencies)),
                    input_fingerprint=input_fingerprint,
                    generation=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        self._validate_task_graph(tasks)
        if legacy_batch_caller is not None:
            self._batch_input_contract(tasks)
        with self.state.transaction() as connection:
            existing_row = connection.execute(
                "SELECT object_hash FROM research_scheduler_run WHERE request_id=?", (request_id,)
            ).fetchone()
            if legacy_batch_caller is not None:
                legacy = self._compatible_legacy_batch(
                    connection,
                    caller=legacy_batch_caller,
                    tasks=tasks,
                    budget_seconds=budget_seconds,
                    explicit=legacy_batch_explicit,
                )
                if legacy is not None:
                    if existing_row is not None:
                        raise ValueError("ambiguous legacy and current batch request graphs")
                    return legacy
            if existing_row is not None:
                existing = ResearchSchedulerRun.model_validate_json(
                    self.objects.get_bytes(str(existing_row["object_hash"]))
                )
                old_tasks = self._tasks_from_connection(connection, existing.run_id)
                if (
                    existing.deadline_at - existing.started_at
                ).total_seconds() != budget_seconds or self._task_contract(
                    old_tasks
                ) != self._task_contract(tasks):
                    raise ValueError(
                        "research request contract differs from its registered graph or budget"
                    )
                return existing
            connection.execute(
                "INSERT INTO research_scheduler_run("
                "run_id,request_id,status,generation,deadline_at,started_at,finished_at,"
                "object_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    request_id,
                    run.status.value,
                    run.generation,
                    run.deadline_at.isoformat(),
                    run.started_at.isoformat(),
                    None,
                    run_ref.sha256,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for task in tasks:
                ref = self.objects.put_json(task.model_dump(mode="json"))
                self._insert_task(connection, task, ref.sha256)
        return run

    @staticmethod
    def _batch_input_contract(
        tasks: Sequence[ResearchSchedulerTask],
    ) -> tuple[tuple[str, str, str], ...]:
        """Legacy compatibility is only for independent, one-company network nodes."""
        candidates = [task.candidate_id for task in tasks]
        if (
            not tasks
            or None in candidates
            or len(set(candidates)) != len(candidates)
            or any(task.dependencies or task.category is not ResearchTaskCategory.NETWORK
                   for task in tasks)
        ):
            raise ValueError("legacy batch compatibility requires independent unique companies")
        return tuple(sorted(
            (str(task.candidate_id), task.category.value, task.input_fingerprint)
            for task in tasks
        ))

    def _compatible_legacy_batch(
        self,
        connection: sqlite3.Connection,
        *,
        caller: str,
        tasks: Sequence[ResearchSchedulerTask],
        budget_seconds: int,
        explicit: bool,
    ) -> ResearchSchedulerRun | None:
        """Read old caller:hash graphs under the same transaction as create_run.

        Keep old node IDs, successes, leases and deadline unchanged. An explicit
        caller cannot fork its contract; implicit families may contain unrelated
        requests. Ambiguous old forks require resolution, never a fresh budget.
        """
        prefix = caller + ":"
        rows = connection.execute(
            "SELECT run_id,request_id,object_hash FROM research_scheduler_run "
            "WHERE substr(request_id,1,?)=? AND length(request_id)=?",
            (len(prefix), prefix, len(prefix) + 64),
        ).fetchall()
        wanted = self._batch_input_contract(tasks)
        matching: list[ResearchSchedulerRun] = []
        for row in rows:
            suffix = str(row["request_id"])[len(prefix):]
            if len(suffix) != 64 or any(char not in "0123456789abcdef" for char in suffix):
                continue
            existing = ResearchSchedulerRun.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            old_tasks = self._tasks_from_connection(connection, existing.run_id)
            if any(not task.node_id.startswith("company-run-") or
                   not task.node_id.removeprefix("company-run-").isascii() or
                   not task.node_id.removeprefix("company-run-").isdigit()
                   for task in old_tasks):
                if explicit:
                    raise ValueError("legacy batch request contract has non-batch task nodes")
                continue
            old_contract = self._batch_input_contract(old_tasks)
            if old_contract != wanted:
                if explicit:
                    raise ValueError("research request contract differs from its legacy graph")
                continue
            if (existing.deadline_at - existing.started_at).total_seconds() != budget_seconds:
                raise ValueError("research request contract differs from its legacy budget")
            matching.append(existing)
        if len(matching) > 1:
            raise ValueError("ambiguous legacy batch request graphs; original deadlines preserved")
        return matching[0] if matching else None

    def tasks(self, run_id: str) -> tuple[ResearchSchedulerTask, ...]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT object_hash FROM research_scheduler_task WHERE run_id=? ORDER BY task_id",
                (run_id,),
            ).fetchall()
        return tuple(
            ResearchSchedulerTask.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        )

    def execute(
        self,
        run_id: str,
        handlers: Mapping[str, TaskHandler],
        *,
        resource_limits: Mapping[ResearchTaskCategory, int] | None = None,
    ) -> ResearchSchedulerRun:
        with schedule_run_ownership(self.state.path, "current-research-scheduler", run_id):
            return self._execute_owned(run_id, handlers, resource_limits=resource_limits)

    def _execute_owned(
        self,
        run_id: str,
        handlers: Mapping[str, TaskHandler],
        *,
        resource_limits: Mapping[ResearchTaskCategory, int] | None = None,
    ) -> ResearchSchedulerRun:
        run = self._run(run_id)
        if run.status not in {
            ResearchSchedulerRunStatus.PENDING,
            ResearchSchedulerRunStatus.RUNNING,
        }:
            return run
        self._recover_expired_tasks_owned(run_id)
        limits = dict(_DEFAULT_LIMITS)
        if resource_limits is not None:
            for category, value in resource_limits.items():
                if value <= 0:
                    raise ValueError("research resource limits must be positive")
                limits[category] = value
        pool = ThreadPoolExecutor(
            max_workers=sum(limits.values()), thread_name_prefix="astock-research"
        )
        running: dict[Future[str], ResearchSchedulerTask] = {}
        running_by_category = {category: 0 for category in ResearchTaskCategory}
        run = self._run(run_id)
        task_state = {task.task_id: task for task in self.tasks(run_id)}
        unowned_task_ids = {
            task.task_id
            for task in task_state.values()
            if task.status is ResearchSchedulerTaskStatus.RUNNING
        }
        for task_id in unowned_task_ids:
            running_by_category[task_state[task_id].category] += 1
        try:
            # One scheduler-owned SQLite connection avoids repeatedly paying the
            # WAL/synchronous setup cost while independent network/LLM handlers run.
            # The in-memory map is only an execution projection: every transition is
            # durably persisted before a dependent task may become READY.
            with self.state.connect() as connection:
                while True:
                    current_run = connection.execute(
                        "SELECT generation,status FROM research_scheduler_run WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    if (
                        current_run is None
                        or int(current_run["generation"]) != run.generation
                        or str(current_run["status"]) != run.status.value
                    ):
                        return self._run_from_connection(connection, run_id)
                    # A takeover may change tasks without changing run generation/status.
                    self._refresh_execution_projection(connection, run_id, task_state)
                    for task_id in tuple(unowned_task_ids):
                        if task_state[task_id].status is not ResearchSchedulerTaskStatus.RUNNING:
                            running_by_category[task_state[task_id].category] -= 1
                            unowned_task_ids.remove(task_id)
                    now = self.clock()
                    if now >= run.deadline_at:
                        self._cancel_cached_tasks(
                            connection,
                            run,
                            task_state,
                            error_code="REQUEST_DEADLINE_EXCEEDED",
                            at=now,
                        )
                        for future in running:
                            future.cancel()
                        return self._finish_cached_run(connection, run, task_state, at=now)

                    for task_id, task in tuple(task_state.items()):
                        if task.status is not ResearchSchedulerTaskStatus.PENDING:
                            continue
                        dependency_states = [task_state[item].status for item in task.dependencies]
                        if any(
                            status
                            in {
                                ResearchSchedulerTaskStatus.FAILED,
                                ResearchSchedulerTaskStatus.CANCELLED,
                            }
                            for status in dependency_states
                        ):
                            task_state[task_id] = self._transition_cached_task(
                                connection,
                                run,
                                task,
                                ResearchSchedulerTaskStatus.CANCELLED,
                                error_code="DEPENDENCY_NOT_COMPLETED",
                                at=now,
                            )
                        elif dependency_states and all(
                            status is ResearchSchedulerTaskStatus.COMPLETED
                            for status in dependency_states
                        ):
                            task_state[task_id] = self._transition_cached_task(
                                connection,
                                run,
                                task,
                                ResearchSchedulerTaskStatus.READY,
                                at=now,
                            )

                    launchable: list[ResearchSchedulerTask] = []
                    reserved_by_category = dict(running_by_category)
                    for task in sorted(task_state.values(), key=lambda item: item.task_id):
                        if task.status is not ResearchSchedulerTaskStatus.READY:
                            continue
                        if reserved_by_category[task.category] >= limits[task.category]:
                            continue
                        handler = handlers.get(task.node_id)
                        if handler is None:
                            task_state[task.task_id] = self._transition_cached_task(
                                connection,
                                run,
                                task,
                                ResearchSchedulerTaskStatus.FAILED,
                                error_code="TASK_HANDLER_UNAVAILABLE",
                                at=now,
                            )
                            continue
                        launchable.append(task)
                        reserved_by_category[task.category] += 1
                    if launchable:
                        started_tasks = self._transition_cached_tasks_running(
                            connection, launchable, run=run, at=now
                        )
                        for started in started_tasks:
                            task_state[started.task_id] = started
                            handler = handlers[started.node_id]
                            future = pool.submit(handler, started)
                            running[future] = started
                            running_by_category[started.category] += 1

                    if running:
                        done, _ = wait(tuple(running), timeout=0.05, return_when=FIRST_COMPLETED)
                        deadline_hit = False
                        for future in done:
                            task = running.pop(future)
                            running_by_category[task.category] -= 1
                            completed_at = self.clock()
                            latest = task_state[task.task_id]
                            if (
                                latest.status is not ResearchSchedulerTaskStatus.RUNNING
                                or latest.generation != task.generation
                                or latest.lease_owner != task.lease_owner
                                or latest.lease_expires_at != task.lease_expires_at
                            ):
                                # The dispatched attempt no longer owns this task.
                                continue
                            if completed_at >= run.deadline_at:
                                task_state[task.task_id] = self._transition_cached_task(
                                    connection,
                                    run,
                                    latest,
                                    ResearchSchedulerTaskStatus.CANCELLED,
                                    error_code="REQUEST_DEADLINE_EXCEEDED",
                                    at=completed_at,
                                )
                                deadline_hit = True
                                continue
                            try:
                                result_artifact_id = future.result()
                                if not result_artifact_id.strip():
                                    raise ValueError(
                                        "research task handler returned a blank artifact id"
                                    )
                            except Exception as exc:  # noqa: BLE001 - bounded task boundary
                                task_state[task.task_id] = self._transition_cached_task(
                                    connection,
                                    run,
                                    latest,
                                    ResearchSchedulerTaskStatus.FAILED,
                                    error_code=f"HANDLER_FAILED:{type(exc).__name__}",
                                    at=completed_at,
                                )
                            else:
                                task_state[task.task_id] = self._transition_cached_task(
                                    connection,
                                    run,
                                    latest,
                                    ResearchSchedulerTaskStatus.COMPLETED,
                                    result_artifact_id=result_artifact_id,
                                    at=completed_at,
                                )
                        if deadline_hit:
                            deadline_at = self.clock()
                            self._cancel_cached_tasks(
                                connection,
                                run,
                                task_state,
                                error_code="REQUEST_DEADLINE_EXCEEDED",
                                at=deadline_at,
                            )
                            for future in running:
                                future.cancel()
                            return self._finish_cached_run(
                                connection, run, task_state, at=deadline_at
                            )
                        continue

                    states = {task.status for task in task_state.values()}
                    if not states or states <= {
                        ResearchSchedulerTaskStatus.COMPLETED,
                        ResearchSchedulerTaskStatus.FAILED,
                        ResearchSchedulerTaskStatus.CANCELLED,
                    }:
                        return self._finish_cached_run(connection, run, task_state, at=self.clock())
                    if unowned_task_ids:
                        recovered = False
                        for task_id in tuple(unowned_task_ids):
                            task = task_state[task_id]
                            if (
                                task.lease_expires_at is not None
                                and task.lease_expires_at > self.clock()
                            ):
                                continue
                            task_state[task_id] = self._transition_cached_task(
                                connection,
                                run,
                                task,
                                ResearchSchedulerTaskStatus.READY,
                                at=self.clock(),
                            )
                            unowned_task_ids.remove(task_id)
                            running_by_category[task.category] -= 1
                            recovered = True
                        if recovered:
                            continue
                        # Independent ready work has been drained. Do not steal an
                        # unexpired lease or spin while only that branch is blocked.
                        return self._run_from_connection(connection, run_id)
                    time.sleep(0.01)
        finally:
            cancellation_errors: list[Exception] = []
            cancelled_handlers: set[int] = set()
            try:
                for future, task in running.items():
                    future.cancel()
                    handler = handlers.get(task.node_id)
                    if (
                        isinstance(handler, CancellableTaskHandler)
                        and id(handler) not in cancelled_handlers
                    ):
                        cancelled_handlers.add(id(handler))
                        try:
                            handler.cancel()
                        except Exception as exc:  # noqa: BLE001 - clean up every owned worker
                            cancellation_errors.append(exc)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if cancellation_errors:
                raise RuntimeError(
                    "owned research worker termination failed"
                ) from cancellation_errors[0]

    def _refresh_execution_projection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_state: dict[str, ResearchSchedulerTask],
    ) -> None:
        """Observe same-generation repairs; reread immutable bytes only on changes."""
        rows = connection.execute(
            "SELECT task_id,object_hash FROM research_scheduler_task WHERE run_id=?", (run_id,)
        ).fetchall()
        for row in rows:
            task_id, object_hash = str(row["task_id"]), str(row["object_hash"])
            cached = task_state.get(task_id)
            if cached is not None and content_hash(cached.model_dump(mode="json")) == object_hash:
                continue
            current = ResearchSchedulerTask.model_validate_json(self.objects.get_bytes(object_hash))
            if current.task_id != task_id or current.run_id != run_id:
                raise ValueError("research task projection identity mismatch")
            task_state[task_id] = current

    def _verify_takeover_sources(self, artifact_ids: Sequence[str]) -> None:
        """A trusted reference must resolve to intact registered source bytes."""
        for artifact_id in sorted(set(artifact_ids)):
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(
                    "LLM takeover trusted-source contract references an unregistered artifact"
                )
            self.objects.get_bytes(str(record["object_hash"]))

    def takeover_packet(
        self,
        *,
        request_id: str,
        task_id: str,
        trusted_artifact_ids: Sequence[str],
        attempted_actions: Sequence[str],
        missing_requirement: str,
        allowed_write_paths: Sequence[str],
        expected_output_schema: str,
        dependency_fingerprint: str,
    ) -> LlmTakeoverPacket:
        self._verify_takeover_sources(trusted_artifact_ids)
        write_paths = self._validated_takeover_write_paths(allowed_write_paths)
        contract = {
            "trusted_artifact_ids": sorted(set(trusted_artifact_ids)),
            "attempted_actions": sorted(set(attempted_actions)),
            "missing_requirement": missing_requirement,
            "allowed_write_paths": write_paths,
            "expected_output_schema": expected_output_schema,
            "dependency_fingerprint": dependency_fingerprint,
        }
        # Single transaction covers current identity, pending reuse, retry count,
        # and creation. Concurrent callers cannot allocate the same round twice.
        with self.state.transaction() as connection:
            task_row = connection.execute(
                "SELECT object_hash FROM research_scheduler_task WHERE task_id=?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise ValueError("research scheduler task is unavailable")
            task = ResearchSchedulerTask.model_validate_json(
                self.objects.get_bytes(str(task_row["object_hash"]))
            )
            run = self._run_from_connection(connection, task.run_id)
            now = self.clock()
            if run.request_id != request_id:
                raise ValueError("LLM takeover belongs to another request")
            if task.generation != run.generation:
                raise ValueError("LLM takeover task generation is stale")
            if task.status not in {
                ResearchSchedulerTaskStatus.FAILED,
                ResearchSchedulerTaskStatus.CANCELLED,
            }:
                raise ValueError("LLM takeover requires a failed or cancelled program task")
            if now >= run.deadline_at:
                raise ValueError("LLM takeover cannot start after the request deadline")
            rows = connection.execute(
                "SELECT round_index,status,packet_object_hash FROM research_llm_takeover "
                "WHERE task_id=? AND generation=? ORDER BY round_index",
                (task.task_id, run.generation),
            ).fetchall()
            pending = next((row for row in rows if str(row["status"]) == "PENDING"), None)
            if pending is not None:
                packet = LlmTakeoverPacket.model_validate_json(
                    self.objects.get_bytes(str(pending["packet_object_hash"]))
                )
                previous = packet.model_dump(mode="json")
                if any(previous[key] != value for key, value in contract.items()):
                    raise ValueError("pending LLM takeover contract has changed")
                if packet.failure_code != (task.error_code or "PROGRAM_PATH_UNAVAILABLE"):
                    raise ValueError("pending LLM takeover failure contract has changed")
                return packet
            if len(rows) >= 2:
                raise ValueError("LLM takeover retry budget is exhausted")
            round_index = len(rows) + 1
            packet_identity = {
                "run_id": run.run_id,
                "task_id": task.task_id,
                "generation": run.generation,
                "round_index": round_index,
                "contract": contract,
            }
            packet = LlmTakeoverPacket(
                packet_id=f"llm-takeover:{content_hash(packet_identity)}",
                request_id=request_id,
                run_id=run.run_id,
                task_id=task.task_id,
                node_id=task.node_id,
                candidate_id=task.candidate_id,
                failure_code=task.error_code or "PROGRAM_PATH_UNAVAILABLE",
                trusted_artifact_ids=sorted(set(trusted_artifact_ids)),
                attempted_actions=sorted(set(attempted_actions)),
                missing_requirement=missing_requirement,
                allowed_write_paths=write_paths,
                expected_output_schema=expected_output_schema,
                dependency_fingerprint=dependency_fingerprint,
                generation=run.generation,
                round_index=round_index,
                deadline_at=run.deadline_at,
                created_at=now,
            )
            ref = self.objects.put_json(packet.model_dump(mode="json"))
            connection.execute(
                "INSERT INTO research_llm_takeover("
                "packet_id,request_id,run_id,task_id,generation,round_index,status,"
                "dependency_fingerprint,packet_object_hash,result_artifact_id,result_object_hash,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    packet.packet_id,
                    request_id,
                    run.run_id,
                    task.task_id,
                    run.generation,
                    round_index,
                    "PENDING",
                    dependency_fingerprint,
                    ref.sha256,
                    None,
                    None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return packet

    def _validated_takeover_write_paths(self, paths: Sequence[str]) -> list[str]:
        project_root = self.state.migrations_dir.parent.resolve()
        normalized: set[str] = set()
        for value in paths:
            if not isinstance(value, str):
                raise ValueError("LLM takeover write path must be project-relative text")
            relative = PurePosixPath(value.replace("\\", "/"))
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or ":" in value
            ):
                raise ValueError("LLM takeover write path must stay inside the project")
            if not (project_root / relative.as_posix()).resolve().is_relative_to(project_root):
                raise ValueError("LLM takeover write path resolves outside the project")
            normalized.add(relative.as_posix())
        return sorted(normalized)

    def apply_takeover_result(
        self,
        result: LlmTakeoverResult,
        *,
        validate_result: TakeoverResultValidator,
    ) -> ResearchSchedulerRun:
        """Validate one bounded LLM repair and resume only the affected graph suffix."""

        now = self.clock()
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT request_id,run_id,task_id,generation,status,dependency_fingerprint,"
                "packet_object_hash FROM research_llm_takeover WHERE packet_id=?",
                (result.packet_id,),
            ).fetchone()
        if row is None:
            raise ValueError("LLM takeover result references an unknown packet")
        if str(row["status"]) != "PENDING":
            raise ValueError("LLM takeover packet is no longer pending")
        packet = LlmTakeoverPacket.model_validate_json(
            self.objects.get_bytes(str(row["packet_object_hash"]))
        )
        if (
            result.request_id != packet.request_id
            or result.run_id != packet.run_id
            or result.task_id != packet.task_id
            or result.generation != packet.generation
            or result.dependency_fingerprint != packet.dependency_fingerprint
        ):
            raise ValueError("LLM takeover result does not match its packet")
        self._verify_takeover_sources(packet.trusted_artifact_ids)
        run = self._run(packet.run_id)
        task = self._task(packet.task_id)
        if run.generation != packet.generation or task.generation != packet.generation:
            raise ValueError("LLM takeover result is stale")
        if now >= run.deadline_at:
            raise ValueError("LLM takeover result arrived after the request deadline")

        artifact_id = result.result_artifact_id or ""
        if result.status is LlmTakeoverResultStatus.RESOLVED:
            artifact = self.state.artifact_record(artifact_id)
            if artifact is None or str(artifact["type"]) != packet.expected_output_schema:
                raise ValueError("LLM takeover result has the wrong registered output contract")
            self.objects.get_bytes(str(artifact["object_hash"]))
            validate_result(artifact_id)
        result_ref = self.objects.put_json(result.model_dump(mode="json"))
        with self.state.transaction() as connection:
            # Validation may be slow; recheck both time and persistent identities after it.
            now = self.clock()
            current_run = self._run_from_connection(connection, packet.run_id)
            current_tasks = {
                item.task_id: item
                for item in self._tasks_from_connection(connection, packet.run_id)
            }
            current_task = current_tasks[packet.task_id]
            current_packet = connection.execute(
                "SELECT status,packet_object_hash FROM research_llm_takeover WHERE packet_id=?",
                (packet.packet_id,),
            ).fetchone()
            if now >= current_run.deadline_at:
                raise ValueError("LLM takeover result arrived after the request deadline")
            if (
                current_run != run
                or current_task != task
                or current_packet is None
                or str(current_packet["status"]) != "PENDING"
                or str(current_packet["packet_object_hash"]) != str(row["packet_object_hash"])
            ):
                raise ValueError("stale LLM takeover write rejected")
            if current_task.status not in {
                ResearchSchedulerTaskStatus.FAILED,
                ResearchSchedulerTaskStatus.CANCELLED,
            }:
                raise ValueError("LLM takeover requires a failed or cancelled task")
            resolved = result.status is LlmTakeoverResultStatus.RESOLVED
            if resolved:
                self._transition_cached_task(
                    connection,
                    current_run,
                    current_task,
                    ResearchSchedulerTaskStatus.COMPLETED,
                    result_artifact_id=artifact_id,
                    at=now,
                )
            cursor = connection.execute(
                "UPDATE research_llm_takeover SET status=?,result_artifact_id=?,"
                "result_object_hash=?,"
                "updated_at=? WHERE packet_id=? AND status='PENDING'",
                (
                    "RESOLVED" if resolved else "UNRESOLVED",
                    artifact_id if resolved else None,
                    result_ref.sha256,
                    now.isoformat(),
                    packet.packet_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale LLM takeover write rejected")
            if not resolved:
                return current_run
            return self._resume_after_takeover(
                current_run, packet.task_id, at=now, connection=connection
            )

    def _resume_after_takeover(
        self,
        run: ResearchSchedulerRun,
        repaired_task_id: str,
        *,
        at: datetime,
        connection: sqlite3.Connection,
    ) -> ResearchSchedulerRun:
        tasks = {task.task_id: task for task in self._tasks_from_connection(connection, run.run_id)}
        reverse: dict[str, set[str]] = {}
        for task in tasks.values():
            for dependency in task.dependencies:
                reverse.setdefault(dependency, set()).add(task.task_id)
        descendants: set[str] = set()
        frontier = list(reverse.get(repaired_task_id, ()))
        while frontier:
            task_id = frontier.pop()
            if task_id in descendants:
                continue
            descendants.add(task_id)
            frontier.extend(reverse.get(task_id, ()))

        updated_tasks: list[tuple[ResearchSchedulerTask, str, ResearchSchedulerTaskStatus]] = []
        for task_id in sorted(descendants):
            task = tasks[task_id]
            if (
                task.status is not ResearchSchedulerTaskStatus.CANCELLED
                or task.error_code != "DEPENDENCY_NOT_COMPLETED"
            ):
                continue
            dependency_states = [tasks[item].status for item in task.dependencies]
            status = (
                ResearchSchedulerTaskStatus.READY
                if dependency_states
                and all(item is ResearchSchedulerTaskStatus.COMPLETED for item in dependency_states)
                else ResearchSchedulerTaskStatus.PENDING
            )
            updated = ResearchSchedulerTask.model_validate(
                task.model_copy(
                    update={
                        "status": status,
                        "started_at": None,
                        "finished_at": None,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "result_artifact_id": None,
                        "error_code": None,
                        "updated_at": at,
                    }
                ).model_dump()
            )
            ref = self.objects.put_json(updated.model_dump(mode="json"))
            updated_tasks.append((updated, ref.sha256, task.status))
            tasks[task_id] = updated

        resumed = ResearchSchedulerRun.model_validate(
            run.model_copy(
                update={
                    "status": ResearchSchedulerRunStatus.RUNNING,
                    "finished_at": None,
                    "updated_at": at,
                }
            ).model_dump()
        )
        run_ref = self.objects.put_json(resumed.model_dump(mode="json"))
        for updated, object_hash, old_status in updated_tasks:
            cursor = connection.execute(
                "UPDATE research_scheduler_task SET status=?,lease_owner=NULL,"
                "lease_expires_at=NULL,"
                "started_at=NULL,finished_at=NULL,result_artifact_id=NULL,error_code=NULL,"
                "object_hash=?,updated_at=? WHERE task_id=? AND generation=? AND status=?",
                (
                    updated.status.value,
                    object_hash,
                    at.isoformat(),
                    updated.task_id,
                    updated.generation,
                    old_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale takeover descendant write rejected")
        cursor = connection.execute(
            "UPDATE research_scheduler_run SET status=?,finished_at=NULL,"
            "object_hash=?,updated_at=? "
            "WHERE run_id=? AND generation=?",
            (
                ResearchSchedulerRunStatus.RUNNING.value,
                run_ref.sha256,
                at.isoformat(),
                resumed.run_id,
                resumed.generation,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("stale takeover run write rejected")
        return resumed

    def recover_expired_tasks(self, run_id: str) -> int:
        """Recover expired attempts without rerunning successes or resetting the deadline."""
        with schedule_run_ownership(self.state.path, "current-research-scheduler", run_id):
            return self._recover_expired_tasks_owned(run_id)

    def _recover_expired_tasks_owned(self, run_id: str) -> int:
        recovered = 0
        with self.state.transaction() as connection:
            run = self._run_from_connection(connection, run_id)
            if run.status not in {
                ResearchSchedulerRunStatus.PENDING,
                ResearchSchedulerRunStatus.RUNNING,
            }:
                return 0
            now = self.clock()
            for task in self._tasks_from_connection(connection, run_id):
                if task.status is not ResearchSchedulerTaskStatus.RUNNING:
                    continue
                if (
                    now < run.deadline_at
                    and task.lease_expires_at is not None
                    and task.lease_expires_at > now
                ):
                    continue
                deadline_hit = now >= run.deadline_at
                self._transition_cached_task(
                    connection,
                    run,
                    task,
                    ResearchSchedulerTaskStatus.CANCELLED
                    if deadline_hit
                    else ResearchSchedulerTaskStatus.READY,
                    error_code="REQUEST_DEADLINE_EXCEEDED" if deadline_hit else None,
                    at=now,
                )
                recovered += 1
        return recovered

    @staticmethod
    def _task_contract(tasks: Sequence[ResearchSchedulerTask]) -> list[tuple]:
        return sorted(
            (
                task.task_id,
                task.node_id,
                task.candidate_id,
                task.category.value,
                tuple(task.dependencies),
                task.input_fingerprint,
            )
            for task in tasks
        )

    def _tasks_from_connection(
        self, connection: sqlite3.Connection, run_id: str
    ) -> tuple[ResearchSchedulerTask, ...]:
        rows = connection.execute(
            "SELECT object_hash FROM research_scheduler_task WHERE run_id=? ORDER BY task_id",
            (run_id,),
        ).fetchall()
        return tuple(
            ResearchSchedulerTask.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        )

    def _run_from_connection(
        self, connection: sqlite3.Connection, run_id: str
    ) -> ResearchSchedulerRun:
        row = connection.execute(
            "SELECT object_hash FROM research_scheduler_run WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("research scheduler run is unavailable")
        return ResearchSchedulerRun.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )

    # ------------------------------------------------------------- internals
    @staticmethod
    def _validate_task_graph(tasks: Sequence[ResearchSchedulerTask]) -> None:
        by_id = {task.task_id: task for task in tasks}
        if len(by_id) != len(tasks):
            raise ValueError("research task ids must be unique")
        for task in tasks:
            missing = set(task.dependencies) - set(by_id)
            if missing:
                raise ValueError(f"research task has unknown dependencies: {sorted(missing)}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("research task graph contains a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in by_id:
            visit(task_id)

    @staticmethod
    def _insert_task(connection, task: ResearchSchedulerTask, object_hash: str) -> None:
        connection.execute(
            "INSERT INTO research_scheduler_task("
            "task_id,run_id,node_id,candidate_id,category,status,dependencies_json,input_fingerprint,"
            "generation,lease_owner,lease_expires_at,started_at,finished_at,result_artifact_id,error_code,"
            "object_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task.task_id,
                task.run_id,
                task.node_id,
                task.candidate_id,
                task.category.value,
                task.status.value,
                json.dumps(task.dependencies, ensure_ascii=False),
                task.input_fingerprint,
                task.generation,
                task.lease_owner,
                task.lease_expires_at.isoformat() if task.lease_expires_at else None,
                task.started_at.isoformat() if task.started_at else None,
                task.finished_at.isoformat() if task.finished_at else None,
                task.result_artifact_id,
                task.error_code,
                object_hash,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )

    def _run(self, run_id: str) -> ResearchSchedulerRun:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM research_scheduler_run WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError("research scheduler run is unavailable")
        return ResearchSchedulerRun.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )

    def _task(self, task_id: str) -> ResearchSchedulerTask:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM research_scheduler_task WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise ValueError("research scheduler task is unavailable")
        return ResearchSchedulerTask.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )

    def _transition_tasks_running(
        self,
        tasks: Sequence[ResearchSchedulerTask],
        *,
        run: ResearchSchedulerRun,
        at: datetime,
    ) -> tuple[ResearchSchedulerTask, ...]:
        with self.state.connect() as connection:
            return self._transition_cached_tasks_running(connection, tasks, run=run, at=at)

    def _transition_cached_tasks_running(
        self,
        connection: sqlite3.Connection,
        tasks: Sequence[ResearchSchedulerTask],
        *,
        run: ResearchSchedulerRun,
        at: datetime,
    ) -> tuple[ResearchSchedulerTask, ...]:
        """Start READY tasks in one short transaction on an existing connection."""

        if not tasks:
            return ()
        lease_expires_at = min(run.deadline_at, at + timedelta(minutes=10))
        updated_rows: list[tuple[ResearchSchedulerTask, str, ResearchSchedulerTaskStatus]] = []
        for task in tasks:
            if task.run_id != run.run_id or task.generation != run.generation:
                raise ValueError("stale research task generation cannot mutate current state")
            if task.status is not ResearchSchedulerTaskStatus.READY:
                raise ValueError("only READY research tasks may be started")
            updated = ResearchSchedulerTask.model_validate(
                task.model_copy(
                    update={
                        "status": ResearchSchedulerTaskStatus.RUNNING,
                        "lease_owner": f"local-executor:{uuid4().hex}",
                        "lease_expires_at": lease_expires_at,
                        "started_at": at,
                        "finished_at": None,
                        "result_artifact_id": None,
                        "error_code": None,
                        "updated_at": at,
                    }
                ).model_dump()
            )
            ref = self.objects.put_json(updated.model_dump(mode="json"))
            updated_rows.append((updated, ref.sha256, task.status))
        connection.execute("BEGIN IMMEDIATE")
        try:
            for updated, object_hash, expected_status in updated_rows:
                cursor = connection.execute(
                    "UPDATE research_scheduler_task SET status=?,lease_owner=?,lease_expires_at=?,"
                    "started_at=?,finished_at=NULL,result_artifact_id=NULL,"
                    "error_code=NULL,object_hash=?,updated_at=? "
                    "WHERE task_id=? AND run_id=? AND generation=? AND status=?",
                    (
                        ResearchSchedulerTaskStatus.RUNNING.value,
                        updated.lease_owner,
                        updated.lease_expires_at.isoformat() if updated.lease_expires_at else None,
                        updated.started_at.isoformat() if updated.started_at else None,
                        object_hash,
                        at.isoformat(),
                        updated.task_id,
                        run.run_id,
                        run.generation,
                        expected_status.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale research scheduler write rejected")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return tuple(item for item, _, _ in updated_rows)

    def _transition_cached_task(
        self,
        connection: sqlite3.Connection,
        run: ResearchSchedulerRun,
        task: ResearchSchedulerTask,
        status: ResearchSchedulerTaskStatus,
        *,
        result_artifact_id: str | None = None,
        error_code: str | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        at: datetime,
    ) -> ResearchSchedulerTask:
        """Persist one task transition without reopening or rereading SQLite."""

        if task.run_id != run.run_id or task.generation != run.generation:
            raise ValueError("stale research task generation cannot mutate current state")
        started_at = task.started_at
        finished_at = task.finished_at
        if status in {ResearchSchedulerTaskStatus.READY, ResearchSchedulerTaskStatus.PENDING}:
            started_at = None
            finished_at = None
        elif status is ResearchSchedulerTaskStatus.RUNNING:
            started_at = at
        if status in {
            ResearchSchedulerTaskStatus.COMPLETED,
            ResearchSchedulerTaskStatus.FAILED,
            ResearchSchedulerTaskStatus.CANCELLED,
        }:
            started_at = started_at or at
            finished_at = at
        updated = ResearchSchedulerTask.model_validate(
            task.model_copy(
                update={
                    "status": status,
                    "result_artifact_id": result_artifact_id,
                    "error_code": error_code,
                    "lease_owner": lease_owner,
                    "lease_expires_at": lease_expires_at,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "updated_at": at,
                }
            ).model_dump()
        )
        ref = self.objects.put_json(updated.model_dump(mode="json"))
        dependency_guard = ""
        dependency_parameters: tuple[str, ...] = ()
        if (
            status is ResearchSchedulerTaskStatus.CANCELLED
            and error_code == "DEPENDENCY_NOT_COMPLETED"
        ):
            # Check the cause in the same SQL statement as cancellation. A parent
            # can be repaired after the executor inspected its cached dependency.
            placeholders = ",".join("?" for _ in task.dependencies) or "NULL"
            dependency_guard = (
                " AND EXISTS(SELECT 1 FROM research_scheduler_task AS dependency "
                f"WHERE dependency.task_id IN ({placeholders}) "
                "AND dependency.status IN ('FAILED','CANCELLED'))"
            )
            dependency_parameters = tuple(task.dependencies)
        cursor = connection.execute(
            "UPDATE research_scheduler_task SET status=?,lease_owner=?,lease_expires_at=?,"
            "started_at=?,finished_at=?,result_artifact_id=?,error_code=?,"
            "object_hash=?,updated_at=? "
            "WHERE task_id=? AND run_id=? AND generation=? AND status=? "
            "AND lease_owner IS ? AND lease_expires_at IS ? AND result_artifact_id IS ? "
            "AND error_code IS ? AND EXISTS(SELECT 1 FROM research_scheduler_run "
            "WHERE run_id=? AND generation=? AND status=?)" + dependency_guard,
            (
                status.value,
                updated.lease_owner,
                updated.lease_expires_at.isoformat() if updated.lease_expires_at else None,
                updated.started_at.isoformat() if updated.started_at else None,
                updated.finished_at.isoformat() if updated.finished_at else None,
                result_artifact_id,
                error_code,
                ref.sha256,
                at.isoformat(),
                task.task_id,
                run.run_id,
                run.generation,
                task.status.value,
                task.lease_owner,
                task.lease_expires_at.isoformat() if task.lease_expires_at else None,
                task.result_artifact_id,
                task.error_code,
                run.run_id,
                run.generation,
                run.status.value,
            )
            + dependency_parameters,
        )
        if cursor.rowcount != 1:
            if dependency_guard:
                current_run = self._run_from_connection(connection, run.run_id)
                row = connection.execute(
                    "SELECT object_hash FROM research_scheduler_task WHERE task_id=?",
                    (task.task_id,),
                ).fetchone()
                if (
                    row is not None
                    and current_run.generation == run.generation
                    and current_run.status is run.status
                ):
                    # The repair won the race. Keep its authoritative task state
                    # and let the next scheduling iteration reevaluate readiness.
                    return ResearchSchedulerTask.model_validate_json(
                        self.objects.get_bytes(str(row["object_hash"]))
                    )
            raise ValueError("stale research scheduler write rejected")
        return updated

    def _cancel_cached_tasks(
        self,
        connection: sqlite3.Connection,
        run: ResearchSchedulerRun,
        task_state: dict[str, ResearchSchedulerTask],
        *,
        error_code: str,
        at: datetime,
    ) -> None:
        for task_id, task in tuple(task_state.items()):
            if task.status in {
                ResearchSchedulerTaskStatus.PENDING,
                ResearchSchedulerTaskStatus.READY,
                ResearchSchedulerTaskStatus.RUNNING,
            }:
                task_state[task_id] = self._transition_cached_task(
                    connection,
                    run,
                    task,
                    ResearchSchedulerTaskStatus.CANCELLED,
                    error_code=error_code,
                    at=at,
                )

    def _finish_cached_run(
        self,
        connection: sqlite3.Connection,
        run: ResearchSchedulerRun,
        task_state: Mapping[str, ResearchSchedulerTask],
        *,
        at: datetime,
    ) -> ResearchSchedulerRun:
        # Finalization is infrequent. Read the authoritative task closure under a
        # short write transaction instead of sealing the executor's cached view.
        with self.state.transaction() as final_connection:
            current = self._run_from_connection(final_connection, run.run_id)
            if current.generation != run.generation or current.status is not run.status:
                return current
            tasks = self._tasks_from_connection(final_connection, run.run_id)
            states = {task.status for task in tasks}
            if states & {
                ResearchSchedulerTaskStatus.PENDING,
                ResearchSchedulerTaskStatus.READY,
                ResearchSchedulerTaskStatus.RUNNING,
            }:
                return current
            if tasks and states == {ResearchSchedulerTaskStatus.COMPLETED}:
                status = ResearchSchedulerRunStatus.COMPLETED
            elif ResearchSchedulerTaskStatus.COMPLETED in states:
                status = ResearchSchedulerRunStatus.PARTIAL
            elif ResearchSchedulerTaskStatus.FAILED in states:
                status = ResearchSchedulerRunStatus.FAILED
            else:
                status = ResearchSchedulerRunStatus.CANCELLED
            at = max(at, self.clock(), current.updated_at)
            updated = ResearchSchedulerRun.model_validate(
                current.model_copy(
                    update={"status": status, "finished_at": at, "updated_at": at}
                ).model_dump()
            )
            ref = self.objects.put_json(updated.model_dump(mode="json"))
            cursor = final_connection.execute(
                "UPDATE research_scheduler_run SET status=?,finished_at=?,"
                "object_hash=?,updated_at=? WHERE run_id=? AND generation=? AND status=?",
                (
                    status.value,
                    at.isoformat(),
                    ref.sha256,
                    at.isoformat(),
                    current.run_id,
                    current.generation,
                    current.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale research scheduler run write rejected")
            return updated

    def _transition_task(
        self,
        task: ResearchSchedulerTask,
        status: ResearchSchedulerTaskStatus,
        *,
        result_artifact_id: str | None = None,
        error_code: str | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        at: datetime,
    ) -> ResearchSchedulerTask:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash FROM research_scheduler_task WHERE task_id=?", (task.task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("research scheduler task is unavailable")
            current = ResearchSchedulerTask.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            if current != task:
                raise ValueError("stale research task snapshot cannot mutate current state")
            run = self._run_from_connection(connection, task.run_id)
            return self._transition_cached_task(
                connection,
                run,
                task,
                status,
                result_artifact_id=result_artifact_id,
                error_code=error_code,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                at=at,
            )

    def _cancel_unfinished(self, run_id: str, error_code: str) -> None:
        now = self.clock()
        for task in self.tasks(run_id):
            if task.status in {
                ResearchSchedulerTaskStatus.PENDING,
                ResearchSchedulerTaskStatus.READY,
                ResearchSchedulerTaskStatus.RUNNING,
            }:
                self._transition_task(
                    task,
                    ResearchSchedulerTaskStatus.CANCELLED,
                    error_code=error_code,
                    at=now,
                )

    def _finish_run(self, run_id: str) -> ResearchSchedulerRun:
        run = self._run(run_id)
        tasks = self.tasks(run_id)
        states = {task.status for task in tasks}
        if tasks and all(task.status is ResearchSchedulerTaskStatus.COMPLETED for task in tasks):
            status = ResearchSchedulerRunStatus.COMPLETED
        elif ResearchSchedulerTaskStatus.COMPLETED in states:
            status = ResearchSchedulerRunStatus.PARTIAL
        elif ResearchSchedulerTaskStatus.FAILED in states:
            status = ResearchSchedulerRunStatus.FAILED
        else:
            status = ResearchSchedulerRunStatus.CANCELLED
        now = self.clock()
        updated = run.model_copy(update={"status": status, "finished_at": now, "updated_at": now})
        updated = ResearchSchedulerRun.model_validate(updated.model_dump())
        ref = self.objects.put_json(updated.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE research_scheduler_run SET status=?,finished_at=?,"
                "object_hash=?,updated_at=? "
                "WHERE run_id=? AND generation=?",
                (
                    status.value,
                    now.isoformat(),
                    ref.sha256,
                    now.isoformat(),
                    run_id,
                    run.generation,
                ),
            )
        return updated


__all__ = ["CurrentResearchSlaService"]
