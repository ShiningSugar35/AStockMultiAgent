"""Additive CLI registration for the recoverable research runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from astock.adaptive.service import AdaptiveResearchStatusService
from astock.candidates.cli_ext import register_candidate_input_commands
from astock.market_data.storage import CanonicalMarketStore
from astock.portfolio.cli import register_portfolio_commands
from astock.research.institutional import InstitutionalResearchService
from astock.research.knowledge_port import KnowledgeSkillProvider
from astock.research.runtime import ResearchRunService
from astock.research.runtime_readiness import ResearchRuntimeReadinessService
from astock.research.trade_view import TradePlanViewService
from astock.research.trading_classification import TradingClassificationService
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
from astock.schemas.research_runtime import (
    ResearchRunFrozenInputs,
    ResearchRunMode,
    ResearchRunRequest,
    TradingClassificationDraft,
)
from astock.shadow.config import load_shadow_evaluation_policy
from astock.shadow.formal_study import ensure_default_formal_study
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
    register_portfolio_commands(app, services, emit)

    def runtime() -> ResearchRunService:
        paths, state, objects = services()
        return ResearchRunService(
            project_root=paths.root,
            state=state,
            objects=objects,
            reference_parquet_root=paths.parquet,
            knowledge_provider=knowledge_provider_factory(state, objects),
        )

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
        as_of: Annotated[str, typer.Option("--as-of")],
        mode: Annotated[ResearchRunMode, typer.Option()] = ResearchRunMode.RECORDED_INPUT,
        institutional_research_required: Annotated[bool, typer.Option()] = False,
        fundamental_model_bundle_artifact_id: Annotated[str | None, typer.Option()] = None,
        institutional_decision_context_artifact_id: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(
            runtime().plan(
                ResearchRunRequest(
                    company_id=company_id,
                    as_of=datetime.fromisoformat(as_of),
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
        as_of: Annotated[str, typer.Option("--as-of")],
        mode: Annotated[ResearchRunMode, typer.Option()] = ResearchRunMode.RECORDED_INPUT,
        sync_reference_inputs: Annotated[bool, typer.Option()] = True,
        institutional_research_required: Annotated[bool, typer.Option()] = False,
        fundamental_model_bundle_artifact_id: Annotated[str | None, typer.Option()] = None,
        institutional_decision_context_artifact_id: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        emit(
            runtime().run(
                ResearchRunRequest(
                    company_id=company_id,
                    as_of=datetime.fromisoformat(as_of),
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
