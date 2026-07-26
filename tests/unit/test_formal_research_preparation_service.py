from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.hashing import content_hash
from astock.documents import DocumentPageRepository, DocumentRepository
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.financial_integrity import FinancialIntegrityService
from astock.research import (
    EvidenceCollectionRunService,
    EvidenceCollectionTaskService,
    EvidencePackService,
    FormalResearchPreparationService,
    ResearchPreparationRejectedError,
    load_research_core_config,
)
from astock.schemas import (
    ClaimStatus,
    ClaimType,
    EvidenceAttachment,
    EvidenceCollectionRun,
    EvidenceCollectionRunStatus,
    EvidencePack,
    EvidenceRelation,
    FinancialAuditRequest,
    FinancialFieldCode,
    FinancialIndustryProfile,
    ResearchPreparationRequest,
    ResearchPreparationStatus,
    ResearchRequest,
)
from tests.helpers import make_financial_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 6, 30, 7, 0, tzinfo=UTC)
CLAIM_AS_OF = datetime(2026, 3, 20, 1, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Seed:
    service: FormalResearchPreparationService
    request: ResearchPreparationRequest
    request_artifact_id: str
    task_artifact_id: str
    run_artifact_id: str
    pack_artifact_id: str
    pack_object_hash: str
    claim_id: str
    financial_audit_run_id: str
    evidence_snapshot_id: str


def _financial_service(state, object_store) -> FinancialIntegrityService:
    return FinancialIntegrityService(
        state,
        object_store,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=(
            PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml"
        ),
    )


def _seed(
    state,
    object_store,
    *,
    empty_run: bool = False,
    financial_status: str = "SUCCEEDED",
    claim_company_id: str = "300750",
    financial_company_id: str = "300750",
    financial_as_of: datetime = datetime(2026, 3, 21, tzinfo=UTC),
    open_conflict: bool = False,
) -> _Seed:
    ticker = "300750"
    research_request = ResearchRequest(company="宁德时代", ticker=ticker)
    request_ref = object_store.put_json(research_request.model_dump(mode="json"))
    request_artifact_id = f"ResearchRequest:{content_hash(research_request)}"
    state.register_artifact(
        artifact_id=request_artifact_id,
        artifact_type="ResearchRequest",
        schema_version=research_request.schema_version,
        object_hash=request_ref.sha256,
        input_hashes=[content_hash(research_request)],
    )
    task_execution = EvidenceCollectionTaskService(state, object_store).create_task(
        request_artifact_id
    )

    facts = make_financial_facts(
        state,
        object_store,
        source_suffix="formal-preparation-recorded",
        company_id=ticker,
    )
    evidence_id = facts[0].evidence_ids[0]
    evidence = EvidenceRepository(state).get_evidence(evidence_id)
    assert evidence is not None
    attachments = [
        EvidenceAttachment(
            evidence_id=evidence_id,
            relation=EvidenceRelation.SUPPORT,
        )
    ]
    if open_conflict:
        conflict_facts = make_financial_facts(
            state,
            object_store,
            source_suffix="formal-preparation-conflict",
            company_id=ticker,
        )
        attachments.append(
            EvidenceAttachment(
                evidence_id=conflict_facts[0].evidence_ids[0],
                relation=EvidenceRelation.REFUTE,
            )
        )
    claim = ClaimEvidenceService(
        object_store,
        state,
        DocumentPageRepository(state),
        DocumentRepository(state),
        EvidenceRepository(state),
    ).create_claim(
        subject_id=claim_company_id,
        predicate="recorded_formal_preparation_scope",
        object_json={"fixture": "industrial_annual_2025"},
        as_of=CLAIM_AS_OF,
        claim_type=ClaimType.FACT,
        confidence=0.9,
        status=ClaimStatus.VALIDATED,
        attachments=attachments,
    )
    claim_artifact_id = f"ClaimEvidenceBundle:{claim.claim.claim_id}"

    if financial_status == "NOT_FOUND":
        financial_audit_run_id = "financial-audit:not-found"
    else:
        audit_facts = facts
        if financial_company_id != ticker:
            audit_facts = make_financial_facts(
                state,
                object_store,
                source_suffix="formal-preparation-financial-mismatch",
                company_id=financial_company_id,
            )
        if financial_status == "NEEDS_INFO":
            audit_facts = [
                fact
                for fact in facts
                if fact.field_code is not FinancialFieldCode.INVENTORY
            ]
        financial = _financial_service(state, object_store).run(
            FinancialAuditRequest(
                company_id=financial_company_id,
                as_of=financial_as_of,
                industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
                facts=audit_facts,
            )
        )
        financial_audit_run_id = financial.pack.audit_run_id

    if empty_run:
        run_execution = EvidenceCollectionRunService(state, object_store).create_run(
            task_execution.artifact_id
        )
        run_artifact_id = run_execution.artifact_id
    else:
        now = datetime(2026, 3, 21, 1, 0, tzinfo=UTC)
        run = EvidenceCollectionRun(
            task_artifact_id=task_execution.artifact_id,
            status=EvidenceCollectionRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            collected_items=[claim_artifact_id],
            missing_items=[],
        )
        run_ref = object_store.put_json(run.model_dump(mode="json"))
        run_artifact_id = (
            "EvidenceCollectionRun:"
            + content_hash({"task_artifact_id": task_execution.artifact_id})
        )
        state.register_artifact(
            artifact_id=run_artifact_id,
            artifact_type="EvidenceCollectionRun",
            schema_version=run.schema_version,
            object_hash=run_ref.sha256,
            input_hashes=[task_execution.object_sha256],
        )
    pack_execution = EvidencePackService(state, object_store).create_pack(
        run_artifact_id
    )
    request = ResearchPreparationRequest(
        research_request_artifact_id=request_artifact_id,
        evidence_pack_artifact_id=pack_execution.artifact_id,
        financial_audit_run_id=financial_audit_run_id,
        claim_ids=[claim.claim.claim_id],
        as_of=AS_OF,
    )
    return _Seed(
        service=FormalResearchPreparationService(
            state,
            object_store,
            load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml"),
        ),
        request=request,
        request_artifact_id=request_artifact_id,
        task_artifact_id=task_execution.artifact_id,
        run_artifact_id=run_artifact_id,
        pack_artifact_id=pack_execution.artifact_id,
        pack_object_hash=pack_execution.object_sha256,
        claim_id=claim.claim.claim_id,
        financial_audit_run_id=financial_audit_run_id,
        evidence_snapshot_id=evidence.snapshot_id,
    )


def _artifact_count(state, artifact_type: str) -> int:
    with state.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM artifact_registry WHERE type=?",
                (artifact_type,),
            ).fetchone()[0]
        )


