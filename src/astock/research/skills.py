"""Versioned research Skill registry, deterministic routing, and cited deltas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.research.repository import ResearchRepository
from astock.research.resource_policy import (
    SpecialistResourcePolicy,
    load_specialist_resource_policy,
)
from astock.schemas import (
    CitedResearchFinding,
    EvidenceConflict,
    EvidenceGrade,
    FactStatus,
    FrozenEvidencePack,
    PointInTimeStatus,
    ResearchCoverageStatus,
    ResearchFindingInput,
    ResearchSkillManifest,
    ResearchSkillRegistry,
    ResearchSkillStatus,
    SerenityMethodContractV2,
    SpecialistAdjustment,
    SpecialistAdjustmentInput,
    SpecialistCoverageStatus,
    SpecialistDelta,
    SpecialistDeltaBuildRequest,
    SpecialistEligibility,
    SpecialistMetric,
    SpecialistMetricInput,
    SpecialistRouteMatch,
    SpecialistRoutePlan,
    SpecialistRouteRequest,
)
from astock.schemas.serenity_v2 import (
    DailyTrendHealthContractV2,
    EventToAlphaContractV2,
    GrowthProbabilityContractV2,
    GrowthValuationContractV2,
    IndustryBottleneckContractV2,
    JuglarCycleDimension,
    JuglarCycleStageContractV1,
)


@dataclass(frozen=True, slots=True)
class SkillRegistryExecution:
    registry: ResearchSkillRegistry
    object_sha256: str


@dataclass(frozen=True, slots=True)
class SpecialistRouteExecution:
    plan: SpecialistRoutePlan
    object_sha256: str


@dataclass(frozen=True, slots=True)
class SpecialistDeltaExecution:
    delta: SpecialistDelta
    object_sha256: str


_EVIDENCE_GRADE_STRENGTH = {
    EvidenceGrade.COMMUNITY_LEAD: 0,
    EvidenceGrade.SECONDARY: 1,
    EvidenceGrade.PRIVATE_PRIMARY: 2,
    EvidenceGrade.PRIMARY_OFFICIAL: 3,
}


class ResearchSkillService:
    """Apply explicit, stable routing rules over one frozen BaseCase."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        registry: ResearchSkillRegistry,
        *,
        resource_policy: SpecialistResourcePolicy | None = None,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.configured_registry = registry
        self.repository = ResearchRepository(state, object_store)
        self.evidence_repository = EvidenceRepository(state)
        self.resource_policy = resource_policy or load_specialist_resource_policy(
            Path(__file__).resolve().parents[3] / "configs" / "specialist_resource_policy.yaml"
        )

    def register_registry(self) -> SkillRegistryExecution:
        config_hash = content_hash(self.configured_registry)
        summary = self.repository.skill_registry_summary(self.configured_registry.registry_version)
        if summary is not None:
            if str(summary["config_hash"]) != config_hash:
                raise ValueError(
                    "research Skill registry version already exists with different content"
                )
            registry = self.repository.get_skill_registry(self.configured_registry.registry_version)
            assert registry is not None
            return SkillRegistryExecution(
                registry=registry,
                object_sha256=str(summary["object_hash"]),
            )

        object_ref = self.object_store.put_json(self.configured_registry.model_dump(mode="json"))
        registry = self.repository.register_skill_registry(
            self.configured_registry,
            object_hash=object_ref.sha256,
            config_hash=config_hash,
        )
        self.state.register_artifact(
            artifact_id=f"ResearchSkillRegistry:{registry.registry_version}",
            artifact_type="ResearchSkillRegistry",
            schema_version=registry.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[config_hash],
        )
        return SkillRegistryExecution(registry=registry, object_sha256=object_ref.sha256)

    def route(self, request: SpecialistRouteRequest) -> SpecialistRouteExecution:
        registry_execution = self.register_registry()
        registry = registry_execution.registry
        base_case = self.repository.get_base_case(request.base_case_id)
        if base_case is None:
            raise ValueError(f"unknown BaseCase for specialist routing: {request.base_case_id}")
        effective_budget = self.resource_policy.resolve(request.specialist_budget)
        if len(request.explicit_skill_ids) > effective_budget:
            raise ValueError("explicit specialist request exceeds the active resource budget")

        manifest_by_id = {item.skill_id: item for item in registry.skills}
        unknown = sorted(set(request.explicit_skill_ids) - set(manifest_by_id))
        if unknown:
            raise ValueError("explicit specialist request contains an unknown Skill")
        explicit_manifests = [manifest_by_id[item] for item in request.explicit_skill_ids]
        if any(not item.counts_as_specialist for item in explicit_manifests):
            raise ValueError("ResearchMemoComposer cannot be routed as a specialist")
        if any(
            item.status is not ResearchSkillStatus.ENABLED_CONTRACT for item in explicit_manifests
        ):
            raise ValueError("explicit specialist request contains a disabled Skill")
        self._reject_explicit_incompatibilities(explicit_manifests)

        request_hash = content_hash(request)
        base_case_hash = self.repository.base_case_object_hash(base_case.base_case_id)
        assert base_case_hash is not None
        route_identity = {
            "base_case_hash": base_case_hash,
            "registry_hash": registry_execution.object_sha256,
            "request_hash": request_hash,
        }
        route_plan_id = f"specialist-route:{content_hash(route_identity)}"
        existing = self.repository.get_route_plan(route_plan_id)
        if existing is not None:
            object_hash = self.repository.route_plan_object_hash(route_plan_id)
            assert object_hash is not None
            return SpecialistRouteExecution(plan=existing, object_sha256=object_hash)

        explicit_ids = set(request.explicit_skill_ids)
        thesis_tags = set(request.thesis_tags) | set(base_case.specialist_tags)
        eligible: list[tuple[ResearchSkillManifest, SpecialistRouteMatch]] = []
        unavailable: list[SpecialistRouteMatch] = []
        excluded: dict[str, list[str]] = {}
        for manifest in sorted(registry.skills, key=lambda item: item.skill_id):
            if not manifest.counts_as_specialist:
                excluded[manifest.skill_id] = ["NON_SPECIALIST_COMPOSER"]
                continue
            if manifest.status is not ResearchSkillStatus.ENABLED_CONTRACT:
                excluded[manifest.skill_id] = ["SKILL_NOT_ENABLED"]
                continue
            match = self._match_manifest(
                manifest,
                request,
                thesis_tags=thesis_tags,
                explicit=manifest.skill_id in explicit_ids,
            )
            if match is None:
                excluded[manifest.skill_id] = ["NO_RULE_MATCH"]
                continue
            if match.eligibility is SpecialistEligibility.UNAVAILABLE:
                unavailable.append(match)
            else:
                eligible.append((manifest, match))

        eligible.sort(key=lambda item: (-item[1].score, item[0].skill_id))
        selected: list[SpecialistRouteMatch] = []
        selected_manifests: list[ResearchSkillManifest] = []
        capped = False
        for manifest, match in eligible:
            conflict = self._selected_conflict(manifest, selected_manifests)
            if conflict is not None:
                excluded[manifest.skill_id] = [f"INCOMPATIBLE_WITH:{conflict}"]
                continue
            if len(selected) >= effective_budget:
                excluded[manifest.skill_id] = ["ROUTE_CAPPED_AT_RESOURCE_BUDGET"]
                capped = True
                continue
            selected.append(match)
            selected_manifests.append(manifest)

        degradation_codes = {code for match in selected for code in match.degradation_codes}
        if unavailable:
            degradation_codes.add("REQUIRED_SPECIALIST_INPUT_UNAVAILABLE")
        if capped:
            degradation_codes.add("ROUTE_CAPPED_AT_RESOURCE_BUDGET")
        if base_case.coverage_status is not ResearchCoverageStatus.COMPLETE:
            degradation_codes.add("BASE_CASE_COVERAGE_INCOMPLETE")
        if not selected:
            degradation_codes.add("NO_SPECIALIST_MATCH")
            coverage_status = SpecialistCoverageStatus.INSUFFICIENT
        elif degradation_codes:
            coverage_status = SpecialistCoverageStatus.PARTIAL
        else:
            coverage_status = SpecialistCoverageStatus.SUFFICIENT
        confidence_cap = min(
            base_case.confidence_cap,
            registry.coverage_confidence_caps[coverage_status],
        )
        plan = SpecialistRoutePlan(
            route_plan_id=route_plan_id,
            base_case_id=base_case.base_case_id,
            evidence_pack_id=base_case.evidence_pack_id,
            registry_version=registry.registry_version,
            selected=selected,
            unavailable=sorted(unavailable, key=lambda item: item.skill_id),
            excluded_skill_reasons=dict(sorted(excluded.items())),
            coverage_status=coverage_status,
            confidence_cap=confidence_cap,
            max_specialists=effective_budget,
            degradation_codes=sorted(degradation_codes),
        )
        object_ref = self.object_store.put_json(plan.model_dump(mode="json"))
        stored = self.repository.register_route_plan(
            plan,
            object_hash=object_ref.sha256,
            request_hash=request_hash,
        )
        stored_hash = self.repository.route_plan_object_hash(stored.route_plan_id)
        assert stored_hash is not None
        self.state.register_artifact(
            artifact_id=f"SpecialistRoutePlan:{stored.route_plan_id}",
            artifact_type="SpecialistRoutePlan",
            schema_version=stored.schema_version,
            object_hash=stored_hash,
            input_hashes=[base_case_hash, registry_execution.object_sha256, request_hash],
        )
        self.state.set_checkpoint(
            scope_type="research-specialist-route",
            scope_key=stored.route_plan_id,
            cursor={
                "selected_count": len(stored.selected),
                "unavailable_count": len(stored.unavailable),
            },
            status="SUCCEEDED",
            object_hash=stored_hash,
        )
        return SpecialistRouteExecution(plan=stored, object_sha256=stored_hash)

    def build_delta(
        self,
        request: SpecialistDeltaBuildRequest,
    ) -> SpecialistDeltaExecution:
        registry_execution = self.register_registry()
        registry = registry_execution.registry
        route_plan = self.repository.get_route_plan(request.route_plan_id)
        if route_plan is None:
            raise ValueError(f"unknown specialist route plan: {request.route_plan_id}")
        if route_plan.base_case_id != request.base_case_id:
            raise ValueError("SpecialistDelta BaseCase must match its route plan")
        selected = {(item.skill_id, item.skill_version): item for item in route_plan.selected}
        if (request.skill_id, request.skill_version) not in selected:
            raise ValueError("SpecialistDelta can only be produced by a selected Skill version")
        manifest = next(
            (
                item
                for item in registry.skills
                if item.skill_id == request.skill_id and item.skill_version == request.skill_version
            ),
            None,
        )
        if manifest is None or manifest.status is not ResearchSkillStatus.ENABLED_CONTRACT:
            raise ValueError("selected specialist Skill version is no longer registered")

        base_case = self.repository.get_base_case(request.base_case_id)
        if base_case is None:
            raise ValueError(f"unknown BaseCase for SpecialistDelta: {request.base_case_id}")
        evidence_pack = self.repository.get_evidence_pack(base_case.evidence_pack_id)
        if evidence_pack is None:
            raise ValueError("SpecialistDelta frozen evidence pack is unavailable")
        request_hash = content_hash(request)
        delta_identity = {
            "route_plan_id": route_plan.route_plan_id,
            "skill_id": request.skill_id,
            "skill_version": request.skill_version,
            "request_hash": request_hash,
        }
        delta_id = f"specialist-delta:{content_hash(delta_identity)}"
        existing = self.repository.get_specialist_delta(delta_id)
        if existing is not None:
            object_hash = self.repository.specialist_delta_object_hash(delta_id)
            assert object_hash is not None
            return SpecialistDeltaExecution(delta=existing, object_sha256=object_hash)

        evidence_scope = set(evidence_pack.evidence_ids)
        if request.method_contract is not None:
            self._require_frozen_evidence(
                request.method_contract.evidence_ids,
                evidence_scope,
            )
            self._validate_v2_method_evidence(
                request.method_contract,
                evidence_pack=evidence_pack,
                base_as_of=base_case.as_of,
            )
        now = datetime.now(UTC)
        findings = self._build_findings(
            request.incremental_findings,
            delta_id=delta_id,
            category="incremental",
            evidence_scope=evidence_scope,
            evidence_grade_by_id=evidence_pack.evidence_grade_by_id,
            created_at=now,
        )
        corrections = self._build_findings(
            request.base_case_corrections,
            delta_id=delta_id,
            category="correction",
            evidence_scope=evidence_scope,
            evidence_grade_by_id=evidence_pack.evidence_grade_by_id,
            created_at=now,
        )
        metrics = [
            self._build_metric(
                item,
                delta_id=delta_id,
                position=index,
                evidence_scope=evidence_scope,
                created_at=now,
            )
            for index, item in enumerate(request.industry_specific_metrics)
        ]
        valuation_adjustments = [
            self._build_adjustment(
                item,
                delta_id=delta_id,
                category="valuation",
                position=index,
                evidence_scope=evidence_scope,
                created_at=now,
            )
            for index, item in enumerate(request.valuation_adjustments)
        ]
        risk_adjustments = [
            self._build_adjustment(
                item,
                delta_id=delta_id,
                category="risk",
                position=index,
                evidence_scope=evidence_scope,
                created_at=now,
            )
            for index, item in enumerate(request.risk_adjustments)
        ]
        evidence_ids = sorted(
            {
                evidence_id
                for item in (
                    *findings,
                    *corrections,
                    *metrics,
                    *valuation_adjustments,
                    *risk_adjustments,
                )
                for evidence_id in item.evidence_ids
            }
            | (
                set(request.method_contract.evidence_ids)
                if request.method_contract is not None
                else set()
            )
        )
        delta = SpecialistDelta(
            delta_id=delta_id,
            base_case_id=base_case.base_case_id,
            evidence_pack_id=evidence_pack.pack_id,
            route_plan_id=route_plan.route_plan_id,
            skill_id=request.skill_id,
            skill_version=request.skill_version,
            incremental_findings=findings,
            base_case_corrections=corrections,
            industry_specific_metrics=metrics,
            additional_evidence_requests=request.additional_evidence_requests,
            failure_modes=request.failure_modes,
            confidence_delta=request.confidence_delta,
            valuation_adjustments=valuation_adjustments,
            risk_adjustments=risk_adjustments,
            coverage_delta=request.coverage_delta,
            evidence_ids=evidence_ids,
            method_contract=request.method_contract,
            created_at=now,
        )
        object_ref = self.object_store.put_json(delta.model_dump(mode="json"))
        stored = self.repository.register_specialist_delta(
            delta,
            object_hash=object_ref.sha256,
            request_hash=request_hash,
        )
        stored_hash = self.repository.specialist_delta_object_hash(stored.delta_id)
        assert stored_hash is not None
        route_hash = self.repository.route_plan_object_hash(route_plan.route_plan_id)
        assert route_hash is not None
        self.state.register_artifact(
            artifact_id=f"SpecialistDelta:{stored.delta_id}",
            artifact_type="SpecialistDelta",
            schema_version=stored.schema_version,
            object_hash=stored_hash,
            input_hashes=[route_hash, registry_execution.object_sha256, request_hash],
        )
        self.state.set_checkpoint(
            scope_type="research-specialist-delta",
            scope_key=stored.delta_id,
            cursor={
                "finding_count": len(stored.incremental_findings),
                "correction_count": len(stored.base_case_corrections),
                "evidence_count": len(stored.evidence_ids),
            },
            status="SUCCEEDED",
            object_hash=stored_hash,
        )
        return SpecialistDeltaExecution(delta=stored, object_sha256=stored_hash)

    def status(self, base_case_id: str) -> dict[str, object]:
        route = self.repository.latest_route_plan_summary(base_case_id)
        if route is None:
            return {"status": "NOT_RUN", "base_case_id": base_case_id}
        deltas = self.repository.specialist_delta_summaries(str(route["route_plan_id"]))
        return {
            "status": route["coverage_status"],
            "base_case_id": base_case_id,
            "route_plan": route,
            "delta_count": len(deltas),
            "deltas": deltas,
        }

    def audit(self, base_case_id: str) -> dict[str, object]:
        route_summary = self.repository.latest_route_plan_summary(base_case_id)
        if route_summary is None:
            return {"status": "NOT_RUN", "base_case_id": base_case_id}
        route_id = str(route_summary["route_plan_id"])
        route = self.repository.get_route_plan(route_id)
        if route is None:
            return {
                "status": "PARTIAL",
                "base_case_id": base_case_id,
                "finding_codes": ["ROUTE_OBJECT_MISSING_OR_INVALID"],
            }
        base_case = self.repository.get_base_case(route.base_case_id)
        registry = self.repository.get_skill_registry(route.registry_version)
        evidence_pack = (
            self.repository.get_evidence_pack(route.evidence_pack_id)
            if base_case is not None
            else None
        )
        route_metadata_mismatch = int(
            int(str(route_summary["selected_count"])) != len(route.selected)
            or int(str(route_summary["unavailable_count"])) != len(route.unavailable)
            or int(str(route_summary["degradation_count"])) != len(route.degradation_codes)
            or str(route_summary["coverage_status"]) != route.coverage_status.value
        )
        selected = {(item.skill_id, item.skill_version) for item in route.selected}
        registry_pairs = (
            {(item.skill_id, item.skill_version) for item in registry.skills}
            if registry is not None
            else set()
        )
        selected_registry_mismatch = sum(item not in registry_pairs for item in selected)
        delta_summaries = self.repository.specialist_delta_summaries(route_id)
        delta_object_missing = 0
        delta_metadata_mismatch = 0
        delta_unselected = 0
        evidence_outside_scope = 0
        evidence_record_missing = 0
        future_evidence = 0
        critical_grade_mismatch = 0
        artifact_registry_mismatch = 0
        evidence_scope = set(evidence_pack.evidence_ids) if evidence_pack else set()
        base_as_of = base_case.as_of if base_case else None
        for summary in delta_summaries:
            delta_id = str(summary["delta_id"])
            delta = self.repository.get_specialist_delta(delta_id)
            if delta is None:
                delta_object_missing += 1
                continue
            delta_metadata_mismatch += int(
                int(str(summary["incremental_finding_count"])) != len(delta.incremental_findings)
                or int(str(summary["correction_count"])) != len(delta.base_case_corrections)
                or int(str(summary["metric_count"])) != len(delta.industry_specific_metrics)
                or int(str(summary["evidence_request_count"]))
                != len(delta.additional_evidence_requests)
                or int(str(summary["evidence_count"])) != len(delta.evidence_ids)
            )
            delta_unselected += int((delta.skill_id, delta.skill_version) not in selected)
            evidence_outside_scope += sum(
                evidence_id not in evidence_scope for evidence_id in delta.evidence_ids
            )
            for evidence_id in delta.evidence_ids:
                evidence = self.evidence_repository.get_evidence(evidence_id)
                evidence_record_missing += int(evidence is None)
                future_evidence += int(
                    evidence is not None
                    and base_as_of is not None
                    and evidence.available_to_system_at > base_as_of
                )
            for finding in (*delta.incremental_findings, *delta.base_case_corrections):
                if finding.critical and evidence_pack is not None:
                    critical_grade_mismatch += int(
                        not any(
                            evidence_pack.evidence_grade_by_id.get(evidence_id)
                            is EvidenceGrade.PRIMARY_OFFICIAL
                            for evidence_id in finding.evidence_ids
                        )
                    )
            artifact_registry_mismatch += self._artifact_mismatch(
                artifact_id=f"SpecialistDelta:{delta.delta_id}",
                object_hash=str(summary["object_hash"]),
            )
        route_artifact_mismatch = self._artifact_mismatch(
            artifact_id=f"SpecialistRoutePlan:{route.route_plan_id}",
            object_hash=str(route_summary["object_hash"]),
        )
        findings = {
            "BASE_CASE_MISSING": int(base_case is None),
            "EVIDENCE_PACK_MISSING": int(evidence_pack is None),
            "REGISTRY_MISSING": int(registry is None),
            "ROUTE_METADATA_MISMATCH": route_metadata_mismatch,
            "SELECTED_SKILL_NOT_REGISTERED": selected_registry_mismatch,
            "DELTA_OBJECT_MISSING": delta_object_missing,
            "DELTA_METADATA_MISMATCH": delta_metadata_mismatch,
            "DELTA_FROM_UNSELECTED_SKILL": delta_unselected,
            "EVIDENCE_OUTSIDE_FROZEN_SCOPE": evidence_outside_scope,
            "EVIDENCE_RECORD_MISSING": evidence_record_missing,
            "FUTURE_EVIDENCE": future_evidence,
            "CRITICAL_EVIDENCE_GRADE_MISMATCH": critical_grade_mismatch,
            "ARTIFACT_REGISTRY_MISMATCH": artifact_registry_mismatch + route_artifact_mismatch,
        }
        finding_codes = sorted(code for code, count in findings.items() if count)
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "base_case_id": base_case_id,
            "route_plan_id": route.route_plan_id,
            "coverage_status": route.coverage_status,
            "selected_count": len(route.selected),
            "unavailable_count": len(route.unavailable),
            "delta_count": len(delta_summaries),
            "finding_codes": finding_codes,
            "finding_counts": findings,
        }

    def _match_manifest(
        self,
        manifest: ResearchSkillManifest,
        request: SpecialistRouteRequest,
        *,
        thesis_tags: set[str],
        explicit: bool,
    ) -> SpecialistRouteMatch | None:
        score = 100 if explicit else 0
        reason_codes = ["EXPLICIT_REQUEST"] if explicit else []
        trigger_hits = sorted(thesis_tags & set(manifest.trigger_tags))
        industry_hits = sorted(set(request.industry_tags) & set(manifest.industry_tags))
        event_hits = sorted(set(request.event_tags) & set(manifest.event_tags))
        if trigger_hits:
            score += 10 * len(trigger_hits)
            reason_codes.extend(f"TRIGGER_TAG:{item}" for item in trigger_hits)
        if industry_hits:
            score += 8 * len(industry_hits)
            reason_codes.extend(f"INDUSTRY_TAG:{item}" for item in industry_hits)
        if event_hits:
            score += 8 * len(event_hits)
            reason_codes.extend(f"EVENT_TAG:{item}" for item in event_hits)
        if request.horizon in manifest.horizons:
            score += 4
            reason_codes.append(f"HORIZON:{request.horizon}")
        if not explicit and not (trigger_hits or industry_hits or event_hits):
            return None
        missing_inputs = sorted(set(manifest.required_inputs) - set(request.available_inputs))
        missing_frequencies = sorted(
            set(manifest.required_frequencies) - set(request.available_frequencies)
        )
        missing_optional = sorted(
            set(manifest.optional_input_degradation_codes) - set(request.available_inputs)
        )
        degradation_codes = sorted(
            {manifest.optional_input_degradation_codes[item] for item in missing_optional}
        )
        if missing_inputs or missing_frequencies:
            eligibility = SpecialistEligibility.UNAVAILABLE
            if missing_inputs:
                reason_codes.append("REQUIRED_INPUT_UNAVAILABLE")
            if missing_frequencies:
                reason_codes.append("FREQUENCY_UNAVAILABLE")
        elif degradation_codes:
            eligibility = SpecialistEligibility.DEGRADED
            reason_codes.append("OPTIONAL_INPUT_UNAVAILABLE")
        else:
            eligibility = SpecialistEligibility.READY
            reason_codes.append("INPUTS_AVAILABLE")
        return SpecialistRouteMatch(
            skill_id=manifest.skill_id,
            skill_version=manifest.skill_version,
            score=score,
            eligibility=eligibility,
            reason_codes=reason_codes,
            missing_required_inputs=missing_inputs,
            missing_required_frequencies=missing_frequencies,
            degradation_codes=degradation_codes,
        )

    @staticmethod
    def _selected_conflict(
        candidate: ResearchSkillManifest,
        selected: list[ResearchSkillManifest],
    ) -> str | None:
        for other in selected:
            if (
                other.skill_id in candidate.incompatible_skills
                or candidate.skill_id in other.incompatible_skills
            ):
                return other.skill_id
        return None

    def _reject_explicit_incompatibilities(
        self,
        manifests: list[ResearchSkillManifest],
    ) -> None:
        for index, manifest in enumerate(manifests):
            conflict = self._selected_conflict(manifest, manifests[:index])
            if conflict is not None:
                raise ValueError("explicit specialist request contains incompatible Skills")

    def _build_findings(
        self,
        inputs: list[ResearchFindingInput],
        *,
        delta_id: str,
        category: str,
        evidence_scope: set[str],
        evidence_grade_by_id: dict[str, EvidenceGrade],
        created_at: datetime,
    ) -> list[CitedResearchFinding]:
        findings: list[CitedResearchFinding] = []
        ids: set[str] = set()
        for position, item in enumerate(inputs):
            self._require_frozen_evidence(item.evidence_ids, evidence_scope)
            if item.critical and not any(
                evidence_grade_by_id[evidence_id] is EvidenceGrade.PRIMARY_OFFICIAL
                for evidence_id in item.evidence_ids
            ):
                raise ValueError(
                    "critical SpecialistDelta findings require PRIMARY_OFFICIAL evidence"
                )
            identity = {
                "delta_id": delta_id,
                "category": category,
                "position": position,
                "finding": item,
            }
            finding_id = f"specialist-finding:{content_hash(identity)}"
            if finding_id in ids:
                raise ValueError("duplicate SpecialistDelta finding")
            ids.add(finding_id)
            findings.append(
                CitedResearchFinding(
                    finding_id=finding_id,
                    statement=item.statement,
                    finding_type=item.finding_type,
                    confidence=item.confidence,
                    critical=item.critical,
                    evidence_ids=item.evidence_ids,
                    created_at=created_at,
                )
            )
        return findings

    def _build_metric(
        self,
        item: SpecialistMetricInput,
        *,
        delta_id: str,
        position: int,
        evidence_scope: set[str],
        created_at: datetime,
    ) -> SpecialistMetric:
        self._require_frozen_evidence(item.evidence_ids, evidence_scope)
        identity = {"delta_id": delta_id, "position": position, "metric": item}
        return SpecialistMetric(
            metric_id=f"specialist-metric:{content_hash(identity)}",
            metric_name=item.metric_name,
            value=item.value,
            unit=item.unit,
            evidence_ids=item.evidence_ids,
            created_at=created_at,
        )

    def _build_adjustment(
        self,
        item: SpecialistAdjustmentInput,
        *,
        delta_id: str,
        category: str,
        position: int,
        evidence_scope: set[str],
        created_at: datetime,
    ) -> SpecialistAdjustment:
        self._require_frozen_evidence(item.evidence_ids, evidence_scope)
        identity = {
            "delta_id": delta_id,
            "category": category,
            "position": position,
            "adjustment": item,
        }
        return SpecialistAdjustment(
            adjustment_id=f"specialist-adjustment:{content_hash(identity)}",
            dimension=item.dimension,
            direction=item.direction,
            magnitude=item.magnitude,
            rationale=item.rationale,
            evidence_ids=item.evidence_ids,
            created_at=created_at,
        )

    @staticmethod
    def _require_frozen_evidence(
        evidence_ids: list[str],
        evidence_scope: set[str],
    ) -> None:
        unknown = sorted(set(evidence_ids) - evidence_scope)
        if unknown:
            raise ValueError("SpecialistDelta references evidence outside the frozen pack")

    def _validate_v2_method_evidence(
        self,
        method_contract: SerenityMethodContractV2,
        *,
        evidence_pack: FrozenEvidencePack,
        base_as_of: datetime,
    ) -> None:
        evidence_requirements = _v2_method_evidence_requirements(method_contract)
        if not evidence_requirements:
            raise ValueError("v2 method contract requires evidence on every method node")

        open_conflict_evidence = self._open_conflict_evidence(evidence_pack.open_conflict_ids)
        for node_evidence, required_grade, role in evidence_requirements:
            self._require_frozen_evidence(node_evidence, set(evidence_pack.evidence_ids))
            for evidence_id in node_evidence:
                pit_status = evidence_pack.pit_status_by_evidence_id.get(evidence_id)
                if pit_status not in {
                    PointInTimeStatus.CERTIFIED,
                    PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
                }:
                    raise ValueError("v2 method evidence requires certified or reconstructed PIT")
                grade = evidence_pack.evidence_grade_by_id.get(evidence_id)
                if grade is None:
                    raise ValueError("v2 method evidence grade is unavailable")
                if _EVIDENCE_GRADE_STRENGTH[grade] < _EVIDENCE_GRADE_STRENGTH[required_grade]:
                    raise ValueError(
                        f"v2 method evidence for {role} requires {required_grade.value}"
                    )
                evidence = self.evidence_repository.get_evidence(evidence_id)
                if evidence is None:
                    raise ValueError("v2 method evidence record is unavailable")
                if evidence.available_to_system_at > base_as_of:
                    raise ValueError("v2 method evidence is future relative to the BaseCase")
                if evidence.valid_from is not None and evidence.valid_from > base_as_of:
                    raise ValueError("v2 method evidence is not yet valid at the BaseCase as_of")
                if evidence.valid_to is not None and evidence.valid_to < base_as_of:
                    raise ValueError("v2 method evidence is stale at the BaseCase as_of")
                if evidence.fact_status in {FactStatus.CONFLICTED, FactStatus.UNVERIFIED}:
                    raise ValueError("v2 method evidence cannot be conflicted or unverified")
                if evidence_id in open_conflict_evidence:
                    raise ValueError("v2 method evidence cannot participate in an open conflict")

    def _open_conflict_evidence(self, conflict_ids: list[str]) -> set[str]:
        if not conflict_ids:
            return set()
        placeholders = ",".join("?" for _ in conflict_ids)
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT conflict_json FROM evidence_conflict "
                f"WHERE conflict_id IN ({placeholders})",
                conflict_ids,
            ).fetchall()
        if len(rows) != len(conflict_ids):
            raise ValueError("v2 method frozen evidence conflict record is unavailable")
        return {
            evidence_id
            for row in rows
            for evidence_id in EvidenceConflict.model_validate_json(
                row["conflict_json"]
            ).evidence_ids
        }

    def _artifact_mismatch(self, *, artifact_id: str, object_hash: str) -> int:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return int(row is None or str(row["object_hash"]) != object_hash)


