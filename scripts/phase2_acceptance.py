"""Run and persist the complete Phase 2 acceptance evidence without private text output."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from astock.acceptance import run_controlled_document_benchmark
from astock.books import BookRepository
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentPageRepository, DocumentRepository
from astock.pit import PointInTimeRepository
from astock.schemas import BookParseReport, PointInTimeStatus
from astock.settings import ProjectPaths


def main() -> int:
    paths = ProjectPaths.discover()
    paths.ensure_directories()
    state = StateStore(paths.state_db, paths.root / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    run_started = datetime.now(UTC)
    run_token = run_started.strftime("%Y%m%dT%H%M%S%fZ")
    benchmark = run_controlled_document_benchmark(
        paths.runtime / "acceptance" / "phase2" / run_token,
    )
    official = _official_sample(state, objects)
    private_book = _private_book_sample(state, objects)
    report_body = {
        "schema_version": "1.0",
        "created_at": run_started.isoformat(),
        "phase": "PHASE_2",
        "controlled_benchmark": benchmark,
        "official_sample": official,
        "private_book_sample": private_book,
        "overall_passed": bool(
            benchmark["all_passed"] and official["passed"] and private_book["passed"]
        ),
    }
    report_id = "phase2-acceptance:" + sha256_bytes(canonical_json_bytes(report_body))
    report = {"report_id": report_id, **report_body}
    object_ref = objects.put_json(report)
    state.register_artifact(
        artifact_id=f"Phase2AcceptanceReport:{report_id}",
        artifact_type="Phase2AcceptanceReport",
        schema_version="1.0",
        object_hash=object_ref.sha256,
        input_hashes=[
            str(official.get("snapshot_object_sha256", "")),
            str(private_book.get("file_sha256", "")),
        ],
    )
    output = {
        "report_id": report_id,
        "report_object_sha256": object_ref.sha256,
        "overall_passed": report["overall_passed"],
        "controlled_document_count": benchmark["document_count"],
        "metrics": benchmark["metrics"],
        "official_sample": official,
        "private_book_sample": private_book,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["overall_passed"] else 1


def _official_sample(state: StateStore, objects: ObjectStore) -> dict[str, object]:
    documents = DocumentRepository(state)
    document_id = "cninfo:1225022887"
    document = documents.get_model(document_id)
    snapshot = documents.latest_snapshot(document_id)
    pit = PointInTimeRepository(state).get_by_source(document_id)
    page_rows = DocumentPageRepository(state).page_rows(document_id)
    page_objects_valid = bool(page_rows) and all(
        objects.verify(str(row["text_object_hash"])) for row in page_rows
    )
    passed = bool(
        document is not None
        and snapshot is not None
        and objects.verify(snapshot.object_sha256)
        and pit is not None
        and pit.point_in_time_status is PointInTimeStatus.DOCUMENT_RECONSTRUCTED
        and page_objects_valid
    )
    return {
        "passed": passed,
        "document_id": document_id,
        "snapshot_object_sha256": snapshot.object_sha256 if snapshot else None,
        "parsed_page_record_count": len(page_rows),
        "pit_status": pit.point_in_time_status.value if pit else None,
    }


def _private_book_sample(state: StateStore, objects: ObjectStore) -> dict[str, object]:
    source_id = "book:mr-dang:value-investing-method"
    with state.connect() as connection:
        manifest_row = connection.execute(
            "SELECT manifest_json FROM book_source_manifest WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    if manifest_row is None:
        return {"passed": False, "source_id": source_id, "reason": "MANIFEST_NOT_FOUND"}
    manifest = BookRepository(state).get_manifest_version(source_id, "v1-local-2026-07-13")
    if manifest is None:
        return {"passed": False, "source_id": source_id, "reason": "VERSION_NOT_FOUND"}
    with state.connect() as connection:
        report_row = connection.execute(
            "SELECT report_json FROM book_parse_report WHERE manifest_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (manifest.manifest_id,),
        ).fetchone()
    parse = BookParseReport.model_validate_json(report_row["report_json"]) if report_row else None
    pages_valid = bool(parse) and all(
        objects.verify(page.text_object_sha256) for page in parse.pages
    )
    passed = bool(
        objects.verify(manifest.raw_object_sha256)
        and parse is not None
        and parse.requested_pages == [1, 125, 249]
        and parse.processed_page_count == 3
        and parse.failed_page_count == 0
        and pages_valid
    )
    return {
        "passed": passed,
        "source_id": source_id,
        "manifest_id": manifest.manifest_id,
        "file_sha256": manifest.file_sha256,
        "source_page_count": manifest.source_page_count,
        "sample_pages": parse.requested_pages if parse else [],
        "processed_page_count": parse.processed_page_count if parse else 0,
        "native_page_count": parse.native_page_count if parse else 0,
        "ocr_page_count": parse.ocr_page_count if parse else 0,
        "failed_page_count": parse.failed_page_count if parse else 0,
        "processing_status": parse.processing_status.value if parse else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
