"""Additive CLI registration for the recoverable research runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.adaptive.service import AdaptiveResearchStatusService
from astock.candidates.cli_ext import register_candidate_input_commands
from astock.market_data.storage import CanonicalMarketStore
from astock.monitoring.cli import register_continuous_monitor_commands
from astock.portfolio.allocators import load_portfolio_allocator_policy
from astock.portfolio.cli import register_portfolio_commands
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
    audit_investor_answer,
    investor_view_from_acquisition,
    investor_view_from_run,
)
from astock.research.production_cli import register_research_production_commands
from astock.research.resource_policy import load_specialist_resource_policy
from astock.research.runtime import ResearchRunService
from astock.research.runtime_readiness import ResearchRuntimeReadinessService
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
from astock.schemas.reference_data import Market
from astock.schemas.research_runtime import (
    ResearchRunFrozenInputs,
    ResearchRunMode,
    ResearchRunRequest,
    TradingClassificationDraft,
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
    register_portfolio_vnext_commands(app, services, emit)
    register_prospective_governance_commands(app, services, emit)
    register_research_production_commands(app, services, emit)
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

    def current_acquisition() -> CurrentResearchAcquisitionService:
        paths, state, objects = services()
        return CurrentResearchAcquisitionService(paths, state, objects)

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
        report = current_acquisition().get(report_id)
        if report is None:
            emit({"status": "NOT_FOUND"})
            raise typer.Exit(code=2)
        emit(investor_view_from_acquisition(report))

    @app.command("research-investor-view")
    def research_investor_view(
        run_id: Annotated[str, typer.Argument()],
        include_execution_readiness: Annotated[bool, typer.Option()] = False,
    ) -> None:
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
                "broker_execution_allowed": False,
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
