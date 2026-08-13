from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.research.adaptation import AdaptiveEdgeService
from astock.schemas import FetchStatus, FinancialPeriodType, SourceSnapshot
from astock.schemas.adaptation import (
    AdaptiveProposalStatus,
    ProviderFailureDiagnostic,
    ProviderRecoveryProposal,
    ResearchModule,
    ResearchPlannerProposal,
    SchemaRepairProposal,
)
from astock.schemas.provider import ProviderHealthStatus
from astock.schemas.reference_data import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    AcquisitionCapability,
)
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


def _runtime(tmp_path: Path) -> tuple[StateStore, ObjectStore, AdaptiveEdgeService]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return state, objects, AdaptiveEdgeService(state, objects, PROJECT_ROOT)


def _snapshot(
    state: StateStore,
    objects: ObjectStore,
    suffix: str,
    *,
    source_id: str = "sina-financial",
) -> SourceSnapshot:
    raw = f"raw-schema-sample:{suffix}".encode()
    ref = objects.put_bytes(raw)
    snapshot = SourceSnapshot(
        created_at=NOW,
        snapshot_id=f"{source_id}:{suffix}:{ref.sha256}",
        source_id=source_id,
        object_sha256=ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        source_url=f"https://example.invalid/{suffix}",
        mime="application/json",
        byte_size=len(raw),
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="PUBLIC_REFERENCE_DATA",
    )
    state.register_snapshot(snapshot)
    return snapshot


def _official_artifact(
    state: StateStore,
    objects: ObjectStore,
    *,
    artifact_id: str = "financial-source:official-cross-check",
    artifact_type: str = "FinancialSourceReleaseManifest",
) -> str:
    ref = objects.put_json({"official": True, "as_of": NOW.isoformat()})
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version="test-v1",
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return artifact_id


def _planner_proposal(*, proposal_id: str = "planner:test") -> ResearchPlannerProposal:
    return ResearchPlannerProposal(
        created_at=NOW,
        proposal_id=proposal_id,
        company_id="600989",
        market=Market.XSHG,
        requested_modules=[ResearchModule.COMMITTEE],
        skipped_optional_reasons={
            ResearchModule.SPECIALISTS: "not needed for this narrow question",
            ResearchModule.KNOWLEDGE: "no method-library delta requested",
            ResearchModule.TRADING_CLASSIFICATION: "research conclusion only",
        },
        requested_acquisition_capabilities=[],
        specialist_budget=4,
    )


def test_research_planner_adds_mandatory_gates_and_freezes_validated_plan(
    tmp_path: Path,
) -> None:
    state, objects, service = _runtime(tmp_path)
    proposal = _planner_proposal()

    plan = service.validate_research_plan(proposal)

    assert plan.status is AdaptiveProposalStatus.VALIDATED
    assert plan.specialist_budget == 4
    assert plan.ordered_modules == [
        ResearchModule.EVIDENCE,
        ResearchModule.PIT,
        ResearchModule.FINANCIAL_INTEGRITY,
        ResearchModule.FUNDAMENTAL_MODEL,
        ResearchModule.BASE_CASE,
        ResearchModule.COMMITTEE,
    ]
    assert set(plan.acquisition_capabilities) == {
        AcquisitionCapability.INSTRUMENT_IDENTITY,
        AcquisitionCapability.DAILY_MARKET,
        AcquisitionCapability.FINANCIAL_ANNUAL,
        AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
    }
    assert plan.paper_ledger_write_allowed is False
    assert plan.broker_execution_allowed is False
    assert state.artifact_record(proposal.proposal_id) is not None
    plan_record = state.artifact_record(plan.plan_id)
    assert plan_record is not None
    assert service.audit_artifact(plan.plan_id).status == "PASS"
    assert objects.verify(str(plan_record["object_hash"]))


