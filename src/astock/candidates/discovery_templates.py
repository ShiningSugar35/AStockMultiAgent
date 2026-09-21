"""Typed discovery-thesis templates compiled from the reviewed research Skill registry.

Industry bottleneck, event transmission and fixed-asset cycle hypotheses are
defined by the project's audited Serenity/local method contracts. This avoids
guessing role semantics from broad knowledge-Skill families.
"""

from __future__ import annotations

from dataclasses import dataclass

from astock.candidates.discovery_theses import (
    REQUIRED_ROLES,
    DiscoveryChannel,
    DiscoveryMethodBinding,
    DiscoveryThesisDefinition,
)
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.schemas.research import ResearchSkillManifest, ResearchSkillRegistry

_METHOD_BY_CHANNEL = {
    DiscoveryChannel.BOTTLENECK: ("IndustryBottleneckSkill", "industry-bottleneck-v2"),
    DiscoveryChannel.EVENT: ("EventToAlphaSkill", "event-to-alpha-v2"),
    DiscoveryChannel.CYCLE: ("JuglarCycleStageSkill", "juglar-cycle-stage-v1"),
}

_APPLICABILITY = {
    DiscoveryChannel.BOTTLENECK: (
        "GENERAL_INDUSTRIAL",
        "RESOURCE_MINING",
    ),
    DiscoveryChannel.EVENT: ("*",),
    DiscoveryChannel.CYCLE: (
        "GENERAL_INDUSTRIAL",
        "RESOURCE_MINING",
        "REAL_ESTATE",
    ),
}

_ROLE_QUESTIONS = {
    DiscoveryChannel.BOTTLENECK: {
        "necessity": "该环节对目标系统或产品是否构成不可省略的必要环节？",
        "supply_scarcity": "供给是否存在可验证的稀缺性、产能约束或合格供应商约束？",
        "substitution_difficulty": "替代方案是否因性能、认证、成本、时间或产能约束而难以快速替代？",
        "company_exposure": "目标公司是否具有可验证的产品、客户、收入、成本或产能暴露？",
        "profit_capture": (
            "瓶颈改善或稀缺性是否能通过价格、销量、份额或成本传导到公司利润与现金流？"
        ),
        "counterevidence_review": (
            "经反证审查后，是否未发现足以否定瓶颈、稀缺性、公司暴露或利润捕获链条的材料？"
        ),
    },
    DiscoveryChannel.EVENT: {
        "event_occurred": "事件是否已经发生并由可核验来源确认，而非预测、传闻或计划？",
        "demand_transmission": "事件是否通过明确机制改变需求、供给、价格、成本或行业规则？",
        "company_exposure": "目标公司是否具有可验证的产品、客户、收入、成本或产能暴露？",
        "financial_transmission": (
            "事件影响是否能够映射到收入、成本、利润、现金流或资本需求的可验证方向？"
        ),
        "verification_window": "是否定义了可观察的验证窗口和后续应出现的经营或财务证据？",
        "counterevidence_review": (
            "经反证审查后，是否未发现足以否定事件发生、传导机制、公司暴露或财务影响的材料？"
        ),
    },
    DiscoveryChannel.CYCLE: {
        "demand": "行业需求是否出现可验证的改善、恶化或结构变化？",
        "price": "产品价格或单位经济是否出现与周期命题一致的可验证变化？",
        "inventory": "渠道或产业库存是否出现与周期阶段一致的可验证变化？",
        "capacity": "有效产能、开工率或新增供给是否出现与周期阶段一致的变化？",
        "capital_expenditure": "资本开支与扩产行为是否支持当前周期阶段判断？",
        "cycle_stage": "需求、价格、库存、产能与资本开支的组合是否支持明确的行业周期阶段？",
        "company_exposure": "目标公司是否具有可验证的产品、客户、收入、成本或产能暴露？",
        "company_operating_change": (
            "公司订单、销量、价格、毛利、现金流等经营事实是否出现与周期命题一致的变化？"
        ),
        "counterevidence_review": (
            "经反证审查后，是否未发现足以否定周期阶段、公司暴露或经营改善链条的材料？"
        ),
    },
}


@dataclass(frozen=True)
class DiscoveryContractCondition:
    skill_id: str
    skill_version: str
    role: str
    condition_id: str
    condition_family: str
    applicability: tuple[str, ...]
    semantic_question: str
    source_references: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryContractTemplate:
    channel: DiscoveryChannel
    skill_id: str
    skill_version: str
    source_references: tuple[str, ...]
    conditions: tuple[DiscoveryContractCondition, ...]


@dataclass(frozen=True)
class DiscoveryContractCatalog:
    registry_version: str
    registry_hash: str
    templates: tuple[DiscoveryContractTemplate, ...]
    result_hash: str

    def template(self, channel: DiscoveryChannel) -> DiscoveryContractTemplate:
        matches = [item for item in self.templates if item.channel is channel]
        if len(matches) != 1:
            raise ValueError(f"discovery contract template missing or ambiguous: {channel.value}")
        return matches[0]

    def condition(
        self,
        skill_id: str,
        condition_id: str,
    ) -> DiscoveryContractCondition | None:
        matches = [
            condition
            for template in self.templates
            for condition in template.conditions
            if condition.skill_id == skill_id and condition.condition_id == condition_id
        ]
        if len(matches) > 1:
            raise ValueError("discovery contract condition identity is ambiguous")
        return matches[0] if matches else None

    def verify(self) -> None:
        expected = _catalog_result_hash(
            self.registry_version,
            self.registry_hash,
            self.templates,
        )
        if expected != self.result_hash:
            raise ValueError("discovery contract catalog content hash mismatch")


