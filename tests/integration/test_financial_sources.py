from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence import EvidenceRepository
from astock.financial_sources import FinancialSourceParquetStore, FinancialSourceService
from astock.financial_sources.official import _exact_report_title
from astock.financial_sources.service import _parse_provider
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.providers.financial_base import (
    FinancialProviderPayload,
    FinancialRawCaptureError,
)
from astock.schemas import (
    DocumentPage,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialSourceReleaseStatus,
    Market,
    RunStatus,
    SourceSnapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PERIOD_END = date(2025, 12, 31)
ORIGINAL_AS_OF = datetime(2026, 7, 22, 12, tzinfo=UTC)
CORRECTION_AS_OF = datetime(2026, 7, 23, 12, tzinfo=UTC)


def _service(
    tmp_path: Path, instrument_market: Market = Market.XSHE
) -> FinancialSourceService:
    state = StateStore(tmp_path / "状态.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "对象" / "sha256")
    parquet_root = tmp_path / "数据" / "parquet"
    instrument_report = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(parquet_root),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    ).sync_instruments(instrument_market)
    assert instrument_report.release_id is not None
    return FinancialSourceService(
        state,
        objects,
        FinancialSourceParquetStore(parquet_root / "financial_sources"),
        PROJECT_ROOT,
    )


def _payload_at(
    service: FinancialSourceService,
    payload: FinancialProviderPayload,
    available_at: datetime,
) -> FinancialProviderPayload:
    original = payload.snapshots[0]
    request_hash = content_hash(
        {"old": original.snapshot_id, "available_at": available_at}
    )
    snapshot = SourceSnapshot(
        created_at=available_at,
        snapshot_id=f"test-financial:{request_hash}",
        source_id=f"{payload.provider_id}:{request_hash}",
        object_sha256=original.object_sha256,
        fetched_at=available_at,
        available_to_system_at=available_at,
        source_url=original.source_url,
        mime=original.mime,
        byte_size=original.byte_size,
        headers_hash=original.headers_hash,
        fetch_status=original.fetch_status,
        rights_status=original.rights_status,
    )
    service.state.register_snapshot(snapshot)
    return FinancialProviderPayload(
        payload.provider_id,
        payload.request_company_id,
        payload.request_market,
        payload.request_period_end,
        payload.tables,
        {statement: snapshot for statement in payload.snapshots_by_statement},
        {statement: request_hash for statement in payload.request_hashes_by_statement},
    )


def _replace_page_text(
    service: FinancialSourceService, page: DocumentPage, changed_text: str
) -> None:
    text_ref = service.objects.put_bytes(changed_text.encode("utf-8"))
    changed = page.model_copy(
        update={
            "text_object_sha256": text_ref.sha256,
            "text_sha256": sha256_bytes(changed_text.encode("utf-8")),
            "text_char_count": len(changed_text),
        }
    )
    page_json = canonical_json_bytes(changed.model_dump(mode="json")).decode("utf-8")
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE document_page SET text_object_hash=?,text_sha256=?,"
            "text_char_count=?,page_manifest_hash=?,page_json=? WHERE page_id=?",
            (
                text_ref.sha256,
                changed.text_sha256,
                changed.text_char_count,
                content_hash(changed),
                page_json,
                page.page_id,
            ),
        )


def test_recorded_financial_source_reaches_existing_audit_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert report.status is FinancialSourceReleaseStatus.CERTIFIED
    assert report.coverage.certified_fact_count == 18
    assert report.provider_ids == ["eastmoney-financial"]
    assert service.status(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL
    )["status"] == "AVAILABLE"

    pack = service.run_audit(
        "000001",
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
    )
    assert pack.status is RunStatus.SUCCEEDED
    assert len(pack.verified_numbers) == 18
    assert not pack.evidence_gaps
    assert len(pack.source_snapshot_ids) == 1
    assert len(pack.pit_ids) == 1


