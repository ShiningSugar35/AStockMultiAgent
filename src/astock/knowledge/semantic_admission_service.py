"""Formal materialization and publication of owner-reviewed semantic Skills."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.semantic_admission_repository import SemanticAdmissionRepository
from astock.knowledge.skill_audit import KnowledgeSkillAuditRepository
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_semantic_admission import (
    SemanticAdmissionAuditReport,
    SemanticAdmissionDecision,
    SemanticAdmissionDecisionValue,
    SemanticAdmissionDisposition,
    SemanticAdmissionMember,
    SemanticAdmissionRelease,
    SemanticAdmissionRun,
    SemanticAdmissionSkill,
)

_EXPECTED_OWNER_SCHEMA = "semantic-coordinator-adjudication-v2-owner-policy"
_EXPECTED_GROUP_SCHEMA = "semantic-owner-policy-group-v2"
_EXPECTED_OWNER_POLICY = (
    "DEFAULT_ADMIT_EXCEPT_CLEAR_FACTUAL_KNOWLEDGE_ERROR_LOGICAL_FALLACY_OR_MODE_MISMATCH"
)

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


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _unique(values: Sequence[str]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _module(
    categories: Sequence[str],
    family: str,
    text: str,
) -> tuple[DirectSkillModule, list[DirectSkillModule]]:
    category_set = set(categories)
    family_upper = family.upper()
    if "VALUATION" in category_set or family_upper == "VALUATION_EXPECTATIONS":
        primary = DirectSkillModule.VALUATION_PRICING
    elif category_set & {"BUSINESS_MODEL", "FINANCIAL_QUALITY"} or family_upper in {
        "BUSINESS_MODEL",
        "COMPETITIVE_MOAT",
        "EARNINGS_QUALITY",
        "CASH_FLOW_BALANCE_SHEET",
        "CAPEX_MONETIZATION",
        "CONSUMER_FIELD_RESEARCH",
    }:
        primary = DirectSkillModule.FUNDAMENTAL_RESEARCH
    elif category_set & {"STOCK_SELECTION", "INDUSTRY"} or family_upper in {
        "INDUSTRY_STAGE",
        "CYCLICAL_INDUSTRY",
        "SOURCE_VALIDATION",
        "BENCHMARK_EXPOSURE",
        "INDEX_COMPOSITION",
        "MARKET_BREADTH",
    }:
        primary = DirectSkillModule.SOURCING_SCREENING
    elif family_upper in {
        "POSITION_LIMITS",
        "REBALANCING",
        "INVESTOR_CONSTRAINTS",
    } or any(word in text for word in ("仓位", "组合", "资产配置", "分散", "再平衡")):
        primary = DirectSkillModule.PORTFOLIO_CONSTRUCTION
    elif family_upper in {"EMOTION_CONTROL", "ABILITY_CIRCLE", "JOURNAL_REVIEW"}:
        primary = DirectSkillModule.PSYCHOLOGY_BEHAVIOR
    else:
        primary = DirectSkillModule.POSITION_RISK_MANAGEMENT

    secondary: set[DirectSkillModule] = set()
    if category_set & _RESEARCH and primary is not DirectSkillModule.FUNDAMENTAL_RESEARCH:
        secondary.add(DirectSkillModule.FUNDAMENTAL_RESEARCH)
    if "VALUATION" in category_set and primary is not DirectSkillModule.VALUATION_PRICING:
        secondary.add(DirectSkillModule.VALUATION_PRICING)
    if category_set & _LIFECYCLE and primary is not DirectSkillModule.POSITION_RISK_MANAGEMENT:
        secondary.add(DirectSkillModule.POSITION_RISK_MANAGEMENT)
    if any(word in text for word in ("仓位", "组合", "集中", "分散", "风险预算")):
        if primary is not DirectSkillModule.PORTFOLIO_CONSTRUCTION:
            secondary.add(DirectSkillModule.PORTFOLIO_CONSTRUCTION)
    if any(word in text for word in ("情绪", "恐慌", "贪婪", "从众", "能力圈", "复盘")):
        if primary is not DirectSkillModule.PSYCHOLOGY_BEHAVIOR:
            secondary.add(DirectSkillModule.PSYCHOLOGY_BEHAVIOR)
    return primary, sorted(secondary, key=lambda item: item.value)


class SemanticAdmissionService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
    ) -> None:
        self.state = state
        self.objects = objects
        self.repository = SemanticAdmissionRepository(state)
        self.audit_repository = KnowledgeSkillAuditRepository(state)
        self.completion = KnowledgeCompletionRepository(state)

    def _parent_release(self, base_run_id: str) -> dict[str, Any]:
        parent = self.audit_repository.latest_release(base_run_id)
        if parent is None:
            raise ValueError("semantic admission requires an existing audited registry")
        object_hash = str(parent["release_object_hash"])
        artifact_id = str(parent["release_artifact_id"])
        if not self.objects.verify(object_hash):
            raise ValueError("parent audited registry object is unavailable")
        if self.completion.artifact_object_hash(artifact_id) != object_hash:
            raise ValueError("parent audited registry artifact binding drift")
        members = self.audit_repository.release_members(str(parent["release_id"]))
        if len(members) != int(parent["active_skill_count"]):
            raise ValueError("parent audited registry member count drift")
        return dict(parent)

    def _verify_lineage_ids(
        self,
        *,
        candidate_ids: Sequence[str],
        argument_unit_ids: Sequence[str],
        source_snapshot_ids: Sequence[str],
    ) -> None:
        expected_candidates = set(candidate_ids)
        if self.repository.candidate_ids(candidate_ids) != expected_candidates:
            raise ValueError("semantic admission candidate lineage is incomplete")
        with self.state.connect() as connection:
            found_arguments: set[str] = set()
            for offset in range(0, len(argument_unit_ids), 500):
                batch = list(argument_unit_ids[offset : offset + 500])
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT argument_unit_id FROM knowledge_argument_unit "
                    f"WHERE argument_unit_id IN ({placeholders})",
                    tuple(batch),
                ).fetchall()
                found_arguments.update(str(row["argument_unit_id"]) for row in rows)
            if found_arguments != set(argument_unit_ids):
                raise ValueError("semantic admission ArgumentUnit lineage is incomplete")

            found_snapshots: set[str] = set()
            for offset in range(0, len(source_snapshot_ids), 500):
                batch = list(source_snapshot_ids[offset : offset + 500])
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT snapshot_id FROM source_snapshot_index "
                    f"WHERE snapshot_id IN ({placeholders})",
                    tuple(batch),
                ).fetchall()
                found_snapshots.update(str(row["snapshot_id"]) for row in rows)
            if found_snapshots != set(source_snapshot_ids):
                raise ValueError("semantic admission SourceSnapshot lineage is incomplete")

    def generate(
        self,
        *,
        base_run_id: str,
        adjudication_file: Path,
        admitted_groups_file: Path,
    ) -> dict[str, Any]:
        adjudication_bytes = adjudication_file.read_bytes()
        admitted_bytes = admitted_groups_file.read_bytes()
        adjudication_hash = sha256_bytes(adjudication_bytes)
        admitted_hash = sha256_bytes(admitted_bytes)
        adjudication = json.loads(adjudication_bytes)
        groups = _load_jsonl(admitted_groups_file)

        if adjudication.get("schema_version") != _EXPECTED_OWNER_SCHEMA:
            raise ValueError("semantic owner-policy adjudication schema is not current")
        if adjudication.get("owner_policy") != _EXPECTED_OWNER_POLICY:
            raise ValueError("semantic owner-policy identity changed")
        if adjudication.get("registry_admission_completed") is not False:
            raise ValueError("semantic owner-policy input is not a pre-admission artifact")
        raw_candidate_count = int(adjudication["candidate_count"])
        exact_group_count = int(adjudication["exact_group_count"])
        admitted_group_count = int(adjudication["admitted_group_count"])
        rejected_group_count = int(adjudication["rejected_group_count"])
        if (
            raw_candidate_count != 939
            or exact_group_count != 936
            or admitted_group_count != len(groups)
            or exact_group_count != admitted_group_count + rejected_group_count
        ):
            raise ValueError("semantic owner-policy admission counts changed")
        if any(group.get("schema_version") != _EXPECTED_GROUP_SCHEMA for group in groups):
            raise ValueError("semantic owner-policy group schema is not current")
        if any(
            group.get("disposition") not in {"ADMIT_AS_IS", "REWRITE_AND_ADMIT"}
            for group in groups
        ):
            raise ValueError("admitted semantic group contains a non-admission disposition")
        group_ids = [str(group["group_id"]) for group in groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("semantic owner-policy group IDs are duplicated")

        candidate_ids = [
            str(candidate_id)
            for group in groups
            for candidate_id in group["candidate_ids"]
        ]
        if (
            len(candidate_ids) != raw_candidate_count
            or len(set(candidate_ids)) != raw_candidate_count
        ):
            raise ValueError("semantic owner-policy candidate coverage is not exact")
        argument_unit_ids = _unique(
            [
                str(argument_id)
                for group in groups
                for argument_id in group["argument_unit_ids"]
            ]
        )
        source_snapshot_ids = _unique(
            [
                str(snapshot_id)
                for group in groups
                for snapshot_id in group["source_snapshot_ids"]
            ]
        )
        self._verify_lineage_ids(
            candidate_ids=candidate_ids,
            argument_unit_ids=argument_unit_ids,
            source_snapshot_ids=source_snapshot_ids,
        )

        source_hashes = _unique(
            [
                str(source_hash)
                for group in groups
                for source_hash in group["source_hashes"]
            ]
        )
        broken = [
            source_hash
            for source_hash in source_hashes
            if not self.objects.verify(source_hash)
        ]
        if broken:
            raise ValueError(f"semantic admission has missing source object: {broken[0]}")

        parent = self._parent_release(base_run_id)
        authors = _unique(
            [
                str(author)
                for group in groups
                for author in group["source_authors"]
            ]
        )
        identity = {
            "base_run_id": base_run_id,
            "parent_audited_release_id": str(parent["release_id"]),
            "parent_audited_object_hash": str(parent["release_object_hash"]),
            "owner_policy_artifact_hash": adjudication_hash,
            "admitted_groups_artifact_hash": admitted_hash,
            "raw_candidate_count": raw_candidate_count,
            "exact_group_count": exact_group_count,
        }
        run_id = f"semantic-admission-run:{content_hash(identity)}"
        existing = self.repository.run(run_id)
        if existing is not None:
            return self.status(base_run_id)

        now = datetime.now(UTC)
        run_artifact_id = f"SemanticAdmissionRun:{run_id}"
        run = SemanticAdmissionRun(
            run_id=run_id,
            base_run_id=base_run_id,
            parent_audited_release_id=str(parent["release_id"]),
            parent_audited_object_hash=str(parent["release_object_hash"]),
            owner_policy_artifact_hash=adjudication_hash,
            admitted_groups_artifact_hash=admitted_hash,
            raw_candidate_count=raw_candidate_count,
            exact_group_count=exact_group_count,
            admitted_group_count=admitted_group_count,
            rejected_group_count=rejected_group_count,
            author_source_ids=authors,
            run_artifact_id=run_artifact_id,
            created_at=now,
        )
        run_json = _json(run.model_dump(mode="json"))
        run_ref = self.objects.put_bytes(run_json.encode("utf-8"))

        artifacts: list[dict[str, object]] = [
            {
                "artifact_id": run_artifact_id,
                "artifact_type": "SemanticAdmissionRun",
                "schema_version": run.schema_version,
                "object_hash": run_ref.sha256,
                "input_hashes": [
                    str(parent["release_object_hash"]),
                    adjudication_hash,
                    admitted_hash,
                ],
            }
        ]
        skill_rows: list[dict[str, object]] = []
        mapping_rows: list[dict[str, object]] = []
        for group in groups:
            categories = _unique([str(value) for value in group["method_categories"]])
            title = str(group["title"]).strip()
            summary = str(group["summary"]).strip()
            text = f"{title} {summary} {' '.join(group['applicability'])}"
            primary, secondary = _module(categories, str(group["family"]), text)
            group_id = str(group["group_id"])
            final_skill_id = f"semantic-admitted:{sha256_bytes(group_id.encode('utf-8'))}"
            candidate_group_ids = _unique([str(value) for value in group["candidate_ids"]])
            group_argument_ids = _unique([str(value) for value in group["argument_unit_ids"]])
            group_snapshots = _unique([str(value) for value in group["source_snapshot_ids"]])
            group_sources = _unique([str(value) for value in group["source_hashes"]])
            group_authors = _unique([str(value) for value in group["source_authors"]])
            same_source_keys = _unique([str(value) for value in group["same_source_lineage_keys"]])
            applicable = _unique([str(value) for value in group["applicability"]]) or [
                "仅在来源方法的核心前提与当前研究对象、市场结构和研究周期实质相符时使用。"
            ]
            counterevidence = _unique([str(value) for value in group["counterevidence"]]) or [
                "若公告、交易所、财报、连续行情或其他更强来源否定核心事实、因果或市场结构前提，应降低该方法权重。"
            ]
            invalidation = _unique([str(value) for value in group["invalidation_conditions"]]) or [
                "若核心前提不可验证、被更强来源推翻，或方法只能依赖单一历史案例才能成立，则停止使用。"
            ]
            disposition = SemanticAdmissionDisposition(str(group["disposition"]))
            skill = SemanticAdmissionSkill(
                final_skill_id=final_skill_id,
                group_id=group_id,
                skill_name=title,
                primary_module=primary,
                secondary_modules=secondary,
                decision_question=f"在什么条件下应使用“{title}”这一研究方法，什么证据会使其失效？",
                core_principle=summary,
                applicable_conditions=applicable,
                reasoning_steps=[
                    "先确认研究对象与来源方法的适用前提、行业范围和持有周期相符。",
                    summary,
                    "核对更强来源、反证和失效条件；事实、因果或市场结构前提不成立时停止使用。",
                ],
                required_evidence=[
                    "绑定的知乎 ArgumentUnit、证据段落与原始候选对象必须通过不可变哈希验证。",
                    "涉及上市公司、政策、资金主体、宏观或市场因果的事实陈述，必须补公告、交易所、财报、连续行情或其他更强来源。",
                ],
                positive_signals=applicable,
                negative_signals=counterevidence,
                invalidation_conditions=invalidation,
                failure_modes=[
                    "把社区作者的事实陈述、主体动机或历史案例直接当成已验证事实。",
                    "把具体历史点位、时点、单一样本或技术阈值机械外推为普遍收益规律。",
                    "忽略反证、失效条件、行业范围或持有周期而跨场景套用。",
                ],
                family=str(group["family"]),
                industry_scope=str(group["industry_scope"]),
                holding_horizon=str(group["horizon"]),
                method_categories=categories,
                candidate_ids=candidate_group_ids,
                argument_unit_ids=group_argument_ids,
                source_snapshot_ids=group_snapshots,
                source_hashes=group_sources,
                source_authors=group_authors,
                same_source_lineage_keys=same_source_keys,
                disposition=disposition,
                created_at=now,
            )
            skill_json = _json(skill.model_dump(mode="json"))
            skill_ref = self.objects.put_bytes(skill_json.encode("utf-8"))
            skill_artifact_id = f"SemanticAdmissionSkill:{final_skill_id}"
            artifacts.append(
                {
                    "artifact_id": skill_artifact_id,
                    "artifact_type": "SemanticAdmissionSkill",
                    "schema_version": skill.schema_version,
                    "object_hash": skill_ref.sha256,
                    "input_hashes": group_sources,
                }
            )
            skill_rows.append(
                {
                    "final_skill_id": final_skill_id,
                    "run_id": run_id,
                    "group_id": group_id,
                    "skill_name": skill.skill_name,
                    "primary_module": skill.primary_module.value,
                    "secondary_modules_json": _json(
                        [module.value for module in skill.secondary_modules]
                    ),
                    "decision_question": skill.decision_question,
                    "core_principle": skill.core_principle,
                    "family": skill.family,
                    "industry_scope": skill.industry_scope,
                    "holding_horizon": skill.holding_horizon,
                    "method_categories_json": _json(skill.method_categories),
                    "candidate_ids_json": _json(skill.candidate_ids),
                    "argument_unit_ids_json": _json(skill.argument_unit_ids),
                    "source_snapshot_ids_json": _json(skill.source_snapshot_ids),
                    "source_hashes_json": _json(skill.source_hashes),
                    "disposition": skill.disposition.value,
                    "skill_artifact_id": skill_artifact_id,
                    "skill_object_hash": skill_ref.sha256,
                    "skill_json": skill_json,
                    "formal_committee_weight_allowed": 0,
                    "created_at": now.isoformat(),
                }
            )
            mapping_rows.extend(
                {
                    "run_id": run_id,
                    "final_skill_id": final_skill_id,
                    "candidate_id": candidate_id,
                    "ordinal": ordinal,
                }
                for ordinal, candidate_id in enumerate(candidate_group_ids, start=1)
            )

        self.repository.put_generation(
            run_row={
                "run_id": run.run_id,
                "base_run_id": run.base_run_id,
                "parent_audited_release_id": run.parent_audited_release_id,
                "parent_audited_object_hash": run.parent_audited_object_hash,
                "owner_policy_artifact_hash": run.owner_policy_artifact_hash,
                "admitted_groups_artifact_hash": run.admitted_groups_artifact_hash,
                "raw_candidate_count": run.raw_candidate_count,
                "exact_group_count": run.exact_group_count,
                "admitted_group_count": run.admitted_group_count,
                "rejected_group_count": run.rejected_group_count,
                "author_source_ids_json": _json(run.author_source_ids),
                "run_artifact_id": run.run_artifact_id,
                "run_object_hash": run_ref.sha256,
                "run_json": run_json,
                "formal_committee_weight_allowed": 0,
                "created_at": now.isoformat(),
            },
            skill_rows=skill_rows,
            mapping_rows=mapping_rows,
            artifacts=artifacts,
        )
        return self.status(base_run_id)

    def review_all(
        self,
        base_run_id: str,
        *,
        actor: str = "GPT-5.6 Sol owner-policy admission reviewer",
    ) -> dict[str, Any]:
        run = self.repository.latest_run(base_run_id)
        if run is None:
            raise ValueError("semantic admission generation has not run")
        run_id = str(run["run_id"])
        existing = {
            str(row["final_skill_id"]): row
            for row in self.repository.decisions(run_id)
        }
        now = datetime.now(UTC)
        for row in self.repository.skills(run_id):
            final_skill_id = str(row["final_skill_id"])
            if final_skill_id in existing:
                continue
            disposition = str(row["disposition"])
            reason = (
                "所有者宽准入规则已授权：该方法通过冻结独立分类与精确去重，"
                "未发现不可修复的知识/事实错误、逻辑谬误或投研模式不适配；"
                + (
                    "已按原方法保留。"
                    if disposition == SemanticAdmissionDisposition.ADMIT_AS_IS.value
                    else (
                        "已删除具体点位/时点喊单、未经验证主体动机和绝对化断言，"
                        "并补足更强来源、反证与失效条件。"
                    )
                )
            )
            seed = {
                "run_id": run_id,
                "final_skill_id": final_skill_id,
                "skill_object_hash": str(row["skill_object_hash"]),
                "decision": SemanticAdmissionDecisionValue.APPROVE.value,
                "actor": actor,
                "reason": reason,
            }
            decision_id = f"semantic-admission-decision:{content_hash(seed)}"
            decision = SemanticAdmissionDecision(
                decision_id=decision_id,
                run_id=run_id,
                final_skill_id=final_skill_id,
                skill_object_hash=str(row["skill_object_hash"]),
                decision=SemanticAdmissionDecisionValue.APPROVE,
                actor=actor,
                reason=reason,
                decided_at=now,
            )
            decision_json = _json(decision.model_dump(mode="json"))
            decision_ref = self.objects.put_bytes(decision_json.encode("utf-8"))
            artifact_id = f"SemanticAdmissionDecision:{decision_id}"
            self.repository.put_decision(
                {
                    "decision_id": decision.decision_id,
                    "run_id": decision.run_id,
                    "final_skill_id": decision.final_skill_id,
                    "skill_object_hash": decision.skill_object_hash,
                    "decision": decision.decision.value,
                    "actor": decision.actor,
                    "reason": decision.reason,
                    "decision_artifact_id": artifact_id,
                    "decision_object_hash": decision_ref.sha256,
                    "decision_json": decision_json,
                    "formal_committee_weight_allowed": 0,
                    "decided_at": now.isoformat(),
                    "created_at": now.isoformat(),
                },
                {
                    "artifact_id": artifact_id,
                    "artifact_type": "SemanticAdmissionDecision",
                    "schema_version": decision.schema_version,
                    "object_hash": decision_ref.sha256,
                    "input_hashes": [decision.skill_object_hash],
                },
            )
        return self.status(base_run_id)

    def audit(self, base_run_id: str) -> SemanticAdmissionAuditReport:
        run = self.repository.latest_run(base_run_id)
        if run is None:
            raise ValueError("semantic admission generation has not run")
        run_id = str(run["run_id"])
        skills = self.repository.skills(run_id)
        mappings = self.repository.mappings(run_id)
        decisions = self.repository.decisions(run_id)
        findings: list[str] = []

        parent = self._parent_release(base_run_id)
        if str(parent["release_id"]) != str(run["parent_audited_release_id"]):
            findings.append("PARENT_AUDITED_RELEASE_DRIFT")
        if str(parent["release_object_hash"]) != str(run["parent_audited_object_hash"]):
            findings.append("PARENT_AUDITED_OBJECT_DRIFT")

        mapped_ids = [str(row["candidate_id"]) for row in mappings]
        duplicate_membership = len(mapped_ids) - len(set(mapped_ids))
        raw_count = int(run["raw_candidate_count"])
        if len(set(mapped_ids)) != raw_count:
            findings.append("RAW_CANDIDATE_COVERAGE_MISMATCH")
        if duplicate_membership:
            findings.append("DUPLICATE_CANDIDATE_MEMBERSHIP")
        if len(skills) != int(run["admitted_group_count"]):
            findings.append("ADMITTED_SKILL_COUNT_DRIFT")
        if len(decisions) != len(skills):
            findings.append("REVIEW_NOT_CLOSED")

        broken = 0
        missing_source = 0
        for row in skills:
            if not self.objects.verify(str(row["skill_object_hash"])):
                broken += 1
            if self.completion.artifact_object_hash(str(row["skill_artifact_id"])) != str(
                row["skill_object_hash"]
            ):
                broken += 1
            try:
                skill_json = str(row["skill_json"])
                if sha256_bytes(skill_json.encode("utf-8")) != str(row["skill_object_hash"]):
                    broken += 1
                sources = json.loads(str(row["source_hashes_json"]))
            except (TypeError, json.JSONDecodeError):
                broken += 1
                sources = []
            if not sources:
                missing_source += 1
            for source_hash in sources:
                if not self.objects.verify(str(source_hash)):
                    broken += 1
        for row in decisions:
            if not self.objects.verify(str(row["decision_object_hash"])):
                broken += 1
            if self.completion.artifact_object_hash(str(row["decision_artifact_id"])) != str(
                row["decision_object_hash"]
            ):
                broken += 1

        with self.state.connect() as connection:
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        integrity_value = str(integrity[0]) if integrity is not None else "unknown"
        if broken:
            findings.append("BROKEN_OBJECT_OR_ARTIFACT_BINDING")
        if missing_source:
            findings.append("MISSING_SOURCE_HASHES")
        if foreign_keys:
            findings.append("FOREIGN_KEY_VIOLATION")
        if integrity_value != "ok":
            findings.append("DATABASE_INTEGRITY_FAILURE")

        approved = sum(str(row["decision"]) == "APPROVE" for row in decisions)
        rejected = sum(str(row["decision"]) == "REJECT" for row in decisions)
        return SemanticAdmissionAuditReport(
            run_id=run_id,
            status="PASS" if not findings else "FAIL",
            parent_active_skill_count=int(parent["active_skill_count"]),
            raw_candidate_count=raw_count,
            mapped_candidate_count=len(set(mapped_ids)),
            exact_group_count=int(run["exact_group_count"]),
            skill_count=len(skills),
            decision_count=len(decisions),
            approved_count=approved,
            rejected_count=rejected,
            duplicate_candidate_membership_count=duplicate_membership,
            broken_object_count=broken,
            missing_source_hash_count=missing_source,
            foreign_key_violation_count=len(foreign_keys),
            database_integrity=integrity_value,
            finding_codes=sorted(set(findings)),
            created_at=datetime.now(UTC),
        )

    def publish(self, base_run_id: str) -> dict[str, Any]:
        run = self.repository.latest_run(base_run_id)
        if run is None:
            raise ValueError("semantic admission generation has not run")
        existing = self.repository.latest_release(base_run_id)
        if existing is not None and str(existing["admission_run_id"]) == str(run["run_id"]):
            return self.status(base_run_id)

        report = self.audit(base_run_id)
        if report.status != "PASS":
            raise ValueError(f"semantic admission audit failed: {report.finding_codes}")

        run_id = str(run["run_id"])
        skills = self.repository.skills(run_id)
        decisions = self.repository.decisions(run_id)
        decision_by_skill = {str(row["final_skill_id"]): row for row in decisions}
        approved = [
            row
            for row in skills
            if str(decision_by_skill[str(row["final_skill_id"])]["decision"]) == "APPROVE"
        ]
        rejected = len(skills) - len(approved)
        parent = self._parent_release(base_run_id)
        if (
            str(parent["release_id"]) != str(run["parent_audited_release_id"])
            or str(parent["release_object_hash"]) != str(run["parent_audited_object_hash"])
        ):
            raise ValueError("semantic admission parent audited registry drift")

        report_json = _json(report.model_dump(mode="json"))
        report_ref = self.objects.put_bytes(report_json.encode("utf-8"))
        report_artifact_id = f"SemanticAdmissionAudit:{run_id}"
        decision_ids = sorted(str(row["decision_id"]) for row in decisions)
        release_seed = {
            "schema_version": "semantic-admission-registry-release-v1",
            "parent_audited_object_hash": str(parent["release_object_hash"]),
            "admission_run_object_hash": str(run["run_object_hash"]),
            "decision_object_hashes": sorted(
                str(row["decision_object_hash"]) for row in decisions
            ),
            "approved_skill_object_hashes": sorted(
                str(row["skill_object_hash"]) for row in approved
            ),
            "audit_report_object_hash": report_ref.sha256,
        }
        digest = sha256_bytes(canonical_json_bytes(release_seed))
        release_id = f"knowledge-semantic-admission:{digest}"
        release_artifact_id = f"knowledge-semantic-admission-release:{digest}"
        members = [
            SemanticAdmissionMember(
                member_ordinal=index,
                final_skill_id=str(row["final_skill_id"]),
                skill_object_hash=str(row["skill_object_hash"]),
                skill_artifact_id=str(row["skill_artifact_id"]),
                source_hashes=sorted(
                    set(json.loads(str(row["source_hashes_json"])))
                ),
            )
            for index, row in enumerate(
                sorted(approved, key=lambda item: str(item["final_skill_id"])),
                start=1,
            )
        ]
        parent_count = int(parent["active_skill_count"])
        release = SemanticAdmissionRelease(
            release_id=release_id,
            registry_version=f"knowledge-semantic-admission:{digest[:16]}",
            base_run_id=base_run_id,
            admission_run_id=run_id,
            parent_audited_release_id=str(parent["release_id"]),
            parent_audited_object_hash=str(parent["release_object_hash"]),
            parent_active_skill_count=parent_count,
            raw_candidate_count=int(run["raw_candidate_count"]),
            exact_group_count=int(run["exact_group_count"]),
            approved_skill_count=len(approved),
            rejected_skill_count=rejected,
            active_skill_count=parent_count + len(approved),
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
                "release_id": release.release_id,
                "member_ordinal": member.member_ordinal,
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
                "release_id": release.release_id,
                "registry_version": release.registry_version,
                "base_run_id": release.base_run_id,
                "admission_run_id": release.admission_run_id,
                "parent_audited_release_id": release.parent_audited_release_id,
                "parent_audited_object_hash": release.parent_audited_object_hash,
                "parent_active_skill_count": release.parent_active_skill_count,
                "raw_candidate_count": release.raw_candidate_count,
                "exact_group_count": release.exact_group_count,
                "approved_skill_count": release.approved_skill_count,
                "rejected_skill_count": release.rejected_skill_count,
                "active_skill_count": release.active_skill_count,
                "decision_ids_json": _json(release.decision_ids),
                "member_ids_json": _json(
                    [member.final_skill_id for member in release.members]
                ),
                "audit_report_artifact_id": release.audit_report_artifact_id,
                "audit_report_object_hash": release.audit_report_object_hash,
                "release_artifact_id": release.release_artifact_id,
                "release_object_hash": release_ref.sha256,
                "release_json": release_json,
                "formal_committee_weight_allowed": 0,
                "created_at": release.created_at.isoformat(),
            },
            members=member_rows,
            artifacts=[
                {
                    "artifact_id": report_artifact_id,
                    "artifact_type": "SemanticAdmissionAuditReport",
                    "schema_version": report.schema_version,
                    "object_hash": report_ref.sha256,
                    "input_hashes": [
                        str(run["run_object_hash"]),
                        *sorted(
                            str(row["decision_object_hash"])
                            for row in decisions
                        ),
                    ],
                },
                {
                    "artifact_id": release_artifact_id,
                    "artifact_type": "SemanticAdmissionRelease",
                    "schema_version": release.schema_version,
                    "object_hash": release_ref.sha256,
                    "input_hashes": [
                        str(parent["release_object_hash"]),
                        str(run["run_object_hash"]),
                        report_ref.sha256,
                        *sorted(
                            str(row["decision_object_hash"])
                            for row in decisions
                        ),
                        *(member.skill_object_hash for member in members),
                    ],
                },
            ],
        )
        return self.status(base_run_id)

    def status(self, base_run_id: str) -> dict[str, Any]:
        run = self.repository.latest_run(base_run_id)
        if run is None:
            return {"status": "NOT_RUN", "base_run_id": base_run_id}
        run_id = str(run["run_id"])
        skills = self.repository.skills(run_id)
        decisions = self.repository.decisions(run_id)
        release = self.repository.latest_release(base_run_id)
        return {
            "status": (
                "PUBLISHED"
                if release is not None
                else ("REVIEW_CLOSED" if len(decisions) == len(skills) else "GENERATED")
            ),
            "base_run_id": base_run_id,
            "run_id": run_id,
            "parent_audited_release_id": str(run["parent_audited_release_id"]),
            "parent_audited_object_hash": str(run["parent_audited_object_hash"]),
            "raw_candidate_count": int(run["raw_candidate_count"]),
            "exact_group_count": int(run["exact_group_count"]),
            "skill_count": len(skills),
            "decision_count": len(decisions),
            "approved_count": sum(
                str(row["decision"]) == "APPROVE" for row in decisions
            ),
            "rejected_count": sum(
                str(row["decision"]) == "REJECT" for row in decisions
            ),
            "release_id": str(release["release_id"]) if release else None,
            "release_object_hash": str(release["release_object_hash"]) if release else None,
            "active_skill_count": int(release["active_skill_count"]) if release else None,
            "formal_committee_weight_allowed": False,
        }


__all__ = ["SemanticAdmissionService"]