def test_research_preparation_recorded_vertical_slice_reuses_frozen_pack(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store)

    first = seed.service.prepare(seed.request)
    assert first.manifest.status is ResearchPreparationStatus.READY_FOR_BASE_CASE
    assert first.manifest.blocking_codes == []
    assert first.manifest.required_action_codes == []
    assert first.manifest.company_id == "300750"
    assert first.manifest.ticker == "300750"
    assert first.manifest.frozen_evidence_pack_id is not None
    assert first.manifest.frozen_evidence_pack_artifact_id == (
        f"FrozenEvidencePack:{first.manifest.frozen_evidence_pack_id}"
    )
    assert object_store.verify(first.manifest_object_sha256)
    assert _artifact_count(state, "FrozenEvidencePack") == 1
    assert _artifact_count(state, "ResearchPreparationManifest") == 1
    assert _artifact_count(state, "BaseCasePack") == 0
    assert _artifact_count(state, "SpecialistDelta") == 0
    assert _artifact_count(state, "CommitteeDecision") == 0

    repeated = seed.service.prepare(seed.request)
    assert repeated.reused_existing
    assert repeated.manifest_artifact_id == first.manifest_artifact_id
    assert repeated.manifest_object_sha256 == first.manifest_object_sha256
    assert repeated.manifest.frozen_evidence_pack_id == (
        first.manifest.frozen_evidence_pack_id
    )
    assert _artifact_count(state, "FrozenEvidencePack") == 1
    assert _artifact_count(state, "ResearchPreparationManifest") == 1


