"""Additive CLI registration for the recoverable research runtime."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.adaptive.service import AdaptiveResearchStatusService
from astock.candidates.cli_ext import register_candidate_input_commands
from astock.core.hashing import content_hash
from astock.market_data.storage import CanonicalMarketStore
from astock.monitoring.cli import register_continuous_monitor_commands
from astock.portfolio.allocators import load_portfolio_allocator_policy
from astock.portfolio.cli import register_portfolio_commands
from astock.portfolio.decision_cli import register_portfolio_decision_commands
from astock.portfolio.vnext_cli import register_portfolio_vnext_commands
from astock.providers.dialects import load_provider_dialects
from astock.providers.runtime import load_transport_profiles
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.research.adaptation import (
    AdaptiveEdgeService,
    load_research_planner_policy,
    load_schema_repair_policy,
)
from astock.research.continuation_cli import register_current_research_continuation_commands
from astock.research.institutional import InstitutionalResearchService
from astock.research.knowledge_port import KnowledgeSkillProvider
from astock.research.policy import CapabilityGraph, load_default_current_research_policy
from astock.research.presentation import (
    ResponseGateway,
    audit_investor_answer,
    investor_view_from_acquisition,
    investor_view_from_run,
    narrative_from_investor_view,
)
from astock.research.production_cli import register_research_production_commands
from astock.research.resource_policy import load_specialist_resource_policy
from astock.research.runtime import ResearchRunService
from astock.research.runtime_readiness import ResearchRuntimeReadinessService
from astock.research.serenity.cli import register_serenity_commands
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.research.team_cli import register_research_team_commands
from astock.research.trade_view import TradePlanViewService
from astock.research.trading_classification import TradingClassificationService
from astock.schemas.adaptation import (
    ProviderDialectCandidateRelease,
    ProviderRecoveryProposal,
    ProviderRecoveryValidation,
    ResearchPlannerProposal,
    SchemaRepairProposal,
    SchemaRepairValidation,
    ValidatedResearchPlan,
)
from astock.schemas.institutional_research import (
    CompanyEconomicsDraft,
    DriverTreeDraft,
    EvidenceSufficiencyRequest,
    ForecastScenarioInput,
    FundamentalModelBundle,
    IndustryProfileDraft,
    InstitutionalDecisionContext,
    InstitutionalDecisionContextBuildRequest,
    InstitutionalDecisionContextDraft,
    InstitutionalResearchFinalizeRequest,
    MarketPriceAnchor,
    ValuationScenarioAssumption,
)
from astock.schemas.presentation import ResponseChannel, ResponseTaskType
from astock.schemas.reference_data import Market
from astock.schemas.research_runtime import (
    ResearchRunFrozenInputs,
    ResearchRunMode,
    ResearchRunReport,
    ResearchRunRequest,
    ResearchRunStatus,
    TradingClassificationDraft,
)
from astock.schemas.research_sla import (
    LlmTakeoverResult,
    ResearchSchedulerTaskStatus,
    ResearchTargetState,
    ResearchTaskCategory,
)
from astock.shadow.config import load_shadow_evaluation_policy
from astock.shadow.formal_study import ensure_default_formal_study
from astock.shadow.governance_cli import register_prospective_governance_commands
from astock.shadow.service import ShadowEvaluationService
from astock.shadow.storage import ParquetShadowStore


def _load_request(path: Path) -> ResearchRunRequest:
    return ResearchRunRequest.model_validate_json(path.read_text(encoding="utf-8"))


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def register_research_runtime_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
    knowledge_provider_factory: Callable[[Any, Any], KnowledgeSkillProvider],
) -> None:
    """Attach staged research and read-only readiness commands to the stable CLI."""

    register_candidate_input_commands(app, services, emit)
    register_continuous_monitor_commands(app, services, emit)
    register_portfolio_commands(app, services, emit)
    register_portfolio_decision_commands(app, services, emit)
    register_portfolio_vnext_commands(app, services, emit)
    register_prospective_governance_commands(app, services, emit)
    register_research_production_commands(app, services, emit)
    register_serenity_commands(app, services, emit)
    register_current_research_continuation_commands(app, services, emit)
    register_research_team_commands(app, services, emit)

    def runtime() -> ResearchRunService:
        paths, state, objects = services()
        return ResearchRunService(
            project_root=paths.root,
            state=state,
            objects=objects,
            reference_parquet_root=paths.parquet,
            knowledge_provider=knowledge_provider_factory(state, objects),
        )

    def current_sla() -> CurrentResearchSlaService:
        _, state, objects = services()
        return CurrentResearchSlaService(state, objects)

    def current_acquisition() -> CurrentResearchAcquisitionService:
        paths, state, objects = services()
        return CurrentResearchAcquisitionService(paths, state, objects)

    def public_investor_payload(view: Any) -> Any:
        gateway = ResponseGateway()
        rendered = gateway.render(
            gateway.context(
                "",
                task_type=ResponseTaskType.COMPANY_QUICK_VIEW,
                channel=ResponseChannel.CLI,
            ),
            narrative=narrative_from_investor_view(view),
        )
        return rendered.payload

    def readiness() -> ResearchRuntimeReadinessService:
        paths, state, objects = services()
        return ResearchRuntimeReadinessService(
            project_root=paths.root,
            state=state,
            objects=objects,
            knowledge_provider=knowledge_provider_factory(state, objects),
        )

    def trade_view() -> TradePlanViewService:
        _, state, objects = services()
        research_runtime = runtime()
        return TradePlanViewService(state, objects, research_runtime.reference)

    def institutional() -> InstitutionalResearchService:
        _, state, objects = services()
        return InstitutionalResearchService(state, objects)

    def adaptive_edge() -> AdaptiveEdgeService:
        paths, state, objects = services()
        return AdaptiveEdgeService(state, objects, paths.root)

    def shadow() -> tuple[Any, ShadowEvaluationService]:
        paths, state, objects = services()
        return paths, ShadowEvaluationService(
            state,
            objects,
            load_shadow_evaluation_policy(paths.root / "configs" / "shadow_evaluation.yaml"),
            ParquetShadowStore(paths.parquet),
            CanonicalMarketStore(paths.parquet, paths.manifests),
        )

    @app.command("research-plan")
    def research_plan(
        company_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option("--as-of")] = None,
        mode: Annotated[ResearchRunMode, typer.Option()] = ResearchRunMode.RECORDED_INPUT,
        institutional_research_required: Annotated[bool, typer.Option()] = False,
        fundamental_model_bundle_artifact_id: Annotated[str | None, typer.Option()] = None,
        institutional_decision_context_artifact_id: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        if as_of is None and mode is not ResearchRunMode.LIVE:
            raise typer.BadParameter("--as-of is required for recorded or historical research")
        resolved_as_of = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
        emit(
            runtime().plan(
                ResearchRunRequest(
                    company_id=company_id,
                    as_of=resolved_as_of,
                    mode=mode,
                    institutional_research_required=institutional_research_required,
                    frozen_inputs=(
                        ResearchRunFrozenInputs(
                            fundamental_model_bundle_artifact_id=(
                                fundamental_model_bundle_artifact_id
                            ),
                            institutional_decision_context_artifact_id=(
                                institutional_decision_context_artifact_id
                            ),
                        )
                        if (
                            fundamental_model_bundle_artifact_id
                            or institutional_decision_context_artifact_id
                        )
                        else None
                    ),
                )
            )
        )

    @app.command("research-acquire-current")
    def research_acquire_current(
        company_id: Annotated[str, typer.Argument()],
        market: Annotated[Market, typer.Option()],
        lookback_days: Annotated[int | None, typer.Option()] = None,
        planner_plan_artifact_id: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        """Acquire current public research inputs before freezing a decision timestamp."""

        emit(
            current_acquisition().acquire(
                company_id,
                market,
                lookback_days=lookback_days,
                planner_plan_artifact_id=planner_plan_artifact_id,
            )
        )

    @app.command("research-acquisition-investor-view")
    def research_acquisition_investor_view(
        report_id: Annotated[str, typer.Argument()],
    ) -> None:
        """Return the stable investor-research-view-v1 machine contract."""

        report = current_acquisition().get(report_id)
        if report is None:
            emit({"status": "NOT_FOUND"})
            raise typer.Exit(code=2)
        emit(investor_view_from_acquisition(report))

    @app.command("research-acquisition-public-view")
    def research_acquisition_public_view(
        report_id: Annotated[str, typer.Argument()],
    ) -> None:
        """Return the audited public presentation projection."""

        report = current_acquisition().get(report_id)
        if report is None:
            emit({"status": "NOT_FOUND"})
            raise typer.Exit(code=2)
        emit(public_investor_payload(investor_view_from_acquisition(report)))

    @app.command("research-investor-view")
    def research_investor_view(
        run_id: Annotated[str, typer.Argument()],
        include_execution_readiness: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Return the stable investor-research-view-v1 machine contract."""

        report = runtime().status(run_id)
        if report is None:
            emit({"status": "NOT_RUN"})
            raise typer.Exit(code=2)
        emit(
            investor_view_from_run(
                report,
                include_execution_readiness=include_execution_readiness,
            )
        )

    @app.command("research-public-view")
    def research_public_view(
        run_id: Annotated[str, typer.Argument()],
        include_execution_readiness: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Return the audited public presentation projection."""

        report = runtime().status(run_id)
        if report is None:
            emit({"status": "NOT_RUN"})
            raise typer.Exit(code=2)
        emit(
            public_investor_payload(
                investor_view_from_run(
                    report,
                    include_execution_readiness=include_execution_readiness,
                )
            )
        )

    @app.command("research-investor-answer-audit")
    def research_investor_answer_audit(
        answer_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        """Reject developer/runtime vocabulary before a normal investor answer is shown."""

        audit = audit_investor_answer(answer_file.read_text(encoding="utf-8"))
        emit(audit)
        if audit.status == "FAIL":
            raise typer.Exit(code=3)

    @app.command("research-capability-status")
    def research_capability_status(
        company_id: Annotated[str, typer.Argument()],
        market: Annotated[Market, typer.Option()],
        lookback_days: Annotated[int | None, typer.Option()] = None,
    ) -> None:
        """Read-only view of the active current-research capability schedule."""

        paths, state, _ = services()
        policy = load_default_current_research_policy(paths.root)
        resolved_lookback = lookback_days or policy.default_lookback_days
        schedule = CapabilityGraph(
            policy,
            current_acquisition().provider_registry,
            state,
        ).build(
            company_id,
            market,
            lookback_days=resolved_lookback,
            planned_at=datetime.now(UTC),
        )
        emit(schedule)

    @app.command("provider-dialect-status")
    def provider_dialect_status() -> None:
        """Read-only provider capability, health, transport, and dialect diagnostics."""

        paths, state, _ = services()
        registry = current_acquisition().provider_registry
        dialects = load_provider_dialects(paths.root / "configs" / "provider_dialects.yaml")
        profiles = load_transport_profiles(paths.root / "configs" / "transport_profiles.yaml")
        providers: list[dict[str, Any]] = []
        for definition in sorted(registry.providers, key=lambda item: item.provider_id):
            health, _ = state.get_provider_probe_health_snapshot(definition.provider_id)
            dialect = dialects.get(definition.provider_id)
            providers.append(
                {
                    "provider_id": definition.provider_id,
                    "capabilities": definition.capabilities,
                    "officiality": definition.officiality,
                    "transport": definition.transport,
                    "transport_profile": definition.transport_profile,
                    "health_status": health.get("status") if health else "NOT_PROBED",
                    "dialect_version": dialect.dialect_version if dialect else None,
                    "response_shape": dialect.response_shape if dialect else None,
                }
            )
        emit(
            {
                "registry_version": registry.registry_version,
                "transport_profiles": sorted(profiles),
                "providers": providers,
            }
        )

    @app.command("adaptive-edge-status")
    def adaptive_edge_status() -> None:
        """Read-only policies and hard safety boundaries for Agent-native adaptation."""

        paths, _, _ = services()
        current_policy = load_default_current_research_policy(paths.root)
        planner_policy = load_research_planner_policy(
            paths.root / "configs" / "research_planner_policy.yaml"
        )
        repair_policy = load_schema_repair_policy(
            paths.root / "configs" / "schema_repair_policy.yaml"
        )
        resource_policy = load_specialist_resource_policy(
            paths.root / "configs" / "specialist_resource_policy.yaml"
        )
        allocator_policy = load_portfolio_allocator_policy(
            paths.root / "configs" / "portfolio_allocators.yaml"
        )
        emit(
            {
                "current_research_policy": current_policy.policy_version,
                "planner_policy": planner_policy.policy_version,
                "mandatory_modules": planner_policy.mandatory_modules,
                "schema_repair_policy": repair_policy.policy_version,
                "schema_repair_minimum_raw_samples": repair_policy.minimum_raw_samples,
                "specialist_resource_policy": resource_policy.policy_version,
                "specialist_default_budget": resource_policy.default_budget,
                "specialist_maximum_budget": resource_policy.maximum_budget,
                "portfolio_allocator_policy": allocator_policy.policy_version,
                "portfolio_default_method": allocator_policy.default_method,
                "paper_ledger_write_allowed": False,
                "manual_last": current_policy.manual_last,
            }
        )

    @app.command("adaptive-edge-schema")
    def adaptive_edge_schema() -> None:
        """Read-only JSON schemas for planner, recovery, and schema-repair proposals."""

        emit(
            {
                "ResearchPlannerProposal": ResearchPlannerProposal.model_json_schema(),
                "ValidatedResearchPlan": ValidatedResearchPlan.model_json_schema(),
                "ProviderRecoveryProposal": ProviderRecoveryProposal.model_json_schema(),
                "ProviderRecoveryValidation": ProviderRecoveryValidation.model_json_schema(),
                "SchemaRepairProposal": SchemaRepairProposal.model_json_schema(),
                "SchemaRepairValidation": SchemaRepairValidation.model_json_schema(),
                "ProviderDialectCandidateRelease": (
                    ProviderDialectCandidateRelease.model_json_schema()
                ),
            }
        )

    @app.command("adaptive-plan-validate")
    def adaptive_plan_validate(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        proposal = ResearchPlannerProposal.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(adaptive_edge().validate_research_plan(proposal))

    @app.command("adaptive-recovery-validate")
    def adaptive_recovery_validate(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        proposal = ProviderRecoveryProposal.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(adaptive_edge().validate_recovery(proposal))

    @app.command("adaptive-schema-repair-validate")
    def adaptive_schema_repair_validate(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        proposal = SchemaRepairProposal.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(adaptive_edge().validate_schema_repair(proposal))

    @app.command("adaptive-schema-repair-admit")
    def adaptive_schema_repair_admit(
        validation_id: Annotated[str, typer.Argument()],
        approve: Annotated[bool, typer.Option("--approve")] = False,
    ) -> None:
        if not approve:
            raise typer.BadParameter("--approve is required for candidate dialect admission")
        emit(
            adaptive_edge().admit_schema_repair(
                validation_id,
                explicit_approval=True,
            )
        )

    @app.command("adaptive-artifact-audit")
    def adaptive_artifact_audit(
        artifact_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(adaptive_edge().audit_artifact(artifact_id))

    @app.command("adaptive-dialect-rollback")
    def adaptive_dialect_rollback(
        release_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(adaptive_edge().rollback_dialect_candidate(release_id))

    @app.command("phase7-study-ensure")
    def phase7_study_ensure(
        candidate_set_id: Annotated[str, typer.Option()] = "phase7-forward-live-v1",
    ) -> None:
        _, service = shadow()
        study, reused = ensure_default_formal_study(
            service,
            now=datetime.now().astimezone(),
            candidate_set_id=candidate_set_id,
        )
        status = service.status(study.study_id)
        emit(
            {
                "status": study.evidence_status,
                "study_id": study.study_id,
                "reused_existing": reused,
                "formal_event_count": status.formal_forward_event_count,
                "assignment_count": status.assignment_count,
                "observation_count": status.observation_count,
            }
        )

    @app.command("phase8-status")
    def phase8_status(
        study_id: Annotated[str | None, typer.Option("--study-id")] = None,
    ) -> None:
        _, service = shadow()
        emit(AdaptiveResearchStatusService(service).status(study_id))

    @app.command("institutional-research-schema")
    def institutional_research_schema() -> None:
        emit(
            {
                "EvidenceSufficiencyRequest": EvidenceSufficiencyRequest.model_json_schema(),
                "IndustryProfileDraft": IndustryProfileDraft.model_json_schema(),
                "CompanyEconomicsDraft": CompanyEconomicsDraft.model_json_schema(),
                "DriverTreeDraft": DriverTreeDraft.model_json_schema(),
                "ForecastScenarioInput": ForecastScenarioInput.model_json_schema(),
                "ValuationScenarioAssumption": ValuationScenarioAssumption.model_json_schema(),
                "MarketPriceAnchor": MarketPriceAnchor.model_json_schema(),
                "InstitutionalResearchFinalizeRequest": (
                    InstitutionalResearchFinalizeRequest.model_json_schema()
                ),
                "FundamentalModelBundle": FundamentalModelBundle.model_json_schema(),
                "InstitutionalDecisionContextDraft": (
                    InstitutionalDecisionContextDraft.model_json_schema()
                ),
                "InstitutionalDecisionContextBuildRequest": (
                    InstitutionalDecisionContextBuildRequest.model_json_schema()
                ),
                "InstitutionalDecisionContext": InstitutionalDecisionContext.model_json_schema(),
            }
        )

    @app.command("institutional-research-finalize")
    def institutional_research_finalize(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        request = InstitutionalResearchFinalizeRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(institutional().finalize(request))

    @app.command("institutional-decision-context-freeze")
    def institutional_decision_context_freeze(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        request = InstitutionalDecisionContextBuildRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(institutional().build_decision_context(request))

    @app.command("fundamental-model-status")
    def fundamental_model_status(company_id: Annotated[str, typer.Argument()]) -> None:
        emit(institutional().status(company_id))

    @app.command("fundamental-model-audit")
    def fundamental_model_audit(artifact_id: Annotated[str, typer.Argument()]) -> None:
        emit(institutional().audit(artifact_id))

    @app.command("research-run-plan")
    def research_run_plan(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        emit(runtime().plan(_load_request(request_file)))

    @app.command("research-run-company")
    def research_run_company(
        company_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option("--as-of")] = None,
        mode: Annotated[ResearchRunMode, typer.Option()] = ResearchRunMode.RECORDED_INPUT,
        sync_reference_inputs: Annotated[bool, typer.Option()] = True,
        institutional_research_required: Annotated[bool, typer.Option()] = False,
        fundamental_model_bundle_artifact_id: Annotated[str | None, typer.Option()] = None,
        institutional_decision_context_artifact_id: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        if as_of is None and mode is not ResearchRunMode.LIVE:
            raise typer.BadParameter("--as-of is required for recorded or historical research")
        resolved_as_of = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
        emit(
            runtime().run(
                ResearchRunRequest(
                    company_id=company_id,
                    as_of=resolved_as_of,
                    mode=mode,
                    sync_reference_inputs=sync_reference_inputs,
                    institutional_research_required=institutional_research_required,
                    frozen_inputs=(
                        ResearchRunFrozenInputs(
                            fundamental_model_bundle_artifact_id=(
                                fundamental_model_bundle_artifact_id
                            ),
                            institutional_decision_context_artifact_id=(
                                institutional_decision_context_artifact_id
                            ),
                        )
                        if (
                            fundamental_model_bundle_artifact_id
                            or institutional_decision_context_artifact_id
                        )
                        else None
                    ),
                )
            )
        )

    @app.command("research-run")
    def research_run(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        emit(runtime().run(_load_request(request_file)))

    @app.command("research-run-batch")
    def research_run_batch(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
        max_parallel_companies: Annotated[int, typer.Option("--max-parallel-companies")] = 3,
    ) -> None:
        payload = json.loads(request_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise typer.BadParameter("batch request must be a JSON object")
        raw_requests = payload.get("requests")
        if not isinstance(raw_requests, list) or not raw_requests:
            raise typer.BadParameter("batch request requires a non-empty requests list")
        if not 1 <= max_parallel_companies <= 4:
            raise typer.BadParameter("--max-parallel-companies must be between 1 and 4")
        budget_seconds = payload.get("budget_seconds", 2700)
        if type(budget_seconds) is not int or not 1 <= budget_seconds <= 2700:
            raise typer.BadParameter("batch budget_seconds must be between 1 and 2700")

        parsed: list[tuple[str, ResearchRunRequest]] = []
        batch_identity: list[dict[str, object]] = []
        for item in raw_requests:
            if not isinstance(item, dict):
                raise typer.BadParameter("every batch item must be an object")
            instrument_id = str(item.get("instrument_id") or "")
            request_payload = item.get("request")
            if not isinstance(request_payload, dict):
                raise typer.BadParameter("every batch item requires a request object")
            request = ResearchRunRequest.model_validate(request_payload)
            if instrument_id not in {
                f"XSHG:{request.company_id}",
                f"XSHE:{request.company_id}",
                f"BJSE:{request.company_id}",
            }:
                raise typer.BadParameter("batch instrument_id must match the research company")
            parsed.append((instrument_id, request))
            batch_identity.append(
                {
                    "instrument_id": instrument_id,
                    "request": request.model_dump(mode="json", exclude={"created_at"}),
                }
            )
        if len({instrument_id for instrument_id, _ in parsed}) != len(parsed):
            raise typer.BadParameter("batch instruments must be unique")

        identity_hash = content_hash(
            sorted(batch_identity, key=lambda item: str(item["instrument_id"]))
        )
        caller_request_id = payload.get("request_id")
        explicit_request = caller_request_id is not None
        if explicit_request and (
            not isinstance(caller_request_id, str) or not caller_request_id.strip()
        ):
            raise typer.BadParameter("batch request_id must be a non-empty string")
        scheduler_request_id = "current-research-batch:v2:" + content_hash(
            {"caller_request_id": caller_request_id}
            if explicit_request
            else {"inputs": identity_hash, "budget_seconds": budget_seconds}
        )
        scheduler = current_sla()
        task_specs = [
            (
                f"company-run-{instrument_id}",
                instrument_id,
                ResearchTaskCategory.NETWORK,
                (),
                content_hash(request.model_dump(mode="json", exclude={"created_at"})),
            )
            for instrument_id, request in sorted(parsed, key=lambda item: item[0])
        ]
        scheduler_run = scheduler.create_run(
            request_id=scheduler_request_id,
            task_specs=task_specs,
            budget_seconds=budget_seconds,
            legacy_batch_caller=(
                str(caller_request_id) if explicit_request else "current-research-batch"
            ),
            legacy_batch_explicit=explicit_request,
        )
        request_by_instrument = dict(parsed)
        request_by_node = {
            task.node_id: request_by_instrument[str(task.candidate_id)]
            for task in scheduler.tasks(scheduler_run.run_id)
        }

        from astock.research.process_worker import KillableResearchWorker, run_company_request

        worker_paths, worker_state, _ = services()
        handlers = {
            node_id: KillableResearchWorker(
                run_company_request,
                (
                    str(worker_paths.root), str(worker_state.path), str(worker_paths.objects),
                    str(worker_paths.parquet), request.model_dump_json(),
                ),
                deadline_at=scheduler_run.deadline_at,
                project_root=worker_paths.root,
                scratch_root=worker_paths.runtime / "worker_tmp",
            )
            for node_id, request in request_by_node.items()
        }
        # Child processes return proposals; only scheduler-accepted registry artifacts
        # enter the report and existing target pool below.
        scheduler_run = scheduler.execute(
            scheduler_run.run_id,
            handlers,
            resource_limits={ResearchTaskCategory.NETWORK: max_parallel_companies},
        )
        tasks = scheduler.tasks(scheduler_run.run_id)
        task_by_instrument = {task.candidate_id: task for task in tasks if task.candidate_id}
        _, batch_state, batch_objects = services()
        reports: dict[str, ResearchRunReport] = {}
        for task in tasks:
            if (
                task.status is not ResearchSchedulerTaskStatus.COMPLETED
                or task.result_artifact_id is None
                or task.candidate_id is None
            ):
                continue
            record = batch_state.artifact_record(task.result_artifact_id)
            if (
                record is None
                or str(record["type"]) != "ResearchRunReport"
                or not batch_objects.verify(str(record["object_hash"]))
            ):
                raise ValueError("accepted batch report is unavailable or failed integrity")
            report = ResearchRunReport.model_validate_json(
                batch_objects.get_bytes(str(record["object_hash"]))
            )
            expected_request = request_by_instrument[task.candidate_id]
            if (
                task.result_artifact_id != f"ResearchRunReport:{report.report_id}"
                or report.company_id != expected_request.company_id
                or report.as_of != expected_request.as_of
                or report.mode is not expected_request.mode
            ):
                raise ValueError("accepted batch report does not match its company request")
            request_record = batch_state.artifact_record(report.request_artifact_id)
            if (
                request_record is None
                or str(request_record["type"]) != "ResearchRunRequest"
                or str(request_record["object_hash"]) != report.request_object_hash
                or not batch_objects.verify(report.request_object_hash)
            ):
                raise ValueError("accepted batch report has invalid request provenance")
            stored_request = ResearchRunRequest.model_validate_json(
                batch_objects.get_bytes(report.request_object_hash)
            )
            if content_hash(
                stored_request.model_dump(mode="json", exclude={"created_at"})
            ) != task.input_fingerprint:
                raise ValueError("accepted batch report input contract differs from its task")
            reports[task.candidate_id] = report
        targets = []
        for instrument_id, request in parsed:
            report = reports.get(instrument_id)
            task = task_by_instrument.get(instrument_id)
            if report is not None:
                report_artifact_id = f"ResearchRunReport:{report.report_id}"
                module_versions = {
                    name: reference.object_hash
                    for name, reference in report.output_artifacts.items()
                }
                target_state = (
                    ResearchTargetState.RESEARCHED
                    if report.status is ResearchRunStatus.COMPLETE
                    else ResearchTargetState.REVIEW_DUE
                )
                targets.append(
                    scheduler.upsert_target(
                        instrument_id=instrument_id,
                        company_id=request.company_id,
                        state=target_state,
                        source_reason="bounded current-research batch",
                        dependency_fingerprint=content_hash(
                            module_versions or {"request": identity_hash}
                        ),
                        module_versions=module_versions,
                        triggers=("FINANCIAL_REPORT", "GOVERNANCE_EVENT", "PRICE_TRIGGER"),
                        latest_result_artifact_id=report_artifact_id,
                        priority=20 if target_state is ResearchTargetState.REVIEW_DUE else 10,
                        last_review_at=report.as_of,
                    )
                )
            elif task is not None:
                targets.append(
                    scheduler.upsert_target(
                        instrument_id=instrument_id,
                        company_id=request.company_id,
                        state=ResearchTargetState.SUSPENDED,
                        source_reason="bounded current-research batch failure",
                        dependency_fingerprint=task.input_fingerprint,
                        module_versions={},
                        triggers=("PROGRAM_RETRY",),
                        priority=30,
                        invalidation_reason=task.error_code or "PROGRAM_PATH_UNAVAILABLE",
                    )
                )
        emit(
            {
                "scheduler_run": scheduler_run,
                "tasks": tasks,
                "reports": reports,
                "targets": targets,
            }
        )

    @app.command("research-sla-status")
    def research_sla_status(run_id: Annotated[str, typer.Argument()]) -> None:
        scheduler = current_sla()
        emit({"tasks": scheduler.tasks(run_id), "targets": scheduler.active_targets()})

    @app.command("research-sla-takeover-packet")
    def research_sla_takeover_packet(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        payload = json.loads(request_file.read_text(encoding="utf-8"))
        emit(
            current_sla().takeover_packet(
                request_id=str(payload["request_id"]),
                task_id=str(payload["task_id"]),
                trusted_artifact_ids=list(payload.get("trusted_artifact_ids", [])),
                attempted_actions=list(payload.get("attempted_actions", [])),
                missing_requirement=str(payload["missing_requirement"]),
                allowed_write_paths=list(payload.get("allowed_write_paths", [])),
                expected_output_schema=str(payload["expected_output_schema"]),
                dependency_fingerprint=str(payload["dependency_fingerprint"]),
            )
        )

    @app.command("research-sla-takeover-apply")
    def research_sla_takeover_apply(
        result_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        result = LlmTakeoverResult.model_validate_json(result_file.read_text(encoding="utf-8"))
        from astock.research.takeover_validation import validate_research_run_takeover

        scheduler = current_sla()
        task = next(
            (item for item in scheduler.tasks(result.run_id) if item.task_id == result.task_id),
            None,
        )
        if task is None:
            raise typer.BadParameter("takeover task is unavailable for this run")
        _, state, objects = services()
        emit(
            scheduler.apply_takeover_result(
                result,
                validate_result=lambda artifact_id: validate_research_run_takeover(
                    artifact_id,
                    task=task,
                    state=state,
                    objects=objects,
                    audit_run=runtime().audit,
                ),
            )
        )

    @app.command("research-run-status")
    def research_run_status(run_id: Annotated[str, typer.Argument()]) -> None:
        result = runtime().status(run_id)
        emit(result if result is not None else {"status": "NOT_RUN", "run_id": run_id})

    @app.command("research-status")
    def research_status(run_id: Annotated[str, typer.Argument()]) -> None:
        result = runtime().status(run_id)
        emit(result if result is not None else {"status": "NOT_RUN", "run_id": run_id})

    @app.command("research-run-audit")
    def research_run_audit(run_id: Annotated[str, typer.Argument()]) -> None:
        emit(runtime().audit(run_id))

    @app.command("research-audit")
    def research_audit(run_id: Annotated[str, typer.Argument()]) -> None:
        emit(runtime().audit(run_id))

    @app.command("research-run-recover")
    def research_run_recover(run_id: Annotated[str, typer.Argument()]) -> None:
        emit(runtime().recover(run_id))

    @app.command("research-recover")
    def research_recover(run_id: Annotated[str, typer.Argument()]) -> None:
        emit(runtime().recover(run_id))

    @app.command("research-run-benchmark")
    def research_run_benchmark(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        emit(runtime().benchmark(_load_request(request_file)))

    @app.command("trade-plan-view")
    def trade_plan_view(
        classified_protocol_artifact_id: Annotated[str, typer.Argument()],
        reference_price_fen: Annotated[int | None, typer.Option()] = None,
        reference_price_source: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(
            trade_view().build(
                classified_protocol_artifact_id,
                reference_price_fen=reference_price_fen,
                reference_price_source=reference_price_source,
            )
        )

    @app.command("trading-classification-baseline-capture")
    def trading_classification_baseline_capture(
        company_id: Annotated[str, typer.Argument()],
        live: Annotated[bool, typer.Option()] = False,
    ) -> None:
        service = runtime().classification
        artifact_id, baseline = service.capture_official_corporate_action_baseline(
            company_id,
            live=live,
        )
        emit({"artifact_id": artifact_id, "baseline": baseline})

    @app.command("trading-classification-resolve")
    def trading_classification_resolve(
        company_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str, typer.Option("--as-of")],
        live: Annotated[bool, typer.Option()] = False,
        sync_reference_inputs: Annotated[bool, typer.Option()] = True,
    ) -> None:
        service = runtime().classification
        emit(
            service.resolve(
                company_id,
                datetime.fromisoformat(as_of),
                live=live,
                sync_reference_inputs=sync_reference_inputs,
            )
        )

    @app.command("trading-classification-freeze")
    def trading_classification_freeze(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        _, state, objects = services()
        draft = TradingClassificationDraft.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        emit(TradingClassificationService(state, objects).freeze(draft))

    @app.command("trading-classification-status")
    def trading_classification_status(
        artifact_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        _, state, objects = services()
        emit(
            TradingClassificationService(state, objects).status(
                artifact_id,
                as_of=_parse_optional_datetime(as_of),
            )
        )

    @app.command("trading-classification-audit")
    def trading_classification_audit(
        artifact_id: Annotated[str, typer.Argument()],
    ) -> None:
        _, state, objects = services()
        emit(TradingClassificationService(state, objects).audit(artifact_id))

    @app.command("research-runtime-readiness")
    def research_runtime_readiness(
        knowledge_run_id: Annotated[str, typer.Argument()],
    ) -> None:
        emit(readiness().provider_readiness(knowledge_run_id))

    @app.command("holding-due")
    def holding_due(
        position_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(readiness().holding_due(position_id, as_of=_parse_optional_datetime(as_of)))

    @app.command("holding-prepare")
    def holding_prepare(
        position_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(readiness().holding_prepare(position_id, as_of=_parse_optional_datetime(as_of)))

    @app.command("paper-replay-checkpoint")
    def paper_replay_checkpoint(
        symbol: Annotated[str, typer.Argument()],
        account_id: Annotated[str, typer.Option()] = "default",
    ) -> None:
        emit(readiness().paper_replay_checkpoint(account_id, symbol))

    @app.command("paper-recovery-plan")
    def paper_recovery_plan(
        symbol: Annotated[str, typer.Argument()],
        account_id: Annotated[str, typer.Option()] = "default",
    ) -> None:
        emit(readiness().paper_recovery_plan(account_id, symbol))


__all__ = ["register_research_runtime_commands"]
