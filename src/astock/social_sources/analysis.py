"""Bounded investor-facing projection over immutable community search results.

This is an interpretation aid, not evidence of issuer facts or a trading signal.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC
from typing import Any

from astock.social_sources.models import (
    SocialLead,
    SocialQueryKind,
    SocialSearchResult,
)


def _lead_reference(lead: SocialLead) -> dict[str, Any]:
    return {
        "platform": lead.platform.value,
        "title": lead.title,
        "url": lead.url,
        "published_at": lead.published_at.astimezone(UTC).isoformat(),
        "published_precision": lead.published_precision,
        "published_raw": lead.published_raw,
        "author": lead.author,
        "source_snapshot_id": lead.snapshot_id,
        "sentiment_hint": lead.sentiment_hint,
        "keywords": list(lead.sentiment_terms),
        "industry_context": (
            lead.query_kinds == (SocialQueryKind.INDUSTRY,)
        ),
        "summary": lead.text[:350],
    }


def summarize_social_research(
    result: SocialSearchResult,
    *,
    max_items_per_group: int = 5,
) -> dict[str, Any]:
    """Create an auditable, content-bounded social layer for investment agents."""
    if not 1 <= max_items_per_group <= 20:
        raise ValueError("social report item limit must be 1..20")
    channels = [
        {
            "source": item.platform.value,
            "complete": item.complete,
            "query_count": item.query_count,
            "completed_queries": item.completed_query_count,
            "raw_items": item.raw_item_count,
            "accepted_items": item.accepted_count,
            "reason": item.reason,
            "checked_through": (
                item.checked_through.astimezone(UTC).isoformat()
                if item.checked_through is not None else None
            ),
        }
        for item in result.coverage
    ]
    groups: dict[str, list[SocialLead]] = {
        "positive_discussion": [],
        "negative_or_risk_discussion": [],
        "mixed_or_neutral_discussion": [],
        "industry_context": [],
    }
    titles: dict[str, list[SocialLead]] = defaultdict(list)
    for lead in result.leads:
        normalized_title = re.sub(r"[^\w\u4e00-\u9fff]+", "", lead.title).casefold()
        if normalized_title:
            titles[normalized_title].append(lead)
        if lead.query_kinds == (SocialQueryKind.INDUSTRY,):
            groups["industry_context"].append(lead)
        elif lead.sentiment_hint == "POSITIVE_HINT":
            groups["positive_discussion"].append(lead)
        elif lead.sentiment_hint == "NEGATIVE_HINT":
            groups["negative_or_risk_discussion"].append(lead)
        else:
            groups["mixed_or_neutral_discussion"].append(lead)
    duplicates = [
        {
            "title": matching[0].title,
            "source_count": len(matching),
            "links": sorted({item.url for item in matching}),
        }
        for matching in titles.values()
        if len({item.url for item in matching}) > 1
    ]
    duplicates.sort(key=lambda item: (-item["source_count"], item["title"]))
    projected = {}
    for key, candidates in groups.items():
        ordered = sorted(
            candidates,
            key=lambda item: (item.published_at, item.lead_id),
            reverse=True,
        )
        projected[key] = {
            "count": len(ordered),
            "items": [_lead_reference(item) for item in ordered[:max_items_per_group]],
        }
    return {
        "schema_version": "community-investment-sentiment-v1",
        "source_result_artifact_id": result.result_artifact_id,
        "social_receipt_id": result.receipt_id,
        "complete": result.complete,
        "channels": channels,
        "discussion_groups": projected,
        "repeated_title_clusters": duplicates[:max_items_per_group],
        "review_requirements": [
            "Separate online opinion from verified company events.",
            "Verify material allegations through formal primary evidence.",
            "Do not infer investment returns or trade actions from post counts.",
        ],
        "formal_investment_authority": False,
    }
