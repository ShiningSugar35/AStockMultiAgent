from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    CapabilityNode,
    CapabilityRequirement,
    CapabilityRunRecord,
    CapabilityRunStatus,
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


@dataclass(frozen=True)
class CapabilityDependencyContext:
    capability_id: str
    plan_id: str
    dependency_artifacts: Mapping[str, tuple[str, ...]]
    completed_artifacts: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class DependencyAwareCapabilityHandler:
    callback: Callable[
        [
            InvestorRequestEnvelope,
            InvestorSessionPreflightReceipt,
            CapabilityDependencyContext,
        ],
        Any,
    ]


CapabilityHandler = (
    Callable[[InvestorRequestEnvelope, InvestorSessionPreflightReceipt], Any]
    | DependencyAwareCapabilityHandler
)


@dataclass(frozen=True)
class CapabilityExecutionResult:
    artifact_ids: tuple[str, ...] = ()
    source_revision: str | None = None
    reused: bool = False
    degraded_reason: str | None = None


_BASE_NODES: dict[str, CapabilityNode] = {
    "REQUEST_TIME": CapabilityNode(
        capability_id="REQUEST_TIME",
        requirement=CapabilityRequirement.REQUIRED,
        output_schema="ResolvedRequestTime",
        parallel_group="preflight",
    ),
    "ENTITY_IDENTITY": CapabilityNode(
        capability_id="ENTITY_IDENTITY",
        requirement=CapabilityRequirement.REQUIRED,
        dependencies=("REQUEST_TIME",),
        output_schema="ResolvedEntitySet",
        parallel_group="preflight",
    ),
    "SESSION_PREFLIGHT": CapabilityNode(
        capability_id="SESSION_PREFLIGHT",
        requirement=CapabilityRequirement.REQUIRED,
        dependencies=("REQUEST_TIME",),
        output_schema="InvestorSessionPreflightReceipt",
        parallel_group="preflight",
    ),
    "CURRENT_MARKET": CapabilityNode(
        capability_id="CURRENT_MARKET",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("ENTITY_IDENTITY",),
        conditional_reason="current price, liquidity or trading status is needed",
        output_schema="MarketPriceAnchor",
        parallel_group="research-input",
    ),
    "MARKET_REGIME": CapabilityNode(
        capability_id="MARKET_REGIME",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("SESSION_PREFLIGHT",),
        conditional_reason="investment judgment or portfolio risk is requested",
        output_schema="MarketRegimeSnapshotV2",
        parallel_group="research-input",
    ),
    "INDUSTRY": CapabilityNode(
        capability_id="INDUSTRY",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("ENTITY_IDENTITY",),
        conditional_reason="company/industry economics or transmission is material",
        output_schema="IndustryProfile",
        parallel_group="company-foundation",
    ),
    "COMPANY_RESEARCH": CapabilityNode(
        capability_id="COMPANY_RESEARCH",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("ENTITY_IDENTITY",),
        conditional_reason="a security-specific investment conclusion is requested",
        output_schema="InstitutionalDecisionContext",
        parallel_group="company-foundation",
    ),
    "FINANCIAL_INTEGRITY": CapabilityNode(
        capability_id="FINANCIAL_INTEGRITY",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("COMPANY_RESEARCH",),
        conditional_reason="formal company judgment requires financial integrity",
        output_schema="FinancialIntegrityEvidencePack",
        parallel_group="company-risk",
    ),
    "GOVERNANCE": CapabilityNode(
        capability_id="GOVERNANCE",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("COMPANY_RESEARCH",),
        conditional_reason="formal company judgment requires governance review",
        output_schema="ResearchRoleOutput",
        parallel_group="company-risk",
    ),
    "EVENT_RESEARCH": CapabilityNode(
        capability_id="EVENT_RESEARCH",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("ENTITY_IDENTITY",),
        conditional_reason="new disclosure, news, policy or catalyst is relevant",
        output_schema="ResearchRoleOutput",
        parallel_group="company-risk",
    ),
    "FORECAST_VALUATION": CapabilityNode(
        capability_id="FORECAST_VALUATION",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("COMPANY_RESEARCH", "FINANCIAL_INTEGRITY", "CURRENT_MARKET"),
        conditional_reason="buy/sell/value decision needs a forecast and valuation",
        output_schema="ValuationPack",
        parallel_group="decision",
    ),
    "RED_TEAM": CapabilityNode(
        capability_id="RED_TEAM",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("FORECAST_VALUATION", "GOVERNANCE", "EVENT_RESEARCH"),
        conditional_reason="formal investment conclusion needs independent challenge",
        output_schema="ResearchRoleOutput",
        parallel_group="decision",
    ),
    "COMMITTEE": CapabilityNode(
        capability_id="COMMITTEE",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("RED_TEAM",),
        conditional_reason="formal recommendation or trade plan is requested",
        output_schema="ClassifiedTradeProtocol",
        parallel_group="decision",
    ),
    "PORTFOLIO": CapabilityNode(
        capability_id="PORTFOLIO",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("SESSION_PREFLIGHT", "MARKET_REGIME"),
        conditional_reason="holdings, allocation or planned purchase is material",
        output_schema="PortfolioAnalysisReport",
        parallel_group="portfolio",
    ),
    "HOLDING_REVIEW": CapabilityNode(
        capability_id="HOLDING_REVIEW",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("PORTFOLIO", "FORECAST_VALUATION"),
        conditional_reason="an existing holding needs an action",
        output_schema="HoldingReviewPack",
        parallel_group="portfolio",
    ),
    "FULL_MARKET": CapabilityNode(
        capability_id="FULL_MARKET",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("MARKET_REGIME",),
        conditional_reason="full-market discovery or recommendation is requested",
        output_schema="FullResearchInputReadinessReport",
        parallel_group="discovery",
    ),
    "FULL_RESEARCH_GATE": CapabilityNode(
        capability_id="FULL_RESEARCH_GATE",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=(
            "CURRENT_MARKET",
            "MARKET_REGIME",
            "FULL_MARKET",
            "INDUSTRY",
            "COMPANY_RESEARCH",
            "FINANCIAL_INTEGRITY",
            "GOVERNANCE",
            "EVENT_RESEARCH",
            "FORECAST_VALUATION",
            "RED_TEAM",
            "COMMITTEE",
            "PORTFOLIO",
        ),
        conditional_reason=(
            "a direct investment recommendation requires a sealed full-research receipt"
        ),
        output_schema="RecommendationResearchReceipt",
        parallel_group="finalize",
    ),
    "SUBJECT_REGISTRY": CapabilityNode(
        capability_id="SUBJECT_REGISTRY",
        requirement=CapabilityRequirement.REQUIRED,
        dependencies=("ENTITY_IDENTITY",),
        output_schema="ResearchSubjectEventSet",
        side_effect=SideEffectClass.META,
        parallel_group="finalize",
    ),
    "EXTERNAL_ACCOUNT": CapabilityNode(
        capability_id="EXTERNAL_ACCOUNT",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("SESSION_PREFLIGHT", "ENTITY_IDENTITY"),
        conditional_reason="an actual account fact is being recorded or reviewed",
        output_schema="ExternalAccountEvent",
        side_effect=SideEffectClass.EA_WRITE,
        parallel_group="account",
    ),
    "ETF": CapabilityNode(
        capability_id="ETF",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("ENTITY_IDENTITY", "CURRENT_MARKET"),
        conditional_reason="ETF selection or hedge evaluation is requested",
        output_schema="ETFResearchMetrics",
        parallel_group="research-input",
    ),
    "PAPER": CapabilityNode(
        capability_id="PAPER",
        requirement=CapabilityRequirement.CONDITIONAL,
        dependencies=("SESSION_PREFLIGHT", "ENTITY_IDENTITY"),
        conditional_reason="paper order preparation, confirmation or replay is requested",
        output_schema="PaperOperationResult",
        side_effect=SideEffectClass.PT_PREPARE,
        parallel_group="account",
    ),
    "RESPONSE_GATEWAY": CapabilityNode(
        capability_id="RESPONSE_GATEWAY",
        requirement=CapabilityRequirement.REQUIRED,
        dependencies=("SESSION_PREFLIGHT", "SUBJECT_REGISTRY"),
        output_schema="InvestorAnswer",
        parallel_group="finalize",
    ),
}


