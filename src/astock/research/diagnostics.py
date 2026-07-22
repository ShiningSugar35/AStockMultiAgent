"""Deterministic specialist diagnostics and citation-preserving memo composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.research.repository import ResearchRepository
from astock.research.skills import ResearchSkillService
from astock.schemas import (
    BASE_CASE_SECTIONS,
    AdjustmentDirection,
    BaseCaseSection,
    DailyTrendDiagnosticRequest,
    DailyTrendDiagnosticRequestV2,
    DataQualityReport,
    DiagnosticStatus,
    EventToAlphaDiagnosticRequest,
    EventToAlphaDiagnosticRequestV2,
    GrowthHypothesisId,
    GrowthPosteriorStepV2,
    GrowthProbabilityContractV2,
    GrowthProbabilityDiagnosticRequest,
    GrowthProbabilityDiagnosticRequestV2,
    GrowthValuationDiagnosticRequest,
    GrowthValuationDiagnosticRequestV2,
    HourlySwingDiagnosticRequest,
    IndustryBottleneckDiagnosticRequest,
    IndustryBottleneckDiagnosticRequestV2,
    QualityStatus,
    ResearchDiagnosticConfig,
    ResearchFindingInput,
    ResearchFindingType,
    ResearchMemoArtifact,
    ResearchMemoComposeRequest,
    ResearchMemoComposeRequestV2,
    ResearchMemoDeltaReference,
    ResearchMemoSectionReference,
    ResearchSkillRegistry,
    SpecialistAdjustmentInput,
    SpecialistCoverageStatus,
    SpecialistDelta,
    SpecialistDeltaBuildRequest,
    SpecialistDiagnosticReport,
    SpecialistDiagnosticRequest,
    SpecialistDiagnosticRequestV2,
    SpecialistEvidenceRequest,
    SpecialistMetricInput,
)


@dataclass(frozen=True, slots=True)
class DiagnosticExecution:
    report: SpecialistDiagnosticReport
    delta: SpecialistDelta
    object_sha256: str
    delta_object_sha256: str


@dataclass(frozen=True, slots=True)
class ResearchMemoExecution:
    memo: ResearchMemoArtifact
    object_sha256: str


@dataclass(frozen=True, slots=True)
class _DiagnosticOutcome:
    status: DiagnosticStatus
    signal_codes: list[str]
    degradation_codes: list[str]
    delta_request: SpecialistDeltaBuildRequest


class ResearchDiagnosticsService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        registry: ResearchSkillRegistry,
        config: ResearchDiagnosticConfig,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.registry = registry
        self.config = config
        self.repository = ResearchRepository(state, object_store)
        self.evidence_repository = EvidenceRepository(state)
        self.skill_service = ResearchSkillService(state, object_store, registry)

    def diagnose(
        self,
        request: SpecialistDiagnosticRequest | SpecialistDiagnosticRequestV2,
    ) -> DiagnosticExecution:
        outcome = self._run_diagnostic(request)
        delta_execution = self.skill_service.build_delta(outcome.delta_request)
        delta = delta_execution.delta
        input_hash = content_hash(request)
        config_hash = content_hash(self.config)
        diagnostic_identity = {
            "input_hash": input_hash,
            "delta_id": delta.delta_id,
            "diagnostics_version": self.config.diagnostics_version,
            "config_hash": config_hash,
        }
        diagnostic_id = f"specialist-diagnostic:{content_hash(diagnostic_identity)}"
        existing = self.repository.get_diagnostic_report(diagnostic_id)
        if existing is not None:
            object_hash = self.repository.diagnostic_report_object_hash(diagnostic_id)
            assert object_hash is not None
            return DiagnosticExecution(
                report=existing,
                delta=delta,
                object_sha256=object_hash,
                delta_object_sha256=delta_execution.object_sha256,
            )

        report = SpecialistDiagnosticReport(
            diagnostic_id=diagnostic_id,
            base_case_id=delta.base_case_id,
            route_plan_id=delta.route_plan_id,
            delta_id=delta.delta_id,
            skill_id=delta.skill_id,
            skill_version=delta.skill_version,
            diagnostics_version=self.config.diagnostics_version,
            status=outcome.status,
            signal_codes=outcome.signal_codes,
            degradation_codes=outcome.degradation_codes,
            metric_names=[item.metric_name for item in delta.industry_specific_metrics],
            evidence_request_codes=[
                item.request_code for item in delta.additional_evidence_requests
            ],
            evidence_ids=delta.evidence_ids,
            input_sha256=input_hash,
            config_sha256=config_hash,
            method_contract_sha256=(
                content_hash(delta.method_contract)
                if delta.method_contract is not None
                else None
            ),
            created_at=delta.created_at,
        )
        object_ref = self.object_store.put_json(report.model_dump(mode="json"))
        stored = self.repository.register_diagnostic_report(
            report,
            object_hash=object_ref.sha256,
        )
        stored_hash = self.repository.diagnostic_report_object_hash(stored.diagnostic_id)
        assert stored_hash is not None
        self.state.register_artifact(
            artifact_id=f"SpecialistDiagnosticReport:{stored.diagnostic_id}",
            artifact_type="SpecialistDiagnosticReport",
            schema_version=stored.schema_version,
            object_hash=stored_hash,
            input_hashes=[delta_execution.object_sha256, input_hash, config_hash],
        )
        self.state.set_checkpoint(
            scope_type="research-specialist-diagnostic",
            scope_key=stored.diagnostic_id,
            cursor={
                "status": stored.status.value,
                "signal_count": len(stored.signal_codes),
                "evidence_count": len(stored.evidence_ids),
            },
            status="SUCCEEDED",
            object_hash=stored_hash,
        )
        return DiagnosticExecution(
            report=stored,
            delta=delta,
            object_sha256=stored_hash,
            delta_object_sha256=delta_execution.object_sha256,
        )

    def compose_memo(
        self,
        request: ResearchMemoComposeRequest | ResearchMemoComposeRequestV2,
    ) -> ResearchMemoExecution:
        base_case = self.repository.get_base_case(request.base_case_id)
        if base_case is None:
            raise ValueError(f"unknown BaseCase for research memo: {request.base_case_id}")
        route = self.repository.get_route_plan(request.route_plan_id)
        if route is None:
            raise ValueError(f"unknown route plan for research memo: {request.route_plan_id}")
        if route.base_case_id != base_case.base_case_id:
            raise ValueError("research memo route plan must reference the requested BaseCase")

        selected_pairs = {
            (item.skill_id, item.skill_version) for item in route.selected
        }
        deltas: list[SpecialistDelta] = []
        supplied_pairs: set[tuple[str, str]] = set()
        for delta_id in sorted(request.delta_ids):
            delta = self.repository.get_specialist_delta(delta_id)
            if delta is None:
                raise ValueError(f"unknown SpecialistDelta for research memo: {delta_id}")
            if (
                delta.base_case_id != base_case.base_case_id
                or delta.route_plan_id != route.route_plan_id
                or delta.evidence_pack_id != base_case.evidence_pack_id
            ):
                raise ValueError("research memo cannot include a Delta from another frozen scope")
            pair = (delta.skill_id, delta.skill_version)
            if pair not in selected_pairs:
                raise ValueError("research memo Delta was not produced by a selected Skill")
            if pair in supplied_pairs:
                raise ValueError("research memo accepts at most one Delta per selected Skill")
            supplied_pairs.add(pair)
            deltas.append(delta)

        missing_selected = sorted(
            skill_id
            for skill_id, version in selected_pairs
            if (skill_id, version) not in supplied_pairs
        )
        if isinstance(request, ResearchMemoComposeRequestV2):
            hypothesis_refs, posterior_refs = _growth_memo_ref_domains(deltas)
            for scenario in request.structured_memo.scenarios:
                if not set(scenario.growth_hypothesis_refs).issubset(hypothesis_refs):
                    raise ValueError(
                        "memo growth_hypothesis_refs require a hypothesis id from a "
                        "frozen GrowthProbability method contract"
                    )
                if (
                    scenario.probability_ref is not None
                    and scenario.probability_ref not in posterior_refs
                ):
                    raise ValueError(
                        "memo probability_ref requires a posterior id from a frozen "
                        "GrowthProbability method contract"
                    )
            valid_source_refs = {
                finding.finding_id
                for section in BASE_CASE_SECTIONS
                for finding in base_case.findings_by_section[section]
            } | {
                reference
                for delta in deltas
                for reference in (
                    *(item.finding_id for item in delta.incremental_findings),
                    *(item.finding_id for item in delta.base_case_corrections),
                    *(item.metric_id for item in delta.industry_specific_metrics),
                    *(item.adjustment_id for item in delta.valuation_adjustments),
                    *(item.adjustment_id for item in delta.risk_adjustments),
                )
            } | {
                reference
                for delta in deltas
                for reference in _serenity_method_source_refs(delta)
            }
            structured_refs = _structured_memo_refs(request)
            if not structured_refs.issubset(valid_source_refs):
                raise ValueError("structured memo references an id outside frozen inputs")
        base_sections = [
            ResearchMemoSectionReference(
                section=section,
                base_finding_ids=[
                    finding.finding_id for finding in base_case.findings_by_section[section]
                ],
                evidence_ids=sorted(
                    {
                        evidence_id
                        for finding in base_case.findings_by_section[section]
                        for evidence_id in finding.evidence_ids
                    }
                ),
                created_at=base_case.created_at,
            )
            for section in BASE_CASE_SECTIONS
        ]
        delta_references = [
            ResearchMemoDeltaReference(
                delta_id=delta.delta_id,
                skill_id=delta.skill_id,
                skill_version=delta.skill_version,
                incremental_finding_ids=[
                    item.finding_id for item in delta.incremental_findings
                ],
                correction_ids=[item.finding_id for item in delta.base_case_corrections],
                metric_ids=[item.metric_id for item in delta.industry_specific_metrics],
                adjustment_ids=[
                    item.adjustment_id
                    for item in (*delta.valuation_adjustments, *delta.risk_adjustments)
                ],
                evidence_ids=delta.evidence_ids,
                created_at=delta.created_at,
            )
            for delta in sorted(deltas, key=lambda item: (item.skill_id, item.delta_id))
        ]
        evidence_ids = sorted(
            {
                evidence_id
                for item in (*base_sections, *delta_references)
                for evidence_id in item.evidence_ids
            }
        )
        degradation_codes = set(route.degradation_codes)
        if missing_selected:
            degradation_codes.add("MISSING_SELECTED_DELTA")
        if base_case.evidence_gaps:
            degradation_codes.add("BASE_CASE_GAPS_OPEN")
        if isinstance(request, ResearchMemoComposeRequestV2):
            if not request.structured_memo.catalysts:
                degradation_codes.add("CATALYSTS_NOT_EVIDENCED")
            if not request.structured_memo.monitoring_items:
                degradation_codes.add("MONITORING_INCOMPLETE")
        if route.coverage_status is SpecialistCoverageStatus.INSUFFICIENT:
            coverage_status = SpecialistCoverageStatus.INSUFFICIENT
        elif missing_selected or degradation_codes:
            coverage_status = SpecialistCoverageStatus.PARTIAL
        else:
            coverage_status = SpecialistCoverageStatus.SUFFICIENT

        input_hash = (
            content_hash(request)
            if isinstance(request, ResearchMemoComposeRequestV2)
            else content_hash(
                {
                    "base_case_id": base_case.base_case_id,
                    "route_plan_id": route.route_plan_id,
                    "delta_ids": sorted(request.delta_ids),
                }
            )
        )
        memo_identity = {
            "base_case_hash": self.repository.base_case_object_hash(base_case.base_case_id),
            "route_hash": self.repository.route_plan_object_hash(route.route_plan_id),
            "delta_hashes": [
                self.repository.specialist_delta_object_hash(delta.delta_id)
                for delta in sorted(deltas, key=lambda item: item.delta_id)
            ],
            "input_hash": input_hash,
            "composer_version": (
                "research-memo-composer-v2"
                if isinstance(request, ResearchMemoComposeRequestV2)
                else "research-memo-composer-v1"
            ),
        }
        memo_id = f"research-memo:{content_hash(memo_identity)}"
        existing = self.repository.get_research_memo(memo_id)
        if existing is not None:
            object_hash = self.repository.research_memo_object_hash(memo_id)
            assert object_hash is not None
            return ResearchMemoExecution(memo=existing, object_sha256=object_hash)
        memo = ResearchMemoArtifact(
            memo_id=memo_id,
            base_case_id=base_case.base_case_id,
            route_plan_id=route.route_plan_id,
            company_id=base_case.company_id,
            as_of=base_case.as_of,
            registry_version=route.registry_version,
            base_sections=base_sections,
            delta_references=delta_references,
            missing_selected_skill_ids=missing_selected,
            open_gap_codes=sorted(gap.gap_code for gap in base_case.evidence_gaps),
            coverage_status=coverage_status,
            confidence_cap=route.confidence_cap,
            degradation_codes=sorted(degradation_codes),
            evidence_ids=evidence_ids,
            composer_version=(
                "research-memo-composer-v2"
                if isinstance(request, ResearchMemoComposeRequestV2)
                else None
            ),
            structured_memo=(
                request.structured_memo
                if isinstance(request, ResearchMemoComposeRequestV2)
                else None
            ),
            created_at=route.created_at,
        )
        object_ref = self.object_store.put_json(memo.model_dump(mode="json"))
        stored = self.repository.register_research_memo(
            memo,
            object_hash=object_ref.sha256,
            input_hash=input_hash,
        )
        stored_hash = self.repository.research_memo_object_hash(stored.memo_id)
        assert stored_hash is not None
        input_hashes = [
            value
            for value in (
                self.repository.base_case_object_hash(base_case.base_case_id),
                self.repository.route_plan_object_hash(route.route_plan_id),
                *(
                    self.repository.specialist_delta_object_hash(delta.delta_id)
                    for delta in deltas
                ),
                input_hash,
            )
            if value is not None
        ]
        self.state.register_artifact(
            artifact_id=f"ResearchMemoArtifact:{stored.memo_id}",
            artifact_type="ResearchMemoArtifact",
            schema_version=stored.schema_version,
            object_hash=stored_hash,
            input_hashes=input_hashes,
        )
        self.state.set_checkpoint(
            scope_type="research-memo",
            scope_key=stored.memo_id,
            cursor={
                "delta_count": len(stored.delta_references),
                "missing_selected_count": len(stored.missing_selected_skill_ids),
                "evidence_count": len(stored.evidence_ids),
            },
            status="SUCCEEDED",
            object_hash=stored_hash,
        )
        return ResearchMemoExecution(memo=stored, object_sha256=stored_hash)

    def status(self, base_case_id: str) -> dict[str, object]:
        diagnostics = self.repository.diagnostic_report_summaries(base_case_id)
        memo = self.repository.latest_research_memo_summary(base_case_id)
        if not diagnostics and memo is None:
            return {"status": "NOT_RUN", "base_case_id": base_case_id}
        return {
            "status": "AVAILABLE",
            "base_case_id": base_case_id,
            "diagnostic_count": len(diagnostics),
            "diagnostics": diagnostics,
            "memo": memo,
        }

    def audit(self, base_case_id: str) -> dict[str, object]:
        base_case = self.repository.get_base_case(base_case_id)
        diagnostics = self.repository.diagnostic_report_summaries(base_case_id)
        memo_summary = self.repository.latest_research_memo_summary(base_case_id)
        if base_case is None and not diagnostics and memo_summary is None:
            return {"status": "NOT_RUN", "base_case_id": base_case_id}
        evidence_pack = (
            self.repository.get_evidence_pack(base_case.evidence_pack_id)
            if base_case is not None
            else None
        )
        evidence_scope = set(evidence_pack.evidence_ids) if evidence_pack else set()
        report_missing = 0
        report_metadata_mismatch = 0
        report_delta_mismatch = 0
        report_artifact_mismatch = 0
        diagnostic_config_mismatch = 0
        evidence_outside_scope = 0
        evidence_missing = 0
        future_evidence = 0
        for summary in diagnostics:
            diagnostic_id = str(summary["diagnostic_id"])
            report = self.repository.get_diagnostic_report(diagnostic_id)
            if report is None:
                report_missing += 1
                continue
            report_metadata_mismatch += int(
                int(str(summary["signal_count"])) != len(report.signal_codes)
                or int(str(summary["degradation_count"]))
                != len(report.degradation_codes)
                or int(str(summary["metric_count"])) != len(report.metric_names)
                or int(str(summary["evidence_request_count"]))
                != len(report.evidence_request_codes)
                or int(str(summary["evidence_count"])) != len(report.evidence_ids)
            )
            delta = self.repository.get_specialist_delta(report.delta_id)
            report_delta_mismatch += int(
                delta is None
                or delta.base_case_id != report.base_case_id
                or delta.route_plan_id != report.route_plan_id
                or delta.skill_id != report.skill_id
                or delta.skill_version != report.skill_version
                or delta.evidence_ids != report.evidence_ids
                or report.method_contract_sha256
                != (
                    content_hash(delta.method_contract)
                    if delta is not None and delta.method_contract is not None
                    else None
                )
            )
            report_artifact_mismatch += self._artifact_mismatch(
                f"SpecialistDiagnosticReport:{report.diagnostic_id}",
                str(summary["object_hash"]),
            )
            diagnostic_config_mismatch += int(
                report.diagnostics_version == self.config.diagnostics_version
                and report.config_sha256 != content_hash(self.config)
            )
            for evidence_id in report.evidence_ids:
                evidence_outside_scope += int(evidence_id not in evidence_scope)
                evidence = self.evidence_repository.get_evidence(evidence_id)
                evidence_missing += int(evidence is None)
                future_evidence += int(
                    evidence is not None
                    and base_case is not None
                    and evidence.available_to_system_at > base_case.as_of
                )

        memo_missing = 0
        memo_metadata_mismatch = 0
        memo_reference_mismatch = 0
        memo_artifact_mismatch = 0
        if memo_summary is not None:
            memo = self.repository.get_research_memo(str(memo_summary["memo_id"]))
            if memo is None:
                memo_missing = 1
            else:
                memo_metadata_mismatch = int(
                    int(str(memo_summary["delta_count"]))
                    != len(memo.delta_references)
                    or int(str(memo_summary["missing_selected_count"]))
                    != len(memo.missing_selected_skill_ids)
                    or int(str(memo_summary["gap_count"])) != len(memo.open_gap_codes)
                    or int(str(memo_summary["degradation_count"]))
                    != len(memo.degradation_codes)
                    or int(str(memo_summary["evidence_count"])) != len(memo.evidence_ids)
                )
                route = self.repository.get_route_plan(memo.route_plan_id)
                memo_reference_mismatch = int(
                    base_case is None
                    or route is None
                    or memo.base_case_id != base_case.base_case_id
                    or route.base_case_id != base_case.base_case_id
                    or any(
                        self.repository.get_specialist_delta(item.delta_id) is None
                        for item in memo.delta_references
                    )
                    or any(item not in evidence_scope for item in memo.evidence_ids)
                )
                memo_artifact_mismatch = self._artifact_mismatch(
                    f"ResearchMemoArtifact:{memo.memo_id}",
                    str(memo_summary["object_hash"]),
                )
        findings = {
            "BASE_CASE_MISSING": int(base_case is None),
            "EVIDENCE_PACK_MISSING": int(evidence_pack is None),
            "DIAGNOSTIC_REPORT_MISSING": report_missing,
            "DIAGNOSTIC_METADATA_MISMATCH": report_metadata_mismatch,
            "DIAGNOSTIC_DELTA_MISMATCH": report_delta_mismatch,
            "DIAGNOSTIC_ARTIFACT_MISMATCH": report_artifact_mismatch,
            "DIAGNOSTIC_CONFIG_MISMATCH": diagnostic_config_mismatch,
            "EVIDENCE_OUTSIDE_FROZEN_SCOPE": evidence_outside_scope,
            "EVIDENCE_RECORD_MISSING": evidence_missing,
            "FUTURE_EVIDENCE": future_evidence,
            "MEMO_MISSING": memo_missing,
            "MEMO_METADATA_MISMATCH": memo_metadata_mismatch,
            "MEMO_REFERENCE_MISMATCH": memo_reference_mismatch,
            "MEMO_ARTIFACT_MISMATCH": memo_artifact_mismatch,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "base_case_id": base_case_id,
            "diagnostic_count": len(diagnostics),
            "memo_count": int(memo_summary is not None),
            "finding_codes": finding_codes,
            "finding_counts": findings,
        }

    def _run_diagnostic(
        self,
        request: SpecialistDiagnosticRequest | SpecialistDiagnosticRequestV2,
    ) -> _DiagnosticOutcome:
        if isinstance(request, IndustryBottleneckDiagnosticRequestV2):
            return self._industry_v2(request)
        if isinstance(request, EventToAlphaDiagnosticRequestV2):
            return self._event_v2(request)
        if isinstance(request, GrowthProbabilityDiagnosticRequestV2):
            return self._growth_probability_v2(request)
        if isinstance(request, GrowthValuationDiagnosticRequestV2):
            return self._growth_valuation_v2(request)
        if isinstance(request, DailyTrendDiagnosticRequestV2):
            return self._daily_trend_v2(request)
        if isinstance(request, IndustryBottleneckDiagnosticRequest):
            return self._industry(request)
        if isinstance(request, EventToAlphaDiagnosticRequest):
            return self._event(request)
        if isinstance(request, GrowthProbabilityDiagnosticRequest):
            return self._growth_probability(request)
        if isinstance(request, GrowthValuationDiagnosticRequest):
            return self._growth_valuation(request)
        if isinstance(request, DailyTrendDiagnosticRequest):
            return self._daily_trend(request)
        if isinstance(request, HourlySwingDiagnosticRequest):
            return self._hourly_swing(request)
        raise TypeError(f"unsupported specialist diagnostic request: {type(request)!r}")

    def _industry_v2(
        self,
        request: IndustryBottleneckDiagnosticRequestV2,
    ) -> _DiagnosticOutcome:
        contract = request.method_contract
        self._validate_v2_scope(request.base_case_id, contract.target_company_id, contract.as_of)
        broken = (
            contract.aggregate_substitutability_ratio
            > self.config.industry.max_substitutability_ratio
        )
        evidence_ids = contract.evidence_ids
        finding = (
            []
            if broken
            else [
                ResearchFindingInput(
                    statement=(
                        "The frozen supply-chain hierarchy, necessary link, scarcity, "
                        "substitution and target-company value capture are verified."
                    ),
                    finding_type=ResearchFindingType.ANALYST_INFERENCE,
                    confidence=0.65,
                    critical=True,
                    evidence_ids=evidence_ids,
                )
            ]
        )
        return _DiagnosticOutcome(
            status=DiagnosticStatus.INSUFFICIENT if broken else DiagnosticStatus.PASS,
            signal_codes=["BOTTLENECK_CHAIN_BROKEN" if broken else "BOTTLENECK_CHAIN_VERIFIED"],
            degradation_codes=["SUBSTITUTION_UNRESOLVED"] if broken else [],
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=finding,
                base_case_corrections=[],
                industry_specific_metrics=[
                    SpecialistMetricInput(
                        metric_name="aggregate_substitutability_ratio",
                        value=str(contract.aggregate_substitutability_ratio),
                        unit="ratio",
                        evidence_ids=evidence_ids,
                    ),
                    SpecialistMetricInput(
                        metric_name="supply_chain_layer_count",
                        value=float(len(contract.chain_nodes)),
                        unit="count",
                        evidence_ids=evidence_ids,
                    ),
                ],
                additional_evidence_requests=[],
                failure_modes=["BOTTLENECK_CHAIN_BROKEN"] if broken else [],
                confidence_delta=0,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={BaseCaseSection.INDUSTRY_SUPPLY_DEMAND: 0 if broken else 0.1},
                method_contract=contract,
            ),
        )

    def _event_v2(
        self,
        request: EventToAlphaDiagnosticRequestV2,
    ) -> _DiagnosticOutcome:
        contract = request.method_contract
        self._validate_v2_scope(request.base_case_id, contract.target_company_id, contract.as_of)
        hard_failure = request.headline_only or contract.business_purity.value == 0
        degradation: list[str] = []
        if request.headline_only:
            degradation.append("HEADLINE_ONLY")
        if contract.business_purity.value == 0:
            degradation.append("BUSINESS_PURITY_UNVERIFIED")
        if contract.scale_elasticity.value is None:
            degradation.append("SCALE_ELASTICITY_UNQUANTIFIED")
        if contract.market_misclassification is None:
            degradation.append("MARKET_MISCLASSIFICATION_UNVERIFIED")
        status = (
            DiagnosticStatus.INSUFFICIENT
            if hard_failure
            else DiagnosticStatus.PARTIAL
            if degradation
            else DiagnosticStatus.PASS
        )
        finding = (
            []
            if hard_failure
            else [
                ResearchFindingInput(
                    statement=(
                        "The frozen event maps demand through a continuous operating-to-financial "
                        "chain with one-to-four-quarter checkpoints and an observable falsifier."
                    ),
                    finding_type=ResearchFindingType.ANALYST_INFERENCE,
                    confidence=0.6 if not degradation else 0.5,
                    critical=True,
                    evidence_ids=contract.evidence_ids,
                )
            ]
        )
        return _DiagnosticOutcome(
            status=status,
            signal_codes=["EVENT_CHAIN_REJECTED" if hard_failure else "EVENT_CHAIN_VERIFIED"],
            degradation_codes=degradation,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=finding,
                base_case_corrections=[],
                industry_specific_metrics=[
                    SpecialistMetricInput(
                        metric_name="business_purity_ratio",
                        value=str(contract.business_purity.value),
                        unit="ratio",
                        evidence_ids=contract.business_purity.evidence_ids,
                    ),
                    SpecialistMetricInput(
                        metric_name="validation_checkpoint_count",
                        value=float(len(contract.validation_checkpoints)),
                        unit="count",
                        evidence_ids=contract.evidence_ids,
                    ),
                ],
                additional_evidence_requests=[],
                failure_modes=degradation,
                confidence_delta=0,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={BaseCaseSection.REVENUE_DRIVERS: 0 if hard_failure else 0.05},
                method_contract=contract,
            ),
        )

    def _growth_probability_v2(
        self,
        request: GrowthProbabilityDiagnosticRequestV2,
    ) -> _DiagnosticOutcome:
        method_input = request.method_input
        self._validate_v2_scope(
            request.base_case_id,
            method_input.target_company_id,
            method_input.as_of,
        )
        prior = dict(method_input.prior_by_hypothesis)
        trajectory: list[GrowthPosteriorStepV2] = []
        for update in method_input.likelihood_updates:
            posterior = _bayesian_update(prior, update.likelihood_by_hypothesis)
            trajectory.append(
                GrowthPosteriorStepV2(
                    sequence=update.sequence,
                    update_id=update.update_id,
                    prior=prior,
                    likelihood=update.likelihood_by_hypothesis,
                    posterior=posterior,
                    created_at=method_input.created_at,
                )
            )
            prior = posterior
        contract = GrowthProbabilityContractV2(
            input=method_input,
            update_trajectory=trajectory,
            final_posterior=prior,
            evidence_ids=method_input.evidence_ids,
            created_at=method_input.created_at,
        )
        by_id = {item.hypothesis_id: item for item in method_input.hypotheses}
        expected_growth = sum(
            (
                prior[hypothesis_id]
                * (by_id[hypothesis_id].growth_lower + by_id[hypothesis_id].growth_upper)
                / Decimal("2")
                for hypothesis_id in GrowthHypothesisId
            ),
            Decimal("0"),
        )
        degradation = [] if method_input.consensus is not None else ["CONSENSUS_UNAVAILABLE"]
        return _DiagnosticOutcome(
            status=DiagnosticStatus.PASS if not degradation else DiagnosticStatus.PARTIAL,
            signal_codes=["GROWTH_POSTERIOR_CONSERVED"],
            degradation_codes=degradation,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=[
                    ResearchFindingInput(
                        statement=(
                            "H0-H5 posterior probabilities were deterministically updated from "
                            "frozen priors and independent likelihoods; midpoint "
                            f"expectation={expected_growth}."
                        ),
                        finding_type=ResearchFindingType.ANALYST_INFERENCE,
                        confidence=0.55,
                        critical=False,
                        evidence_ids=contract.evidence_ids,
                    )
                ],
                base_case_corrections=[],
                industry_specific_metrics=[
                    SpecialistMetricInput(
                        metric_name=f"posterior_{hypothesis_id.value}",
                        value=str(prior[hypothesis_id]),
                        unit="probability",
                        evidence_ids=contract.evidence_ids,
                    )
                    for hypothesis_id in GrowthHypothesisId
                ],
                additional_evidence_requests=[],
                failure_modes=["FALSE_PRECISION", *degradation],
                confidence_delta=0,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={BaseCaseSection.REVENUE_DRIVERS: 0.05},
                method_contract=contract,
            ),
        )

    def _growth_valuation_v2(
        self,
        request: GrowthValuationDiagnosticRequestV2,
    ) -> _DiagnosticOutcome:
        contract = request.method_contract
        self._validate_v2_scope(request.base_case_id, contract.target_company_id, contract.as_of)
        expected_factors = {
            "durability",
            "cash_conversion",
            "concentration",
            "capital_intensity",
            "dilution",
        }
        degradation: list[str] = []
        not_applicable = contract.applicability.value == "NOT_APPLICABLE"
        metrics: list[SpecialistMetricInput] = []
        if not_applicable:
            degradation.append("VALUATION_NOT_APPLICABLE")
        else:
            if contract.tam_runway is None:
                degradation.append("TAM_UNAVAILABLE")
            elif contract.tam_runway.current_revenue == 0:
                degradation.append("TAM_DENOMINATOR_ZERO")
            if {item.factor_id for item in contract.quality_factors} != expected_factors:
                degradation.append("QUALITY_FACTORS_INCOMPLETE")
            assert contract.peg is not None
            peg_applicable = contract.peg.pe_multiple > 0 and contract.peg.growth_value > 0
            if not peg_applicable:
                degradation.append("PEG_NOT_APPLICABLE")
            metrics.append(
                SpecialistMetricInput(
                    metric_name="peg",
                    value=(
                        str(contract.peg.pe_multiple / contract.peg.growth_value)
                        if peg_applicable
                        else "NOT_APPLICABLE"
                    ),
                    unit=contract.peg.peg_unit,
                    evidence_ids=contract.peg.evidence_ids,
                )
            )
        if (
            not not_applicable
            and contract.tam_runway is not None
            and contract.tam_runway.current_revenue > 0
        ):
            metrics.append(
                SpecialistMetricInput(
                    metric_name="tam_revenue_multiple",
                    value=str(
                        contract.tam_runway.tam_value
                        * contract.tam_runway.addressable_share
                        / contract.tam_runway.current_revenue
                    ),
                    unit="revenue_multiple",
                    evidence_ids=contract.tam_runway.evidence_ids,
                )
            )
        if not not_applicable and contract.consensus is None:
            degradation.append("CONSENSUS_UNAVAILABLE")
        return _DiagnosticOutcome(
            status=DiagnosticStatus.PASS if not degradation else DiagnosticStatus.PARTIAL,
            signal_codes=["REPORT_ONLY_UNCALIBRATED"],
            degradation_codes=degradation,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=[
                    ResearchFindingInput(
                        statement=(
                            "The frozen valuation applicability gate and any applicable TAM, "
                            "quality and unit-safe PEG components are report-only and do not "
                            "produce a target price or valuation adjustment."
                        ),
                        finding_type=ResearchFindingType.ANALYST_INFERENCE,
                        confidence=0.5,
                        critical=False,
                        evidence_ids=contract.evidence_ids,
                    )
                ],
                base_case_corrections=[],
                industry_specific_metrics=metrics,
                additional_evidence_requests=[],
                failure_modes=["REPORT_ONLY_UNCALIBRATED", *degradation],
                confidence_delta=0,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={
                    BaseCaseSection.VALUATION_EXPECTATIONS: 0 if not_applicable else 0.05
                },
                method_contract=contract,
            ),
        )

    def _daily_trend_v2(
        self,
        request: DailyTrendDiagnosticRequestV2,
    ) -> _DiagnosticOutcome:
        contract = request.method_contract
        self._validate_v2_scope(request.base_case_id, contract.target_company_id, contract.as_of)
        report, report_hash = self._load_quality_report(
            contract.daily_series.quality_report_id
        )
        if (
            report.symbol != contract.daily_series.symbol
            or report.frequency is not contract.daily_series.frequency
            or report.adjustment_mode is not contract.daily_series.adjustment_mode
            or report.bar_count != contract.daily_series.bar_count
            or report_hash != contract.daily_series.dataset_version
        ):
            raise ValueError("daily method series does not match its DataQualityReport")
        if report.actual_end is None or report.actual_end > contract.as_of:
            raise ValueError("daily quality report must end no later than the BaseCase as_of")
        degradation = ["DMA_CALLER_SUPPLIED_UNVERIFIED"]
        hard_failure = (
            report.quality_status is not QualityStatus.PASS
            or report.bar_count < self.config.daily.minimum_bars
        )
        if report.quality_status is not QualityStatus.PASS:
            degradation.append("DAILY_QUALITY_FAILED")
        if report.bar_count < self.config.daily.minimum_bars:
            degradation.append("INSUFFICIENT_DAILY_BARS")
        windows = {item.window for item in contract.moving_averages}
        for window in (50, 100, 200):
            if window not in windows:
                degradation.append(f"DMA{window}_UNAVAILABLE")
        fundamentals = {item.metric for item in contract.fundamental_growth}
        if fundamentals != {"REVENUE", "EARNINGS"}:
            degradation.append("FUNDAMENTALS_UNAVAILABLE")
        if not contract.estimate_revisions:
            degradation.append("REVISIONS_UNAVAILABLE")
        status = DiagnosticStatus.INSUFFICIENT if hard_failure else DiagnosticStatus.PARTIAL
        all_price_positive = all(item.close > item.value for item in contract.moving_averages)
        earnings_negative = any(
            item.metric == "EARNINGS" and item.current < item.prior
            for item in contract.fundamental_growth
        )
        revisions_negative = any(
            item.current_estimate < item.prior_estimate for item in contract.estimate_revisions
        )
        component_signal = (
            "GF_DMA_MIXED"
            if all_price_positive and (earnings_negative or revisions_negative)
            else "GF_DMA_COMPONENTS_REPORTED"
        )
        return _DiagnosticOutcome(
            status=status,
            signal_codes=[
                component_signal,
                "DMA_CALLER_SUPPLIED_UNVERIFIED",
                "GF_DMA_REPORT_ONLY_UNCALIBRATED",
            ],
            degradation_codes=degradation,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=(
                    []
                    if hard_failure
                    else [
                        ResearchFindingInput(
                            statement=(
                                "Caller-supplied, dataset-bound daily moving averages, "
                                "fundamentals and estimate revisions are reported as "
                                f"{component_signal}; they were not recomputed from bars here "
                                "and this is not a buy or sell signal."
                            ),
                            finding_type=ResearchFindingType.ANALYST_INFERENCE,
                            confidence=0.5,
                            critical=False,
                            evidence_ids=contract.evidence_ids,
                        )
                    ]
                ),
                base_case_corrections=[],
                industry_specific_metrics=[
                    SpecialistMetricInput(
                        metric_name=f"dma_{item.window}",
                        value=str(item.value),
                        unit=item.currency,
                        evidence_ids=item.evidence_ids,
                    )
                    for item in contract.moving_averages
                ],
                additional_evidence_requests=[],
                failure_modes=["REPORT_ONLY_UNCALIBRATED", *degradation],
                confidence_delta=0,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={BaseCaseSection.PRICE_TREND_CONTEXT: 0},
                method_contract=contract,
            ),
        )

    def _validate_v2_scope(
        self,
        base_case_id: str,
        target_company_id: str,
        as_of: datetime,
    ) -> None:
        base_case = self.repository.get_base_case(base_case_id)
        if base_case is None:
            raise ValueError(f"unknown BaseCase for v2 diagnostic: {base_case_id}")
        if target_company_id != base_case.company_id or as_of != base_case.as_of:
            raise ValueError("v2 method contract company/as_of must match the frozen BaseCase")

    def _load_quality_report(self, report_id: str) -> tuple[DataQualityReport, str]:
        artifact_id = f"DataQualityReport:{report_id}"
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=? AND type=?",
                (artifact_id, "DataQualityReport"),
            ).fetchone()
        if row is None:
            raise ValueError("daily v2 requires a registered DataQualityReport artifact")
        object_hash = str(row["object_hash"])
        return (
            DataQualityReport.model_validate_json(self.object_store.get_bytes(object_hash)),
            object_hash,
        )

    def _industry(
        self,
        request: IndustryBottleneckDiagnosticRequest,
    ) -> _DiagnosticOutcome:
        failed_layers: list[tuple[str, bool]] = [
            ("SYSTEM_CHANGE", not request.system_change_verified),
            ("NECESSARY_LINK", not request.necessary_link_verified),
            ("SCARCITY", not request.scarcity_verified),
            (
                "SUBSTITUTABILITY",
                request.substitutability_ratio
                > self.config.industry.max_substitutability_ratio,
            ),
            ("VALUE_CAPTURE", not request.value_capture_verified),
        ]
        missing = [name for name, failed in failed_layers if failed]
        complete = not missing
        all_evidence = _ordered_union(
            request.system_change_evidence_ids,
            request.necessary_link_evidence_ids,
            request.scarcity_evidence_ids,
            request.substitutability_evidence_ids,
            request.value_capture_evidence_ids,
        )
        findings = (
            [
                ResearchFindingInput(
                    statement=(
                        "The system-change to necessary-link, scarcity, substitutability, "
                        "and listed-company value-capture chain is verified."
                    ),
                    finding_type=ResearchFindingType.ANALYST_INFERENCE,
                    confidence=0.7,
                    critical=True,
                    evidence_ids=all_evidence,
                )
            ]
            if complete
            else []
        )
        evidence_requests = [
            SpecialistEvidenceRequest(
                request_code=f"VERIFY_{layer}",
                reason=f"The {layer.lower()} layer is not verified.",
                required_evidence=[f"PRIMARY_OFFICIAL_{layer}_EVIDENCE"],
                blocking=True,
            )
            for layer in missing
        ]
        delta_request = SpecialistDeltaBuildRequest(
            base_case_id=request.base_case_id,
            route_plan_id=request.route_plan_id,
            skill_id=request.skill_id,
            skill_version=request.skill_version,
            incremental_findings=findings,
            base_case_corrections=[],
            industry_specific_metrics=[
                SpecialistMetricInput(
                    metric_name="substitutability_ratio",
                    value=str(request.substitutability_ratio),
                    unit="ratio",
                    evidence_ids=request.substitutability_evidence_ids,
                )
            ],
            additional_evidence_requests=evidence_requests,
            failure_modes=[] if complete else ["BOTTLENECK_CHAIN_BROKEN"],
            confidence_delta=0.05 if complete else -0.1,
            valuation_adjustments=[],
            risk_adjustments=[],
            coverage_delta={
                BaseCaseSection.INDUSTRY_SUPPLY_DEMAND: 0.1 if complete else -0.1,
                BaseCaseSection.COMPETITIVE_POSITION: 0.05 if complete else -0.05,
            },
        )
        return _DiagnosticOutcome(
            status=DiagnosticStatus.PASS if complete else DiagnosticStatus.INSUFFICIENT,
            signal_codes=[
                "BOTTLENECK_CHAIN_VERIFIED"
                if complete
                else "BOTTLENECK_CHAIN_INCOMPLETE"
            ],
            degradation_codes=[] if complete else ["INSUFFICIENT_EVIDENCE"],
            delta_request=delta_request,
        )

    def _event(self, request: EventToAlphaDiagnosticRequest) -> _DiagnosticOutcome:
        missing: list[tuple[str, str]] = []
        if not request.event_verified:
            missing.append(("VERIFY_EVENT", "PRIMARY_OFFICIAL_EVENT_EVIDENCE"))
        if request.headline_only:
            missing.append(("REPLACE_HEADLINE_ONLY_INPUT", "FULL_EVENT_DISCLOSURE"))
        if not all(
            (
                request.operating_metric,
                request.operating_direction,
                request.financial_metric,
                request.financial_direction,
                request.window_start,
                request.window_end,
            )
        ) or not request.transmission_evidence_ids:
            missing.append(("VERIFY_EVENT_TRANSMISSION", "OPERATING_AND_FINANCIAL_EVIDENCE"))
        if not request.falsifier or not request.falsifier_evidence_ids:
            missing.append(("DEFINE_EVENT_FALSIFIER", "FALSIFIER_EVIDENCE"))
        complete = not missing
        evidence_ids = _ordered_union(
            request.event_evidence_ids,
            request.transmission_evidence_ids,
            request.falsifier_evidence_ids,
        )
        findings = []
        if complete:
            assert request.window_start is not None and request.window_end is not None
            findings.append(
                ResearchFindingInput(
                    statement=(
                        f"The verified event maps to {request.operating_metric} "
                        f"({request.operating_direction}) and {request.financial_metric} "
                        f"({request.financial_direction}) during "
                        f"{request.window_start.isoformat()}..{request.window_end.isoformat()}, "
                        f"subject to falsifier: {request.falsifier}."
                    ),
                    finding_type=ResearchFindingType.ANALYST_INFERENCE,
                    confidence=0.65,
                    critical=True,
                    evidence_ids=evidence_ids,
                )
            )
        delta_request = SpecialistDeltaBuildRequest(
            base_case_id=request.base_case_id,
            route_plan_id=request.route_plan_id,
            skill_id=request.skill_id,
            skill_version=request.skill_version,
            incremental_findings=findings,
            base_case_corrections=[],
            industry_specific_metrics=[],
            additional_evidence_requests=[
                SpecialistEvidenceRequest(
                    request_code=code,
                    reason="The event-to-operating-to-financial chain is incomplete.",
                    required_evidence=[required],
                    blocking=True,
                )
                for code, required in _unique_pairs(missing)
            ],
            failure_modes=[] if complete else ["EVENT_TRANSMISSION_INCOMPLETE"],
            confidence_delta=0.03 if complete else -0.1,
            valuation_adjustments=[],
            risk_adjustments=[],
            coverage_delta={
                BaseCaseSection.REVENUE_DRIVERS: 0.05 if complete else -0.05,
                BaseCaseSection.PROFIT_DRIVERS: 0.05 if complete else -0.05,
            },
        )
        degradation = []
        if not complete:
            degradation.append("EVENT_TRANSMISSION_INCOMPLETE")
        if request.headline_only:
            degradation.append("HEADLINE_ONLY")
        return _DiagnosticOutcome(
            status=DiagnosticStatus.PASS if complete else DiagnosticStatus.INSUFFICIENT,
            signal_codes=["EVENT_CHAIN_VERIFIED" if complete else "EVENT_CHAIN_REJECTED"],
            degradation_codes=degradation,
            delta_request=delta_request,
        )

    def _growth_probability(
        self,
        request: GrowthProbabilityDiagnosticRequest,
    ) -> _DiagnosticOutcome:
        expected_growth = sum(
            (item.probability * item.annual_growth_rate for item in request.scenarios),
            Decimal("0"),
        )
        expected_duration = sum(
            (
                item.probability * Decimal(item.duration_years)
                for item in request.scenarios
            ),
            Decimal("0"),
        )
        scenario_evidence = _ordered_union(
            *(item.evidence_ids for item in request.scenarios)
        )
        evidence_ids = _ordered_union(
            scenario_evidence,
            request.consensus_evidence_ids,
        )
        metrics = [
            SpecialistMetricInput(
                metric_name="probability_weighted_annual_growth",
                value=str(expected_growth),
                unit="ratio",
                evidence_ids=scenario_evidence,
            ),
            SpecialistMetricInput(
                metric_name="probability_weighted_duration",
                value=str(expected_duration),
                unit="years",
                evidence_ids=scenario_evidence,
            ),
            SpecialistMetricInput(
                metric_name="scenario_count",
                value=float(len(request.scenarios)),
                unit="count",
                evidence_ids=scenario_evidence,
            ),
        ]
        if request.consensus_available:
            assert request.consensus_growth_rate is not None
            metrics.append(
                SpecialistMetricInput(
                    metric_name="consensus_growth_rate",
                    value=str(request.consensus_growth_rate),
                    unit="ratio",
                    evidence_ids=request.consensus_evidence_ids,
                )
            )
        finding = ResearchFindingInput(
            statement=(
                f"Mutually exclusive growth scenarios imply probability-weighted annual "
                f"growth {expected_growth} for {expected_duration} years."
            ),
            finding_type=ResearchFindingType.ANALYST_INFERENCE,
            confidence=0.65 if request.consensus_available else 0.55,
            critical=False,
            evidence_ids=evidence_ids,
        )
        degradation = [] if request.consensus_available else ["CONSENSUS_UNAVAILABLE"]
        return _DiagnosticOutcome(
            status=(
                DiagnosticStatus.PASS
                if request.consensus_available
                else DiagnosticStatus.PARTIAL
            ),
            signal_codes=["GROWTH_PROBABILITY_CONSERVED"],
            degradation_codes=degradation,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=[finding],
                base_case_corrections=[],
                industry_specific_metrics=metrics,
                additional_evidence_requests=(
                    []
                    if request.consensus_available
                    else [
                        SpecialistEvidenceRequest(
                            request_code="OBTAIN_CONSENSUS_ESTIMATES",
                            reason="Consensus is optional but unavailable for cross-checking.",
                            required_evidence=["VERSIONED_CONSENSUS_ESTIMATES"],
                            blocking=False,
                        )
                    ]
                ),
                failure_modes=["FALSE_PRECISION", "BASE_RATE_NEGLECT"],
                confidence_delta=0.02 if request.consensus_available else -0.03,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={
                    BaseCaseSection.REVENUE_DRIVERS: 0.05,
                    BaseCaseSection.PROFIT_DRIVERS: 0.05,
                    BaseCaseSection.REINVESTMENT: 0.05,
                },
            ),
        )

    def _growth_valuation(
        self,
        request: GrowthValuationDiagnosticRequest,
    ) -> _DiagnosticOutcome:
        adjusted_research_growth = request.research_growth_rate - request.dilution_rate
        growth_gap = adjusted_research_growth - request.market_implied_growth_rate
        neutral_band = self.config.valuation.neutral_growth_gap
        if growth_gap > neutral_band:
            direction = AdjustmentDirection.INCREASE
            signal = "RESEARCH_GROWTH_ABOVE_IMPLIED"
        elif growth_gap < -neutral_band:
            direction = AdjustmentDirection.DECREASE
            signal = "RESEARCH_GROWTH_BELOW_IMPLIED"
        else:
            direction = AdjustmentDirection.NEUTRAL
            signal = "RESEARCH_GROWTH_NEAR_IMPLIED"
        magnitude = float(min(abs(growth_gap), Decimal("1")))
        metrics = [
            SpecialistMetricInput(
                metric_name=name,
                value=str(value),
                unit="ratio",
                evidence_ids=request.valuation_evidence_ids,
            )
            for name, value in (
                ("market_implied_growth_rate", request.market_implied_growth_rate),
                ("research_growth_rate", request.research_growth_rate),
                ("dilution_rate", request.dilution_rate),
                ("adjusted_research_growth_rate", adjusted_research_growth),
                ("growth_expectation_gap", growth_gap),
                ("reinvestment_rate", request.reinvestment_rate),
            )
        ]
        evidence_ids = _ordered_union(
            request.valuation_evidence_ids,
            request.consensus_evidence_ids,
        )
        if request.consensus_available:
            assert request.consensus_growth_rate is not None
            metrics.append(
                SpecialistMetricInput(
                    metric_name="consensus_growth_rate",
                    value=str(request.consensus_growth_rate),
                    unit="ratio",
                    evidence_ids=request.consensus_evidence_ids,
                )
            )
        degradation = [] if request.consensus_available else ["CONSENSUS_UNAVAILABLE"]
        return _DiagnosticOutcome(
            status=(
                DiagnosticStatus.PASS
                if request.consensus_available
                else DiagnosticStatus.PARTIAL
            ),
            signal_codes=[signal],
            degradation_codes=degradation,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=[
                    ResearchFindingInput(
                        statement=(
                            f"Dilution-adjusted research growth differs from market-implied "
                            f"growth by {growth_gap}; this is an expectations comparison, "
                            "not a target price."
                        ),
                        finding_type=ResearchFindingType.ANALYST_INFERENCE,
                        confidence=0.6 if request.consensus_available else 0.5,
                        critical=False,
                        evidence_ids=evidence_ids,
                    )
                ],
                base_case_corrections=[],
                industry_specific_metrics=metrics,
                additional_evidence_requests=(
                    []
                    if request.consensus_available
                    else [
                        SpecialistEvidenceRequest(
                            request_code="OBTAIN_CONSENSUS_ESTIMATES",
                            reason="Consensus is unavailable and is not replaced with zero.",
                            required_evidence=["VERSIONED_CONSENSUS_ESTIMATES"],
                            blocking=False,
                        )
                    ]
                ),
                failure_modes=["TARGET_PRICE_FALSE_PRECISION", "TERMINAL_VALUE_DOMINANCE"],
                confidence_delta=0 if request.consensus_available else -0.03,
                valuation_adjustments=[
                    SpecialistAdjustmentInput(
                        dimension="growth_expectations",
                        direction=direction,
                        magnitude=magnitude,
                        rationale=(
                            "Adjustment is bounded by the versioned neutral growth gap and "
                            "does not produce a standalone target price."
                        ),
                        evidence_ids=request.valuation_evidence_ids,
                    )
                ],
                risk_adjustments=[],
                coverage_delta={BaseCaseSection.VALUATION_EXPECTATIONS: 0.1},
            ),
        )

    def _daily_trend(
        self,
        request: DailyTrendDiagnosticRequest,
    ) -> _DiagnosticOutcome:
        rules = self.config.daily
        gate_codes: list[str] = []
        if request.quality_status is not QualityStatus.PASS:
            gate_codes.append("QUALITY_GATE_FAILED")
        if request.bar_count < rules.minimum_bars:
            gate_codes.append("INSUFFICIENT_DAILY_BARS")
        metrics = [
            SpecialistMetricInput(
                metric_name=name,
                value=str(value),
                unit=unit,
                evidence_ids=request.evidence_ids,
            )
            for name, value, unit in (
                ("daily_bar_count", request.bar_count, "count"),
                ("daily_close_vs_ma20", request.close_vs_ma20, "ratio"),
                ("daily_ma20_slope", request.ma20_slope, "ratio"),
                ("daily_ma60_slope", request.ma60_slope, "ratio"),
                ("daily_drawdown_from_60d_high", request.drawdown_from_60d_high, "ratio"),
                ("daily_volume_ratio_20d", request.volume_ratio_20d, "ratio"),
            )
        ]
        if gate_codes:
            return self._trend_gate_failure(
                request=request,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                metric_inputs=metrics,
                gate_codes=gate_codes,
                coverage_code=BaseCaseSection.PRICE_TREND_CONTEXT,
            )
        score = sum(
            (
                int(request.close_vs_ma20 > 0),
                int(request.ma20_slope > 0),
                int(request.ma60_slope > 0),
                int(request.volume_ratio_20d >= 1),
                -int(request.drawdown_from_60d_high <= rules.drawdown_alert),
            )
        )
        if score >= rules.positive_min_score:
            context = "HEALTHY"
        elif score <= rules.negative_max_score:
            context = "WEAK"
        else:
            context = "MIXED"
        risk_adjustments = []
        if context == "WEAK":
            risk_adjustments.append(
                SpecialistAdjustmentInput(
                    dimension="daily_trend_context",
                    direction=AdjustmentDirection.INCREASE,
                    magnitude=0.1,
                    rationale="Weak daily context raises timing risk but is not a sell order.",
                    evidence_ids=request.evidence_ids,
                )
            )
        return _DiagnosticOutcome(
            status=DiagnosticStatus.PASS,
            signal_codes=[f"DAILY_TREND_{context}"],
            degradation_codes=[],
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=[
                    ResearchFindingInput(
                        statement=(
                            f"Versioned daily indicators classify price-trend context as "
                            f"{context} with score {score}; this is not a buy signal."
                        ),
                        finding_type=ResearchFindingType.ANALYST_INFERENCE,
                        confidence=0.55,
                        critical=False,
                        evidence_ids=request.evidence_ids,
                    )
                ],
                base_case_corrections=[],
                industry_specific_metrics=[
                    *metrics,
                    SpecialistMetricInput(
                        metric_name="daily_trend_score",
                        value=float(score),
                        unit="score",
                        evidence_ids=request.evidence_ids,
                    ),
                ],
                additional_evidence_requests=[],
                failure_modes=["TREND_IS_NOT_A_FUNDAMENTAL_FACT"],
                confidence_delta=-0.03 if context == "WEAK" else 0,
                valuation_adjustments=[],
                risk_adjustments=risk_adjustments,
                coverage_delta={BaseCaseSection.PRICE_TREND_CONTEXT: 0.1},
            ),
        )

    def _hourly_swing(
        self,
        request: HourlySwingDiagnosticRequest,
    ) -> _DiagnosticOutcome:
        rules = self.config.hourly
        gate_codes: list[str] = []
        if request.quality_status is not QualityStatus.PASS:
            gate_codes.append("QUALITY_GATE_FAILED")
        if request.bar_count < rules.minimum_bars:
            gate_codes.append("INSUFFICIENT_HOURLY_BARS")
        metrics = [
            SpecialistMetricInput(
                metric_name=name,
                value=str(value),
                unit=unit,
                evidence_ids=request.evidence_ids,
            )
            for name, value, unit in (
                ("hourly_bar_count", request.bar_count, "count"),
                ("hourly_close_vs_vwap_20h", request.close_vs_vwap_20h, "ratio"),
                ("hourly_ema12_slope", request.ema12_slope, "ratio"),
                ("hourly_realized_volatility_20h", request.realized_volatility_20h, "ratio"),
                ("hourly_drawdown_10h", request.drawdown_10h, "ratio"),
                ("hourly_volume_ratio_20h", request.volume_ratio_20h, "ratio"),
            )
        ]
        if gate_codes:
            return self._trend_gate_failure(
                request=request,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                metric_inputs=metrics,
                gate_codes=gate_codes,
                coverage_code=BaseCaseSection.PRICE_TREND_CONTEXT,
            )
        assert rules.volatility_alert is not None
        score = sum(
            (
                int(request.close_vs_vwap_20h > 0),
                int(request.ema12_slope > 0),
                int(request.volume_ratio_20h >= 1),
                -int(request.realized_volatility_20h >= rules.volatility_alert),
                -int(request.drawdown_10h <= rules.drawdown_alert),
            )
        )
        if score >= rules.positive_min_score:
            context = "POSITIVE"
        elif score <= rules.negative_max_score:
            context = "NEGATIVE"
        else:
            context = "NEUTRAL"
        risk_adjustments = []
        if context == "NEGATIVE":
            risk_adjustments.append(
                SpecialistAdjustmentInput(
                    dimension="hourly_timing_context",
                    direction=AdjustmentDirection.INCREASE,
                    magnitude=0.1,
                    rationale="Negative hourly context raises timing risk but is not an order.",
                    evidence_ids=request.evidence_ids,
                )
            )
        return _DiagnosticOutcome(
            status=DiagnosticStatus.PASS,
            signal_codes=[f"HOURLY_SWING_{context}"],
            degradation_codes=[],
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                incremental_findings=[
                    ResearchFindingInput(
                        statement=(
                            f"Independent 60-minute rules classify timing context as "
                            f"{context} with score {score}; this is not an order."
                        ),
                        finding_type=ResearchFindingType.ANALYST_INFERENCE,
                        confidence=0.5,
                        critical=False,
                        evidence_ids=request.evidence_ids,
                    )
                ],
                base_case_corrections=[],
                industry_specific_metrics=[
                    *metrics,
                    SpecialistMetricInput(
                        metric_name="hourly_swing_score",
                        value=float(score),
                        unit="score",
                        evidence_ids=request.evidence_ids,
                    ),
                ],
                additional_evidence_requests=[],
                failure_modes=["HOURLY_CONTEXT_IS_NOT_AN_ORDER"],
                confidence_delta=-0.03 if context == "NEGATIVE" else 0,
                valuation_adjustments=[],
                risk_adjustments=risk_adjustments,
                coverage_delta={BaseCaseSection.PRICE_TREND_CONTEXT: 0.05},
            ),
        )

    @staticmethod
    def _trend_gate_failure(
        *,
        request: DailyTrendDiagnosticRequest | HourlySwingDiagnosticRequest,
        skill_id: str,
        skill_version: str,
        metric_inputs: list[SpecialistMetricInput],
        gate_codes: list[str],
        coverage_code: BaseCaseSection,
    ) -> _DiagnosticOutcome:
        evidence_requests = [
            SpecialistEvidenceRequest(
                request_code=code,
                reason="The market diagnostic quality or sample gate did not pass.",
                required_evidence=["PASSING_VERSIONED_MARKET_QUALITY_REPORT"],
                blocking=True,
            )
            for code in gate_codes
        ]
        return _DiagnosticOutcome(
            status=DiagnosticStatus.INSUFFICIENT,
            signal_codes=["MARKET_DIAGNOSTIC_GATE_REJECTED"],
            degradation_codes=gate_codes,
            delta_request=SpecialistDeltaBuildRequest(
                base_case_id=request.base_case_id,
                route_plan_id=request.route_plan_id,
                skill_id=skill_id,
                skill_version=skill_version,
                incremental_findings=[],
                base_case_corrections=[],
                industry_specific_metrics=metric_inputs,
                additional_evidence_requests=evidence_requests,
                failure_modes=["MARKET_QUALITY_GATE_FAILED"],
                confidence_delta=-0.1,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={coverage_code: -0.1},
            ),
        )

    def _artifact_mismatch(self, artifact_id: str, object_hash: str) -> int:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return int(row is None or str(row["object_hash"]) != object_hash)


def _ordered_union(*values: list[str]) -> list[str]:
    return sorted({item for group in values for item in group})


def _unique_pairs(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(values))


def _structured_memo_refs(request: ResearchMemoComposeRequestV2) -> set[str]:
    memo = request.structured_memo
    refs = {
        reference
        for controversy in memo.controversies
        for reference in (
            *controversy.supporting_source_refs,
            *controversy.opposing_source_refs,
        )
    }
    for scenario in memo.scenarios:
        refs.update(scenario.assumption_source_refs)
        refs.update(scenario.growth_hypothesis_refs)
        if scenario.probability_ref is not None:
            refs.add(scenario.probability_ref)
    for catalyst in memo.catalysts:
        refs.update(catalyst.source_refs)
    for invalidation in memo.invalidations:
        refs.update(invalidation.source_refs)
    refs.update(item.source_ref for item in memo.monitoring_items)
    return refs


def _growth_memo_ref_domains(
    deltas: list[SpecialistDelta],
) -> tuple[set[str], set[str]]:
    hypothesis_refs: set[str] = set()
    posterior_refs: set[str] = set()
    for delta in deltas:
        contract = delta.method_contract
        if not isinstance(contract, GrowthProbabilityContractV2):
            continue
        for hypothesis in contract.input.hypotheses:
            hypothesis_id = hypothesis.hypothesis_id.value
            hypothesis_refs.update({hypothesis_id, f"hypothesis:{hypothesis_id}"})
        for hypothesis_id in contract.final_posterior:
            value = hypothesis_id.value
            posterior_refs.update({f"posterior:{value}", f"posterior_{value}"})
    return hypothesis_refs, posterior_refs


def _serenity_method_source_refs(delta: SpecialistDelta) -> set[str]:
    contract = delta.method_contract
    if contract is None:
        return set()
    payload = contract.model_dump(mode="json")
    refs: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {
                    "node_id",
                    "universe_id",
                    "alternative_id",
                    "event_id",
                    "update_id",
                    "invalidation_id",
                    "factor_id",
                } and isinstance(item, str):
                    refs.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    kind = str(payload["contract_kind"])
    if kind == "INDUSTRY_BOTTLENECK":
        refs.update(f"scarcity:{item['metric']}" for item in payload["scarcity"])
        refs.update(
            f"value_capture:{item['company_id']}" for item in payload["value_capture"]
        )
    elif kind == "EVENT_TO_ALPHA":
        refs.add(f"business_purity:{payload['business_purity']['metric']}")
        refs.update(
            f"transmission:{item['step_no']}" for item in payload["transmission_steps"]
        )
        refs.update(
            f"checkpoint:{item['quarter_offset']}"
            for item in payload["validation_checkpoints"]
        )
        refs.update({"event:scale_elasticity", "event:falsifier"})
        if payload["market_misclassification"] is not None:
            refs.add("event:market_misclassification")
    elif kind == "GROWTH_PROBABILITY":
        refs.update(
            reference
            for hypothesis_id in GrowthHypothesisId
            for reference in (
                hypothesis_id.value,
                f"hypothesis:{hypothesis_id.value}",
                f"posterior:{hypothesis_id.value}",
                f"posterior_{hypothesis_id.value}",
            )
        )
    elif kind == "GROWTH_VALUATION":
        if payload["tam_runway"] is not None:
            refs.add("valuation:tam_runway")
        if payload["peg"] is not None:
            refs.add("valuation:peg")
        refs.update(
            f"valuation:quality:{item['factor_id']}" for item in payload["quality_factors"]
        )
    elif kind == "DAILY_TREND_HEALTH":
        refs.add("daily:series")
        refs.update(f"dma:{item['window']}" for item in payload["moving_averages"])
        refs.update(
            f"fundamental:{item['metric']}" for item in payload["fundamental_growth"]
        )
        refs.update(
            f"revision:{item['metric']}:{item['forecast_period']}"
            for item in payload["estimate_revisions"]
        )
    return refs


def _bayesian_update(
    prior: dict[GrowthHypothesisId, Decimal],
    likelihood: dict[GrowthHypothesisId, Decimal],
) -> dict[GrowthHypothesisId, Decimal]:
    weighted = {
        hypothesis_id: prior[hypothesis_id] * likelihood[hypothesis_id]
        for hypothesis_id in GrowthHypothesisId
    }
    denominator = sum(weighted.values(), Decimal("0"))
    if denominator <= 0:
        raise ValueError("Bayesian likelihood update has zero evidence probability")
    quantum = Decimal("0.000000000001")
    result: dict[GrowthHypothesisId, Decimal] = {}
    ordered = list(GrowthHypothesisId)
    for hypothesis_id in ordered[:-1]:
        result[hypothesis_id] = (weighted[hypothesis_id] / denominator).quantize(
            quantum
        )
    result[ordered[-1]] = Decimal("1") - sum(result.values(), Decimal("0"))
    if result[ordered[-1]] < 0 or result[ordered[-1]] > 1:
        raise ValueError("Bayesian residual normalization escaped probability bounds")
    return result


__all__ = [
    "DiagnosticExecution",
    "ResearchDiagnosticsService",
    "ResearchMemoExecution",
]
