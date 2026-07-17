"""Stable local CLI used directly and by project Repo Skills."""

from __future__ import annotations

import json
import platform
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import typer
from pydantic import BaseModel, TypeAdapter, ValidationError

from astock import __version__
from astock.books import PrivateDocxIngestService, PrivatePdfIngestService
from astock.core.codex_runs import CodexRunService, build_context_budget
from astock.core.errors import AStockError
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
from astock.knowledge import (
    DistillationRepository,
    KnowledgeCoverageAuditService,
    KnowledgeDistillationService,
    KnowledgeDraftRepository,
    KnowledgeDraftService,
    KnowledgeRepository,
    ParquetKnowledgeStore,
    ZhihuCollectionService,
    ZhihuResponseImportService,
    get_knowledge_source,
    load_distillation_rules,
    load_knowledge_sources,
    load_zhihu_endpoint_templates,
)
from astock.market_data.storage import (
    CanonicalMarketStore,
    ParquetMarketStore,
    canonical_manifest_path,
)
from astock.market_data.sync import MarketSyncService
from astock.paper_trading import LedgerService, PaperReplayService, load_fee_schedule
from astock.providers import EastMoney5mProvider, Sina5mProvider
from astock.research import (
    ResearchCoreService,
    ResearchDiagnosticsService,
    ResearchRepository,
    ResearchSkillService,
    load_research_core_config,
    load_research_diagnostic_config,
    load_research_skill_registry,
)
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    BaseCaseBuildRequest,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    DistillationClassRuleSet,
    DocumentType,
    EvidenceFreezeRequest,
    FinancialAuditRequest,
    InstrumentType,
    KnowledgeSourceRegistry,
    Market,
    ResearchCoreConfig,
    ResearchDiagnosticConfig,
    ResearchMemoComposeRequest,
    ResearchSkillRegistry,
    SpecialistDeltaBuildRequest,
    SpecialistDiagnosticRequest,
    SpecialistRouteRequest,
    ZhihuContentType,
    ZhihuEndpointTemplateRegistry,
    ZhihuResponseKind,
)
from astock.settings import ProjectPaths

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
    return load_zhihu_endpoint_templates(
        paths.root / "configs" / "zhihu_endpoint_templates.yaml"
    )


def _distillation_rules(paths: ProjectPaths) -> DistillationClassRuleSet:
    return load_distillation_rules(
        paths.root / "configs" / "knowledge_distillation_rules.yaml"
    )


def _research_core(paths: ProjectPaths) -> ResearchCoreConfig:
    return load_research_core_config(paths.root / "configs" / "research_core.yaml")


def _research_skills(paths: ProjectPaths) -> ResearchSkillRegistry:
    return load_research_skill_registry(paths.root / "configs" / "research_skills.yaml")


