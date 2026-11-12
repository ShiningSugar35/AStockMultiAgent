"""Optional live social research handler for the existing investor capability graph.

Only active on a paired browser installation. Registered/replayed execution
continues to use immutable supplied artifacts and never initiates collection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from astock.core.errors import AStockError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.capabilities import CapabilityExecutionResult
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.social_sources.config import load_social_search_config
from astock.social_sources.provider import SocialBrowserSearchProvider


def _terms(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: tuple[object, ...] = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(value)
    else:
        return ()
    return tuple(dict.fromkeys(
        text for item in values
        if isinstance(item, str)
        if (text := " ".join(item.split()).strip()) and len(text) <= 120
    ))[:8]


def paired_social_handler(
    store: InvestorOrchestrationStore,
    project_root: Path,
):
    """Return no handler if setup is incomplete, without writing any credentials."""
    try:
        config = load_social_search_config(project_root, create_local=False)
    except (ValueError, OSError):
        return None
    if not config.enabled:
        return None
    state = StateStore(store.path)
    objects = ObjectStore(store.path.parent / "objects" / "sha256")

    def collect(
        request: InvestorRequestEnvelope,
        _preflight: InvestorSessionPreflightReceipt,
    ) -> CapabilityExecutionResult:
        # Broad recommendations discover candidates first; social collection runs
        # when named subjects have been identified, not for an entire universe.
        subjects = tuple(dict.fromkeys(
            entity.split(":", 1)[1]
            for entity in request.entity_ids
            if isinstance(entity, str)
            and ":" in entity
            and entity.split(":", 1)[0] in {"XSHG", "XSHE", "BJSE"}
            and len(entity.split(":", 1)[1]) == 6
            and entity.split(":", 1)[1].isascii()
            and entity.split(":", 1)[1].isdigit()
        ))
        if not subjects:
            # No named issuer is normal for market-wide discovery and account
            # queries. An optional research input cannot degrade a formally
            # certified request just because no subject has been selected yet.
            return CapabilityExecutionResult()
        # Keep community discovery supplemental and bounded: longer portfolio
        # requests continue through the scheduled per-subject watch lane.
        deadline = monotonic() + 90.0
        metadata: dict[str, Any] = request.metadata
        names = _terms(metadata.get("social_company_names"))
        aliases = _terms(metadata.get("social_aliases"))
        industries = _terms(metadata.get("social_industry_keywords"))
        management = _terms(metadata.get("social_management_keywords"))
        ids: list[str] = []
        failures = 0
        targets = ContinuousMonitorRepository(state, objects)
        for symbol in subjects[:2]:
            remaining = deadline - monotonic()
            if remaining < 10:
                failures += 1
                break
            try:
                now = datetime.now(UTC)
                # Reuse the canonical watch identity when available rather
                # than relying only on a six-digit search or guessing names.
                instrument = next(
                    (identity for identity in request.entity_ids
                     if identity.endswith(":" + symbol)), ""
                )
                cached_name: tuple[str, ...] = ()
                cached_aliases: tuple[str, ...] = ()
                if instrument:
                    watched = targets.get_target(
                        "monitor-target:" + instrument
                    )
                    if watched is not None and watched.display_name != symbol:
                        cached_name = (watched.display_name,)
                        cached_aliases = tuple(watched.aliases)
                result = SocialBrowserSearchProvider(
                    objects, state, project_root,
                    timeout_seconds=min(45.0, remaining),
                ).search_checked(
                    symbol=symbol,
                    company_id=symbol,
                    company_names=cached_name or (
                        names if len(subjects) == 1 else ()
                    ),
                    aliases=cached_aliases or (
                        aliases if len(subjects) == 1 else ()
                    ),
                    industry_keywords=industries if len(subjects) == 1 else (),
                    management_keywords=management if len(subjects) == 1 else (),
                    start=now - timedelta(days=7),
                    end=now,
                )
                ids.append(result.result_artifact_id)
                # The separate watch-ingestion path reuses the same immutable
                # result and will not create an order or a second event store.
                from astock.social_sources.monitor import SocialWatchIngestionService

                SocialWatchIngestionService(project_root, state, objects).ingest(result)
                if not result.complete:
                    failures += 1
            except (AStockError, OSError, ValueError, RuntimeError):
                failures += 1
        # An optional source outage or a bounded subject budget is not a
        # failure of formally required investment capabilities. Valid partial
        # SocialSearchResult artifacts carry their own source coverage receipts;
        # if nothing was captured the optional capability is simply SKIPPED.
        return CapabilityExecutionResult(artifact_ids=tuple(ids))

    return collect
