"""Browser-backed Xueqiu/Zhihu search provider with auditable seven-day coverage."""

from __future__ import annotations

import re
import time as clock
from collections import OrderedDict
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, urlparse, urlunparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.supplemental import SupplementalRequest
from astock.schemas.evidence import FetchStatus, SourceSnapshot
from astock.social_sources.bridge import SocialCaptureHTTPServer, SocialCaptureSession
from astock.social_sources.config import SocialSearchConfig, load_social_search_config
from astock.social_sources.models import (
    SocialCaptureResult,
    SocialCaptureStatus,
    SocialLead,
    SocialParseStatus,
    SocialPlatform,
    SocialPlatformCoverage,
    SocialQueryCapture,
    SocialQueryKind,
    SocialSearchJob,
    SocialSearchQuery,
    SocialSearchReceipt,
    SocialSearchRequest,
    SocialSearchResult,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_POSITIVE_DEFAULT = ("利好", "增长", "超预期", "新订单", "放量", "涨价", "受益", "改善")
_NEGATIVE_DEFAULT = ("利空", "暴雷", "亏损", "下滑", "减持", "诉讼", "处罚", "调查", "造假")


class SocialBrowserSearchProvider:
    """Search both community platforms through the signed-in Chrome profile."""

    provider_id = "social-browser-search"
    capability = "news.community.lead"
    platforms = (SocialPlatform.XUEQIU, SocialPlatform.ZHIHU)

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        project_root: Path,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.objects = objects
        self.state = state
        self.project_root = project_root.resolve()
        # Only the explicit one-time pairing command may create a bridge key.
        # Ordinary research should fail fast when the browser is not paired,
        # rather than silently creating a key and waiting for an absent extension.
        try:
            self.config: SocialSearchConfig = load_social_search_config(
                self.project_root, create_local=False
            )
        except ValueError as exc:
            raise ProviderError(
                "local social capture extension pairing is not configured",
                failure_class=FailureClass.AUTH_REQUIRED,
            ) from exc
        self.timeout_seconds = min(
            float(timeout_seconds), float(self.config.request_timeout_seconds)
        )

    def session_status(self) -> dict[str, object]:
        extension = self.project_root / "browser_extension" / "social_capture" / "manifest.json"
        local = self.project_root / "runtime" / "local" / "social_capture.yaml"
        return {
            "configured": bool(self.config.enabled and extension.is_file() and local.is_file()),
            "bridge_host": self.config.bridge_host,
            "bridge_port": self.config.bridge_port,
            "extension_id": self.config.extension_id,
        }

    def fetch_hints(
        self, request: SupplementalRequest
    ) -> tuple[list[dict[str, object]], SourceSnapshot]:
        result = self.search_checked(
            symbol=request.symbol,
            company_id=str(getattr(request, "company_id", "") or ""),
            company_names=tuple(getattr(request, "company_names", ()) or ()),
            aliases=tuple(getattr(request, "aliases", ()) or ()),
            industry_keywords=tuple(getattr(request, "industry_keywords", ()) or ()),
            management_keywords=tuple(getattr(request, "management_keywords", ()) or ()),
            start=_request_bound(request.start, is_end=False),
            end=_request_bound(request.end, is_end=True),
        )
        rows = [
            {
                "lead_id": lead.lead_id,
                "platform": lead.platform.value,
                "symbol": lead.symbol,
                "company_id": lead.company_id,
                "company_name": lead.company_name,
                "title": lead.title,
                "text": lead.text,
                "author": lead.author,
                "url": lead.url,
                "published_at": lead.published_at.isoformat(),
                "content_type": lead.content_type,
                "is_original": lead.is_original,
                "is_video": lead.is_video,
                "sentiment_hint": lead.sentiment_hint,
                "sentiment_terms": list(lead.sentiment_terms),
                "matched_queries": list(lead.matched_queries),
                "query_kinds": [item.value for item in lead.query_kinds],
                "snapshot_id": lead.snapshot_id,
                "coverage_status": "COMPLETE" if result.complete else "PARTIAL",
            }
            for lead in result.leads
        ]
        snapshot = self.state.get_snapshot(result.snapshot_id)
        if snapshot is None:
            raise ProviderError(
                "social search receipt snapshot was not registered",
                failure_class=FailureClass.INVALID_RESPONSE,
            )
        return rows, snapshot

    def search_checked(
        self,
        *,
        symbol: str = "",
        company_id: str = "",
        company_names: tuple[str, ...] | list[str] = (),
        aliases: tuple[str, ...] | list[str] = (),
        industry_keywords: tuple[str, ...] | list[str] = (),
        management_keywords: tuple[str, ...] | list[str] = (),
        start: datetime,
        end: datetime,
        max_queries_per_platform: int | None = None,
    ) -> SocialSearchResult:
        if not self.config.enabled:
            raise ProviderError(
                "social search is disabled in local configuration",
                failure_class=FailureClass.AUTH_REQUIRED,
            )
        now = datetime.now(UTC)
        requested_start = _as_aware_datetime(start)
        requested_end = min(_as_aware_datetime(end), now)
        window_start = max(
            requested_start,
            requested_end - timedelta(days=self.config.lookback_days),
        )
        if window_start >= requested_end:
            raise ValueError("social search window must contain a positive interval")
        request = SocialSearchRequest(
            symbol=str(symbol or "").strip(),
            company_id=str(company_id or "").strip(),
            company_names=_unique_terms(company_names),
            aliases=_unique_terms(aliases),
            industry_keywords=_unique_terms(industry_keywords),
            management_keywords=_unique_terms(management_keywords),
            start=window_start,
            end=requested_end,
            max_queries_per_platform=min(
                max_queries_per_platform or self.config.max_queries_per_platform,
                self.config.max_queries_per_platform,
            ),
        )
        session_id = uuid4().hex
        jobs = tuple(
            SocialSearchJob(
                job_id=f"{platform.value.lower()}-{uuid4().hex[:16]}",
                session_id=session_id,
                platform=platform,
                queries=_build_queries(request, platform),
                start=window_start,
                end=requested_end,
                max_scrolls=self.config.max_scrolls,
                max_items_per_query=self.config.max_items_per_query,
                created_at=now,
            )
            for platform in self.platforms
        )
        jobs = tuple(job for job in jobs if job.queries)
        if not jobs:
            raise ValueError("social search request contains no searchable terms")
        session = SocialCaptureSession(
            jobs,
            lease_seconds=self.config.job_lease_seconds,
        )
        started = clock.monotonic()
        try:
            with SocialCaptureHTTPServer(self.config, session):
                # Extension alarms poll every 30 seconds. Fail early when no
                # browser agent has checked in, rather than holding a full
                # research budget for an unpaired session.
                session.wait(min(35.0, self.timeout_seconds))
                if (
                    session.status()["extension_last_seen_at"] is None
                    and not session.results()
                ):
                    raise ProviderError(
                        "social capture extension is not connected",
                        failure_class=FailureClass.AUTH_REQUIRED,
                    )
                remaining = self.timeout_seconds - (clock.monotonic() - started)
                if remaining > 0:
                    session.wait(remaining)
        except OSError as exc:
            raise ProviderError(
                "local social capture bridge could not start",
                failure_class=FailureClass.NETWORK,
                details={"error_type": type(exc).__name__},
            ) from exc
        results = session.results()
        if not results:
            state = session.status()
            failure = (
                FailureClass.AUTH_REQUIRED
                if state["extension_last_seen_at"] is None
                else FailureClass.NETWORK
            )
            raise ProviderError(
                "Chrome social capture extension did not return a search result",
                failure_class=failure,
                details={"session_id": session_id, "job_counts": state["counts"]},
            )
        return self._persist_result(request, session_id, jobs, results, now)

    def _persist_result(
        self,
        request: SocialSearchRequest,
        session_id: str,
        jobs: tuple[SocialSearchJob, ...],
        results: tuple[SocialCaptureResult, ...],
        requested_at: datetime,
    ) -> SocialSearchResult:
        result_by_platform = {item.platform: item for item in results}
        job_by_platform = {item.platform: item for item in jobs}
        leads: OrderedDict[str, SocialLead] = OrderedDict()
        coverage: list[SocialPlatformCoverage] = []
        query_snapshot_ids: list[str] = []
        input_hashes: list[str] = []
        company_name = (
            request.company_names[0]
            if request.company_names
            else request.aliases[0] if request.aliases else ""
        )
        for platform in self.platforms:
            job = job_by_platform.get(platform)
            captured = result_by_platform.get(platform)
            if job is None:
                continue
            if captured is None:
                coverage.append(
                    SocialPlatformCoverage(
                        platform=platform,
                        start=request.start,
                        end=request.end,
                        query_count=len(job.queries),
                        completed_query_count=0,
                        raw_item_count=0,
                        accepted_count=0,
                        filtered_time_count=0,
                        filtered_content_count=0,
                        duplicate_count=0,
                        complete=False,
                        reason="CAPTURE_TIMEOUT",
                        checked_through=request.start,
                        snapshot_ids=(),
                    )
                )
                continue
            raw_count = 0
            accepted_count = 0
            filtered_time = 0
            undated_count = 0
            filtered_content = 0
            duplicate_count = 0
            completed_queries = 0
            platform_snapshot_ids: list[str] = []
            query_map = {query.query_id: query for query in job.queries}
            for query_capture in captured.query_results:
                query = query_map.get(query_capture.query_id)
                if query is None:
                    continue
                snapshot = self._persist_query_snapshot(platform, query_capture, session_id)
                platform_snapshot_ids.append(snapshot.snapshot_id)
                query_snapshot_ids.append(snapshot.snapshot_id)
                input_hashes.append(snapshot.object_sha256)
                raw_count += len(query_capture.items)
                if (
                    query_capture.parse_status
                    in {SocialParseStatus.PARSED, SocialParseStatus.EMPTY}
                    and not query_capture.has_more
                ):
                    completed_queries += 1
                for item in query_capture.items:
                    publication, _, _ = _resolve_publication(
                        item.model_dump(mode="python"),
                        platform=platform,
                        request=request,
                        captured_at=query_capture.captured_at,
                    )
                    normalized = _normalize_item(
                        item.model_dump(mode="python"),
                        platform=platform,
                        request=request,
                        company_name=company_name,
                        captured_at=query_capture.captured_at,
                        snapshot_id=snapshot.snapshot_id,
                        query=query.text,
                        query_kind=query.kind,
                        config=self.config,
                    )
                    if normalized is None:
                        if publication is None:
                            undated_count += 1
                        elif not request.start <= publication <= request.end:
                            filtered_time += 1
                        else:
                            filtered_content += 1
                        continue
                    existing = leads.get(normalized.url)
                    if existing is not None:
                        duplicate_count += 1
                        merged = existing.model_copy(
                            update={
                                "matched_queries": tuple(
                                    sorted(
                                set(existing.matched_queries)
                                | set(normalized.matched_queries)
                            )
                                ),
                                "query_kinds": tuple(
                                    sorted(
                                        set(existing.query_kinds) | set(normalized.query_kinds),
                                        key=lambda item: item.value,
                                    )
                                ),
                            }
                        )
                        leads[normalized.url] = merged
                        continue
                    leads[normalized.url] = normalized
                    accepted_count += 1
            parse_complete = (
                len(captured.query_results) == len(job.queries)
                and completed_queries == len(job.queries)
                and not captured.error_code
                and undated_count == 0
            )
            reason = ""
            if not parse_complete:
                reason = captured.error_code or "QUERY_CAPTURE_INCOMPLETE"
                if any(item.has_more for item in captured.query_results):
                    reason = "MORE_RESULTS_AVAILABLE"
                elif undated_count:
                    reason = "UNVERIFIABLE_PUBLISHED_AT"
                if any(
                    item.parse_status is SocialParseStatus.LOGIN_REQUIRED
                    for item in captured.query_results
                ):
                    reason = "LOGIN_REQUIRED"
            coverage.append(
                SocialPlatformCoverage(
                    platform=platform,
                    start=request.start,
                    end=request.end,
                    query_count=len(job.queries),
                    completed_query_count=completed_queries,
                    raw_item_count=raw_count,
                    accepted_count=accepted_count,
                    filtered_time_count=filtered_time,
                    undated_count=undated_count,
                    filtered_content_count=filtered_content,
                    duplicate_count=duplicate_count,
                    complete=parse_complete,
                    reason=reason,
                    checked_through=request.end if parse_complete else request.start,
                    snapshot_ids=tuple(platform_snapshot_ids),
                )
            )
        complete = len(coverage) == len(self.platforms) and all(item.complete for item in coverage)
        completed_at = datetime.now(UTC)
        ordered_leads = tuple(
            sorted(
                leads.values(),
                key=lambda item: (item.published_at, item.platform.value, item.lead_id),
            )
        )
        receipt_body = {
            "schema_version": "social-search-receipt-v1",
            "request": request.model_dump(mode="json"),
            "requested_at": requested_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "status": (
                SocialCaptureStatus.COMPLETE.value
                if complete else SocialCaptureStatus.PARTIAL.value
            ),
            "coverage": [item.model_dump(mode="json") for item in coverage],
            "lead_ids": [item.lead_id for item in ordered_leads],
            "snapshot_ids": sorted(query_snapshot_ids),
        }
        receipt_hash = content_hash(receipt_body)
        receipt_id = f"SocialSearchReceipt:{receipt_hash}"
        receipt = SocialSearchReceipt(
            receipt_id=receipt_id,
            request=request,
            requested_at=requested_at,
            completed_at=completed_at,
            status=SocialCaptureStatus.COMPLETE if complete else SocialCaptureStatus.PARTIAL,
            coverage=tuple(coverage),
            lead_ids=tuple(item.lead_id for item in ordered_leads),
            snapshot_ids=tuple(sorted(query_snapshot_ids)),
            receipt_hash=receipt_hash,
        )
        receipt_ref = self.objects.put_json(receipt.model_dump(mode="json"))
        aggregate_snapshot = SourceSnapshot(
            snapshot_id=f"social-browser-search:{session_id}:{receipt_ref.sha256}",
            source_id="social-browser-search",
            object_sha256=receipt_ref.sha256,
            fetched_at=completed_at,
            available_to_system_at=completed_at,
            source_url=f"social://search/{session_id}",
            mime="application/vnd.astock.social-search+json",
            byte_size=len(self.objects.get_bytes(receipt_ref.sha256)),
            headers_hash=content_hash({
                "transport": "CHROME_EXTENSION_DOM",
                "schema": "social-search-receipt-v1",
            }),
            fetch_status=FetchStatus.SUCCEEDED if complete else FetchStatus.PARTIAL,
            rights_status="COMMUNITY_LEAD",
        )
        self.state.register_snapshot(aggregate_snapshot)
        self.state.register_artifact(
            artifact_id=receipt_id,
            artifact_type="SocialSearchReceipt",
            schema_version=receipt.schema_version,
            object_hash=receipt_ref.sha256,
            input_hashes=sorted(set(input_hashes)),
        )
        result_body = {
            "receipt_id": receipt_id,
            "snapshot_id": aggregate_snapshot.snapshot_id,
            "snapshot_ids": sorted(set(query_snapshot_ids + [aggregate_snapshot.snapshot_id])),
            "leads": [lead.model_dump(mode="json") for lead in ordered_leads],
            "coverage": [item.model_dump(mode="json") for item in coverage],
            "complete": complete,
        }
        result = SocialSearchResult(
            result_artifact_id=f"SocialSearchResult:{content_hash(result_body)}",
            **result_body,
        )
        result_ref = self.objects.put_json(result.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=result.result_artifact_id,
            artifact_type="SocialSearchResult",
            schema_version=result.schema_version,
            object_hash=result_ref.sha256,
            input_hashes=sorted(set([receipt_ref.sha256, *input_hashes])),
        )
        return result

    def _persist_query_snapshot(
        self,
        platform: SocialPlatform,
        query_capture: SocialQueryCapture,
        session_id: str,
    ) -> SourceSnapshot:
        payload = query_capture.model_dump(mode="json")
        reference = self.objects.put_json(payload)
        source_id = f"social-{platform.value.lower()}-search"
        fetch_status = _fetch_status(query_capture)
        snapshot = SourceSnapshot(
            # StateStore enforces uniqueness of (source_id, object_hash).
            # Replayed identical pages reuse the same immutable snapshot id.
            snapshot_id=f"{source_id}:{reference.sha256}",
            source_id=source_id,
            object_sha256=reference.sha256,
            fetched_at=query_capture.captured_at,
            available_to_system_at=datetime.now(UTC),
            source_url=query_capture.page_url,
            mime="application/vnd.astock.social-search-page+json",
            byte_size=len(self.objects.get_bytes(reference.sha256)),
            headers_hash=content_hash(
                {
                    "transport": "CHROME_EXTENSION_DOM",
                    "selector_version": query_capture.selector_version,
                    "parse_status": query_capture.parse_status.value,
                }
            ),
            fetch_status=fetch_status,
            rights_status="COMMUNITY_LEAD",
        )
        self.state.register_snapshot(snapshot)
        return snapshot


class XueqiuBrowserSearchProvider(SocialBrowserSearchProvider):
    provider_id = "xueqiu-browser-search"
    platforms = (SocialPlatform.XUEQIU,)


class ZhihuBrowserSearchProvider(SocialBrowserSearchProvider):
    provider_id = "zhihu-browser-search"
    platforms = (SocialPlatform.ZHIHU,)


def _build_queries(
    request: SocialSearchRequest,
    platform: SocialPlatform,
) -> tuple[SocialSearchQuery, ...]:
    company = request.company_names[0] if request.company_names else (
        request.aliases[0] if request.aliases else request.symbol
    )
    candidates: list[tuple[str, SocialQueryKind]] = []
    if company:
        candidates.append((company, SocialQueryKind.COMPANY))
    if request.symbol and request.symbol != company:
        candidates.append((request.symbol, SocialQueryKind.SYMBOL))
    elif len(request.aliases) > 1:
        candidates.append((request.aliases[1], SocialQueryKind.COMPANY))
    if request.industry_keywords:
        term = request.industry_keywords[0]
        candidates.append((f"{company} {term}".strip(), SocialQueryKind.INDUSTRY))
    management = request.management_keywords or ()
    if not management:
        management = ("董事长", "总经理", "实际控制人")
    if management:
        candidates.append((f"{company} {management[0]}".strip(), SocialQueryKind.MANAGEMENT))
    if len(management) > 1 and request.max_queries_per_platform > 4:
        candidates.append((f"{company} {management[1]}".strip(), SocialQueryKind.MANAGEMENT))
    unique: OrderedDict[str, tuple[str, SocialQueryKind]] = OrderedDict()
    for text, kind in candidates:
        cleaned = " ".join(text.split()).strip()
        key = cleaned.casefold()
        if cleaned and key not in unique:
            unique[key] = (cleaned, kind)
    result: list[SocialSearchQuery] = []
    for index, (text, kind) in enumerate(list(unique.values())[: request.max_queries_per_platform]):
        if platform is SocialPlatform.XUEQIU:
            url = f"https://xueqiu.com/k?q={quote_plus(text)}&type=status"
        else:
            url = f"https://www.zhihu.com/search?type=content&q={quote_plus(text)}"
        result.append(
            SocialSearchQuery(
                query_id=f"{platform.value.lower()}-{index + 1}",
                text=text,
                kind=kind,
                search_url=url,
            )
        )
    return tuple(result)


def _normalize_item(
    raw: dict[str, object],
    *,
    platform: SocialPlatform,
    request: SocialSearchRequest,
    company_name: str,
    captured_at: datetime,
    snapshot_id: str,
    query: str,
    query_kind: SocialQueryKind,
    config: SocialSearchConfig,
) -> SocialLead | None:
    url = _canonical_social_url(str(raw.get("url") or ""), platform)
    if not url:
        return None
    title = _clean(str(raw.get("title") or ""))
    text = _clean(str(raw.get("text") or ""))[: config.maximum_text_characters]
    if not title and text:
        title = text[:180]
    if not title or len(text) < config.minimum_text_characters:
        return None
    if not _relevant_to_subject(
        title, text, request=request, query_kind=query_kind,
    ):
        return None
    published_at, published_precision, published_raw = _resolve_publication(
        raw, platform=platform, request=request, captured_at=captured_at
    )
    if published_at is None or not request.start <= published_at <= request.end:
        return None
    is_original = raw.get("is_original")
    is_video = raw.get("is_video")
    if platform is SocialPlatform.XUEQIU and (
        is_original is not True
        or re.search(r"来自(?:新闻|微博|微信公众号|网站)", published_raw)
    ):
        return None
    if platform is SocialPlatform.ZHIHU and is_video is not False:
        return None
    sentiment, sentiment_terms = _sentiment_hint(text, title, config)
    # Stable across recaptures, relative-time parsing and title edits.
    identity = content_hash({"platform": platform.value, "url": url})[:40]
    return SocialLead(
        lead_id=f"{platform.value.lower()}:{identity}",
        platform=platform,
        symbol=request.symbol,
        company_id=request.company_id,
        company_name=company_name,
        url=url,
        title=title,
        text=text,
        author=_clean(str(raw.get("author") or ""))[:300],
        published_at=published_at,
        published_precision=published_precision,
        published_raw=published_raw,
        captured_at=captured_at,
        matched_queries=(query,),
        query_kinds=(query_kind,),
        content_type=str(raw.get("content_type") or "UNKNOWN")[:80],
        is_original=is_original if isinstance(is_original, bool) else None,
        is_video=is_video if isinstance(is_video, bool) else None,
        sentiment_hint=sentiment,
        sentiment_terms=sentiment_terms,
        snapshot_id=snapshot_id,
    )


def _relevant_to_subject(
    title: str,
    text: str,
    *,
    request: SocialSearchRequest,
    query_kind: SocialQueryKind,
) -> bool:
    """Reject unrelated feed cards while retaining contextual industry leads."""
    content = f"{title} {text}".casefold()
    anchors = _unique_terms([
        *request.company_names, *request.aliases, request.symbol,
    ])
    matching_identity = any(
        len(term) >= 2 and term.casefold() in content for term in anchors
    )
    if query_kind is SocialQueryKind.INDUSTRY:
        return any(
            term.casefold() in content for term in request.industry_keywords
        )
    if query_kind is SocialQueryKind.MANAGEMENT:
        # Do not assign a generic executive discussion to an unrelated company.
        return matching_identity and any(
            term.casefold() in content
            for term in (request.management_keywords or (
                "董事长", "总经理", "实际控制人",
            ))
        )
    return matching_identity


def _canonical_social_url(value: str, platform: SocialPlatform) -> str | None:
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    allowed = (
        {"xueqiu.com", "www.xueqiu.com"}
        if platform is SocialPlatform.XUEQIU
        else {"zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com"}
    )
    if parsed.hostname.lower() not in allowed:
        return None
    if parsed.path in {"", "/", "/k", "/search", "/signin", "/signin/"}:
        return None
    return urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


class _VisibleTimestampParser(HTMLParser):
    """Read only a platform timestamp element from a captured DOM excerpt."""

    def __init__(self, marker: str) -> None:
        super().__init__(convert_charrefs=True)
        self.marker = marker
        self.depth = 0
        self.parts: list[str] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if self.depth:
            self.depth += 1
            return
        if tag in {"a", "span", "time"} and self.marker in (
            dict(attrs).get("class") or ""
        ).split():
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self.depth:
            self.depth -= 1
            if not self.depth:
                self.done = True

    def handle_data(self, data: str) -> None:
        if self.depth and not self.done:
            self.parts.append(data)


def _visible_published_raw(raw_excerpt: str, platform: SocialPlatform) -> str:
    """Recover date labels from frozen pages captured before the JS selector fix."""
    if not raw_excerpt:
        return ""
    marker = "date-and-source" if platform is SocialPlatform.XUEQIU else "SearchItem-time"
    parser = _VisibleTimestampParser(marker)
    parser.feed(raw_excerpt[:12000])
    return " ".join("".join(parser.parts).split())[:160]


def _day_only(value: str, captured_at: datetime) -> datetime | None:
    """Interpret a date label as a Shanghai calendar day, not an instant."""
    match = re.fullmatch(r"(?:(20\d{2})[-/])?(\d{1,2})[-/](\d{1,2})", value)
    if match is None:
        return None
    raw_year, month, day = match.groups()
    captured_local = captured_at.astimezone(_SHANGHAI)
    year = int(raw_year) if raw_year else captured_local.year
    try:
        candidate = datetime(year, int(month), int(day), tzinfo=_SHANGHAI)
        if raw_year is None and candidate.date() > captured_local.date():
            candidate = datetime(year - 1, int(month), int(day), tzinfo=_SHANGHAI)
    except ValueError:
        return None
    return candidate.astimezone(UTC)


def _resolve_publication(
    raw: dict[str, object],
    *,
    platform: SocialPlatform,
    request: SocialSearchRequest,
    captured_at: datetime,
) -> tuple[datetime | None, Literal["INSTANT", "DAY", "RELATIVE"], str]:
    """Preserve date precision and reject seven-day boundary-day ambiguities."""
    exact = str(raw.get("published_at") or "").strip()
    visible = str(raw.get("published_raw") or "").strip()
    if not visible and not exact:
        visible = _visible_published_raw(
            str(raw.get("raw_excerpt") or ""), platform
        )
    day = _day_only(visible, captured_at)
    if day is not None:
        # Treat the day as a set of possible publication instants. Its start
        # is an indexing anchor, NOT a fabricated exact publication time.
        if day + timedelta(days=1) <= request.start or day >= request.end:
            return day, "DAY", visible
        if day < request.start or day + timedelta(days=1) > request.end:
            return None, "DAY", visible
        return day, "DAY", visible
    value = exact or visible
    parsed = _parse_published_at(value, captured_at)
    if parsed is not None and parsed > captured_at + timedelta(seconds=60):
        parsed = None
    relative = bool(re.search(r"刚刚|昨天|前天|\d+\s*(?:秒|分钟|小时|天)前", value))
    return parsed, "RELATIVE" if relative else "INSTANT", visible or value


def _parse_published_at(value: str, captured_at: datetime) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            number = int(raw)
            seconds = number / 1000 if number > 10_000_000_000 else number
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=_SHANGHAI)
        return parsed.astimezone(UTC)
    except ValueError:
        pass
    now = captured_at.astimezone(UTC)
    relative = (
        (r"(\d+)\s*秒前", 1),
        (r"(\d+)\s*分钟前", 60),
        (r"(\d+)\s*小时前", 3600),
        (r"(\d+)\s*天前", 86400),
    )
    if "刚刚" in raw or "分钟前" in raw and raw.startswith("刚"):
        return now
    for pattern, multiplier in relative:
        match = re.search(pattern, raw)
        if match:
            return now - timedelta(seconds=int(match.group(1)) * multiplier)
    relative_day = re.search(r"(昨天|前天)(?:\s*(\d{1,2}):(\d{2}))?", raw)
    if relative_day:
        local = now.astimezone(_SHANGHAI) - timedelta(
            days=1 if relative_day.group(1) == "昨天" else 2
        )
        if relative_day.group(2) is not None:
            return local.replace(
                hour=int(relative_day.group(2)),
                minute=int(relative_day.group(3)),
                second=0,
                microsecond=0,
            ).astimezone(UTC)
        return local.astimezone(UTC)
    match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s*(\d{1,2}):(\d{2})", raw)
    if match:
        year, month, day, hour, minute = (int(value) for value in match.groups())
        return datetime(year, month, day, hour, minute, tzinfo=_SHANGHAI).astimezone(UTC)
    match = re.search(r"(?<!\d)(\d{1,2})[-月/](\d{1,2})日?\s*(\d{1,2}):(\d{2})", raw)
    if match:
        month, day, hour, minute = (int(value) for value in match.groups())
        year = now.astimezone(_SHANGHAI).year
        candidate = datetime(year, month, day, hour, minute, tzinfo=_SHANGHAI)
        if candidate > now.astimezone(_SHANGHAI) + timedelta(days=1):
            candidate = candidate.replace(year=year - 1)
        return candidate.astimezone(UTC)
    return None


