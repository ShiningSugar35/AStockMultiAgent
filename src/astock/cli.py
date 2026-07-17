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
from pydantic import BaseModel, ValidationError

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
    KnowledgeRepository,
    ParquetKnowledgeStore,
    ZhihuCollectionService,
    get_knowledge_source,
    load_knowledge_sources,
)
from astock.market_data.storage import (
    CanonicalMarketStore,
    ParquetMarketStore,
    canonical_manifest_path,
)
from astock.market_data.sync import MarketSyncService
from astock.paper_trading import LedgerService, PaperReplayService, load_fee_schedule
from astock.providers import EastMoney5mProvider, Sina5mProvider
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    DocumentType,
    FinancialAuditRequest,
    InstrumentType,
    KnowledgeSourceRegistry,
    Market,
    ZhihuContentType,
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