def test_research_planner_cannot_enable_execution_or_skip_optional_without_reason(
    tmp_path: Path,
) -> None:
    _state, _objects, service = _runtime(tmp_path)
    with pytest.raises(ValidationError):
        ResearchPlannerProposal(
            created_at=NOW,
            proposal_id="planner:unsafe",
            company_id="600989",
            market=Market.XSHG,
            requested_modules=[],
            broker_execution_allowed=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="explain every skipped optional module"):
        service.validate_research_plan(
            ResearchPlannerProposal(
                created_at=NOW,
                proposal_id="planner:missing-skip-reasons",
                company_id="600989",
                market=Market.XSHG,
                requested_modules=[],
            )
        )


def test_validated_planner_plan_changes_optional_current_acquisition_but_core_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, objects, edge = _runtime(tmp_path)
    plan = edge.validate_research_plan(_planner_proposal(proposal_id="planner:acquisition"))
    runtime_root = tmp_path / "runtime"
    paths = ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime_root,
        objects=tmp_path / "objects",
        parquet=runtime_root / "parquet",
        manifests=runtime_root / "manifests",
        state_db=tmp_path / "state.sqlite",
    )
    times = iter([NOW, NOW + timedelta(seconds=1)])
    acquisition = CurrentResearchAcquisitionService(
        paths,
        state,
        objects,
        clock=lambda: next(times),
    )
    monkeypatch.setattr(
        acquisition,
        "_discover_financial_periods",
        lambda *_args: (
            [
                (
                    AcquisitionCapability.FINANCIAL_ANNUAL,
                    date(2025, 12, 31),
                    FinancialPeriodType.ANNUAL,
                ),
                (
                    AcquisitionCapability.FINANCIAL_LATEST_INTERIM,
                    date(2026, 6, 30),
                    FinancialPeriodType.SEMIANNUAL,
                ),
            ],
            [],
        ),
    )

    def successful_reference(
        capability: AcquisitionCapability, _action: object
    ) -> AcquisitionAttempt:
        return AcquisitionAttempt(
            created_at=NOW,
            capability=capability,
            status=AcquisitionAttemptStatus.SUCCEEDED,
            provider_path=["fixture-provider"],
            fallback_used=False,
            record_count=1,
            latency_ms=1,
        )

    def successful_financial(
        capability: AcquisitionCapability,
        *_args: object,
        **_kwargs: object,
    ) -> AcquisitionAttempt:
        return AcquisitionAttempt(
            created_at=NOW,
            capability=capability,
            status=AcquisitionAttemptStatus.SUCCEEDED,
            provider_path=["fixture-financial"],
            fallback_used=False,
            record_count=1,
            latency_ms=1,
        )

    monkeypatch.setattr(acquisition, "_reference_attempt", successful_reference)
    monkeypatch.setattr(acquisition, "_financial_attempt", successful_financial)

    report = acquisition.acquire(
        "600989",
        Market.XSHG,
        planner_plan_artifact_id=plan.plan_id,
    )

    capabilities = {item.capability for item in report.attempts}
    assert AcquisitionCapability.CORPORATE_ACTIONS not in capabilities
    assert capabilities == set(plan.acquisition_capabilities)
    assert report.planner_plan_artifact_id == plan.plan_id
    assert report.schedule_artifact_id is not None


def test_provider_recovery_validates_only_allowlisted_capability_paths(tmp_path: Path) -> None:
    _state, _objects, service = _runtime(tmp_path)
    proposal = ProviderRecoveryProposal(
        created_at=NOW,
        proposal_id="recovery:daily",
        requested_capability="market.daily_unadjusted",
        diagnostics=[
            ProviderFailureDiagnostic(
                provider_id="eastmoney-reference",
                capability="market.daily_unadjusted",
                failure_class="NETWORK",
                retryable=False,
                health_status=ProviderHealthStatus.UNAVAILABLE,
                transport_profile="eastmoney-browser-v1",
            )
        ],
        proposed_provider_ids=["sina-reference"],
    )

    validation = service.validate_recovery(proposal)

    assert validation.status is AdaptiveProposalStatus.VALIDATED
    assert validation.allowed_provider_ids == ["sina-reference"]
    assert validation.manual_last is True
    rejected = service.validate_recovery(
        proposal.model_copy(
            update={
                "proposal_id": "recovery:invalid",
                "proposed_provider_ids": ["imaginary-provider"],
            }
        )
    )
    assert rejected.status is AdaptiveProposalStatus.REJECTED
    assert "UNKNOWN_PROVIDER:imaginary-provider" in rejected.rejection_codes
    assert "NO_ALLOWLISTED_RECOVERY_PATH" in rejected.rejection_codes


