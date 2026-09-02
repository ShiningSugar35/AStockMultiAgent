"""Event-driven continuous investment monitoring service."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from astock.core.errors import PublicErrorMapper
from astock.core.hashing import content_hash
from astock.core.logging import emit_operational_event
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.cninfo import CninfoDisclosureProvider
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.market_data.storage import CanonicalMarketStore, ParquetMarketStore
from astock.market_data.sync import MarketSyncService
from astock.monitoring.config import ContinuousMonitorConfig
from astock.monitoring.news import GdeltNewsLeadProvider
from astock.monitoring.repository import ContinuousMonitorRepository
from astock.paper_trading.ledger import LedgerService
from astock.paper_trading.operation import MarketReferencePaperVerifier
from astock.paper_trading.replay import PaperReplayService, load_fee_schedule
from astock.providers.config import load_provider_registry
from astock.providers.dialects import load_provider_dialects
from astock.providers.eastmoney import EastMoney5mProvider
from astock.providers.runtime import ProviderFactory, load_transport_profiles
from astock.providers.sina import Sina5mProvider
from astock.schemas import (
    AdjustmentMode,
    BarRequest,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    Frequency,
    InstrumentType,
    Market,
    MarketBar,
    OperationalSeverity,
)
from astock.schemas.adaptation import ResearchModule
from astock.schemas.continuous_monitoring import (
    ContinuousMonitorTarget,
    MonitorComparison,
    MonitorEvent,
    MonitorEventType,
    MonitorMetric,
    MonitorRule,
    MonitorRunReport,
    MonitorRunStatus,
    MonitorSeverity,
    MonitorSource,
    MonitorTargetEnrollRequest,
    MonitorTargetReason,
)
from astock.schemas.research_production import (
    CatalystRecord,
    CatalystStatus,
)
from astock.schemas.research_production import (
    ResearchModule as ProductionResearchModule,
)
from astock.settings import ProjectPaths

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MATERIAL_DISCLOSURE_TERMS = (
    "年度报告",
    "半年度报告",
    "季度报告",
    "业绩预告",
    "业绩快报",
    "重大合同",
    "中标",
    "收购",
    "重组",
    "并购",
    "停产",
    "事故",
    "立案",
    "调查",
    "处罚",
    "减持",
    "回购",
    "诉讼",
    "退市",
    "停牌",
)


class ContinuousMonitorService:
    def __init__(
        self,
        paths: ProjectPaths,
        state: StateStore,
        objects: ObjectStore,
        config: ContinuousMonitorConfig,
        *,
        repository: ContinuousMonitorRepository | None = None,
        market_sync_factory: Callable[[], MarketSyncService] | None = None,
        disclosure_provider: CninfoDisclosureProvider | None = None,
        news_provider: GdeltNewsLeadProvider | None = None,
    ) -> None:
        self.paths = paths
        self.state = state
        self.objects = objects
        self.config = config
        self.repo = repository or ContinuousMonitorRepository(state, objects)
        self.canonical = CanonicalMarketStore(paths.parquet, paths.manifests)
        self._market_sync_factory = market_sync_factory
        self.disclosures = disclosure_provider or CninfoDisclosureProvider(objects, state)
        self._provider_factory: ProviderFactory | None = None
        if news_provider is not None:
            self.news = news_provider
        else:
            if config.news.endpoint != GdeltNewsLeadProvider.default_endpoint:
                raise ValueError(
                    "continuous monitor GDELT endpoint diverges from Provider Registry adapter"
                )
            self._provider_factory = ProviderFactory(
                load_provider_registry(paths.root / "configs" / "provider_registry.yaml"),
                load_transport_profiles(paths.root / "configs" / "transport_profiles.yaml"),
                objects,
                state,
                paths.root / "tests" / "fixtures",
                dialects=load_provider_dialects(paths.root / "configs" / "provider_dialects.yaml"),
            )
            self.news = self._provider_factory.create_for_capability(
                GdeltNewsLeadProvider.capability,
                GdeltNewsLeadProvider,
            )

    def enroll(
        self,
        *,
        symbol: str,
        market: Market,
        company_id: str,
        display_name: str,
        reason: MonitorTargetReason,
        aliases: list[str] | None = None,
        at: datetime | None = None,
    ) -> ContinuousMonitorTarget:
        return self.repo.enroll(
            MonitorTargetEnrollRequest(
                symbol=symbol,
                market=market,
                company_id=company_id,
                display_name=display_name,
                reason=reason,
                aliases=aliases or [],
                created_at=(at or datetime.now(UTC)),
            )
        )

    def sync_paper_targets(self, *, at: datetime | None = None) -> list[str]:
        """Auto-enrol open paper positions/orders without creating trades."""

        now = at or datetime.now(UTC)
        enrolled: list[str] = []
        with closing(self.state.connect()) as connection:
            position_rows = connection.execute(
                "SELECT p.symbol,i.market FROM position p "
                "JOIN paper_position_identity i ON i.account_id=p.account_id AND i.symbol=p.symbol "
                "WHERE p.qty_total>0 ORDER BY i.market,p.symbol"
            ).fetchall()
            order_rows = connection.execute(
                "SELECT DISTINCT o.symbol,COALESCE(i.market,'') AS market FROM order_record o "
                "LEFT JOIN paper_position_identity i ON i.account_id=o.account_id AND i.symbol=o.symbol "  # noqa: E501
                "WHERE o.status IN ('NEW','ACCEPTED','PARTIALLY_FILLED') ORDER BY o.symbol"
            ).fetchall()
        for row in position_rows:
            market = _market_from_text(str(row["market"]))
            if market is None:
                continue
            target = self.enroll(
                symbol=str(row["symbol"]),
                market=market,
                company_id=str(row["symbol"]),
                display_name=str(row["symbol"]),
                reason=MonitorTargetReason.PAPER_POSITION,
                at=now,
            )
            enrolled.append(target.target_id)
        for row in order_rows:
            market = _market_from_text(str(row["market"])) or _infer_market(str(row["symbol"]))
            target = self.enroll(
                symbol=str(row["symbol"]),
                market=market,
                company_id=str(row["symbol"]),
                display_name=str(row["symbol"]),
                reason=MonitorTargetReason.OPEN_PAPER_ORDER,
                at=now,
            )
            enrolled.append(target.target_id)
        return sorted(set(enrolled))

    def run_cycle(
        self,
        *,
        owner_id: str,
        live: bool,
        now: datetime | None = None,
    ) -> MonitorRunReport:
        started = (now or datetime.now(UTC)).astimezone(UTC)
        findings: list[str] = []
        success: Counter[MonitorSource] = Counter()
        failure: Counter[MonitorSource] = Counter()
        event_count = 0
        task_count = 0
        try:
            self.sync_paper_targets(at=started)
        except Exception as exc:
            findings.append(f"PAPER_TARGET_SYNC_FAILED:{type(exc).__name__}")
        targets = self.repo.active_targets(limit=self.config.max_targets_per_cycle)
        for target in targets:
            for source, handler in (
                (MonitorSource.MARKET_60M, self._process_market),
                (MonitorSource.CNINFO, self._process_disclosures),
                (MonitorSource.GDELT, self._process_news),
                (MonitorSource.CATALYST, self._process_catalysts),
                (MonitorSource.SCHEDULE, self._process_schedule),
                (MonitorSource.PAPER, self._process_paper),
            ):
                if not self.repo.source_due(
                    target.target_id,
                    source,
                    at=started,
                    cadence=self.config.cadence(source),
                ):
                    continue
                if not live and source in {MonitorSource.CNINFO, MonitorSource.GDELT}:
                    continue
                try:
                    events, tasks, cursor = handler(target, started, live)
                    event_count += events
                    task_count += tasks
                    self.repo.source_success(
                        target.target_id,
                        source,
                        cursor=cursor,
                        at=started,
                    )
                    success[source] += 1
                except Exception as exc:
                    failure[source] += 1
                    count = self.repo.source_failure(
                        target.target_id,
                        source,
                        at=started,
                        retry_backoff_seconds=self.config.retry_backoff_seconds,
                    )
                    findings.append(
                        f"{target.target_id}:{source.value}:{type(exc).__name__}:attempt={count}"
                    )
                    PublicErrorMapper.record(
                        exc,
                        component="continuous_monitor",
                        event="monitor_source_failure",
                        context={
                            "target_id": target.target_id,
                            "source": source.value,
                            "failure_count": count,
                        },
                    )
                    degraded = self._event(
                        target,
                        event_type=MonitorEventType.DATA_SOURCE_DEGRADED,
                        severity=MonitorSeverity.WATCH,
                        observed_at=started,
                        available_at=started,
                        source=source,
                        source_ref=type(exc).__name__,
                        payload={"failure_class": type(exc).__name__, "failure_count": count},
                        affected_modules=[],
                        requires_research=False,
                        dedupe_suffix=f"{source.value}:{count}:{started.date().isoformat()}",
                    )
                    _, inserted = self.repo.record_event(degraded)
                    event_count += int(inserted)
        ended = datetime.now(UTC)
        run_status = (
            MonitorRunStatus.SUCCEEDED
            if not failure
            else MonitorRunStatus.PARTIAL
            if success
            else MonitorRunStatus.FAILED
        )
        report = MonitorRunReport(
            run_id="monitor-run:"
            + content_hash(
                {
                    "owner": owner_id,
                    "started": started.isoformat(),
                    "ended": ended.isoformat(),
                    "live": live,
                    "targets": [item.target_id for item in targets],
                    "success": {key.value: value for key, value in success.items()},
                    "failure": {key.value: value for key, value in failure.items()},
                }
            ),
            owner_id=owner_id,
            started_at=started,
            ended_at=ended,
            status=run_status,
            live=live,
            target_count=len(targets),
            event_count=event_count,
            task_count=task_count,
            source_success=dict(success),
            source_failure=dict(failure),
            findings=findings,
        )
        self.repo.record_run(report)
        emit_operational_event(
            component="continuous_monitor",
            event="monitor_cycle_completed",
            severity=(
                OperationalSeverity.INFO
                if run_status is MonitorRunStatus.SUCCEEDED
                else OperationalSeverity.WARNING
            ),
            run_id=report.run_id,
            context={
                "live": live,
                "status": run_status.value,
                "target_count": len(targets),
                "event_count": event_count,
                "task_count": task_count,
                "source_success": {key.value: value for key, value in success.items()},
                "source_failure": {key.value: value for key, value in failure.items()},
            },
        )
        return report

    def _process_market(
        self,
        target: ContinuousMonitorTarget,
        now: datetime,
        live: bool,
    ) -> tuple[int, int, str | None]:
        request = self._bar_request(target, now)
        if live:
            self._market_sync().sync_intraday(request)
        bars = [
            item
            for item in self.canonical.read_bars(request)
            if item.timestamp <= now.astimezone(_SHANGHAI)
        ]
        if not bars:
            raise ValueError("no canonical 60m market bars available")
        bars.sort(key=lambda item: item.timestamp)
        latest = bars[-1]
        cursor_row = self.repo.cursor(target.target_id, MonitorSource.MARKET_60M)
        prior_cursor = str(cursor_row["cursor"]) if cursor_row and cursor_row["cursor"] else None
        events = tasks = 0
        latest_price = float(latest.close)
        pre_update_target = target
        self.repo.update_market_state(
            target.target_id,
            last_price=latest_price,
            observed_at=latest.timestamp,
        )
        if prior_cursor != latest.timestamp.astimezone(UTC).isoformat():
            event = self._event(
                target,
                event_type=MonitorEventType.PRICE_BAR_UPDATED,
                severity=MonitorSeverity.INFO,
                observed_at=latest.timestamp,
                available_at=now,
                source=MonitorSource.MARKET_60M,
                source_ref=latest.observation_id,
                payload=_bar_payload(latest),
                affected_modules=[],
                requires_research=False,
                dedupe_suffix=latest.observation_id,
            )
            _, inserted = self.repo.record_event(event)
            events += int(inserted)
        metrics = calculate_monitor_metrics(
            bars,
            high_watermark=pre_update_target.high_watermark_price or latest_price,
            last_review_at=pre_update_target.last_review_at,
            now=now,
            one_day_bars=self.config.market.one_day_bars,
            five_day_bars=self.config.market.five_day_bars,
            volume_ratio_window=self.config.market.volume_ratio_window,
        )
        for rule in self.repo.rules(target.target_id):
            value = metrics.get(rule.metric)
            if value is None or not rule_triggered(rule, value=value, now=now):
                continue
            event_type = (
                MonitorEventType.DRAWDOWN_TRIGGER
                if rule.metric is MonitorMetric.DRAWDOWN_FROM_WATCH_HIGH
                else MonitorEventType.PRICE_TRIGGER
            )
            event = self._event(
                target,
                event_type=event_type,
                severity=rule.severity,
                observed_at=latest.timestamp,
                available_at=now,
                source=MonitorSource.MARKET_60M,
                source_ref=rule.rule_id,
                payload={
                    "rule_id": rule.rule_id,
                    "metric": rule.metric.value,
                    "value": value,
                    "comparison": rule.comparison.value,
                    "threshold": rule.threshold,
                    "action": rule.action.value,
                },
                affected_modules=rule.affected_modules,
                requires_research=bool(rule.affected_modules),
                dedupe_suffix=f"{rule.rule_id}:{latest.timestamp.astimezone(UTC).isoformat()}",
            )
            stored, inserted = self.repo.record_event(event)
            events += int(inserted)
            if inserted:
                self.repo.mark_rule_triggered(rule.rule_id, at=now)
                _, task_inserted = self.repo.ensure_task(stored)
                tasks += int(task_inserted)
        return events, tasks, latest.timestamp.astimezone(UTC).isoformat()

    def _process_disclosures(
        self,
        target: ContinuousMonitorTarget,
        now: datetime,
        live: bool,
    ) -> tuple[int, int, str | None]:
        del live
        exchange = _disclosure_exchange(target.market)
        if exchange is None:
            return 0, 0, now.isoformat()
        cursor = self.repo.cursor(target.target_id, MonitorSource.CNINFO)
        cursor_dt = _cursor_datetime(cursor)
        start = (
            (cursor_dt - timedelta(days=1)).date()
            if cursor_dt
            else (now - timedelta(days=2)).date()
        )
        batch = self.disclosures.search(
            DisclosureSearchRequest(
                symbol=target.symbol,
                exchange=exchange,
                start_date=start,
                end_date=now.astimezone(_SHANGHAI).date(),
                category=DisclosureCategory.ALL,
                keyword="",
                page_number=1,
                page_size=100,
            )
        )
        events = tasks = 0
        latest = cursor_dt
        for item in sorted(batch.announcements, key=lambda value: value.published_at):
            latest = max(latest, item.published_at) if latest else item.published_at
            material = _contains_material_term(item.title, _MATERIAL_DISCLOSURE_TERMS)
            modules = _disclosure_modules(item.title)
            event = self._event(
                target,
                event_type=MonitorEventType.OFFICIAL_DISCLOSURE,
                severity=MonitorSeverity.MATERIAL if material else MonitorSeverity.WATCH,
                observed_at=min(item.published_at, now),
                available_at=now,
                source=MonitorSource.CNINFO,
                source_ref=item.announcement_id,
                payload={
                    "announcement_id": item.announcement_id,
                    "title": item.title,
                    "source_url": item.source_url,
                    "raw_snapshot_id": batch.raw_snapshot_id,
                },
                affected_modules=modules,
                requires_research=material,
                dedupe_suffix=item.announcement_id,
            )
            stored, inserted = self.repo.record_event(event)
            events += int(inserted)
            if inserted:
                _, task_inserted = self.repo.ensure_task(stored)
                tasks += int(task_inserted)
        return events, tasks, (latest.astimezone(UTC).isoformat() if latest else now.isoformat())

    def _process_news(
        self,
        target: ContinuousMonitorTarget,
        now: datetime,
        live: bool,
    ) -> tuple[int, int, str | None]:
        del live
        cursor = self.repo.cursor(target.target_id, MonitorSource.GDELT)
        cursor_dt = _cursor_datetime(cursor)
        start = cursor_dt or now - timedelta(minutes=self.config.news.lookback_minutes)
        leads = self.news.search(
            names=[target.display_name, *target.aliases],
            symbol=target.symbol,
            start=start,
            end=now,
            max_records=self.config.news.max_records_per_target,
        )
        events = tasks = 0
        latest = cursor_dt
        for lead in sorted(leads, key=lambda item: item.seen_at):
            latest = max(latest, lead.seen_at) if latest else lead.seen_at
            material = _contains_material_term(lead.title, self.config.news.material_keywords)
            modules = (
                sorted(
                    {ResearchModule.EVIDENCE, ResearchModule.BASE_CASE, ResearchModule.COMMITTEE},
                    key=lambda item: item.value,
                )
                if material
                else []
            )
            event = self._event(
                target,
                event_type=MonitorEventType.NEWS_LEAD,
                severity=MonitorSeverity.MATERIAL if material else MonitorSeverity.WATCH,
                observed_at=min(lead.seen_at, now),
                available_at=now,
                source=MonitorSource.GDELT,
                source_ref=lead.lead_id,
                payload={
                    "title": lead.title,
                    "url": lead.url,
                    "domain": lead.domain,
                    "language": lead.language or "",
                    "source_country": lead.source_country or "",
                    "snapshot_id": lead.snapshot_id,
                    "lead_only": True,
                },
                affected_modules=modules,
                requires_research=material,
                dedupe_suffix=lead.lead_id,
                news_lead_only=True,
            )
            stored, inserted = self.repo.record_event(event)
            events += int(inserted)
            if inserted:
                _, task_inserted = self.repo.ensure_task(stored)
                tasks += int(task_inserted)
        return events, tasks, (latest.astimezone(UTC).isoformat() if latest else now.isoformat())

    def _process_catalysts(
        self,
        target: ContinuousMonitorTarget,
        now: datetime,
        live: bool,
    ) -> tuple[int, int, str | None]:
        del live
        events = tasks = 0
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT catalyst_id,object_hash,expected_from,expected_to,status "
                "FROM catalyst_registry_index WHERE company_id=? "
                "AND status NOT IN ('CLOSED','INVALIDATED') ORDER BY expected_from,catalyst_id",
                (target.company_id,),
            ).fetchall()
        for row in rows:
            catalyst = CatalystRecord.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            if catalyst.status in {CatalystStatus.CLOSED, CatalystStatus.INVALIDATED}:
                continue
            phase = None
            severity = MonitorSeverity.WATCH
            if now >= catalyst.expected_to:
                phase = "OVERDUE"
                severity = MonitorSeverity.MATERIAL
            elif catalyst.expected_from <= now <= catalyst.expected_to:
                phase = "WINDOW_OPEN"
            if phase is None:
                continue
            event = self._event(
                target,
                event_type=MonitorEventType.CATALYST_DUE,
                severity=severity,
                observed_at=now,
                available_at=now,
                source=MonitorSource.CATALYST,
                source_ref=catalyst.catalyst_id,
                payload={
                    "catalyst_id": catalyst.catalyst_id,
                    "phase": phase,
                    "expected_from": catalyst.expected_from.isoformat(),
                    "expected_to": catalyst.expected_to.isoformat(),
                    "kpi_ids": [item.kpi_id for item in catalyst.kpi_rules],
                },
                affected_modules=_catalyst_research_modules(catalyst.affected_modules),
                requires_research=True,
                dedupe_suffix=f"{catalyst.catalyst_id}:{phase}",
            )
            stored, inserted = self.repo.record_event(event)
            events += int(inserted)
            if inserted:
                _, task_inserted = self.repo.ensure_task(stored)
                tasks += int(task_inserted)
        return events, tasks, now.isoformat()

    def _process_schedule(
        self,
        target: ContinuousMonitorTarget,
        now: datetime,
        live: bool,
    ) -> tuple[int, int, str | None]:
        del live
        baseline = target.last_review_at or target.enrolled_at
        due = baseline + timedelta(days=self.config.scheduled_review_days)
        if now < due:
            return 0, 0, due.isoformat()
        modules = sorted(
            {
                ResearchModule.EVIDENCE,
                ResearchModule.FUNDAMENTAL_MODEL,
                ResearchModule.BASE_CASE,
                ResearchModule.SPECIALISTS,
                ResearchModule.COMMITTEE,
            },
            key=lambda item: item.value,
        )
        event = self._event(
            target,
            event_type=MonitorEventType.SCHEDULED_REVIEW_DUE,
            severity=MonitorSeverity.WATCH,
            observed_at=due,
            available_at=now,
            source=MonitorSource.SCHEDULE,
            source_ref=None,
            payload={"last_review_at": baseline.isoformat(), "due_at": due.isoformat()},
            affected_modules=modules,
            requires_research=True,
            dedupe_suffix=due.date().isoformat(),
        )
        stored, inserted = self.repo.record_event(event)
        task_inserted = False
        if inserted:
            _, task_inserted = self.repo.ensure_task(stored)
        return int(inserted), int(task_inserted), due.isoformat()

    def _process_paper(
        self,
        target: ContinuousMonitorTarget,
        now: datetime,
        live: bool,
    ) -> tuple[int, int, str | None]:
        """Replay confirmed open paper orders without creating new orders or positions."""

        del live
        if MonitorTargetReason.OPEN_PAPER_ORDER not in target.reasons:
            return 0, 0, now.isoformat()
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT account_id FROM order_record WHERE symbol=? "
                "AND status IN ('NEW','ACCEPTED','PARTIALLY_FILLED') ORDER BY account_id",
                (target.symbol,),
            ).fetchall()
        if not rows:
            return 0, 0, now.isoformat()
        references = MarketReferenceService(
            self.state,
            self.objects,
            ReferenceParquetStore(self.paths.parquet),
            self.paths.root / "tests" / "fixtures" / "reference",
        )
        replay = PaperReplayService(
            LedgerService(self.state, self.objects),
            self.canonical,
            MarketReferencePaperVerifier(references),
        )
        fees = load_fee_schedule(self.paths.root / "configs" / "fee_rules.yaml")
        request = self._bar_request(target, now)
        events = tasks = 0
        for row in rows:
            report = replay.replay(
                account_id=str(row["account_id"]),
                request=request,
                requested_cursor=now.astimezone(_SHANGHAI),
                fee_schedule=fees,
            )
            event = self._event(
                target,
                event_type=MonitorEventType.PAPER_REPLAY_DUE,
                severity=MonitorSeverity.MATERIAL if report.fill_ids else MonitorSeverity.INFO,
                observed_at=now,
                available_at=now,
                source=MonitorSource.PAPER,
                source_ref=str(row["account_id"]),
                payload={
                    "account_id": str(row["account_id"]),
                    "processed_bars": report.processed_bars,
                    "matched_orders": report.matched_orders,
                    "fill_ids": report.fill_ids,
                    "replay_quality": report.replay_quality.value,
                    "ledger_only": True,
                },
                affected_modules=(
                    [ResearchModule.TRADING_CLASSIFICATION, ResearchModule.COMMITTEE]
                    if report.fill_ids
                    else []
                ),
                requires_research=bool(report.fill_ids),
                dedupe_suffix=(
                    f"{row['account_id']}:"
                    f"{report.checkpoint.market_cursor if report.checkpoint else now.isoformat()}"
                ),
            )
            stored, inserted = self.repo.record_event(event)
            events += int(inserted)
            if inserted:
                _, task_inserted = self.repo.ensure_task(stored)
                tasks += int(task_inserted)
        return events, tasks, now.isoformat()

    def _bar_request(self, target: ContinuousMonitorTarget, now: datetime) -> BarRequest:
        end = now.astimezone(_SHANGHAI)
        return BarRequest(
            symbol=target.symbol,
            market=target.market,
            instrument_type=(
                InstrumentType.INDEX if target.market is Market.INDEX else InstrumentType.STOCK
            ),
            frequency=Frequency.H1,
            requested_start=end - timedelta(days=self.config.market.lookback_days),
            requested_end=end,
            adjustment_mode=AdjustmentMode.NONE,
        )

    def _market_sync(self) -> MarketSyncService:
        if self._market_sync_factory is not None:
            return self._market_sync_factory()
        return MarketSyncService(
            [
                EastMoney5mProvider(self.objects, self.state),
                Sina5mProvider(self.objects, self.state),
            ],
            self.state,
            ParquetMarketStore(self.paths.parquet, "market_observation"),
            self.canonical,
        )

    def _event(
        self,
        target: ContinuousMonitorTarget,
        *,
        event_type: MonitorEventType,
        severity: MonitorSeverity,
        observed_at: datetime,
        available_at: datetime,
        source: MonitorSource,
        source_ref: str | None,
        payload: dict[str, object],
        affected_modules: list[ResearchModule],
        requires_research: bool,
        dedupe_suffix: str,
        news_lead_only: bool = False,
    ) -> MonitorEvent:
        payload_hash = content_hash(payload)
        dedupe_key = content_hash(
            {
                "target_id": target.target_id,
                "event_type": event_type.value,
                "source": source.value,
                "suffix": dedupe_suffix,
                "payload_hash": payload_hash,
            }
        )
        return MonitorEvent(
            event_id="monitor-event:" + dedupe_key,
            target_id=target.target_id,
            company_id=target.company_id,
            event_type=event_type,
            severity=severity,
            observed_at=observed_at,
            available_at=max(available_at, observed_at),
            source=source,
            source_ref=source_ref,
            payload=payload,
            payload_hash=payload_hash,
            dedupe_key=dedupe_key,
            affected_modules=sorted(set(affected_modules), key=lambda item: item.value),
            requires_research=requires_research,
            news_lead_only=news_lead_only,
        )


def calculate_monitor_metrics(
    bars: list[MarketBar],
    *,
    high_watermark: float,
    last_review_at: datetime | None,
    now: datetime,
    one_day_bars: int,
    five_day_bars: int,
    volume_ratio_window: int,
) -> dict[MonitorMetric, float | None]:
    if not bars:
        return {item: None for item in MonitorMetric}
    closes = [float(item.close) for item in bars]
    volumes = [float(item.volume) for item in bars]
    latest = closes[-1]
    metrics: dict[MonitorMetric, float | None] = {
        MonitorMetric.LAST_PRICE: latest,
        MonitorMetric.RETURN_1D: _return_over_bars(closes, one_day_bars),
        MonitorMetric.RETURN_5D: _return_over_bars(closes, five_day_bars),
        MonitorMetric.DRAWDOWN_FROM_WATCH_HIGH: (
            latest / high_watermark - 1.0 if high_watermark > 0 else None
        ),
        MonitorMetric.VOLUME_RATIO: None,
        MonitorMetric.DAYS_SINCE_REVIEW: None,
    }
    if len(volumes) >= 2:
        history = volumes[max(0, len(volumes) - volume_ratio_window - 1) : -1]
        average = sum(history) / len(history) if history else 0.0
        metrics[MonitorMetric.VOLUME_RATIO] = volumes[-1] / average if average > 0 else None
    if last_review_at is not None:
        metrics[MonitorMetric.DAYS_SINCE_REVIEW] = max(
            0.0, (now.astimezone(UTC) - last_review_at.astimezone(UTC)).total_seconds() / 86400
        )
    return metrics


def rule_triggered(rule: MonitorRule, *, value: float, now: datetime) -> bool:
    if not rule.active:
        return False
    if rule.last_triggered_at is not None and now.astimezone(UTC) < (
        rule.last_triggered_at.astimezone(UTC) + timedelta(seconds=rule.cooldown_seconds)
    ):
        return False
    if rule.comparison is MonitorComparison.GT:
        return value > rule.threshold
    if rule.comparison is MonitorComparison.GE:
        return value >= rule.threshold
    if rule.comparison is MonitorComparison.LT:
        return value < rule.threshold
    if rule.comparison is MonitorComparison.LE:
        return value <= rule.threshold
    return abs(value - rule.threshold) <= 1e-12


def _return_over_bars(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-bars - 1] <= 0:
        return None
    return closes[-1] / closes[-bars - 1] - 1.0


def _bar_payload(bar: MarketBar) -> dict[str, object]:
    return {
        "observation_id": bar.observation_id,
        "timestamp": bar.timestamp.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "amount": str(bar.amount),
        "adjustment_mode": bar.adjustment_mode.value,
    }


def _catalyst_research_modules(
    modules: list[ProductionResearchModule],
) -> list[ResearchModule]:
    mapping: dict[ProductionResearchModule, ResearchModule] = {
        ProductionResearchModule.EVIDENCE: ResearchModule.EVIDENCE,
        ProductionResearchModule.INDUSTRY: ResearchModule.FUNDAMENTAL_MODEL,
        ProductionResearchModule.COMPANY_ECONOMICS: ResearchModule.FUNDAMENTAL_MODEL,
        ProductionResearchModule.DRIVER_TREE: ResearchModule.FUNDAMENTAL_MODEL,
        ProductionResearchModule.SHARED_HYPOTHESIS: ResearchModule.BASE_CASE,
        ProductionResearchModule.FORECAST: ResearchModule.FUNDAMENTAL_MODEL,
        ProductionResearchModule.VALUATION: ResearchModule.FUNDAMENTAL_MODEL,
        ProductionResearchModule.MARKET_TRADE_CONTEXT: ResearchModule.TRADING_CLASSIFICATION,
        ProductionResearchModule.RESEARCH_MEMO: ResearchModule.BASE_CASE,
        ProductionResearchModule.COMMITTEE: ResearchModule.COMMITTEE,
    }
    return sorted({mapping[module] for module in modules}, key=lambda item: item.value)


def _contains_material_term(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def _disclosure_modules(title: str) -> list[ResearchModule]:
    modules = {ResearchModule.EVIDENCE, ResearchModule.BASE_CASE}
    if any(term in title for term in ("报告", "业绩", "财务", "审计")):
        modules.update(
            {
                ResearchModule.FINANCIAL_INTEGRITY,
                ResearchModule.FUNDAMENTAL_MODEL,
                ResearchModule.COMMITTEE,
            }
        )
    elif _contains_material_term(title, _MATERIAL_DISCLOSURE_TERMS):
        modules.update({ResearchModule.FUNDAMENTAL_MODEL, ResearchModule.COMMITTEE})
    return sorted(modules, key=lambda item: item.value)


def _disclosure_exchange(market: Market) -> DisclosureExchange | None:
    if market is Market.XSHG:
        return DisclosureExchange.SSE
    if market is Market.XSHE:
        return DisclosureExchange.SZSE
    return None


def _cursor_datetime(row: dict[str, object] | None) -> datetime | None:
    if not row or not row.get("cursor"):
        return None
    parsed = datetime.fromisoformat(str(row["cursor"]).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _market_from_text(value: str) -> Market | None:
    try:
        return Market(value)
    except ValueError:
        return None


def _infer_market(symbol: str) -> Market:
    if symbol.startswith(("4", "8", "9")):
        return Market.BJSE
    if symbol.startswith(("5", "6", "9")):
        return Market.XSHG
    return Market.XSHE


__all__ = [
    "ContinuousMonitorService",
    "calculate_monitor_metrics",
    "rule_triggered",
]