__all__ = [
    "ResearchSkillService",
    "SkillRegistryExecution",
    "SpecialistDeltaExecution",
    "SpecialistRouteExecution",
]


def _v2_method_evidence_requirements(
    contract: SerenityMethodContractV2,
) -> list[tuple[list[str], EvidenceGrade, str]]:
    """Return the evidence floor for each typed Serenity method-node role."""

    official = EvidenceGrade.PRIMARY_OFFICIAL
    secondary = EvidenceGrade.SECONDARY

    if isinstance(contract, IndustryBottleneckContractV2):
        nodes = [
            contract.system_change,
            *contract.chain_nodes,
            contract.candidate_universe,
            contract.necessary_link,
            *contract.scarcity,
            *contract.substitutions,
            *contract.value_capture,
            *contract.invalidation_conditions,
        ]
        return [(node.evidence_ids, official, "industry method node") for node in nodes]

    if isinstance(contract, EventToAlphaContractV2):
        official_nodes = [
            contract.event,
            contract.business_purity,
            *contract.transmission_steps,
            contract.scale_elasticity,
            *contract.validation_checkpoints,
            contract.falsifier,
        ]
        requirements = [
            (node.evidence_ids, official, "event fact/transmission node") for node in official_nodes
        ]
        if contract.market_misclassification is not None:
            requirements.append(
                (
                    contract.market_misclassification.evidence_ids,
                    secondary,
                    "event market-misclassification node",
                )
            )
        return requirements

    if isinstance(contract, GrowthProbabilityContractV2):
        method_input = contract.input
        requirements = [
            (node.evidence_ids, official, "growth hypothesis/likelihood node")
            for node in (*method_input.hypotheses, *method_input.likelihood_updates)
        ]
        requirements.append(
            (method_input.prior_basis.evidence_ids, secondary, "growth prior basis")
        )
        if method_input.consensus is not None:
            requirements.append(
                (method_input.consensus.evidence_ids, secondary, "growth consensus")
            )
        return requirements

    if isinstance(contract, GrowthValuationContractV2):
        requirements = [
            (node.evidence_ids, official, "valuation quality factor")
            for node in contract.quality_factors
        ]
        if contract.tam_runway is not None:
            requirements.append((contract.tam_runway.evidence_ids, official, "valuation TAM"))
        if contract.peg is not None:
            requirements.append((contract.peg.evidence_ids, secondary, "valuation PEG"))
        if contract.consensus is not None:
            requirements.append((contract.consensus.evidence_ids, secondary, "valuation consensus"))
        return requirements

    if isinstance(contract, DailyTrendHealthContractV2):
        requirements = [
            (contract.daily_series.evidence_ids, secondary, "daily series"),
            *(
                (node.evidence_ids, secondary, "daily moving average")
                for node in contract.moving_averages
            ),
            *(
                (node.evidence_ids, official, "daily fundamental growth")
                for node in contract.fundamental_growth
            ),
            *(
                (node.evidence_ids, secondary, "daily estimate revision")
                for node in contract.estimate_revisions
            ),
        ]
        return requirements

    if isinstance(contract, JuglarCycleStageContractV1):
        requirements = [
            (
                node.evidence_ids,
                (
                    secondary
                    if node.dimension is JuglarCycleDimension.CAPITAL_MARKET_REACTION
                    else official
                ),
                f"Juglar dimension {node.dimension.value}",
            )
            for node in contract.dimension_scores
        ]
        requirements.extend(
            (node.evidence_ids, official, "Juglar counter-evidence")
            for node in contract.counterevidence
        )
        requirements.extend(
            (node.evidence_ids, official, "Juglar migration signal")
            for node in contract.migration_signals
        )
        return requirements

    raise ValueError("unsupported Serenity v2 method contract")