def test_schema_repair_requires_raw_official_and_contract_evidence_before_candidate_admission(
    tmp_path: Path,
) -> None:
    state, objects, service = _runtime(tmp_path)
    first = _snapshot(state, objects, "one")
    second = _snapshot(state, objects, "two")
    official = _official_artifact(state, objects)
    dialect_path = PROJECT_ROOT / "configs" / "provider_dialects.yaml"
    before = dialect_path.read_bytes()
    proposal = SchemaRepairProposal(
        created_at=NOW,
        proposal_id="schema-repair:sina:vnext",
        provider_id="sina-financial",
        base_dialect_version="sina-finance-report-2026-v1",
        candidate_field_mapping={"TOTAL_ASSET_NEW": "total_assets"},
        candidate_response_paths={"report_list": "result.payload.report_list"},
        candidate_native_monetary_unit="CNY",
        sample_snapshot_ids=[first.snapshot_id, second.snapshot_id],
        official_evidence_artifact_ids=[official],
        contract_test_ids=[
            "tests/unit/test_provider_dialects.py::test_unknown_financial_dialect_fails_after_raw_snapshot_is_persisted"
        ],
    )

    validation = service.validate_schema_repair(proposal)

    assert validation.status is AdaptiveProposalStatus.VALIDATED
    assert validation.formal_fact_write_allowed is False
    assert validation.active_runtime_mutation_allowed is False
    with pytest.raises(ValueError, match="explicit approval"):
        service.admit_schema_repair(validation.validation_id, explicit_approval=False)
    release = service.admit_schema_repair(validation.validation_id, explicit_approval=True)
    assert release.status is AdaptiveProposalStatus.ADMITTED
    assert release.explicit_approval_bound is True
    assert release.formal_fact_write_allowed is False
    assert release.active_runtime_mutation_allowed is False
    assert dialect_path.read_bytes() == before
    assert service.audit_artifact(release.release_id).status == "PASS"
    rollback = service.rollback_dialect_candidate(release.release_id)
    assert rollback.status is AdaptiveProposalStatus.REJECTED
    assert rollback.restored_active_dialect_version == "sina-finance-report-2026-v1"
    assert rollback.active_runtime_mutation_allowed is False


def test_schema_repair_rejects_unknown_mapping_bad_official_type_and_missing_contract(
    tmp_path: Path,
) -> None:
    state, objects, service = _runtime(tmp_path)
    first = _snapshot(state, objects, "bad-one")
    second = _snapshot(state, objects, "bad-two")
    fake_official = _official_artifact(
        state,
        objects,
        artifact_id="fake:official",
        artifact_type="RandomInternalArtifact",
    )
    proposal = SchemaRepairProposal(
        created_at=NOW,
        proposal_id="schema-repair:rejected",
        provider_id="sina-financial",
        base_dialect_version="sina-finance-report-2026-v1",
        candidate_field_mapping={"MYSTERY": "invented_canonical_fact"},
        candidate_response_paths={"report_list": "future.path"},
        candidate_native_monetary_unit="CNY",
        sample_snapshot_ids=[first.snapshot_id, second.snapshot_id],
        official_evidence_artifact_ids=[fake_official],
        contract_test_ids=["tests/unit/does_not_exist.py::test_fake"],
    )

    validation = service.validate_schema_repair(proposal)

    assert validation.status is AdaptiveProposalStatus.REJECTED
    assert "UNKNOWN_CANONICAL_FIELD_TARGET" in validation.rejection_codes
    assert f"NON_OFFICIAL_ARTIFACT_TYPE:{fake_official}" in validation.rejection_codes
    assert "OFFICIAL_CROSS_CHECK_REQUIRED" in validation.rejection_codes
    assert any(code.startswith("CONTRACT_TEST_NOT_FOUND:") for code in validation.rejection_codes)
