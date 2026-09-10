from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from astock.investor_orchestration.capabilities import CapabilityHandler
from astock.investor_orchestration.models import (
    CapabilityRequirement,
    InvestorAnswer,
    InvestorAnswerDraft,
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.service import InvestorOrchestrationService


class BusinessScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    title: str
    intent: RequestIntent
    required: tuple[str, ...]
    conditional: tuple[str, ...] = ()
    prohibited: tuple[str, ...] = ()
    allowed_side_effects: tuple[SideEffectClass, ...]
    side_effect_description: str
    acceptance: str
    source_chain: str

    @model_validator(mode="after")
    def capability_sets_do_not_overlap(self) -> BusinessScenario:
        sets = [set(self.required), set(self.conditional), set(self.prohibited)]
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise ValueError("scenario capability sets overlap")
        return self

    def requirement_map(self) -> dict[str, CapabilityRequirement]:
        result = {capability_id: CapabilityRequirement.REQUIRED for capability_id in self.required}
        result.update(
            {capability_id: CapabilityRequirement.CONDITIONAL for capability_id in self.conditional}
        )
        result.update(
            {capability_id: CapabilityRequirement.PROHIBITED for capability_id in self.prohibited}
        )
        return result


class BusinessScenarioManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    policy_version: str
    source_matrix: str
    scenario_count: int
    scenarios: tuple[BusinessScenario, ...]

    @model_validator(mode="after")
    def count_and_ids_are_unique(self) -> BusinessScenarioManifest:
        ids = [scenario.id for scenario in self.scenarios]
        if self.scenario_count != len(ids):
            raise ValueError("scenario_count does not match scenarios")
        if len(ids) != len(set(ids)):
            raise ValueError("scenario ids must be unique")
        return self

    def get(self, scenario_id: int) -> BusinessScenario:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise KeyError(f"unknown business scenario: {scenario_id}")


class ScenarioExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: int
    request_id: str
    answer: InvestorAnswer
    required_capability_coverage: float
    prohibited_call_count: int
    coverage_complete: bool
    economic_side_effect_allowed: bool
    failures: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)


class ScenarioContractRunner:
    def __init__(
        self,
        orchestration: InvestorOrchestrationService,
        manifest: BusinessScenarioManifest,
    ) -> None:
        self.orchestration = orchestration
        self.manifest = manifest

    @classmethod
    def from_path(
        cls,
        orchestration: InvestorOrchestrationService,
        path: str | Path = "configs/business_scenarios_v1.yaml",
    ) -> ScenarioContractRunner:
        manifest = BusinessScenarioManifest.model_validate(
            yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        )
        return cls(orchestration, manifest)

    def run(
        self,
        scenario_id: int,
        request: InvestorRequestEnvelope,
        draft: InvestorAnswerDraft | None = None,
        *,
        handlers: Mapping[str, CapabilityHandler],
    ) -> ScenarioExecutionResult:
        scenario = self.manifest.get(scenario_id)
        if request.normalized_intent is not scenario.intent:
            raise ValueError(f"scenario {scenario_id} requires intent {scenario.intent.value}")
        allowed = request.side_effect in scenario.allowed_side_effects
        if not allowed:
            raise ValueError(
                f"side effect {request.side_effect.value} is not allowed for scenario {scenario_id}"
            )
        preflight, _, coverage = self.orchestration.execute(
            request,
            handlers=handlers,
            scenario_requirements=scenario.requirement_map(),
        )
        answer = (
            self.orchestration.publish_verified(coverage_receipt_id=coverage.receipt_id)
            if draft is None
            else self.orchestration.gateway.render(
                draft,
                preflight=preflight,
                coverage=coverage,
            )
        )
        failures: list[str] = []
        if answer.degraded:
            failures.append("ANSWER_NOT_CERTIFIED")
        if not coverage.coverage_complete:
            failures.append("COVERAGE_NOT_CERTIFIED")
        if coverage.required_capability_coverage != 1.0:
            failures.append("REQUIRED_CAPABILITY_COVERAGE")
        if coverage.prohibited_call_count:
            failures.append("PROHIBITED_CAPABILITY_CALL")
        if preflight.context.empty_holdings and (
            answer.actual_holding_section is not None or answer.paper_holding_section is not None
        ):
            failures.append("EMPTY_HOLDING_NOT_SILENT")
        return ScenarioExecutionResult(
            scenario_id=scenario_id,
            request_id=request.request_id,
            answer=answer,
            required_capability_coverage=coverage.required_capability_coverage,
            prohibited_call_count=coverage.prohibited_call_count,
            coverage_complete=coverage.coverage_complete,
            economic_side_effect_allowed=allowed,
            failures=tuple(failures),
            evidence={
                "preflight_receipt_id": preflight.receipt_id,
                "coverage_receipt_id": coverage.receipt_id,
                "acceptance": scenario.acceptance,
            },
        )

    def validate_handlers(
        self,
        handlers: Mapping[str, CapabilityHandler],
    ) -> tuple[str, ...]:
        required = {
            capability_id
            for scenario in self.manifest.scenarios
            for capability_id in scenario.required
        }
        built_in = {
            "REQUEST_TIME",
            "ENTITY_IDENTITY",
            "SESSION_PREFLIGHT",
            "MARKET_REGIME",
            "SUBJECT_REGISTRY",
            "RESPONSE_GATEWAY",
        }
        return tuple(sorted(required - built_in - set(handlers)))

    def ids(self) -> tuple[int, ...]:
        return tuple(scenario.id for scenario in self.manifest.scenarios)

    def sample(self, count: int) -> tuple[BusinessScenario, ...]:
        if count < 0:
            raise ValueError("count must be non-negative")
        return self.manifest.scenarios[:count]

    def ensure_exact_ids(self, expected_ids: Sequence[int]) -> None:
        if self.ids() != tuple(expected_ids):
            raise ValueError("manifest ids differ from the frozen scenario contract")
