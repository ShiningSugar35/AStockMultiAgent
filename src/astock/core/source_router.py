"""Policy-driven capability routing with deterministic evidence-safety gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import yaml

from astock.core.project_root import resolve_project_root
from astock.core.state import StateStore
from astock.schemas import (
    AccessTransport,
    RateLimitState,
    SourceAccessDecision,
    SourceAccessRequest,
    SourceClass,
    TransportCapability,
)


@dataclass(frozen=True, slots=True)
class SourceAccessPolicy:
    policy_version: str
    manual_last: bool
    strong_official_capability_tokens: tuple[str, ...]
    weights: dict[str, Decimal]
    transport_scores: dict[str, Decimal]
    officiality_scores: dict[str, Decimal]
    health_scores: dict[str, Decimal]


def load_source_access_policy(path: Path) -> SourceAccessPolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid source-access policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "source-access-policy-v2":
        raise ValueError("Unsupported source-access policy")
    if raw.get("manual_last") is not True:
        raise ValueError("Source-access policy must keep Manual last")
    weights = _decimal_map(raw.get("weights"), "weights")
    transport_scores = _decimal_map(raw.get("transport_scores"), "transport_scores")
    officiality_scores = _decimal_map(raw.get("officiality_scores"), "officiality_scores")
    health_scores = _decimal_map(raw.get("health_scores"), "health_scores")
    required_weight_keys = {
        "formal_eligibility",
        "officiality",
        "completeness",
        "local_availability",
        "health",
        "freshness",
        "independence",
        "transport",
        "latency",
        "cost_efficiency",
        "auth_ease",
        "retryable_recovery",
    }
    if set(weights) != required_weight_keys:
        raise ValueError("Source-access policy weight set is incomplete")
    tokens = raw.get("strong_official_capability_tokens")
    if not isinstance(tokens, list) or not tokens:
        raise ValueError("Strong-official capability tokens must be non-empty")
    return SourceAccessPolicy(
        policy_version=str(raw["schema_version"]),
        manual_last=True,
        strong_official_capability_tokens=tuple(str(item).lower() for item in tokens),
        weights=weights,
        transport_scores=transport_scores,
        officiality_scores=officiality_scores,
        health_scores=health_scores,
    )


def _decimal_map(value: object, label: str) -> dict[str, Decimal]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Source-access policy {label} must be non-empty")
    return {str(key): Decimal(str(item)) for key, item in value.items()}


class SourceAccessRouter:
    """Select one source for a capability; source_id is only a backwards-compatible hint."""

    def __init__(
        self,
        state: StateStore | None = None,
        *,
        policy: SourceAccessPolicy | None = None,
    ) -> None:
        self.state = state
        project_root = resolve_project_root(module_file=Path(__file__))
        self.policy = policy or load_source_access_policy(
            project_root / "configs" / "source_access_policy.yaml"
        )

    def rank(
        self,
        request: SourceAccessRequest,
        capabilities: list[TransportCapability],
    ) -> list[TransportCapability]:
        """Return policy-ranked automated sources without performing acquisition."""

        matching = [
            item
            for item in capabilities
            if request.requested_capability in item.requested_capabilities
        ]
        automated = [item for item in matching if item.transport is not AccessTransport.MANUAL]
        if self.state is not None:
            from astock.core.source_resilience import SourceCircuitBreaker

            breaker = SourceCircuitBreaker(self.state)
            automated = [
                item.model_copy(
                    update={
                        "available": False,
                        "reason": f"{item.reason}; circuit breaker is OPEN",
                    }
                )
                if not breaker.is_available(item.source_id, request.requested_capability)
                else item
                for item in automated
            ]
        if request.formal_use:
            automated = [item for item in automated if item.formal_eligible]
        if request.require_complete:
            automated = [item for item in automated if item.completeness_score == Decimal("1")]

        strong_official = any(
            token in request.requested_capability.lower()
            for token in self.policy.strong_official_capability_tokens
        )
        return sorted(
            automated,
            key=lambda item: (
                0 if strong_official and item.available and self._is_primary_official(item) else 1,
                -self._score(item),
                0 if request.source_id is not None and item.source_id == request.source_id else 1,
                item.source_id,
                item.transport.value,
                item.reason,
            ),
        )

    def decide(
        self,
        request: SourceAccessRequest,
        capabilities: list[TransportCapability],
    ) -> SourceAccessDecision:
        started = datetime.now(UTC)
        ranked = self.rank(request, capabilities)
        attempted_transports: list[AccessTransport] = []
        fallback_sources = list(dict.fromkeys(item.source_id for item in ranked))
        selected: TransportCapability | None = None
        for item in ranked:
            attempted_transports.append(item.transport)
            if item.available:
                selected = item
                break

        if selected is not None:
            selected_transport = selected.transport
            selected_source_id = selected.source_id
            reason = (
                f"{selected.reason}; source={selected.source_id}; "
                f"policy={self.policy.policy_version}; score={self._score(selected)}"
            )
        else:
            selected_transport = AccessTransport.MANUAL
            selected_source_id = None
            reason = (
                "No policy-eligible automated source is available; "
                "create a manual investigation task."
            )
            if AccessTransport.MANUAL not in attempted_transports:
                attempted_transports.append(AccessTransport.MANUAL)
            if "MANUAL" not in fallback_sources:
                fallback_sources.append("MANUAL")

        decision_source_id = (
            selected_source_id
            or request.source_id
            or (f"capability:{request.requested_capability}")
        )
        decision = SourceAccessDecision(
            decision_id=uuid4().hex,
            source_id=decision_source_id,
            selected_source_id=selected_source_id,
            requested_capability=request.requested_capability,
            selected_transport=selected_transport,
            selection_reason=reason,
            fallback_chain=attempted_transports,
            fallback_source_chain=fallback_sources,
            request_started_at=started,
            request_finished_at=datetime.now(UTC),
            rate_limit_state=RateLimitState.UNKNOWN,
        )
        if self.state is not None:
            self.state.record_source_decision(decision)
        return decision

    def _score(self, item: TransportCapability) -> Decimal:
        weights = self.policy.weights
        officiality_key = item.officiality
        if officiality_key == "UNKNOWN":
            if item.source_class is SourceClass.PRIMARY_OFFICIAL_WEB:
                officiality_key = "PRIMARY_OFFICIAL"
            elif item.source_class is SourceClass.SECONDARY_STRUCTURED:
                officiality_key = "SECONDARY_STRUCTURED"
        officiality = self.policy.officiality_scores.get(officiality_key, Decimal("0"))
        health = self.policy.health_scores.get(item.health_status, Decimal("0"))
        transport = self.policy.transport_scores.get(item.transport.value, Decimal("0"))
        latency_score = Decimal("1") / (Decimal("1") + Decimal(item.latency_ms) / Decimal("1000"))
        retryable_failure = Decimal("1") if item.retryable_failure else Decimal("0")
        formal_eligibility = Decimal("1") if item.formal_eligible else Decimal("0")
        return (
            weights["formal_eligibility"] * formal_eligibility
            + weights["officiality"] * officiality
            + weights["completeness"] * item.completeness_score
            + weights["local_availability"] * item.local_availability_score
            + weights["health"] * health
            + weights["freshness"] * item.freshness_score
            + weights["independence"] * item.independence_score
            + weights["transport"] * transport
            + weights["latency"] * latency_score
            + weights["cost_efficiency"] * item.cost_efficiency_score
            + weights["auth_ease"] * item.auth_ease_score
            + weights["retryable_recovery"] * retryable_failure
        )

    @staticmethod
    def _is_primary_official(item: TransportCapability) -> bool:
        return (
            item.officiality == "PRIMARY_OFFICIAL"
            or item.source_class is SourceClass.PRIMARY_OFFICIAL_WEB
        )


__all__ = ["SourceAccessPolicy", "SourceAccessRouter", "load_source_access_policy"]