def _manifest_for(
    registry: ResearchSkillRegistry,
    channel: DiscoveryChannel,
) -> ResearchSkillManifest:
    expected_id, expected_version = _METHOD_BY_CHANNEL[channel]
    matches = [
        manifest
        for manifest in registry.skills
        if manifest.skill_id == expected_id and manifest.skill_version == expected_version
    ]
    if len(matches) != 1:
        raise ValueError(
            f"reviewed discovery method is missing or version-drifted: "
            f"{expected_id}@{expected_version}"
        )
    manifest = matches[0]
    if manifest.status.value != "ENABLED_CONTRACT":
        raise ValueError(f"discovery method is not enabled: {manifest.skill_id}")
    if not manifest.source_references:
        raise ValueError(f"discovery method has no audited source references: {manifest.skill_id}")
    return manifest


def _condition(
    *,
    manifest: ResearchSkillManifest,
    channel: DiscoveryChannel,
    role: str,
) -> DiscoveryContractCondition:
    question = _ROLE_QUESTIONS[channel].get(role)
    if question is None:
        raise ValueError(
            "discovery role lacks an explicit reviewed question: "
            f"{channel.value}:{role}"
        )
    identity = {
        "channel": channel.value,
        "skill_id": manifest.skill_id,
        "skill_version": manifest.skill_version,
        "role": role,
        "question": question,
    }
    return DiscoveryContractCondition(
        skill_id=manifest.skill_id,
        skill_version=manifest.skill_version,
        role=role,
        condition_id=f"method-condition:{content_hash(identity)}",
        condition_family=f"{channel.value.lower()}:{role}",
        applicability=tuple(sorted(_APPLICABILITY[channel])),
        semantic_question=question,
        source_references=tuple(sorted(manifest.source_references)),
    )


def _template(
    registry: ResearchSkillRegistry,
    channel: DiscoveryChannel,
) -> DiscoveryContractTemplate:
    manifest = _manifest_for(registry, channel)
    roles = REQUIRED_ROLES[channel]
    questions = _ROLE_QUESTIONS[channel]
    if set(questions) != set(roles):
        raise ValueError(f"discovery role-question coverage drift: {channel.value}")
    conditions = tuple(
        _condition(manifest=manifest, channel=channel, role=role)
        for role in roles
    )
    return DiscoveryContractTemplate(
        channel=channel,
        skill_id=manifest.skill_id,
        skill_version=manifest.skill_version,
        source_references=tuple(sorted(manifest.source_references)),
        conditions=conditions,
    )


def _catalog_payload(
    registry_version: str,
    registry_hash: str,
    templates: tuple[DiscoveryContractTemplate, ...],
) -> dict[str, object]:
    return {
        "schema_version": "discovery-contract-catalog-v1",
        "registry_version": registry_version,
        "registry_hash": registry_hash,
        "templates": [
            {
                "channel": template.channel.value,
                "skill_id": template.skill_id,
                "skill_version": template.skill_version,
                "source_references": list(template.source_references),
                "conditions": [
                    {
                        "skill_id": condition.skill_id,
                        "skill_version": condition.skill_version,
                        "role": condition.role,
                        "condition_id": condition.condition_id,
                        "condition_family": condition.condition_family,
                        "applicability": list(condition.applicability),
                        "semantic_question": condition.semantic_question,
                        "source_references": list(condition.source_references),
                    }
                    for condition in template.conditions
                ],
            }
            for template in templates
        ],
    }


def _catalog_result_hash(
    registry_version: str,
    registry_hash: str,
    templates: tuple[DiscoveryContractTemplate, ...],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(_catalog_payload(registry_version, registry_hash, templates))
    )


def compile_discovery_contract_catalog(
    registry: ResearchSkillRegistry,
) -> DiscoveryContractCatalog:
    registry_hash = content_hash(registry.model_dump(mode="json"))
    templates = tuple(_template(registry, channel) for channel in DiscoveryChannel)
    result_hash = _catalog_result_hash(registry.registry_version, registry_hash, templates)
    catalog = DiscoveryContractCatalog(
        registry_version=registry.registry_version,
        registry_hash=registry_hash,
        templates=templates,
        result_hash=result_hash,
    )
    catalog.verify()
    return catalog


def compile_discovery_thesis_definitions(
    catalog: DiscoveryContractCatalog,
    *,
    family_prefix: str = "reviewed-method",
) -> tuple[DiscoveryThesisDefinition, ...]:
    catalog.verify()
    definitions: list[DiscoveryThesisDefinition] = []
    for channel in DiscoveryChannel:
        template = catalog.template(channel)
        conditions_by_role = {condition.role: condition for condition in template.conditions}
        roles = REQUIRED_ROLES[channel]
        if set(conditions_by_role) != set(roles):
            raise ValueError(f"discovery template role coverage drift: {channel.value}")
        definitions.append(
            DiscoveryThesisDefinition(
                family_id=f"{family_prefix}:{channel.value.lower()}",
                channel=channel,
                bindings=tuple(
                    DiscoveryMethodBinding(
                        role=role,
                        final_skill_id=template.skill_id,
                        condition_id=conditions_by_role[role].condition_id,
                    )
                    for role in roles
                ),
            )
        )
    return tuple(definitions)


__all__ = [
    "DiscoveryContractCatalog",
    "DiscoveryContractCondition",
    "DiscoveryContractTemplate",
    "compile_discovery_contract_catalog",
    "compile_discovery_thesis_definitions",
]