def test_research_preparation_empty_run_needs_info_without_downstream_writes(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store, empty_run=True)
    before_journal = 0
    with state.connect() as connection:
        before_journal = int(connection.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert "EVIDENCE_COLLECTION_INCOMPLETE" in execution.manifest.blocking_codes
    assert "FORMAL_EVIDENCE_MISSING" in execution.manifest.required_action_codes
    assert execution.manifest.frozen_evidence_pack_id is None
    assert _artifact_count(state, "FrozenEvidencePack") == 0
    assert _artifact_count(state, "BaseCasePack") == 0
    assert _artifact_count(state, "SpecialistDelta") == 0
    assert _artifact_count(state, "CommitteeDecision") == 0
    with state.connect() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM journal").fetchone()[0]) == (
            before_journal
        )


def test_research_preparation_financial_audit_not_found_needs_info(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store, financial_status="NOT_FOUND")
    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert execution.manifest.required_action_codes == ["FINANCIAL_AUDIT_NOT_FOUND"]
    assert execution.manifest.frozen_evidence_pack_id is None
    repeated = seed.service.prepare(seed.request)
    assert repeated.reused_existing
    assert repeated.manifest_artifact_id == execution.manifest_artifact_id


def test_research_preparation_financial_audit_needs_info_references_manual_tasks(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store, financial_status="NEEDS_INFO")
    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert "FINANCIAL_AUDIT_NEEDS_INFO" in execution.manifest.required_action_codes
    assert execution.manifest.financial_manual_task_ids
    assert execution.manifest.frozen_evidence_pack_id is None


def test_research_preparation_financial_hard_block_stops_freeze(
    state,
    object_store,
    monkeypatch,
) -> None:
    seed = _seed(state, object_store)
    record = seed.service.financial_repository.get_run(seed.financial_audit_run_id)
    pack = seed.service.financial_repository.get_pack(seed.financial_audit_run_id)
    assert record is not None and record.report_object_hash is not None and pack is not None
    hard_blocked = pack.model_copy(update={"hard_blocks": ["RECORDED_HARD_BLOCK"]})

    def load_hard_blocked(*_args):
        return hard_blocked, record.report_object_hash

    monkeypatch.setattr(seed.service, "_load_financial_pack", load_hard_blocked)
    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert execution.manifest.required_action_codes == ["FINANCIAL_HARD_BLOCK"]
    assert _artifact_count(state, "FrozenEvidencePack") == 0