def test_as_of_excludes_late_pdf_and_preserves_revision_chain(tmp_path: Path) -> None:
    service = _service(tmp_path)
    unavailable = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=datetime(2026, 7, 22, 9, 5, tzinfo=UTC),
    )
    assert unavailable.status is FinancialSourceReleaseStatus.NEEDS_INFO
    assert "OFFICIAL_REPORT_NOT_AVAILABLE_AT_AS_OF" in unavailable.reason_codes

    original = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    corrected = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=CORRECTION_AS_OF,
    )
    assert original.release_id != corrected.release_id
    current = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert current is not None
    manifest = service._verified_manifest(current)
    assert manifest.previous_release_id == original.release_id
    assert manifest.supersedes_release_id == original.release_id
    historical = service.repository.get(
        "000001",
        PERIOD_END.isoformat(),
        FinancialPeriodType.ANNUAL.value,
        as_of=ORIGINAL_AS_OF,
    )
    assert historical is not None
    assert historical["release_id"] == original.release_id
    head_before = current["release_id"]
    repeated_original = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert repeated_original.release_id == original.release_id
    head_after = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert head_after is not None
    assert head_after["release_id"] == head_before


def test_explicit_as_of_filters_late_primary_provider(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    primary = service.eastmoney.fetch("000001", Market.XSHE, PERIOD_END)
    late = _payload_at(service, primary, CORRECTION_AS_OF)
    monkeypatch.setattr(service.eastmoney, "fetch", lambda *args, **kwargs: late)
    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert report.status is FinancialSourceReleaseStatus.CERTIFIED
    assert report.provider_ids == ["sina-financial"]
    assert "PROVIDER_SNAPSHOT_LATE:BALANCE_SHEET" in report.reason_codes
    assert late.snapshots[0].snapshot_id not in report.raw_snapshot_ids


def test_live_default_cutoff_includes_completed_capture(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    primary = service.eastmoney.fetch("000001", Market.XSHE, PERIOD_END)
    capture_finished = datetime.now(UTC) + timedelta(seconds=1)
    completed = _payload_at(service, primary, capture_finished)
    monkeypatch.setattr(service.eastmoney, "fetch", lambda *args, **kwargs: completed)
    original_get = service.official.get
    observed_cutoffs: list[datetime] = []

    def recorded_official(*args, **kwargs):
        observed_cutoffs.append(kwargs["as_of"])
        assert kwargs["allow_live_capture_after_cutoff"] is True
        kwargs["live"] = False
        kwargs["allow_live_capture_after_cutoff"] = False
        return original_get(*args, **kwargs)

    monkeypatch.setattr(service.official, "get", recorded_official)
    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        live=True,
    )
    assert report.status is FinancialSourceReleaseStatus.CERTIFIED
    assert observed_cutoffs[0] >= capture_finished
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    assert manifest.available_to_system_at >= capture_finished


def test_instrument_market_binding_and_official_index_lineage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="instrument"):
        service.sync(
            "000001",
            Market.XSHG,
            PERIOD_END,
            FinancialPeriodType.ANNUAL,
            as_of=ORIGINAL_AS_OF,
        )
    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None and report.release_id == row["release_id"]
    manifest = service._verified_manifest(row)
    assert manifest.instrument_id == "XSHE:000001"
    assert manifest.instrument_type.value == "STOCK"
    assert service.state.get_snapshot(manifest.official_index_snapshot_id) is not None
    assert manifest.official_index_snapshot_id != manifest.official_snapshot_id


def test_bjse_live_path_is_explicitly_blocked(tmp_path: Path) -> None:
    service = _service(tmp_path, Market.BJSE)
    report = service.sync(
        "920015",
        Market.BJSE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        live=True,
    )
    assert report.status is FinancialSourceReleaseStatus.NEEDS_INFO
    assert report.reason_codes == ["BJSE_OFFICIAL_FINANCIAL_REPORT_BLOCKED"]


def test_common_first_and_third_quarter_titles_are_exactly_recognized() -> None:
    assert _exact_report_title(
        "示例股份2025年第一季度报告",
        date(2025, 3, 31),
        FinancialPeriodType.QUARTERLY,
    )
    assert _exact_report_title(
        "示例股份2025年三季度报告（修订）",
        date(2025, 9, 30),
        FinancialPeriodType.QUARTERLY,
    )
    assert not _exact_report_title(
        "示例股份2025年第二季度报告",
        date(2025, 9, 30),
        FinancialPeriodType.QUARTERLY,
    )