_INTENT_REQUIREMENTS: dict[RequestIntent, set[str]] = {
    RequestIntent.RESEARCH: {
        "CURRENT_MARKET",
        "MARKET_REGIME",
        "INDUSTRY",
        "COMPANY_RESEARCH",
        "FINANCIAL_INTEGRITY",
        "GOVERNANCE",
        "EVENT_RESEARCH",
        "FORECAST_VALUATION",
        "RED_TEAM",
    },
    RequestIntent.FULL_RESEARCH_RECOMMENDATION: {
        "CURRENT_MARKET",
        "MARKET_REGIME",
        "FULL_MARKET",
        "COMPANY_RESEARCH",
        "INDUSTRY",
        "FINANCIAL_INTEGRITY",
        "GOVERNANCE",
        "EVENT_RESEARCH",
        "FORECAST_VALUATION",
        "RED_TEAM",
        "COMMITTEE",
        "PORTFOLIO",
        "FULL_RESEARCH_GATE",
    },
    RequestIntent.ACCOUNT_FACT_WRITE: {"EXTERNAL_ACCOUNT"},
    RequestIntent.PAPER_PREPARE: {"PAPER", "CURRENT_MARKET"},
    RequestIntent.PAPER_CONFIRM: {"PAPER", "CURRENT_MARKET"},
    RequestIntent.PAPER_STATUS: {"PAPER"},
    RequestIntent.MONITOR: {"CURRENT_MARKET", "MARKET_REGIME", "EVENT_RESEARCH"},
}


