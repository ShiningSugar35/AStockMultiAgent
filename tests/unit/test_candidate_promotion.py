from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astock.candidates.promotion import ResearchSeedPromotionService, _PromotionBlocked
from astock.candidates.service import CandidateScanService
from astock.candidates.verification import ProductionCandidateInputVerifier
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.candidate_promotion import SeedPromotionRequest
from astock.schemas.candidates import (
    CandidateArtifactRole,
    CandidateCompanyInput,
    CandidateCoverageStatus,
    CandidateInputArtifact,
    CandidateInputRelease,
    CandidateInstrumentUniverseProof,
    CandidatePitStatus,
    CandidateQualityStatus,
    CandidateTradability,
)
from astock.schemas.evidence import SourceSnapshot
from astock.schemas.market import InstrumentType, Market
from astock.schemas.reference_data import (
    DatasetReleaseManifest,
    InstrumentRecord,
    ReferenceCoverage,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferenceFileDescriptor,
    ReferencePitStatus,
)
from astock.schemas.research_runtime import TradingClassificationCorporateActionBaseline
from astock.schemas.research_seeds import (
    ResearchSeed,
    ResearchSeedOrigin,
    ResearchSeedReport,
    ResearchSeedStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 11, 6, tzinfo=UTC)


class _FakeCandidateRepository:
    def status_by_company(self, company_id: str) -> dict[str, object]:
        return {"candidate_version_id": f"candidate-version:{company_id}"}


class _FakeCandidateService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.repository = _FakeCandidateRepository()

    def stage_input_release(self, release: CandidateInputRelease) -> str:
        ref = self.objects.put_json(release.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"candidate-input-release:{release.input_release_id}",
            artifact_type="CandidateInputRelease",
            schema_version=release.schema_version,
            object_hash=ref.sha256,
            input_hashes=[item.object_hash for item in release.artifacts],
        )
        return ref.sha256

    def scan(self, request: object) -> object:
        del request
        return SimpleNamespace(scan_id="scan:promotion", status=SimpleNamespace(value="SUCCEEDED"))

    def audit(self, scan_id: str) -> object:
        assert scan_id == "scan:promotion"
        return SimpleNamespace(status=SimpleNamespace(value="PASS"))


class _FakePromotionService(ResearchSeedPromotionService):
    def _promote_company(self, seed: ResearchSeed, **_: object) -> tuple[Any, list[Any], str]:
        if seed.company_id == "600002":
            raise _PromotionBlocked(
                "FINANCIAL_INTEGRITY_REQUIRED",
                ["NO_SUCCEEDED_FINANCIAL_INTEGRITY_PACK"],
                [],
            )
        artifacts: list[CandidateInputArtifact] = []
        role_ids: dict[CandidateArtifactRole, str] = {}
        for role in (
            CandidateArtifactRole.INSTRUMENT_TRADABILITY,
            CandidateArtifactRole.TRADING_CALENDAR,
            CandidateArtifactRole.DAILY_LOCAL_VERSIONED,
            CandidateArtifactRole.CORPORATE_ACTION,
            CandidateArtifactRole.DATA_QUALITY,
            CandidateArtifactRole.ANNOUNCEMENT_EVENTS,
            CandidateArtifactRole.FINANCIAL_INTEGRITY,
        ):
            artifact_id = f"promotion-test:{seed.company_id}:{role.value}"
            ref = self.objects.put_json({"artifact_id": artifact_id})
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type=f"PromotionTest{role.value}",
                schema_version="promotion-test-v1",
                object_hash=ref.sha256,
                input_hashes=[],
            )
            role_ids[role] = artifact_id
            artifacts.append(
                CandidateInputArtifact(
                    artifact_id=artifact_id,
                    role=role,
                    artifact_type=f"PromotionTest{role.value}",
                    artifact_schema_version="promotion-test-v1",
                    dataset_kind=role.value,
                    formal_status="CERTIFIED",
                    source_family="promotion-test",
                    object_hash=ref.sha256,
                    coverage_status=CandidateCoverageStatus.COMPLETE,
                    available_to_system_at=NOW,
                    pit_status=CandidatePitStatus.CERTIFIED,
                    created_at=NOW,
                )
            )
        company = CandidateCompanyInput(
            company_id=seed.company_id,
            instrument_id=f"{seed.market.value}:{seed.company_id}",
            market=seed.market,
            symbol=seed.company_id,
            name=seed.name,
            instrument_type=InstrumentType.STOCK,
            tradability=CandidateTradability.TRADABLE,
            instrument_artifact_id=role_ids[CandidateArtifactRole.INSTRUMENT_TRADABILITY],
            calendar_artifact_id=role_ids[CandidateArtifactRole.TRADING_CALENDAR],
            daily_artifact_id=role_ids[CandidateArtifactRole.DAILY_LOCAL_VERSIONED],
            corporate_action_artifact_id=role_ids[CandidateArtifactRole.CORPORATE_ACTION],
            quality_artifact_id=role_ids[CandidateArtifactRole.DATA_QUALITY],
            announcement_artifact_id=role_ids[CandidateArtifactRole.ANNOUNCEMENT_EVENTS],
            financial_artifact_id=role_ids[CandidateArtifactRole.FINANCIAL_INTEGRITY],
            quality_status=CandidateQualityStatus.PASS,
            created_at=NOW,
        )
        return company, artifacts, f"financial:{seed.company_id}"


