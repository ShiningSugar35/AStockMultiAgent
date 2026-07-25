"""Stable local CLI used directly and by project Repo Skills."""

from __future__ import annotations

import json
import platform
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import typer
from pydantic import BaseModel, TypeAdapter, ValidationError

from astock import __version__
from astock.adaptive import AdaptiveResearchStatusService
from astock.books import PrivateDocxIngestService, PrivatePdfIngestService
from astock.candidates import (
    CandidateParquetStore,
    CandidateRepository,
    CandidateScanService,
    ProductionCandidateInputVerifier,
    load_candidate_scan_config,
)
from astock.committee import CommitteeService, load_committee_rules
from astock.core.atomic import atomic_write_text
from astock.core.codex_runs import (
    CodexRunService,
    build_context_budget,
    registered_committee_artifact_types,
    registered_phase4_artifact_types,
    registered_shadow_artifact_types,
    registered_strict_artifact_types,
)
from astock.core.errors import AStockError, FailureClass
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import (
    CninfoDisclosureProvider,
    DisclosureSyncService,
    DocumentPageRepository,
    DocumentRepository,
    PdfParseService,
)
from astock.financial_integrity import (
    FinancialIntegrityRepository,
    FinancialIntegrityService,
)
from astock.financial_sources import FinancialSourceParquetStore, FinancialSourceService
from astock.knowledge import (
    DistillationRepository,
    KnowledgeCoverageAuditService,
    KnowledgeDistillationService,
    KnowledgeDraftService,
    KnowledgeRepository,
    KnowledgeStructureProfileService,
    ParquetKnowledgeStore,
    ParquetSemanticStore,
    SemanticEmbeddingService,
    SemanticFunnelRepository,
    SemanticFunnelService,
    SemanticPacketService,
    SentenceTransformerBackend,
    ZhihuArticleRecoveryService,
    ZhihuCollectionService,
    ZhihuFullCaptureSession,
    ZhihuLoopbackCaptureSession,
    ZhihuManualTaskService,
    ZhihuPythonRecoveryService,
    ZhihuResponseImportService,
    build_coordinator_capture_extension,
    create_loopback_capture_server,
    default_model_directory,
    get_knowledge_source,
    install_local_model,
    load_distillation_rules,
    load_knowledge_sources,
    load_semantic_funnel_config,
    load_zhihu_endpoint_templates,
    loopback_installer_url,
    loopback_status_url,
    serve_loopback_capture,
    verify_local_model,
)
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.market_data.storage import (
    CanonicalMarketStore,
    ParquetMarketStore,
    canonical_manifest_path,
)
from astock.market_data.sync import MarketSyncService
from astock.paper_trading import (
    LedgerService,
    MarketReferencePaperVerifier,
    PaperOperationService,
    PaperReplayService,
    load_fee_schedule,
    load_paper_authorization_keys,
    load_paper_confirmation,
    load_paper_operation,
    load_paper_trading_rules,
)
from astock.providers import (
    EastMoney5mProvider,
    ProviderProbeService,
    Sina5mProvider,
    load_provider_registry,
)
from astock.research import (
    Phase4ChainService,
    PositionLifecycleService,
    EvidenceCollectionRunService,
    EvidenceCollectionTaskService,
    ResearchCoreService,
    ResearchDiagnosticsService,
    ResearchRequestService,
    ResearchRepository,
    ResearchSkillService,
    load_position_lifecycle_config,
    load_research_core_config,
    load_research_diagnostic_config,
    load_research_skill_registry,
    validate_registry_open_source_audits,
    verify_open_source_tree,
)
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    BaseCaseBuildRequest,
    CandidateAuditStatus,
    CandidateScanRequest,
    CandidateScanStatus,
    CodexArtifactReference,
    CodexRunInputManifest,
    CommitteeAccessPolicy,
    CommitteeDecisionRequest,
    ContextBudgetReport,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    DistillationClassRuleSet,
    DocumentType,
    EvidenceFreezeRequest,
    FinancialAuditRequest,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialSourceReleaseStatus,
    HoldingReviewRequest,
    InstrumentType,
    KnowledgeSourceRegistry,
    Market,
    MarketRegimeFeatures,
    PositionLifecycleConfig,
    PositionPlanCreateRequest,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ResearchCoreConfig,
    ResearchDiagnosticConfig,
    ResearchMemoComposeRequest,
    ResearchMemoComposeRequestV2,
    ResearchSkillRegistry,
    SemanticFunnelRun,
    ShadowDecisionAssignmentRequest,
    ShadowExecutionObservationDraft,
    ShadowStudyCreateRequest,
    SpecialistDeltaBuildRequest,
    SpecialistDiagnosticRequest,
    SpecialistDiagnosticRequestV2,
    SpecialistRouteRequest,
    ZhihuContentType,
    ZhihuEndpointTemplateRegistry,
    ZhihuResponseKind,
)
from astock.settings import ProjectPaths
from astock.shadow import (
    ParquetShadowStore,
    ShadowEvaluationService,
    load_shadow_evaluation_policy,
)

app = typer.Typer(
    name="astock",
    help="Deterministic A-share research and paper-trading foundation.",
    no_args_is_help=True,
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _services() -> tuple[ProjectPaths, StateStore, ObjectStore]:
    paths = ProjectPaths.discover()
    paths.ensure_directories()
    state = StateStore(paths.state_db, paths.root / "migrations")
    state.migrate()
    return paths, state, ObjectStore(paths.objects)


def _knowledge_sources(paths: ProjectPaths) -> KnowledgeSourceRegistry:
    return load_knowledge_sources(paths.root / "configs" / "knowledge_sources.yaml")


def _zhihu_endpoint_templates(paths: ProjectPaths) -> ZhihuEndpointTemplateRegistry:
    return load_zhihu_endpoint_templates(paths.root / "configs" / "zhihu_endpoint_templates.yaml")


def _distillation_rules(paths: ProjectPaths) -> DistillationClassRuleSet:
    return load_distillation_rules(paths.root / "configs" / "knowledge_distillation_rules.yaml")


def _semantic_funnel_service(
    paths: ProjectPaths,
    state: StateStore,
    objects: ObjectStore,
) -> SemanticFunnelService:
    return SemanticFunnelService(
        KnowledgeRepository(state),
        SemanticFunnelRepository(state),
        objects,
        load_semantic_funnel_config(
            paths.root / "configs" / "knowledge_semantic_funnel.yaml"
        ),
        _distillation_rules(paths),
    )


def _research_core(paths: ProjectPaths) -> ResearchCoreConfig:
    return load_research_core_config(paths.root / "configs" / "research_core.yaml")


def _research_skills(paths: ProjectPaths) -> ResearchSkillRegistry:
    return load_research_skill_registry(paths.root / "configs" / "research_skills.yaml")


def _research_diagnostics(paths: ProjectPaths) -> ResearchDiagnosticConfig:
    return load_research_diagnostic_config(paths.root / "configs" / "research_diagnostics.yaml")


def _position_lifecycle(paths: ProjectPaths) -> PositionLifecycleConfig:
    return load_position_lifecycle_config(paths.root / "configs" / "position_lifecycle.yaml")


def _committee_service(
    paths: ProjectPaths,
    state: StateStore,
    objects: ObjectStore,
) -> CommitteeService:
    return CommitteeService(
        state,
        objects,
        load_committee_rules(paths.root / "configs" / "committee_rules.yaml"),
    )


def _shadow_service(
    paths: ProjectPaths,
    state: StateStore,
    objects: ObjectStore,
) -> ShadowEvaluationService:
    return ShadowEvaluationService(
        state,
        objects,
        load_shadow_evaluation_policy(paths.root / "configs" / "shadow_evaluation.yaml"),
        ParquetShadowStore(paths.parquet),
    )


def _phase4_chain(
    paths: ProjectPaths,
    state: StateStore,
    objects: ObjectStore,
) -> Phase4ChainService:
    return Phase4ChainService(
        state,
        objects,
        _research_core(paths),
        _research_skills(paths),
        _research_diagnostics(paths),
        _position_lifecycle(paths),
    )


def _context_budget_with_registered(
    service: CodexRunService,
    *,
    skills: list[str],
    artifact_paths: list[Path],
    artifact_ids: list[str],
) -> tuple[ContextBudgetReport, list[CodexArtifactReference]]:
    budget = build_context_budget(skills=skills, artifact_paths=artifact_paths)
    references: list[CodexArtifactReference] = []
    seen_hashes: set[str] = set()
    duplicates = list(budget.duplicate_inputs_avoided)
    registered_bytes = 0
    for artifact_id in dict.fromkeys(artifact_ids):
        reference = service.resolve_artifact_reference(artifact_id)
        if reference.object_sha256 in seen_hashes:
            duplicates.append(artifact_id)
            continue
        seen_hashes.add(reference.object_sha256)
        references.append(reference)
        registered_bytes += len(service.object_store.get_bytes(reference.object_sha256))
    total_bytes = budget.artifact_byte_size + registered_bytes
    return (
        budget.model_copy(
            update={
                "selected_skills": list(dict.fromkeys(skills)),
                "selected_artifacts": [
                    *budget.selected_artifacts,
                    *(item.artifact_id for item in references),
                ],
                "artifact_byte_size": total_bytes,
                "estimated_text_tokens": (total_bytes + 3) // 4,
                "duplicate_inputs_avoided": duplicates,
            }
        ),
        references,
    )


def _emit(value: Any) -> None:
    typer.echo(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True))


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (datetime, Path, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _parse_local_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        if len(value) <= 10 and end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def _request(symbol: str, market: Market, start: str | None, end: str | None) -> BarRequest:
    now = datetime.now(_SHANGHAI)
    requested_start = _parse_local_datetime(start) if start else now - timedelta(days=45)
    requested_end = _parse_local_datetime(end, end_of_day=True) if end else now
    return BarRequest(
        symbol=symbol,
        market=market,
        instrument_type=(InstrumentType.INDEX if market == Market.INDEX else InstrumentType.STOCK),
        requested_start=requested_start,
        requested_end=requested_end,
        adjustment_mode=AdjustmentMode.NONE,
    )


def _sync(symbol: str, market: Market, start: str | None, end: str | None) -> dict[str, Any]:
    paths, state, objects = _services()
    providers = [
        EastMoney5mProvider(objects, state),
        Sina5mProvider(objects, state),
    ]
    service = MarketSyncService(
        providers,
        state,
        ParquetMarketStore(paths.parquet, "market_observation"),
        CanonicalMarketStore(paths.parquet, paths.manifests),
    )
    result = service.sync_5m(_request(symbol, market, start, end))
    return {
        "job_id": result.job_id,
        "providers": [
            {
                "provider_id": batch.provider_id,
                "bar_count": batch.bar_count,
                "actual_start": batch.actual_start,
                "actual_end": batch.actual_end,
                "raw_snapshot_id": batch.raw_snapshot_id,
            }
            for batch in result.batches
        ],
        "failures": result.failures,
        "canonical_updated": result.canonical_updated,
        "canonical_publish_reason": result.canonical_publish_reason,
        "quality": result.canonical_report,
        "canonical_manifest": result.canonical_manifest,
    }


def _disclosure_request(
    symbol: str,
    exchange: DisclosureExchange,
    start: str,
    end: str,
    category: DisclosureCategory,
    keyword: str,
    page_size: int,
) -> DisclosureSearchRequest:
    return DisclosureSearchRequest(
        symbol=symbol,
        exchange=exchange,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        category=category,
        keyword=keyword,
        page_size=page_size,
    )


@app.command("init")
def initialize(
    account_id: Annotated[str, typer.Option(help="Paper-account identifier.")] = "default",
    initial_cash_yuan: Annotated[
        str, typer.Option(help="Initial paper cash in yuan; only used on first initialization.")
    ] = "1000000",
) -> None:
    """Create runtime directories, migrate state, and initialize a paper account."""

    paths, state, _ = _services()
    initial_cash_fen = int((Decimal(initial_cash_yuan) * 100).quantize(Decimal("1")))
    result = LedgerService(state).initialize_account(account_id, initial_cash_fen)
    _emit(
        {
            "project_root": str(paths.root),
            "runtime_root": str(paths.runtime),
            "database": str(paths.state_db),
            "database_integrity": state.integrity_check(),
            "account_id": account_id,
            "account_initialized": bool(result and result.created),
            "version": __version__,
        }
    )


@app.command("probe")
def probe() -> None:
    """Report local deterministic capabilities without calling the network."""

    paths, state, objects = _services()
    providers = [EastMoney5mProvider(objects, state), Sina5mProvider(objects, state)]
    shadow = _shadow_service(paths, state, objects)
    adaptive = AdaptiveResearchStatusService(shadow).status()
    _emit(
        {
            "version": __version__,
            "python": platform.python_version(),
            "python_supported": sys.version_info[:2] == (3, 12),
            "project_root": str(paths.root),
            "state_integrity": state.integrity_check(),
            "modes": {
                "CODEX_INTERACTIVE_MODE": "AVAILABLE",
                "DETERMINISTIC_MODE": "AVAILABLE",
                "OPENAI_COMPATIBLE_MODE": "OPTIONAL_DISABLED",
                "QMT": "UNAVAILABLE",
            },
            "codex_artifacts": {
                "input_manifest_version": "codex-run-input-v2",
                "registered_output_required": True,
                "strict_phase4_types": registered_phase4_artifact_types(),
                "strict_committee_types": registered_committee_artifact_types(),
                "strict_shadow_types": registered_shadow_artifact_types(),
                "strict_all_types": registered_strict_artifact_types(),
            },
            "candidate_registry": {
                "status": "AVAILABLE",
                "rules_version": load_candidate_scan_config(
                    paths.root / "configs" / "candidate_scan.yaml"
                ).rules_version,
                "network_access": False,
                "broker_execution": False,
                "paper_ledger_write": False,
                "committee_write": False,
            },
            "committee": {
                "status": "AVAILABLE",
                "rules_version": _committee_service(
                    paths, state, objects
                ).configured_rules.rules_version,
                "supported_input_types": CommitteeService.supported_input_types(),
                "network_access": False,
                "api_access": False,
                "mcp_access": False,
                "browser_access": False,
                "full_document_access": False,
                "provider_narrative": "OPTIONAL_DISABLED",
            },
            "shadow_evaluation": {
                "status": shadow.status().status,
                "policy_version": shadow.configured_policy.policy_version,
                "weights_frozen": True,
                "minimum_independent_decisions": (
                    shadow.configured_policy.minimum_independent_decisions
                ),
                "phase8_minimum_observation_months": (
                    shadow.configured_policy.phase8_observation_months
                ),
                "network_access": False,
                "broker_execution": False,
                "main_paper_ledger_write": False,
                "online_weight_changes": False,
            },
            "adaptive_research": {
                "implementation_status": adaptive.implementation_status,
                "status": adaptive.capability_status,
                "shadow_policy_version": adaptive.shadow_policy_version,
                "phase8_admission_status": adaptive.phase8_admission_status,
                "adaptive_weights": adaptive.adaptive_weights_enabled,
                "online_learning": adaptive.online_learning_allowed,
                "main_paper_ledger_write": (adaptive.main_paper_ledger_write_allowed),
                "broker_execution": adaptive.broker_execution_allowed,
                "next_permitted_stage": adaptive.next_permitted_stage,
                "reason_codes": adaptive.reason_codes,
                "sample_gaps": {
                    "observation_months": adaptive.observation_month_gap,
                    "independent_decisions": adaptive.independent_decision_gap,
                    "walk_forward_folds": (adaptive.qualifying_walk_forward_fold_gap),
                    "market_regimes": adaptive.qualifying_market_regime_gap,
                },
            },
            "providers": [provider.capability() for provider in providers],
        }
    )


def _provider_probe_service(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore
) -> ProviderProbeService:
    return ProviderProbeService(
        project_root=paths.root,
        registry=load_provider_registry(paths.root / "configs" / "provider_registry.yaml"),
        state=state,
        objects=objects,
    )


def _market_reference_service(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore
) -> MarketReferenceService:
    return MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(paths.parquet),
        paths.root / "tests" / "fixtures" / "reference",
    )