def _sentiment_hint(
    text: str,
    title: str,
    config: SocialSearchConfig,
) -> tuple[Literal["POSITIVE_HINT", "NEGATIVE_HINT", "MIXED_OR_NEUTRAL"], tuple[str, ...]]:
    combined = f"{title} {text}"
    positive = tuple(
        term for term in (config.positive_hint_terms or _POSITIVE_DEFAULT)
        if term in combined
    )
    negative = tuple(
        term for term in (config.negative_hint_terms or _NEGATIVE_DEFAULT)
        if term in combined
    )
    terms = tuple(sorted(set(positive + negative)))
    if len(positive) > len(negative):
        return "POSITIVE_HINT", terms
    if len(negative) > len(positive):
        return "NEGATIVE_HINT", terms
    return "MIXED_OR_NEUTRAL", terms


def _fetch_status(capture: SocialQueryCapture) -> FetchStatus:
    if capture.parse_status is SocialParseStatus.LOGIN_REQUIRED:
        return FetchStatus.ACCESS_RESTRICTED
    if capture.parse_status in {SocialParseStatus.UNPARSED, SocialParseStatus.ERROR}:
        return FetchStatus.FETCH_FAILED
    if capture.has_more:
        return FetchStatus.PARTIAL
    return FetchStatus.SUCCEEDED


def _unique_terms(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result: OrderedDict[str, str] = OrderedDict()
    for value in values:
        text = " ".join(str(value).split()).strip()
        if text and text.casefold() not in result:
            result[text.casefold()] = text
    return tuple(result.values())


def _clean(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split()).strip()


def _request_bound(value: date | datetime, *, is_end: bool) -> datetime:
    if isinstance(value, datetime):
        return _as_aware_datetime(value)
    if isinstance(value, date):
        result = datetime.combine(value, time.min, tzinfo=UTC)
        return result + timedelta(days=1) - timedelta(seconds=1) if is_end else result
    raise ValueError("social search date bounds must be dates or aware datetimes")


def _as_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("social search times must be timezone-aware")
    return value.astimezone(UTC)
