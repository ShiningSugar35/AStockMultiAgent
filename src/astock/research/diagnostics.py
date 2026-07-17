"""Deterministic specialist diagnostics and citation-preserving memo composition."""

from __future__ import annotations

from dataclasses import dataclass
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
    DiagnosticStatus,
    EventToAlphaDiagnosticRequest,
    GrowthProbabilityDiagnosticRequest,
    GrowthValuationDiagnosticRequest,
    HourlySwingDiagnosticRequest,
    IndustryBottleneckDiagnosticRequest,
    QualityStatus,
    ResearchDiagnosticConfig,
    ResearchFindingInput,
    ResearchFindingType,
    ResearchMemoArtifact,
    ResearchMemoComposeRequest,
    ResearchMemoDeltaReference,
    ResearchMemoSectionReference,
    ResearchSkillRegistry,
    SpecialistAdjustmentInput,
    SpecialistCoverageStatus,
    SpecialistDelta,
    SpecialistDeltaBuildRequest,
    SpecialistDiagnosticReport,
    SpecialistDiagnosticRequest,
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

    def diagnose(self, request: SpecialistDiagnosticRequest) -> DiagnosticExecution:
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
        request: ResearchMemoComposeRequest,
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
        if route.coverage_status is SpecialistCoverageStatus.INSUFFICIENT:
            coverage_status = SpecialistCoverageStatus.INSUFFICIENT
        elif missing_selected or degradation_codes:
            coverage_status = SpecialistCoverageStatus.PARTIAL
        else:
            coverage_status = SpecialistCoverageStatus.SUFFICIENT

        input_hash = content_hash(
            {
                "base_case_id": base_case.base_case_id,
                "route_plan_id": route.route_plan_id,
                "delta_ids": sorted(request.delta_ids),
            }
        )
        memo_identity = {
            "base_case_hash": self.repository.base_case_object_hash(base_case.base_case_id),
            "route_hash": self.repository.route_plan_object_hash(route.route_plan_id),
            "delta_hashes": [
                self.repository.specialist_delta_object_hash(delta.delta_id)
                for delta in sorted(deltas, key=lambda item: item.delta_id)
            ],
            "input_hash": input_hash,
            "composer_version": "research-memo-composer-v1",
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
        request: SpecialistDiagnosticRequest,
    ) -> _DiagnosticOutcome:
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


__all__ = [
    "DiagnosticExecution",
    "ResearchDiagnosticsService",
    "ResearchMemoExecution",
]
