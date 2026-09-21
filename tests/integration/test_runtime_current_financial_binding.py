"""Current audit arrival is not a historical gate; identity and bytes stay binding.

All inputs are isolated recorded fixtures produced by canonical services. These
cases do not assert live data access, investment quality, or a finished research run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from astock.acceptance.phase6 import Phase6RecordedService
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_integrity import FinancialIntegrityService
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
from astock.research.runtime import ResearchRunService
from astock.schemas.financial import FinancialAuditRequest, FinancialIntegrityEvidencePack
from astock.schemas.research_runtime import (
    ResearchRunFrozenInputs,
    ResearchRunMode,
    ResearchRunRequest,
    ResearchRunStage,
    ResearchRunStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RuntimeFixture:
    state: StateStore
    objects: ObjectStore
    runtime: ResearchRunService
    financial: FinancialIntegrityService
    request: ResearchRunRequest
    audit_request: FinancialAuditRequest
    original_artifact_id: str
    original_object_hash: str
    original_bytes: bytes


@pytest.fixture
def fixture(tmp_path: Path) -> RuntimeFixture:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    parquet = tmp_path / "parquet"
    recorded = Phase6RecordedService(PROJECT_ROOT, state, objects, parquet).run("300750")
    as_of = recorded.committee_decision.as_of
    report = recorded.report
    financial = FinancialIntegrityService(
        state,
        objects,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml",
    )
    original_id = report.financial_integrity_artifact_id
    original = state.artifact_record(original_id)
    assert original is not None
    original_hash = str(original["object_hash"])
    original_bytes = objects.get_bytes(original_hash)
    pack = FinancialIntegrityEvidencePack.model_validate_json(original_bytes)
    audit = financial.repository.get_run(pack.audit_run_id)
    assert audit is not None
    audit_request = FinancialAuditRequest.model_validate_json(
        objects.get_bytes(audit.request_object_hash)
    )
    request = ResearchRunRequest(
        company_id="300750",
        as_of=as_of,
        mode=ResearchRunMode.RECORDED_INPUT,
        frozen_inputs=ResearchRunFrozenInputs(
            frozen_evidence_pack_artifact_id=report.frozen_evidence_pack_artifact_id,
            financial_integrity_artifact_id=original_id,
            created_at=as_of,
        ),
        institutional_research_required=True,
        auto_resolve_inputs=False,
        sync_reference_inputs=False,
        created_at=as_of,
    )
    runtime = ResearchRunService(
        project_root=PROJECT_ROOT,
        state=state,
        objects=objects,
        reference_parquet_root=parquet,
        knowledge_provider=RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state), objects
        ),
    )
    return RuntimeFixture(
        state, objects, runtime, financial, request, audit_request,
        original_id, original_hash, original_bytes,
    )


def _with_financial(fixture: RuntimeFixture, artifact_id: str) -> ResearchRunRequest:
    assert fixture.request.frozen_inputs is not None
    return fixture.request.model_copy(
        update={
            "frozen_inputs": fixture.request.frozen_inputs.model_copy(
                update={"financial_integrity_artifact_id": artifact_id}
            )
        }
    )


@pytest.mark.parametrize("offset_minutes", [-1, 0, 1, 60])
def test_current_runtime_consumes_registered_audit_without_request_time_cutoff(
    fixture: RuntimeFixture, offset_minutes: int,
) -> None:
    # Re-audit unchanged source facts through the canonical service at a distinct
    # audit time, rather than editing an immutable pack or forging a report hash.
    audit_time = fixture.request.as_of + timedelta(minutes=offset_minutes)
    execution = fixture.financial.run(
        fixture.audit_request.model_copy(update={"as_of": audit_time, "created_at": audit_time})
    )
    artifact_id = f"FinancialIntegrityEvidencePack:{execution.pack.audit_run_id}"
    record = fixture.state.artifact_record(artifact_id)
    assert record is not None
    result = fixture.runtime.run(_with_financial(fixture, artifact_id))
    assert result.output_artifacts["financial_integrity"].artifact_id == artifact_id
    assert result.output_artifacts["financial_integrity"].object_hash == record["object_hash"]
    assert result.as_of == fixture.request.as_of
    assert result.status is ResearchRunStatus.NEEDS_INFO
    assert result.current_stage is ResearchRunStage.FUNDAMENTAL_MODEL
    assert "FUNDAMENTAL_MODEL_BUNDLE_REQUIRED" in result.needs_info_codes
    assert result.paper_ledger_write_count == 0
    assert fixture.objects.get_bytes(fixture.original_object_hash) == fixture.original_bytes
    assert fixture.state.artifact_record(fixture.original_artifact_id) is not None
    first_report_id = f"ResearchRunReport:{result.report_id}"
    first_record = fixture.state.artifact_record(first_report_id)
    assert first_record is not None
    first_bytes = fixture.objects.get_bytes(str(first_record["object_hash"]))
    repeated = fixture.runtime.run(_with_financial(fixture, artifact_id))
    # Each attempt has its own telemetry and previous-report lineage. Reuse means
    # the request and financial output remain identical, not deleting attempt history.
    assert repeated.run_id == result.run_id
    assert repeated.request_artifact_id == result.request_artifact_id
    assert repeated.previous_report_artifact_id == first_report_id
    assert fixture.objects.get_bytes(str(first_record["object_hash"])) == first_bytes
    assert (
        repeated.output_artifacts["financial_integrity"].model_dump(exclude={"created_at"})
        == result.output_artifacts["financial_integrity"].model_dump(exclude={"created_at"})
    )


def test_current_runtime_still_rejects_foreign_company_audit(fixture: RuntimeFixture) -> None:
    # A separately registered wrong-company envelope tests the unchanged identity
    # invariant; it is deliberately not a successful canonical financial audit.
    payload = FinancialIntegrityEvidencePack.model_validate_json(fixture.original_bytes)
    foreign = payload.model_copy(update={"company_id": "600000", "audit_run_id": "foreign"})
    reference = fixture.objects.put_json(foreign.model_dump(mode="json"))
    artifact_id = "FinancialIntegrityEvidencePack:foreign"
    fixture.state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="FinancialIntegrityEvidencePack",
        schema_version=foreign.schema_version,
        object_hash=reference.sha256,
        input_hashes=[fixture.original_object_hash],
    )
    with pytest.raises(ValueError, match="company"):
        fixture.runtime.run(_with_financial(fixture, artifact_id))
    assert fixture.objects.get_bytes(fixture.original_object_hash) == fixture.original_bytes


def test_current_runtime_still_rejects_corrupted_audit_bytes(fixture: RuntimeFixture) -> None:
    # Corruption is confined to this test's temporary object store.
    fixture.objects.path_for(fixture.original_object_hash).write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        fixture.runtime.run(fixture.request)
