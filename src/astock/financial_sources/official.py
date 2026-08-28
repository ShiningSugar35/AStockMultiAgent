"""Exact official-report selection, immutable PDF registration, parsing, and PIT."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from astock.core.errors import AStockError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import (
    DisclosureEnumerationProvider,
    DocumentPageRepository,
    DocumentRepository,
    PdfParseService,
)
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.providers import ProviderFactory
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
    OfficialFinancialLineageKind,
    OfficialWebDocumentCapture,
    PointInTimeMetadata,
    PointInTimeStatus,
    SourceClass,
    SourceDocument,
    SourceSnapshot,
)


@dataclass(frozen=True, slots=True)
class OfficialFinancialReport:
    document: SourceDocument
    index_snapshot: SourceSnapshot
    lineage_kind: OfficialFinancialLineageKind
    lineage_snapshot_ids: list[str]
    exhaustive_proof_allowed: bool
    snapshot: SourceSnapshot
    pit: PointInTimeMetadata
    pages: list[DocumentPage]
    supersedes_document_id: str | None


@dataclass(frozen=True, slots=True)
class _OfficialFinancialCandidate:
    document: SourceDocument
    snapshot: SourceSnapshot
    index_snapshot: SourceSnapshot
    lineage_kind: OfficialFinancialLineageKind
    lineage_snapshots: tuple[SourceSnapshot, ...]
    exhaustive_proof_allowed: bool
    declared_supersedes: str | None


class OfficialFinancialReportService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        fixture_path: Path,
        provider_factory: ProviderFactory,
    ) -> None:
        self.state = state
        self.objects = objects
        self.fixture_path = fixture_path.resolve()
        self.provider_factory = provider_factory
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
        if live:
            local_candidates = self._captured_candidates(company_id, period_end, period_type)
            try:
                remote_candidates = self._live_candidates(
                    company_id,
                    market,
                    period_end,
                    period_type,
                )
            except (AStockError, OSError, RuntimeError, ValueError):
                if not local_candidates:
                    raise
                remote_candidates = []
            candidates = _deduplicate_candidates([*local_candidates, *remote_candidates])
        else:
            candidates = self._recorded_candidates(company_id, period_end, period_type)
        selection_cutoff = as_of
        if live and allow_live_capture_after_cutoff and candidates:
            selection_cutoff = max(
                as_of,
                datetime.now(UTC),
                *(item.snapshot.available_to_system_at for item in candidates),
                *(
                    snapshot.available_to_system_at
                    for item in candidates
                    for snapshot in item.lineage_snapshots
                ),
            )
        eligible = [
            item
            for item in candidates
            if item.document.published_at <= selection_cutoff
            and item.snapshot.available_to_system_at <= selection_cutoff
            and all(
                snapshot.available_to_system_at <= selection_cutoff
                for snapshot in item.lineage_snapshots
            )
        ]
        if not eligible:
            return None
        previous_source: str | None = None
        reports: list[OfficialFinancialReport] = []
        for candidate in sorted(
            eligible,
            key=lambda item: (item.document.published_at, item.document.document_id),
        ):
            document = candidate.document
            snapshot = candidate.snapshot
            index_snapshot = candidate.index_snapshot
            supersedes = candidate.declared_supersedes or previous_source
            if supersedes is not None and self.documents.get_model(supersedes) is None:
                supersedes = None
            self.documents.register(document, snapshot)
            canonical_snapshot = self.documents.snapshot(snapshot.snapshot_id)
            if canonical_snapshot is None:
                raise ValueError("Official financial snapshot registration is incomplete")
            snapshot = canonical_snapshot
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
                    lineage_kind=candidate.lineage_kind,
                    lineage_snapshot_ids=[
                        item.snapshot_id for item in candidate.lineage_snapshots
                    ],
                    exhaustive_proof_allowed=candidate.exhaustive_proof_allowed,
                    snapshot=snapshot,
                    pit=pit,
                    pages=[page for page in pages if page is not None],
                    supersedes_document_id=supersedes,
                )
            )
            previous_source = document.document_id
        return reports[-1]

    def _captured_candidates(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
    ) -> list[_OfficialFinancialCandidate]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,schema_version,object_hash,input_hashes_json "
                "FROM artifact_registry WHERE type='OfficialWebDocumentCapture' "
                "ORDER BY created_at,artifact_id"
            ).fetchall()
        result: list[_OfficialFinancialCandidate] = []
        for row in rows:
            object_hash = str(row["object_hash"])
            if not self.objects.verify(object_hash):
                raise ValueError("Official Web capture artifact object is corrupt")
            try:
                capture = OfficialWebDocumentCapture.model_validate_json(
                    self.objects.get_bytes(object_hash)
                )
                input_hashes = json.loads(str(row["input_hashes_json"]))
            except (AStockError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("Official Web capture artifact is invalid") from exc
            if (
                str(row["artifact_id"])
                != f"OfficialWebDocumentCapture:{capture.capture_id}"
                or str(row["schema_version"]) != capture.schema_version
                or capture.requested_capability != "financial.official_document"
                or not capture.formal_eligible
                or capture.exhaustive_proof_allowed
            ):
                continue
            document = self.documents.get_model(capture.document_id)
            snapshot = self.documents.snapshot(capture.snapshot_id)
            admission_snapshot = self.state.get_snapshot(capture.admission_snapshot_id)
            pit = self.pit.repository.get(capture.pit_id)
            if (
                document is None
                or snapshot is None
                or admission_snapshot is None
                or pit is None
                or snapshot.object_sha256 != capture.object_sha256
                or not self.objects.verify(snapshot.object_sha256)
                or not self.objects.verify(admission_snapshot.object_sha256)
                or input_hashes
                != [snapshot.object_sha256, admission_snapshot.object_sha256]
            ):
                raise ValueError("Official Web capture lineage is incomplete")
            try:
                admission = json.loads(
                    self.objects.get_bytes(admission_snapshot.object_sha256)
                )
            except (AStockError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("Official Web admission snapshot is invalid") from exc
            proposal_payload = admission.get("proposal") if isinstance(admission, dict) else None
            decision_payload = admission.get("decision") if isinstance(admission, dict) else None
            if (
                not isinstance(admission, dict)
                or not isinstance(proposal_payload, dict)
                or not isinstance(decision_payload, dict)
                or admission.get("schema_version") != "official-web-admission-v1"
                or admission.get("document_id") != document.document_id
                or admission.get("document_snapshot_id") != snapshot.snapshot_id
                or admission.get("document_object_sha256") != snapshot.object_sha256
                or admission.get("exhaustive_proof_allowed") is not False
                or proposal_payload.get("requested_capability")
                != "financial.official_document"
                or proposal_payload.get("candidate_url") != document.source_url
                or proposal_payload.get("formal_use") is not True
                or proposal_payload.get("require_complete") is not False
                or decision_payload.get("requested_capability")
                != "financial.official_document"
                or decision_payload.get("allowed") is not True
                or decision_payload.get("source_id") != capture.source_id
                or decision_payload.get("formal_eligible") is not True
                or decision_payload.get("exhaustive_proof_allowed") is not False
                or decision_payload.get("admission_status") != "ADMIT_AFTER_SNAPSHOT"
                or capture.source_class is not SourceClass.PRIMARY_OFFICIAL_WEB
                or document.publisher != capture.source_id
                or snapshot.source_id != capture.source_id
                or admission_snapshot.source_id != f"{capture.source_id}:admission"
                or pit.source_document_id != document.document_id
                or pit.source_snapshot_id != snapshot.snapshot_id
                or pit.period_end != period_end
                or pit.point_in_time_status is not PointInTimeStatus.DOCUMENT_RECONSTRUCTED
                or pit.availability_basis is not AvailabilityBasis.FETCH_OBSERVED
                or pit.available_to_system_at != snapshot.available_to_system_at
                or company_id not in document.company_ids
                or document.document_type is not _document_type(period_type)
                or not _exact_report_title(document.title, period_end, period_type)
                or document.source_url != str(capture.source_url)
                or snapshot.source_url != document.source_url
                or admission_snapshot.source_url != document.source_url
                or snapshot.fetch_status is not FetchStatus.SUCCEEDED
                or admission_snapshot.fetch_status is not FetchStatus.SUCCEEDED
            ):
                continue
            result.append(
                _OfficialFinancialCandidate(
                    document=document,
                    snapshot=snapshot,
                    index_snapshot=admission_snapshot,
                    lineage_kind=(
                        OfficialFinancialLineageKind.OFFICIAL_WEB_EXACT_ITEM_ADMISSION
                    ),
                    lineage_snapshots=(admission_snapshot,),
                    exhaustive_proof_allowed=False,
                    declared_supersedes=None,
                )
            )
        return result

    def _recorded_candidates(
        self,
        company_id: str,
        period_end: date,
        period_type: FinancialPeriodType,
    ) -> list[_OfficialFinancialCandidate]:
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
                _OfficialFinancialCandidate(
                    document=document,
                    snapshot=snapshot,
                    index_snapshot=index_snapshot,
                    lineage_kind=OfficialFinancialLineageKind.RECORDED_EXACT_ITEM_FIXTURE,
                    lineage_snapshots=(index_snapshot,),
                    exhaustive_proof_allowed=False,
                    declared_supersedes=f"cninfo:{supersedes}" if supersedes else None,
                )
            )
        return result

    def _live_candidates(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        period_type: FinancialPeriodType,
    ) -> list[_OfficialFinancialCandidate]:
        if market is Market.BJSE:
            return []
        exchange = DisclosureExchange.SSE if market is Market.XSHG else DisclosureExchange.SZSE
        category = {
            FinancialPeriodType.ANNUAL: DisclosureCategory.ANNUAL_REPORT,
            FinancialPeriodType.SEMIANNUAL: DisclosureCategory.SEMIANNUAL_REPORT,
            FinancialPeriodType.QUARTERLY: DisclosureCategory.QUARTERLY_REPORT,
        }[period_type]
        provider = self.provider_factory.create_for_capability(
            "disclosure.enumerate",
            DisclosureEnumerationProvider,
            formal_use=True,
            require_complete=True,
        )
        batches = provider.search_all(
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
        matched = []
        lineage_snapshots: list[SourceSnapshot] = []
        for batch in batches:
            index_snapshot = self.state.get_snapshot(batch.raw_snapshot_id)
            if index_snapshot is None:
                raise ValueError("Official financial search index snapshot is missing")
            lineage_snapshots.append(index_snapshot)
            matched.extend(
                (announcement, index_snapshot)
                for announcement in batch.announcements
                if _exact_report_title(announcement.title, period_end, period_type)
            )
        lineage = tuple(lineage_snapshots)
        result: list[_OfficialFinancialCandidate] = []
        previous: str | None = None
        for announcement, index_snapshot in sorted(
            matched,
            key=lambda item: (item[0].published_at, item[0].announcement_id),
        ):
            downloaded = provider.download(announcement)
            result.append(
                _OfficialFinancialCandidate(
                    document=downloaded.document,
                    snapshot=downloaded.snapshot,
                    index_snapshot=index_snapshot,
                    lineage_kind=(
                        OfficialFinancialLineageKind.CNINFO_EXHAUSTIVE_ENUMERATION
                    ),
                    lineage_snapshots=lineage,
                    exhaustive_proof_allowed=True,
                    declared_supersedes=previous,
                )
            )
            previous = downloaded.document.document_id
        return result


def _deduplicate_candidates(
    candidates: list[_OfficialFinancialCandidate],
) -> list[_OfficialFinancialCandidate]:
    result: list[_OfficialFinancialCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        identity = (candidate.document.source_url, candidate.snapshot.object_sha256)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(candidate)
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
