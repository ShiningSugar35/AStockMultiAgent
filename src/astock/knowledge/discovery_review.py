"""Deterministic reviewed disposition planning for active knowledge Skills.

The planner does not invent numeric thresholds. It only routes an already-admitted
Skill to one primary discovery responsibility and, when the responsibility is
semantic discovery, compiles the Skill's own decision question into a typed
four-state condition. The catalog compiler remains the exact-coverage gate.
"""

from __future__ import annotations

import json
from datetime import datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_completion import (
    KnowledgeDiscoveryConditionKind,
    KnowledgeDiscoveryConditionSpec,
    KnowledgeDiscoveryDisposition,
    KnowledgeDiscoveryDispositionBatch,
    KnowledgeDiscoveryDispositionSpec,
    KnowledgeSkillInventoryMember,
    KnowledgeSkillInventorySnapshot,
)

_COMPILER_VERSION = "skill-discovery-compiler-v2-structured"

_INDUSTRY_FINANCIAL_PROFILES = {
    "FINANCIALS": ("BANK", "INSURANCE", "SECURITIES"),
    "REAL_ESTATE": ("REAL_ESTATE",),
}
_GENERAL_INDUSTRY_SCOPES = {
    "SEMICONDUCTORS",
    "AI_SOFTWARE",
    "CONSUMER",
    "HEALTHCARE",
    "MATERIALS",
    "NEW_ENERGY",
    "AEROSPACE_DEFENSE",
    "AUTOMOTIVE",
}

_TIMING_FAMILIES = {
    "TECHNICAL_PATTERN",
    "TREND_CONFIRMATION",
    "VOLUME_SUPPLY",
    "RELATIVE_STRENGTH",
    "REBOUND_VS_REVERSAL",
    "MARKET_LIQUIDITY",
    "MARKET_BREADTH",
    "CROSS_ASSET_FX",
    "CORE_TACTICAL",
}
_PORTFOLIO_GOVERNANCE_FAMILIES = {
    "POSITION_LIMITS",
    "REBALANCING",
    "INVESTOR_CONSTRAINTS",
    "EMOTION_CONTROL",
    "ABILITY_CIRCLE",
    "JOURNAL_REVIEW",
    "HEDGE_STRESS_TEST",
}
_DEEP_REVIEW_FAMILIES = {
    "SOURCE_VALIDATION",
    "ERROR_RECOVERY",
}
_DISCOVERY_FAMILIES = {
    "CATALYST_PRICE_RESPONSE",
    "EVENT_RISK",
    "CROWDING_LEVERAGE",
    "VALUATION_EXPECTATIONS",
    "INDUSTRY_STAGE",
    "BUSINESS_MODEL",
    "EARNINGS_QUALITY",
    "CASH_FLOW_BALANCE_SHEET",
    "CAPEX_MONETIZATION",
    "CYCLICAL_INDUSTRY",
    "CONSUMER_FIELD_RESEARCH",
    "COMPETITIVE_MOAT",
    "MULTIFACTOR_FRAMEWORK",
    "BENCHMARK_EXPOSURE",
    "INDEX_COMPOSITION",
}