def _research_diagnostics(paths: ProjectPaths) -> ResearchDiagnosticConfig:
    return load_research_diagnostic_config(
        paths.root / "configs" / "research_diagnostics.yaml"
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
            "providers": [provider.capability() for provider in providers],
        }
    )


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
                industry_profiles
                or paths.root / "configs" / "financial_industry_profiles.yaml"
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

    paths, state, _ = _services()
    parsed_cursor = _parse_local_datetime(cursor)
    profile_path = fee_rules or paths.root / "configs" / "fee_rules.yaml"
    schedule = load_fee_schedule(profile_path)
    store = CanonicalMarketStore(paths.parquet, paths.manifests)
    service = PaperReplayService(LedgerService(state), store)
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
) -> None:
    """Estimate local input size and duplicate reads before a Codex research run."""

    report = build_context_budget(skills=skills or [], artifact_paths=artifacts or [])
    _emit(report)


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
    execution = ResearchCoreService(state, objects, _research_core(paths)).freeze_evidence(
        request
    )
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
        request = BaseCaseBuildRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        _emit({"status": "REJECTED", "error_code": "INVALID_BASE_CASE_REQUEST"})
        raise typer.Exit(code=2) from exc
    execution = ResearchCoreService(state, objects, _research_core(paths)).build_base_case(
        request
    )
    pack = execution.pack
    _emit(
        {
            "status": "BUILT",
            "base_case_id": pack.base_case_id,
            "evidence_pack_id": pack.evidence_pack_id,
            "object_sha256": execution.object_sha256,
            "company_id": pack.company_id,
            "as_of": pack.as_of,
            "finding_count": sum(
                len(items) for items in pack.findings_by_section.values()
            ),
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
                    "input_schema": {
                        "IndustryBottleneckSkill": "IndustryBottleneckDiagnosticRequest",
                        "EventToAlphaSkill": "EventToAlphaDiagnosticRequest",
                        "GrowthProbabilitySkill": "GrowthProbabilityDiagnosticRequest",
                        "GrowthValuationLens": "GrowthValuationDiagnosticRequest",
                        "DailyTrendHealthSkill": "DailyTrendDiagnosticRequest",
                        "HourlySwingSkill": "HourlySwingDiagnosticRequest",
                    }[item.skill_id],
                }
                for item in registry.skills
                if item.counts_as_specialist
            ],
            "memo": {
                "skill_id": "ResearchMemoComposer",
                "skill_version": "research-memo-composer-v1",
                "input_schema": "ResearchMemoComposeRequest",
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
        request = TypeAdapter(SpecialistDiagnosticRequest).validate_json(
            request_file.read_text(encoding="utf-8")
        )
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
        request = ResearchMemoComposeRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
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
def knowledge_coverage_audit() -> None:
    """Reconcile allowlisted coverage, objects, SQLite, and Parquet indexes."""

    paths, state, objects = _services()
    report = KnowledgeCoverageAuditService(state, objects, paths.parquet).audit_registry(
        _knowledge_sources(paths)
    )
    _emit({"status": report.status, "report": report})


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
    """Classify immutable author material and create a pending review queue."""

    paths, state, objects = _services()
    source = get_knowledge_source(_knowledge_sources(paths), source_id)
    execution = KnowledgeDistillationService(state, objects, paths.parquet).run(
        source,
        _distillation_rules(paths),
    )
    _emit(
        {
            "status": execution.run.status,
            "run_id": execution.run.run_id,
            "report": execution.report,
            "review_queue": {
                "queue_id": execution.review_queue.queue_id,
                "candidate_count": len(execution.review_queue.unit_ids),
                "human_review_status": execution.review_queue.human_review_status,
            },
            "parquet_file_count": 1,
            "book_cleaning_report_ids": execution.book_cleaning_report_ids,
            "book_method_coverage_report_ids": (
                execution.book_method_coverage_report_ids
            ),
        }
    )


@app.command("knowledge-distill-status")
def knowledge_distill_status(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return the latest safe distillation report for one author."""

    paths, state, _ = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    report = DistillationRepository(state).latest_author_report(source_id)
    _emit(
        {"status": "NOT_RUN", "report": None}
        if report is None
        else {"status": report.coverage_status, "report": report}
    )


@app.command("knowledge-distill-audit")
def knowledge_distill_audit(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Verify distillation objects and SQLite/Parquet correspondence."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    audit = KnowledgeDistillationService(state, objects, paths.parquet).audit(source_id)
    _emit(audit)


@app.command("knowledge-review-queue")
def knowledge_review_queue(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return review-queue metadata without source text or private paths."""

    paths, state, _ = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    summary = DistillationRepository(state).latest_review_queue_summary(source_id)
    _emit(
        {"status": "NOT_RUN", "queue": None}
        if summary is None
        else {"status": summary["human_review_status"], "queue": summary}
    )


@app.command("knowledge-draft-generate")
def knowledge_draft_generate(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Generate private excerpt drafts and unevaluated Skill candidates."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    execution = KnowledgeDraftService(state, objects).generate(source_id)
    _emit(
        {
            "status": "PENDING_REVIEW",
            "report": execution.report,
            "viewpoint_draft_count": len(execution.viewpoint_drafts),
            "skill_candidate_count": len(execution.skill_candidates),
        }
    )


@app.command("knowledge-draft-status")
def knowledge_draft_status(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Return private draft metadata without source excerpts or private paths."""

    paths, state, _ = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    report = KnowledgeDraftRepository(state).latest_report(source_id)
    _emit(
        {"status": "NOT_RUN", "report": None}
        if report is None
        else {"status": report.human_review_status, "report": report}
    )


@app.command("knowledge-draft-audit")
def knowledge_draft_audit(
    source_id: Annotated[str, typer.Argument(help="Allowlisted author source id.")],
) -> None:
    """Audit private draft payloads, source references, and approval gates."""

    paths, state, objects = _services()
    get_knowledge_source(_knowledge_sources(paths), source_id)
    _emit(KnowledgeDraftService(state, objects).audit(source_id))


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


@app.command("zhihu-import-replay")
def zhihu_import_replay(
    envelope_id: Annotated[str, typer.Argument(help="Registered response envelope id.")],
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
                    "participation_chain_count": len(
                        comment_execution.participation_chains
                    ),
                }
            )
            return
        replay = service.replay_listing(
            envelope_id,
            _knowledge_sources(paths),
            ParquetKnowledgeStore(paths.parquet),
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
) -> None:
    """Initialize an auditable Codex run directory and context budget."""

    paths, state, objects = _services()
    budget = build_context_budget(skills=skills or [], artifact_paths=artifacts or [])
    manifest = CodexRunService(paths.runtime, objects, state).initialize(
        {"request": request},
        context_budget=budget,
        input_manifest={"artifacts": budget.selected_artifacts},
    )
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
        _emit({"valid": False, "failure_class": exc.failure_class.value, "message": str(exc)})
        raise typer.Exit(code=2) from exc
    _emit(report)
    if not report.valid:
        raise typer.Exit(code=2)
