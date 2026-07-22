from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.candidates import (
    CandidateInterrupted,
    CandidateParquetStore,
    CandidateRepository,
    CandidateScanService,
    CandidateTestInputVerifier,
    CandidateVerificationResult,
    ProductionCandidateInputVerifier,
    load_candidate_scan_config,
)
from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import InstrumentRecord, InstrumentType, Market
from astock.schemas.candidates import (
    CandidateAnnouncementEvent,
    CandidateArtifactRole,
    CandidateCheckpointStep,
    CandidateCompanyInput,
    CandidateCoverageStatus,
    CandidateDailyPoint,
    CandidateEvidenceSeverity,
    CandidateFinancialFlag,
    CandidateHoldingChange,
    CandidateHoldingObservation,
    CandidateInputArtifact,
    CandidateInputRelease,
    CandidateLifecycleStatus,
    CandidatePitStatus,
    CandidateQualityStatus,
    CandidateRecord,
    CandidateScanRequest,
    CandidateScanStatus,
    CandidateSignalType,
    CandidateSourceMode,
    CandidateTradability,
    CandidateWatchlistIntent,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def candidate_runtime(tmp_path: Path) -> tuple[CandidateScanService, StateStore, ObjectStore]:
    runtime = tmp_path / "中文候选运行时"
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(runtime / "对象" / "sha256")
    service = CandidateScanService(
        CandidateRepository(state),
        objects,
        CandidateParquetStore(runtime / "数据" / "候选"),
        load_candidate_scan_config(PROJECT_ROOT / "configs" / "candidate_scan.yaml"),
        CandidateTestInputVerifier(objects),
    )
    return service, state, objects


def _release(
    state: StateStore,
    objects: ObjectStore,
    release_id: str,
    as_of: datetime,
    *,
    include_signals: bool = True,
    quality: CandidateQualityStatus = CandidateQualityStatus.PASS,
    coverage: CandidateCoverageStatus = CandidateCoverageStatus.COMPLETE,
    pit: CandidatePitStatus = CandidatePitStatus.CERTIFIED,
    tradability: CandidateTradability = CandidateTradability.TRADABLE,
    market: Market = Market.XSHG,
    symbol: str = "600519",
    instrument_type: InstrumentType = InstrumentType.STOCK,
    source_mode: CandidateSourceMode = CandidateSourceMode.LOCAL,
    company_id_override: str | None = None,
    proven_company_ids: list[str] | None = None,
) -> CandidateInputRelease:
    available_at = as_of - timedelta(hours=1)
    roles = [
        CandidateArtifactRole.INSTRUMENT_TRADABILITY,
        CandidateArtifactRole.TRADING_CALENDAR,
        CandidateArtifactRole.DAILY_LOCAL_VERSIONED,
        CandidateArtifactRole.CORPORATE_ACTION,
        CandidateArtifactRole.DATA_QUALITY,
        CandidateArtifactRole.ANNOUNCEMENT_EVENTS,
        CandidateArtifactRole.FINANCIAL_INTEGRITY,
        CandidateArtifactRole.USER_WATCHLIST,
        CandidateArtifactRole.HOLDING_REVIEW,
    ]
    artifacts: dict[CandidateArtifactRole, CandidateInputArtifact] = {}
    company_id = company_id_override or f"company:{symbol}"
    for role in roles:
        artifact_id = f"upstream:{release_id}:{role.value}"
        evidence_ids = {
            CandidateArtifactRole.ANNOUNCEMENT_EVENTS: [f"evidence:{release_id}:event"],
            CandidateArtifactRole.FINANCIAL_INTEGRITY: [f"evidence:{release_id}:financial"],
            CandidateArtifactRole.HOLDING_REVIEW: [f"evidence:{release_id}:holding"],
        }.get(role, [])
        ref = objects.put_json(
            {
                "artifact_id": artifact_id,
                "release_id": release_id,
                "company_ids": (proven_company_ids or [company_id])
                if role is CandidateArtifactRole.INSTRUMENT_TRADABILITY
                else [],
            }
        )
        state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=f"Fixture{role.value}",
            schema_version="fixture-v1",
            object_hash=ref.sha256,
            input_hashes=[],
        )
        artifacts[role] = CandidateInputArtifact(
            created_at=available_at,
            artifact_id=artifact_id,
            role=role,
            artifact_type=f"Fixture{role.value}",
            artifact_schema_version="fixture-v1",
            dataset_kind=role.value,
            formal_status=pit.value,
            source_family={
                CandidateArtifactRole.ANNOUNCEMENT_EVENTS: "cninfo-announcements",
                CandidateArtifactRole.FINANCIAL_INTEGRITY: "financial-integrity",
                CandidateArtifactRole.HOLDING_REVIEW: "holding-review",
                CandidateArtifactRole.USER_WATCHLIST: "user-watchlist",
            }.get(role, f"fixture-{role.value.lower()}"),
            object_hash=ref.sha256,
            coverage_status=coverage,
            available_to_system_at=available_at,
            pit_status=pit,
            source_snapshot_ids=[f"snapshot:{release_id}:{role.value}"],
            evidence_ids=evidence_ids,
        )
    daily = []
    for offset in range(21):
        observed = as_of - timedelta(days=21 - offset, hours=2)
        daily.append(
            CandidateDailyPoint(
                created_at=available_at,
                session_date=date(2026, 6, 1) + timedelta(days=offset),
                close=(
                    Decimal("100")
                    if offset < 20 or not include_signals
                    else Decimal("120")
                ),
                volume=(
                    Decimal("100")
                    if offset < 20 or not include_signals
                    else Decimal("200")
                ),
                turnover_cny=Decimal("30000000"),
                source_artifact_id=artifacts[
                    CandidateArtifactRole.DAILY_LOCAL_VERSIONED
                ].artifact_id,
                observed_at=observed,
                available_to_system_at=observed + timedelta(minutes=1),
                pit_status=pit,
            )
        )
    event_evidence = [f"evidence:{release_id}:event"]
    financial_evidence = [f"evidence:{release_id}:financial"]
    holding_evidence = [f"evidence:{release_id}:holding"]
    company = CandidateCompanyInput(
        created_at=available_at,
        company_id=company_id,
        instrument_id=f"{market.value}:{symbol}",
        market=market,
        symbol=symbol,
        name=f"样例{symbol}",
        instrument_type=instrument_type,
        tradability=tradability,
        instrument_artifact_id=artifacts[
            CandidateArtifactRole.INSTRUMENT_TRADABILITY
        ].artifact_id,
        calendar_artifact_id=artifacts[CandidateArtifactRole.TRADING_CALENDAR].artifact_id,
        daily_artifact_id=artifacts[
            CandidateArtifactRole.DAILY_LOCAL_VERSIONED
        ].artifact_id,
        corporate_action_artifact_id=artifacts[
            CandidateArtifactRole.CORPORATE_ACTION
        ].artifact_id,
        quality_artifact_id=artifacts[CandidateArtifactRole.DATA_QUALITY].artifact_id,
        announcement_artifact_id=artifacts[
            CandidateArtifactRole.ANNOUNCEMENT_EVENTS
        ].artifact_id,
        financial_artifact_id=artifacts[
            CandidateArtifactRole.FINANCIAL_INTEGRITY
        ].artifact_id,
        quality_status=quality,
        daily_points=daily,
        announcement_events=(
            [
                CandidateAnnouncementEvent(
                    created_at=available_at,
                    event_id=f"event:{release_id}",
                    event_type="MAJOR_CONTRACT",
                    severity=CandidateEvidenceSeverity.HIGH,
                    source_artifact_id=artifacts[
                        CandidateArtifactRole.ANNOUNCEMENT_EVENTS
                    ].artifact_id,
                    observed_at=available_at,
                    available_to_system_at=available_at,
                    pit_status=pit,
                    evidence_ids=event_evidence,
                )
            ]
            if include_signals
            else []
        ),
        financial_flags=(
            [
                CandidateFinancialFlag(
                    created_at=available_at,
                    finding_id=f"finding:{release_id}",
                    severity=CandidateEvidenceSeverity.MEDIUM,
                    evidence_closed=True,
                    source_artifact_id=artifacts[
                        CandidateArtifactRole.FINANCIAL_INTEGRITY
                    ].artifact_id,
                    observed_at=available_at,
                    available_to_system_at=available_at,
                    pit_status=pit,
                    evidence_ids=financial_evidence,
                )
            ]
            if include_signals
            else []
        ),
        watchlist_intents=(
            [
                CandidateWatchlistIntent(
                    created_at=available_at,
                    intent_id=f"intent:{release_id}",
                    source_artifact_id=artifacts[
                        CandidateArtifactRole.USER_WATCHLIST
                    ].artifact_id,
                    observed_at=available_at,
                    available_to_system_at=available_at,
                    pit_status=pit,
                )
            ]
            if include_signals
            else []
        ),
        holding_observations=(
            [
                CandidateHoldingObservation(
                    created_at=available_at,
                    review_id=f"review:{release_id}",
                    change=CandidateHoldingChange.NEW_EVIDENCE,
                    source_artifact_id=artifacts[
                        CandidateArtifactRole.HOLDING_REVIEW
                    ].artifact_id,
                    observed_at=available_at,
                    available_to_system_at=available_at,
                    pit_status=pit,
                    evidence_ids=holding_evidence,
                )
            ]
            if include_signals
            else []
        ),
    )
    return CandidateInputRelease(
        created_at=as_of,
        input_release_id=release_id,
        as_of=as_of,
        source_mode=source_mode,
        artifacts=list(artifacts.values()),
        companies=[company],
        expected_company_ids=[company_id],
        expected_company_count=1,
        company_universe_semantic_hash=content_hash([company_id]),
        coverage_proof_artifact_ids=[
            artifacts[CandidateArtifactRole.INSTRUMENT_TRADABILITY].artifact_id
        ],
    )


