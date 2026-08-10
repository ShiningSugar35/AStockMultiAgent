"""Canonical Phase 7 formal-forward study definition.

The factory freezes only evaluation policy and arm contracts. It does not create
assignments, observations, synthetic samples, or trading decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from astock.schemas.research import ResearchSkillStatus
from astock.schemas.shadow import (
    FrozenWeightProfile,
    ShadowArmDraft,
    ShadowArmResearchStatus,
    ShadowArmType,
    ShadowEvaluationPolicy,
    ShadowStudyCreateRequest,
    ShadowStudyManifest,
    ShadowStudyMode,
)

if TYPE_CHECKING:
    from astock.shadow.service import ShadowEvaluationService

_PROTOCOL_FAMILY_VERSION = "research-protocol-family-v2"
_STUDY_NAME = "phase7-forward-formal-v1"
_DEFAULT_CANDIDATE_POLICY_ID = "candidate-scan"
_DEFAULT_CANDIDATE_POLICY_VERSION = "candidate-scan-v1"


def _weight(component: str, created_at: datetime) -> FrozenWeightProfile:
    return FrozenWeightProfile(
        profile_id=f"phase7-weight:{component}",
        profile_version="phase7-frozen-weight-v1",
        component_weights={component: Decimal("1")},
        created_at=created_at,
    )


def build_default_formal_study_request(
    policy: ShadowEvaluationPolicy,
    *,
    created_at: datetime,
    effective_from: datetime,
    candidate_set_id: str = "phase7-forward-live-v1",
    candidate_policy_id: str = _DEFAULT_CANDIDATE_POLICY_ID,
    candidate_policy_version: str = _DEFAULT_CANDIDATE_POLICY_VERSION,
    initial_capital_fen: int = 100_000_000,
    fixed_notional_fen: int = 1_000_000,
) -> ShadowStudyCreateRequest:
    """Build the one canonical formal study without fabricating any observations."""

    common = {
        "protocol_family_version": _PROTOCOL_FAMILY_VERSION,
        "cost_model_version": policy.cost_model_version,
        "fill_model_version": policy.fill_model_version,
        "corporate_action_version": policy.corporate_action_version,
        "created_at": created_at,
    }
    arms = [
        ShadowArmDraft(
            arm_key="a-rule-baseline",
            arm_type=ShadowArmType.RULE_BASELINE,
            weight_profile=_weight("rule_baseline", created_at),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="b-base-case",
            arm_type=ShadowArmType.BASE_CASE_ONLY,
            weight_profile=_weight("base_case", created_at),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="c-serenity-specialist",
            arm_type=ShadowArmType.BASE_CASE_PLUS_SPECIALIST,
            weight_profile=_weight("serenity_specialist", created_at),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            specialist_skill_id="IndustryBottleneckSkill",
            specialist_skill_version="industry-bottleneck-v2",
            specialist_skill_status=ResearchSkillStatus.ENABLED_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="d-full-committee",
            arm_type=ShadowArmType.FULL_COMMITTEE,
            weight_profile=_weight("full_committee", created_at),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
        ShadowArmDraft(
            arm_key="e-csi300-benchmark",
            arm_type=ShadowArmType.CSI300_BENCHMARK,
            weight_profile=_weight("csi300_benchmark", created_at),
            research_status=ShadowArmResearchStatus.BENCHMARK,
            benchmark_symbol="000300",
            **common,
        ),
        ShadowArmDraft(
            arm_key="f-equal-weight-candidate",
            arm_type=ShadowArmType.EQUAL_WEIGHT_CANDIDATE,
            weight_profile=_weight("equal_weight_candidate", created_at),
            research_status=ShadowArmResearchStatus.PRODUCTION_CONTRACT,
            **common,
        ),
    ]
    return ShadowStudyCreateRequest(
        study_name=_STUDY_NAME,
        mode=ShadowStudyMode.FORWARD_FORMAL,
        effective_from=effective_from,
        candidate_policy_id=candidate_policy_id,
        candidate_policy_version=candidate_policy_version,
        candidate_set_id=candidate_set_id,
        initial_capital_fen=initial_capital_fen,
        fixed_notional_fen=fixed_notional_fen,
        arms=arms,
        created_at=created_at,
    )


def ensure_default_formal_study(
    service: ShadowEvaluationService,
    *,
    now: datetime,
    candidate_set_id: str = "phase7-forward-live-v1",
    effective_delay_seconds: int = 60,
) -> tuple[ShadowStudyManifest, bool]:
    """Create one canonical formal study, or reuse the matching active definition."""

    if effective_delay_seconds < 1:
        raise ValueError("formal study effective delay must be positive")
    latest = service.repository.latest_study_summary()
    existing = service.repository.get_study(str(latest["study_id"])) if latest is not None else None
    if (
        existing is not None
        and existing.mode is ShadowStudyMode.FORWARD_FORMAL
        and existing.policy_version == service.configured_policy.policy_version
        and existing.candidate_set_id == candidate_set_id
    ):
        return existing, True
    request = build_default_formal_study_request(
        service.configured_policy,
        created_at=now,
        effective_from=now + timedelta(seconds=effective_delay_seconds),
        candidate_set_id=candidate_set_id,
    )
    return service.create_study(request).manifest, False


__all__ = ["build_default_formal_study_request", "ensure_default_formal_study"]