def _paper_operation_service(
    paths: ProjectPaths,
    state: StateStore,
    objects: ObjectStore,
    fee_rules: Path | None = None,
) -> PaperOperationService:
    schedule = load_fee_schedule(fee_rules or paths.root / "configs" / "fee_rules.yaml")
    paper_rules_path = paths.root / "configs" / "paper_trading_rules.yaml"
    return PaperOperationService(
        state,
        objects,
        LedgerService(state, objects),
        MarketReferencePaperVerifier(_market_reference_service(paths, state, objects)),
        schedule,
        trusted_confirmation_keys=load_paper_authorization_keys(paper_rules_path),
        trading_rules=load_paper_trading_rules(
            paper_rules_path
        ),
    )


def _financial_source_service(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore
) -> FinancialSourceService:
    return FinancialSourceService(
        state,
        objects,
        FinancialSourceParquetStore(paths.parquet / "financial_sources"),
        paths.root,
    )


def _candidate_scan_service(
    paths: ProjectPaths, state: StateStore, objects: ObjectStore
) -> CandidateScanService:
    parquet = CandidateParquetStore(paths.parquet / "candidates")
    return CandidateScanService(
        CandidateRepository(state),
        objects,
        parquet,
        load_candidate_scan_config(paths.root / "configs" / "candidate_scan.yaml"),
        ProductionCandidateInputVerifier(
            state,
            objects,
            paths.parquet,
            paths.root / "tests" / "fixtures" / "reference",
        ),
    )


@app.command("provider-list")
def provider_list() -> None:
    """List declared providers and durable health without calling the network."""

    paths, state, objects = _services()
    service = _provider_probe_service(paths, state, objects)
    _emit(
        {
            "schema_version": "provider-list-v1",
            "registry_version": service.registry.registry_version,
            "providers": [item.model_dump(mode="json") for item in service.list()],
        }
    )


@app.command("provider-status")
def provider_status(
    provider_id: Annotated[
        str | None, typer.Argument(help="Registered provider identifier; omit to list all.")
    ] = None,
) -> None:
    """Read verified provider health without calling the network."""

    paths, state, objects = _services()
    service = _provider_probe_service(paths, state, objects)
    try:
        if provider_id is not None:
            _emit(service.status(provider_id).model_dump(mode="json"))
        else:
            _emit(
                {
                    "schema_version": "provider-status-list-v1",
                    "registry_version": service.registry.registry_version,
                    "providers": [item.model_dump(mode="json") for item in service.list()],
                }
            )
    except ValueError:
        _emit({"status": "FAILED", "failure_code": "UNKNOWN_PROVIDER"})
        raise typer.Exit(code=1) from None


@app.command("provider-probe")
def provider_probe(
    provider_id: Annotated[
        str | None, typer.Argument(help="Registered provider identifier; omit to probe all.")
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Explicitly call the remote provider; default uses recorded fixtures.",
        ),
    ] = False,
    probe_key: Annotated[
        str | None,
        typer.Option(
            "--probe-key",
            help="Stable retry/idempotency key; required for live probes.",
        ),
    ] = None,
) -> None:
    """Run a recorded probe, or an explicitly requested low-frequency live probe."""

    paths, state, objects = _services()
    service = _provider_probe_service(paths, state, objects)
    if live and probe_key is None:
        _emit({"status": "FAILED", "failure_code": "PROBE_KEY_REQUIRED"})
        raise typer.Exit(code=1)
    try:
        provider_ids = (
            [provider_id]
            if provider_id is not None
            else [item.provider_id for item in service.registry.providers]
        )
        results = [service.probe(item, live=live, probe_key=probe_key) for item in provider_ids]
        if provider_id is not None:
            _emit(results[0].model_dump(mode="json"))
        else:
            _emit(
                {
                    "schema_version": "provider-probe-list-v1",
                    "registry_version": service.registry.registry_version,
                    "probe_mode": "LIVE" if live else "RECORDED",
                    "providers": [item.model_dump(mode="json") for item in results],
                }
            )
    except ValueError:
        _emit({"status": "FAILED", "failure_code": "UNKNOWN_OR_UNSUPPORTED_PROVIDER"})
        raise typer.Exit(code=1) from None
    except RuntimeError:
        _emit({"status": "FAILED", "failure_code": "CORRUPT_PROVIDER_STATE"})
        raise typer.Exit(code=1) from None


@app.command("sync-5m")
def sync_5m(
    symbol: Annotated[str, typer.Argument(help="Six-digit stock or index code.")],
    market: Annotated[Market, typer.Option(case_sensitive=False)] = Market.XSHG,
    start: Annotated[str | None, typer.Option(help="ISO local start date/time.")] = None,
    end: Annotated[str | None, typer.Option(help="ISO local end date/time.")] = None,
) -> None:
    """Synchronize unadjusted 5m bars through both free providers when available."""

    try:
        _emit(_sync(symbol, market, start, end))
    except AStockError as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": exc.failure_class.value,
                "retryable": exc.retryable,
                "message": str(exc),
                "details": exc.details,
            }
        )
        raise typer.Exit(code=2) from exc


@app.command("sync-market")
def sync_market(
    symbol: Annotated[str, typer.Argument(help="Six-digit stock or index code.")],
    market: Annotated[Market, typer.Option(case_sensitive=False)] = Market.XSHG,
    start: Annotated[str | None, typer.Option()] = None,
    end: Annotated[str | None, typer.Option()] = None,
) -> None:
    """M1 market sync entry; currently delegates to the verified 5m path."""

    try:
        result = _sync(symbol, market, start, end)
        result["implemented_frequency"] = "5m"
        _emit(result)
    except AStockError as exc:
        _emit({"status": "FAILED", "failure_class": exc.failure_class.value, "message": str(exc)})
        raise typer.Exit(code=2) from exc