def _request(
    service: CandidateScanService,
    release: CandidateInputRelease,
    *,
    live: bool = False,
) -> CandidateScanRequest:
    object_hash = service.stage_input_release(release)
    return CandidateScanRequest(
        created_at=release.as_of,
        request_id=f"request:{release.input_release_id}",
        input_release_id=release.input_release_id,
        input_release_object_hash=object_hash,
        as_of=release.as_of,
        live=live,
    )


def test_candidate_scan_vertical_emits_all_signals_and_audits(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    request = _request(service, _release(state, objects, "release:ready", as_of))

    report = service.scan(request)

    assert report.status is CandidateScanStatus.SUCCEEDED
    scan_status = service.status(scan_id=report.scan_id)
    record = scan_status["records"][0]
    assert record.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY
    manifest_row = service.repository.get_signal_manifest(report.scan_id)
    assert manifest_row is not None
    from astock.schemas.candidates import CandidateSignalManifest

    manifest = CandidateSignalManifest.model_validate_json(
        objects.get_bytes(str(manifest_row["manifest_object_hash"]))
    )
    signals = service.parquet.read_signals(manifest.descriptor)
    assert {item.signal_type for item in signals} == set(CandidateSignalType)
    assert service.audit(report.scan_id).status.value == "PASS"
    assert "中文候选运行时" in str(service.parquet.root)


def test_price_volume_or_watchlist_alone_never_becomes_research_ready(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(state, objects, "release:weak", as_of, include_signals=False)
    company = release.companies[0]
    watch_artifact = next(
        item for item in release.artifacts if item.role is CandidateArtifactRole.USER_WATCHLIST
    )
    company.watchlist_intents = [
        CandidateWatchlistIntent(
            created_at=as_of,
            intent_id="intent:weak",
            source_artifact_id=watch_artifact.artifact_id,
            observed_at=as_of - timedelta(hours=1),
            available_to_system_at=as_of - timedelta(hours=1),
            pit_status=CandidatePitStatus.CERTIFIED,
        )
    ]
    report = service.scan(_request(service, release))
    record = service.status(scan_id=report.scan_id)["records"][0]
    assert record.lifecycle_status is CandidateLifecycleStatus.OBSERVATION
    assert record.strength.value == "WEAK"


def test_quality_fail_disables_technical_support(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        "release:quality-fail",
        as_of,
        include_signals=False,
        quality=CandidateQualityStatus.FAIL,
    )
    report = service.scan(_request(service, release))
    manifest_row = service.repository.get_signal_manifest(report.scan_id)
    assert manifest_row is not None
    from astock.schemas.candidates import CandidateSignalManifest

    manifest = CandidateSignalManifest.model_validate_json(
        objects.get_bytes(str(manifest_row["manifest_object_hash"]))
    )
    signals = service.parquet.read_signals(manifest.descriptor)
    assert not any(item.signal_type is CandidateSignalType.PRICE_VOLUME_CLUE for item in signals)
    liquidity = next(
        item for item in signals if item.signal_type is CandidateSignalType.LIQUIDITY_GATE
    )
    assert liquidity.disposition.value == "GATE_FAIL"
    assert "TECHNICAL_DISABLED_QUALITY_FAIL" in liquidity.reason_codes


def test_partial_release_does_not_close_and_two_complete_misses_do(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    start = datetime(2026, 7, 20, 8, tzinfo=UTC)
    ready = service.scan(_request(service, _release(state, objects, "release:life:0", start)))
    assert ready.status is CandidateScanStatus.SUCCEEDED

    partial_release = _release(
        state,
        objects,
        "release:life:partial",
        start + timedelta(days=1),
        include_signals=False,
        coverage=CandidateCoverageStatus.PARTIAL,
    )
    partial = service.scan(_request(service, partial_release))
    assert partial.status is CandidateScanStatus.NEEDS_INFO
    current = service.status(company_id="company:600519")["record"]
    assert current.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY

    first_miss = service.scan(
        _request(
            service,
            _release(
                state,
                objects,
                "release:life:1",
                start + timedelta(days=2),
                include_signals=False,
            ),
        )
    )
    first_record = service.status(scan_id=first_miss.scan_id)["records"][0]
    assert first_record.lifecycle_status is CandidateLifecycleStatus.REVIEW_DUE
    assert first_record.miss_count == 1

    second_miss = service.scan(
        _request(
            service,
            _release(
                state,
                objects,
                "release:life:2",
                start + timedelta(days=3),
                include_signals=False,
            ),
        )
    )
    second_record = service.status(scan_id=second_miss.scan_id)["records"][0]
    assert second_record.lifecycle_status is CandidateLifecycleStatus.CLOSED
    assert second_record.miss_count == 2


def test_three_interruption_boundaries_recover_idempotently(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    request = _request(service, _release(state, objects, "release:recover", as_of))
    for step in (
        CandidateCheckpointStep.INPUTS_VALIDATED,
        CandidateCheckpointStep.SIGNALS_WRITTEN,
        CandidateCheckpointStep.CANDIDATES_WRITTEN,
        CandidateCheckpointStep.REGISTRY_COMMITTED,
    ):
        with pytest.raises(CandidateInterrupted):
            service.scan(request, interrupt_after=step)
    before = service.repository.get_scan(
        content_hash(
            {
                "request_hash": content_hash(
                    {
                        "input_release_id": request.input_release_id,
                        "input_release_object_hash": request.input_release_object_hash,
                        "as_of": request.as_of,
                        "rules_version": request.rules_version,
                        "formal_historical": request.formal_historical,
                        "live": request.live,
                    }
                ),
                "input_release_id": request.input_release_id,
                "rules_version": request.rules_version,
            }
        )
    )
    assert before is not None
    assert before["checkpoint_step"] == "REGISTRY_COMMITTED"
    with state.connect() as connection:
        before_counts = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM candidate_record_version WHERE scan_id=?),"
                "(SELECT COUNT(*) FROM candidate_scan_member WHERE scan_id=?),"
                "(SELECT COUNT(*) FROM candidate_universe_snapshot WHERE scan_id=?)",
                (before["scan_id"], before["scan_id"], before["scan_id"]),
            ).fetchone()
        )
    universe_before = service.repository.get_universe(str(before["scan_id"]))
    assert universe_before is not None
    object_hash_before = str(universe_before["snapshot_object_hash"])
    descriptor_before = str(universe_before["member_descriptor_json"])
    report = service.scan(request)
    assert report.status is CandidateScanStatus.SUCCEEDED
    assert len(report.interrupted_attempt_ids) == 4
    with state.connect() as connection:
        after_counts = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM candidate_record_version WHERE scan_id=?),"
                "(SELECT COUNT(*) FROM candidate_scan_member WHERE scan_id=?),"
                "(SELECT COUNT(*) FROM candidate_universe_snapshot WHERE scan_id=?)",
                (report.scan_id, report.scan_id, report.scan_id),
            ).fetchone()
        )
    universe_after = service.repository.get_universe(report.scan_id)
    assert universe_after is not None
    assert after_counts == before_counts
    assert universe_after["snapshot_object_hash"] == object_hash_before
    assert universe_after["member_descriptor_json"] == descriptor_before
    assert service.scan(request).model_dump() == report.model_dump()
    assert service.audit(report.scan_id).status.value == "PASS"

    repeated_request = request.model_copy(update={"request_id": "request:recover:repeated"})
    assert service.scan(repeated_request).scan_id == report.scan_id


