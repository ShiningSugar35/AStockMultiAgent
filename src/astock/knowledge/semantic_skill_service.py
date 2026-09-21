"""Deduplicate, classify, review, audit, and publish semantic Skill candidates."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.semantic_skill_repository import SemanticSkillRepository
from astock.knowledge.skill_audit import KnowledgeSkillAuditRepository
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_semantic_skills import (
    SemanticEffectiveSkill,
    SemanticSkillAuditReport,
    SemanticSkillGenerationAudit,
    SemanticSkillGenerationRun,
    SemanticSkillOverlayMember,
    SemanticSkillOverlayRelease,
    SemanticSkillReviewDecision,
    SemanticSkillReviewRecord,
)

POLICY_VERSION = "semantic-candidate-overlay-v1"
_TOKEN = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")
_DIRECT_CALL = re.compile(
    r"(?:20\d{2}年|今天|明天|本周|下周|本月|下月|\d{3,5}点|\d+(?:\.\d+)?元)"
    r".{0,24}(?:买入|卖出|加仓|减仓|清仓|满仓|目标位)"
    r"|(?:买入|卖出|加仓|减仓|清仓|满仓|目标位).{0,24}"
    r"(?:20\d{2}年|今天|明天|本周|下周|本月|下月|\d{3,5}点|\d+(?:\.\d+)?元)"
)
_MOTIVE = re.compile(r"(?:内幕|操纵|国家队.{0,8}一定|外资.{0,8}一定|上面.{0,8}一定)")

_RESEARCH = {
    "STOCK_SELECTION",
    "BUSINESS_MODEL",
    "INDUSTRY",
    "FINANCIAL_QUALITY",
}
_LIFECYCLE = {
    "ENTRY",
    "HOLDING",
    "ADD",
    "TRIM",
    "EXIT",
    "RISK",
    "FAILURE_CASE",
    "COUNTEREVIDENCE_INVALIDATION",
    "REVIEW",
}

_INDUSTRIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SEMICONDUCTORS", ("半导体", "芯片", "算力", "光模块", "cpo", "存储", "晶圆")),
    ("AI_SOFTWARE", ("人工智能", "ai", "大模型", "软件", "saas", "互联网", "科网")),
    ("CONSUMER", ("消费", "白酒", "零售", "品牌", "ip", "潮玩", "门店")),
    ("FINANCIALS", ("银行", "保险", "券商", "金融", "证券")),
    ("HEALTHCARE", ("医药", "医疗", "创新药", "药品", "生物")),
    ("MATERIALS", ("有色", "黄金", "白银", "稀土", "钨", "钼", "锑", "金属", "化工")),
    ("NEW_ENERGY", ("新能源", "电池", "储能", "光伏", "锂电", "固态")),
    ("AEROSPACE_DEFENSE", ("军工", "航天", "卫星", "航空", "导弹", "无人机")),
    ("REAL_ESTATE", ("地产", "房地产")),
    ("AUTOMOTIVE", ("汽车", "整车", "智能车")),
    ("MARKET_WIDE", ("指数", "宽基", "etf", "市场", "成交量", "流动性", "筹码", "融资盘")),
)

_HORIZONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SHORT", ("短期", "短线", "日内", "几天", "交易日")),
    ("MEDIUM", ("中期", "数周", "几周", "季度", "几个月")),
    ("LONG", ("长期", "中长期", "多年", "资产配置", "家庭配置", "长周期")),
)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(_TOKEN.findall(folded))


def _ngrams(text: str) -> set[str]:
    value = _normalize(text)
    result: set[str] = set()
    for size in (2, 3):
        if len(value) >= size:
            result.update(value[index : index + size] for index in range(len(value) - size + 1))
    return result


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _module(categories: list[str], text: str) -> tuple[DirectSkillModule, list[DirectSkillModule]]:
    category_set = set(categories)
    if "VALUATION" in category_set:
        primary = DirectSkillModule.VALUATION_PRICING
    elif category_set & {"BUSINESS_MODEL", "FINANCIAL_QUALITY"}:
        primary = DirectSkillModule.FUNDAMENTAL_RESEARCH
    elif category_set & {"STOCK_SELECTION", "INDUSTRY"}:
        primary = DirectSkillModule.SOURCING_SCREENING
    elif any(word in text for word in ("仓位", "组合", "底仓", "资产配置", "分散", "再平衡")):
        primary = DirectSkillModule.PORTFOLIO_CONSTRUCTION
    else:
        primary = DirectSkillModule.POSITION_RISK_MANAGEMENT

    secondary: set[DirectSkillModule] = set()
    if category_set & _RESEARCH and primary is not DirectSkillModule.FUNDAMENTAL_RESEARCH:
        secondary.add(DirectSkillModule.FUNDAMENTAL_RESEARCH)
    if "VALUATION" in category_set and primary is not DirectSkillModule.VALUATION_PRICING:
        secondary.add(DirectSkillModule.VALUATION_PRICING)
    if category_set & _LIFECYCLE and primary is not DirectSkillModule.POSITION_RISK_MANAGEMENT:
        secondary.add(DirectSkillModule.POSITION_RISK_MANAGEMENT)
    psychology_terms = ("心理", "情绪", "从众", "沉没成本", "恐慌", "追涨", "损失厌恶")
    if any(word in text for word in psychology_terms):
        if primary is not DirectSkillModule.PSYCHOLOGY_BEHAVIOR:
            secondary.add(DirectSkillModule.PSYCHOLOGY_BEHAVIOR)
    return primary, sorted(secondary, key=lambda item: item.value)


def _classify(text: str) -> tuple[list[str], list[str]]:
    folded = text.casefold()
    industries = sorted(
        name for name, terms in _INDUSTRIES if any(term.casefold() in folded for term in terms)
    )
    horizons = sorted(
        name for name, terms in _HORIZONS if any(term.casefold() in folded for term in terms)
    )
    return industries or ["UNKNOWN"], horizons or ["UNKNOWN"]


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        lroot, rroot = self.find(left), self.find(right)
        if lroot != rroot:
            self.parent[max(lroot, rroot)] = min(lroot, rroot)


class SemanticSkillService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.repository = SemanticSkillRepository(state)
        self.completion = KnowledgeCompletionRepository(state)
        self.audit_repository = KnowledgeSkillAuditRepository(state)

    def _parent_release(self, base_run_id: str) -> dict[str, Any]:
        release = self.audit_repository.latest_release(base_run_id)
        if release is None:
            raise ValueError("semantic overlay requires an audited parent registry")
        if not self.objects.verify(str(release["release_object_hash"])):
            raise ValueError("semantic overlay parent registry object is unavailable")
        return dict(release)

    def _candidate_inputs(self, llm_batch_id: str) -> list[dict[str, Any]]:
        rows = self.repository.raw_candidates(llm_batch_id)
        if not rows:
            raise ValueError("semantic overlay has no imported candidates")
        all_argument_ids = sorted(
            {argument_id for row in rows for argument_id in row["argument_unit_ids"]}
        )
        arguments = self.repository.argument_rows(all_argument_ids)
        result: list[dict[str, Any]] = []
        for row in rows:
            payload_hash = str(row["payload_object_hash"])
            if not self.objects.verify(payload_hash):
                raise ValueError(f"candidate payload object is unavailable: {row['candidate_id']}")
            payload = json.loads(self.objects.get_bytes(payload_hash).decode("utf-8"))
            evidence_ids = sorted(set(map(str, payload.get("evidence_paragraph_ids", []))))
            paragraphs = self.repository.paragraph_rows(evidence_ids)
            if set(paragraphs) != set(evidence_ids):
                raise ValueError(f"candidate evidence paragraph is missing: {row['candidate_id']}")
            argument_units = []
            source_hashes = {payload_hash}
            semantic_run_ids: set[str] = set()
            author_source_ids = {str(row["author_source_id"])}
            for argument_id in row["argument_unit_ids"]:
                argument = arguments.get(argument_id)
                if argument is None:
                    raise ValueError(f"candidate argument is missing: {argument_id}")
                unit = json.loads(str(argument["unit_json"]))
                argument_units.append(unit)
                source_hashes.add(str(argument["text_object_hash"]))
                semantic_run_ids.add(str(unit["run_id"]))
                author_source_ids.add(str(unit["author_source_id"]))
            for paragraph in paragraphs.values():
                source_hashes.add(str(paragraph["text_object_hash"]))
            if any(not self.objects.verify(item) for item in source_hashes):
                raise ValueError(f"candidate source object is unavailable: {row['candidate_id']}")
            categories = sorted(set(map(str, payload.get("method_categories", []))))
            text = " ".join(
                [
                    str(payload.get("title", "")),
                    str(payload.get("method_summary", "")),
                    *map(str, payload.get("applicability", [])),
                ]
            )
            primary, secondary = _module(categories, text)
            industries, horizons = _classify(text)
            result.append(
                {
                    "candidate_id": str(row["candidate_id"]),
                    "author_source_ids": sorted(author_source_ids),
                    "semantic_run_ids": sorted(semantic_run_ids),
                    "argument_unit_ids": sorted(set(map(str, row["argument_unit_ids"]))),
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "method_categories": categories,
                    "primary_module": primary,
                    "secondary_modules": secondary,
                    "industries": industries,
                    "horizons": horizons,
                    "source_hashes": sorted(source_hashes),
                    "title_ngrams": _ngrams(str(payload.get("title", ""))),
                    "full_ngrams": _ngrams(text),
                    "normalized_title": _normalize(str(payload.get("title", ""))),
                }
            )
        return result

    @staticmethod
    def _dedup(inputs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        uf = _UnionFind(len(inputs))
        buckets: dict[DirectSkillModule, list[int]] = defaultdict(list)
        for index, item in enumerate(inputs):
            buckets[item["primary_module"]].append(index)
        for indexes in buckets.values():
            for offset, left_index in enumerate(indexes):
                left = inputs[left_index]
                for right_index in indexes[offset + 1 :]:
                    right = inputs[right_index]
                    title_similarity = _jaccard(left["title_ngrams"], right["title_ngrams"])
                    full_similarity = _jaccard(left["full_ngrams"], right["full_ngrams"])
                    same_title = (
                        bool(left["normalized_title"])
                        and left["normalized_title"] == right["normalized_title"]
                    )
                    category_overlap = bool(
                        set(left["method_categories"]) & set(right["method_categories"])
                    )
                    if same_title or (
                        category_overlap
                        and (
                            (title_similarity >= 0.72 and full_similarity >= 0.60)
                            or full_similarity >= 0.82
                        )
                    ):
                        uf.union(left_index, right_index)
        groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for index, item in enumerate(inputs):
            groups[uf.find(index)].append(item)
        return [
            sorted(group, key=lambda item: item["candidate_id"])
            for _, group in sorted(groups.items())
        ]

    @staticmethod
    def _representative(group: list[dict[str, Any]]) -> dict[str, Any]:
        return sorted(
            group,
            key=lambda item: (
                -float(item["payload"].get("model_confidence", 0.0)),
                -len(str(item["payload"].get("method_summary", ""))),
                item["candidate_id"],
            ),
        )[0]

    def generate(
        self,
        base_run_id: str,
        llm_batch_id: str,
    ) -> dict[str, Any]:
        existing = self.repository.latest_generation(base_run_id)
        if existing is not None and str(existing["llm_batch_id"]) == llm_batch_id:
            return self.status(base_run_id)
        parent = self._parent_release(base_run_id)
        inputs = self._candidate_inputs(llm_batch_id)
        semantic_run_ids = sorted(
            {run_id for item in inputs for run_id in item["semantic_run_ids"]}
        )
        if len(semantic_run_ids) != 1:
            raise ValueError("semantic overlay batch must bind exactly one semantic run")
        groups = self._dedup(inputs)
        identity = {
            "base_run_id": base_run_id,
            "parent_registry_release_id": str(parent["release_id"]),
            "parent_registry_object_hash": str(parent["release_object_hash"]),
            "semantic_run_id": semantic_run_ids[0],
            "llm_batch_id": llm_batch_id,
            "policy": POLICY_VERSION,
            "candidate_ids": [item["candidate_id"] for item in inputs],
            "groups": [[item["candidate_id"] for item in group] for group in groups],
        }
        run_id = f"semantic-skill-run:{content_hash(identity)}"
        now = datetime.now(UTC)
        run_artifact_id = f"SemanticSkillGenerationRun:{run_id}"
        run = SemanticSkillGenerationRun(
            run_id=run_id,
            base_run_id=base_run_id,
            parent_registry_release_id=str(parent["release_id"]),
            parent_registry_object_hash=str(parent["release_object_hash"]),
            semantic_run_id=semantic_run_ids[0],
            llm_batch_id=llm_batch_id,
            generation_policy_version=POLICY_VERSION,
            raw_candidate_count=len(inputs),
            effective_skill_count=len(groups),
            author_source_ids=sorted(
                {author for item in inputs for author in item["author_source_ids"]}
            ),
            run_artifact_id=run_artifact_id,
            created_at=now,
        )
        run_json = _json(run.model_dump(mode="json"))
        run_ref = self.objects.put_bytes(run_json.encode("utf-8"))
        artifacts: list[dict[str, Any]] = [
            {
                "artifact_id": run_artifact_id,
                "artifact_type": "SemanticSkillGenerationRun",
                "schema_version": run.schema_version,
                "object_hash": run_ref.sha256,
                "input_hashes": [
                    str(parent["release_object_hash"]),
                    *sorted({item["payload_hash"] for item in inputs}),
                ],
            }
        ]
        group_rows: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        for group in groups:
            rep = self._representative(group)
            payload = rep["payload"]
            candidate_ids = sorted(item["candidate_id"] for item in group)
            argument_ids = sorted(
                {argument_id for item in group for argument_id in item["argument_unit_ids"]}
            )
            authors = sorted({author for item in group for author in item["author_source_ids"]})
            run_ids = sorted({value for item in group for value in item["semantic_run_ids"]})
            source_hashes = sorted({value for item in group for value in item["source_hashes"]})
            categories = sorted({value for item in group for value in item["method_categories"]})
            industries = sorted({value for item in group for value in item["industries"]})
            horizons = sorted({value for item in group for value in item["horizons"]})
            secondary = sorted(
                {value for item in group for value in item["secondary_modules"]},
                key=lambda item: item.value,
            )
            primary = rep["primary_module"]
            secondary = [item for item in secondary if item is not primary]
            group_seed = {
                "run_id": run_id,
                "candidate_ids": candidate_ids,
                "primary_module": primary.value,
                "method_categories": categories,
            }
            digest = sha256_bytes(canonical_json_bytes(group_seed))
            group_id = f"semantic-skill-group:{digest}"
            final_skill_id = f"semantic-skill:{digest}"
            method_summary = str(payload.get("method_summary", "")).strip()
            applicability = sorted(set(map(str, payload.get("applicability", []))))
            counterevidence = sorted(set(map(str, payload.get("counterevidence", []))))
            invalidation = sorted(set(map(str, payload.get("invalidation_conditions", []))))
            confidence = max(
                0.0,
                min(
                    1.0,
                    sum(float(item["payload"].get("model_confidence", 0.0)) for item in group)
                    / len(group),
                ),
            )
            skill_title = str(payload.get("title", "")).strip()
            skill = SemanticEffectiveSkill(
                final_skill_id=final_skill_id,
                skill_name=skill_title or "未命名语义方法",
                primary_module=primary,
                secondary_modules=secondary,
                decision_question=f"何时应使用“{skill_title}”这一研究方法？",
                core_principle=method_summary,
                applicable_conditions=applicability or ["仅用于与来源论证前提一致的研究场景。"],
                reasoning_steps=[
                    "先核对来源论证的适用前提与当前研究对象是否一致。",
                    method_summary,
                    "再检查反证与失效条件，只有证据仍支持时才继续使用。",
                ],
                required_evidence=[
                    "来源 ArgumentUnit 与证据段落的不可变对象哈希。",
                    "涉及上市公司或宏观事实时补充公告、交易所、财报或其他更强来源。",
                ],
                positive_signals=applicability,
                negative_signals=counterevidence,
                invalidation_conditions=invalidation or ["来源核心前提失效时停止使用。"],
                failure_modes=[
                    "把社区作者的事实判断直接当作权威事实。",
                    "脱离适用条件跨公司、跨时期机械套用。",
                    "忽略反证或失效条件，只保留结论。",
                ],
                method_categories=categories,
                applicable_industries=industries,
                holding_horizon=horizons,
                confidence=confidence,
                author_source_ids=authors,
                semantic_run_ids=run_ids,
                candidate_ids=candidate_ids,
                argument_unit_ids=argument_ids,
                source_hashes=source_hashes,
                created_at=now,
            )
            skill_json = _json(skill.model_dump(mode="json"))
            skill_ref = self.objects.put_bytes(skill_json.encode("utf-8"))
            skill_artifact_id = f"SemanticEffectiveSkill:{final_skill_id}"
            generation_audit = SemanticSkillGenerationAudit(
                group_id=group_id,
                final_skill_id=final_skill_id,
                checks=[
                    "ALL_RAW_CANDIDATES_MAPPED_EXACTLY_ONCE",
                    "METHOD_CATEGORY_PRESENT",
                    "SOURCE_OBJECTS_VERIFIED",
                    "EVIDENCE_PARAGRAPHS_BOUND",
                    "INDUSTRY_AND_HORIZON_CLASSIFIED",
                    "COMMUNITY_FACTUAL_USE_REQUIRES_STRONGER_SOURCE",
                    "FORMAL_COMMITTEE_WEIGHT_DISABLED",
                ],
                raw_candidate_count=len(group),
                source_hash_count=len(source_hashes),
                created_at=now,
            )
            audit_json = _json(generation_audit.model_dump(mode="json"))
            audit_ref = self.objects.put_bytes(audit_json.encode("utf-8"))
            audit_artifact_id = f"SemanticSkillGenerationAudit:{group_id}"
            artifacts.extend(
                [
                    {
                        "artifact_id": skill_artifact_id,
                        "artifact_type": "SemanticEffectiveSkill",
                        "schema_version": skill.schema_version,
                        "object_hash": skill_ref.sha256,
                        "input_hashes": source_hashes,
                    },
                    {
                        "artifact_id": audit_artifact_id,
                        "artifact_type": "SemanticSkillGenerationAudit",
                        "schema_version": generation_audit.schema_version,
                        "object_hash": audit_ref.sha256,
                        "input_hashes": [skill_ref.sha256, *source_hashes],
                    },
                ]
            )
            group_rows.append(
                {
                    "group_id": group_id,
                    "run_id": run_id,
                    "final_skill_id": final_skill_id,
                    "skill_name": skill.skill_name,
                    "primary_module": primary.value,
                    "secondary_modules_json": _json([item.value for item in secondary]),
                    "decision_question": skill.decision_question,
                    "core_principle": skill.core_principle,
                    "method_categories_json": _json(categories),
                    "applicable_industries_json": _json(industries),
                    "holding_horizon_json": _json(horizons),
                    "candidate_ids_json": _json(candidate_ids),
                    "argument_unit_ids_json": _json(argument_ids),
                    "author_source_ids_json": _json(authors),
                    "source_hashes_json": _json(source_hashes),
                    "confidence": confidence,
                    "skill_artifact_id": skill_artifact_id,
                    "skill_object_hash": skill_ref.sha256,
                    "skill_json": skill_json,
                    "generation_audit_artifact_id": audit_artifact_id,
                    "generation_audit_object_hash": audit_ref.sha256,
                    "generation_audit_json": audit_json,
                    "generation_audit_status": "PASS",
                    "formal_committee_weight_allowed": 0,
                    "created_at": now.isoformat(),
                }
            )
            mappings.extend(
                {
                    "run_id": run_id,
                    "group_id": group_id,
                    "candidate_id": candidate_id,
                    "ordinal": ordinal,
                }
                for ordinal, candidate_id in enumerate(candidate_ids, start=1)
            )
        self.repository.put_generation(
            run_row={
                "run_id": run.run_id,
                "base_run_id": run.base_run_id,
                "parent_registry_release_id": run.parent_registry_release_id,
                "parent_registry_object_hash": run.parent_registry_object_hash,
                "semantic_run_id": run.semantic_run_id,
                "llm_batch_id": run.llm_batch_id,
                "generation_policy_version": run.generation_policy_version,
                "raw_candidate_count": run.raw_candidate_count,
                "effective_skill_count": run.effective_skill_count,
                "author_source_ids_json": _json(run.author_source_ids),
                "run_artifact_id": run_artifact_id,
                "run_object_hash": run_ref.sha256,
                "run_json": run_json,
                "formal_committee_weight_allowed": 0,
                "created_at": now.isoformat(),
            },
            group_rows=group_rows,
            mappings=mappings,
            artifacts=artifacts,
        )
        return self.status(base_run_id)

    @staticmethod
    def _review(group: dict[str, Any]) -> tuple[SemanticSkillReviewDecision, str]:
        skill = json.loads(str(group["skill_json"]))
        text = f"{group['skill_name']} {group['core_principle']}"
        if float(group["confidence"]) < 0.55:
            return SemanticSkillReviewDecision.REJECT, "独立审阅拒绝：模型置信度低于 0.55。"
        if _DIRECT_CALL.search(text):
            return (
                SemanticSkillReviewDecision.REJECT,
                "独立审阅拒绝：仍包含时间点或价格点位驱动的直接交易指令。",
            )
        if _MOTIVE.search(text):
            return (
                SemanticSkillReviewDecision.REJECT,
                "独立审阅拒绝：包含不可验证主体动机或确定性归因。",
            )
        if not skill.get("invalidation_conditions") or not skill.get("required_evidence"):
            return SemanticSkillReviewDecision.REJECT, "独立审阅拒绝：缺少失效条件或证据要求。"
        return (
            SemanticSkillReviewDecision.APPROVE,
            "独立审阅通过：同义同源已归并，来源哈希完整，分类明确，包含反证/失效条件；社区事实仍要求更强来源复核。",
        )

    def review_all(
        self,
        base_run_id: str,
        *,
        actor: str = "deterministic-semantic-admission-review-v1",
    ) -> dict[str, Any]:
        generation = self.repository.latest_generation(base_run_id)
        if generation is None:
            raise ValueError("semantic Skill generation has not run")
        run_id = str(generation["run_id"])
        existing = {str(row["group_id"]) for row in self.repository.decisions(run_id)}
        now = datetime.now(UTC)
        for group in self.repository.groups(run_id):
            group_id = str(group["group_id"])
            if group_id in existing:
                continue
            decision_value, reason = self._review(group)
            seed = {
                "run_id": run_id,
                "group_id": group_id,
                "skill_object_hash": str(group["skill_object_hash"]),
                "decision": decision_value.value,
                "actor": actor,
                "reason": reason,
            }
            decision_id = f"semantic-skill-review:{sha256_bytes(canonical_json_bytes(seed))}"
            decision = SemanticSkillReviewRecord(
                decision_id=decision_id,
                run_id=run_id,
                group_id=group_id,
                final_skill_id=str(group["final_skill_id"]),
                skill_object_hash=str(group["skill_object_hash"]),
                decision=decision_value,
                actor=actor,
                reason=reason,
                decided_at=now,
            )
            decision_json = _json(decision.model_dump(mode="json"))
            decision_ref = self.objects.put_bytes(decision_json.encode("utf-8"))
            artifact_id = f"SemanticSkillReviewDecision:{decision_id}"
            self.repository.put_decision(
                {
                    "decision_id": decision_id,
                    "run_id": run_id,
                    "group_id": group_id,
                    "final_skill_id": decision.final_skill_id,
                    "skill_object_hash": decision.skill_object_hash,
                    "decision": decision.decision.value,
                    "actor": actor,
                    "reason": reason,
                    "decision_artifact_id": artifact_id,
                    "decision_object_hash": decision_ref.sha256,
                    "decision_json": decision_json,
                    "formal_committee_weight_allowed": 0,
                    "decided_at": now.isoformat(),
                    "created_at": now.isoformat(),
                },
                {
                    "artifact_id": artifact_id,
                    "artifact_type": "SemanticSkillReviewDecision",
                    "schema_version": decision.schema_version,
                    "object_hash": decision_ref.sha256,
                    "input_hashes": [decision.skill_object_hash],
                },
            )
        return self.status(base_run_id)

    def audit(self, base_run_id: str) -> SemanticSkillAuditReport:
        generation = self.repository.latest_generation(base_run_id)
        if generation is None:
            raise ValueError("semantic Skill generation has not run")
        run_id = str(generation["run_id"])
        groups = self.repository.groups(run_id)
        mappings = self.repository.group_candidates(run_id)
        decisions = self.repository.decisions(run_id)
        findings: list[str] = []
        raw_count = int(generation["raw_candidate_count"])
        mapped_ids = [str(row["candidate_id"]) for row in mappings]
        duplicate_membership = len(mapped_ids) - len(set(mapped_ids))
        if len(set(mapped_ids)) != raw_count:
            findings.append("RAW_CANDIDATE_COVERAGE_MISMATCH")
        if len(groups) != int(generation["effective_skill_count"]):
            findings.append("EFFECTIVE_SKILL_COUNT_DRIFT")
        if len(decisions) != len(groups):
            findings.append("REVIEW_NOT_CLOSED")
        broken = 0
        missing_source = 0
        for group in groups:
            if not self.objects.verify(str(group["skill_object_hash"])):
                broken += 1
            if not self.objects.verify(str(group["generation_audit_object_hash"])):
                broken += 1
            source_hashes = json.loads(str(group["source_hashes_json"]))
            if not source_hashes:
                missing_source += 1
            for source_hash in source_hashes:
                if not self.objects.verify(str(source_hash)):
                    broken += 1
        for decision in decisions:
            if not self.objects.verify(str(decision["decision_object_hash"])):
                broken += 1
        parent = self._parent_release(base_run_id)
        if str(parent["release_id"]) != str(generation["parent_registry_release_id"]):
            findings.append("PARENT_REGISTRY_RELEASE_DRIFT")
        if str(parent["release_object_hash"]) != str(generation["parent_registry_object_hash"]):
            findings.append("PARENT_REGISTRY_OBJECT_DRIFT")
        if broken:
            findings.append("BROKEN_OBJECTS")
        if duplicate_membership:
            findings.append("DUPLICATE_CANDIDATE_MEMBERSHIP")
        if missing_source:
            findings.append("MISSING_SOURCE_HASHES")
        approved = sum(str(row["decision"]) == "APPROVE" for row in decisions)
        rejected = sum(str(row["decision"]) == "REJECT" for row in decisions)
        report = SemanticSkillAuditReport(
            run_id=run_id,
            status="PASS" if not findings else "FAIL",
            raw_candidate_count=raw_count,
            mapped_candidate_count=len(set(mapped_ids)),
            effective_skill_count=len(groups),
            decision_count=len(decisions),
            approved_count=approved,
            rejected_count=rejected,
            broken_object_count=broken,
            duplicate_membership_count=duplicate_membership,
            missing_source_hash_count=missing_source,
            finding_codes=sorted(set(findings)),
            created_at=datetime.now(UTC),
        )
        return report

    def publish(self, base_run_id: str) -> dict[str, Any]:
        existing = self.repository.latest_release(base_run_id)
        generation = self.repository.latest_generation(base_run_id)
        if generation is None:
            raise ValueError("semantic Skill generation has not run")
        if existing is not None and str(existing["generation_run_id"]) == str(generation["run_id"]):
            return self.status(base_run_id)
        report = self.audit(base_run_id)
        if report.status != "PASS":
            raise ValueError(f"semantic Skill audit failed: {report.finding_codes}")
        run_id = str(generation["run_id"])
        groups = self.repository.groups(run_id)
        decisions = self.repository.decisions(run_id)
        decision_by_group = {str(row["group_id"]): row for row in decisions}
        approved = [
            row
            for row in groups
            if str(decision_by_group[str(row["group_id"])]["decision"]) == "APPROVE"
        ]
        parent = self._parent_release(base_run_id)
        if (
            str(parent["release_id"]) != str(generation["parent_registry_release_id"])
            or str(parent["release_object_hash"]) != str(generation["parent_registry_object_hash"])
        ):
            raise ValueError("semantic Skill parent registry drift")
        report_json = _json(report.model_dump(mode="json"))
        report_ref = self.objects.put_bytes(report_json.encode("utf-8"))
        report_artifact_id = f"SemanticSkillOverlayAudit:{run_id}"
        decision_ids = sorted(str(row["decision_id"]) for row in decisions)
        release_seed = {
            "schema_version": "knowledge-semantic-overlay-registry-v1",
            "parent_registry_object_hash": str(parent["release_object_hash"]),
            "generation_run_object_hash": str(generation["run_object_hash"]),
            "decision_object_hashes": sorted(str(row["decision_object_hash"]) for row in decisions),
            "approved_skill_object_hashes": sorted(
                str(row["skill_object_hash"]) for row in approved
            ),
            "audit_report_object_hash": report_ref.sha256,
        }
        digest = sha256_bytes(canonical_json_bytes(release_seed))
        release_id = f"knowledge-semantic-overlay:{digest}"
        release_artifact_id = f"knowledge-semantic-overlay-release:{digest}"
        members = [
            SemanticSkillOverlayMember(
                member_ordinal=index,
                group_id=str(row["group_id"]),
                final_skill_id=str(row["final_skill_id"]),
                skill_object_hash=str(row["skill_object_hash"]),
                skill_artifact_id=str(row["skill_artifact_id"]),
                source_hashes=sorted(set(json.loads(str(row["source_hashes_json"])))),
            )
            for index, row in enumerate(
                sorted(approved, key=lambda item: str(item["final_skill_id"])), start=1
            )
        ]
        rejected_count = len(groups) - len(approved)
        parent_count = int(parent["active_skill_count"])
        release = SemanticSkillOverlayRelease(
            release_id=release_id,
            registry_version=f"knowledge-semantic-overlay:{digest[:16]}",
            base_run_id=base_run_id,
            generation_run_id=run_id,
            parent_registry_release_id=str(parent["release_id"]),
            parent_registry_object_hash=str(parent["release_object_hash"]),
            parent_admitted_skill_count=parent_count,
            raw_candidate_count=int(generation["raw_candidate_count"]),
            effective_skill_count=len(groups),
            overlay_approved_count=len(approved),
            overlay_rejected_count=rejected_count,
            overlay_admitted_skill_count=len(approved),
            composite_admitted_skill_count=parent_count + len(approved),
            decision_ids=decision_ids,
            members=members,
            audit_report_artifact_id=report_artifact_id,
            audit_report_object_hash=report_ref.sha256,
            release_artifact_id=release_artifact_id,
            created_at=datetime.now(UTC),
        )
        release_json = _json(release.model_dump(mode="json"))
        release_ref = self.objects.put_bytes(release_json.encode("utf-8"))
        member_rows = [
            {
                "release_id": release_id,
                "member_ordinal": member.member_ordinal,
                "group_id": member.group_id,
                "final_skill_id": member.final_skill_id,
                "skill_object_hash": member.skill_object_hash,
                "skill_artifact_id": member.skill_artifact_id,
                "admission_basis": member.admission_basis,
                "source_hashes_json": _json(member.source_hashes),
            }
            for member in members
        ]
        self.repository.put_release(
            release_row={
                "release_id": release_id,
                "registry_version": release.registry_version,
                "base_run_id": base_run_id,
                "generation_run_id": run_id,
                "parent_registry_release_id": release.parent_registry_release_id,
                "parent_registry_object_hash": release.parent_registry_object_hash,
                "parent_admitted_skill_count": parent_count,
                "raw_candidate_count": release.raw_candidate_count,
                "effective_skill_count": release.effective_skill_count,
                "overlay_approved_count": release.overlay_approved_count,
                "overlay_rejected_count": release.overlay_rejected_count,
                "overlay_admitted_skill_count": release.overlay_admitted_skill_count,
                "composite_admitted_skill_count": release.composite_admitted_skill_count,
                "decision_ids_json": _json(decision_ids),
                "member_ids_json": _json([member.final_skill_id for member in members]),
                "audit_report_artifact_id": report_artifact_id,
                "audit_report_object_hash": report_ref.sha256,
                "release_artifact_id": release_artifact_id,
                "release_object_hash": release_ref.sha256,
                "release_json": release_json,
                "formal_committee_weight_allowed": 0,
                "created_at": release.created_at.isoformat(),
            },
            members=member_rows,
            artifacts=[
                {
                    "artifact_id": report_artifact_id,
                    "artifact_type": "SemanticSkillAuditReport",
                    "schema_version": report.schema_version,
                    "object_hash": report_ref.sha256,
                    "input_hashes": [
                        str(generation["run_object_hash"]),
                        *sorted(str(row["decision_object_hash"]) for row in decisions),
                    ],
                },
                {
                    "artifact_id": release_artifact_id,
                    "artifact_type": "SemanticSkillOverlayRelease",
                    "schema_version": release.schema_version,
                    "object_hash": release_ref.sha256,
                    "input_hashes": [
                        str(parent["release_object_hash"]),
                        str(generation["run_object_hash"]),
                        report_ref.sha256,
                        *sorted(str(row["decision_object_hash"]) for row in decisions),
                        *(member.skill_object_hash for member in members),
                    ],
                },
            ],
        )
        return self.status(base_run_id)

    def status(self, base_run_id: str) -> dict[str, Any]:
        generation = self.repository.latest_generation(base_run_id)
        if generation is None:
            return {"status": "NOT_RUN", "base_run_id": base_run_id}
        run_id = str(generation["run_id"])
        groups = self.repository.groups(run_id)
        decisions = self.repository.decisions(run_id)
        release = self.repository.latest_release(base_run_id)
        approved = sum(str(row["decision"]) == "APPROVE" for row in decisions)
        rejected = sum(str(row["decision"]) == "REJECT" for row in decisions)
        return {
            "status": "PUBLISHED" if release is not None else (
                "REVIEW_CLOSED" if len(decisions) == len(groups) else "GENERATED"
            ),
            "base_run_id": base_run_id,
            "run_id": run_id,
            "semantic_run_id": str(generation["semantic_run_id"]),
            "llm_batch_id": str(generation["llm_batch_id"]),
            "parent_registry_release_id": str(generation["parent_registry_release_id"]),
            "parent_registry_object_hash": str(generation["parent_registry_object_hash"]),
            "raw_candidate_count": int(generation["raw_candidate_count"]),
            "effective_skill_count": len(groups),
            "decision_count": len(decisions),
            "approved_count": approved,
            "rejected_count": rejected,
            "deduplicated_candidate_count": int(generation["raw_candidate_count"]) - len(groups),
            "release_id": str(release["release_id"]) if release else None,
            "release_object_hash": str(release["release_object_hash"]) if release else None,
            "composite_admitted_skill_count": (
                int(release["composite_admitted_skill_count"]) if release else None
            ),
            "formal_committee_weight_allowed": False,
        }


__all__ = ["SemanticSkillService"]