def test_research_preparation_missing_claim_needs_info(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store)
    request = seed.request.model_copy(update={"claim_ids": ["claim:not-found"]})
    execution = seed.service.prepare(request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert execution.manifest.required_action_codes == ["CLAIM_SCOPE_EMPTY"]


def test_research_preparation_claim_company_mismatch_needs_info(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store, claim_company_id="000001")
    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert execution.manifest.required_action_codes == ["CLAIM_COMPANY_MISMATCH"]


def test_research_preparation_open_evidence_conflict_needs_info(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store, open_conflict=True)
    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert execution.manifest.required_action_codes == ["OPEN_EVIDENCE_CONFLICT"]
    assert _artifact_count(state, "FrozenEvidencePack") == 0


@pytest.mark.parametrize(
    ("mismatch", "error_match"),
    [
        ("company", "financial audit company mismatch"),
        ("as_of", "financial audit is newer"),
    ],
)
def test_research_preparation_rejects_financial_company_or_as_of_mismatch(
    state,
    object_store,
    mismatch: str,
    error_match: str,
) -> None:
    seed = (
        _seed(state, object_store, financial_company_id="000001")
        if mismatch == "company"
        else _seed(
            state,
            object_store,
            financial_as_of=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    with pytest.raises(ResearchPreparationRejectedError, match=error_match):
        seed.service.prepare(seed.request)
    assert _artifact_count(state, "ResearchPreparationManifest") == 0


def test_research_preparation_missing_pit_needs_info(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store)
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM point_in_time_metadata WHERE source_snapshot_id=?",
            (seed.evidence_snapshot_id,),
        )
    execution = seed.service.prepare(seed.request)
    assert execution.manifest.status is ResearchPreparationStatus.NEEDS_INFO
    assert execution.manifest.required_action_codes == ["PIT_METADATA_REQUIRED"]
    assert _artifact_count(state, "FrozenEvidencePack") == 0


@pytest.mark.parametrize(
    "stage",
    ["research_request", "task", "run", "evidence_pack"],
)
def test_research_preparation_rejects_collection_lineage_mismatch(
    state,
    object_store,
    stage: str,
) -> None:
    seed = _seed(state, object_store, financial_status="NOT_FOUND")
    if stage == "research_request":
        other = ResearchRequest(company="其他公司", ticker="000001")
        other_ref = object_store.put_json(other.model_dump(mode="json"))
        other_id = "ResearchRequest:other"
        state.register_artifact(
            artifact_id=other_id,
            artifact_type="ResearchRequest",
            schema_version=other.schema_version,
            object_hash=other_ref.sha256,
            input_hashes=[other_ref.sha256],
        )
        request = seed.request.model_copy(
            update={"research_request_artifact_id": other_id}
        )
    else:
        with state.connect() as connection:
            pack_row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (seed.pack_artifact_id,),
            ).fetchone()
        assert pack_row is not None
        pack = EvidencePack.model_validate_json(
            object_store.get_bytes(str(pack_row["object_hash"]))
        )
        with state.connect() as connection:
            run_row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (seed.run_artifact_id,),
            ).fetchone()
        assert run_row is not None
        if stage == "evidence_pack":
            pack = pack.model_copy(update={"company": "其他公司"})
        inputs = [str(run_row["object_hash"])]
        bad_ref = object_store.put_json(pack.model_dump(mode="json"))
        bad_pack_id = f"EvidencePack:bad-{stage}"
        state.register_artifact(
            artifact_id=bad_pack_id,
            artifact_type="EvidencePack",
            schema_version=pack.schema_version,
            object_hash=bad_ref.sha256,
            input_hashes=inputs,
        )
        request = seed.request.model_copy(
            update={"evidence_pack_artifact_id": bad_pack_id}
        )
        if stage == "task":
            with state.transaction() as connection:
                connection.execute(
                    "UPDATE artifact_registry SET input_hashes_json='[]' "
                    "WHERE artifact_id=?",
                    (seed.task_artifact_id,),
                )
        elif stage == "run":
            with state.transaction() as connection:
                connection.execute(
                    "UPDATE artifact_registry SET input_hashes_json='[]' "
                    "WHERE artifact_id=?",
                    (seed.run_artifact_id,),
                )
    with pytest.raises(ResearchPreparationRejectedError):
        seed.service.prepare(request)
    assert _artifact_count(state, "ResearchPreparationManifest") == 0


def test_research_preparation_rejects_wrong_artifact_type(
    state,
    object_store,
) -> None:
    seed = _seed(state, object_store, financial_status="NOT_FOUND")
    request = seed.request.model_copy(
        update={"evidence_pack_artifact_id": seed.request_artifact_id}
    )
    with pytest.raises(ResearchPreparationRejectedError, match="not EvidencePack"):
        seed.service.prepare(request)
    assert _artifact_count(state, "ResearchPreparationManifest") == 0


@pytest.mark.parametrize("failure_mode", ["missing", "corrupt"])
def test_research_preparation_rejects_missing_or_corrupt_object(
    state,
    object_store,
    failure_mode: str,
) -> None:
    seed = _seed(state, object_store, financial_status="NOT_FOUND")
    object_path = object_store.path_for(seed.pack_object_hash)
    if failure_mode == "missing":
        object_path.unlink()
    else:
        object_path.write_bytes(b"corrupted-object")
    with pytest.raises(ResearchPreparationRejectedError, match="object is unavailable"):
        seed.service.prepare(seed.request)
    assert _artifact_count(state, "ResearchPreparationManifest") == 0
