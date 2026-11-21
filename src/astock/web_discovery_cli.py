"""CLI surface for the offline Web discovery lead capability.

Both commands are explicit, human/Agent-invoked entry points. They never start
a background task, never write to SQLite/ObjectStore, and never perform network
I/O. Search results, when supplied, are external data and stay DISCOVERY_ONLY.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from astock.research.web_discovery import (
    EXTERNAL_DATA_NOTICE,
    admit_proposals,
    build_lead,
    build_search_links,
    load_official_domains,
    load_web_discovery_config,
)
from astock.schemas.web_discovery import (
    WebDiscoveryAdmission,
    WebDiscoveryInput,
    WebDiscoveryLead,
)


def _fail(emit: Callable[[Any], None], reason_code: str, detail: str) -> None:
    emit({"status": "FAILED", "reason_code": reason_code, "detail": detail})
    raise typer.Exit(code=2)


def register_web_discovery_commands(
    app: typer.Typer,
    emit: Callable[[Any], None],
) -> None:
    @app.command("web-discovery-schema")
    def web_discovery_schema() -> None:
        emit(
            {
                "input": WebDiscoveryInput.model_json_schema(),
                "lead": WebDiscoveryLead.model_json_schema(),
                "admission": WebDiscoveryAdmission.model_json_schema(),
            }
        )

    @app.command("web-discovery-config")
    def web_discovery_config() -> None:
        config = load_web_discovery_config()
        emit(
            {
                "config": config.model_dump(mode="json"),
                "automatic_route_wired": False,
                "network_requests": 0,
                "completeness": "DISCOVERY_ONLY",
            }
        )

    @app.command("web-discovery-links")
    def web_discovery_links(
        query: Annotated[str, typer.Argument()],
        capability: Annotated[
            str | None,
            typer.Option("--capability", help="Capability from authority_domains.yaml"),
        ] = None,
        official_only: Annotated[bool, typer.Option("--official-only/--all")] = False,
    ) -> None:
        config = load_web_discovery_config()
        effective_capability = capability or config.default_capability
        try:
            domains = load_official_domains(effective_capability)
            links = build_search_links(
                query,
                config=config,
                official_domains=domains,
                official_only=official_only,
            )
        except ValueError as exc:
            _fail(emit, "INVALID_WEB_DISCOVERY_QUERY", str(exc))
            return
        emit(
            {
                "query": query,
                "capability": effective_capability,
                "official_domains": list(domains),
                "link_count": len(links),
                "network_requests": 0,
                "completeness": "DISCOVERY_ONLY",
                "links": [link.model_dump(mode="json") for link in links],
                "notice": EXTERNAL_DATA_NOTICE,
            }
        )

    @app.command("web-discovery-admit")
    def web_discovery_admit(
        input_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        config = load_web_discovery_config()
        try:
            with input_file.open("rb") as handle:
                raw = handle.read(config.max_input_bytes + 1)
            if len(raw) > config.max_input_bytes:
                _fail(emit, "WEB_DISCOVERY_INPUT_TOO_LARGE", "input exceeds configured byte budget")
                return
            payload = WebDiscoveryInput.model_validate_json(raw)
        except (OSError, UnicodeError, ValidationError) as exc:
            _fail(emit, "INVALID_WEB_DISCOVERY_INPUT", str(exc))
            return
        if len(payload.results) > config.max_input_results():
            _fail(
                emit,
                "TOO_MANY_RESULTS",
                f"result count exceeds {config.max_input_results()}",
            )
            return
        try:
            domains = load_official_domains(payload.requested_capability)
            links = build_search_links(payload.query, config=config, official_domains=domains)
            lead = build_lead(payload, config=config, official_domains=domains, links=links)
            admission = admit_proposals(lead)
        except (ValueError, ValidationError) as exc:
            _fail(emit, "WEB_DISCOVERY_REJECTED", str(exc))
            return
        # Downstream consumers must use this filtered view, never lead.results:
        # rejected candidates stay in the lead for audit but never in admission output.
        allowed = set(admission.discovery_allowed_urls)
        emit(
            {
                "lead": lead.model_dump(mode="json"),
                "admission": admission.model_dump(mode="json"),
                "allowed_lead_results": [
                    result.model_dump(mode="json")
                    for result in lead.results
                    if str(result.canonical_url) in allowed
                ],
                "automatic_route_wired": False,
                "network_requests": 0,
                "notice": EXTERNAL_DATA_NOTICE,
            }
        )


__all__ = ["register_web_discovery_commands"]