@app.command("sync-instruments")
def sync_instruments(
    market: Annotated[Market | None, typer.Option(case_sensitive=False)] = None,
    live: Annotated[bool, typer.Option("--live")] = False,
) -> None:
    """Publish an immutable instrument-master release; recorded unless --live is explicit."""

    paths, state, objects = _services()
    try:
        report = _market_reference_service(paths, state, objects).sync_instruments(
            market, live=live
        )
    except (AStockError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "REFERENCE_SYNC_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if report.status is ReferenceCoverageStatus.FAILED:
        raise typer.Exit(code=2)


@app.command("sync-calendar")
def sync_calendar(
    exchange: Annotated[Market, typer.Option(case_sensitive=False)],
    start: Annotated[str, typer.Option(help="Inclusive YYYY-MM-DD date.")],
    end: Annotated[str, typer.Option(help="Inclusive YYYY-MM-DD date.")],
    live: Annotated[bool, typer.Option("--live")] = False,
) -> None:
    """Publish a point-in-time exchange-calendar release."""

    paths, state, objects = _services()
    try:
        report = _market_reference_service(paths, state, objects).sync_calendar(
            exchange, date.fromisoformat(start), date.fromisoformat(end), live=live
        )
    except (AStockError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "REFERENCE_SYNC_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if report.status is ReferenceCoverageStatus.FAILED:
        raise typer.Exit(code=2)


@app.command("sync-daily")
def sync_daily(
    symbol: Annotated[str, typer.Argument(help="Six-digit stock or index code.")],
    market: Annotated[Market, typer.Option(case_sensitive=False)],
    start: Annotated[str, typer.Option(help="Inclusive YYYY-MM-DD date.")],
    end: Annotated[str, typer.Option(help="Inclusive YYYY-MM-DD date.")],
    live: Annotated[bool, typer.Option("--live")] = False,
) -> None:
    """Publish unadjusted daily observations with Shanghai close semantics."""

    paths, state, objects = _services()
    try:
        report = _market_reference_service(paths, state, objects).sync_daily(
            symbol,
            market,
            date.fromisoformat(start),
            date.fromisoformat(end),
            live=live,
        )
    except (AStockError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "REFERENCE_SYNC_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if report.status is ReferenceCoverageStatus.FAILED:
        raise typer.Exit(code=2)


@app.command("sync-corporate-actions")
def sync_corporate_actions(
    symbol: Annotated[str, typer.Argument(help="Six-digit stock code.")],
    market: Annotated[Market, typer.Option(case_sensitive=False)],
    start: Annotated[str, typer.Option(help="Inclusive YYYY-MM-DD date.")],
    end: Annotated[str, typer.Option(help="Inclusive YYYY-MM-DD date.")],
    live: Annotated[bool, typer.Option("--live")] = False,
) -> None:
    """Record secondary action hints and deterministic official-document linkage only."""

    paths, state, objects = _services()
    try:
        report = _market_reference_service(paths, state, objects).sync_corporate_actions(
            symbol,
            market,
            date.fromisoformat(start),
            date.fromisoformat(end),
            live=live,
        )
    except (AStockError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "REFERENCE_SYNC_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if report.status is ReferenceCoverageStatus.FAILED:
        raise typer.Exit(code=2)


@app.command("reference-status")
def reference_status(
    dataset_kind: Annotated[ReferenceDatasetKind, typer.Argument(case_sensitive=False)],
    scope_key: Annotated[str, typer.Argument()],
    as_of: Annotated[str | None, typer.Option(help="Aware ISO timestamp.")] = None,
) -> None:
    """Read a verified current or point-in-time reference release without networking."""

    paths, state, objects = _services()
    try:
        parsed_as_of = datetime.fromisoformat(as_of) if as_of else None
        if parsed_as_of is not None and parsed_as_of.tzinfo is None:
            raise ValueError("--as-of must include a timezone")
        _emit(
            _market_reference_service(paths, state, objects).status(
                dataset_kind, scope_key, as_of=parsed_as_of
            )
        )
    except ValueError as exc:
        _emit({"status": "FAILED", "failure_code": "INVALID_INPUT"})
        raise typer.Exit(code=2) from exc


@app.command("reference-audit")
def reference_audit() -> None:
    """Verify release objects, SQLite pointers, and immutable Parquet files."""

    paths, state, objects = _services()
    result = _market_reference_service(paths, state, objects).audit()
    _emit(result)
    if result["status"] != "PASS":
        raise typer.Exit(code=2)


@app.command("sync-financial")
def sync_financial(
    company_id: Annotated[str, typer.Argument(help="Six-digit A-share company code.")],
    period_end: Annotated[str, typer.Option(help="Financial period end YYYY-MM-DD.")],
    market: Annotated[Market, typer.Option(case_sensitive=False)] = Market.XSHG,
    period_type: Annotated[
        FinancialPeriodType, typer.Option(case_sensitive=False)
    ] = FinancialPeriodType.ANNUAL,
    as_of: Annotated[str | None, typer.Option(help="Aware ISO cutoff timestamp.")] = None,
    live: Annotated[bool, typer.Option("--live")] = False,
    cross_check: Annotated[bool, typer.Option("--cross-check")] = False,
) -> None:
    """Certify secondary financial hints against exact official PDF evidence."""

    paths, state, objects = _services()
    try:
        parsed_end = date.fromisoformat(period_end)
        parsed_as_of = datetime.fromisoformat(as_of) if as_of else None
        if parsed_as_of is not None and (
            parsed_as_of.tzinfo is None or parsed_as_of.utcoffset() is None
        ):
            raise ValueError("as_of requires timezone")
        report = _financial_source_service(paths, state, objects).sync(
            company_id,
            market,
            parsed_end,
            period_type,
            as_of=parsed_as_of,
            live=live,
            cross_check=cross_check,
        )
    except (AStockError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "FINANCIAL_SOURCE_SYNC_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if report.status is not FinancialSourceReleaseStatus.CERTIFIED:
        raise typer.Exit(code=3)


@app.command("financial-source-status")
def financial_source_status(
    company_id: Annotated[str, typer.Argument(help="Six-digit A-share company code.")],
    period_end: Annotated[str, typer.Option(help="Financial period end YYYY-MM-DD.")],
    period_type: Annotated[
        FinancialPeriodType, typer.Option(case_sensitive=False)
    ] = FinancialPeriodType.ANNUAL,
    as_of: Annotated[str | None, typer.Option(help="Aware ISO cutoff timestamp.")] = None,
) -> None:
    """Read one verified current or point-in-time financial-source release."""

    paths, state, objects = _services()
    try:
        parsed_as_of = datetime.fromisoformat(as_of) if as_of else None
        if parsed_as_of is not None and (
            parsed_as_of.tzinfo is None or parsed_as_of.utcoffset() is None
        ):
            raise ValueError("as_of requires timezone")
        result = _financial_source_service(paths, state, objects).status(
            company_id,
            date.fromisoformat(period_end),
            period_type,
            as_of=parsed_as_of,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "INVALID_FINANCIAL_SOURCE_QUERY"})
        raise typer.Exit(code=2) from exc
    _emit(result)
    if result["status"] == "CORRUPT":
        raise typer.Exit(code=2)
    if result["status"] == "NOT_AVAILABLE":
        raise typer.Exit(code=3)


@app.command("financial-source-audit")
def financial_source_audit(
    company_id: Annotated[
        str | None,
        typer.Argument(help="Optional company code; omit to audit all release chains."),
    ] = None,
    period_end: Annotated[
        str | None, typer.Option(help="Financial period end YYYY-MM-DD.")
    ] = None,
    period_type: Annotated[
        FinancialPeriodType, typer.Option(case_sensitive=False)
    ] = FinancialPeriodType.ANNUAL,
    as_of: Annotated[str | None, typer.Option(help="Aware ISO cutoff timestamp.")] = None,
    industry_profile: Annotated[
        FinancialIndustryProfile, typer.Option(case_sensitive=False)
    ] = FinancialIndustryProfile.GENERAL_INDUSTRIAL,
) -> None:
    """Audit release integrity, or run the existing evidence pack for one release."""

    paths, state, objects = _services()
    service = _financial_source_service(paths, state, objects)
    if company_id is None:
        result = service.audit()
        _emit(result)
        if result["status"] != "PASS":
            raise typer.Exit(code=2)
        return
    try:
        if period_end is None or as_of is None:
            raise ValueError("period_end and as_of are required for company audit")
        parsed_as_of = datetime.fromisoformat(as_of)
        if parsed_as_of.tzinfo is None or parsed_as_of.utcoffset() is None:
            raise ValueError("as_of requires timezone")
        pack = service.run_audit(
            company_id,
            date.fromisoformat(period_end),
            period_type,
            as_of=parsed_as_of,
            industry_profile=industry_profile,
        )
    except (AStockError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "FINANCIAL_SOURCE_AUDIT_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit({"schema_version": "financial-source-audit-pack-v1", "pack": pack})


@app.command("candidate-scan")
def candidate_scan(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Run candidate-scan-v1 from one immutable input-release reference."""

    paths, state, objects = _services()
    try:
        request = CandidateScanRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        report = _candidate_scan_service(paths, state, objects).scan(request)
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "CANDIDATE_SCAN_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if report.status is CandidateScanStatus.NEEDS_INFO:
        raise typer.Exit(code=3)


@app.command("candidate-status")
def candidate_status(
    scan_id: Annotated[str | None, typer.Option(help="Candidate scan id.")] = None,
    company_id: Annotated[str | None, typer.Option(help="Canonical company id.")] = None,
) -> None:
    """Read one scan or the latest immutable candidate version for a company."""

    paths, state, objects = _services()
    try:
        result = _candidate_scan_service(paths, state, objects).status(
            scan_id=scan_id,
            company_id=company_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "CANDIDATE_STATUS_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(result)
    if result["status"] == "NOT_FOUND":
        raise typer.Exit(code=3)


@app.command("candidate-audit")
def candidate_audit(
    scan_id: Annotated[str, typer.Argument(help="Candidate scan id.")],
) -> None:
    """Verify candidate objects, Parquet facts, registry pointers, and evidence bindings."""

    paths, state, objects = _services()
    report = _candidate_scan_service(paths, state, objects).audit(scan_id)
    _emit(report)
    if report.status is CandidateAuditStatus.FAIL:
        raise typer.Exit(code=2)


@app.command("quality-report")
def quality_report(
    symbol: Annotated[str, typer.Argument()],
    frequency: Annotated[str, typer.Option()] = "5m",
    market: Annotated[Market, typer.Option(case_sensitive=False)] = Market.XSHG,
) -> None:
    """Read the latest protected canonical manifest; never synthesizes missing data."""

    paths = ProjectPaths.discover()
    path = canonical_manifest_path(paths.manifests, market, frequency, symbol)
    if not path.is_file():
        _emit({"status": "NOT_FOUND", "manifest": str(path)})
        raise typer.Exit(code=3)
    _emit(json.loads(path.read_text(encoding="utf-8")))


@app.command("disclosure-search")
def disclosure_search(
    symbol: Annotated[str, typer.Argument(help="Six-digit A-share code.")],
    start: Annotated[str, typer.Option(help="Inclusive date in YYYY-MM-DD format.")],
    end: Annotated[str, typer.Option(help="Inclusive date in YYYY-MM-DD format.")],
    exchange: Annotated[DisclosureExchange, typer.Option(case_sensitive=False)],
    category: Annotated[
        DisclosureCategory, typer.Option(case_sensitive=False)
    ] = DisclosureCategory.ALL,
    keyword: Annotated[str, typer.Option()] = "",
    page_size: Annotated[int, typer.Option(min=1, max=100)] = 30,
) -> None:
    """Search CNINFO and immutably snapshot the official index response."""

    paths, state, objects = _services()
    del paths
    provider = CninfoDisclosureProvider(objects, state)
    try:
        result = provider.search(
            _disclosure_request(
                symbol,
                exchange,
                start,
                end,
                category,
                keyword,
                page_size,
            )
        )
    except (AStockError, ValueError) as exc:
        if isinstance(exc, AStockError):
            _emit(
                {
                    "status": "FAILED",
                    "failure_class": exc.failure_class.value,
                    "message": str(exc),
                }
            )
        else:
            _emit({"status": "FAILED", "failure_class": "INVALID_INPUT", "message": str(exc)})
        raise typer.Exit(code=2) from exc
    _emit(result)


@app.command("disclosure-sync")
def disclosure_sync(
    symbol: Annotated[str, typer.Argument(help="Six-digit A-share code.")],
    start: Annotated[str, typer.Option(help="Inclusive date in YYYY-MM-DD format.")],
    end: Annotated[str, typer.Option(help="Inclusive date in YYYY-MM-DD format.")],
    exchange: Annotated[DisclosureExchange, typer.Option(case_sensitive=False)],
    category: Annotated[
        DisclosureCategory, typer.Option(case_sensitive=False)
    ] = DisclosureCategory.ANNUAL_REPORT,
    keyword: Annotated[str, typer.Option()] = "",
    maximum_documents: Annotated[int, typer.Option(min=0, max=20)] = 1,
) -> None:
    """Search, download, snapshot, and register official CNINFO PDFs."""

    _, state, objects = _services()
    provider = CninfoDisclosureProvider(objects, state)
    service = DisclosureSyncService(provider, DocumentRepository(state), state)
    try:
        result = service.sync(
            _disclosure_request(
                symbol,
                exchange,
                start,
                end,
                category,
                keyword,
                max(maximum_documents, 1),
            ),
            maximum_documents=maximum_documents,
        )
    except (AStockError, ValueError) as exc:
        if isinstance(exc, AStockError):
            _emit(
                {
                    "status": "FAILED",
                    "failure_class": exc.failure_class.value,
                    "retryable": exc.retryable,
                    "message": str(exc),
                }
            )
        else:
            _emit({"status": "FAILED", "failure_class": "INVALID_INPUT", "message": str(exc)})
        raise typer.Exit(code=2) from exc
    _emit(result)


@app.command("pdf-parse")
def pdf_parse(
    document_id: Annotated[str, typer.Argument(help="Registered SourceDocument identifier.")],
    pages: Annotated[list[int] | None, typer.Option("--page", min=1)] = None,
    ocr: Annotated[
        bool,
        typer.Option("--ocr/--no-ocr", help="OCR only pages below the native text threshold."),
    ] = True,
    text_threshold: Annotated[int, typer.Option(min=0, max=1000)] = 24,
    ocr_dpi: Annotated[int, typer.Option(min=100, max=400)] = 200,
) -> None:
    """Parse registered PDF pages with native text first and selective OCR fallback."""

    _, state, objects = _services()
    documents = DocumentRepository(state)
    document = documents.get_model(document_id)
    snapshot = documents.latest_snapshot(document_id)
    if document is None or snapshot is None:
        _emit({"status": "NOT_FOUND", "document_id": document_id})
        raise typer.Exit(code=3)
    parser = PdfParseService(
        objects,
        state,
        DocumentPageRepository(state),
        text_threshold=text_threshold,
        ocr_dpi=ocr_dpi,
    )
    try:
        report = parser.parse(document, snapshot, page_numbers=pages, ocr_enabled=ocr)
    except (AStockError, ValueError) as exc:
        if isinstance(exc, AStockError):
            _emit(
                {
                    "status": "FAILED",
                    "failure_class": exc.failure_class.value,
                    "message": str(exc),
                    "details": exc.details,
                }
            )
        else:
            _emit({"status": "FAILED", "failure_class": "INVALID_INPUT", "message": str(exc)})
        raise typer.Exit(code=2) from exc
    _emit(report)


@app.command("private-pdf-ingest")
def private_pdf_ingest(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False)],
    source_id: Annotated[str, typer.Option(help="Stable configured private-source id.")],
    title: Annotated[str, typer.Option(help="Local research display title.")],
    author_source_id: Annotated[str, typer.Option(help="Configured author source id.")],
    file_version: Annotated[str, typer.Option(help="User-controlled immutable file version.")],
    pages: Annotated[list[int] | None, typer.Option("--page", min=1)] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Parse every page; mutually exclusive with --page."),
    ] = False,
    book: Annotated[
        bool,
        typer.Option("--book/--generic-pdf", help="Register as private book or generic PDF."),
    ] = True,
    ocr: Annotated[
        bool,
        typer.Option("--ocr/--no-ocr", help="OCR only selected low-text sample pages."),
    ] = True,
) -> None:
    """Ingest a private PDF without emitting its path, filename, headings, or text."""

    _, state, objects = _services()
    try:
        result = PrivatePdfIngestService(objects, state).ingest(
            path,
            source_id=source_id,
            display_name=title,
            author_source_id=author_source_id,
            file_version=file_version,
            document_type=(DocumentType.PRIVATE_BOOK if book else DocumentType.PRIVATE_PDF),
            sample_pages=pages,
            full_parse=full,
            ocr_enabled=ocr,
        )
    except (AStockError, ValueError) as exc:
        if isinstance(exc, AStockError):
            _emit(
                {
                    "status": "FAILED",
                    "failure_class": exc.failure_class.value,
                    "message": str(exc),
                }
            )
        else:
            _emit({"status": "FAILED", "failure_class": "INVALID_INPUT", "message": str(exc)})
        raise typer.Exit(code=2) from exc
    manifest = result.manifest
    parse = result.parse_report
    _emit(
        {
            "status": "INGESTED",
            "manifest": {
                "manifest_id": manifest.manifest_id,
                "source_id": manifest.source_id,
                "document_id": manifest.document_id,
                "snapshot_id": manifest.snapshot_id,
                "pit_id": manifest.pit_id,
                "file_sha256": manifest.file_sha256,
                "raw_object_sha256": manifest.raw_object_sha256,
                "file_name_sha256": manifest.file_name_sha256,
                "file_version": manifest.file_version,
                "byte_size": manifest.byte_size,
                "source_page_count": manifest.source_page_count,
                "rights_status": manifest.rights_status,
                "git_policy": manifest.git_policy,
                "external_republication_policy": manifest.external_republication_policy,
                "raw_retention_policy": manifest.raw_retention_policy,
                "cleaning_reconstructable": manifest.cleaning_reconstructable,
            },
            "pit_status": result.pit_metadata.point_in_time_status,
            "parse": (
                {
                    "book_parse_report_id": parse.book_parse_report_id,
                    "parse_scope": parse.parse_scope,
                    "processing_status": parse.processing_status,
                    "parser_name": parse.parser_name,
                    "parser_version": parse.parser_version,
                    "requested_pages": parse.requested_pages,
                    "processed_page_count": parse.processed_page_count,
                    "native_page_count": parse.native_page_count,
                    "ocr_page_count": parse.ocr_page_count,
                    "empty_page_count": parse.empty_page_count,
                    "failed_page_count": parse.failed_page_count,
                    "parsed_text_char_count": parse.parsed_text_char_count,
                    "pages": [
                        {
                            "page_id": page.page_id,
                            "page_number": page.page_number,
                            "extraction_method": page.extraction_method,
                            "text_char_count": page.text_char_count,
                            "text_sha256": page.text_sha256,
                            "section_depth": len(page.section_path),
                        }
                        for page in parse.pages
                    ],
                    "report_object_sha256": parse.report_object_sha256,
                }
                if parse is not None
                else None
            ),
        }
    )


@app.command("private-docx-ingest")
def private_docx_ingest(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False)],
    source_id: Annotated[str, typer.Option(help="Stable configured private-source id.")],
    title: Annotated[str, typer.Option(help="Local research display title.")],
    author_source_id: Annotated[str, typer.Option(help="Configured author source id.")],
    file_version: Annotated[str, typer.Option(help="User-controlled immutable file version.")],
) -> None:
    """Ingest and fully parse private DOCX without emitting path, headings, links, or text."""

    _, state, objects = _services()
    try:
        result = PrivateDocxIngestService(objects, state).ingest(
            path,
            source_id=source_id,
            display_name=title,
            author_source_id=author_source_id,
            file_version=file_version,
        )
    except (AStockError, ValueError) as exc:
        if isinstance(exc, AStockError):
            _emit(
                {
                    "status": "FAILED",
                    "failure_class": exc.failure_class.value,
                    "message": str(exc),
                }
            )
        else:
            _emit({"status": "FAILED", "failure_class": "INVALID_INPUT", "message": str(exc)})
        raise typer.Exit(code=2) from exc
    manifest = result.manifest
    parse = result.parse_report
    _emit(
        {
            "status": "INGESTED",
            "manifest": {
                "manifest_id": manifest.manifest_id,
                "source_id": manifest.source_id,
                "document_id": manifest.document_id,
                "snapshot_id": manifest.snapshot_id,
                "pit_id": manifest.pit_id,
                "file_sha256": manifest.file_sha256,
                "raw_object_sha256": manifest.raw_object_sha256,
                "file_name_sha256": manifest.file_name_sha256,
                "file_version": manifest.file_version,
                "byte_size": manifest.byte_size,
                "rights_status": manifest.rights_status,
                "git_policy": manifest.git_policy,
                "external_republication_policy": manifest.external_republication_policy,
                "raw_retention_policy": manifest.raw_retention_policy,
                "cleaning_reconstructable": manifest.cleaning_reconstructable,
            },
            "pit_status": result.pit_metadata.point_in_time_status,
            "parse": {
                "docx_parse_report_id": parse.docx_parse_report_id,
                "processing_status": parse.processing_status,
                "coverage_status": parse.coverage_status,
                "parser_name": parse.parser_name,
                "parser_version": parse.parser_version,
                "source_part_count": parse.source_part_count,
                "source_paragraph_count": parse.source_paragraph_count,
                "processed_block_count": parse.processed_block_count,
                "nonempty_block_count": parse.nonempty_block_count,
                "empty_block_count": parse.empty_block_count,
                "table_count": parse.table_count,
                "table_cell_count": parse.table_cell_count,
                "hyperlink_count": parse.hyperlink_count,
                "embedded_visual_count": parse.embedded_visual_count,
                "unsupported_object_count": parse.unsupported_object_count,
                "parsed_text_char_count": parse.parsed_text_char_count,
                "block_set_sha256": parse.block_set_sha256,
                "gap_types": [gap["gap_type"] for gap in parse.gaps],
                "report_object_sha256": parse.report_object_sha256,
            },
        }
    )


@app.command("financial-audit")
def financial_audit(
    input_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, help="Audit request JSON."),
    ],
    rule_config: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Versioned financial-rule registry; defaults to project config.",
        ),
    ] = None,
    industry_profiles: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Versioned industry profiles; defaults to project config.",
        ),
    ] = None,
) -> None:
    """Run an advisory-only financial audit with evidence and PIT gates."""

    paths, state, objects = _services()
    try:
        payload = json.loads(input_file.read_text(encoding="utf-8"))
        request = FinancialAuditRequest.model_validate(payload)
    except OSError as exc:
        _emit({"status": "FAILED", "failure_class": "INPUT_READ_FAILED"})
        raise typer.Exit(code=2) from exc
    except json.JSONDecodeError as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": "INVALID_JSON",
                "line": exc.lineno,
                "column": exc.colno,
            }
        )
        raise typer.Exit(code=2) from exc
    except ValidationError as exc:
        details = [
            {"location": list(error["loc"]), "type": error["type"], "message": error["msg"]}
            for error in exc.errors(include_input=False, include_url=False)
        ]
        _emit({"status": "FAILED", "failure_class": "INVALID_INPUT", "errors": details})
        raise typer.Exit(code=2) from exc
    try:
        service = FinancialIntegrityService(
            state,
            objects,
            rule_config_path=rule_config or paths.root / "configs" / "financial_rules.yaml",
            industry_profile_path=(
                industry_profiles or paths.root / "configs" / "financial_industry_profiles.yaml"
            ),
        )
        execution = service.run(request)
    except (AStockError, ValueError) as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": (
                    exc.failure_class.value if isinstance(exc, AStockError) else "INVALID_INPUT"
                ),
                "message": str(exc),
            }
        )
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": execution.pack.status,
            "artifact_hash": execution.artifact_hash,
            "reused_existing": execution.reused_existing,
            "pack": execution.pack,
        }
    )


@app.command("financial-audit-schema")
def financial_audit_schema() -> None:
    """Print the strict JSON Schema accepted by the financial-audit command."""

    _emit(FinancialAuditRequest.model_json_schema())


@app.command("financial-audit-status")
def financial_audit_status(
    audit_run_id: Annotated[str, typer.Argument(help="Deterministic financial audit run id.")],
) -> None:
    """Read one persisted financial-audit checkpoint and artifact summary."""

    _, state, objects = _services()
    repository = FinancialIntegrityRepository(state, objects)
    record = repository.get_run(audit_run_id)
    if record is None:
        _emit({"status": "NOT_FOUND", "audit_run_id": audit_run_id})
        raise typer.Exit(code=3)
    pack = repository.get_pack(audit_run_id)
    _emit(
        {
            "audit_run_id": record.audit_run_id,
            "status": record.status,
            "checkpoint_step": record.checkpoint_step,
            "artifact_hash": record.report_object_hash,
            "attempt_count": repository.attempt_count(audit_run_id),
            "coverage_status": pack.coverage_status if pack else None,
            "risk_level": pack.risk_level if pack else None,
            "evidence_gap_count": len(pack.evidence_gaps) if pack else None,
            "manual_task_count": len(pack.manual_tasks) if pack else None,
        }
    )


@app.command("paper-status")
def paper_status(
    account_id: Annotated[str, typer.Option()] = "default",
) -> None:
    """Recover and report paper cash, frozen cash, positions, orders, and journal integrity."""

    _, state, _ = _services()
    service = LedgerService(state)
    status = service.status(account_id)
    status["nav"] = service.portfolio_nav(account_id)
    _emit(status)


def _run_confirmed_paper_operation(
    *,
    request_path: Path,
    confirmation_path: Path | None,
    expected_operation_type: str,
    fee_rules: Path | None,
) -> None:
    paths, state, objects = _services()
    try:
        request = load_paper_operation(request_path)
        confirmation = (
            load_paper_confirmation(confirmation_path) if confirmation_path else None
        )
        report = _paper_operation_service(paths, state, objects, fee_rules).execute(
            request,
            confirmation,
            expected_operation_type=expected_operation_type,
        )
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        emitted_status = (
            "NEEDS_INFO"
            if isinstance(exc, AStockError)
            and exc.failure_class is FailureClass.DATA_QUALITY
            else "REJECTED"
        )
        _emit(
            {
                "status": emitted_status,
                "failure_class": (
                    exc.failure_class.value if isinstance(exc, AStockError) else "INVALID_INPUT"
                ),
                "message": str(exc),
                "ledger_write_allowed": False,
            }
        )
        raise typer.Exit(code=3) from exc
    _emit({"status": "COMPLETE", "report": report})


@app.command("paper-order-place")
def paper_order_place(
    request: Annotated[Path, typer.Argument(help="Immutable PLACE_ORDER request JSON.")],
    confirmation: Annotated[
        Path | None,
        typer.Option(help="Independent MANUAL_CLI confirmation JSON."),
    ] = None,
    fee_rules: Annotated[
        Path | None,
        typer.Option(help="Effective-dated paper fee schedule."),
    ] = None,
) -> None:
    """Place a simulated order only from an exact, unexpired user confirmation."""

    _run_confirmed_paper_operation(
        request_path=request,
        confirmation_path=confirmation,
        expected_operation_type="PLACE_ORDER",
        fee_rules=fee_rules,
    )


@app.command("paper-order-cancel")
def paper_order_cancel(
    request: Annotated[Path, typer.Argument(help="Immutable CANCEL_ORDER request JSON.")],
    confirmation: Annotated[Path | None, typer.Option()] = None,
    fee_rules: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Cancel one simulated order through the confirmed operation boundary."""

    _run_confirmed_paper_operation(
        request_path=request,
        confirmation_path=confirmation,
        expected_operation_type="CANCEL_ORDER",
        fee_rules=fee_rules,
    )


@app.command("paper-settle")
def paper_settle(
    request: Annotated[Path, typer.Argument(help="Immutable SETTLE request JSON.")],
    confirmation: Annotated[Path | None, typer.Option()] = None,
    fee_rules: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Settle T+1 lots against an exact verified-calendar release."""

    _run_confirmed_paper_operation(
        request_path=request,
        confirmation_path=confirmation,
        expected_operation_type="SETTLE",
        fee_rules=fee_rules,
    )


@app.command("paper-mark")
def paper_mark(
    request: Annotated[Path, typer.Argument(help="Immutable MARK request JSON.")],
    confirmation: Annotated[Path | None, typer.Option()] = None,
    fee_rules: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Persist a read-only NAV mark from unadjusted reference releases."""

    _run_confirmed_paper_operation(
        request_path=request,
        confirmation_path=confirmation,
        expected_operation_type="MARK",
        fee_rules=fee_rules,
    )


@app.command("paper-recover")
def paper_recover(
    request: Annotated[Path, typer.Argument(help="Immutable RECOVER request JSON.")],
    confirmation: Annotated[Path | None, typer.Option()] = None,
    fee_rules: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Audit and recover only deterministic paper-account invariants."""

    _run_confirmed_paper_operation(
        request_path=request,
        confirmation_path=confirmation,
        expected_operation_type="RECOVER",
        fee_rules=fee_rules,
    )


@app.command("paper-replay")
def paper_replay(
    symbol: Annotated[str, typer.Argument()],
    cursor: Annotated[str, typer.Option(help="Last verified 5m bar timestamp in ISO format.")],
    account_id: Annotated[str, typer.Option()] = "default",
    market: Annotated[Market, typer.Option(case_sensitive=False)] = Market.XSHG,
    fee_rules: Annotated[
        Path | None,
        typer.Option(help="Effective-dated YAML fee profile; defaults to configs/fee_rules.yaml."),
    ] = None,
    maximum_participation_rate: Annotated[
        str,
        typer.Option(help="Maximum share of a 5m bar volume usable by all paper fills."),
    ] = "0.10",
) -> None:
    """Match open paper limit orders on canonical 5m bars and advance the checkpoint."""

    paths, state, objects = _services()
    parsed_cursor = _parse_local_datetime(cursor)
    profile_path = fee_rules or paths.root / "configs" / "fee_rules.yaml"
    schedule = load_fee_schedule(profile_path)
    store = CanonicalMarketStore(paths.parquet, paths.manifests)
    service = PaperReplayService(
        LedgerService(state, objects),
        store,
        MarketReferencePaperVerifier(_market_reference_service(paths, state, objects)),
    )
    try:
        report = service.replay(
            account_id=account_id,
            request=_request(symbol, market, None, cursor),
            requested_cursor=parsed_cursor,
            fee_schedule=schedule,
            maximum_participation_rate=Decimal(maximum_participation_rate),
        )
    except AStockError as exc:
        _emit(
            {
                "status": "UNREPLAYABLE",
                "failure_class": exc.failure_class.value,
                "message": str(exc),
                "details": exc.details,
            }
        )
        raise typer.Exit(code=3) from exc
    _emit({"status": "REPLAYED", "report": report})


@app.command("context-plan")
def context_plan(
    skills: Annotated[list[str] | None, typer.Option("--skill")] = None,
    artifacts: Annotated[list[Path] | None, typer.Option("--artifact")] = None,
    artifact_ids: Annotated[list[str] | None, typer.Option("--artifact-id")] = None,
) -> None:
    """Estimate local input size and duplicate reads before a Codex research run."""

    if artifact_ids:
        paths, state, objects = _services()
        service = CodexRunService(paths.runtime, objects, state)
        try:
            report, _ = _context_budget_with_registered(
                service,
                skills=skills or [],
                artifact_paths=artifacts or [],
                artifact_ids=artifact_ids,
            )
        except ValueError as exc:
            _emit({"status": "REJECTED", "error_code": "INVALID_CODEX_INPUT"})
            raise typer.Exit(code=2) from exc
    else:
        report = build_context_budget(skills=skills or [], artifact_paths=artifacts or [])
    _emit(report)


@app.command("research-request")
def research_request(
    company_or_name: Annotated[
        str,
        typer.Argument(help="Stock code (6 digits) or company name."),
    ],
    requested_modules: Annotated[
        list[str] | None,
        typer.Option("--module", help="Requested research module(s): financial|evidence|research"),
    ] = None,
) -> None:
    """Build one deterministic research intake request artifact."""

    paths, state, objects = _services()
    try:
        execution = ResearchRequestService(
            state,
            objects,
            paths.parquet,
        ).create_request(company_or_name, requested_modules=requested_modules)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RESEARCH_REQUEST"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": "CREATED",
            "artifact_id": execution.artifact_id,
            "artifact_hash": execution.object_sha256,
            "request": execution.request,
            "reused_existing": execution.reused_existing,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )


@app.command("research-evidence-task")
def research_evidence_task(
    request_artifact_id: Annotated[
        str,
        typer.Argument(help="ResearchRequest artifact id."),
    ],
) -> None:
    """Build one deterministic evidence-collection task from an existing request."""

    paths, state, objects = _services()
    try:
        execution = EvidenceCollectionTaskService(state, objects).create_task(
            request_artifact_id
        )
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RESEARCH_TASK_REQUEST"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": "CREATED",
            "artifact_id": execution.artifact_id,
            "artifact_hash": execution.object_sha256,
            "task": execution.task,
            "reused_existing": execution.reused_existing,
        }
    )


@app.command("research-evidence-run")
def research_evidence_run(
    task_artifact_id: Annotated[
        str,
        typer.Argument(help="EvidenceCollectionTask artifact id."),
    ],
) -> None:
    """Build one deterministic evidence-collection run from an existing task."""

    paths, state, objects = _services()
    try:
        execution = EvidenceCollectionRunService(state, objects).create_run(task_artifact_id)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RESEARCH_RUN_REQUEST"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": "CREATED",
            "artifact_id": execution.artifact_id,
            "artifact_hash": execution.object_sha256,
            "run": execution.run,
            "reused_existing": execution.reused_existing,
        }
    )


@app.command("research-evidence-freeze")
def research_evidence_freeze(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Freeze one company's point-in-time Claim--Evidence scope."""

    paths, state, objects = _services()
    try:
        request = EvidenceFreezeRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_FREEZE_REQUEST"})
        raise typer.Exit(code=2) from exc
    execution = ResearchCoreService(state, objects, _research_core(paths)).freeze_evidence(request)
    pack = execution.pack
    _emit(
        {
            "status": "FROZEN",
            "pack_id": pack.pack_id,
            "object_sha256": execution.object_sha256,
            "company_id": pack.company_id,
            "as_of": pack.as_of,
            "claim_count": len(pack.claim_ids),
            "evidence_count": len(pack.evidence_ids),
            "open_conflict_count": len(pack.open_conflict_ids),
            "coverage_status": pack.coverage_status,
            "degradation_codes": pack.degradation_codes,
        }
    )


@app.command("research-base-case-build")
def research_base_case_build(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Validate and store one common BaseCase over frozen evidence."""

    paths, state, objects = _services()
    try:
        request = BaseCaseBuildRequest.model_validate_json(request_file.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_BASE_CASE_REQUEST"})
        raise typer.Exit(code=2) from exc
    execution = ResearchCoreService(state, objects, _research_core(paths)).build_base_case(request)
    pack = execution.pack
    _emit(
        {
            "status": "BUILT",
            "base_case_id": pack.base_case_id,
            "evidence_pack_id": pack.evidence_pack_id,
            "object_sha256": execution.object_sha256,
            "company_id": pack.company_id,
            "as_of": pack.as_of,
            "finding_count": sum(len(items) for items in pack.findings_by_section.values()),
            "evidence_count": len(pack.evidence_ids),
            "gap_count": len(pack.evidence_gaps),
            "coverage_status": pack.coverage_status,
            "base_confidence": pack.base_confidence,
            "confidence_cap": pack.confidence_cap,
            "degradation_codes": pack.degradation_codes,
        }
    )


@app.command("research-base-case-status")
def research_base_case_status(
    company_id: Annotated[str, typer.Argument(help="Canonical company id.")],
) -> None:
    """Return the latest safe BaseCase index without research prose."""

    _, state, objects = _services()
    summary = ResearchRepository(state, objects).latest_base_case_summary(company_id)
    _emit(
        {"status": "NOT_RUN", "base_case": None}
        if summary is None
        else {"status": summary["coverage_status"], "base_case": summary}
    )


@app.command("research-base-case-audit")
def research_base_case_audit(
    company_id: Annotated[str, typer.Argument(help="Canonical company id.")],
) -> None:
    """Audit BaseCase objects, frozen scope, citations, and index counts."""

    paths, state, objects = _services()
    _emit(ResearchCoreService(state, objects, _research_core(paths)).audit(company_id))


@app.command("open-source-audit-status")
def open_source_audit_status() -> None:
    """Validate fixed Serenity source, license, file and local mapping manifests."""

    paths, _, _ = _services()
    registry = _research_skills(paths)
    manifests = validate_registry_open_source_audits(registry, paths.root)
    _emit(
        {
            "status": "PASS",
            "registry_version": registry.registry_version,
            "audits": [
                {
                    "audit_id": manifest.audit_id,
                    "upstream_repository": manifest.upstream_repository,
                    "commit_sha": manifest.commit_sha,
                    "license_id": manifest.license_id,
                    "license_sha256": manifest.license_sha256,
                    "reviewed_file_count": len(manifest.reviewed_files),
                    "local_contracts": [
                        mapping.local_contract_id for mapping in manifest.local_mappings
                    ],
                    "local_patch_sha256": manifest.local_patch_sha256,
                    "local_adaptation_sha256": manifest.local_adaptation_sha256,
                    "local_adaptation_file_count": len(
                        manifest.local_adaptation_files
                    ),
                    "normal_runtime_network_required": (
                        manifest.normal_runtime_network_required
                    ),
                    "source_vendored": manifest.source_vendored,
                }
                for manifest in manifests
            ],
        }
    )


@app.command("open-source-audit-verify")
def open_source_audit_verify(
    audit_id: Annotated[str, typer.Argument(help="Frozen open-source audit id.")],
    source_root: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ],
) -> None:
    """Recompute one audit against a local checkout of its exact commit."""

    paths, _, _ = _services()
    manifests = validate_registry_open_source_audits(_research_skills(paths), paths.root)
    manifest = next((item for item in manifests if item.audit_id == audit_id), None)
    if manifest is None:
        _emit({"status": "REJECTED", "reason_code": "UNKNOWN_OPEN_SOURCE_AUDIT"})
        raise typer.Exit(code=2)
    report = verify_open_source_tree(manifest, source_root)
    _emit(report)
    if report["status"] != "PASS":
        raise typer.Exit(code=2)


@app.command("research-specialist-list")
def research_specialist_list() -> None:
    """Register and list versioned research Skill contracts."""

    paths, state, objects = _services()
    execution = ResearchSkillService(
        state,
        objects,
        _research_skills(paths),
    ).register_registry()
    registry = execution.registry
    _emit(
        {
            "status": "REGISTERED",
            "registry_version": registry.registry_version,
            "object_sha256": execution.object_sha256,
            "max_specialists": registry.max_specialists,
            "skills": [
                {
                    "skill_id": item.skill_id,
                    "skill_version": item.skill_version,
                    "kind": item.kind,
                    "status": item.status,
                    "counts_as_specialist": item.counts_as_specialist,
                    "required_inputs": item.required_inputs,
                    "required_frequencies": item.required_frequencies,
                }
                for item in registry.skills
            ],
        }
    )


@app.command("research-specialist-route")
def research_specialist_route(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Select at most three specialists using explicit deterministic rules."""

    paths, state, objects = _services()
    try:
        request = SpecialistRouteRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        execution = ResearchSkillService(
            state,
            objects,
            _research_skills(paths),
        ).route(request)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_SPECIALIST_ROUTE"})
        raise typer.Exit(code=2) from exc
    plan = execution.plan
    _emit(
        {
            "status": "ROUTED",
            "route_plan_id": plan.route_plan_id,
            "base_case_id": plan.base_case_id,
            "object_sha256": execution.object_sha256,
            "coverage_status": plan.coverage_status,
            "confidence_cap": plan.confidence_cap,
            "selected": [
                {
                    "skill_id": item.skill_id,
                    "skill_version": item.skill_version,
                    "eligibility": item.eligibility,
                    "reason_codes": item.reason_codes,
                    "degradation_codes": item.degradation_codes,
                }
                for item in plan.selected
            ],
            "unavailable": [
                {
                    "skill_id": item.skill_id,
                    "skill_version": item.skill_version,
                    "reason_codes": item.reason_codes,
                    "missing_required_inputs": item.missing_required_inputs,
                    "missing_required_frequencies": item.missing_required_frequencies,
                }
                for item in plan.unavailable
            ],
            "excluded_skill_reasons": plan.excluded_skill_reasons,
            "degradation_codes": plan.degradation_codes,
        }
    )


@app.command("research-delta-import")
def research_delta_import(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Validate and store one selected specialist's incremental research output."""

    paths, state, objects = _services()
    try:
        request = SpecialistDeltaBuildRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        execution = ResearchSkillService(
            state,
            objects,
            _research_skills(paths),
        ).build_delta(request)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_SPECIALIST_DELTA"})
        raise typer.Exit(code=2) from exc
    delta = execution.delta
    _emit(
        {
            "status": "IMPORTED",
            "delta_id": delta.delta_id,
            "base_case_id": delta.base_case_id,
            "route_plan_id": delta.route_plan_id,
            "skill_id": delta.skill_id,
            "skill_version": delta.skill_version,
            "object_sha256": execution.object_sha256,
            "incremental_finding_count": len(delta.incremental_findings),
            "correction_count": len(delta.base_case_corrections),
            "metric_count": len(delta.industry_specific_metrics),
            "evidence_request_count": len(delta.additional_evidence_requests),
            "evidence_count": len(delta.evidence_ids),
            "confidence_delta": delta.confidence_delta,
        }
    )


@app.command("research-specialist-status")
def research_specialist_status(
    base_case_id: Annotated[str, typer.Argument(help="Frozen BaseCase id.")],
) -> None:
    """Return safe route and delta indexes without research prose."""

    paths, state, objects = _services()
    _emit(
        ResearchSkillService(
            state,
            objects,
            _research_skills(paths),
        ).status(base_case_id)
    )


@app.command("research-specialist-audit")
def research_specialist_audit(
    base_case_id: Annotated[str, typer.Argument(help="Frozen BaseCase id.")],
) -> None:
    """Audit route, delta, frozen evidence scope, and safe index counts."""

    paths, state, objects = _services()
    _emit(
        ResearchSkillService(
            state,
            objects,
            _research_skills(paths),
        ).audit(base_case_id)
    )


@app.command("research-diagnostic-schema")
def research_diagnostic_schema() -> None:
    """List deterministic diagnostic contracts and their fixed rule version."""

    paths, _, _ = _services()
    config = _research_diagnostics(paths)
    registry = _research_skills(paths)
    _emit(
        {
            "diagnostics_version": config.diagnostics_version,
            "diagnostics": [
                {
                    "skill_id": item.skill_id,
                    "skill_version": item.skill_version,
                    "input_schema": (
                        {
                            "IndustryBottleneckSkill": (
                                "IndustryBottleneckDiagnosticRequestV2"
                            ),
                            "EventToAlphaSkill": "EventToAlphaDiagnosticRequestV2",
                            "GrowthProbabilitySkill": (
                                "GrowthProbabilityDiagnosticRequestV2"
                            ),
                            "GrowthValuationLens": (
                                "GrowthValuationDiagnosticRequestV2"
                            ),
                            "DailyTrendHealthSkill": "DailyTrendDiagnosticRequestV2",
                        }[item.skill_id]
                        if item.skill_version.endswith("-v2")
                        else {
                        "IndustryBottleneckSkill": "IndustryBottleneckDiagnosticRequest",
                        "EventToAlphaSkill": "EventToAlphaDiagnosticRequest",
                        "GrowthProbabilitySkill": "GrowthProbabilityDiagnosticRequest",
                        "GrowthValuationLens": "GrowthValuationDiagnosticRequest",
                        "DailyTrendHealthSkill": "DailyTrendDiagnosticRequest",
                        "HourlySwingSkill": "HourlySwingDiagnosticRequest",
                        }[item.skill_id]
                    ),
                }
                for item in registry.skills
                if item.counts_as_specialist
            ],
            "memo": {
                "skill_id": "ResearchMemoComposer",
                "skill_version": next(
                    item.skill_version
                    for item in registry.skills
                    if item.skill_id == "ResearchMemoComposer"
                ),
                "input_schema": (
                    "ResearchMemoComposeRequestV2"
                    if next(
                        item.skill_version
                        for item in registry.skills
                        if item.skill_id == "ResearchMemoComposer"
                    ).endswith("-v2")
                    else "ResearchMemoComposeRequest"
                ),
            },
        }
    )


@app.command("research-specialist-diagnose")
def research_specialist_diagnose(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Run one selected deterministic diagnostic and store its cited Delta."""

    paths, state, objects = _services()
    try:
        raw_request = request_file.read_text(encoding="utf-8")
        raw_payload = json.loads(raw_request)
        adapter = (
            TypeAdapter(SpecialistDiagnosticRequestV2)
            if isinstance(raw_payload, dict)
            and str(raw_payload.get("skill_version", "")).endswith("-v2")
            else TypeAdapter(SpecialistDiagnosticRequest)
        )
        request = adapter.validate_json(raw_request)
        execution = ResearchDiagnosticsService(
            state,
            objects,
            _research_skills(paths),
            _research_diagnostics(paths),
        ).diagnose(request)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RESEARCH_DIAGNOSTIC"})
        raise typer.Exit(code=2) from exc
    report = execution.report
    delta = execution.delta
    _emit(
        {
            "status": report.status,
            "diagnostic_id": report.diagnostic_id,
            "delta_id": delta.delta_id,
            "base_case_id": report.base_case_id,
            "route_plan_id": report.route_plan_id,
            "skill_id": report.skill_id,
            "skill_version": report.skill_version,
            "diagnostics_version": report.diagnostics_version,
            "object_sha256": execution.object_sha256,
            "delta_object_sha256": execution.delta_object_sha256,
            "signal_codes": report.signal_codes,
            "degradation_codes": report.degradation_codes,
            "metric_count": len(report.metric_names),
            "evidence_request_count": len(report.evidence_request_codes),
            "evidence_count": len(report.evidence_ids),
        }
    )


@app.command("research-memo-compose")
def research_memo_compose(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Compose a citation-preserving reference memo without new research facts."""

    paths, state, objects = _services()
    try:
        raw_request = request_file.read_text(encoding="utf-8")
        raw_payload = json.loads(raw_request)
        request = (
            ResearchMemoComposeRequestV2.model_validate_json(raw_request)
            if isinstance(raw_payload, dict) and "structured_memo" in raw_payload
            else ResearchMemoComposeRequest.model_validate_json(raw_request)
        )
        execution = ResearchDiagnosticsService(
            state,
            objects,
            _research_skills(paths),
            _research_diagnostics(paths),
        ).compose_memo(request)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RESEARCH_MEMO"})
        raise typer.Exit(code=2) from exc
    memo = execution.memo
    _emit(
        {
            "status": "COMPOSED",
            "memo_id": memo.memo_id,
            "base_case_id": memo.base_case_id,
            "route_plan_id": memo.route_plan_id,
            "object_sha256": execution.object_sha256,
            "coverage_status": memo.coverage_status,
            "confidence_cap": memo.confidence_cap,
            "delta_count": len(memo.delta_references),
            "missing_selected_skill_ids": memo.missing_selected_skill_ids,
            "open_gap_count": len(memo.open_gap_codes),
            "evidence_count": len(memo.evidence_ids),
            "degradation_codes": memo.degradation_codes,
        }
    )


@app.command("research-diagnostic-status")
def research_diagnostic_status(
    base_case_id: Annotated[str, typer.Argument(help="Frozen BaseCase id.")],
) -> None:
    """Return safe diagnostic and memo indexes without research prose."""

    paths, state, objects = _services()
    _emit(
        ResearchDiagnosticsService(
            state,
            objects,
            _research_skills(paths),
            _research_diagnostics(paths),
        ).status(base_case_id)
    )


@app.command("research-diagnostic-audit")
def research_diagnostic_audit(
    base_case_id: Annotated[str, typer.Argument(help="Frozen BaseCase id.")],
) -> None:
    """Audit diagnostic objects, Delta links, evidence scope, and memo references."""

    paths, state, objects = _services()
    _emit(
        ResearchDiagnosticsService(
            state,
            objects,
            _research_skills(paths),
            _research_diagnostics(paths),
        ).audit(base_case_id)
    )


@app.command("position-lifecycle-schema")
def position_lifecycle_schema() -> None:
    """List the frozen lifecycle rules and manual-confirmation safety gates."""

    paths, state, objects = _services()
    service = PositionLifecycleService(state, objects, _position_lifecycle(paths))
    rules, object_hash = service.register_rules()
    _emit(
        {
            "rules_version": rules.rules_version,
            "object_sha256": object_hash,
            "action_priority": rules.action_priority,
            "base_action_confidence": rules.base_action_confidence,
            "coverage_confidence_caps": rules.coverage_confidence_caps,
            "requires_user_confirmation": rules.requires_user_confirmation,
            "add_requires_new_evidence": rules.add_requires_new_evidence,
            "hard_block_codes": [
                rules.conflict_hard_block_code,
                rules.invalidated_evidence_hard_block_code,
                rules.add_support_missing_code,
            ],
        }
    )


@app.command("position-plan-create")
def position_plan_create(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Create a cited monitoring plan without exposing its private thesis text."""

    paths, state, objects = _services()
    try:
        request = PositionPlanCreateRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        execution = PositionLifecycleService(
            state,
            objects,
            _position_lifecycle(paths),
        ).create_plan(request)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_POSITION_PLAN"})
        raise typer.Exit(code=2) from exc
    plan = execution.plan
    _emit(
        {
            "status": "MONITORED",
            "plan_id": plan.plan_id,
            "position_id": plan.position_id,
            "company_id": plan.company_id,
            "base_case_id": plan.base_case_id,
            "route_plan_id": plan.route_plan_id,
            "memo_id": plan.memo_id,
            "rules_version": plan.rules_version,
            "as_of": plan.as_of,
            "next_review_at": plan.next_review_at,
            "condition_count": len([*plan.price_rules, *plan.fundamental_rules, *plan.event_rules]),
            "baseline_evidence_count": len(plan.baseline_evidence_ids),
            "coverage_status": plan.coverage_status,
            "object_sha256": execution.object_sha256,
        }
    )


@app.command("position-plan-status")
def position_plan_status(
    position_id: Annotated[str, typer.Argument(help="Monitored position id.")],
) -> None:
    """Return safe monitoring and latest-review indexes without thesis prose."""

    paths, state, objects = _services()
    _emit(
        PositionLifecycleService(
            state,
            objects,
            _position_lifecycle(paths),
        ).status(position_id)
    )


@app.command("holding-review-run")
def holding_review_run(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Generate an incremental action proposal that always needs user confirmation."""

    paths, state, objects = _services()
    try:
        request = HoldingReviewRequest.model_validate_json(request_file.read_text(encoding="utf-8"))
        execution = PositionLifecycleService(
            state,
            objects,
            _position_lifecycle(paths),
        ).review(request)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_HOLDING_REVIEW"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": "PROPOSED",
            "update_id": execution.update.update_id,
            "review_id": execution.review.review_id,
            "proposal_id": execution.proposal.proposal_id,
            "position_id": execution.proposal.position_id,
            "action": execution.proposal.action,
            "action_confidence": execution.review.action_confidence,
            "requires_user_confirmation": execution.proposal.requires_user_confirmation,
            "triggered_rules": execution.review.triggered_rules,
            "hard_blocks": execution.review.hard_blocks,
            "degradation_codes": execution.review.degradation_codes,
            "evidence_count": len(execution.review.evidence_ids),
            "object_sha256": execution.review_object_sha256,
        }
    )


@app.command("holding-review-status")
def holding_review_status(
    position_id: Annotated[str, typer.Argument(help="Monitored position id.")],
) -> None:
    """Return the latest safe lifecycle checkpoint for a position."""

    paths, state, objects = _services()
    _emit(
        PositionLifecycleService(
            state,
            objects,
            _position_lifecycle(paths),
        ).status(position_id)
    )


@app.command("holding-review-audit")
def holding_review_audit(
    position_id: Annotated[str, typer.Argument(help="Monitored position id.")],
) -> None:
    """Audit lineage, continuous windows, artifacts, and confirmation gates."""

    paths, state, objects = _services()
    _emit(
        PositionLifecycleService(
            state,
            objects,
            _position_lifecycle(paths),
        ).audit(position_id)
    )


@app.command("research-chain-status")
def research_chain_status(
    company_id: Annotated[str, typer.Argument(help="Company research entity id.")],
    position_id: Annotated[
        str | None,
        typer.Option(help="Optional monitored position id for lifecycle status."),
    ] = None,
) -> None:
    """Return safe status across the complete implemented Phase 4 chain."""

    paths, state, objects = _services()
    _emit(_phase4_chain(paths, state, objects).status(company_id, position_id=position_id))


@app.command("research-chain-audit")
def research_chain_audit(
    company_id: Annotated[str, typer.Argument(help="Company research entity id.")],
    position_id: Annotated[
        str | None,
        typer.Option(help="Optional monitored position id for lifecycle audit."),
    ] = None,
) -> None:
    """Audit existing Phase 4 artifacts without rerunning research or using network."""

    paths, state, objects = _services()
    _emit(_phase4_chain(paths, state, objects).audit(company_id, position_id=position_id))


@app.command("committee-schema")
def committee_schema() -> None:
    """Print the frozen-input committee request contract and active rule version."""

    paths, state, objects = _services()
    service = _committee_service(paths, state, objects)
    _emit(
        {
            "request_schema": CommitteeDecisionRequest.model_json_schema(),
            "rules": service.configured_rules,
            "supported_input_types": service.supported_input_types(),
            "external_access": {
                "network": False,
                "api": False,
                "mcp": False,
                "browser": False,
                "full_document": False,
                "new_research": False,
            },
        }
    )


@app.command("committee-input-resolve")
def committee_input_resolve(
    artifact_ids: Annotated[list[str] | None, typer.Option("--artifact-id")] = None,
) -> None:
    """Resolve registered artifact ids into exact committee references and policy hashes."""

    if not artifact_ids:
        _emit({"status": "REJECTED", "error_code": "ARTIFACT_IDS_REQUIRED"})
        raise typer.Exit(code=2)
    paths, state, objects = _services()
    service = _committee_service(paths, state, objects)
    try:
        references = sorted(
            (service.resolve_reference(item) for item in dict.fromkeys(artifact_ids)),
            key=lambda item: item.artifact_id,
        )
        policy = CommitteeAccessPolicy(
            frozen_artifact_hashes=sorted(item.object_sha256 for item in references)
        )
    except (AStockError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_COMMITTEE_INPUT"})
        raise typer.Exit(code=2) from exc
    _emit({"artifact_references": references, "access_policy": policy})


@app.command("committee-plan")
def committee_plan(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Validate and preview a committee verdict without durable committee writes."""

    paths, state, objects = _services()
    service = _committee_service(paths, state, objects)
    try:
        request = CommitteeDecisionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        _emit(service.plan(request))
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_COMMITTEE_REQUEST"})
        raise typer.Exit(code=2) from exc


@app.command("committee-decide")
def committee_decide(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Create an immutable DecisionPack, TradeProtocol, and any NEEDS_INFO tasks."""

    paths, state, objects = _services()
    service = _committee_service(paths, state, objects)
    try:
        request = CommitteeDecisionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        execution = service.decide(request)
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "COMMITTEE_DECISION_REJECTED"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": "DECIDED",
            "decision": execution.decision,
            "protocol": execution.protocol,
            "counter_case": execution.counter_case,
            "investigation_tasks": execution.investigation_tasks,
            "object_sha256_by_type": execution.object_sha256_by_type,
        }
    )


@app.command("committee-status")
def committee_status(
    decision_id: Annotated[str | None, typer.Option("--decision-id")] = None,
    company_id: Annotated[str | None, typer.Option("--company-id")] = None,
) -> None:
    """Return safe committee, protocol, and investigation-task metadata."""

    paths, state, objects = _services()
    try:
        _emit(
            _committee_service(paths, state, objects).status(
                decision_id=decision_id,
                company_id=company_id,
            )
        )
    except ValueError as exc:
        _emit({"status": "REJECTED", "error_code": "COMMITTEE_ID_REQUIRED"})
        raise typer.Exit(code=2) from exc


@app.command("committee-audit")
def committee_audit(
    decision_id: Annotated[str, typer.Argument(help="Committee decision id.")],
) -> None:
    """Recompute and audit one decision without networking or new research."""

    paths, state, objects = _services()
    _emit(_committee_service(paths, state, objects).audit(decision_id))


@app.command("committee-recover")
def committee_recover(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Idempotently finish an interrupted deterministic committee write."""

    paths, state, objects = _services()
    service = _committee_service(paths, state, objects)
    try:
        request = CommitteeDecisionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        _emit(service.recover(request))
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "COMMITTEE_NOT_RECOVERABLE"})
        raise typer.Exit(code=2) from exc


@app.command("committee-task-status")
def committee_task_status(
    task_id: Annotated[str, typer.Argument(help="Committee investigation task id.")],
) -> None:
    """Return safe status for one NEEDS_INFO investigation task."""

    paths, state, objects = _services()
    _emit(_committee_service(paths, state, objects).task_status(task_id))


@app.command("committee-task-resolve")
def committee_task_resolve(
    task_id: Annotated[str, typer.Argument(help="Open committee task id.")],
    resolution_artifact_id: Annotated[
        str,
        typer.Argument(help="New registered frozen artifact that addresses the gap."),
    ],
) -> None:
    """Link a new frozen artifact to a task; this never reruns the committee itself."""

    paths, state, objects = _services()
    try:
        _emit(
            _committee_service(paths, state, objects).resolve_task(
                task_id,
                resolution_artifact_id,
            )
        )
    except ValueError as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_TASK_RESOLUTION"})
        raise typer.Exit(code=2) from exc


@app.command("shadow-schema")
def shadow_schema() -> None:
    """Print the frozen shadow-evaluation contracts and empirical gates."""

    paths, state, objects = _services()
    service = _shadow_service(paths, state, objects)
    _emit(
        {
            "study_request_schema": ShadowStudyCreateRequest.model_json_schema(),
            "assignment_schema": ShadowDecisionAssignmentRequest.model_json_schema(),
            "market_regime_features_schema": MarketRegimeFeatures.model_json_schema(),
            "observation_schema": ShadowExecutionObservationDraft.model_json_schema(),
            "policy": service.configured_policy,
            "hard_boundaries": {
                "weights_frozen": True,
                "future_inputs_allowed": False,
                "not_pit_safe_formal_samples_allowed": False,
                "online_weight_changes_allowed": False,
                "broker_execution_allowed": False,
                "main_paper_ledger_write_allowed": False,
                "independence_key_is_deterministic": True,
            },
        }
    )


@app.command("shadow-independence-key")
def shadow_independence_key(
    study_id: Annotated[str, typer.Argument(help="Frozen shadow study id.")],
    company_id: Annotated[str, typer.Argument(help="Frozen company id.")],
    thesis_version: Annotated[str, typer.Argument(help="Frozen thesis version.")],
    event_id: Annotated[str, typer.Argument(help="Frozen official event id.")],
) -> None:
    """Compute the only accepted independence key for one frozen episode."""

    paths, state, objects = _services()
    try:
        service = _shadow_service(paths, state, objects)
        _emit(
            {
                "status": "FROZEN",
                "study_id": study_id,
                "independence_rule_version": (service.configured_policy.independence_rule_version),
                "independence_key": service.build_independence_key(
                    study_id,
                    company_id=company_id,
                    thesis_version=thesis_version,
                    event_id=event_id,
                ),
            }
        )
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "SHADOW_STUDY_NOT_AVAILABLE"})
        raise typer.Exit(code=2) from exc


@app.command("shadow-study-plan")
def shadow_study_plan(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Validate a frozen study definition without durable shadow writes."""

    paths, state, objects = _services()
    try:
        request = ShadowStudyCreateRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        _emit(_shadow_service(paths, state, objects).plan_study(request))
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_SHADOW_STUDY"})
        raise typer.Exit(code=2) from exc


@app.command("shadow-study-create")
def shadow_study_create(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Create an immutable study and its isolated comparison arms."""

    paths, state, objects = _services()
    try:
        request = ShadowStudyCreateRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        execution = _shadow_service(paths, state, objects).create_study(request)
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "SHADOW_STUDY_CREATE_REJECTED"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": execution.manifest.evidence_status,
            "study": execution.manifest,
            "arms": execution.arms,
            "object_sha256_by_id": execution.object_sha256_by_id,
        }
    )


@app.command("shadow-assign")
def shadow_assign(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Freeze every arm assignment before any outcome is visible."""

    paths, state, objects = _services()
    try:
        request = ShadowDecisionAssignmentRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        assignment = _shadow_service(paths, state, objects).assign(request)
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "SHADOW_ASSIGNMENT_REJECTED"})
        raise typer.Exit(code=2) from exc
    _emit({"status": "ASSIGNED", "assignment": assignment})


@app.command("market-regime-classify")
def market_regime_classify(
    study_id: Annotated[str, typer.Argument(help="Frozen shadow study id.")],
    features_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Classify one signal-time market snapshot with fixed deterministic precedence."""

    paths, state, objects = _services()
    try:
        features = MarketRegimeFeatures.model_validate_json(
            features_file.read_text(encoding="utf-8")
        )
        snapshot = _shadow_service(paths, state, objects).classify_regime(
            study_id,
            features,
        )
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "MARKET_REGIME_REJECTED"})
        raise typer.Exit(code=2) from exc
    _emit({"status": snapshot.regime, "snapshot": snapshot})


@app.command("shadow-observation-record")
def shadow_observation_record(
    observation_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Record a versioned observation only after all PnL and NAV identities reconcile."""

    paths, state, objects = _services()
    try:
        draft = ShadowExecutionObservationDraft.model_validate_json(
            observation_file.read_text(encoding="utf-8")
        )
        observation = _shadow_service(paths, state, objects).record_observation(draft)
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "SHADOW_OBSERVATION_REJECTED"})
        raise typer.Exit(code=2) from exc
    _emit({"status": observation.status, "observation": observation})


@app.command("shadow-evaluate")
def shadow_evaluate(
    study_id: Annotated[str, typer.Argument(help="Frozen shadow study id.")],
    as_of: Annotated[str, typer.Option(help="Timezone-aware ISO evaluation time.")],
) -> None:
    """Compute deterministic paired out-of-sample metrics and Phase 8 admission."""

    paths, state, objects = _services()
    try:
        parsed_as_of = datetime.fromisoformat(as_of)
        execution = _shadow_service(paths, state, objects).evaluate(
            study_id,
            as_of=parsed_as_of,
        )
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "SHADOW_EVALUATION_REJECTED"})
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "status": execution.report.evidence_status,
            "report": execution.report,
            "phase8_admission": execution.admission,
            "report_object_sha256": execution.report_object_sha256,
            "admission_object_sha256": execution.admission_object_sha256,
        }
    )


@app.command("shadow-status")
def shadow_status(
    study_id: Annotated[str | None, typer.Option("--study-id")] = None,
) -> None:
    """Return safe shadow sample, maturity, report, and admission counts."""

    paths, state, objects = _services()
    _emit(_shadow_service(paths, state, objects).status(study_id))


@app.command("shadow-audit")
def shadow_audit(
    study_id: Annotated[str, typer.Argument(help="Frozen shadow study id.")],
) -> None:
    """Audit frozen objects, indexes, pair assignments, reports, and admission."""

    paths, state, objects = _services()
    _emit(_shadow_service(paths, state, objects).audit(study_id))


@app.command("shadow-recover")
def shadow_recover(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Idempotently rebuild provable study/arm indexes from the original request."""

    paths, state, objects = _services()
    try:
        request = ShadowStudyCreateRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        result = _shadow_service(paths, state, objects).recover_study(request)
    except (AStockError, OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "SHADOW_NOT_RECOVERABLE"})
        raise typer.Exit(code=2) from exc
    _emit(result)


@app.command("phase8-admission")
def phase8_admission(
    study_id: Annotated[str, typer.Argument(help="Frozen shadow study id.")],
) -> None:
    """Read the latest deterministic Phase 8 research-admission result."""

    paths, state, objects = _services()
    _emit(_shadow_service(paths, state, objects).latest_admission(study_id))


@app.command("adaptive-research-status")
def adaptive_research_status(
    study_id: Annotated[str | None, typer.Option("--study-id")] = None,
) -> None:
    """Report the read-only, admission-gated Phase 8 research boundary."""

    paths, state, objects = _services()
    shadow = _shadow_service(paths, state, objects)
    _emit(AdaptiveResearchStatusService(shadow).status(study_id))


@app.command("knowledge-source-list")
def knowledge_source_list() -> None:
    """List the validated knowledge allowlist without exposing private source text."""

    paths, _, _ = _services()
    registry = _knowledge_sources(paths)
    _emit(
        {
            "sources": [
                {
                    "source_id": source.source_id,
                    "display_name": source.display_name,
                    "identity_status": source.identity_status,
                    "access_status": source.access_status,
                    "enabled": source.enabled,
                    "online_collection_required": source.online_collection_required,
                    "content_types": source.collection_scope.content_types,
                }
                for source in registry.sources
            ]
        }
    )


@app.command("knowledge-local-coverage")
def knowledge_local_coverage(
    source_id: Annotated[str, typer.Argument(help="Allowlisted local-export source id.")],
    seed_source_id: Annotated[
        str | None,
        typer.Option(help="Required only when the author has multiple local seeds."),
    ] = None,
) -> None:
    """Verify a private local export without emitting its path or content."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    report = KnowledgeCoverageAuditService(state, objects, paths.parquet).audit_local_source(
        source,
        seed_source_id=seed_source_id,
    )
    _emit({"status": report.status, "report": report})


@app.command("knowledge-coverage-audit")
def knowledge_coverage_audit(
    quiescence_lag_seconds: Annotated[
        int,
        typer.Option(
            min=0,
            help="Seconds before audit start used as the frozen online-data cutoff.",
        ),
    ] = 30,
) -> None:
    """Reconcile allowlisted coverage, objects, SQLite, and Parquet indexes."""

    paths, state, objects = _services()
    report = KnowledgeCoverageAuditService(state, objects, paths.parquet).audit_registry(
        _knowledge_sources(paths),
        quiescence_lag=timedelta(seconds=quiescence_lag_seconds),
    )
    _emit({"status": report.status, "report": report})


@app.command("knowledge-structure-analyze")
def knowledge_structure_analyze(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Record text-free source structure metrics and the selected processing strategy."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    profiles = KnowledgeStructureProfileService(state, objects).analyze(source)
    _emit(
        {
            "status": "PENDING_REVIEW",
            "source_id": source_id,
            "profile_count": len(profiles),
            "profiles": profiles,
        }
    )


@app.command("knowledge-structure-status")
def knowledge_structure_status(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return the latest text-free source structure profiles for one author."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    _emit(KnowledgeStructureProfileService(state, objects).status(source_id))


@app.command("knowledge-structure-audit")
def knowledge_structure_audit(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Verify current structure profiles, immutable objects, and artifact links."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    _emit(KnowledgeStructureProfileService(state, objects).audit(source))


@app.command("knowledge-semantic-plan")
def knowledge_semantic_plan(
    source_id: Annotated[str, typer.Argument(help="Allowlisted Zhihu author source id.")],
) -> None:
    """Preview DETAIL_VERIFIED full-item inputs without emitting source text."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    plan = _semantic_funnel_service(paths, state, objects).plan(source_id)
    _emit({"status": "PLANNED", "plan": plan})


@app.command("knowledge-semantic-run")
def knowledge_semantic_run(
    source_id: Annotated[str, typer.Argument(help="Allowlisted Zhihu author source id.")],
) -> None:
    """Build ParagraphUnits and complete ArgumentUnits without comments or external models."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    execution = _semantic_funnel_service(paths, state, objects).run(source_id)
    _emit(
        {
            "status": execution.run.stage,
            "run_id": execution.run.run_id,
            "content_item_count": execution.run.content_item_count,
            "paragraph_count": execution.run.paragraph_count,
            "argument_unit_count": execution.run.argument_unit_count,
            "candidate_item_count": execution.candidate_item_count,
            "excluded_item_count": execution.excluded_item_count,
            "ready_argument_count": execution.ready_argument_count,
            "review_argument_count": execution.review_argument_count,
            "excluded_argument_count": execution.excluded_argument_count,
            "comments_included": False,
        }
    )


@app.command("knowledge-semantic-status")
def knowledge_semantic_status(
    source_id: Annotated[str, typer.Argument(help="Allowlisted Zhihu author source id.")],
) -> None:
    """Return private-safe metadata for the latest argument-aware run."""

    paths, state, _ = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    repository = SemanticFunnelRepository(state)
    active_run = repository.latest_run(source_id)
    completed_run = repository.latest_completed_run(source_id)
    if active_run is None:
        _emit({"status": "NOT_RUN", "active_run": None, "latest_usable_run": None})
        return

    def compact(semantic_run: SemanticFunnelRun) -> dict[str, object]:
        return {
            "run_id": semantic_run.run_id,
            "stage": semantic_run.stage,
            "input_manifest_sha256": semantic_run.input_manifest_sha256,
            "input_hash_count": len(semantic_run.input_hashes),
            "rule_config_sha256": semantic_run.rule_config_sha256,
            "versions": {
                "pipeline": semantic_run.pipeline_version,
                "paragraphizer": semantic_run.paragraphizer_version,
                "role_rule": semantic_run.role_rule_version,
                "relation_rule": semantic_run.relation_rule_version,
                "argument_builder": semantic_run.argument_builder_version,
                "keyword_rule": semantic_run.keyword_rule_version,
            },
        }

    usable_payload = None
    if completed_run is not None:
        usable_payload = {
            **compact(completed_run),
            "counts": repository.counts(completed_run.run_id),
            "summary": repository.summary(completed_run.run_id),
        }
    _emit(
        {
            "status": active_run.stage,
            "active_run": compact(active_run),
            "latest_usable_run": usable_payload,
        }
    )


@app.command("knowledge-semantic-model-install")
def knowledge_semantic_model_install() -> None:
    """Install and hash the approved fixed local BGE model revision."""

    paths, _, _ = _services()
    directory = default_model_directory(paths.runtime)
    manifest = install_local_model(directory)
    _emit(
        {
            "status": "AVAILABLE",
            "model_id": manifest.model_id,
            "model_revision": manifest.model_revision,
            "bundle_sha256": manifest.bundle_sha256,
            "dimension": manifest.dimension,
            "local_only": True,
        }
    )


@app.command("knowledge-semantic-model-status")
def knowledge_semantic_model_status() -> None:
    """Verify the approved local semantic model without network access."""

    paths, _, _ = _services()
    try:
        manifest = verify_local_model(default_model_directory(paths.runtime))
    except FileNotFoundError:
        _emit({"status": "UNAVAILABLE", "reason_code": "LOCAL_MODEL_NOT_INSTALLED"})
        return
    except ValueError:
        _emit({"status": "REJECTED", "reason_code": "LOCAL_MODEL_HASH_MISMATCH"})
        return
    _emit(
        {
            "status": "AVAILABLE",
            "model_id": manifest.model_id,
            "model_revision": manifest.model_revision,
            "bundle_sha256": manifest.bundle_sha256,
            "dimension": manifest.dimension,
            "local_only": True,
        }
    )


@app.command("knowledge-semantic-embedding-run")
def knowledge_semantic_embedding_run(
    run_id: Annotated[str, typer.Argument(help="Argument-aware semantic run id.")],
    batch_size: Annotated[int, typer.Option(min=1, max=128)] = 16,
) -> None:
    """Generate the three required local embedding views and uncalibrated scores."""

    paths, state, objects = _services()
    model_directory = default_model_directory(paths.runtime)
    asset = verify_local_model(model_directory)
    config = load_semantic_funnel_config(
        paths.root / "configs" / "knowledge_semantic_funnel.yaml"
    )
    execution = SemanticEmbeddingService(
        SemanticFunnelRepository(state),
        objects,
        ParquetSemanticStore(paths.parquet),
        config,
        SentenceTransformerBackend(model_directory, batch_size=batch_size),
        asset,
    ).run(run_id)
    _emit(
        {
            "status": "EMBEDDING_SCREENED",
            "run_id": run_id,
            "embedding_manifest_id": execution.manifest.manifest_id,
            "vector_count": execution.vector_count,
            "score_count": execution.score_count,
            "keep_count": execution.keep_count,
            "review_count": execution.review_count,
            "calibration_required_count": execution.calibration_required_count,
            "automatic_exclusion_enabled": False,
            "parquet_file_count": 2,
        }
    )


@app.command("knowledge-semantic-packet-export")
def knowledge_semantic_packet_export(
    run_id: Annotated[str, typer.Argument(help="Embedding-screened semantic run id.")],
) -> None:
    """Materialize complete ArgumentUnits for a manual OpenCode/DeepSeek run."""

    paths, state, objects = _services()
    execution = SemanticPacketService(
        SemanticFunnelRepository(state),
        objects,
        ParquetSemanticStore(paths.parquet),
        paths.runtime,
        paths.root / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).export(run_id)
    relative_directory = (
        execution.batch_directory.relative_to(paths.root).as_posix()
        if execution.batch_directory.is_relative_to(paths.root)
        else execution.batch_directory.name
    )
    _emit(
        {
            "status": execution.batch.status,
            "batch_id": execution.batch.batch_id,
            "run_id": run_id,
            "exported_argument_count": execution.batch.exported_argument_count,
            "held_back_calibration_count": execution.held_back_calibration_count,
            "held_back_structural_count": execution.held_back_structural_count,
            "held_back_oversize_count": execution.held_back_oversize_count,
            "batch_directory": relative_directory,
            "prompt_file": "OPENCODE_DEEPSEEK_PROMPT.md",
            "expected_result_file": "deepseek-results.jsonl",
            "external_request_sent": False,
        }
    )


@app.command("knowledge-semantic-result-stage")
def knowledge_semantic_result_stage(
    batch_id: Annotated[str, typer.Argument(help="Offline semantic batch id.")],
    result_file: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Validate and stage one complete OpenCode/DeepSeek JSONL response."""

    paths, state, objects = _services()
    batch = SemanticPacketService(
        SemanticFunnelRepository(state),
        objects,
        ParquetSemanticStore(paths.parquet),
        paths.runtime,
        paths.root / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).stage_results(batch_id, result_file)
    _emit(
        {
            "status": batch.status,
            "batch_id": batch.batch_id,
            "run_id": batch.run_id,
            "validated_result_count": batch.imported_result_count,
        }
    )


@app.command("knowledge-semantic-result-import")
def knowledge_semantic_result_import(
    batch_id: Annotated[str, typer.Argument(help="Staged offline semantic batch id.")],
) -> None:
    """Atomically create pending AU-level candidates from validated kept results."""

    paths, state, objects = _services()
    batch, candidate_count = SemanticPacketService(
        SemanticFunnelRepository(state),
        objects,
        ParquetSemanticStore(paths.parquet),
        paths.runtime,
        paths.root / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).import_results(batch_id)
    _emit(
        {
            "status": batch.status,
            "batch_id": batch.batch_id,
            "run_id": batch.run_id,
            "validated_result_count": batch.imported_result_count,
            "skill_candidate_count": candidate_count,
            "evaluation_status": "NOT_RUN",
            "approval_status": "PENDING",
        }
    )


@app.command("knowledge-distill-plan")
def knowledge_distill_plan(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Preview private-safe distillation inputs without emitting source text."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    plan = KnowledgeDistillationService(state, objects, paths.parquet).plan(
        source,
        _distillation_rules(paths),
    )
    _emit({"status": "PLANNED", "plan": plan})


@app.command("knowledge-distill-run")
def knowledge_distill_run(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Reject the legacy paragraph-level write path; use knowledge-semantic-run."""

    _emit(
        {
            "status": "POLICY_REJECTED",
            "reason_code": "LEGACY_SEMANTIC_PIPELINE_PAUSED",
            "source_id": source_id,
            "replacement_command": "knowledge-semantic-run",
        }
    )
    raise typer.Exit(code=2)


@app.command("knowledge-distill-status")
def knowledge_distill_status(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return the latest safe distillation report for one author."""

    paths, state, _ = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    repository = DistillationRepository(state)
    report = repository.latest_author_report(source_id)
    rules = _distillation_rules(paths)
    run = repository.get_run(report.run_id) if report is not None else None
    stale = bool(
        run is None
        or run.classification_rule_version != rules.rule_version
    ) if report is not None else False
    _emit(
        {"status": "NOT_RUN", "report": None}
        if report is None
        else {
            "status": "STALE" if stale else report.coverage_status,
            "stale": stale,
            "current_rule_version": rules.rule_version,
            "current_generation_rule_version": (
                KnowledgeDraftService.generation_rule_version
            ),
            "report": report,
        }
    )


@app.command("knowledge-distill-audit")
def knowledge_distill_audit(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Verify distillation objects and SQLite/Parquet correspondence."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    rules = _distillation_rules(paths)
    audit = KnowledgeDistillationService(state, objects, paths.parquet).audit(
        source_id,
        expected_rule_version=rules.rule_version,
    )
    _emit(audit)


@app.command("knowledge-review-queue")
def knowledge_review_queue(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return review-queue metadata without source text or private paths."""

    paths, state, _ = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    repository = DistillationRepository(state)
    report = repository.latest_author_report(source_id)
    summary = repository.latest_review_queue_summary(source_id)
    run = repository.get_run(report.run_id) if report is not None else None
    rules = _distillation_rules(paths)
    stale = bool(
        run is None
        or run.classification_rule_version != rules.rule_version
    ) if report is not None else False
    _emit(
        {"status": "NOT_RUN", "queue": None}
        if summary is None
        else {
            "status": "STALE" if stale else summary["human_review_status"],
            "stale": stale,
            "current_rule_version": rules.rule_version,
            "queue": summary,
        }
    )


@app.command("knowledge-draft-generate")
def knowledge_draft_generate(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Reject legacy paragraph-to-Skill writes; use the AU/DeepSeek import path."""

    _emit(
        {
            "status": "POLICY_REJECTED",
            "reason_code": "LEGACY_SEMANTIC_PIPELINE_PAUSED",
            "source_id": source_id,
            "replacement_command": "knowledge-semantic-packet-export",
        }
    )
    raise typer.Exit(code=2)


@app.command("knowledge-draft-status")
def knowledge_draft_status(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return private draft metadata without source excerpts or private paths."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    rules = _distillation_rules(paths)
    report = KnowledgeDraftService(
        state,
        objects,
        required_classification_rule_version=rules.rule_version,
    ).current_report(source_id)
    distillation_repository = DistillationRepository(state)
    run = (
        distillation_repository.get_run(report.run_id)
        if report is not None
        else None
    )
    stale = bool(
        run is None
        or run.classification_rule_version != rules.rule_version
    ) if report is not None else False
    _emit(
        {"status": "NOT_RUN", "report": None}
        if report is None
        else {
            "status": "STALE" if stale else report.human_review_status,
            "stale": stale,
            "current_rule_version": rules.rule_version,
            "report": report,
        }
    )


@app.command("knowledge-draft-audit")
def knowledge_draft_audit(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Audit private draft payloads, source references, and approval gates."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    rules = _distillation_rules(paths)
    _emit(
        KnowledgeDraftService(
            state,
            objects,
            required_classification_rule_version=rules.rule_version,
        ).audit(source_id)
    )


@app.command("zhihu-author-probe")
def zhihu_author_probe(
    source_id: Annotated[str, typer.Argument(help="Allowlisted knowledge source id.")],
) -> None:
    """Probe one confirmed Zhihu profile with a persisted low-frequency response."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    service = ZhihuCollectionService(
        state,
        objects,
        ParquetKnowledgeStore(paths.parquet),
    )
    try:
        identity = service.probe_identity(source)
    except AStockError as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": exc.failure_class,
                "message": str(exc),
                "details": exc.details,
            }
        )
        raise typer.Exit(code=3) from exc
    _emit({"status": "CONFIRMED", "identity": identity})


@app.command("zhihu-response-import")
def zhihu_response_import(
    envelope: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ],
) -> None:
    """Persist one credential-free Chrome/manual Zhihu response envelope."""

    paths, state, objects = _services()
    service = ZhihuResponseImportService(state, objects, paths.runtime)
    try:
        execution = service.import_file(
            envelope,
            _knowledge_sources(paths),
            _zhihu_endpoint_templates(paths),
        )
    except AStockError as exc:
        _emit(
            {
                "status": "REJECTED",
                "failure_class": exc.failure_class,
                "message": str(exc),
                "details": exc.details,
            }
        )
        raise typer.Exit(code=3) from exc
    record = execution.record
    _emit(
        {
            "status": "IMPORTED",
            "envelope_id": record.envelope_id,
            "author_source_id": record.author_source_id,
            "response_kind": record.response_kind,
            "content_type": record.content_type,
            "http_status": record.status_code,
            "body_byte_size": record.body_byte_size,
            "source_snapshot_id": record.source_snapshot_id,
            "raw_object_sha256": record.raw_object_sha256,
            "import_status": record.import_status,
            "response_failure": execution.response_failure,
        }
    )


@app.command("zhihu-capture-serve")
def zhihu_capture_serve(
    source_id: Annotated[str, typer.Argument(help="Allowlisted knowledge source id.")],
    content_type: Annotated[
        ZhihuContentType,
        typer.Option(case_sensitive=False, help="answers, articles, or thoughts"),
    ] = ZhihuContentType.ANSWERS,
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8765,
    page_size: Annotated[int, typer.Option(min=1, max=100)] = 20,
    request_interval_seconds: Annotated[
        float,
        typer.Option(min=2, help="Delay between browser requests; minimum 2 seconds."),
    ] = 2.0,
    ttl_seconds: Annotated[
        int,
        typer.Option(min=60, max=3600, help="One-time capture session lifetime."),
    ] = 900,
) -> None:
    """Serve one credential-free, localhost-only Zhihu capture session."""

    paths, state, objects = _services()
    try:
        session = ZhihuLoopbackCaptureSession(
            state,
            objects,
            paths.runtime,
            ParquetKnowledgeStore(paths.parquet),
            _knowledge_sources(paths),
            _zhihu_endpoint_templates(paths),
            source_id=source_id,
            content_type=content_type,
            page_size=page_size,
            request_interval_seconds=request_interval_seconds,
            ttl_seconds=ttl_seconds,
        )
        server = create_loopback_capture_server(session, port=port)
    except (AStockError, OSError, ValueError) as exc:
        failure = exc.failure_class if isinstance(exc, AStockError) else "INVALID_ARGUMENT"
        _emit(
            {
                "status": "REJECTED",
                "failure_class": failure,
                "message": str(exc),
            }
        )
        raise typer.Exit(code=3) from exc
    _emit(
        {
            **session.safe_status(),
            "bind_host": "127.0.0.1",
            "installer_url": loopback_installer_url(server),
            "status_url": loopback_status_url(server),
        }
    )
    try:
        final_status = serve_loopback_capture(server, session)
    except KeyboardInterrupt:
        _emit({**session.safe_status(), "status": "INTERRUPTED"})
        return
    _emit(final_status)


@app.command("zhihu-full-capture-serve")
def zhihu_full_capture_serve(
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8765,
    page_size: Annotated[int, typer.Option(min=1, max=100)] = 20,
    request_interval_seconds: Annotated[
        float,
        typer.Option(min=2, help="Delay between browser requests; minimum 2 seconds."),
    ] = 2.0,
    ttl_seconds: Annotated[
        int,
        typer.Option(min=60, max=86_400, help="Full capture session lifetime."),
    ] = 21_600,
    session_token: Annotated[
        str | None,
        typer.Option(
            hidden=True,
            envvar="ASTOCK_ZHIHU_CAPTURE_SESSION_TOKEN",
        ),
    ] = None,
) -> None:
    """Serve one localhost session for all online authors and verified boundaries."""

    paths, state, objects = _services()
    try:
        session = ZhihuFullCaptureSession(
            state,
            objects,
            paths.runtime,
            ParquetKnowledgeStore(paths.parquet),
            _knowledge_sources(paths),
            _zhihu_endpoint_templates(paths),
            page_size=page_size,
            request_interval_seconds=request_interval_seconds,
            ttl_seconds=ttl_seconds,
            session_token=session_token,
        )
        server = create_loopback_capture_server(session, port=port)
    except (AStockError, OSError, ValueError) as exc:
        failure = exc.failure_class if isinstance(exc, AStockError) else "INVALID_ARGUMENT"
        _emit(
            {
                "status": "REJECTED",
                "failure_class": failure,
                "message": str(exc),
            }
        )
        raise typer.Exit(code=3) from exc
    _emit(
        {
            **session.safe_status(),
            "bind_host": "127.0.0.1",
            "installer_url": loopback_installer_url(server),
            "status_url": loopback_status_url(server),
            "extension_directory": str(
                build_coordinator_capture_extension(
                    runtime_root=paths.runtime,
                    bridge_origin=loopback_installer_url(server).split(
                        "/install/", maxsplit=1
                    )[0],
                    session_token=session.session_token,
                    interval_ms=round(request_interval_seconds * 1000),
                ).relative_to(paths.root)
            ),
        }
    )
    try:
        final_status = serve_loopback_capture(server, session)
    except KeyboardInterrupt:
        _emit({**session.safe_status(), "status": "INTERRUPTED"})
        return
    _emit(final_status)


@app.command("zhihu-import-replay")
def zhihu_import_replay(
    envelope_id: Annotated[str, typer.Argument(help="Registered response envelope id.")],
    recover_consumed: Annotated[
        bool,
        typer.Option(
            help=(
                "Replay a consumed raw listing only when no page manifest was committed; "
                "used after a parser upgrade."
            )
        ),
    ] = False,
) -> None:
    """Consume one imported listing response through the normal checkpoint pipeline."""

    paths, state, objects = _services()
    service = ZhihuResponseImportService(state, objects, paths.runtime)
    try:
        imported = service.repository.get_imported_response(envelope_id)
        if imported is not None and imported.response_kind in {
            ZhihuResponseKind.ROOT_COMMENTS,
            ZhihuResponseKind.CHILD_COMMENTS,
        }:
            comment_replay = service.replay_comment(
                envelope_id,
                _knowledge_sources(paths),
                ParquetKnowledgeStore(paths.parquet),
            )
            if comment_replay.comment_execution is None:
                _emit(
                    {
                        "status": (
                            "CONSUMED_WITH_GAP"
                            if comment_replay.response_failure
                            else "ALREADY_CONSUMED"
                        ),
                        "envelope_id": comment_replay.record.envelope_id,
                        "import_status": comment_replay.record.import_status,
                        "response_failure": comment_replay.response_failure,
                    }
                )
                return
            comment_execution = comment_replay.comment_execution
            _emit(
                {
                    "status": "CONSUMED",
                    "envelope_id": comment_replay.record.envelope_id,
                    "import_status": comment_replay.record.import_status,
                    "response_kind": comment_replay.record.response_kind,
                    "comment_page_id": comment_execution.page.page_id,
                    "comment_record_count": len(comment_execution.comment_records),
                    "participation_chain_count": len(comment_execution.participation_chains),
                }
            )
            return
        if imported is not None and imported.response_kind is ZhihuResponseKind.CONTENT_DETAIL:
            detail_replay = service.replay_detail(
                envelope_id,
                _knowledge_sources(paths),
                ParquetKnowledgeStore(paths.parquet),
            )
            if detail_replay.content_record is None:
                _emit(
                    {
                        "status": (
                            "CONSUMED_WITH_GAP"
                            if detail_replay.response_failure
                            else "ALREADY_CONSUMED"
                        ),
                        "envelope_id": detail_replay.record.envelope_id,
                        "import_status": detail_replay.record.import_status,
                        "response_failure": detail_replay.response_failure,
                    }
                )
                return
            _emit(
                {
                    "status": "CONSUMED",
                    "envelope_id": detail_replay.record.envelope_id,
                    "import_status": detail_replay.record.import_status,
                    "content_id": detail_replay.content_record.content_id,
                    "content_type": detail_replay.content_record.content_type,
                    "content_completeness": (detail_replay.content_record.content_completeness),
                }
            )
            return
        replay = service.replay_listing(
            envelope_id,
            _knowledge_sources(paths),
            ParquetKnowledgeStore(paths.parquet),
            recover_consumed=recover_consumed,
        )
    except AStockError as exc:
        _emit(
            {
                "status": "REJECTED",
                "failure_class": exc.failure_class,
                "message": str(exc),
                "details": exc.details,
            }
        )
        raise typer.Exit(code=3) from exc
    if replay.sync_execution is None:
        _emit(
            {
                "status": "ALREADY_CONSUMED",
                "envelope_id": replay.record.envelope_id,
                "import_status": replay.record.import_status,
            }
        )
        return
    execution = replay.sync_execution
    _emit(
        {
            "status": "CONSUMED",
            "envelope_id": replay.record.envelope_id,
            "import_status": replay.record.import_status,
            "coverage_status": execution.report.coverage_status,
            "terminal_condition": execution.report.terminal_condition,
            "listing_page_count": len(execution.listing_pages),
            "content_record_count": len(execution.content_records),
        }
    )


@app.command("zhihu-python-recover")
def zhihu_python_recover(
    source_ids: Annotated[
        list[str] | None,
        typer.Option("--source-id", help="Optional allowlisted author; repeat for several."),
    ] = None,
    response_kinds: Annotated[
        list[ZhihuResponseKind] | None,
        typer.Option(
            "--response-kind",
            help="Optional verified CONTENT_DETAIL task kind.",
        ),
    ] = None,
    max_requests: Annotated[
        int | None,
        typer.Option(min=1, help="Optional smoke limit; omitted means run to a hard boundary."),
    ] = None,
    request_interval_seconds: Annotated[
        float,
        typer.Option(min=2, help="Minimum delay between verified Python API requests."),
    ] = 2.0,
) -> None:
    """Recover active Zhihu listings and verified content details with Python HTTP."""

    paths, state, objects = _services()
    service = ZhihuPythonRecoveryService(
        state,
        objects,
        paths.runtime,
        ParquetKnowledgeStore(paths.parquet),
        _knowledge_sources(paths),
        _zhihu_endpoint_templates(paths),
        request_interval_seconds=request_interval_seconds,
    )
    try:
        execution = service.run(
            source_ids=source_ids,
            response_kinds=response_kinds,
            max_requests=max_requests,
        )
    except (AStockError, ValueError) as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": (
                    exc.failure_class if isinstance(exc, AStockError) else "INVALID_ARGUMENT"
                ),
                "message": str(exc),
            }
        )
        raise typer.Exit(code=3) from exc
    _emit(execution)


@app.command("zhihu-article-html-recover")
def zhihu_article_html_recover(
    source_ids: Annotated[
        list[str] | None,
        typer.Option("--source-id", help="Optional allowlisted author; repeat for several."),
    ] = None,
    max_requests: Annotated[
        int | None,
        typer.Option(min=1, help="Optional smoke limit; omitted means run to a hard boundary."),
    ] = None,
    request_interval_seconds: Annotated[
        float,
        typer.Option(min=2, help="Minimum delay between canonical article page requests."),
    ] = 2.0,
) -> None:
    """Recover enumerated article full text from strict canonical HTML pages."""

    paths, state, objects = _services()
    service = ZhihuArticleRecoveryService(
        state,
        objects,
        ParquetKnowledgeStore(paths.parquet),
        _knowledge_sources(paths),
        request_interval_seconds=request_interval_seconds,
    )
    try:
        execution = service.run(
            source_ids=source_ids,
            max_requests=max_requests,
        )
    except (AStockError, ValueError) as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": (
                    exc.failure_class if isinstance(exc, AStockError) else "INVALID_ARGUMENT"
                ),
                "message": str(exc),
            }
        )
        raise typer.Exit(code=3) from exc
    _emit(execution)


@app.command("zhihu-author-sync")
def zhihu_author_sync(
    source_id: Annotated[str, typer.Argument(help="Allowlisted knowledge source id.")],
    content_type: Annotated[
        ZhihuContentType,
        typer.Option(case_sensitive=False, help="answers, articles, or thoughts"),
    ] = ZhihuContentType.ANSWERS,
    max_pages: Annotated[
        int | None,
        typer.Option(help="Optional smoke limit; a capped run remains PARTIAL."),
    ] = None,
    page_size: Annotated[int, typer.Option(min=1, max=100)] = 20,
    request_interval_seconds: Annotated[
        float,
        typer.Option(min=0, help="Minimum delay between listing requests."),
    ] = 2.0,
) -> None:
    """Synchronize one allowlisted Zhihu listing without emitting collected text."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    service = ZhihuCollectionService(
        state,
        objects,
        ParquetKnowledgeStore(paths.parquet),
        minimum_request_interval_seconds=request_interval_seconds,
    )
    try:
        execution = service.sync_listing(
            source,
            content_type,
            max_pages=max_pages,
            page_size=page_size,
        )
    except AStockError as exc:
        _emit(
            {
                "status": "FAILED",
                "failure_class": exc.failure_class,
                "message": str(exc),
                "details": exc.details,
            }
        )
        raise typer.Exit(code=3) from exc
    _emit(
        {
            "status": execution.report.coverage_status,
            "job_id": execution.job_id,
            "report": execution.report,
            "listing_page_count": len(execution.listing_pages),
            "content_record_count": len(execution.content_records),
            "parquet_file_count": len(execution.parquet_files),
        }
    )


@app.command("zhihu-manual-tasks")
def zhihu_manual_tasks(
    include_tasks: Annotated[
        bool,
        typer.Option(help="Also print every task; the full list is always saved locally."),
    ] = False,
) -> None:
    """Refresh and export exact manual recovery boundaries without source text."""

    paths, state, objects = _services()
    tasks = ZhihuManualTaskService(state, objects).refresh(_knowledge_sources(paths))
    report_path = paths.runtime / "reports" / "zhihu-manual-tasks.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(ZoneInfo("UTC")),
        "open_task_count": len(tasks),
        "tasks": tasks,
    }
    atomic_write_text(
        report_path,
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True),
    )
    counts: dict[str, int] = {}
    for task in tasks:
        key = f"{task.author_source_id}|{task.response_kind}"
        counts[key] = counts.get(key, 0) + 1
    output: dict[str, Any] = {
        "status": "COMPLETE" if not tasks else "MANUAL_ACTION_REQUIRED",
        "open_task_count": len(tasks),
        "count_by_author_and_kind": counts,
        "report_file": "runtime/reports/zhihu-manual-tasks.json",
    }
    if include_tasks:
        output["tasks"] = tasks
    _emit(output)


@app.command("zhihu-coverage")
def zhihu_coverage(
    source_id: Annotated[str, typer.Argument(help="Allowlisted knowledge source id.")],
    content_type: Annotated[
        ZhihuContentType,
        typer.Option(case_sensitive=False, help="answers, articles, or thoughts"),
    ] = ZhihuContentType.ANSWERS,
) -> None:
    """Return the latest durable coverage report for one Zhihu listing scope."""

    _, state, _ = _services()
    report = KnowledgeRepository(state).latest_coverage_report(source_id, content_type)
    _emit(
        {"status": "NOT_COLLECTED", "report": None}
        if report is None
        else {"status": report.coverage_status, "report": report}
    )


@app.command("codex-run-init")
def codex_run_init(
    request: Annotated[str, typer.Argument(help="Natural-language task text.")],
    skills: Annotated[list[str] | None, typer.Option("--skill")] = None,
    artifacts: Annotated[list[Path] | None, typer.Option("--artifact")] = None,
    artifact_ids: Annotated[list[str] | None, typer.Option("--artifact-id")] = None,
    require_registered_output: Annotated[
        bool,
        typer.Option(
            "--require-registered-output/--allow-unregistered-output",
            help="Require an exact registered deterministic artifact as output.",
        ),
    ] = False,
) -> None:
    """Initialize an auditable Codex run directory and context budget."""

    paths, state, objects = _services()
    service = CodexRunService(paths.runtime, objects, state)
    try:
        budget, references = _context_budget_with_registered(
            service,
            skills=skills or [],
            artifact_paths=artifacts or [],
            artifact_ids=artifact_ids or [],
        )
        input_manifest = CodexRunInputManifest(
            selected_skills=skills or [],
            artifact_references=references,
            legacy_artifact_paths=[str(path.resolve()) for path in artifacts or []],
            require_registered_output=require_registered_output,
        )
        manifest = service.initialize(
            {"request": request},
            context_budget=budget,
            input_manifest=input_manifest,
        )
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_CODEX_INPUT"})
        raise typer.Exit(code=2) from exc
    _emit(manifest)


@app.command("codex-run-import")
def codex_run_import(
    run_id: Annotated[str, typer.Argument()],
    draft: Annotated[Path, typer.Argument(exists=True, file_okay=True, dir_okay=False)],
) -> None:
    """Stage and validate a Codex artifact draft; direct ledger commands are rejected."""

    paths, state, objects = _services()
    service = CodexRunService(paths.runtime, objects, state)
    try:
        service.stage_draft(run_id, draft)
        report = service.import_draft(run_id)
    except AStockError as exc:
        _emit({"valid": False, "failure_class": exc.failure_class.value})
        raise typer.Exit(code=2) from exc
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"valid": False, "error_code": "INVALID_CODEX_DRAFT"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if not report.valid:
        raise typer.Exit(code=2)


@app.command("codex-run-status")
def codex_run_status(
    run_id: Annotated[str, typer.Argument(help="Codex run identifier.")],
) -> None:
    """Return safe frozen-input and validated-output metadata for one run."""

    paths, state, objects = _services()
    try:
        _emit(CodexRunService(paths.runtime, objects, state).status(run_id))
    except ValueError as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RUN_ID"})
        raise typer.Exit(code=2) from exc


@app.command("codex-run-audit")
def codex_run_audit(
    run_id: Annotated[str, typer.Argument(help="Codex run identifier.")],
) -> None:
    """Audit frozen inputs, files, objects, indexes, and run status."""

    paths, state, objects = _services()
    try:
        _emit(CodexRunService(paths.runtime, objects, state).audit(run_id))
    except ValueError as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_RUN_ID"})
        raise typer.Exit(code=2) from exc


@app.command("codex-run-recover")
def codex_run_recover(
    run_id: Annotated[str, typer.Argument(help="Codex run identifier.")],
) -> None:
    """Idempotently finish a staged run after an interrupted import."""

    paths, state, objects = _services()
    service = CodexRunService(paths.runtime, objects, state)
    try:
        report = service.recover(run_id)
    except (OSError, ValidationError, ValueError) as exc:
        _emit({"valid": False, "error_code": "CODEX_RUN_NOT_RECOVERABLE"})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if not report.valid:
        raise typer.Exit(code=2)
