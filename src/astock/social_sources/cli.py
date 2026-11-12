"""Optional, source-backed social research commands for all investment workflows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import typer

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.social_sources.analysis import summarize_social_research
from astock.social_sources.config import load_social_search_config
from astock.social_sources.models import SocialSearchResult
from astock.social_sources.monitor import SocialWatchIngestionService
from astock.social_sources.provider import SocialBrowserSearchProvider


def register_social_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, StateStore, ObjectStore]],
    emit: Callable[[Any], None],
) -> None:
    """Use the canonical project runtime; do not create a second evidence store."""

    @app.command("social-source-status")
    def social_source_status() -> None:
        paths, _, _ = services()
        try:
            config = load_social_search_config(paths.root, create_local=False)
        except ValueError:
            emit({
                "schema_version": "social-source-status-v1",
                "configured": False,
                "extension_path": "browser_extension/social_capture",
                "source_status": "SETUP_REQUIRED",
            })
            return
        emit({
            "schema_version": "social-source-status-v1",
            "configured": config.enabled,
            "source_status": "READY_TO_CHECK" if config.enabled else "DISABLED",
            "bridge_host": config.bridge_host,
            "bridge_port": config.bridge_port,
            "extension_path": "browser_extension/social_capture",
            "secrets_returned": False,
        })

    @app.command("social-capture-token")
    def social_capture_token() -> None:
        """One-time local Chrome extension pairing; never write token to a report."""
        paths, _, _ = services()
        config = load_social_search_config(paths.root)
        typer.echo(config.bridge_key)

    @app.command("social-search")
    def social_search(
        symbol: Annotated[str, typer.Option(help="Six-digit A-share code.")] = "",
        name: Annotated[list[str] | None, typer.Option("--name")] = None,
        alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
        industry: Annotated[list[str] | None, typer.Option("--industry")] = None,
        management: Annotated[list[str] | None, typer.Option("--management")] = None,
        days: Annotated[int, typer.Option(min=1, max=7)] = 7,
    ) -> None:
        """Collect recent Xueqiu original posts and Zhihu non-video posts."""
        if not any((symbol.strip(), name, alias, industry, management)):
            raise typer.BadParameter("specify a stock or at least one research keyword")
        paths, state, objects = services()
        now = datetime.now(UTC)
        result = SocialBrowserSearchProvider(
            objects, state, paths.root
        ).search_checked(
            symbol=symbol,
            company_id=symbol,
            company_names=tuple(name or ()),
            aliases=tuple(alias or ()),
            industry_keywords=tuple(industry or ()),
            management_keywords=tuple(management or ()),
            start=now - timedelta(days=days),
            end=now,
        )
        watch_ingestion = SocialWatchIngestionService(
            paths.root, state, objects
        ).ingest(result)
        emit({
            "schema_version": "social-investor-discovery-v1",
            "result_artifact_id": result.result_artifact_id,
            "receipt_id": result.receipt_id,
            "watch_ingestion": watch_ingestion,
            "analysis": summarize_social_research(result),
            "complete": result.complete,
            "coverage": [item.model_dump(mode="json") for item in result.coverage],
            "leads": [
                {
                    "lead_id": item.lead_id,
                    "platform": item.platform.value,
                    "title": item.title,
                    "summary": item.text[:500],
                    "author": item.author,
                    "url": item.url,
                    "published_at": item.published_at.isoformat(),
                    "published_precision": item.published_precision,
                    "published_raw": item.published_raw,
                    "sentiment_hint": item.sentiment_hint,
                    "query_kinds": [kind.value for kind in item.query_kinds],
                    "source_snapshot_id": item.snapshot_id,
                }
                for item in result.leads
            ],
            "formal_investment_authority": False,
        })

    @app.command("social-search-show")
    def social_search_show(
        artifact_id: Annotated[str, typer.Argument()],
    ) -> None:
        """Replay a saved community result without touching the browser."""
        _, state, objects = services()
        record = state.artifact_record(artifact_id)
        if record is None or record["type"] != "SocialSearchResult":
            raise typer.BadParameter("unknown social search result")
        object_hash = str(record["object_hash"])
        if not objects.verify(object_hash):
            raise typer.BadParameter("social search result object hash invalid")
        result = SocialSearchResult.model_validate_json(objects.get_bytes(object_hash))
        if result.result_artifact_id != artifact_id:
            raise typer.BadParameter("social search result identity mismatch")
        emit({
            "result": result.model_dump(mode="json"),
            "analysis": summarize_social_research(result),
        })
