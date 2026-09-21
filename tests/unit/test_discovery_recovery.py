from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.candidates.discovery_recovery import DiscoveryRecoveryService
from astock.candidates.discovery_runtime import (
    DiscoveryEvidenceNeed,
    DiscoveryRuntimeCompanyContext,
)
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.market import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    ExternalAuthority,
    ExternalResearchNeed,
)
from astock.settings import ProjectPaths

NOW = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)


def _paths(tmp_path: Path) -> ProjectPaths:
    runtime = tmp_path / "runtime"
    return ProjectPaths(
        root=Path(__file__).resolve().parents[2],
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )


def _context(company_id: str) -> DiscoveryRuntimeCompanyContext:
    return DiscoveryRuntimeCompanyContext(
        company_id=company_id,
        market=Market.XSHG,
        name=f"Company {company_id}",
        industry_id="industry:test",
        business_profile="GENERAL_INDUSTRIAL",
    )


def _need(
    company_id: str,
    role: str,
    capabilities: tuple[str, ...],
) -> DiscoveryEvidenceNeed:
    return DiscoveryEvidenceNeed(
        company_id=company_id,
        channel="EVENT",
        role=role,
        thesis_family_id="reviewed-method:event",
        research_question=f"Review {role}",
        existing_capabilities=capabilities,
        preferred_authorities=("ISSUER_IR", "EXCHANGE_OFFICIAL"),
    )


class _FakeAcquisition:
    calls: list[str] = []

    def __init__(
        self,
        _paths: ProjectPaths,
        state: StateStore,
        objects: ObjectStore,
    ) -> None:
        self.state = state
        self.objects = objects

    def acquire(self, company_id: str, market: Market) -> CurrentResearchAcquisitionReport:
        self.calls.append(company_id)
        attempts = [
            AcquisitionAttempt(
                capability=AcquisitionCapability.FINANCIAL_ANNUAL,
                status=AcquisitionAttemptStatus.SUCCEEDED,
                provider_path=["secondary-a", "official-b"],
                fallback_used=True,
                record_count=2,
                latency_ms=11,
                source_snapshot_ids=[],
            ),
            AcquisitionAttempt(
                capability=AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
                status=AcquisitionAttemptStatus.SUCCEEDED,
                provider_path=["secondary-a"],
                fallback_used=False,
                record_count=1,
                latency_ms=7,
                source_snapshot_ids=[],
            ),
            AcquisitionAttempt(
                capability=AcquisitionCapability.DAILY_MARKET,
                status=AcquisitionAttemptStatus.FAILED,
                provider_path=["market-a"],
                fallback_used=False,
                record_count=0,
                latency_ms=3,
                internal_reason_codes=["SOURCE_UNAVAILABLE"],
                source_snapshot_ids=[],
            ),
        ]
        report = CurrentResearchAcquisitionReport(
            report_id=f"current-research-acquisition:{company_id}",
            company_id=company_id,
            market=market,
            started_at=NOW,
            decision_as_of=NOW,
            status=CurrentResearchAcquisitionStatus.NEEDS_EXTERNAL_RESEARCH,
            attempts=attempts,
            external_research_needs=[
                ExternalResearchNeed(
                    capability=AcquisitionCapability.DAILY_MARKET,
                    research_question="Recover daily market",
                    preferred_authorities=[ExternalAuthority.EXCHANGE_OFFICIAL],
                )
            ],
            created_at=NOW,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=report.report_id,
            artifact_type="CurrentResearchAcquisitionReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        return report


def test_recovery_executes_existing_capabilities_and_persists_budget_receipt(
    tmp_path: Path,
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    _FakeAcquisition.calls = []
    paths = _paths(tmp_path)
    service = DiscoveryRecoveryService(
        paths,
        state,
        object_store,
        acquisition_factory=_FakeAcquisition,
        monotonic=iter((0.0, 0.0, 1.0)).__next__,
    )

    batch = service.execute(
        contexts=(_context("600001"),),
        evidence_needs=(
            _need(
                "600001",
                "company_exposure",
                ("FINANCIAL_ANNUAL", "FINANCIAL_LATEST_INTERIM"),
            ),
            _need("600001", "price", ("DAILY_MARKET",)),
            _need("600001", "demand_transmission", ()),
        ),
        recovery_budget_seconds=60,
        max_companies=12,
    )

    assert _FakeAcquisition.calls == ["600001"]
    assert len(batch.company_recoveries) == 1
    recovery = batch.company_recoveries[0]
    assert recovery.local_review_ready_roles == ("company_exposure",)
    assert recovery.public_research_required_roles == ("price",)
    assert recovery.fallback_capabilities == ("FINANCIAL_ANNUAL",)
    assert batch.no_existing_capability_roles == (("600001", "demand_transmission"),)
    assert not batch.budget_exhausted
    record = state.artifact_record(batch.artifact_id)
    assert record is not None
    assert object_store.verify(str(record["object_hash"]))
    checkpoint = state.get_checkpoint("verified-discovery-recovery", "latest")
    assert checkpoint is not None
    assert checkpoint["status"] == "REVIEW_REQUIRED"
    assert checkpoint["cursor"]["local_review_ready_role_count"] == 1
    assert checkpoint["cursor"]["public_research_required_role_count"] == 2


def test_recovery_stops_before_next_company_when_budget_is_exhausted(
    tmp_path: Path,
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    _FakeAcquisition.calls = []
    paths = _paths(tmp_path)
    service = DiscoveryRecoveryService(
        paths,
        state,
        object_store,
        acquisition_factory=_FakeAcquisition,
        monotonic=iter((0.0, 0.0, 61.0, 61.5)).__next__,
    )

    batch = service.execute(
        contexts=(_context("600001"), _context("600002")),
        evidence_needs=(
            _need("600001", "price", ("DAILY_MARKET",)),
            _need("600002", "price", ("DAILY_MARKET",)),
        ),
        recovery_budget_seconds=60,
        max_companies=12,
    )

    assert _FakeAcquisition.calls == ["600001"]
    assert batch.budget_exhausted
    assert batch.unattempted_roles == (("600002", "price"),)
    checkpoint = state.get_checkpoint("verified-discovery-recovery", "latest")
    assert checkpoint is not None
    assert checkpoint["status"] == "BUDGET_EXHAUSTED"
    assert checkpoint["cursor"]["unattempted_role_count"] == 1
