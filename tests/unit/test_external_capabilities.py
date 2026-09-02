from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from astock.cli import app
from astock.core.object_store import ObjectStore
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.external_capabilities import (
    ExternalCapabilityQualificationError,
    ExternalCapabilityService,
    load_external_capability_registry,
)
from astock.monitoring.news import GdeltNewsLeadProvider
from astock.providers.config import load_provider_registry
from astock.schemas.external_capabilities import (
    CapabilityQualificationChecks,
    CapabilityQualificationReport,
    CapabilityRevocation,
    ExternalCapabilityDefinition,
    ExternalCapabilityKind,
    ExternalCapabilityStage,
    QualificationCheckStatus,
    capability_revocation_id,
    qualification_report_id,
)
from astock.schemas.market import (
    AccessTransport,
    CompletenessSemantics,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path) -> tuple[ExternalCapabilityService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return ExternalCapabilityService(PROJECT_ROOT, state, objects), state, objects


def _all_pass() -> CapabilityQualificationChecks:
    return CapabilityQualificationChecks(
        license=QualificationCheckStatus.PASS,
        terms_of_service=QualificationCheckStatus.PASS,
        data_rights=QualificationCheckStatus.PASS,
        pit=QualificationCheckStatus.PASS,
        provenance=QualificationCheckStatus.PASS,
        credential_handling=QualificationCheckStatus.PASS,
        sbom=QualificationCheckStatus.PASS,
        security_review=QualificationCheckStatus.PASS,
        maintenance=QualificationCheckStatus.PASS,
        cost=QualificationCheckStatus.PASS,
        latency=QualificationCheckStatus.PASS,
        cache_behavior=QualificationCheckStatus.PASS,
        offline_behavior=QualificationCheckStatus.PASS,
        failure_behavior=QualificationCheckStatus.PASS,
        exit_uninstall=QualificationCheckStatus.PASS,
    )


def _report(
    service: ExternalCapabilityService,
    objects: ObjectStore,
    *,
    capability_id: str = "akshare",
    at: datetime | None = None,
    stage: ExternalCapabilityStage = ExternalCapabilityStage.PRODUCTION_BACKUP,
    checks: CapabilityQualificationChecks | None = None,
) -> CapabilityQualificationReport:
    now = at or datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    definition = service.definition(capability_id)
    evidence = objects.put_bytes(f"qualification:{capability_id}".encode())
    evidence_hashes = [evidence.sha256]
    resolved_checks = checks or _all_pass()
    expiry = now + timedelta(days=min(30, definition.qualification_validity_days))
    report_id = qualification_report_id(
        capability_id=capability_id,
        candidate_version="fixture-v1",
        requested_stage=stage,
        admitted_stage=stage,
        checks=resolved_checks,
        recorded_validation=QualificationCheckStatus.PASS,
        controlled_live_validation=QualificationCheckStatus.PASS,
        source_class_ceiling=definition.source_class_ceiling,
        completeness_ceiling=definition.completeness_ceiling,
        evidence_object_hashes=evidence_hashes,
        valid_from=now,
        expires_at=expiry,
        reason_codes=[],
    )
    return CapabilityQualificationReport(
        report_id=report_id,
        capability_id=capability_id,
        candidate_version="fixture-v1",
        requested_stage=stage,
        admitted_stage=stage,
        checks=resolved_checks,
        recorded_validation=QualificationCheckStatus.PASS,
        controlled_live_validation=QualificationCheckStatus.PASS,
        source_class_ceiling=definition.source_class_ceiling,
        completeness_ceiling=definition.completeness_ceiling,
        evidence_object_hashes=evidence_hashes,
        valid_from=now,
        expires_at=expiry,
        reason_codes=[],
    )


def test_external_capability_schema_cli_is_registered_and_machine_readable() -> None:
    result = CliRunner().invoke(app, ["external-capability-schema"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"qualification_request", "qualification_report", "revocation"}
    assert payload["qualification_report"]["title"] == "CapabilityQualificationReport"


def test_registry_covers_supported_external_kinds_and_approved_candidates() -> None:
    registry = load_external_capability_registry(
        PROJECT_ROOT / "configs" / "external_capabilities.yaml"
    )
    kinds = {item.kind for item in registry.capabilities}
    assert {
        ExternalCapabilityKind.API,
        ExternalCapabilityKind.PYTHON_LIBRARY,
        ExternalCapabilityKind.MCP,
        ExternalCapabilityKind.CRAWLER,
        ExternalCapabilityKind.SKILL,
    } <= kinds
    ids = {item.capability_id for item in registry.capabilities}
    assert {
        "akshare",
        "arelle",
        "docling",
        "playwright-mcp",
        "crawl4ai",
        "changedetection-io",
        "source-qualification-auditor",
        "report-visual-qa",
        "schema-drift-recorder",
    } <= ids


def test_broker_execution_capability_cannot_be_admitted() -> None:
    with pytest.raises(ValueError, match="permanently rejected"):
        ExternalCapabilityDefinition(
            capability_id="broker-mcp",
            display_name="Broker MCP",
            kind=ExternalCapabilityKind.MCP,
            logical_capabilities=["broker.order.execute"],
            default_stage=ExternalCapabilityStage.SHADOW,
            maximum_stage=ExternalCapabilityStage.PRODUCTION_BACKUP,
            exit_contract="Remove integration.",
        )


def test_production_backup_requires_every_gate_and_both_smokes(tmp_path: Path) -> None:
    service, _state, objects = _service(tmp_path)
    failed_checks = _all_pass().model_copy(update={"license": QualificationCheckStatus.FAIL})
    with pytest.raises(ValueError, match="every qualification check"):
        _report(service, objects, checks=failed_checks)


def test_qualification_expiry_and_revocation_fail_closed(tmp_path: Path) -> None:
    service, _state, objects = _service(tmp_path)
    now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    report = service.register_qualification(_report(service, objects, at=now))
    assert service.production_backup_allowed(
        "akshare", "market.reference.backup", primary_available=False, at=now
    )
    assert not service.production_backup_allowed(
        "akshare", "market.reference.backup", primary_available=True, at=now
    )
    assert service.active_report("akshare", at=report.expires_at) is None

    revoked_at = now + timedelta(hours=1)
    revocation_id = capability_revocation_id(
        capability_id="akshare",
        report_id=report.report_id,
        revoked_at=revoked_at,
        reason="fixture revocation",
    )
    service.revoke(
        CapabilityRevocation(
            revocation_id=revocation_id,
            capability_id="akshare",
            report_id=report.report_id,
            revoked_at=revoked_at,
            reason="fixture revocation",
        )
    )
    assert service.active_report("akshare", at=revoked_at) is None
    qualification_artifact = service.state.artifact_record(
        f"external-capability-qualification:{report.report_id}"
    )
    revocation_artifact = service.state.artifact_record(
        f"external-capability-revocation:{revocation_id}"
    )
    assert qualification_artifact is not None
    assert revocation_artifact is not None
    assert revocation_artifact["input_hashes"] == [qualification_artifact["object_hash"]]

    with service.state.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE external_capability_qualification SET expires_at=? WHERE report_id=?",
                ((report.expires_at + timedelta(days=1)).isoformat(), report.report_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM external_capability_revocation WHERE revocation_id=?",
                (revocation_id,),
            )


def test_qualification_index_drift_cannot_extend_verified_admission(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    report = service.register_qualification(_report(service, objects, at=now))
    after_expiry = report.expires_at + timedelta(hours=1)
    with state.connect() as connection:
        connection.execute("DROP TRIGGER external_capability_qualification_no_update")
        connection.execute(
            "UPDATE external_capability_qualification SET expires_at=? WHERE report_id=?",
            ((after_expiry + timedelta(days=1)).isoformat(), report.report_id),
        )
        connection.commit()

    assert service.active_report("akshare", at=after_expiry) is None
    assert not service.production_backup_allowed(
        "akshare",
        "market.reference.backup",
        primary_available=False,
        at=after_expiry,
    )


def test_missing_evidence_and_authority_elevation_are_rejected(tmp_path: Path) -> None:
    service, _state, objects = _service(tmp_path)
    report = _report(service, objects)
    missing = report.model_copy(update={"evidence_object_hashes": ["0" * 64]})
    missing = missing.model_copy(
        update={
            "report_id": qualification_report_id(
                capability_id=missing.capability_id,
                candidate_version=missing.candidate_version,
                requested_stage=missing.requested_stage,
                admitted_stage=missing.admitted_stage,
                checks=missing.checks,
                recorded_validation=missing.recorded_validation,
                controlled_live_validation=missing.controlled_live_validation,
                source_class_ceiling=missing.source_class_ceiling,
                completeness_ceiling=missing.completeness_ceiling,
                evidence_object_hashes=missing.evidence_object_hashes,
                valid_from=missing.valid_from,
                expires_at=missing.expires_at,
                reason_codes=missing.reason_codes,
            )
        }
    )
    with pytest.raises(ExternalCapabilityQualificationError, match="missing or corrupt"):
        service.register_qualification(missing)

    elevated = report.model_copy(update={"source_class_ceiling": SourceClass.PRIMARY_OFFICIAL_WEB})
    elevated = elevated.model_copy(
        update={
            "report_id": qualification_report_id(
                capability_id=elevated.capability_id,
                candidate_version=elevated.candidate_version,
                requested_stage=elevated.requested_stage,
                admitted_stage=elevated.admitted_stage,
                checks=elevated.checks,
                recorded_validation=elevated.recorded_validation,
                controlled_live_validation=elevated.controlled_live_validation,
                source_class_ceiling=elevated.source_class_ceiling,
                completeness_ceiling=elevated.completeness_ceiling,
                evidence_object_hashes=elevated.evidence_object_hashes,
                valid_from=elevated.valid_from,
                expires_at=elevated.expires_at,
                reason_codes=elevated.reason_codes,
            )
        }
    )
    with pytest.raises(ExternalCapabilityQualificationError, match="authority ceiling"):
        service.register_qualification(elevated)


def _transport(
    source_id: str,
    *,
    available: bool,
    backup: bool = False,
    qualification_valid: bool = True,
) -> TransportCapability:
    return TransportCapability(
        source_id=source_id,
        transport=AccessTransport.API,
        requested_capabilities=["fixture.capability"],
        available=available,
        reason="fixture",
        formal_eligible=False,
        completeness_semantics=CompletenessSemantics.EXACT_ITEM,
        completeness_score=Decimal("1"),
        production_backup=backup,
        external_capability_id="fixture-backup" if backup else None,
        qualification_valid=qualification_valid,
    )


def test_source_router_uses_backup_only_after_standard_route_is_unavailable() -> None:
    router = SourceAccessRouter()
    request = SourceAccessRequest(requested_capability="fixture.capability")
    standard = _transport("primary", available=True)
    backup = _transport("backup", available=True, backup=True)
    assert router.decide(request, [standard, backup]).selected_source_id == "primary"

    unavailable = _transport("primary", available=False)
    assert router.decide(request, [unavailable, backup]).selected_source_id == "backup"

    invalid_backup = _transport("backup", available=True, backup=True, qualification_valid=False)
    assert router.decide(request, [unavailable, invalid_backup]).selected_source_id is None


def test_gdelt_is_provider_registered_lead_only() -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    gdelt = next(item for item in registry.providers if item.provider_id == "gdelt-news-leads")
    assert gdelt.capabilities == ["news.discovery.lead"]
    assert gdelt.formal_capabilities == []
    assert (
        gdelt.completeness_semantics["news.discovery.lead"] is CompletenessSemantics.DISCOVERY_ONLY
    )


def test_gdelt_persists_snapshot_and_opens_capability_breaker_on_429(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "articles": [
                        {
                            "title": "贵州茅台测试新闻",
                            "url": "https://example.com/news/1",
                            "domain": "example.com",
                            "seendate": "20260902T000000Z",
                            "language": "Chinese",
                            "sourcecountry": "China",
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(429, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GdeltNewsLeadProvider(objects, state, client=client)
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
    leads = provider.search(
        names=["贵州茅台"], symbol="600519", start=start, end=end, max_records=10
    )
    assert len(leads) == 1
    snapshot = state.get_snapshot(leads[0].snapshot_id)
    assert snapshot is not None
    assert snapshot.rights_status == "PUBLIC_NEWS_LEAD"

    with pytest.raises(httpx.HTTPStatusError):
        provider.search(names=["贵州茅台"], symbol="600519", start=start, end=end, max_records=10)
    before = calls
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        provider.search(names=["贵州茅台"], symbol="600519", start=start, end=end, max_records=10)
    assert calls == before
