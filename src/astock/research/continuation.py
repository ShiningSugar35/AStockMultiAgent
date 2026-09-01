"""Durable same-request continuation for current company research."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from astock.core.hashing import content_hash
from astock.core.logging import emit_operational_event
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.repository import DocumentRepository
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.research.policy import load_default_current_research_policy
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
from astock.schemas.research_team import (
    RecommendationReadinessRequest,
    RecommendationReadinessStatus,
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
        acquisition_policy = getattr(self.acquisition, "policy", None)
        if (
            acquisition_policy is not None
            and acquisition_policy.automatic_resolution_budget_seconds
            != self.policy.automatic_resolution_budget_seconds
        ):
            raise ValueError("current acquisition budget differs from the canonical policy")
        if (
            self.team.policy.automatic_resolution_budget_seconds
            != self.policy.automatic_resolution_budget_seconds
        ):
            raise ValueError("research-team budget differs from the canonical policy")
        self.documents = DocumentRepository(state)

    def start(self, request: CurrentResearchContinuationRequest) -> CurrentResearchContinuation:
        if (
            request.automatic_resolution_budget_seconds
            != self.policy.automatic_resolution_budget_seconds
        ):
            raise ValueError(
                "current research continuation must use the canonical 1800-second budget"
            )
        continuation_id = self._continuation_id(request)
        existing = self.get(continuation_id)
        if existing is not None:
            return existing

        request_ref = self.objects.put_json(request.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"CurrentResearchContinuationRequest:{continuation_id}",
            artifact_type="CurrentResearchContinuationRequest",
            schema_version=request.schema_version,
            object_hash=request_ref.sha256,
            input_hashes=[],
        )
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
            started_at=request.created_at,
            deadline_at=request.created_at
            + timedelta(seconds=request.automatic_resolution_budget_seconds),
            status=status,
            automatic_resolution_budget_seconds=request.automatic_resolution_budget_seconds,
            max_automatic_rounds=request.max_automatic_rounds,
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
    ) -> CurrentResearchContinuation:
        """Drive automatic evidence and ready team stages in one caller request.

        Network search and role synthesis remain Agent-owned callbacks. This
        deterministic loop owns budgets, frozen result lineage, readiness gates,
        recovery, and the rule that an internal gap is never an investor answer.
        """

        record = self._require(continuation_id)
        while record.status in {
            CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
            CurrentResearchContinuationStatus.TEAM_RESEARCH_REQUIRED,
        }:
            if record.status is CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED:
                if self.clock() >= record.deadline_at:
                    return self._escalate_manual(record)
                if any(
                    task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND
                    for task in record.external_tasks
                ):
                    record = self.resume(record.continuation_id)
                    continue
                unresolved = tuple(
                    task
                    for task in record.external_tasks
                    if task.status is ExternalResearchTaskStatus.PENDING
                )
                if not unresolved:
                    record = self.resume(record.continuation_id)
                    continue
                for task in unresolved:
                    result = resolve_external(record, task)
                    record = self._apply_automatic_resolution(record, result)
                    if record.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT:
                        return record
                record = self.resume(record.continuation_id)
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

    def apply_automatic_resolution(
        self,
        result: CurrentResearchAutomaticResolution,
    ) -> CurrentResearchContinuation:
        """Validate and persist one Agent-owned automatic resolution result."""

        record = self._require(result.continuation_id)
        if record.status is not CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED:
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
                }
            )
            if item.task_id == task.task_id
            else item
            for item in record.external_tasks
        ]
        record = self._persist(
            record.model_copy(
                update={
                    "automatic_resolution_artifact_ids": history,
                    "external_tasks": updated_tasks,
                }
            )
        )
        if result.private_material_required:
            return self._escalate_private_material(record, task)
        for capture_artifact_id in result.capture_artifact_ids:
            record = self.bind_external_evidence(
                CurrentResearchEvidenceBinding(
                    continuation_id=record.continuation_id,
                    task_id=task.task_id,
                    capture_artifact_id=capture_artifact_id,
                    created_at=self.clock(),
                )
            )
        return record

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
        self,
        binding: CurrentResearchEvidenceBinding,
    ) -> CurrentResearchContinuation:
        record = self._require(binding.continuation_id)
        private_material_resume = (
            record.status is CurrentResearchContinuationStatus.NEEDS_USER_INPUT
            and record.private_material_required
            and not record.automatic_budget_exhausted
        )
        if (
            record.status is not CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED
            and not private_material_resume
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
                    }
                )
            )
        update: dict[str, object] = {
            "external_tasks": sorted(updated_tasks, key=lambda item: item.task_id)
        }
        if private_material_resume:
            update.update(
                {
                    "status": CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED,
                    "manual_actions": [],
                    "private_material_required": False,
                    "automatic_budget_exhausted": False,
                }
            )
        updated = record.model_copy(update=update)
        return self._persist(updated)

    def resume(self, continuation_id: str) -> CurrentResearchContinuation:
        record = self._require(continuation_id)
        if record.status is not CurrentResearchContinuationStatus.AUTO_RESOLUTION_REQUIRED:
            return record
        deadline_exhausted = self.clock() >= record.deadline_at
        rounds_exhausted = record.automatic_rounds_completed >= record.max_automatic_rounds
        bound_evidence_available = any(
            task.status is ExternalResearchTaskStatus.EVIDENCE_BOUND and task.capture_artifact_ids
            for task in record.external_tasks
        )
        if deadline_exhausted or (rounds_exhausted and not bound_evidence_available):
            return self._escalate_manual(record)

        trusted_identity_capture_ids = tuple(
            sorted(
                {
                    capture_id
                    for task in record.external_tasks
                    for capture_id in task.capture_artifact_ids
                }
            )
        )
        report = self.acquisition.acquire(
            record.company_id,
            record.market,
            lookback_days=record.lookback_days,
            planner_plan_artifact_id=record.planner_plan_artifact_id,
            reuse_report_artifact_id=record.current_acquisition_report_artifact_id,
            trusted_identity_capture_ids=trusted_identity_capture_ids,
        )
        # A successful capture on the configured final automatic round still
        # needs one deterministic reacquisition so the frozen evidence can be
        # consumed. This is not an extra resolver round: preserve the current
        # round number, and fail closed immediately if the evidence does not
        # clear the gap.
        next_round = (
            record.automatic_rounds_completed
            if rounds_exhausted
            else record.automatic_rounds_completed + 1
        )
        tasks = self._reconcile_tasks(record, report, automatic_round=next_round)
        history = list(record.acquisition_report_artifact_ids)
        if report.report_id not in history:
            history.append(report.report_id)
        if tasks:
            updated = record.model_copy(
                update={
                    "automatic_rounds_completed": next_round,
                    "acquisition_report_artifact_ids": history,
                    "current_acquisition_report_artifact_id": report.report_id,
                    "external_tasks": tasks,
                }
            )
            updated = self._persist(updated)
            return self._escalate_manual(updated) if rounds_exhausted else updated

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
            RecommendationReadinessRequest(
                plan_id=plan.plan_id,
                checks={},
                created_at=self.clock(),
            )
        )
        readiness_artifact_id = f"RecommendationReadinessReport:{readiness.report_id}"
        ready = readiness.status is RecommendationReadinessStatus.READY
        updated = record.model_copy(
            update={
                "status": (
                    CurrentResearchContinuationStatus.READY_FOR_INVESTOR_VIEW
                    if ready
                    else CurrentResearchContinuationStatus.OBSERVATION_ONLY_FOR_INVESTOR_VIEW
                ),
                "readiness_report_artifact_id": readiness_artifact_id,
                "investor_view_allowed": True,
                "formal_recommendation_allowed": ready,
            }
        )
        return self._persist(updated)

    def get(self, continuation_id: str) -> CurrentResearchContinuation | None:
        checkpoint = self.state.get_checkpoint("current-research-continuation", continuation_id)
        if checkpoint is None or not checkpoint.get("object_hash"):
            return None
        object_hash = str(checkpoint["object_hash"])
        if not self.objects.verify(object_hash):
            return None
        return CurrentResearchContinuation.model_validate_json(self.objects.get_bytes(object_hash))

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
            "user_assistance_request_allowed": record.status
            is CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
            "investor_view_allowed": record.investor_view_allowed,
            "formal_recommendation_allowed": record.formal_recommendation_allowed,
            "broker_execution_allowed": False,
        }

    def _require(self, continuation_id: str) -> CurrentResearchContinuation:
        record = self.get(continuation_id)
        if record is None:
            raise ValueError("unknown current research continuation")
        if (
            record.automatic_resolution_budget_seconds
            != self.policy.automatic_resolution_budget_seconds
        ):
            raise ValueError(
                "stored current research continuation uses a non-canonical budget"
            )
        return record

    @staticmethod
    def _continuation_id(request: CurrentResearchContinuationRequest) -> str:
        identity = request.model_dump(mode="json", exclude={"created_at"})
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
        tasks: list[CurrentResearchExternalTask] = []
        for capability, task in existing.items():
            need = needs.pop(capability, None)
            if need is None:
                tasks.append(
                    task.model_copy(update={"status": ExternalResearchTaskStatus.RESOLVED})
                )
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
                        "last_failure_code": (
                            "BOUND_EVIDENCE_NOT_ACCEPTED_BY_ACQUISITION"
                            if task.capture_artifact_ids
                            else "AUTOMATIC_SOURCE_STILL_UNAVAILABLE"
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
                item.model_copy(update={"status": ExternalResearchTaskStatus.RESOLVED})
                for item in tasks
            ],
            key=lambda item: item.task_id,
        )

    def _escalate_private_material(
        self,
        record: CurrentResearchContinuation,
        task: CurrentResearchExternalTask,
    ) -> CurrentResearchContinuation:
        action = ManualResearchAction(
            capability=task.capability,
            instruction=task.research_question,
            why_needed=(
                "The required formal material is private or otherwise unavailable to all "
                "approved automatic public-source channels."
            ),
            created_at=self.clock(),
        )
        updated = record.model_copy(
            update={
                "status": CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
                "manual_actions": [action],
                "automatic_budget_exhausted": False,
                "private_material_required": True,
            }
        )
        return self._persist(updated)

    def _escalate_manual(
        self,
        record: CurrentResearchContinuation,
    ) -> CurrentResearchContinuation:
        unresolved = [
            item
            for item in record.external_tasks
            if item.status is not ExternalResearchTaskStatus.RESOLVED
        ]
        manual_actions = [
            ManualResearchAction(
                capability=item.capability,
                instruction=item.research_question,
                why_needed=(
                    "Automatic official-source acquisition and bounded Web evidence recovery "
                    "were exhausted; provide only private or otherwise inaccessible "
                    "formal material."
                ),
                created_at=self.clock(),
            )
            for item in sorted(unresolved, key=lambda task: task.capability.value)
        ]
        updated = record.model_copy(
            update={
                "status": CurrentResearchContinuationStatus.NEEDS_USER_INPUT,
                "manual_actions": manual_actions,
                "automatic_budget_exhausted": True,
            }
        )
        return self._persist(updated)

    def _persist(
        self,
        record: CurrentResearchContinuation,
    ) -> CurrentResearchContinuation:
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
                }
                else OperationalSeverity.INFO
            ),
            run_id=record.continuation_id,
            request_id=record.request_id,
            context={
                "company_id": record.company_id,
                "market": record.market.value,
                "status": record.status.value,
                "automatic_resolution_budget_seconds": (
                    record.automatic_resolution_budget_seconds
                ),
                "automatic_rounds_completed": record.automatic_rounds_completed,
                "pending_external_task_count": sum(
                    item.status is ExternalResearchTaskStatus.PENDING
                    for item in record.external_tasks
                ),
                "automatic_budget_exhausted": record.automatic_budget_exhausted,
                "investor_view_allowed": record.investor_view_allowed,
                "formal_recommendation_allowed": (
                    record.formal_recommendation_allowed
                ),
            },
        )
        return record


__all__ = [
    "CurrentResearchContinuationService",
    "CurrentResearchExternalResolver",
    "CurrentResearchTeamExecutor",
]