def _seed(company_id: str, *, origins: list[ResearchSeedOrigin]) -> ResearchSeed:
    return ResearchSeed(
        seed_id=f"research-seed:{company_id}",
        company_id=company_id,
        market=Market.XSHG,
        name=f"公司{company_id}",
        origins=origins,
        research_priority_score=0.8,
        reason_codes=["TEST"],
        candidate_version_id=(
            f"existing:{company_id}" if ResearchSeedOrigin.EXISTING_CANDIDATE in origins else None
        ),
        created_at=NOW,
    )


def _runtime(tmp_path: Path) -> tuple[StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return state, ObjectStore(tmp_path / "objects")


def _register_seed_report(state: StateStore, objects: ObjectStore) -> str:
    report = ResearchSeedReport(
        report_id="research-seeds:promotion-test",
        as_of=NOW,
        data_cutoff_at=NOW,
        status=ResearchSeedStatus.READY,
        profiles=[],
        seeds=[
            _seed("600001", origins=[ResearchSeedOrigin.MARKET]),
            _seed("600002", origins=[ResearchSeedOrigin.EXPERT_SKILL]),
            _seed("600003", origins=[ResearchSeedOrigin.EXISTING_CANDIDATE]),
        ],
        source_snapshot_ids=[],
        source_object_hashes=[],
        warning_codes=[],
        market_seed_count=1,
        expert_seed_count=1,
        existing_candidate_seed_count=1,
        created_at=NOW,
    )
    ref = objects.put_json(report.model_dump(mode="json"))
    artifact_id = f"ResearchSeedReport:{report.report_id}"
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="ResearchSeedReport",
        schema_version=report.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return artifact_id


def test_promotion_isolates_blocked_seed_and_reuses_existing_candidate(tmp_path: Path) -> None:
    state, objects = _runtime(tmp_path)
    fake_candidates = _FakeCandidateService(state, objects)
    service = _FakePromotionService(
        project_root=PROJECT_ROOT,
        state=state,
        objects=objects,
        reference=cast(Any, object()),
        candidates=cast(CandidateScanService, fake_candidates),
        financial_sources=cast(Any, object()),
        trading_classification=cast(Any, object()),
        cninfo=cast(Any, object()),
    )
    seed_artifact = _register_seed_report(state, objects)

    report = service.promote(
        SeedPromotionRequest(
            seed_report_artifact_id=seed_artifact,
            max_seeds=3,
            live=True,
            created_at=NOW,
        )
    )

    assert report.status.value == "PARTIAL"
    assert report.promoted_company_count == 1
    assert report.blocked_company_count == 1
    assert report.reused_candidate_count == 1
    assert report.candidate_input_release_id is not None
    assert report.candidate_scan_id == "scan:promotion"
    assert [item.company_id for item in report.tasks] == ["600002"]
    assert report.tasks[0].task_code == "FINANCIAL_INTEGRITY_REQUIRED"
    assert not report.recommendation_allowed
    assert service.audit(f"SeedPromotionReport:{report.promotion_id}")["status"] == "PASS"


def test_plain_instrument_release_rejects_unproven_candidate_subset(
    tmp_path: Path,
) -> None:
    state, objects = _runtime(tmp_path)
    verifier = ProductionCandidateInputVerifier(
        state,
        objects,
        tmp_path,
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    artifact = cast(
        Any, SimpleNamespace(artifact_id="market-reference:test", source_snapshot_ids=[])
    )
    company = cast(
        Any,
        SimpleNamespace(
            company_id="600001",
            instrument_id="XSHG:600001",
            market=Market.XSHG,
            symbol="600001",
            name="测试公司",
            instrument_type=InstrumentType.STOCK,
            tradability=CandidateTradability.TRADABLE,
            instrument_artifact_id="market-reference:test",
        ),
    )
    release = cast(CandidateInputRelease, SimpleNamespace(companies=[company], as_of=NOW))
    records = [
        InstrumentRecord(
            instrument_id="XSHG:600001",
            market=Market.XSHG,
            symbol="600001",
            name="测试公司",
            instrument_type=InstrumentType.STOCK,
            tradable=True,
            status_date=NOW.date(),
            is_st=False,
            source_snapshot_id="snapshot:test",
            available_to_system_at=NOW,
            created_at=NOW,
        ),
        InstrumentRecord(
            instrument_id="XSHG:600002",
            market=Market.XSHG,
            symbol="600002",
            name="其他公司",
            instrument_type=InstrumentType.STOCK,
            tradable=True,
            status_date=NOW.date(),
            is_st=False,
            source_snapshot_id="snapshot:test",
            available_to_system_at=NOW,
            created_at=NOW,
        ),
    ]
    artifact.source_snapshot_ids = ["snapshot:test"]

    try:
        verifier._verify_instrument_inputs(artifact, release, records)
    except ValueError as exc:
        assert "universe differs" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("plain full-market master cannot prove an arbitrary subset")


def test_seed_instrument_universe_proof_allows_only_the_frozen_seed_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, objects = _runtime(tmp_path)
    verifier = ProductionCandidateInputVerifier(
        state,
        objects,
        tmp_path,
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    seed_artifact_id = _register_seed_report(state, objects)
    seed_record = state.artifact_record(seed_artifact_id)
    assert seed_record is not None
    seed_hash = str(seed_record["object_hash"])

    snapshot_ref = objects.put_json({"instruments": ["600001", "600002"]})
    snapshot = SourceSnapshot(
        snapshot_id="snapshot:instrument-proof",
        source_id="baostock-reference:instrument.master",
        object_sha256=snapshot_ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        mime="application/json",
        byte_size=snapshot_ref.byte_size,
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    records = [
        InstrumentRecord(
            instrument_id="XSHG:600001",
            market=Market.XSHG,
            symbol="600001",
            name="测试公司",
            instrument_type=InstrumentType.STOCK,
            tradable=True,
            status_date=NOW.date(),
            is_st=False,
            source_snapshot_id=snapshot.snapshot_id,
            available_to_system_at=NOW,
            created_at=NOW,
        ),
        InstrumentRecord(
            instrument_id="XSHG:600002",
            market=Market.XSHG,
            symbol="600002",
            name="其他公司",
            instrument_type=InstrumentType.STOCK,
            tradable=True,
            status_date=NOW.date(),
            is_st=False,
            source_snapshot_id=snapshot.snapshot_id,
            available_to_system_at=NOW,
            created_at=NOW,
        ),
    ]
    content_sha = "1" * 64
    descriptors = [
        ReferenceFileDescriptor(
            path="observation.parquet",
            sha256="2" * 64,
            schema_fingerprint="3" * 64,
            row_count=2,
            logical_content_hash=content_sha,
            created_at=NOW,
        ),
        ReferenceFileDescriptor(
            path="canonical.parquet",
            sha256="4" * 64,
            schema_fingerprint="5" * 64,
            row_count=2,
            logical_content_hash=content_sha,
            created_at=NOW,
        ),
    ]
    parent = DatasetReleaseManifest(
        release_id="6" * 64,
        content_hash=content_sha,
        dataset_kind=ReferenceDatasetKind.INSTRUMENT_MASTER,
        scope_key=Market.XSHG.value,
        provider_id="baostock-reference",
        batch_id="7" * 64,
        raw_snapshot_ids=[snapshot.snapshot_id],
        observation_files=[descriptors[0]],
        canonical_files=[descriptors[1]],
        coverage=ReferenceCoverage(
            record_count=2,
            status=ReferenceCoverageStatus.COMPLETE,
            created_at=NOW,
        ),
        pit_status=ReferencePitStatus.RECONSTRUCTED,
        available_to_system_at=NOW,
        created_at=NOW,
    )
    parent_ref = objects.put_json(parent.model_dump(mode="json"))
    parent_artifact_id = f"market-reference:{parent.release_id}"
    state.register_artifact(
        artifact_id=parent_artifact_id,
        artifact_type="DatasetReleaseManifest",
        schema_version=parent.schema_version,
        object_hash=parent_ref.sha256,
        input_hashes=[snapshot.object_sha256],
    )
    proof = CandidateInstrumentUniverseProof(
        proof_id="proof:600001",
        seed_report_artifact_id=seed_artifact_id,
        seed_report_object_hash=seed_hash,
        parent_instrument_artifact_id=parent_artifact_id,
        parent_instrument_object_hash=parent_ref.sha256,
        parent_release_id=parent.release_id,
        as_of=NOW,
        company_ids=["600001"],
        instruments=[records[0]],
        source_snapshot_ids=[snapshot.snapshot_id],
        created_at=NOW,
    )
    proof_ref = objects.put_json(proof.model_dump(mode="json"))
    proof_artifact_id = f"CandidateInstrumentUniverseProof:{proof.proof_id}"
    state.register_artifact(
        artifact_id=proof_artifact_id,
        artifact_type="CandidateInstrumentUniverseProof",
        schema_version=proof.schema_version,
        object_hash=proof_ref.sha256,
        input_hashes=[seed_hash, parent_ref.sha256],
    )
    artifact = CandidateInputArtifact(
        artifact_id=proof_artifact_id,
        role=CandidateArtifactRole.INSTRUMENT_TRADABILITY,
        artifact_type="CandidateInstrumentUniverseProof",
        artifact_schema_version=proof.schema_version,
        dataset_kind="INSTRUMENT_TRADABILITY_SUBSET",
        formal_status=CandidatePitStatus.DOCUMENT_RECONSTRUCTED.value,
        source_family="seed-promotion-instrument-subset",
        object_hash=proof_ref.sha256,
        coverage_status=CandidateCoverageStatus.COMPLETE,
        available_to_system_at=NOW,
        pit_status=CandidatePitStatus.DOCUMENT_RECONSTRUCTED,
        source_snapshot_ids=[snapshot.snapshot_id],
        created_at=NOW,
    )
    company = cast(
        Any,
        SimpleNamespace(
            company_id="600001",
            instrument_id="XSHG:600001",
            market=Market.XSHG,
            symbol="600001",
            name="测试公司",
            instrument_type=InstrumentType.STOCK,
            tradability=CandidateTradability.TRADABLE,
            instrument_artifact_id=proof_artifact_id,
        ),
    )
    release = cast(CandidateInputRelease, SimpleNamespace(companies=[company], as_of=NOW))
    monkeypatch.setattr(
        verifier.reference,
        "status",
        lambda *args, **kwargs: {"status": "AVAILABLE", "release": parent.model_dump(mode="json")},
    )
    monkeypatch.setattr(verifier, "_reference_records", lambda *args, **kwargs: records)

    assert verifier._verify_instrument_universe_proof(artifact, release) == {"600001"}

    tampered = proof.model_copy(
        update={
            "proof_id": "proof:tampered",
            "parent_instrument_object_hash": "8" * 64,
        }
    )
    tampered_ref = objects.put_json(tampered.model_dump(mode="json"))
    tampered_artifact_id = "CandidateInstrumentUniverseProof:proof:tampered"
    state.register_artifact(
        artifact_id=tampered_artifact_id,
        artifact_type="CandidateInstrumentUniverseProof",
        schema_version=tampered.schema_version,
        object_hash=tampered_ref.sha256,
        input_hashes=[],
    )
    tampered_artifact = artifact.model_copy(
        update={"artifact_id": tampered_artifact_id, "object_hash": tampered_ref.sha256}
    )
    with pytest.raises(ValueError, match="parent release"):
        verifier._verify_instrument_universe_proof(tampered_artifact, release)


def test_corporate_action_absence_requires_exact_cninfo_index_snapshot(tmp_path: Path) -> None:
    state, objects = _runtime(tmp_path)
    snapshot_ref = objects.put_json({"announcements": []})
    snapshot = SourceSnapshot(
        snapshot_id=f"cninfo-disclosures:index:{snapshot_ref.sha256}",
        source_id="cninfo-disclosures:index",
        object_sha256=snapshot_ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        mime="application/json",
        byte_size=snapshot_ref.byte_size,
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    baseline = TradingClassificationCorporateActionBaseline(
        baseline_id="baseline:test",
        company_id="600001",
        market=Market.XSHG,
        symbol="600001",
        as_of=NOW,
        window_start="2026-07-01",
        window_end="2026-08-11",
        reference_status="OFFICIAL_ENUMERATION_COMPLETE",
        raw_snapshot_ids=[snapshot.snapshot_id],
        official_query_snapshot_ids=[snapshot.snapshot_id],
        candidate_announcement_ids=[],
        observed_record_count=0,
        reason_codes=[],
        absence_is_officially_certified=True,
        created_at=NOW,
    )
    baseline_ref = objects.put_json(baseline.model_dump(mode="json"))
    artifact_id = "TradingClassificationCorporateActionBaseline:baseline:test"
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="TradingClassificationCorporateActionBaseline",
        schema_version=baseline.schema_version,
        object_hash=baseline_ref.sha256,
        input_hashes=[snapshot.object_sha256],
    )
    artifact = CandidateInputArtifact(
        artifact_id=artifact_id,
        role=CandidateArtifactRole.CORPORATE_ACTION,
        artifact_type="TradingClassificationCorporateActionBaseline",
        artifact_schema_version=baseline.schema_version,
        dataset_kind="CORPORATE_ACTION_BASELINE",
        formal_status="CERTIFIED_ABSENCE",
        source_family="cninfo-official-corporate-action-baseline",
        object_hash=baseline_ref.sha256,
        coverage_status=CandidateCoverageStatus.COMPLETE,
        available_to_system_at=NOW,
        pit_status=CandidatePitStatus.CERTIFIED,
        source_snapshot_ids=[snapshot.snapshot_id],
        created_at=NOW,
    )
    company = cast(
        Any,
        SimpleNamespace(
            company_id="600001",
            symbol="600001",
            market=Market.XSHG,
            corporate_action_artifact_id=artifact_id,
        ),
    )
    release = cast(CandidateInputRelease, SimpleNamespace(companies=[company], as_of=NOW))
    verifier = ProductionCandidateInputVerifier(
        state,
        objects,
        tmp_path,
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )

    verifier._verify_corporate_action_baseline(artifact, release)
    with state.transaction() as connection:
        connection.execute(
            "UPDATE source_snapshot_index SET source_id='community-index' WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        )
    try:
        verifier._verify_corporate_action_baseline(artifact, release)
    except ValueError as exc:
        assert "source snapshot" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("non-CNINFO absence baseline must fail closed")


def test_promotion_identity_is_content_addressed() -> None:
    assert content_hash({"seed": "a"}) != content_hash({"seed": "b"})
