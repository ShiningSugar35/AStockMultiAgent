"""Policy-driven and auditable source-access routing with Manual always last."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import yaml

from astock.core.state import StateStore
from astock.schemas import (
    AccessTransport,
    RateLimitState,
    SourceAccessDecision,
    SourceAccessRequest,
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
    if not isinstance(raw, dict) or raw.get("schema_version") != "source-access-policy-v1":
        raise ValueError("Unsupported source-access policy")
    if raw.get("manual_last") is not True:
        raise ValueError("Source-access policy must keep Manual last")
    weights = _decimal_map(raw.get("weights"), "weights")
    transport_scores = _decimal_map(raw.get("transport_scores"), "transport_scores")
    officiality_scores = _decimal_map(raw.get("officiality_scores"), "officiality_scores")
    health_scores = _decimal_map(raw.get("health_scores"), "health_scores")
    required_weight_keys = {
        "officiality",
        "health",
        "freshness",
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
    def __init__(
        self,
        state: StateStore | None = None,
        *,
        policy: SourceAccessPolicy | None = None,
    ) -> None:
        self.state = state
        project_root = Path(__file__).resolve().parents[3]
        self.policy = policy or load_source_access_policy(
            project_root / "configs" / "source_access_policy.yaml"
        )

    def decide(
        self,
        request: SourceAccessRequest,
        capabilities: list[TransportCapability],
    ) -> SourceAccessDecision:
        started = datetime.now(UTC)
        matching = [
            item
            for item in capabilities
            if item.source_id == request.source_id
            and request.requested_capability in item.requested_capabilities
        ]
        automated = [
            item for item in matching if item.transport is not AccessTransport.MANUAL
        ]
        strong_official = any(
            token in request.requested_capability.lower()
            for token in self.policy.strong_official_capability_tokens
        )
        available_official = [
            item
            for item in automated
            if item.available and item.officiality == "PRIMARY_OFFICIAL"
        ]
        rank_pool = available_official if strong_official and available_official else automated
        ranked = sorted(
            rank_pool,
            key=lambda item: (-self._score(item), item.transport.value, item.reason),
        )
        attempted: list[AccessTransport] = []
        selected: TransportCapability | None = None
        for item in ranked:
            attempted.append(item.transport)
            if item.available:
                selected = item
                break
        if selected is not None:
            selected_transport = selected.transport
            reason = (
                f"{selected.reason}; policy={self.policy.policy_version}; "
                f"score={self._score(selected)}"
            )
        else:
            selected_transport = AccessTransport.MANUAL
            reason = "No automated capability is available; create a manual investigation task."
            if AccessTransport.MANUAL not in attempted:
                attempted.append(AccessTransport.MANUAL)
        decision = SourceAccessDecision(
            decision_id=uuid4().hex,
            source_id=request.source_id,
            requested_capability=request.requested_capability,
            selected_transport=selected_transport,
            selection_reason=reason,
            fallback_chain=attempted,
            request_started_at=started,
            request_finished_at=datetime.now(UTC),
            rate_limit_state=RateLimitState.UNKNOWN,
        )
        if self.state is not None:
            self.state.record_source_decision(decision)
        return decision

    def _score(self, item: TransportCapability) -> Decimal:
        weights = self.policy.weights
        officiality = self.policy.officiality_scores.get(item.officiality, Decimal("0"))
        health = self.policy.health_scores.get(item.health_status, Decimal("0"))
        transport = self.policy.transport_scores.get(item.transport.value, Decimal("0"))
        latency_score = Decimal("1") / (Decimal("1") + Decimal(item.latency_ms) / Decimal("1000"))
        retryable_recovery = Decimal("1") if item.retryable_failure else Decimal("0")
        return (
            weights["officiality"] * officiality
            + weights["health"] * health
            + weights["freshness"] * item.freshness_score
            + weights["transport"] * transport
            + weights["latency"] * latency_score
            + weights["cost_efficiency"] * item.cost_efficiency_score
            + weights["auth_ease"] * item.auth_ease_score
            + weights["retryable_recovery"] * retryable_recovery
        )


__all__ = ["SourceAccessPolicy", "SourceAccessRouter", "load_source_access_policy"]