def test_pdf_evidence_covers_table_period_unit_subject_and_value(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    facts = service.parquet.read_facts(manifest.certified_files[0])
    shares = next(fact for fact in facts if fact.field_code.value == "SHARES_OUTSTANDING")
    evidence = EvidenceRepository(service.state).get_evidence(shares.evidence_ids[0])
    assert evidence is not None
    excerpt = service.objects.get_bytes(evidence.excerpt_object_sha256).decode("utf-8")
    assert "合并资产负债表" in excerpt
    assert "2025年12月31日" in excerpt
    assert "币种：人民币" in excerpt and "单位：万元" in excerpt
    assert "期末普通股股份总数（股） 19405918198" in excerpt


@pytest.mark.parametrize("duplicate", ["2025年12月31日", "单位：万元"])
def test_pdf_column_or_unit_ambiguity_blocks_affected_statement(
    tmp_path: Path, duplicate: str
) -> None:
    service = _service(tmp_path)
    official = service.official.get(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
        live=False,
    )
    assert official is not None
    page = official.pages[0]
    text = service.objects.get_bytes(page.text_object_sha256).decode("utf-8")
    changed_text = text.replace(duplicate, f"{duplicate}\n{duplicate}", 1)
    text_ref = service.objects.put_bytes(changed_text.encode("utf-8"))
    changed = page.model_copy(
        update={
            "text_object_sha256": text_ref.sha256,
            "text_sha256": sha256_bytes(changed_text.encode("utf-8")),
            "text_char_count": len(changed_text),
        }
    )
    page_json = canonical_json_bytes(changed.model_dump(mode="json")).decode("utf-8")
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE document_page SET text_object_hash=?,text_sha256=?,"
            "text_char_count=?,page_manifest_hash=?,page_json=? WHERE page_id=?",
            (
                text_ref.sha256,
                changed.text_sha256,
                changed.text_char_count,
                content_hash(changed),
                page_json,
                page.page_id,
            ),
        )
    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert "OFFICIAL_VALUE_NOT_FOUND:TOTAL_ASSETS" in report.reason_codes
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    facts = service.parquet.read_facts(manifest.certified_files[0])
    assert all(fact.statement_type.value != "BALANCE_SHEET" for fact in facts)


def test_pdf_value_in_parent_company_table_is_not_certified(tmp_path: Path) -> None:
    service = _service(tmp_path)
    official = service.official.get(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
        live=False,
    )
    assert official is not None
    page = official.pages[0]
    text = service.objects.get_bytes(page.text_object_sha256).decode("utf-8")
    changed_text = text.replace("资产总计 1000", "", 1).rstrip()
    changed_text += "\n\n母公司资产负债表\n\n资产总计 1000\n"
    _replace_page_text(service, page, changed_text)

    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert "OFFICIAL_VALUE_NOT_FOUND:TOTAL_ASSETS" in report.reason_codes
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    facts = service.parquet.read_facts(manifest.certified_files[0])
    assert all(fact.field_code.value != "TOTAL_ASSETS" for fact in facts)


def test_pdf_other_period_column_blocks_affected_statement(tmp_path: Path) -> None:
    service = _service(tmp_path)
    official = service.official.get(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
        live=False,
    )
    assert official is not None
    page = official.pages[0]
    text = service.objects.get_bytes(page.text_object_sha256).decode("utf-8")
    changed_text = text.replace(
        "2025年12月31日",
        "2025年12月31日\n\n2024年12月31日",
        1,
    )
    _replace_page_text(service, page, changed_text)

    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert "OFFICIAL_VALUE_NOT_FOUND:TOTAL_ASSETS" in report.reason_codes
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    facts = service.parquet.read_facts(manifest.certified_files[0])
    assert all(fact.statement_type.value != "BALANCE_SHEET" for fact in facts)


def test_legal_parent_and_foreign_currency_subjects_do_not_reject_consolidated_table(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    official = service.official.get(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
        live=False,
    )
    assert official is not None
    page = official.pages[0]
    text = service.objects.get_bytes(page.text_object_sha256).decode("utf-8")
    changed_text = text.replace(
        "所有者权益合计 400",
        "所有者权益合计 400\n\n"
        "归属于母公司所有者权益合计 400\n\n"
        "外币报表折算差额 0",
        1,
    )
    _replace_page_text(service, page, changed_text)

    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert report.status is FinancialSourceReleaseStatus.CERTIFIED
    assert report.coverage.certified_fact_count == 18


def test_cross_check_and_fallback_remain_secondary_hints(tmp_path: Path, monkeypatch) -> None:
    cross_checked = _service(tmp_path / "交叉核验")
    report = cross_checked.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
        cross_check=True,
    )
    assert report.status is FinancialSourceReleaseStatus.CERTIFIED
    assert report.provider_ids == ["eastmoney-financial", "sina-financial"]
    assert "SECONDARY_PROVIDER_CONFLICT:REVENUE" in report.reason_codes

    fallback = _service(tmp_path / "回退")

    def fail_primary(*args, **kwargs):
        raise FinancialRawCaptureError("RECORDED_FAILURE", [])

    monkeypatch.setattr(fallback.eastmoney, "fetch", fail_primary)
    fallback_report = fallback.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert fallback_report.status is FinancialSourceReleaseStatus.CERTIFIED
    assert fallback_report.provider_ids == ["sina-financial"]
    assert "SINA_FALLBACK_USED" in fallback_report.reason_codes