def validate_request_permissions(request: InvestorRequestEnvelope) -> None:
    """Intent cannot silently upgrade the explicitly declared side-effect lane."""
    if request.decision_time is not None and request.side_effect not in {
        SideEffectClass.READ,
        SideEffectClass.META,
        SideEffectClass.NONE,
    }:
        raise ValueError("current decision freezes are read-only and cannot replay economic writes")
    allowed = {
        RequestIntent.FULL_RESEARCH_RECOMMENDATION: {
            SideEffectClass.READ,
            SideEffectClass.META,
            SideEffectClass.NONE,
            SideEffectClass.EA_PROVISIONAL,
        },
        RequestIntent.ACCOUNT_FACT_WRITE: {
            SideEffectClass.EA_WRITE,
            SideEffectClass.EA_PROVISIONAL,
        },
        RequestIntent.PAPER_PREPARE: {SideEffectClass.PT_PREPARE},
        RequestIntent.PAPER_CONFIRM: {SideEffectClass.PT_CONFIRM},
        RequestIntent.PAPER_STATUS: {SideEffectClass.READ, SideEffectClass.PT_REPLAY},
    }.get(
        request.normalized_intent,
        {SideEffectClass.READ, SideEffectClass.META, SideEffectClass.NONE},
    )
    if request.side_effect not in allowed:
        raise ValueError("request intent and side-effect permission are incompatible")