@pytest.mark.parametrize("corruption", ["object", "parquet", "sqlite"])
def test_registry_committed_recovery_fails_closed_on_corruption(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
    corruption: str,
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    request = _request(
        service,
        _release(state, objects, f"release:recover-corrupt:{corruption}", as_of),
    )
    with pytest.raises(CandidateInterrupted):
        service.scan(
            request,
            interrupt_after=CandidateCheckpointStep.REGISTRY_COMMITTED,
        )
    with state.connect() as connection:
        scan_id = str(
            connection.execute(
                "SELECT scan_id FROM candidate_scan_run WHERE input_release_id=?",
                (request.input_release_id,),
            ).fetchone()[0]
        )
    if corruption == "object":
        record_row = service.repository.list_scan_records(scan_id)[0]
        objects.path_for(str(record_row["record_object_hash"])).write_bytes(b"tampered")
    elif corruption == "parquet":
        universe = service.repository.get_universe(scan_id)
        assert universe is not None
        from astock.schemas.candidates import CandidateFileDescriptor

        descriptor = CandidateFileDescriptor.model_validate_json(
            str(universe["member_descriptor_json"])
        )
        (service.parquet.root / descriptor.path).write_bytes(b"tampered")
    else:
        with state.transaction() as connection:
            connection.execute(
                "DELETE FROM candidate_scan_member WHERE scan_id=?",
                (scan_id,),
            )
    with pytest.raises((AStockError, ValueError)):
        service.scan(request)
    row = service.repository.get_scan(scan_id)
    assert row is not None
    assert row["report_object_hash"] is None


def test_future_or_not_pit_safe_input_is_needs_info(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        "release:not-pit",
        as_of,
        pit=CandidatePitStatus.NOT_PIT_SAFE,
    )
    report = service.scan(_request(service, release))
    assert report.status is CandidateScanStatus.NEEDS_INFO
    assert any(code.startswith("NOT_PIT_SAFE:") for code in report.needs_info_codes)
    assert not service.status(scan_id=report.scan_id)["records"]


def test_future_nested_evidence_is_excluded_and_forces_needs_info(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(state, objects, "release:future-event", as_of)
    release.companies[0].announcement_events[0].available_to_system_at = as_of + timedelta(
        minutes=1
    )
    report = service.scan(_request(service, release))
    assert report.status is CandidateScanStatus.NEEDS_INFO
    assert any(code.startswith("FUTURE_INPUT:") for code in report.needs_info_codes)


def test_duplicate_medium_events_from_one_source_are_not_independent(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(state, objects, "release:duplicate-source", as_of)
    company = release.companies[0]
    first = company.announcement_events[0]
    first.severity = CandidateEvidenceSeverity.MEDIUM
    company.announcement_events.append(
        first.model_copy(update={"event_id": "event:duplicate-source:second"})
    )
    company.financial_flags = []
    company.holding_observations = []
    report = service.scan(_request(service, release))
    record = service.status(scan_id=report.scan_id)["records"][0]
    assert record.strength.value == "MODERATE"
    assert record.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY


def test_distinct_artifacts_with_same_underlying_source_are_not_independent(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(state, objects, "release:underlying-duplicate", as_of)
    company = release.companies[0]
    company.announcement_events[0].severity = CandidateEvidenceSeverity.MEDIUM
    event_artifact = next(
        item
        for item in release.artifacts
        if item.role is CandidateArtifactRole.ANNOUNCEMENT_EVENTS
    )
    financial_artifact = next(
        item
        for item in release.artifacts
        if item.role is CandidateArtifactRole.FINANCIAL_INTEGRITY
    )
    shared_evidence = event_artifact.evidence_ids
    financial_artifact.source_family = event_artifact.source_family
    financial_artifact.source_snapshot_ids = event_artifact.source_snapshot_ids
    financial_artifact.evidence_ids = shared_evidence
    company.financial_flags[0].evidence_ids = shared_evidence
    company.holding_observations = []
    report = service.scan(_request(service, release))
    record = service.status(scan_id=report.scan_id)["records"][0]
    assert record.strength.value == "MODERATE"


def test_unproven_complete_company_universe_is_needs_info_without_lifecycle_writes(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        "release:false-complete",
        as_of,
        proven_company_ids=["company:600519", "company:000001"],
    )
    report = service.scan(_request(service, release))
    assert report.status is CandidateScanStatus.NEEDS_INFO
    assert "COMPANY_COVERAGE_PROOF_MISMATCH" in report.needs_info_codes
    assert service.status(scan_id=report.scan_id)["records"] == []
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_record_version WHERE scan_id=?",
            (report.scan_id,),
        ).fetchone()[0] == 0


def test_historical_backfill_never_links_future_scan_or_replaces_current_status(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    t1 = datetime(2026, 7, 20, 8, tzinfo=UTC)
    t2 = t1 + timedelta(days=1)
    newer = service.scan(
        _request(service, _release(state, objects, "release:history:t2", t2))
    )
    newer_record = service.status(scan_id=newer.scan_id)["records"][0]
    older = service.scan(
        _request(service, _release(state, objects, "release:history:t1", t1))
    )
    older_record = service.status(scan_id=older.scan_id)["records"][0]
    assert older_record.previous_version_id is None
    current = service.status(company_id="company:600519")["record"]
    assert current.candidate_version_id == newer_record.candidate_version_id
    assert service.audit(older.scan_id).status.value == "PASS"
    assert service.audit(newer.scan_id).status.value == "PASS"
    with state.transaction() as connection:
        connection.execute(
            "UPDATE candidate_record_version SET previous_version_id=? "
            "WHERE candidate_version_id=?",
            (newer_record.candidate_version_id, newer_record.candidate_version_id),
        )
    corrupted = service.audit(newer.scan_id)
    assert corrupted.status.value == "FAIL"
    assert "PREVIOUS_VERSION_ASOF_INVALID" in corrupted.failure_codes


def test_production_verifier_rejects_fixture_relabeling_by_default(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    test_service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    request = _request(
        test_service,
        _release(state, objects, "release:fixture-forbidden", as_of),
    )
    production_service = CandidateScanService(
        CandidateRepository(state),
        objects,
        test_service.parquet,
        test_service.config,
    )
    report = production_service.scan(request)
    assert report.status is CandidateScanStatus.NEEDS_INFO
    assert any("ARTIFACT_CONTRACT_MISMATCH" in code for code in report.needs_info_codes)
    assert production_service.status(scan_id=report.scan_id)["records"] == []


def test_production_verifier_rejects_self_declared_instrument_fields(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(state, objects, "release:typed-instrument", as_of)
    artifact = next(
        item
        for item in release.artifacts
        if item.role is CandidateArtifactRole.INSTRUMENT_TRADABILITY
    )
    company = release.companies[0]
    typed = InstrumentRecord(
        created_at=artifact.available_to_system_at,
        instrument_id=company.instrument_id,
        market=company.market,
        symbol=company.symbol,
        name="typed canonical name",
        instrument_type=company.instrument_type,
        tradable=True,
        status_date=as_of.date(),
        is_st=False,
        source_snapshot_id=artifact.source_snapshot_ids[0],
        available_to_system_at=artifact.available_to_system_at,
    )
    verifier = ProductionCandidateInputVerifier(
        state,
        objects,
        service.parquet.root.parent,
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    with pytest.raises(ValueError, match="instrument fields"):
        verifier._verify_instrument_inputs(artifact, release, [typed])


def test_audit_replays_typed_input_verification(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    report = service.scan(
        _request(service, _release(state, objects, "release:audit-replay", as_of))
    )

    class RejectingVerifier:
        def verify(self, release: CandidateInputRelease) -> CandidateVerificationResult:
            return CandidateVerificationResult(
                issue_codes=("TYPED_CONTENT_MISMATCH",),
                proven_company_ids=frozenset(release.expected_company_ids),
            )

    service.input_verifier = RejectingVerifier()
    audit = service.audit(report.scan_id)
    assert audit.status.value == "FAIL"
    assert "SCAN_REPORT_SEMANTIC_BINDING_INVALID" in audit.failure_codes


def test_recurrence_reuses_identity_and_increments_reactivation_count(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    start = datetime(2026, 7, 20, 8, tzinfo=UTC)
    first = service.scan(
        _request(service, _release(state, objects, "release:reactivate:0", start))
    )
    first_record = service.status(scan_id=first.scan_id)["records"][0]
    service.scan(
        _request(
            service,
            _release(
                state,
                objects,
                "release:reactivate:1",
                start + timedelta(days=1),
                include_signals=False,
            ),
        )
    )
    recurring = service.scan(
        _request(
            service,
            _release(
                state,
                objects,
                "release:reactivate:2",
                start + timedelta(days=2),
            ),
        )
    )
    record = service.status(scan_id=recurring.scan_id)["records"][0]
    assert record.candidate_id == first_record.candidate_id
    assert record.reactivation_count == 1


def test_code_change_creates_instrument_identity_and_reviews_old_code(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    start = datetime(2026, 7, 20, 8, tzinfo=UTC)
    first = service.scan(
        _request(service, _release(state, objects, "release:code:old", start))
    )
    old_record = service.status(scan_id=first.scan_id)["records"][0]
    changed = _release(
        state,
        objects,
        "release:code:new",
        start + timedelta(days=1),
        symbol="601519",
        company_id_override="company:600519",
    )
    second = service.scan(_request(service, changed))
    records = service.status(scan_id=second.scan_id)["records"]
    new_record = next(item for item in records if item.instrument_id == "XSHG:601519")
    old_review = next(item for item in records if item.instrument_id == "XSHG:600519")
    assert new_record.candidate_id != old_record.candidate_id
    assert new_record.lifecycle_status is CandidateLifecycleStatus.RESEARCH_READY
    assert old_review.lifecycle_status is CandidateLifecycleStatus.REVIEW_DUE


def test_delisted_instrument_stays_observation_only(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        "release:delisted",
        as_of,
        tradability=CandidateTradability.DELISTED,
    )
    report = service.scan(_request(service, release))
    record = service.status(scan_id=report.scan_id)["records"][0]
    assert record.lifecycle_status is CandidateLifecycleStatus.OBSERVATION


def test_live_release_requires_explicit_live_request(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        "release:live",
        as_of,
        source_mode=CandidateSourceMode.LIVE,
    )
    with pytest.raises(ValueError, match="explicit live"):
        service.scan(_request(service, release))
    live_report = service.scan(_request(service, release, live=True))
    assert live_report.status is CandidateScanStatus.SUCCEEDED


def test_candidate_record_rejects_trading_fields() -> None:
    payload = {
        "candidate_id": "a" * 64,
        "candidate_version_id": "b" * 64,
        "scan_id": "c" * 64,
        "input_release_id": "release",
        "company_id": "company:600519",
        "instrument_id": "XSHG:600519",
        "as_of": "2026-07-20T08:00:00Z",
        "lifecycle_status": "OBSERVATION",
        "evaluation_status": "EVALUATED",
        "strength": "WEAK",
        "quality_status": "PASS",
        "tradability": "TRADABLE",
        "liquidity_gate_passed": True,
        "miss_count": 0,
        "reactivation_count": 0,
        "target_price": "100",
    }
    with pytest.raises(ValidationError):
        CandidateRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("market", "symbol"),
    [
        (Market.XSHG, "600519"),
        (Market.XSHE, "300750"),
        (Market.XSHG, "688981"),
        (Market.BJSE, "920799"),
    ],
)
def test_supported_a_share_markets(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
    market: Market,
    symbol: str,
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        f"release:{market.value}:{symbol}",
        as_of,
        market=market,
        symbol=symbol,
    )
    report = service.scan(_request(service, release))
    assert report.status is CandidateScanStatus.SUCCEEDED


def test_index_context_cannot_be_research_ready(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    release = _release(
        state,
        objects,
        "release:index",
        as_of,
        market=Market.INDEX,
        symbol="000300",
        instrument_type=InstrumentType.INDEX,
        tradability=CandidateTradability.INDEX_CONTEXT,
    )
    report = service.scan(_request(service, release))
    record = service.status(scan_id=report.scan_id)["records"][0]
    assert record.lifecycle_status is CandidateLifecycleStatus.OBSERVATION


def test_audit_detects_object_hash_tampering(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    report = service.scan(
        _request(service, _release(state, objects, "release:tamper", as_of))
    )
    row = service.repository.get_signal_manifest(report.scan_id)
    assert row is not None
    path = objects.path_for(str(row["manifest_object_hash"]))
    path.write_bytes(b"tampered")
    audit = service.audit(report.scan_id)
    assert audit.status.value == "FAIL"
    assert "SIGNAL_MANIFEST_OBJECT_INVALID" in audit.failure_codes


def test_audit_detects_parquet_and_registry_pointer_tampering(
    candidate_runtime: tuple[CandidateScanService, StateStore, ObjectStore],
) -> None:
    service, state, objects = candidate_runtime
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    report = service.scan(
        _request(service, _release(state, objects, "release:pointer-tamper", as_of))
    )
    row = service.repository.get_signal_manifest(report.scan_id)
    assert row is not None
    from astock.schemas.candidates import CandidateFileDescriptor

    descriptor = CandidateFileDescriptor.model_validate_json(
        str(row["parquet_descriptor_json"])
    )
    (service.parquet.root / descriptor.path).write_bytes(b"tampered parquet")
    with state.transaction() as connection:
        connection.execute(
            "UPDATE artifact_registry SET object_hash=? WHERE artifact_id=?",
            ("0" * 64, row["manifest_artifact_id"]),
        )
    audit = service.audit(report.scan_id)
    assert audit.status.value == "FAIL"
    assert "SIGNAL_PARQUET_INVALID" in audit.failure_codes
    assert "SIGNAL_REGISTRY_POINTER_INVALID" in audit.failure_codes