def test_recorded_provider_uses_raw_native_envelope_and_request_identity(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = service.eastmoney.fetch("000001", Market.XSHE, PERIOD_END)
    raw = service.objects.get_bytes(first.snapshots[0].object_sha256)
    payload = json.loads(raw)
    assert set(payload) == {
        "schema_version",
        "available_to_system_at",
        "responses",
    }
    assert "tables" not in payload
    assert set(payload["responses"]) == {
        "BALANCE_SHEET",
        "INCOME_STATEMENT",
        "CASH_FLOW_STATEMENT",
    }
    second = service.eastmoney.fetch("000002", Market.XSHE, PERIOD_END)
    assert second.snapshots[0].object_sha256 == first.snapshots[0].object_sha256
    assert second.snapshots[0].snapshot_id != first.snapshots[0].snapshot_id
    assert second.request_company_id == "000002"


def test_live_normalization_failure_reports_persisted_raw_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    captured: list[SourceSnapshot] = []

    def malformed_capture(url: str, *, params: dict, request_context: dict):
        payload = {
            "success": True,
            "result": {
                "data": [
                    {
                        "SECURITY_CODE": "000001",
                        "REPORT_DATE": PERIOD_END.isoformat(),
                    }
                ]
            },
        }
        raw = canonical_json_bytes(payload)
        observed_at = datetime.now(UTC)
        snapshot = service.eastmoney._persist(
            raw,
            source_url=url,
            content_type="application/json",
            observed_at=observed_at,
            request={"url": url, "params": params, "context": request_context},
        )
        captured.append(snapshot)
        return payload, snapshot

    def fail_backup(*args, **kwargs):
        raise FinancialRawCaptureError("RECORDED_FAILURE", [])

    monkeypatch.setattr(service.eastmoney, "_capture_json", malformed_capture)
    monkeypatch.setattr(service.sina, "fetch", fail_backup)
    report = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        live=True,
    )
    assert report.status is FinancialSourceReleaseStatus.FAILED
    assert len(captured) == 1
    assert report.raw_snapshot_ids == [captured[0].snapshot_id]
    assert service.state.get_snapshot(captured[0].snapshot_id) is not None
    assert service.objects.verify(captured[0].object_sha256)