class CapabilityPlanner:
    def __init__(
        self,
        *,
        policy_version: str = "full-research-recommendation-v1",
        store: InvestorOrchestrationStore | None = None,
    ) -> None:
        self.policy_version = policy_version
        self.store = store

    def plan(
        self,
        request: InvestorRequestEnvelope,
        preflight: InvestorSessionPreflightReceipt,
        *,
        scenario_requirements: Mapping[str, CapabilityRequirement] | None = None,
    ) -> CapabilityExecutionPlan:
        if request.request_id != preflight.request_id:
            raise ValueError("request and preflight identities differ")
        validate_request_permissions(request)
        required = set(_INTENT_REQUIREMENTS[request.normalized_intent])
        overrides = dict(scenario_requirements or {})
        unknown = set(overrides) - set(_BASE_NODES)
        if unknown:
            raise ValueError(f"unknown capability overrides: {sorted(unknown)}")
        immutable_required = required | {
            key
            for key, node in _BASE_NODES.items()
            if node.requirement is CapabilityRequirement.REQUIRED
        }
        for key in immutable_required & set(overrides):
            if overrides[key] is not CapabilityRequirement.REQUIRED:
                raise ValueError(f"scenario cannot downgrade mandatory capability: {key}")
        nodes: dict[str, CapabilityNode] = {}
        for capability_id, template in _BASE_NODES.items():
            if capability_id in overrides:
                requirement = overrides[capability_id]
            elif (
                capability_id in required or template.requirement is CapabilityRequirement.REQUIRED
            ):
                requirement = CapabilityRequirement.REQUIRED
            else:
                requirement = CapabilityRequirement.OPTIONAL
            nodes[capability_id] = template.model_copy(update={"requirement": requirement})

        holding_context_required = bool(request.metadata.get("full_research_holding_context"))
        if holding_context_required:
            nodes["HOLDING_REVIEW"] = nodes["HOLDING_REVIEW"].model_copy(
                update={"requirement": CapabilityRequirement.REQUIRED}
            )
        if nodes["HOLDING_REVIEW"].requirement is CapabilityRequirement.REQUIRED:
            nodes["FULL_RESEARCH_GATE"] = nodes["FULL_RESEARCH_GATE"].model_copy(
                update={
                    "dependencies": tuple(
                        dict.fromkeys((*nodes["FULL_RESEARCH_GATE"].dependencies, "HOLDING_REVIEW"))
                    )
                }
            )
        full_research_account_required = (
            request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
            and nodes["EXTERNAL_ACCOUNT"].requirement is CapabilityRequirement.REQUIRED
        )
        if full_research_account_required:
            account_side_effect = (
                SideEffectClass.EA_PROVISIONAL
                if request.side_effect is SideEffectClass.EA_PROVISIONAL
                else SideEffectClass.READ
            )
            account_update: dict[str, object] = {"side_effect": account_side_effect}
            if account_side_effect is SideEffectClass.EA_PROVISIONAL:
                account_update["output_schema"] = "ExternalAccountOperationReceipt"
            nodes["EXTERNAL_ACCOUNT"] = nodes["EXTERNAL_ACCOUNT"].model_copy(update=account_update)
        if request.side_effect in {
            SideEffectClass.READ,
            SideEffectClass.META,
            SideEffectClass.NONE,
        }:
            for capability_id in ("EXTERNAL_ACCOUNT", "PAPER"):
                if capability_id == "EXTERNAL_ACCOUNT" and full_research_account_required:
                    continue
                if capability_id not in required:
                    nodes[capability_id] = nodes[capability_id].model_copy(
                        update={"requirement": CapabilityRequirement.PROHIBITED}
                    )
        if request.normalized_intent is RequestIntent.ACCOUNT_FACT_WRITE:
            nodes["MARKET_REGIME"] = nodes["MARKET_REGIME"].model_copy(
                update={"requirement": CapabilityRequirement.OPTIONAL}
            )
            nodes["EXTERNAL_ACCOUNT"] = nodes["EXTERNAL_ACCOUNT"].model_copy(
                update={
                    "side_effect": request.side_effect,
                    "output_schema": "ExternalAccountOperationReceipt",
                }
            )
            nodes["PAPER"] = nodes["PAPER"].model_copy(
                update={"requirement": CapabilityRequirement.PROHIBITED}
            )
        if request.normalized_intent in {
            RequestIntent.PAPER_PREPARE,
            RequestIntent.PAPER_CONFIRM,
            RequestIntent.PAPER_STATUS,
        }:
            paper_side_effect = {
                RequestIntent.PAPER_PREPARE: SideEffectClass.PT_PREPARE,
                RequestIntent.PAPER_CONFIRM: SideEffectClass.PT_CONFIRM,
                RequestIntent.PAPER_STATUS: request.side_effect,
            }[request.normalized_intent]
            output_schema = {
                RequestIntent.PAPER_PREPARE: "PaperPreparationReceipt",
                RequestIntent.PAPER_CONFIRM: "PaperOperationReport",
                RequestIntent.PAPER_STATUS: (
                    "ReplayExecutionReport"
                    if paper_side_effect is SideEffectClass.PT_REPLAY
                    else "PortfolioNAV"
                ),
            }[request.normalized_intent]
            nodes["PAPER"] = nodes["PAPER"].model_copy(
                update={"side_effect": paper_side_effect, "output_schema": output_schema}
            )
        if (
            not preflight.regime.available
            and nodes["MARKET_REGIME"].requirement is CapabilityRequirement.REQUIRED
        ):
            nodes["MARKET_REGIME"] = nodes["MARKET_REGIME"].model_copy(
                update={
                    "conditional_reason": (
                        "required but unavailable; final conclusion must degrade"
                    )
                }
            )

        selected = self._promote_required_dependencies(self._dependency_closure(nodes))
        final_dependencies = tuple(
            sorted(
                capability_id
                for capability_id, node in selected.items()
                if capability_id != "RESPONSE_GATEWAY"
                and node.requirement is not CapabilityRequirement.PROHIBITED
            )
        )
        if "RESPONSE_GATEWAY" in selected:
            selected["RESPONSE_GATEWAY"] = selected["RESPONSE_GATEWAY"].model_copy(
                update={"dependencies": final_dependencies}
            )
        ordered = self._topological_order(selected)
        planned_at = utc_now()
        body = {
            "request_id": request.request_id,
            "policy_version": self.policy_version,
            "nodes": ordered,
            "planned_at": planned_at,
        }
        plan_hash = content_hash(body)
        plan = CapabilityExecutionPlan(
            plan_id=f"cap-plan-{uuid.uuid5(uuid.NAMESPACE_URL, plan_hash)}",
            request_id=request.request_id,
            policy_version=self.policy_version,
            nodes=tuple(ordered),
            planned_at=planned_at,
            plan_hash=plan_hash,
        )
        if self.store is not None:
            self.store.save_capability_plan(plan)
        return plan

    @staticmethod
    def _dependency_closure(nodes: Mapping[str, CapabilityNode]) -> dict[str, CapabilityNode]:
        included = {
            capability_id
            for capability_id, node in nodes.items()
            if node.requirement
            in {
                CapabilityRequirement.REQUIRED,
                CapabilityRequirement.CONDITIONAL,
                CapabilityRequirement.PROHIBITED,
            }
        }
        queue = deque(included)
        while queue:
            capability_id = queue.popleft()
            for dependency in nodes[capability_id].dependencies:
                if dependency not in included:
                    included.add(dependency)
                    queue.append(dependency)
        return {capability_id: nodes[capability_id] for capability_id in included}

    @staticmethod
    def _promote_required_dependencies(
        nodes: Mapping[str, CapabilityNode],
    ) -> dict[str, CapabilityNode]:
        promoted = dict(nodes)
        queue = deque(
            capability_id
            for capability_id, node in promoted.items()
            if capability_id != "RESPONSE_GATEWAY"
            and node.requirement is CapabilityRequirement.REQUIRED
        )
        seen: set[str] = set()
        while queue:
            capability_id = queue.popleft()
            if capability_id in seen:
                continue
            seen.add(capability_id)
            for dependency in promoted[capability_id].dependencies:
                dependency_node = promoted.get(dependency)
                if dependency_node is None:
                    continue
                if dependency_node.requirement in {
                    CapabilityRequirement.OPTIONAL,
                    CapabilityRequirement.CONDITIONAL,
                }:
                    dependency_node = dependency_node.model_copy(
                        update={"requirement": CapabilityRequirement.REQUIRED}
                    )
                    promoted[dependency] = dependency_node
                if dependency_node.requirement is CapabilityRequirement.REQUIRED:
                    queue.append(dependency)
        return promoted

    @staticmethod
    def _topological_order(nodes: Mapping[str, CapabilityNode]) -> list[CapabilityNode]:
        indegree = {capability_id: 0 for capability_id in nodes}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for capability_id, node in nodes.items():
            for dependency in node.dependencies:
                if dependency not in nodes:
                    continue
                indegree[capability_id] += 1
                outgoing[dependency].append(capability_id)
        queue = deque(sorted(key for key, value in indegree.items() if value == 0))
        ordered: list[CapabilityNode] = []
        while queue:
            capability_id = queue.popleft()
            ordered.append(nodes[capability_id])
            for child in sorted(outgoing[capability_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(ordered) != len(nodes):
            raise ValueError("capability graph contains a dependency cycle")
        return ordered

    @classmethod
    def scenario_requirements(
        cls, path: str | Path, scenario_id: int
    ) -> dict[str, CapabilityRequirement]:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        for scenario in raw["scenarios"]:
            if int(scenario["id"]) == scenario_id:
                mapping: dict[str, CapabilityRequirement] = {}
                for capability_id in scenario.get("required", []):
                    mapping[str(capability_id)] = CapabilityRequirement.REQUIRED
                for capability_id in scenario.get("conditional", []):
                    mapping[str(capability_id)] = CapabilityRequirement.CONDITIONAL
                for capability_id in scenario.get("prohibited", []):
                    mapping[str(capability_id)] = CapabilityRequirement.PROHIBITED
                return mapping
        raise KeyError(f"unknown scenario id: {scenario_id}")


class CapabilityExecutor:
    def __init__(
        self,
        handlers: Mapping[str, CapabilityHandler],
        *,
        store: InvestorOrchestrationStore | None = None,
    ) -> None:
        self.handlers = dict(handlers)
        self.store = store
        self.verifier = RegisteredOutputVerifier(store) if store is not None else None

    def execute(
        self,
        plan: CapabilityExecutionPlan,
        request: InvestorRequestEnvelope,
        preflight: InvestorSessionPreflightReceipt,
    ) -> CapabilityCoverageReceipt:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        plan = CapabilityExecutionPlan.model_validate(plan.model_dump())
        preflight = InvestorSessionPreflightReceipt.model_validate(preflight.model_dump())
        validate_request_permissions(request)
        if request.request_id != plan.request_id or request.request_id != preflight.request_id:
            raise ValueError("execution plan, request and preflight identities differ")
        plan_body = plan.model_dump(exclude={"plan_id", "plan_hash"})
        if content_hash(plan_body) != plan.plan_hash:
            raise ValueError("execution plan hash mismatch")
        nodes_by_id = {node.capability_id: node for node in plan.nodes}
        if len(nodes_by_id) != len(plan.nodes):
            raise ValueError("duplicate capability nodes")
        mandatory = _INTENT_REQUIREMENTS[request.normalized_intent] | {
            key
            for key, node in _BASE_NODES.items()
            if node.requirement is CapabilityRequirement.REQUIRED
        }
        if any(
            key not in nodes_by_id
            or nodes_by_id[key].requirement is not CapabilityRequirement.REQUIRED
            for key in mandatory
        ):
            raise ValueError("execution plan is missing mandatory capabilities")
        baseline = CapabilityPlanner(policy_version=plan.policy_version).plan(request, preflight)
        baseline_nodes = {node.capability_id: node for node in baseline.nodes}
        for node in plan.nodes:
            expected = baseline_nodes.get(node.capability_id, _BASE_NODES.get(node.capability_id))
            if (
                expected is not None
                and node.capability_id == "EXTERNAL_ACCOUNT"
                and request.normalized_intent is RequestIntent.FULL_RESEARCH_RECOMMENDATION
                and node.requirement is CapabilityRequirement.REQUIRED
            ):
                account_side_effect = (
                    SideEffectClass.EA_PROVISIONAL
                    if request.side_effect is SideEffectClass.EA_PROVISIONAL
                    else SideEffectClass.READ
                )
                account_update: dict[str, object] = {
                    "requirement": CapabilityRequirement.REQUIRED,
                    "side_effect": account_side_effect,
                }
                if account_side_effect is SideEffectClass.EA_PROVISIONAL:
                    account_update["output_schema"] = "ExternalAccountOperationReceipt"
                expected = expected.model_copy(update=account_update)
            if expected is None or node.output_schema != expected.output_schema:
                raise ValueError("execution plan changed a capability output contract")
            if not set(expected.dependencies).issubset(node.dependencies):
                raise ValueError("execution plan removed a capability dependency")
            if (
                expected.requirement is CapabilityRequirement.PROHIBITED
                and node.requirement is not CapabilityRequirement.PROHIBITED
            ):
                raise ValueError("execution plan enabled a prohibited capability")
            if node.side_effect is not expected.side_effect:
                raise ValueError("execution plan changed a capability side-effect lane")
        records: list[CapabilityRunRecord] = []
        status_by_id: dict[str, CapabilityRunStatus] = {}
        artifacts_by_id: dict[str, tuple[str, ...]] = {}
        requirement_by_id = {node.capability_id: node.requirement for node in plan.nodes}
        for node in plan.nodes:
            if node.requirement is CapabilityRequirement.PROHIBITED:
                records.append(
                    CapabilityRunRecord(
                        capability_id=node.capability_id,
                        status=CapabilityRunStatus.PROHIBITED,
                        reason="prohibited by request policy",
                    )
                )
                status_by_id[node.capability_id] = CapabilityRunStatus.PROHIBITED
                continue
            blocked_dependency = next(
                (
                    dependency
                    for dependency in node.dependencies
                    if status_by_id.get(dependency)
                    in {
                        CapabilityRunStatus.FAILED,
                        CapabilityRunStatus.DEGRADED,
                        CapabilityRunStatus.SKIPPED,
                        CapabilityRunStatus.PROHIBITED,
                    }
                    and (
                        node.capability_id != "RESPONSE_GATEWAY"
                        or requirement_by_id.get(dependency) is CapabilityRequirement.REQUIRED
                    )
                ),
                None,
            )
            if blocked_dependency is not None:
                blocked_status = (
                    CapabilityRunStatus.FAILED
                    if node.requirement is CapabilityRequirement.REQUIRED
                    else CapabilityRunStatus.SKIPPED
                )
                records.append(
                    CapabilityRunRecord(
                        capability_id=node.capability_id,
                        status=blocked_status,
                        reason=f"dependency unavailable: {blocked_dependency}",
                    )
                )
                status_by_id[node.capability_id] = blocked_status
                continue
            handler = self.handlers.get(node.capability_id)
            if handler is None:
                status = (
                    CapabilityRunStatus.FAILED
                    if node.requirement is CapabilityRequirement.REQUIRED
                    else CapabilityRunStatus.SKIPPED
                )
                records.append(
                    CapabilityRunRecord(
                        capability_id=node.capability_id,
                        status=status,
                        reason="no registered handler",
                    )
                )
                status_by_id[node.capability_id] = status
                continue
            started = time.perf_counter()
            try:
                if node.side_effect not in {
                    SideEffectClass.READ,
                    SideEffectClass.META,
                    SideEffectClass.NONE,
                    request.side_effect,
                }:
                    raise ValueError("capability requires an unauthorized economic side effect")
                if any(dependency not in status_by_id for dependency in node.dependencies):
                    raise ValueError("capability dependency was not executed")
                if isinstance(handler, DependencyAwareCapabilityHandler):
                    dependency_context = CapabilityDependencyContext(
                        capability_id=node.capability_id,
                        plan_id=plan.plan_id,
                        dependency_artifacts={
                            dependency: artifacts_by_id.get(dependency, ())
                            for dependency in node.dependencies
                        },
                        completed_artifacts=dict(artifacts_by_id),
                    )
                    raw_result = handler.callback(request, preflight, dependency_context)
                else:
                    raw_result = handler(request, preflight)
                if not isinstance(raw_result, CapabilityExecutionResult):
                    raise ValueError("handler must return its typed execution result")
                result = raw_result
                has_typed_output = bool(result.artifact_ids) and all(
                    isinstance(artifact_id, str) and artifact_id.strip()
                    for artifact_id in result.artifact_ids
                )
                reason = result.degraded_reason
                if not reason and has_typed_output:
                    if self.verifier is None:
                        raise ValueError("registered output verification is unavailable")
                    self.verifier.verify(node, result.artifact_ids, request, preflight)
                if reason:
                    status = CapabilityRunStatus.DEGRADED
                elif not has_typed_output:
                    status = (
                        CapabilityRunStatus.FAILED
                        if node.requirement is CapabilityRequirement.REQUIRED
                        else CapabilityRunStatus.SKIPPED
                    )
                    reason = f"output missing for {node.output_schema}"
                else:
                    status = (
                        CapabilityRunStatus.REUSED
                        if result.reused
                        else CapabilityRunStatus.COMPLETED
                    )
                records.append(
                    CapabilityRunRecord(
                        capability_id=node.capability_id,
                        status=status,
                        artifact_ids=result.artifact_ids,
                        reason=reason,
                        source_revision=result.source_revision,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                )
                status_by_id[node.capability_id] = status
                if status in {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}:
                    artifacts_by_id[node.capability_id] = result.artifact_ids
            except Exception as exc:  # noqa: BLE001 - converted to typed failure receipt
                records.append(
                    CapabilityRunRecord(
                        capability_id=node.capability_id,
                        status=CapabilityRunStatus.FAILED,
                        reason=f"{type(exc).__name__}: {exc}",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                )
                status_by_id[node.capability_id] = CapabilityRunStatus.FAILED

        required_ids = {
            node.capability_id
            for node in plan.nodes
            if node.requirement is CapabilityRequirement.REQUIRED
        }
        successful = {
            record.capability_id
            for record in records
            if record.status in {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}
        }
        coverage = 1.0 if not required_ids else len(required_ids & successful) / len(required_ids)
        prohibited_ids = {
            node.capability_id
            for node in plan.nodes
            if node.requirement is CapabilityRequirement.PROHIBITED
        }
        prohibited_calls = sum(
            1
            for record in records
            if record.capability_id in prohibited_ids
            and record.status not in {CapabilityRunStatus.PROHIBITED, CapabilityRunStatus.SKIPPED}
        )
        conflicts = tuple(
            f"{record.capability_id}:{record.reason or record.status.value}"
            for record in records
            if record.status in {CapabilityRunStatus.FAILED, CapabilityRunStatus.DEGRADED}
        )
        body = {
            "request_id": request.request_id,
            "plan_id": plan.plan_id,
            "policy_version": plan.policy_version,
            "records": records,
            "required_capability_coverage": coverage,
            "prohibited_call_count": prohibited_calls,
            "coverage_complete": coverage == 1.0 and prohibited_calls == 0 and not conflicts,
            "outputs_verified": self.verifier is not None,
            "preflight_receipt_id": preflight.receipt_id,
            "request_fingerprint": content_hash(request),
            "unresolved_conflicts": conflicts,
        }
        receipt_hash = content_hash(body)
        receipt = CapabilityCoverageReceipt(
            receipt_id=f"coverage-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_hash)}",
            receipt_hash=receipt_hash,
            **body,
        )
        if self.store is not None:
            self.store.save_coverage_receipt(receipt)
        return receipt
