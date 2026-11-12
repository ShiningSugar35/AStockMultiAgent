"""Route new community discovery leads into the existing watch-event ledger.

The collector does not own account, order or portfolio state. Ordinary opinion
posts remain observations; material claims only request evidence review.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.monitoring.config import load_continuous_monitor_config
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.schemas.adaptation import ResearchModule
from astock.schemas.continuous_monitoring import (
    ContinuousMonitorTarget,
    MonitorEvent,
    MonitorEventType,
    MonitorSeverity,
    MonitorSource,
    MonitorTargetStatus,
)
from astock.social_sources.models import (
    SocialQueryKind,
    SocialSearchResult,
)


class SocialWatchIngestionService:
    """Idempotent inbound events for watched securities, no new daemon."""

    def __init__(
        self, project_root: Path, state: StateStore, objects: ObjectStore
    ) -> None:
        self.repo = ContinuousMonitorRepository(state, objects)
        self.state = state
        self.objects = objects
        self.material_terms = load_continuous_monitor_config(
            project_root / "configs" / "continuous_monitor.yaml"
        ).news.material_keywords

    def ingest(
        self, result: SocialSearchResult, *,
        at: datetime | None = None,
    ) -> dict[str, int]:
        record = self.state.artifact_record(result.result_artifact_id)
        if record is None or record["type"] != "SocialSearchResult":
            raise ValueError("social search result is not registered")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("social search result hash failed verification")
        if SocialSearchResult.model_validate_json(
            self.objects.get_bytes(object_hash)
        ) != result:
            raise ValueError("social search result changed after capture")
        now = at or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("social event observation time must be timezone-aware")
        now = now.astimezone(UTC)
        events = 0
        tasks = 0
        skipped = 0
        watched: dict[str, ContinuousMonitorTarget | None] = {}
        for lead in result.leads:
            snapshot = self.state.get_snapshot(lead.snapshot_id)
            if snapshot is None or not self.objects.verify(snapshot.object_sha256):
                raise ValueError("community lead has no valid source snapshot")
            if not lead.symbol:
                skipped += 1
                continue
            if lead.symbol not in watched:
                target = None
                for market in ("XSHE", "XSHG", "BJSE"):
                    candidate = self.repo.get_target(
                        f"monitor-target:{market}:{lead.symbol}"
                    )
                    if candidate is not None and candidate.status is MonitorTargetStatus.ACTIVE:
                        target = candidate
                        break
                watched[lead.symbol] = target
            target = watched[lead.symbol]
            if target is None:
                skipped += 1
                continue

            # Context-only industry discussion never becomes an issuer allegation.
            company_specific = any(
                kind in {SocialQueryKind.COMPANY, SocialQueryKind.SYMBOL,
                         SocialQueryKind.MANAGEMENT}
                for kind in lead.query_kinds
            )
            material = company_specific and any(
                term in f"{lead.title} {lead.text}".casefold()
                for term in self.material_terms
            )
            modules = (
                sorted(
                    {ResearchModule.EVIDENCE, ResearchModule.BASE_CASE,
                     ResearchModule.COMMITTEE},
                    key=lambda item: item.value,
                )
                if material else []
            )
            payload: dict[str, object] = {
                "title": lead.title,
                "url": lead.url,
                "platform": lead.platform.value,
                "author": lead.author,
                "published_at": lead.published_at.isoformat(),
                "published_precision": lead.published_precision,
                "published_raw": lead.published_raw,
                "snapshot_id": lead.snapshot_id,
                "lead_id": lead.lead_id,
                "lead_only": True,
                "sentiment_hint": lead.sentiment_hint,
                "query_kinds": [kind.value for kind in lead.query_kinds],
            }
            dedupe_key = content_hash({
                "target_id": target.target_id,
                "source": MonitorSource.COMMUNITY.value,
                "lead_id": lead.lead_id,
            })
            event = MonitorEvent(
                event_id="monitor-event:" + dedupe_key,
                target_id=target.target_id,
                company_id=target.company_id,
                event_type=MonitorEventType.NEWS_LEAD,
                severity=MonitorSeverity.MATERIAL if material else MonitorSeverity.WATCH,
                observed_at=min(now, lead.published_at.astimezone(UTC)),
                available_at=now,
                source=MonitorSource.COMMUNITY,
                source_ref=lead.lead_id,
                payload=payload,
                payload_hash=content_hash(payload),
                dedupe_key=dedupe_key,
                affected_modules=modules,
                requires_research=material,
                news_lead_only=True,
            )
            stored, inserted = self.repo.record_event(event)
            events += int(inserted)
            _, created = self.repo.ensure_task(stored)
            tasks += int(created)
        return {
            "inserted_events": events,
            "inserted_research_tasks": tasks,
            "not_watched": skipped,
        }