def test_observation_identity_includes_request_and_instrument_lineage(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    payload = service.eastmoney.fetch("000001", Market.XSHE, PERIOD_END)
    binding = service.instruments.resolve(
        "000001", Market.XSHE, as_of=ORIGINAL_AS_OF
    )
    baseline, _ = _parse_provider(
        payload,
        service.mappings,
        "000001",
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        binding,
        as_of=ORIGINAL_AS_OF,
    )
    changed_request = replace(
        payload,
        request_hashes_by_statement={
            statement: "f" * 64 for statement in payload.request_hashes_by_statement
        },
    )
    request_observations, _ = _parse_provider(
        changed_request,
        service.mappings,
        "000001",
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        binding,
        as_of=ORIGINAL_AS_OF,
    )
    changed_binding = replace(
        binding,
        release_id="1" * 64,
        manifest_artifact_id=f"market-reference:{'1' * 64}",
        manifest_object_hash="2" * 64,
        content_hash="3" * 64,
    )
    instrument_observations, _ = _parse_provider(
        payload,
        service.mappings,
        "000001",
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        changed_binding,
        as_of=ORIGINAL_AS_OF,
    )
    baseline_ids = {item.observation_id for item in baseline}
    assert baseline_ids.isdisjoint(
        item.observation_id for item in request_observations
    )
    assert baseline_ids.isdisjoint(
        item.observation_id for item in instrument_observations
    )


def test_parquet_tamper_is_reported_as_corrupt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    descriptor = manifest.certified_files[0]
    (service.parquet.root / descriptor.path).write_bytes(b"tampered")
    status = service.status("000001", PERIOD_END, FinancialPeriodType.ANNUAL)
    assert status["status"] == "CORRUPT"
    assert service.audit()["status"] == "FAIL"
    with pytest.raises((OSError, ValueError)):
        service.run_audit(
            "000001",
            PERIOD_END,
            FinancialPeriodType.ANNUAL,
            as_of=ORIGINAL_AS_OF,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        )


def test_pit_corruption_is_not_downgraded_to_needs_info(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    manifest = service._verified_manifest(row)
    with service.state.transaction() as connection:
        connection.execute(
            "UPDATE point_in_time_metadata SET pit_json='not-json' WHERE pit_id=?",
            (manifest.official_pit_id,),
        )
    with pytest.raises(ValueError):
        service.run_audit(
            "000001",
            PERIOD_END,
            FinancialPeriodType.ANNUAL,
            as_of=ORIGINAL_AS_OF,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        )


def test_manifest_corruption_is_not_downgraded_to_needs_info(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    row = service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    )
    assert row is not None
    service.objects.path_for(str(row["manifest_object_hash"])).write_bytes(b"tampered")
    assert service.status(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL
    )["status"] == "CORRUPT"
    with pytest.raises(StorageError):
        service.run_audit(
            "000001",
            PERIOD_END,
            FinancialPeriodType.ANNUAL,
            as_of=ORIGINAL_AS_OF,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        )


def test_publish_crash_leaves_no_head_and_retry_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    publish = service.repository.publish

    def crash(*args, **kwargs):
        raise RuntimeError("simulated publish crash")

    monkeypatch.setattr(service.repository, "publish", crash)
    with pytest.raises(RuntimeError, match="simulated publish crash"):
        service.sync(
            "000001",
            Market.XSHE,
            PERIOD_END,
            FinancialPeriodType.ANNUAL,
            as_of=ORIGINAL_AS_OF,
        )
    assert service.repository.get(
        "000001", PERIOD_END.isoformat(), FinancialPeriodType.ANNUAL.value
    ) is None
    monkeypatch.setattr(service.repository, "publish", publish)
    retry = service.sync(
        "000001",
        Market.XSHE,
        PERIOD_END,
        FinancialPeriodType.ANNUAL,
        as_of=ORIGINAL_AS_OF,
    )
    assert retry.status is FinancialSourceReleaseStatus.CERTIFIED


def test_financial_source_cli_sync_status_and_audit(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "命令行运行时"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    runner = CliRunner()
    instrument = runner.invoke(app, ["sync-instruments", "--market", "XSHE"])
    assert instrument.exit_code == 0, instrument.output
    synced = runner.invoke(
        app,
        [
            "sync-financial",
            "000001",
            "--market",
            "XSHE",
            "--period-end",
            "2025-12-31",
            "--as-of",
            ORIGINAL_AS_OF.isoformat(),
        ],
    )
    assert synced.exit_code == 0, synced.output
    assert '"status": "CERTIFIED"' in synced.output

    status = runner.invoke(
        app,
        ["financial-source-status", "000001", "--period-end", "2025-12-31"],
    )
    assert status.exit_code == 0, status.output
    assert '"status": "AVAILABLE"' in status.output
    audit = runner.invoke(app, ["financial-source-audit"])
    assert audit.exit_code == 0, audit.output
    assert '"status": "PASS"' in audit.output

    invalid = runner.invoke(
        app,
        ["sync-financial", "000001", "--period-end", "not-a-date"],
    )
    assert invalid.exit_code == 2
    assert "FINANCIAL_SOURCE_SYNC_FAILED" in invalid.output
    assert str(runtime) not in invalid.output


def test_financial_source_cli_live_without_as_of_preserves_capture_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(tmp_path / "runtime"))
    captured: dict[str, object] = {}

    def stop_after_capture(
        _service: FinancialSourceService,
        _company_id: str,
        _market: Market,
        _period_end: date,
        _period_type: FinancialPeriodType,
        **kwargs: object,
    ) -> None:
        captured.update(kwargs)
        raise ValueError("stop after argument capture")

    monkeypatch.setattr(FinancialSourceService, "sync", stop_after_capture)
    result = CliRunner().invoke(
        app,
        [
            "sync-financial",
            "000001",
            "--market",
            "XSHE",
            "--period-end",
            "2025-12-31",
            "--live",
        ],
    )

    assert result.exit_code == 2
    assert captured["as_of"] is None
    assert captured["live"] is True
