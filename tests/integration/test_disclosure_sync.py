from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from astock.core.object_store import ObjectStore
from astock.documents import CninfoDisclosureProvider, DisclosureSyncService, DocumentRepository
from astock.schemas import DisclosureCategory, DisclosureExchange, DisclosureSearchRequest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "documents" / "cninfo_annual_000001.json"


def test_disclosure_sync_is_recoverable_and_idempotent(tmp_path: Path, state) -> None:
    raw_index = FIXTURE.read_bytes()
    pdf_bytes = b"%PDF-1.7\n% integration fixture\n%%EOF\n"

    def handler(http_request: httpx.Request) -> httpx.Response:
        content = raw_index if http_request.method == "POST" else pdf_bytes
        mime = "application/json" if http_request.method == "POST" else "application/pdf"
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": mime},
            request=http_request,
        )

    objects = ObjectStore(tmp_path / "objects")
    provider = CninfoDisclosureProvider(
        objects,
        state,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    repository = DocumentRepository(state)
    service = DisclosureSyncService(provider, repository, state)
    request = DisclosureSearchRequest(
        symbol="000001",
        exchange=DisclosureExchange.SZSE,
        start_date=date(2025, 1, 1),
        end_date=date(2026, 7, 13),
        category=DisclosureCategory.ANNUAL_REPORT,
    )
    first = service.sync(request)
    second = service.sync(request)
    assert first.downloaded[0].snapshot.object_sha256 == second.downloaded[0].snapshot.object_sha256
    stored = repository.get("cninfo:1225022887")
    assert stored is not None
    assert stored["company_ids"] == ["000001"]
    assert len(first.pit_metadata_ids) == 1
    assert first.pit_metadata_ids == second.pit_metadata_ids
    assert first.downloaded[0].pit_metadata is not None
    assert first.downloaded[0].pit_metadata.point_in_time_status.value == "DOCUMENT_RECONSTRUCTED"
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_document").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM document_snapshot").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM point_in_time_metadata").fetchone()[0] == 1
        statuses = connection.execute(
            "SELECT status FROM job WHERE type='disclosure-sync' ORDER BY created_at"
        ).fetchall()
    assert [row["status"] for row in statuses] == ["SUCCEEDED", "SUCCEEDED"]
