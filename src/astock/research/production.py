"""Phase 12 research-production routing, scheduling, usage instrumentation, and catalysts."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.repository import ResearchRepository
from astock.research.resource_policy import load_specialist_resource_policy
from astock.research.skills import ResearchSkillService
from astock.schemas.research import (
    ResearchCostClass,
    ResearchSkillKind,
    ResearchSkillRegistry,
    ResearchSkillStatus,
)
from astock.schemas.research_production import (
    CatalystMonitorReport,
    CatalystMonitorRequest,
    CatalystRecord,
    CatalystRecordRequest,
    CatalystStatus,
    KPIComparison,
    OrdinalResearchLevel,
    ProductionRouteMatch,
    ProductionSkillRole,
    ResearchNeedVector,
    ResearchPriorityBucket,
    ResearchPriorityDecision,
    ResearchProductionPolicy,
    ResearchProductionRouteNeedsInfo,
    ResearchProductionRoutePlan,
    SkillCapabilityVector,
    SkillEfficiencyReport,
    SkillEfficiencySummary,
    SkillLifecycleRecommendation,
    SkillUsageEvent,
)

_LEVEL_POINTS = {
    OrdinalResearchLevel.LOW: 0,
    OrdinalResearchLevel.MEDIUM: 1,
    OrdinalResearchLevel.HIGH: 2,
    OrdinalResearchLevel.CRITICAL: 3,
}
_COST_POINTS = {ResearchCostClass.LOW: 1, ResearchCostClass.MEDIUM: 2}


def default_research_production_policy(
    *,
    created_at: datetime | None = None,
    project_root: Path | None = None,
) -> ResearchProductionPolicy:
    root = project_root or Path(__file__).resolve().parents[3]
    path = root / "configs" / "research_production_policy.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid research production policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "research-production-policy-v2":
        raise ValueError("Unsupported research production policy")
    raw_roles = raw.get("role_by_kind")
    if not isinstance(raw_roles, dict) or set(raw_roles) != {
        item.value for item in ResearchSkillKind
    }:
        raise ValueError("research production policy must classify every Skill kind")
    minimum = int(raw["minimum_specialist_budget"])
    default = int(raw["default_specialist_budget"])
    hard_max = int(raw["hard_max_specialists"])
    resource_policy = load_specialist_resource_policy(
        root / "configs" / "specialist_resource_policy.yaml"
    )
    if not (
        resource_policy.minimum_budget
        <= minimum
        <= default
        <= hard_max
        <= resource_policy.maximum_budget
    ):
        raise ValueError("research production specialist budgets exceed active resource policy")
    if raw.get("automatic_skill_modification_allowed") is not False:
        raise ValueError("research production policy cannot modify Skills automatically")
    if raw.get("online_weight_learning_allowed") is not False:
        raise ValueError("research production policy cannot enable online weight learning")
    return ResearchProductionPolicy(
        schema_version=str(raw["schema_version"]),
        policy_id=str(raw["policy_id"]),
        policy_version=str(raw["policy_version"]),
        default_specialist_budget=default,
        minimum_specialist_budget=minimum,
        hard_max_specialists=hard_max,
        role_by_kind={
            ResearchSkillKind(str(kind)): ProductionSkillRole(str(role))
            for kind, role in raw_roles.items()
        },
        created_at=created_at or datetime.now(UTC),
    )


class ResearchProductionService:
    """Route research work by need and cost while preserving legacy Skill artifacts."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        registry: ResearchSkillRegistry,
        policy: ResearchProductionPolicy | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.registry = registry
        self.policy = policy or default_research_production_policy(created_at=registry.created_at)
        self.research = ResearchRepository(state, objects)
        self.skill_service = ResearchSkillService(state, objects, registry)

    def register_policy(self) -> ResearchProductionPolicy:
        registry_execution = self.skill_service.register_registry()
        config_hash = content_hash(self.policy)
        existing = self._policy_row(self.policy.policy_version)
        if existing is not None:
            if str(existing["config_hash"]) != config_hash:
                raise ValueError("research production policy changed without a version bump")
            return ResearchProductionPolicy.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        object_ref = self.objects.put_json(self.policy.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO research_production_policy_index("
                "policy_version,policy_id,object_hash,config_hash,created_at) VALUES(?,?,?,?,?)",
                (
                    self.policy.policy_version,
                    self.policy.policy_id,
                    object_ref.sha256,
                    config_hash,
                    self.policy.created_at.astimezone(UTC).isoformat(),
                ),
            )
        self.state.register_artifact(
            artifact_id=self.policy_artifact_id(self.policy.policy_version),
            artifact_type="ResearchProductionPolicy",
            schema_version=self.policy.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[registry_execution.object_sha256],
        )
        return self.policy

    def capability_vectors(self) -> list[SkillCapabilityVector]:
        policy = self.register_policy()
        capabilities: list[SkillCapabilityVector] = []
        for manifest in sorted(self.registry.skills, key=lambda item: item.skill_id):
            ontology = sorted(
                set(manifest.trigger_tags)
                | set(manifest.industry_tags)
                | set(manifest.event_tags)
                | {manifest.kind.value.lower()}
            )
            capabilities.append(
                SkillCapabilityVector(
                    skill_id=manifest.skill_id,
                    skill_version=manifest.skill_version,
                    kind=manifest.kind,
                    role=policy.role_by_kind[manifest.kind],
                    ontology_terms=ontology,
                    required_inputs=sorted(manifest.required_inputs),
                    required_frequencies=sorted(manifest.required_frequencies),
                    incompatible_skills=sorted(manifest.incompatible_skills),
                    cost_class=manifest.cost_class,
                    created_at=policy.created_at,
                )
            )
        return capabilities

    def schedule(self, need: ResearchNeedVector) -> ResearchPriorityDecision:
        points = {
            "MATERIALITY": _LEVEL_POINTS[need.materiality],
            "NOVELTY": _LEVEL_POINTS[need.novelty],
            "PORTFOLIO_RELEVANCE": _LEVEL_POINTS[need.portfolio_relevance],
            "CATALYST_URGENCY": _LEVEL_POINTS[need.catalyst_urgency],
            "DATA_AVAILABILITY": _LEVEL_POINTS[need.data_availability],
            "SOURCE_DIVERSITY": _LEVEL_POINTS[need.source_diversity],
        }
        cost = _LEVEL_POINTS[need.estimated_research_cost]
        score = max(0, sum(points.values()) - cost)
        if score >= 13:
            bucket = ResearchPriorityBucket.URGENT
        elif score >= 9:
            bucket = ResearchPriorityBucket.HIGH
        elif score >= 5:
            bucket = ResearchPriorityBucket.STANDARD
        else:
            bucket = ResearchPriorityBucket.DEFER
        if (
            bucket is ResearchPriorityBucket.URGENT
            and need.materiality is OrdinalResearchLevel.CRITICAL
            and need.uncertainty in {OrdinalResearchLevel.HIGH, OrdinalResearchLevel.CRITICAL}
        ):
            budget = self.policy.hard_max_specialists
        elif bucket in {ResearchPriorityBucket.HIGH, ResearchPriorityBucket.URGENT}:
            budget = self.policy.default_specialist_budget
        else:
            budget = self.policy.minimum_specialist_budget
        positive = sorted(key for key, value in points.items() if value >= 2)
        limiting = sorted(
            [key for key, value in points.items() if value == 0]
            + (["ESTIMATED_RESEARCH_COST_HIGH"] if cost >= 2 else [])
        )
        return ResearchPriorityDecision(
            priority_bucket=bucket,
            ordinal_score=score,
            positive_factor_codes=positive,
            limiting_factor_codes=limiting,
            specialist_budget=budget,
            created_at=need.created_at,
        )

    def route_for_user(
        self,
        need: ResearchNeedVector,
    ) -> ResearchProductionRoutePlan | ResearchProductionRouteNeedsInfo:
        base_case = self.research.get_base_case(need.base_case_id)
        if base_case is None:
            requested_artifact_id = f"BaseCasePack:{need.base_case_id}"
            registered = self.state.artifact_record(requested_artifact_id)
            if registered is not None:
                raise ValueError("registered BaseCase artifact is missing from the research index")
            latest = self.research.latest_base_case_summary(need.company_id)
            available_id: str | None = None
            available_hash: str | None = None
            if latest is not None:
                candidate_id = str(latest["base_case_id"])
                candidate_hash = str(latest["object_hash"])
                candidate_artifact = self.state.artifact_record(f"BaseCasePack:{candidate_id}")
                if (
                    candidate_artifact is not None
                    and str(candidate_artifact["type"]) == "BaseCasePack"
                    and str(candidate_artifact["object_hash"]) == candidate_hash
                    and self.objects.verify(candidate_hash)
                ):
                    available_id = candidate_id
                    available_hash = candidate_hash
            required_action = (
                "SELECT_REGISTERED_MATCHING_BASE_CASE"
                if available_id is not None
                else "REGISTER_MATCHING_BASE_CASE"
            )
            return ResearchProductionRouteNeedsInfo(
                need_id=need.need_id,
                company_id=need.company_id,
                requested_base_case_id=need.base_case_id,
                requested_artifact_id=requested_artifact_id,
                available_base_case_id=available_id,
                available_base_case_object_hash=available_hash,
                finding_codes=["BASE_CASE_NOT_REGISTERED"],
                required_action_codes=[required_action],
                created_at=need.created_at,
            )
        return self.route(need)

    def route(self, need: ResearchNeedVector) -> ResearchProductionRoutePlan:
        base_case = self.research.get_base_case(need.base_case_id)
        if base_case is None:
            raise ValueError(f"unknown BaseCase for production routing: {need.base_case_id}")
        if base_case.company_id != need.company_id:
            raise ValueError("research need company does not match its BaseCase")
        policy = self.register_policy()
        self._verify_embedding_recall(need)
        priority = self.schedule(need)
        need_terms = (
            set(need.ontology_terms)
            | set(need.thesis_tags)
            | set(need.industry_tags)
            | set(need.event_tags)
            | set(base_case.specialist_tags)
        )
        manifest_by_id = {item.skill_id: item for item in self.registry.skills}
        matches: list[ProductionRouteMatch] = []
        excluded: dict[str, list[str]] = {}
        for capability in self.capability_vectors():
            manifest = manifest_by_id[capability.skill_id]
            reasons: set[str] = set()
            hard_applicable = True
            if manifest.status is not ResearchSkillStatus.ENABLED_CONTRACT:
                hard_applicable = False
                reasons.add("SKILL_NOT_ENABLED")
            missing_inputs = sorted(set(capability.required_inputs) - set(need.available_inputs))
            if missing_inputs:
                hard_applicable = False
                reasons.update(f"MISSING_INPUT:{item}" for item in missing_inputs)
            missing_frequencies = sorted(
                set(capability.required_frequencies) - set(need.available_frequencies)
            )
            if missing_frequencies:
                hard_applicable = False
                reasons.update(f"MISSING_FREQUENCY:{item}" for item in missing_frequencies)
            if need.horizon not in manifest.horizons:
                hard_applicable = False
                reasons.add("HORIZON_NOT_APPLICABLE")
            overlap = len(need_terms & set(capability.ontology_terms))
            embedding = need.embedding_recall_scores.get(capability.skill_id)
            if overlap == 0 and (embedding is None or embedding < 0.50):
                if capability.role is not ProductionSkillRole.COMPOSER:
                    reasons.add("ONTOLOGY_AND_EMBEDDING_RECALL_WEAK")
            semantic_match = overlap > 0 or (embedding is not None and embedding >= 0.50)
            if not hard_applicable:
                applicability = 0
            elif semantic_match or capability.role is ProductionSkillRole.COMPOSER:
                applicability = 4
            else:
                applicability = 2
            incremental = min(2, overlap) + int(embedding is not None and embedding >= 0.50)
            materiality = _LEVEL_POINTS[need.materiality]
            uncertainty = _LEVEL_POINTS[need.uncertainty]
            marginal_cost = _COST_POINTS[capability.cost_class]
            route_score = (
                (materiality + 1)
                * (uncertainty + 1)
                * (applicability + 1)
                * (incremental + 1)
                / marginal_cost
                if hard_applicable
                else 0.0
            )
            match = ProductionRouteMatch(
                skill_id=capability.skill_id,
                skill_version=capability.skill_version,
                kind=capability.kind,
                role=capability.role,
                hard_applicable=hard_applicable,
                ontology_overlap_count=overlap,
                embedding_recall_score=embedding,
                materiality_points=materiality,
                uncertainty_points=uncertainty,
                applicability_points=applicability,
                incremental_evidence_points=incremental,
                marginal_cost_points=marginal_cost,
                route_score=route_score,
                reason_codes=sorted(reasons or {"APPLICABLE"}),
                created_at=need.created_at,
            )
            if not hard_applicable:
                excluded[capability.skill_id] = sorted(reasons)
            else:
                matches.append(match)

        fundamental_candidates = sorted(
            [
                item
                for item in matches
                if item.role is ProductionSkillRole.FUNDAMENTAL_SPECIALIST
                and (item.ontology_overlap_count > 0 or (item.embedding_recall_score or 0) >= 0.50)
            ],
            key=lambda item: (
                -item.route_score,
                -(item.embedding_recall_score or 0),
                item.skill_id,
            ),
        )
        selected: list[ProductionRouteMatch] = []
        selected_ids: set[str] = set()
        selected_kinds: set[ResearchSkillKind] = set()
        for match in fundamental_candidates:
            manifest = manifest_by_id[match.skill_id]
            conflicts = set(manifest.incompatible_skills) & selected_ids
            reverse_conflict = {
                item.skill_id
                for item in self.registry.skills
                if item.skill_id in selected_ids and match.skill_id in item.incompatible_skills
            }
            if conflicts or reverse_conflict or match.kind in selected_kinds:
                excluded[match.skill_id] = ["DIVERSITY_OR_CONFLICT_FILTER"]
                continue
            if len(selected) >= priority.specialist_budget:
                excluded[match.skill_id] = ["DYNAMIC_SPECIALIST_BUDGET_REACHED"]
                continue
            if len(selected) >= policy.hard_max_specialists:
                excluded[match.skill_id] = ["HARD_MAX_FOUR_SPECIALISTS"]
                continue
            selected.append(match)
            selected_ids.add(match.skill_id)
            selected_kinds.add(match.kind)

        def support(role: ProductionSkillRole) -> list[ProductionRouteMatch]:
            return sorted(
                [
                    item
                    for item in matches
                    if item.role is role
                    and (
                        role is ProductionSkillRole.COMPOSER
                        or item.ontology_overlap_count > 0
                        or (item.embedding_recall_score or 0) >= 0.50
                    )
                ],
                key=lambda item: (-item.route_score, item.skill_id),
            )

        findings: set[str] = set()
        if not need.embedding_recall_scores:
            findings.add("EMBEDDING_RECALL_NOT_AVAILABLE")
        if not selected:
            findings.add("NO_FUNDAMENTAL_SPECIALIST_SELECTED")
        if priority.specialist_budget == policy.hard_max_specialists:
            findings.add("HARD_MAX_FOUR_BUDGET_ACTIVATED")
        input_hash = content_hash(
            {
                "need": need,
                "base_case": content_hash(base_case),
                "policy": content_hash(policy),
                "registry": content_hash(self.registry),
            }
        )
        route_id = f"research-production-route:{content_hash({'input_hash': input_hash})}"
        plan = ResearchProductionRoutePlan(
            route_plan_id=route_id,
            policy_version=policy.policy_version,
            registry_version=self.registry.registry_version,
            need_id=need.need_id,
            base_case_id=need.base_case_id,
            priority=priority,
            selected_fundamental_specialists=selected,
            shared_hypothesis_modules=support(ProductionSkillRole.SHARED_HYPOTHESIS),
            canonical_valuation_modules=support(ProductionSkillRole.CANONICAL_VALUATION),
            market_trade_context_modules=support(ProductionSkillRole.MARKET_TRADE_CONTEXT),
            composers=support(ProductionSkillRole.COMPOSER),
            excluded=dict(sorted(excluded.items())),
            hard_max_specialists=policy.hard_max_specialists,
            embedding_recall_used=bool(need.embedding_recall_scores),
            finding_codes=sorted(findings),
            created_at=need.created_at,
        )
        object_ref = self.objects.put_json(plan.model_dump(mode="json"))
        existing = self._route_row(plan.route_plan_id)
        if existing is not None:
            if str(existing["input_hash"]) != input_hash:
                raise ValueError("research production route identity collision")
            return ResearchProductionRoutePlan.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO research_production_route_index("
                "route_plan_id,policy_version,registry_version,need_id,base_case_id,company_id,"
                "priority_bucket,specialist_budget,specialist_count,object_hash,input_hash,"
                "created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.route_plan_id,
                    plan.policy_version,
                    plan.registry_version,
                    plan.need_id,
                    plan.base_case_id,
                    need.company_id,
                    plan.priority.priority_bucket.value,
                    plan.priority.specialist_budget,
                    len(plan.selected_fundamental_specialists),
                    object_ref.sha256,
                    input_hash,
                    plan.created_at.astimezone(UTC).isoformat(),
                ),
            )
        policy_row = self._policy_row(policy.policy_version)
        assert policy_row is not None
        registry_row = self.research.skill_registry_summary(self.registry.registry_version)
        assert registry_row is not None
        base_hash = self.research.base_case_object_hash(need.base_case_id)
        assert base_hash is not None
        input_hashes = [str(policy_row["object_hash"]), str(registry_row["object_hash"]), base_hash]
        if need.embedding_recall_object_hash:
            input_hashes.append(need.embedding_recall_object_hash)
        self.state.register_artifact(
            artifact_id=plan.route_plan_id,
            artifact_type="ResearchProductionRoutePlan",
            schema_version=plan.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted(input_hashes),
        )
        return plan

    def record_usage(self, event: SkillUsageEvent) -> SkillUsageEvent:
        route = self._load_route(event.route_plan_id)
        selected_matches = [
            *route.selected_fundamental_specialists,
            *route.shared_hypothesis_modules,
            *route.canonical_valuation_modules,
            *route.market_trade_context_modules,
            *route.composers,
        ]
        selected = {(item.skill_id, item.skill_version) for item in selected_matches}
        if (event.skill_id, event.skill_version) not in selected:
            raise ValueError("Skill usage event is outside the frozen production route")
        route_row = self._route_row(event.route_plan_id)
        assert route_row is not None
        if str(route_row["company_id"]) != event.company_id:
            raise ValueError("Skill usage company does not match the production route")
        if event.prospective_lift_artifact_id:
            self._verify_artifact_pair(
                event.prospective_lift_artifact_id,
                event.prospective_lift_object_hash or "",
            )
        semantic = event.model_dump(mode="json", exclude={"usage_event_id", "created_at"})
        expected_id = f"skill-usage:{content_hash(semantic)}"
        if event.usage_event_id != expected_id:
            raise ValueError(f"Skill usage id must equal deterministic identity {expected_id}")
        event_hash = content_hash(event)
        existing = self._usage_row(event.usage_event_id)
        if existing is not None:
            if str(existing["event_hash"]) != event_hash:
                raise ValueError("Skill usage event identity collision")
            return SkillUsageEvent.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        object_ref = self.objects.put_json(event.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO skill_usage_event_index("
                "usage_event_id,route_plan_id,company_id,skill_id,skill_version,corrected_claim,"
                "found_gap,changed_driver,provided_falsifier,changed_ic_state,prospective_lift,"
                "token_cost,object_hash,event_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.usage_event_id,
                    event.route_plan_id,
                    event.company_id,
                    event.skill_id,
                    event.skill_version,
                    int(event.corrected_claim),
                    int(event.found_gap),
                    int(event.changed_driver),
                    int(event.provided_falsifier),
                    int(event.changed_investment_committee_state),
                    event.prospective_lift,
                    event.token_cost,
                    object_ref.sha256,
                    event_hash,
                    event.created_at.astimezone(UTC).isoformat(),
                ),
            )
        inputs = [str(route_row["object_hash"])]
        if event.prospective_lift_object_hash:
            inputs.append(event.prospective_lift_object_hash)
        self.state.register_artifact(
            artifact_id=event.usage_event_id,
            artifact_type="SkillUsageEvent",
            schema_version=event.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted(inputs),
        )
        return event

    def efficiency_report(self) -> SkillEfficiencyReport:
        policy = self.register_policy()
        events = self._usage_events()
        grouped: dict[tuple[str, str], list[SkillUsageEvent]] = defaultdict(list)
        for event in events:
            grouped[(event.skill_id, event.skill_version)].append(event)
        summaries: list[SkillEfficiencySummary] = []
        for manifest in sorted(self.registry.skills, key=lambda item: item.skill_id):
            rows = grouped.get((manifest.skill_id, manifest.skill_version), [])
            lifts = [item.prospective_lift for item in rows if item.prospective_lift is not None]
            duplicates = sorted({value for item in rows for value in item.near_duplicate_skill_ids})
            conflicts = sorted({value for item in rows for value in item.conflict_skill_ids})
            useful_count = sum(
                int(item.corrected_claim)
                + int(item.found_gap)
                + int(item.changed_driver)
                + int(item.provided_falsifier)
                + int(item.changed_investment_committee_state)
                for item in rows
            )
            if not lifts:
                recommendation = SkillLifecycleRecommendation.INSUFFICIENT_PROSPECTIVE_EVIDENCE
            elif (duplicates or conflicts) and len(rows) >= 3:
                recommendation = SkillLifecycleRecommendation.REVIEW_DUPLICATE_OR_CONFLICT
            elif useful_count == 0 and len(rows) >= 3:
                recommendation = SkillLifecycleRecommendation.REVIEW_LOW_VALUE
            else:
                recommendation = SkillLifecycleRecommendation.KEEP
            summaries.append(
                SkillEfficiencySummary(
                    skill_id=manifest.skill_id,
                    skill_version=manifest.skill_version,
                    usage_count=len(rows),
                    corrected_claim_count=sum(item.corrected_claim for item in rows),
                    found_gap_count=sum(item.found_gap for item in rows),
                    changed_driver_count=sum(item.changed_driver for item in rows),
                    provided_falsifier_count=sum(item.provided_falsifier for item in rows),
                    changed_ic_state_count=sum(
                        item.changed_investment_committee_state for item in rows
                    ),
                    total_token_cost=sum(item.token_cost for item in rows),
                    mean_prospective_lift=(sum(lifts) / len(lifts) if lifts else None),
                    near_duplicate_skill_ids=duplicates,
                    conflict_skill_ids=conflicts,
                    recommendation=recommendation,
                    created_at=datetime.now(UTC),
                )
            )
        report = SkillEfficiencyReport(
            report_id="skill-efficiency:"
            + content_hash(
                {
                    "policy": policy.policy_version,
                    "usage": sorted(content_hash(item) for item in events),
                }
            ),
            policy_version=policy.policy_version,
            summaries=summaries,
            created_at=datetime.now(UTC),
        )
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=report.report_id,
            artifact_type="SkillEfficiencyReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted({str(row["object_hash"]) for row in self._usage_rows()}),
        )
        return report

    def register_catalyst(self, request: CatalystRecordRequest) -> CatalystRecord:
        self._verify_artifact_sets(request.source_artifact_ids, request.source_object_hashes)
        semantic = request.model_dump(mode="json", exclude={"created_at"})
        catalyst_hash = content_hash(semantic)
        catalyst_id = f"catalyst:{catalyst_hash}"
        existing = self._catalyst_row(catalyst_id)
        if existing is not None:
            if str(existing["catalyst_hash"]) != catalyst_hash:
                raise ValueError("catalyst identity collision")
            return CatalystRecord.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        record = CatalystRecord(
            **request.model_dump(),
            catalyst_id=catalyst_id,
            catalyst_sha256=catalyst_hash,
        )
        object_ref = self.objects.put_json(record.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO catalyst_registry_index("
                "catalyst_id,company_id,thesis_id,catalyst_type,expected_from,expected_to,status,"
                "object_hash,catalyst_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record.catalyst_id,
                    record.company_id,
                    record.thesis_id,
                    record.catalyst_type,
                    record.expected_from.astimezone(UTC).isoformat(),
                    record.expected_to.astimezone(UTC).isoformat(),
                    record.status.value,
                    object_ref.sha256,
                    record.catalyst_sha256,
                    record.created_at.astimezone(UTC).isoformat(),
                ),
            )
        self.state.register_artifact(
            artifact_id=record.catalyst_id,
            artifact_type="CatalystRecord",
            schema_version=record.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=record.source_object_hashes,
        )
        return record

    def monitor_catalyst(self, request: CatalystMonitorRequest) -> CatalystMonitorReport:
        catalyst = self._load_catalyst(request.catalyst_id)
        for observation in request.observations:
            self._verify_artifact_pair(
                observation.source_artifact_id, observation.source_object_hash
            )
        prior_status = self._latest_catalyst_status(catalyst.catalyst_id) or catalyst.status
        values = {item.kpi_id: item.value for item in request.observations}
        triggered = sorted(
            rule.kpi_id
            for rule in catalyst.kpi_rules
            if rule.kpi_id in values
            and self._compare(values[rule.kpi_id], rule.comparison, rule.threshold)
        )
        missing = sorted(rule.kpi_id for rule in catalyst.kpi_rules if rule.kpi_id not in values)
        if request.observed_status is not None:
            evaluated = request.observed_status
        elif catalyst.kpi_rules and len(triggered) == len(catalyst.kpi_rules):
            evaluated = CatalystStatus.CONFIRMED
        elif request.as_of > catalyst.expected_to and (
            missing or len(triggered) < len(catalyst.kpi_rules)
        ):
            evaluated = CatalystStatus.MISSED
        else:
            evaluated = prior_status
        should_rerun = evaluated is not prior_status or bool(triggered)
        rerun_modules = catalyst.affected_modules if should_rerun else []
        input_hash = content_hash(
            {
                "catalyst": catalyst.catalyst_sha256,
                "as_of": request.as_of.isoformat(),
                "observations": [item.model_dump(mode="json") for item in request.observations],
                "observed_status": request.observed_status,
            }
        )
        monitor_id = "catalyst-monitor:" + content_hash(
            {"catalyst": catalyst.catalyst_id, "input": input_hash}
        )
        existing = self._monitor_row(monitor_id)
        if existing is not None:
            return CatalystMonitorReport.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        report = CatalystMonitorReport(
            monitor_id=monitor_id,
            catalyst_id=catalyst.catalyst_id,
            as_of=request.as_of,
            prior_status=prior_status,
            evaluated_status=evaluated,
            triggered_kpi_ids=triggered,
            missing_kpi_ids=missing,
            rerun_modules=rerun_modules,
            created_at=request.as_of,
        )
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO catalyst_monitor_index("
                "monitor_id,catalyst_id,as_of,prior_status,evaluated_status,rerun_module_count,"
                "object_hash,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    report.monitor_id,
                    report.catalyst_id,
                    report.as_of.astimezone(UTC).isoformat(),
                    report.prior_status.value,
                    report.evaluated_status.value,
                    len(report.rerun_modules),
                    object_ref.sha256,
                    input_hash,
                    report.created_at.astimezone(UTC).isoformat(),
                ),
            )
        catalyst_row = self._catalyst_row(catalyst.catalyst_id)
        assert catalyst_row is not None
        inputs = [
            str(catalyst_row["object_hash"]),
            *[item.source_object_hash for item in request.observations],
        ]
        self.state.register_artifact(
            artifact_id=report.monitor_id,
            artifact_type="CatalystMonitorReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted(set(inputs)),
        )
        return report

    def audit(self, artifact_id: str) -> dict[str, Any]:
        record = self.state.artifact_record(artifact_id)
        allowed = {
            "ResearchProductionPolicy",
            "ResearchProductionRoutePlan",
            "SkillUsageEvent",
            "SkillEfficiencyReport",
            "CatalystRecord",
            "CatalystMonitorReport",
        }
        if record is None or str(record["type"]) not in allowed:
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_ARTIFACT"],
            }
        findings: set[str] = set()
        if not self.objects.verify(str(record["object_hash"])):
            findings.add("OBJECT_UNAVAILABLE")
        for input_hash in record["input_hashes"]:
            if len(str(input_hash)) == 64:
                with closing(self.state.connect()) as connection:
                    known = connection.execute(
                        "SELECT 1 FROM artifact_registry WHERE object_hash=? LIMIT 1",
                        (str(input_hash),),
                    ).fetchone()
                if known is not None and not self.objects.verify(str(input_hash)):
                    findings.add("INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "finding_codes": sorted(findings),
            "automatic_skill_modification_allowed": False,
            "online_weight_learning_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    @staticmethod
    def policy_artifact_id(policy_version: str) -> str:
        return f"ResearchProductionPolicy:{policy_version}"

    def _verify_embedding_recall(self, need: ResearchNeedVector) -> None:
        if need.embedding_recall_artifact_id:
            self._verify_artifact_pair(
                need.embedding_recall_artifact_id,
                need.embedding_recall_object_hash or "",
            )
        unknown = sorted(
            set(need.embedding_recall_scores) - {item.skill_id for item in self.registry.skills}
        )
        if unknown:
            raise ValueError("embedding recall references unknown Skills")

    def _verify_artifact_pair(self, artifact_id: str, object_hash: str) -> None:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["object_hash"]) != object_hash:
            raise ValueError(f"artifact provenance mismatch: {artifact_id}")
        if not self.objects.verify(object_hash):
            raise ValueError(f"artifact object unavailable: {artifact_id}")

    def _verify_artifact_sets(self, artifact_ids: list[str], object_hashes: list[str]) -> None:
        actual_hashes: set[str] = set()
        for artifact_id in artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(f"unregistered catalyst source artifact: {artifact_id}")
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                raise ValueError(f"catalyst source object unavailable: {artifact_id}")
            actual_hashes.add(object_hash)
        if actual_hashes != set(object_hashes):
            raise ValueError("catalyst source artifact/hash provenance does not reconcile")

    @staticmethod
    def _compare(value: float, comparison: KPIComparison, threshold: float) -> bool:
        if comparison is KPIComparison.GE:
            return value >= threshold
        if comparison is KPIComparison.LE:
            return value <= threshold
        if comparison is KPIComparison.GT:
            return value > threshold
        if comparison is KPIComparison.LT:
            return value < threshold
        return value == threshold

    def _load_route(self, route_plan_id: str) -> ResearchProductionRoutePlan:
        row = self._route_row(route_plan_id)
        if row is None:
            raise ValueError(f"unknown research production route: {route_plan_id}")
        return ResearchProductionRoutePlan.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )

    def _load_catalyst(self, catalyst_id: str) -> CatalystRecord:
        row = self._catalyst_row(catalyst_id)
        if row is None:
            raise ValueError(f"unknown catalyst: {catalyst_id}")
        return CatalystRecord.model_validate_json(self.objects.get_bytes(str(row["object_hash"])))

    def _latest_catalyst_status(self, catalyst_id: str) -> CatalystStatus | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT evaluated_status FROM catalyst_monitor_index WHERE catalyst_id=? "
                "ORDER BY as_of DESC,monitor_id DESC LIMIT 1",
                (catalyst_id,),
            ).fetchone()
        return CatalystStatus(str(row["evaluated_status"])) if row is not None else None

    def _policy_row(self, policy_version: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM research_production_policy_index WHERE policy_version=?",
            (policy_version,),
        )

    def _route_row(self, route_plan_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM research_production_route_index WHERE route_plan_id=?",
            (route_plan_id,),
        )

    def _usage_row(self, usage_event_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM skill_usage_event_index WHERE usage_event_id=?",
            (usage_event_id,),
        )

    def _catalyst_row(self, catalyst_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM catalyst_registry_index WHERE catalyst_id=?",
            (catalyst_id,),
        )

    def _monitor_row(self, monitor_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM catalyst_monitor_index WHERE monitor_id=?",
            (monitor_id,),
        )

    def _usage_rows(self) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM skill_usage_event_index "
                "ORDER BY skill_id,skill_version,created_at,usage_event_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def _usage_events(self) -> list[SkillUsageEvent]:
        return [
            SkillUsageEvent.model_validate_json(self.objects.get_bytes(str(row["object_hash"])))
            for row in self._usage_rows()
        ]

    def _one(self, sql: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None


__all__ = [
    "ResearchProductionService",
    "default_research_production_policy",
]
