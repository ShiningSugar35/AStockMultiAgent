"""Point-in-time evidence freezing and one-pass common BaseCase construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.pit.repository import PointInTimeRepository
from astock.pit.service import PointInTimeService
from astock.research.repository import ResearchRepository
from astock.schemas import (
    BASE_CASE_SECTIONS,
    BaseCaseBuildRequest,
    BaseCasePack,
    BaseCaseSection,
    CitedResearchFinding,
    ClaimEvidenceBundle,
    ClaimStatus,
    ConflictResolutionStatus,
    Evidence,
    EvidenceFreezeRequest,
    EvidenceGrade,
    FrozenEvidencePack,
    PointInTimeStatus,
    ResearchCoreConfig,
    ResearchCoverageStatus,
    ResearchGap,
    ResearchGapInput,
    ResearchGapSeverity,
)


@dataclass(frozen=True, slots=True)
class EvidenceFreezeExecution:
    pack: FrozenEvidencePack
    object_sha256: str


@dataclass(frozen=True, slots=True)
class BaseCaseExecution:
    pack: BaseCasePack
    object_sha256: str


class ResearchCoreService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        config: ResearchCoreConfig,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.config = config
        self.evidence_repository = EvidenceRepository(state)
        self.pit_repository = PointInTimeRepository(state)
        self.repository = ResearchRepository(state, object_store)

    def freeze_evidence(self, request: EvidenceFreezeRequest) -> EvidenceFreezeExecution:
        request = request.model_copy(update={"as_of": request.as_of.astimezone(UTC)})
        bundles = self._claim_bundles(request)
        evidence_by_id = self._evidence_for_bundles(bundles, request)
        pit_id_by_evidence: dict[str, str | None] = {}
        pit_status_by_evidence: dict[str, PointInTimeStatus | None] = {}
        degradation_codes: set[str] = set()
        for evidence_id, evidence in evidence_by_id.items():
            candidates = self.pit_repository.for_snapshot(evidence.snapshot_id)
            if len(candidates) > 1:
                raise ValueError(
                    f"ambiguous PIT lineage for evidence {evidence_id}: "
                    f"{len(candidates)} records"
                )
            if not candidates:
                pit_id_by_evidence[evidence_id] = None
                pit_status_by_evidence[evidence_id] = None
                degradation_codes.add("PIT_METADATA_MISSING")
                if request.formal_historical:
                    raise ValueError(
                        f"formal historical evidence lacks PIT metadata: {evidence_id}"
                    )
                continue
            metadata = candidates[0]
            PointInTimeService.assert_usable(
                metadata,
                request.as_of,
                formal_historical=request.formal_historical,
                allow_approximated=request.allow_approximated,
            )
            pit_id_by_evidence[evidence_id] = metadata.pit_id
            pit_status_by_evidence[evidence_id] = metadata.point_in_time_status
            if metadata.point_in_time_status is PointInTimeStatus.APPROXIMATED:
                degradation_codes.add("APPROXIMATED_PIT_INCLUDED")
            elif metadata.point_in_time_status is PointInTimeStatus.NOT_PIT_SAFE:
                degradation_codes.add("NOT_PIT_SAFE_INCLUDED")

        conflicts = sorted(
            {
                bundle.conflict.conflict_id: bundle.conflict
                for bundle in bundles
                if bundle.conflict is not None
            }.values(),
            key=lambda item: item.conflict_id,
        )
        open_conflict_ids = [
            conflict.conflict_id
            for conflict in conflicts
            if conflict.resolution_status is ConflictResolutionStatus.OPEN
        ]
        if open_conflict_ids:
            degradation_codes.add("OPEN_EVIDENCE_CONFLICT")
        coverage_status = (
            ResearchCoverageStatus.COMPLETE
            if not degradation_codes
            else ResearchCoverageStatus.PARTIAL
        )
        claim_ids = [bundle.claim.claim_id for bundle in bundles]
        evidence_ids = sorted(evidence_by_id)
        frozen_input = {
            "company_id": request.company_id,
            "as_of": request.as_of,
            "formal_historical": request.formal_historical,
            "allow_approximated": request.allow_approximated,
            "claims": [content_hash(bundle) for bundle in bundles],
            "evidence": [content_hash(evidence_by_id[item]) for item in evidence_ids],
            "pit_ids": [pit_id_by_evidence[item] for item in evidence_ids],
        }
        frozen_input_hash = content_hash(frozen_input)
        pack_id = f"frozen-evidence:{frozen_input_hash}"
        existing = self.repository.get_evidence_pack(pack_id)
        if existing is not None:
            object_hash = self.repository.evidence_pack_object_hash(pack_id)
            assert object_hash is not None
            return EvidenceFreezeExecution(pack=existing, object_sha256=object_hash)
        now = datetime.now(UTC)
        pack = FrozenEvidencePack(
            pack_id=pack_id,
            company_id=request.company_id,
            as_of=request.as_of,
            formal_historical=request.formal_historical,
            allow_approximated=request.allow_approximated,
            claim_ids=claim_ids,
            evidence_ids=evidence_ids,
            conflict_ids=[item.conflict_id for item in conflicts],
            open_conflict_ids=open_conflict_ids,
            evidence_grade_by_id={
                evidence_id: evidence_by_id[evidence_id].evidence_grade
                for evidence_id in evidence_ids
            },
            pit_id_by_evidence_id={
                evidence_id: pit_id_by_evidence[evidence_id]
                for evidence_id in evidence_ids
            },
            pit_status_by_evidence_id={
                evidence_id: pit_status_by_evidence[evidence_id]
                for evidence_id in evidence_ids
            },
            missing_pit_evidence_ids=sorted(
                evidence_id
                for evidence_id, pit_id in pit_id_by_evidence.items()
                if pit_id is None
            ),
            coverage_status=coverage_status,
            degradation_codes=sorted(degradation_codes),
            frozen_input_sha256=frozen_input_hash,
            frozen_at=now,
            created_at=now,
        )
        object_ref = self.object_store.put_json(pack.model_dump(mode="json"))
        stored = self.repository.register_evidence_pack(
            pack,
            object_hash=object_ref.sha256,
            request_hash=frozen_input_hash,
        )
        stored_object_hash = self.repository.evidence_pack_object_hash(stored.pack_id)
        assert stored_object_hash is not None
        self.state.register_artifact(
            artifact_id=f"FrozenEvidencePack:{stored.pack_id}",
            artifact_type="FrozenEvidencePack",
            schema_version=stored.schema_version,
            object_hash=stored_object_hash,
            input_hashes=[*stored.evidence_ids, *stored.claim_ids],
        )
        self.state.set_checkpoint(
            scope_type="research-evidence-freeze",
            scope_key=stored.pack_id,
            cursor={
                "claim_count": len(stored.claim_ids),
                "evidence_count": len(stored.evidence_ids),
            },
            status="SUCCEEDED",
            object_hash=stored_object_hash,
        )
        return EvidenceFreezeExecution(pack=stored, object_sha256=stored_object_hash)

    def build_base_case(self, request: BaseCaseBuildRequest) -> BaseCaseExecution:
        evidence_pack = self.repository.get_evidence_pack(request.evidence_pack_id)
        if evidence_pack is None:
            raise ValueError(f"unknown frozen evidence pack: {request.evidence_pack_id}")
        if request.draft.company_id != evidence_pack.company_id:
            raise ValueError("BaseCase company must match the frozen evidence pack")
        if request.draft.as_of != evidence_pack.as_of:
            raise ValueError("BaseCase as_of must match the frozen evidence pack")
        draft = request.draft.model_copy(update={"as_of": evidence_pack.as_of})
        draft_hash = content_hash(draft)
        identity = {
            "evidence_pack_id": evidence_pack.pack_id,
            "kernel_version": self.config.kernel_version,
            "draft_hash": draft_hash,
        }
        base_case_id = f"base-case:{content_hash(identity)}"
        existing = self.repository.get_base_case(base_case_id)
        if existing is not None:
            object_hash = self.repository.base_case_object_hash(base_case_id)
            assert object_hash is not None
            return BaseCaseExecution(pack=existing, object_sha256=object_hash)

        findings_by_section: dict[BaseCaseSection, list[CitedResearchFinding]] = {}
        finding_ids: set[str] = set()
        cited_evidence_ids: set[str] = set()
        for section in BASE_CASE_SECTIONS:
            section_findings: list[CitedResearchFinding] = []
            for finding in draft.findings_by_section[section]:
                unknown = sorted(set(finding.evidence_ids) - set(evidence_pack.evidence_ids))
                if unknown:
                    raise ValueError(
                        "BaseCase finding references evidence outside the frozen pack: "
                        + ", ".join(unknown)
                    )
                if finding.critical and not any(
                    evidence_pack.evidence_grade_by_id[evidence_id]
                    is EvidenceGrade.PRIMARY_OFFICIAL
                    for evidence_id in finding.evidence_ids
                ):
                    raise ValueError(
                        "critical BaseCase findings require PRIMARY_OFFICIAL evidence"
                    )
                finding_identity = {
                    "base_case_id": base_case_id,
                    "section": section.value,
                    "finding": finding,
                }
                finding_id = f"research-finding:{content_hash(finding_identity)}"
                if finding_id in finding_ids:
                    raise ValueError(f"duplicate BaseCase finding: {finding_id}")
                finding_ids.add(finding_id)
                cited_evidence_ids.update(finding.evidence_ids)
                section_findings.append(
                    CitedResearchFinding(
                        finding_id=finding_id,
                        statement=finding.statement,
                        finding_type=finding.finding_type,
                        confidence=finding.confidence,
                        critical=finding.critical,
                        evidence_ids=finding.evidence_ids,
                        created_at=evidence_pack.frozen_at,
                    )
                )
            findings_by_section[section] = section_findings

        gap_inputs = {gap.gap_code: gap for gap in draft.evidence_gaps}
        for conflict_id in evidence_pack.open_conflict_ids:
            code = f"OPEN_EVIDENCE_CONFLICT:{conflict_id}"
            gap_inputs.setdefault(
                code,
                ResearchGapInput(
                    gap_code=code,
                    severity=ResearchGapSeverity.BLOCKING,
                    decision_impact="Conflicting frozen evidence prevents a high-confidence case.",
                    required_evidence=["RESOLVE_FROZEN_EVIDENCE_CONFLICT"],
                    created_at=evidence_pack.frozen_at,
                ),
            )
        if evidence_pack.missing_pit_evidence_ids:
            code = "PIT_COVERAGE_INCOMPLETE"
            gap_inputs.setdefault(
                code,
                ResearchGapInput(
                    gap_code=code,
                    severity=ResearchGapSeverity.MATERIAL,
                    decision_impact="Some evidence lacks point-in-time metadata.",
                    required_evidence=["REGISTER_POINT_IN_TIME_METADATA"],
                    created_at=evidence_pack.frozen_at,
                ),
            )
        gaps = [
            ResearchGap(
                gap_id=f"research-gap:{content_hash({'base_case_id': base_case_id, 'gap': gap})}",
                gap_code=gap.gap_code,
                severity=gap.severity,
                decision_impact=gap.decision_impact,
                required_evidence=gap.required_evidence,
                created_at=evidence_pack.frozen_at,
            )
            for gap in sorted(gap_inputs.values(), key=lambda item: item.gap_code)
        ]
        coverage_by_section = {
            section: float(bool(findings_by_section[section]))
            for section in BASE_CASE_SECTIONS
        }
        has_blocking_gap = any(
            gap.severity is ResearchGapSeverity.BLOCKING for gap in gaps
        )
        has_empty_section = any(not findings_by_section[item] for item in BASE_CASE_SECTIONS)
        if has_blocking_gap:
            coverage_status = ResearchCoverageStatus.INSUFFICIENT
        elif (
            gaps
            or has_empty_section
            or evidence_pack.coverage_status is not ResearchCoverageStatus.COMPLETE
        ):
            coverage_status = ResearchCoverageStatus.PARTIAL
        else:
            coverage_status = ResearchCoverageStatus.COMPLETE
        degradation_codes = set(evidence_pack.degradation_codes)
        degradation_codes.update(
            gap.gap_code
            for gap in gaps
            if gap.severity in {ResearchGapSeverity.MATERIAL, ResearchGapSeverity.BLOCKING}
        )
        if has_empty_section:
            degradation_codes.add("EMPTY_REQUIRED_SECTION")
        section_coverage_ratio = sum(coverage_by_section.values()) / len(BASE_CASE_SECTIONS)
        confidence_cap = min(
            self.config.confidence_caps[coverage_status],
            section_coverage_ratio,
        )
        if draft.requested_base_confidence > confidence_cap:
            degradation_codes.add("CONFIDENCE_CAPPED_BY_COVERAGE")
        base_confidence = min(
            draft.requested_base_confidence,
            confidence_cap,
        )
        pack = BaseCasePack(
            base_case_id=base_case_id,
            evidence_pack_id=evidence_pack.pack_id,
            company_id=evidence_pack.company_id,
            as_of=evidence_pack.as_of,
            kernel_version=self.config.kernel_version,
            findings_by_section=findings_by_section,
            evidence_gaps=gaps,
            specialist_tags=sorted(set(draft.specialist_tags)),
            requested_base_confidence=draft.requested_base_confidence,
            base_confidence=base_confidence,
            confidence_cap=confidence_cap,
            coverage_by_section=coverage_by_section,
            coverage_status=coverage_status,
            degradation_codes=sorted(degradation_codes),
            evidence_ids=sorted(cited_evidence_ids),
            created_at=evidence_pack.frozen_at,
        )
        object_ref = self.object_store.put_json(pack.model_dump(mode="json"))
        stored = self.repository.register_base_case(
            pack,
            object_hash=object_ref.sha256,
            draft_hash=draft_hash,
        )
        stored_object_hash = self.repository.base_case_object_hash(stored.base_case_id)
        assert stored_object_hash is not None
        evidence_pack_hash = self.repository.evidence_pack_object_hash(evidence_pack.pack_id)
        assert evidence_pack_hash is not None
        self.state.register_artifact(
            artifact_id=f"BaseCasePack:{stored.base_case_id}",
            artifact_type="BaseCasePack",
            schema_version=stored.schema_version,
            object_hash=stored_object_hash,
            input_hashes=[evidence_pack_hash, draft_hash],
        )
        self.state.set_checkpoint(
            scope_type="research-base-case",
            scope_key=stored.base_case_id,
            cursor={
                "finding_count": len(finding_ids),
                "evidence_count": len(stored.evidence_ids),
                "gap_count": len(stored.evidence_gaps),
            },
            status="SUCCEEDED",
            object_hash=stored_object_hash,
        )
        return BaseCaseExecution(pack=stored, object_sha256=stored_object_hash)

    def audit(self, company_id: str) -> dict[str, object]:
        summary = self.repository.latest_base_case_summary(company_id)
        if summary is None:
            return {"status": "NOT_RUN", "company_id": company_id}
        base_case_id = str(summary["base_case_id"])
        pack = self.repository.get_base_case(base_case_id)
        if pack is None:
            return {
                "status": "PARTIAL",
                "company_id": company_id,
                "finding_codes": ["BASE_CASE_OBJECT_MISSING_OR_INVALID"],
            }
        evidence_pack = self.repository.get_evidence_pack(pack.evidence_pack_id)
        evidence_pack_missing = int(evidence_pack is None)
        frozen_evidence_missing = 0
        frozen_pit_mismatch = 0
        if evidence_pack is not None:
            for evidence_id in evidence_pack.evidence_ids:
                if self.evidence_repository.get_evidence(evidence_id) is None:
                    frozen_evidence_missing += 1
                pit_id = evidence_pack.pit_id_by_evidence_id[evidence_id]
                expected_status = evidence_pack.pit_status_by_evidence_id[evidence_id]
                if pit_id is None:
                    if expected_status is not None:
                        frozen_pit_mismatch += 1
                    continue
                metadata = self.pit_repository.get(pit_id)
                if (
                    metadata is None
                    or metadata.point_in_time_status is not expected_status
                    or metadata.available_to_system_at > pack.as_of
                ):
                    frozen_pit_mismatch += 1
        missing_evidence = 0
        out_of_scope_evidence = 0
        future_evidence = 0
        critical_grade_mismatch = 0
        evidence_scope = set(evidence_pack.evidence_ids) if evidence_pack else set()
        for finding in (
            finding
            for section in BASE_CASE_SECTIONS
            for finding in pack.findings_by_section[section]
        ):
            evidence_records = [
                self.evidence_repository.get_evidence(evidence_id)
                for evidence_id in finding.evidence_ids
            ]
            missing_evidence += sum(item is None for item in evidence_records)
            out_of_scope_evidence += sum(
                evidence_id not in evidence_scope for evidence_id in finding.evidence_ids
            )
            future_evidence += sum(
                item is not None and item.available_to_system_at > pack.as_of
                for item in evidence_records
            )
            if finding.critical and not any(
                item is not None and item.evidence_grade is EvidenceGrade.PRIMARY_OFFICIAL
                for item in evidence_records
            ):
                critical_grade_mismatch += 1
        metadata_count_mismatch = int(
            int(str(summary["finding_count"]))
            != sum(len(items) for items in pack.findings_by_section.values())
            or int(str(summary["evidence_count"])) != len(pack.evidence_ids)
            or int(str(summary["gap_count"])) != len(pack.evidence_gaps)
        )
        base_object_hash = self.repository.base_case_object_hash(pack.base_case_id)
        evidence_pack_object_hash = self.repository.evidence_pack_object_hash(
            pack.evidence_pack_id
        )
        with self.state.connect() as connection:
            base_artifact = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (f"BaseCasePack:{pack.base_case_id}",),
            ).fetchone()
            evidence_artifact = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (f"FrozenEvidencePack:{pack.evidence_pack_id}",),
            ).fetchone()
        artifact_registry_mismatch = int(
            base_object_hash is None
            or base_artifact is None
            or str(base_artifact["object_hash"]) != base_object_hash
            or evidence_pack_object_hash is None
            or evidence_artifact is None
            or str(evidence_artifact["object_hash"]) != evidence_pack_object_hash
        )
        findings = {
            "EVIDENCE_PACK_MISSING": evidence_pack_missing,
            "FROZEN_EVIDENCE_MISSING": frozen_evidence_missing,
            "FROZEN_PIT_MISMATCH": frozen_pit_mismatch,
            "EVIDENCE_RECORD_MISSING": missing_evidence,
            "EVIDENCE_OUTSIDE_FROZEN_SCOPE": out_of_scope_evidence,
            "FUTURE_EVIDENCE": future_evidence,
            "CRITICAL_EVIDENCE_GRADE_MISMATCH": critical_grade_mismatch,
            "METADATA_COUNT_MISMATCH": metadata_count_mismatch,
            "ARTIFACT_REGISTRY_MISMATCH": artifact_registry_mismatch,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "company_id": company_id,
            "base_case_id": base_case_id,
            "evidence_pack_id": pack.evidence_pack_id,
            "coverage_status": pack.coverage_status,
            "finding_count": sum(len(items) for items in pack.findings_by_section.values()),
            "evidence_count": len(pack.evidence_ids),
            "gap_count": len(pack.evidence_gaps),
            "evidence_pack_missing_count": evidence_pack_missing,
            "frozen_evidence_missing_count": frozen_evidence_missing,
            "frozen_pit_mismatch_count": frozen_pit_mismatch,
            "missing_evidence_count": missing_evidence,
            "out_of_scope_evidence_count": out_of_scope_evidence,
            "future_evidence_count": future_evidence,
            "critical_grade_mismatch_count": critical_grade_mismatch,
            "metadata_count_mismatch_count": metadata_count_mismatch,
            "artifact_registry_mismatch_count": artifact_registry_mismatch,
            "finding_codes": finding_codes,
        }

    def _claim_bundles(self, request: EvidenceFreezeRequest) -> list[ClaimEvidenceBundle]:
        if request.claim_ids:
            bundles = []
            for claim_id in request.claim_ids:
                bundle = self.evidence_repository.get_claim_bundle(claim_id)
                if bundle is None:
                    raise ValueError(f"unknown claim in evidence freeze: {claim_id}")
                bundles.append(bundle)
        else:
            bundles = self.evidence_repository.claim_bundles_for_subject(
                request.company_id,
                as_of=request.as_of,
            )
        if not bundles:
            raise ValueError(f"no claims available to freeze for {request.company_id}")
        unique = {bundle.claim.claim_id: bundle for bundle in bundles}
        if len(unique) != len(bundles):
            raise ValueError("resolved evidence freeze claims must be unique")
        ordered = [unique[item] for item in sorted(unique)]
        for bundle in ordered:
            if bundle.claim.subject_id != request.company_id:
                raise ValueError(
                    f"claim belongs to a different company: {bundle.claim.claim_id}"
                )
            if bundle.claim.as_of > request.as_of:
                raise ValueError(f"future claim cannot be frozen: {bundle.claim.claim_id}")
            if bundle.claim.status is ClaimStatus.REJECTED:
                raise ValueError(f"rejected claim cannot be frozen: {bundle.claim.claim_id}")
            if not bundle.links:
                raise ValueError(f"claim has no evidence links: {bundle.claim.claim_id}")
        return ordered

    def _evidence_for_bundles(
        self,
        bundles: list[ClaimEvidenceBundle],
        request: EvidenceFreezeRequest,
    ) -> dict[str, Evidence]:
        evidence_by_id: dict[str, Evidence] = {}
        for bundle in bundles:
            for link in bundle.links:
                evidence = self.evidence_repository.get_evidence(link.evidence_id)
                if evidence is None:
                    raise ValueError(f"claim references unknown evidence: {link.evidence_id}")
                if evidence.available_to_system_at > request.as_of:
                    raise ValueError(f"future evidence cannot be frozen: {evidence.evidence_id}")
                if evidence.valid_from is not None and evidence.valid_from > request.as_of:
                    raise ValueError(
                        "not-yet-valid evidence cannot be frozen: "
                        f"{evidence.evidence_id}"
                    )
                if evidence.valid_to is not None and evidence.valid_to < request.as_of:
                    raise ValueError(f"expired evidence cannot be frozen: {evidence.evidence_id}")
                evidence_by_id[evidence.evidence_id] = evidence
        return dict(sorted(evidence_by_id.items()))


__all__ = [
    "BaseCaseExecution",
    "EvidenceFreezeExecution",
    "ResearchCoreService",
]
