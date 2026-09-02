from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.external_capabilities import ExternalCapabilityService
from astock.external_capability_trials import (
    load_capability_qualification_evidence,
    register_capability_qualification_evidence,
)
from astock.research.observability import AgentObservabilityService
from astock.schemas.agent_observability import (
    AgentTaskObservationRequest,
    AgentTaskStatus,
)
from astock.schemas.external_capabilities import (
    CapabilityQualificationReport,
    CapabilityRevocation,
    ExternalCapabilityStage,
    QualificationCheckStatus,
    capability_revocation_id,
)
from astock.schemas.external_capability_trials import CapabilityQualificationEvidence
from astock.schemas.market import (
    AccessTransport,
    CompletenessSemantics,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = PROJECT_ROOT / "third_party" / "qualifications" / "e02"
REPORT_ROOT = EVIDENCE_ROOT / "reports"
CANDIDATES = {
    "arelle",
    "docling",
    "playwright-mcp",
    "akshare",
    "crawl4ai",
    "changedetection-io",
    "source-qualification-auditor",
    "report-visual-qa",
    "schema-drift-recorder",
}
EXTERNAL_CANDIDATES = {
    "arelle",
    "docling",
    "playwright-mcp",
    "akshare",
    "crawl4ai",
    "changedetection-io",
}
SKILL_CANDIDATES = {
    "source-qualification-auditor",
    "report-visual-qa",
    "schema-drift-recorder",
}


def _service(tmp_path: Path) -> tuple[ExternalCapabilityService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return ExternalCapabilityService(PROJECT_ROOT, state, objects), state, objects


def _load(capability_id: str) -> CapabilityQualificationEvidence:
    return load_capability_qualification_evidence(EVIDENCE_ROOT / f"{capability_id}.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _transport(source_id: str, *, available: bool, backup: bool = False) -> TransportCapability:
    return TransportCapability(
        source_id=source_id,
        transport=AccessTransport.API,
        requested_capabilities=["governance.external.qualification"],
        available=available,
        reason="e02-exit-drill",
        formal_eligible=False,
        completeness_semantics=CompletenessSemantics.EXACT_ITEM,
        completeness_score=Decimal("1"),
        production_backup=backup,
        external_capability_id="source-qualification-auditor" if backup else None,
        qualification_valid=True,
    )


def test_e02_tracked_evidence_covers_exact_candidate_set_and_freezes_current_versions() -> None:
    assert {path.stem for path in EVIDENCE_ROOT.glob("*.json")} == CANDIDATES
    assert {path.stem for path in REPORT_ROOT.glob("*.json")} == CANDIDATES
    frozen_versions = {
        "arelle": "2.44.6",
        "docling": "2.124.0",
        "playwright-mcp": "0.0.80",
        "akshare": "1.18.94",
        "crawl4ai": "0.9.3",
        "changedetection-io": "0.55.8",
    }
    for capability_id, version in frozen_versions.items():
        evidence = _load(capability_id)
        assert evidence.candidate_version == version
        assert evidence.admitted_stage is ExternalCapabilityStage.SHADOW
        assert evidence.recorded_validation is QualificationCheckStatus.FAIL
        assert evidence.controlled_live_validation is QualificationCheckStatus.FAIL
        assert evidence.reason_codes
        assert not evidence.checks.all_pass()

    source_auditor = _load("source-qualification-auditor")
    assert source_auditor.admitted_stage is ExternalCapabilityStage.PRODUCTION_BACKUP
    assert source_auditor.checks.all_pass()
    assert source_auditor.recorded_validation is QualificationCheckStatus.PASS
    assert source_auditor.controlled_live_validation is QualificationCheckStatus.PASS

    for capability_id in {"report-visual-qa", "schema-drift-recorder"}:
        evidence = _load(capability_id)
        assert evidence.admitted_stage is ExternalCapabilityStage.SHADOW
        assert evidence.controlled_live_validation is QualificationCheckStatus.FAIL
        assert evidence.reason_codes


def test_e02_skill_evidence_hashes_bind_actual_repo_contracts() -> None:
    for capability_id in SKILL_CANDIDATES:
        evidence = _load(capability_id)
        skill_path = PROJECT_ROOT / ".agents" / "skills" / capability_id / "SKILL.md"
        skill_source = next(
            item
            for item in evidence.sources
            if item.source_ref.endswith(f"/{capability_id}/SKILL.md")
        )
        assert skill_source.sha256 == _sha256(skill_path)
        assert evidence.candidate_version == f"sha256:{_sha256(skill_path)}"
    source_auditor = _load("source-qualification-auditor")
    assert next(
        item for item in source_auditor.sources if item.source_ref == "repo:uv.lock"
    ).sha256 == _sha256(PROJECT_ROOT / "uv.lock")


def test_e02_all_tracked_evidence_materializes_through_canonical_m06(tmp_path: Path) -> None:
    service, _state, objects = _service(tmp_path)
    for capability_id in sorted(CANDIDATES):
        evidence_path = EVIDENCE_ROOT / f"{capability_id}.json"
        evidence = _load(capability_id)
        frozen_report = CapabilityQualificationReport.model_validate_json(
            (REPORT_ROOT / f"{capability_id}.json").read_bytes()
        )
        saved = register_capability_qualification_evidence(
            service,
            objects,
            evidence_path,
        )
        assert saved == frozen_report
        assert saved.evidence_object_hashes == [_sha256(evidence_path)]
        assert saved.capability_id == capability_id
        active = service.active_report(capability_id, at=evidence.valid_from + timedelta(seconds=1))
        assert active is not None
        assert active.report_id == saved.report_id
        assert active.admitted_stage is evidence.admitted_stage

    source_auditor = _load("source-qualification-auditor")
    at = source_auditor.valid_from + timedelta(seconds=1)
    assert service.production_backup_allowed(
        capability_id="source-qualification-auditor",
        logical_capability="governance.external.qualification",
        primary_available=False,
        at=at,
    )
    assert not service.production_backup_allowed(
        capability_id="source-qualification-auditor",
        logical_capability="governance.external.qualification",
        primary_available=True,
        at=at,
    )


def test_source_qualification_skill_controlled_live_observability_and_exit_drill(
    tmp_path: Path,
) -> None:
    service, state, objects = _service(tmp_path)
    evidence = _load("source-qualification-auditor")
    saved = register_capability_qualification_evidence(
        service,
        objects,
        EVIDENCE_ROOT / "source-qualification-auditor.json",
    )

    observability = AgentObservabilityService(
        state,
        objects,
        project_root=PROJECT_ROOT,
        manifest_root=tmp_path / "manifests",
    )
    observation = observability.register(
        AgentTaskObservationRequest(
            task_id="e02-source-qualification-controlled-live",
            task_status=AgentTaskStatus.COMPLETED,
            eligible_skill_ids=["source-qualification-auditor"],
            selected_skill_ids=["source-qualification-auditor"],
            completed_skill_ids=["source-qualification-auditor"],
            expected_skill_ids=["source-qualification-auditor"],
            duration_ms=1,
        )
    )
    assert observation.completed_skill_ids == ["source-qualification-auditor"]
    report = observability.report(lookback_days=0)
    summary = next(
        item for item in report.skill_summaries if item.skill_id == "source-qualification-auditor"
    )
    assert summary.selected_task_count == 1
    assert summary.completed_task_count == 1

    router = SourceAccessRouter()
    request = SourceAccessRequest(requested_capability="governance.external.qualification")
    primary = _transport("primary", available=True)
    backup = _transport("qualified-skill", available=True, backup=True)
    assert router.decide(request, [primary, backup]).selected_source_id == "primary"
    assert (
        router.decide(request, [_transport("primary", available=False), backup]).selected_source_id
        == "qualified-skill"
    )

    revoked_at = evidence.valid_from + timedelta(minutes=1)
    reason = "E-02 controlled exit drill"
    revocation = CapabilityRevocation(
        revocation_id=capability_revocation_id(
            capability_id=evidence.capability_id,
            report_id=saved.report_id,
            revoked_at=revoked_at,
            reason=reason,
        ),
        capability_id=evidence.capability_id,
        report_id=saved.report_id,
        revoked_at=revoked_at,
        reason=reason,
    )
    service.revoke(revocation)
    assert (
        service.active_report(evidence.capability_id, at=revoked_at + timedelta(seconds=1)) is None
    )
    assert router.decide(request, [primary, backup]).selected_source_id == "primary"

    isolated_root = tmp_path / "isolated-project"
    isolated_skill = isolated_root / ".agents" / "skills" / "source-qualification-auditor"
    shutil.copytree(
        PROJECT_ROOT / ".agents" / "skills" / "source-qualification-auditor",
        isolated_skill,
    )
    isolated_observability = AgentObservabilityService(
        state,
        objects,
        project_root=isolated_root,
        manifest_root=tmp_path / "isolated-manifests",
    )
    isolated_observability.register(
        AgentTaskObservationRequest(
            task_id="e02-before-uninstall",
            task_status=AgentTaskStatus.COMPLETED,
            eligible_skill_ids=["source-qualification-auditor"],
            selected_skill_ids=["source-qualification-auditor"],
            completed_skill_ids=["source-qualification-auditor"],
            expected_skill_ids=["source-qualification-auditor"],
            duration_ms=1,
        )
    )
    shutil.rmtree(isolated_skill)
    with pytest.raises(ValueError, match="unknown canonical Agent Skills"):
        isolated_observability.register(
            AgentTaskObservationRequest(
                task_id="e02-after-uninstall",
                task_status=AgentTaskStatus.NEEDS_INFO,
                eligible_skill_ids=["source-qualification-auditor"],
                selected_skill_ids=["source-qualification-auditor"],
                completed_skill_ids=[],
                expected_skill_ids=["source-qualification-auditor"],
                duration_ms=1,
            )
        )
    assert service.definition("arelle").capability_id == "arelle"
    assert router.decide(request, [primary]).selected_source_id == "primary"


def test_e02_skill_observability_records_positive_negative_and_conflict_routes(
    tmp_path: Path,
) -> None:
    _service_instance, state, objects = _service(tmp_path)
    observability = AgentObservabilityService(
        state,
        objects,
        project_root=PROJECT_ROOT,
        manifest_root=tmp_path / "manifests",
    )
    requests = [
        AgentTaskObservationRequest(
            task_id="e02-report-visual-incomplete",
            task_status=AgentTaskStatus.NEEDS_INFO,
            eligible_skill_ids=["report-visual-qa"],
            selected_skill_ids=["report-visual-qa"],
            completed_skill_ids=[],
            expected_skill_ids=["report-visual-qa"],
            duration_ms=1,
            finding_codes=["RENDERED_PAGE_INSPECTION_UNAVAILABLE"],
        ),
        AgentTaskObservationRequest(
            task_id="e02-schema-drift-positive",
            task_status=AgentTaskStatus.COMPLETED,
            eligible_skill_ids=["schema-drift-recorder"],
            selected_skill_ids=["schema-drift-recorder"],
            completed_skill_ids=["schema-drift-recorder"],
            expected_skill_ids=["schema-drift-recorder"],
            duration_ms=1,
        ),
        AgentTaskObservationRequest(
            task_id="e02-conflict-route",
            task_status=AgentTaskStatus.COMPLETED,
            eligible_skill_ids=["report-visual-qa", "schema-drift-recorder"],
            selected_skill_ids=["schema-drift-recorder"],
            completed_skill_ids=["schema-drift-recorder"],
            expected_skill_ids=["schema-drift-recorder"],
            duration_ms=1,
        ),
    ]
    for item in requests:
        observability.register(item)
    report = observability.report(lookback_days=0)
    summaries = {item.skill_id: item for item in report.skill_summaries}
    assert summaries["report-visual-qa"].eligible_task_count == 2
    assert summaries["report-visual-qa"].completed_task_count == 0
    assert summaries["schema-drift-recorder"].completed_task_count == 2
    assert report.automatic_skill_modification_allowed is False


def test_e02_all_governance_skills_uninstall_without_core_path_damage(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    isolated_root = tmp_path / "isolated-all-skills"
    for skill_id in sorted(SKILL_CANDIDATES):
        shutil.copytree(
            PROJECT_ROOT / ".agents" / "skills" / skill_id,
            isolated_root / ".agents" / "skills" / skill_id,
        )
    observability = AgentObservabilityService(
        state,
        objects,
        project_root=isolated_root,
        manifest_root=tmp_path / "isolated-all-manifests",
    )
    for skill_id in sorted(SKILL_CANDIDATES):
        observability.register(
            AgentTaskObservationRequest(
                task_id=f"e02-uninstall-before-{skill_id}",
                task_status=AgentTaskStatus.COMPLETED,
                eligible_skill_ids=[skill_id],
                selected_skill_ids=[skill_id],
                completed_skill_ids=[skill_id],
                expected_skill_ids=[skill_id],
                duration_ms=1,
            )
        )
        shutil.rmtree(isolated_root / ".agents" / "skills" / skill_id)
        with pytest.raises(ValueError, match="unknown canonical Agent Skills"):
            observability.register(
                AgentTaskObservationRequest(
                    task_id=f"e02-uninstall-after-{skill_id}",
                    task_status=AgentTaskStatus.NEEDS_INFO,
                    eligible_skill_ids=[skill_id],
                    selected_skill_ids=[skill_id],
                    completed_skill_ids=[],
                    expected_skill_ids=[skill_id],
                    duration_ms=1,
                )
            )

    assert {
        service.definition(skill_id).capability_id for skill_id in SKILL_CANDIDATES
    } == SKILL_CANDIDATES
    router = SourceAccessRouter()
    request = SourceAccessRequest(requested_capability="governance.external.qualification")
    assert (
        router.decide(request, [_transport("primary", available=True)]).selected_source_id
        == "primary"
    )


def test_e02_governance_skill_contracts_protect_facts_permissions_and_broker_boundary() -> None:
    bodies = {
        skill_id: (PROJECT_ROOT / ".agents" / "skills" / skill_id / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for skill_id in SKILL_CANDIDATES
    }
    for body in bodies.values():
        assert "broker_execution_allowed=false" in body
        assert "## Prohibitions" in body
        assert "agent-observation-register" in body
    assert "Do not let a Skill" in bodies["source-qualification-auditor"]
    assert "Do not alter investment facts" in bodies["report-visual-qa"]
    assert "Do not auto-admit a schema repair" in bodies["schema-drift-recorder"]


def test_e02_evidence_rejects_naive_timestamps() -> None:
    evidence = _load("source-qualification-auditor").model_dump(mode="json")
    evidence["valid_from"] = "2026-09-02T06:16:28"
    with pytest.raises(ValidationError, match="timezone info"):
        CapabilityQualificationEvidence.model_validate(evidence)


def test_e02_production_backup_fails_closed_on_incomplete_gate_or_authority_elevation(
    tmp_path: Path,
) -> None:
    evidence = _load("source-qualification-auditor")
    incomplete = evidence.model_dump(mode="json")
    incomplete["checks"]["license"] = "FAIL"
    with pytest.raises(
        ValidationError, match="production backup evidence requires every M-06 check"
    ):
        CapabilityQualificationEvidence.model_validate(incomplete)

    elevated = evidence.model_copy(
        update={"source_class_ceiling": SourceClass.PRIMARY_OFFICIAL_WEB}
    )
    path = tmp_path / "elevated.json"
    path.write_text(elevated.model_dump_json(indent=2), encoding="utf-8")
    service, _state, objects = _service(tmp_path / "authority")
    with pytest.raises(ValueError, match="authority ceiling"):
        register_capability_qualification_evidence(service, objects, path)


def test_e02_external_provider_and_transport_candidates_are_not_hard_promoted() -> None:
    for capability_id in EXTERNAL_CANDIDATES:
        evidence = _load(capability_id)
        assert evidence.admitted_stage is ExternalCapabilityStage.SHADOW
        assert evidence.recorded_validation is QualificationCheckStatus.FAIL
        assert evidence.controlled_live_validation is QualificationCheckStatus.FAIL
        assert not evidence.checks.all_pass()
