"""Exact official-report selection, immutable PDF registration, parsing, and PIT."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import (
    CninfoDisclosureProvider,
    DocumentPageRepository,
    DocumentRepository,
    PdfParseService,
)
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    AvailabilityBasis,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    DocumentPage,
    DocumentType,
    FetchStatus,
    FinancialPeriodType,
    Market,
    PointInTimeMetadata,
    PointInTimeStatus,
    SourceDocument,
    SourceSnapshot,
)


@dataclass(frozen=True, slots=True)
class OfficialFinancialReport:
    document: SourceDocument
    index_snapshot: SourceSnapshot
    snapshot: SourceSnapshot
    pit: PointInTimeMetadata
    pages: list[DocumentPage]
    supersedes_document_id: str | None


class OfficialFinancialReportService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        fixture_path: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.fixture_path = fixture_path.resolve()
        self.documents = DocumentRepository(state)
        self.pages = DocumentPageRepository(state)
        self.parser = PdfParseService(objects, state, self.pages)
        self.pit = PointInTimeService(PointInTimeRepository(state), state, objects)

    def get(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        period_type: FinancialPeriodType,
        *,
        as_of: datetime,
        live: bool,
        allow_live_capture_after_cutoff: bool = False,
    ) -> OfficialFinancialReport | None:
        candidates = (
            self._live_candidates(company_id, market, period_end, period_type)
            if live
            else self._recorded_candidates(company_id, period_end, period_type)
        )
        selection_cutoff = as_of
        if live and allow_live_capture_after_cutoff and candidates:
            selection_cutoff = max(
                as_of,
                datetime.now(UTC),
                *(item[1].available_to_system_at for item in candidates),
                *(item[2].available_to_system_at for item in candidates),
            )
        eligible = [
            item
            for item in candidates
            if item[0].published_at <= selection_cutoff
            and item[1].available_to_system_at <= selection_cutoff
            and item[2].available_to_system_at <= selection_cutoff
        ]
        if not eligible:
            return None
        previous_source: str | None = None
        reports: list[OfficialFinancialReport] = []
        for document, snapshot, index_snapshot, declared_supersedes in sorted(
            eligible, key=lambda item: (item[0].published_at, item[0].document_id)
        ):
            supersedes = declared_supersedes or previous_source
            if supersedes is not None and self.documents.get_model(supersedes) is None:
                supersedes = None
            self.documents.register(document, snapshot)
            pit = self.pit.create(
                source_id=document.document_id,
                source_document_id=document.document_id,
                source_snapshot_id=snapshot.snapshot_id,
                period_end=period_end,
                published_at=document.published_at,
                effective_at=document.effective_at,
                ingested_at=snapshot.fetched_at,
                available_to_system_at=snapshot.available_to_system_at,
                revised_at=document.published_at if supersedes else None,
                supersedes_source_id=supersedes,
                point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
                availability_basis=AvailabilityBasis.FETCH_OBSERVED,
            )
            parse = self.parser.parse(document, snapshot, ocr_enabled=False)
            pages = [self.pages.get_page_by_id(page_id) for page_id in parse.page_ids]
            if any(page is None for page in pages):
                raise ValueError("Official PDF page registration is incomplete")
            reports.append(
                OfficialFinancialReport(
                    document=document,
                    index_snapshot=index_snapshot,
                    snapshot=snapshot,
                    pit=pit,
                    pages=[page for page in pages if page is not None],
                    supersedes_document_id=supersedes,
                )
            )
            previous_source = document.document_id
        return reports[-1]

    def _recorded_candidates(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
    ) -> list[tuple[SourceDocument, SourceSnapshot, SourceSnapshot, str | None]]:
        raw = self.fixture_path.read_bytes()
        try:
            payload = json.loads(raw)
            if payload.get("schema_version") != "financial-official-reports-v1":
                raise ValueError
            reports = payload["reports"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Official financial report fixture is invalid") from exc
        if not isinstance(reports, list):
            raise ValueError("Official financial report list is invalid")
        result = []
        for item in reports:
            if not isinstance(item, dict):
                raise ValueError("Official financial report row is invalid")
            if (
                item.get("company_id") != company_id
                or item.get("period_end") != period_end.isoformat()
                or item.get("period_type") != period_type.value
            ):
                continue
            pdf = base64.b64decode(str(item["pdf_base64"]), validate=True)
            if not pdf.lstrip().startswith(b"%PDF-"):
                raise ValueError("Official financial report is not a PDF")
            observed = datetime.fromisoformat(
                str(item["observed_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
            index_payload = {
                "schema_version": "financial-official-index-v1",
                "report": {
                    key: value for key, value in item.items() if key != "pdf_base64"
                },
            }
            index_ref = self.objects.put_bytes(canonical_json_bytes(index_payload))
            index_snapshot = SourceSnapshot(
                created_at=observed,
                snapshot_id=f"cninfo-financial:index:{index_ref.sha256}",
                source_id="cninfo-financial:index",
                object_sha256=index_ref.sha256,
                fetched_at=observed,
                available_to_system_at=observed,
                source_url="recorded://cninfo-financial-index",
                mime="application/json",
                byte_size=index_ref.byte_size,
                fetch_status=FetchStatus.SUCCEEDED,
                rights_status="PUBLIC_DISCLOSURE",
            )
            self.state.register_snapshot(index_snapshot)
            published = datetime.fromisoformat(
                str(item["published_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
            object_ref = self.objects.put_bytes(pdf)
            announcement_id = str(item["announcement_id"])
            document_id = f"cninfo:{announcement_id}"
            snapshot = SourceSnapshot(
                created_at=observed,
                snapshot_id=f"cninfo-financial:document:{announcement_id}:{object_ref.sha256}",
                source_id=f"cninfo-financial:document:{announcement_id}",
                object_sha256=object_ref.sha256,
                fetched_at=observed,
                available_to_system_at=observed,
                source_url=str(item["source_url"]),
                mime="application/pdf",
                byte_size=object_ref.byte_size,
                headers_hash=content_hash({"announcement_id": announcement_id}),
                fetch_status=FetchStatus.SUCCEEDED,
                rights_status="PUBLIC_DISCLOSURE",
            )
            self.state.register_snapshot(snapshot)
            document = SourceDocument(
                created_at=observed,
                document_id=document_id,
                title=str(item["title"]),
                publisher=str(item["publisher"]),
                document_type=_document_type(period_type),
                company_ids=[company_id],
                published_at=published,
                effective_at=published,
                disclosure_id=announcement_id,
                source_url=str(item["source_url"]),
                rights_status="PUBLIC_DISCLOSURE",
            )
            supersedes = item.get("supersedes_announcement_id")
            result.append(
                (
                    document,
                    snapshot,
                    index_snapshot,
                    f"cninfo:{supersedes}" if supersedes else None,
                )
            )
        return result

    def _live_candidates(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        period_type: FinancialPeriodType,
    ) -> list[tuple[SourceDocument, SourceSnapshot, SourceSnapshot, str | None]]:
        if market is Market.BJSE:
            return []
        exchange = DisclosureExchange.SSE if market is Market.XSHG else DisclosureExchange.SZSE
        category = {
            FinancialPeriodType.ANNUAL: DisclosureCategory.ANNUAL_REPORT,
            FinancialPeriodType.SEMIANNUAL: DisclosureCategory.SEMIANNUAL_REPORT,
            FinancialPeriodType.QUARTERLY: DisclosureCategory.QUARTERLY_REPORT,
        }[period_type]
        provider = CninfoDisclosureProvider(self.objects, self.state)
        batch = provider.search(
            DisclosureSearchRequest(
                symbol=company_id,
                exchange=exchange,
                start_date=period_end + timedelta(days=1),
                end_date=date.today(),
                category=category,
                keyword=_title_key(period_end, period_type),
                page_size=100,
            )
        )
        index_snapshot = self.state.get_snapshot(batch.raw_snapshot_id)
        if index_snapshot is None:
            raise ValueError("Official financial search index snapshot is missing")
        matched = [
            announcement
            for announcement in batch.announcements
            if _exact_report_title(announcement.title, period_end, period_type)
        ]
        result = []
        previous: str | None = None
        for announcement in sorted(matched, key=lambda item: item.published_at):
            downloaded = provider.download(announcement)
            result.append(
                (downloaded.document, downloaded.snapshot, index_snapshot, previous)
            )
            previous = downloaded.document.document_id
        return result


def _document_type(period_type: FinancialPeriodType) -> DocumentType:
    return {
        FinancialPeriodType.ANNUAL: DocumentType.ANNUAL_REPORT,
        FinancialPeriodType.SEMIANNUAL: DocumentType.SEMIANNUAL_REPORT,
        FinancialPeriodType.QUARTERLY: DocumentType.QUARTERLY_REPORT,
    }[period_type]


def _title_key(period_end: date, period_type: FinancialPeriodType) -> str:
    if period_type is FinancialPeriodType.QUARTERLY:
        suffix = "第一季度报告" if period_end.month == 3 else "第三季度报告"
    else:
        suffix = {
            FinancialPeriodType.ANNUAL: "年度报告",
            FinancialPeriodType.SEMIANNUAL: "半年度报告",
        }[period_type]
    return f"{period_end.year}年{suffix}"


def _exact_report_title(
    title: str, period_end: date, period_type: FinancialPeriodType
) -> bool:
    if period_type is FinancialPeriodType.QUARTERLY:
        quarter = "一|第一" if period_end.month == 3 else "三|第三"
        key = rf"{period_end.year}年(?:{quarter})季度报告"
    else:
        key = re.escape(_title_key(period_end, period_type))
    return re.fullmatch(rf".*{key}(?:[（(](?:更正|修订)(?:后)?[）)])?", title) is not None


__all__ = ["OfficialFinancialReport", "OfficialFinancialReportService"]
