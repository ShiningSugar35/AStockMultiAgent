"""Explicit minimal disclosure boundary for scheduled semantic analysis.

Account identifiers, quantities, costs, cash, orders and whole database records
are never part of this context. Missing optional facts stay unavailable rather
than being invented or filled from an unrelated account.
"""

from __future__ import annotations

from datetime import datetime

from astock.investor_orchestration.canonical_state import normalized_instrument
from astock.investor_orchestration.models import (
    InvestorSessionPreflightReceipt,
    MaterialEventView,
    ScheduledDomain,
    ScheduledResearchPolicy,
    ScheduledWindow,
    StrictModel,
)
from astock.research.presentation import audit_public_answer


class ScheduledEventContext(StrictModel):
    material_events: tuple[MaterialEventView, ...] = ()


class ScheduledAnalysisContext(StrictModel):
    as_of: datetime
    context: ScheduledEventContext
    unavailable_fields: tuple[str, ...] = ()


def minimal_analysis_context(
    preflight: InvestorSessionPreflightReceipt,
    policy: ScheduledResearchPolicy,
    *,
    domain: ScheduledDomain,
    instrument_id: str,
    window: ScheduledWindow,
) -> ScheduledAnalysisContext:
    """Project an allowlisted per-subject context without exposing either ledger."""
    allowed = set(policy.field_allowlist)
    supported = {
        "domain",
        "instrument_id",
        "anonymous_account_lane",
        "exposure_ratio",
        "material_event",
        "price_change_band",
        "thesis_delta",
        "recommended_review_action",
        "evidence_as_of",
    }
    if not allowed <= supported:
        raise ValueError("scheduled disclosure requests unsupported private fields")
    if not {"domain", "instrument_id", "evidence_as_of"} <= allowed:
        raise ValueError("scheduled analysis requires explicit identity/time disclosure consent")
    if domain not in policy.domains or window not in policy.windows:
        raise ValueError("scheduled analysis is outside the consented domain or window")
    target = normalized_instrument(instrument_id)
    events = []
    if "material_event" in allowed:
        for event in preflight.context.material_events:
            if event.available_at > preflight.as_of:
                raise ValueError("a future monitor event reached the disclosure boundary")
            if (
                event.instrument_id is not None
                and normalized_instrument(event.instrument_id) != target
            ):
                continue
            if not audit_public_answer(event.summary).safe_to_send:
                raise ValueError("monitor summary failed the public disclosure audit")
            events.append(event.model_copy(deep=True))
    return ScheduledAnalysisContext(
        as_of=preflight.as_of,
        context=ScheduledEventContext(material_events=tuple(events)),
        unavailable_fields=tuple(
            sorted(
                allowed
                & {
                    "exposure_ratio",
                    "price_change_band",
                    "thesis_delta",
                    "recommended_review_action",
                }
            )
        ),
    )