class KnowledgeDiscoveryDispositionReviewer:
    """Produce one deterministic reviewed primary disposition per active Skill."""

    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    def review(
        self,
        inventory: KnowledgeSkillInventorySnapshot,
    ) -> KnowledgeDiscoveryDispositionBatch:
        status = inventory.provider_status
        if status.registry_object_hash is None:
            raise ValueError("discovery review requires immutable registry identity")
        release_payload = self._payload(status.registry_object_hash)
        reviewed_at = self._release_created_at(release_payload)
        specs = [
            self._spec(member)
            for member in sorted(inventory.members, key=lambda item: item.final_skill_id)
        ]
        return KnowledgeDiscoveryDispositionBatch(
            run_id=inventory.run_id,
            registry_object_hash=status.registry_object_hash,
            compiler_version=_COMPILER_VERSION,
            reviewer="deterministic-active-skill-disposition-review-v2",
            reviewed_at=reviewed_at,
            specs=specs,
        )

    def _spec(
        self,
        member: KnowledgeSkillInventoryMember,
    ) -> KnowledgeDiscoveryDispositionSpec:
        payload = self._payload(member.object_hash)
        if str(payload.get("final_skill_id", payload.get("skill_id", ""))) not in {
            "",
            member.final_skill_id,
        }:
            raise ValueError(f"discovery review Skill identity drift: {member.final_skill_id}")
        family = str(payload.get("family", "")).strip().upper()
        if family in _TIMING_FAMILIES:
            return KnowledgeDiscoveryDispositionSpec(
                final_skill_id=member.final_skill_id,
                disposition=KnowledgeDiscoveryDisposition.TIMING_CONTEXT,
                reason_codes=["STRUCTURED_FAMILY_TIMING_CONTEXT"],
            )
        if family in _PORTFOLIO_GOVERNANCE_FAMILIES:
            return KnowledgeDiscoveryDispositionSpec(
                final_skill_id=member.final_skill_id,
                disposition=KnowledgeDiscoveryDisposition.PORTFOLIO_REPORT_GOVERNANCE,
                reason_codes=["STRUCTURED_FAMILY_PORTFOLIO_GOVERNANCE"],
            )
        if family in _DEEP_REVIEW_FAMILIES:
            return KnowledgeDiscoveryDispositionSpec(
                final_skill_id=member.final_skill_id,
                disposition=KnowledgeDiscoveryDisposition.DEEP_RESEARCH_REVIEW,
                reason_codes=["STRUCTURED_FAMILY_DEEP_REVIEW"],
            )
        if family in _DISCOVERY_FAMILIES:
            return self._semantic_spec(
                member,
                payload,
                reason_code="STRUCTURED_FAMILY_SEMANTIC_DISCOVERY",
                family=family,
            )

        if member.primary_module in {
            DirectSkillModule.SOURCING_SCREENING,
            DirectSkillModule.FUNDAMENTAL_RESEARCH,
            DirectSkillModule.VALUATION_PRICING,
        }:
            return self._semantic_spec(
                member,
                payload,
                reason_code="ACTIVE_RESEARCH_SKILL_SEMANTIC_DISCOVERY",
                family=family or member.primary_module.value.lower(),
            )
        if member.primary_module is DirectSkillModule.POSITION_RISK_MANAGEMENT:
            return KnowledgeDiscoveryDispositionSpec(
                final_skill_id=member.final_skill_id,
                disposition=KnowledgeDiscoveryDisposition.TIMING_CONTEXT,
                reason_codes=["ACTIVE_RISK_SKILL_TIMING_CONTEXT"],
            )
        if member.primary_module in {
            DirectSkillModule.PORTFOLIO_CONSTRUCTION,
            DirectSkillModule.PSYCHOLOGY_BEHAVIOR,
        }:
            return KnowledgeDiscoveryDispositionSpec(
                final_skill_id=member.final_skill_id,
                disposition=KnowledgeDiscoveryDisposition.PORTFOLIO_REPORT_GOVERNANCE,
                reason_codes=["ACTIVE_PORTFOLIO_BEHAVIOR_GOVERNANCE"],
            )
        return KnowledgeDiscoveryDispositionSpec(
            final_skill_id=member.final_skill_id,
            disposition=KnowledgeDiscoveryDisposition.NOT_APPLICABLE,
            reason_codes=["NO_CURRENT_DISCOVERY_ROLE"],
        )

    def _semantic_spec(
        self,
        member: KnowledgeSkillInventoryMember,
        payload: dict[str, object],
        *,
        reason_code: str,
        family: str,
    ) -> KnowledgeDiscoveryDispositionSpec:
        question = member.decision_question.strip()
        if not question:
            raise ValueError(
                "semantic discovery Skill has no decision question: "
                f"{member.final_skill_id}"
            )
        industry_scope = str(payload.get("industry_scope", "")).strip().upper()
        holding_horizon = str(payload.get("holding_horizon", "")).strip().upper()
        if industry_scope in _INDUSTRY_FINANCIAL_PROFILES:
            applicability = list(_INDUSTRY_FINANCIAL_PROFILES[industry_scope])
        elif industry_scope in _GENERAL_INDUSTRY_SCOPES:
            applicability = ["GENERAL_INDUSTRIAL"]
        else:
            applicability = ["*"]

        raw_counterevidence = payload.get("negative_signals", [])
        counterevidence = (
            [str(value) for value in raw_counterevidence]
            if isinstance(raw_counterevidence, list)
            else []
        )
        raw_invalidation = payload.get("invalidation_conditions", [])
        invalidation = (
            [str(value) for value in raw_invalidation]
            if isinstance(raw_invalidation, list)
            else []
        )
        seed = {
            "final_skill_id": member.final_skill_id,
            "skill_object_hash": member.object_hash,
            "question": question,
            "family": family,
        }
        condition_id = f"skill-condition:{content_hash(seed)}"
        condition = KnowledgeDiscoveryConditionSpec(
            condition_id=condition_id,
            condition_family=family or "active_skill_semantic",
            condition_kind=KnowledgeDiscoveryConditionKind.SEMANTIC_QUESTION,
            applicability=sorted(set(applicability)),
            prerequisites=[],
            semantic_question=question,
            proxy_note=(
                "This condition asks the admitted Skill's own decision question. "
                f"Financial-profile applicability={','.join(applicability)}; "
                f"semantic industry scope={industry_scope or 'UNSPECIFIED'}; "
                f"holding horizon={holding_horizon or 'UNSPECIFIED'}. "
                "No numeric proxy is invented and UNKNOWN never becomes a failure."
            ),
            counterevidence=sorted(set(counterevidence)),
            invalidation_dependencies=sorted(
                {
                    "active_registry_release",
                    "company_fact_state",
                    f"industry_scope:{industry_scope or 'UNSPECIFIED'}",
                    f"holding_horizon:{holding_horizon or 'UNSPECIFIED'}",
                    *(
                        f"skill_invalidation:{value}"
                        for value in invalidation[:3]
                        if value.strip()
                    ),
                }
            ),
            conflict_group=f"skill-premise:{member.final_skill_id}",
        )
        return KnowledgeDiscoveryDispositionSpec(
            final_skill_id=member.final_skill_id,
            disposition=KnowledgeDiscoveryDisposition.SEMANTIC_DISCOVERY,
            reason_codes=[reason_code],
            conditions=[condition],
        )

    def _payload(self, object_hash: str) -> dict[str, object]:
        raw = self.objects.get_bytes(object_hash)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"discovery review object is not valid JSON: {object_hash}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"discovery review object is not an object: {object_hash}")
        return value

    @staticmethod
    def _release_created_at(payload: dict[str, object]) -> datetime:
        raw = payload.get("created_at")
        if not isinstance(raw, str):
            raise ValueError("active registry release has no created_at")
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))


__all__ = ["KnowledgeDiscoveryDispositionReviewer"]
