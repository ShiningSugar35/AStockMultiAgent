"""Generate, review, audit, and publish visual-enhanced Zhihu Skills."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.visual_skill_repository import VisualSkillRepository
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_visual import VisualEvidencePack, ZhihuVisualPackStatus
from astock.schemas.knowledge_visual_skills import (
    VisualEnhancedKnowledgeSkill,
    VisualSkillAuditRecord,
    VisualSkillGenerationRun,
    VisualSkillNoSkillRecord,
    VisualSkillOverlayMember,
    VisualSkillOverlayRelease,
    VisualSkillReviewDecision,
    VisualSkillReviewRecord,
)

_REQUIRED_AUTHORS = (
    "zhihu:huang-wei-yan-30",
    "zhihu:mr-dang-77",
    "zhihu:xiao-peng-61-47",
)
_POLICY_VERSION = "zhihu-visual-skill-overlay-v1"
_TOPIC_THRESHOLD = 0.55
_METHOD_THRESHOLD = 0.70
_LINE_RE = re.compile(r"^\[(?P<ordinal>\d+)\|(?P<role>[A-Z_]+)\]\s*(?P<text>.*)$")
_IMAGE_ONLY_RE = re.compile(r"^\[图片(?::[^\]]*)?\]$")

_METHOD_TO_MODULE = {
    "STOCK_SELECTION": DirectSkillModule.SOURCING_SCREENING,
    "BUSINESS_MODEL": DirectSkillModule.FUNDAMENTAL_RESEARCH,
    "INDUSTRY": DirectSkillModule.FUNDAMENTAL_RESEARCH,
    "FINANCIAL_QUALITY": DirectSkillModule.FUNDAMENTAL_RESEARCH,
    "VALUATION": DirectSkillModule.VALUATION_PRICING,
    "ENTRY": DirectSkillModule.PORTFOLIO_CONSTRUCTION,
    "HOLDING": DirectSkillModule.POSITION_RISK_MANAGEMENT,
    "ADD": DirectSkillModule.POSITION_RISK_MANAGEMENT,
    "TRIM": DirectSkillModule.POSITION_RISK_MANAGEMENT,
    "EXIT": DirectSkillModule.POSITION_RISK_MANAGEMENT,
    "RISK": DirectSkillModule.POSITION_RISK_MANAGEMENT,
    "COUNTEREVIDENCE_INVALIDATION": DirectSkillModule.POSITION_RISK_MANAGEMENT,
    "FAILURE_CASE": DirectSkillModule.PSYCHOLOGY_BEHAVIOR,
    "REVIEW": DirectSkillModule.PSYCHOLOGY_BEHAVIOR,
}

_MODULE_QUESTION = {
    DirectSkillModule.SOURCING_SCREENING: "该论证可形成什么候选筛选条件？",
    DirectSkillModule.FUNDAMENTAL_RESEARCH: "该论证对行业、商业模式或财务质量的研究方法是什么？",
    DirectSkillModule.VALUATION_PRICING: "该论证如何用于估值与定价判断？",
    DirectSkillModule.PORTFOLIO_CONSTRUCTION: "该论证如何用于建仓或组合决策？",
    DirectSkillModule.POSITION_RISK_MANAGEMENT: "该论证如何用于持有、加减仓、退出或风险控制？",
    DirectSkillModule.PSYCHOLOGY_BEHAVIOR: "该论证揭示了什么失败模式、行为偏差或复盘规则？",
}

_MODULE_LABEL = {
    DirectSkillModule.SOURCING_SCREENING: "筛选",
    DirectSkillModule.FUNDAMENTAL_RESEARCH: "基本面",
    DirectSkillModule.VALUATION_PRICING: "估值",
    DirectSkillModule.PORTFOLIO_CONSTRUCTION: "组合",
    DirectSkillModule.POSITION_RISK_MANAGEMENT: "风控",
    DirectSkillModule.PSYCHOLOGY_BEHAVIOR: "行为",
}

_ROLE_PRIORITY = (
    "OPERATIONAL_RULE",
    "CONCLUSION",
    "CAUSAL_REASON",
    "CLAIM",
    "EVIDENCE",
    "EXPLANATION",
    "COUNTERARGUMENT",
    "MARKET_OBSERVATION",
    "BACKGROUND",
    "QUESTION",
    "TITLE",
)


class VisualSkillService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.repository = VisualSkillRepository(state)
        self.completion = KnowledgeCompletionRepository(state)

    def _ready_packs(self) -> dict[str, tuple[str, str, VisualEvidencePack]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,object_hash,created_at FROM artifact_registry "
                "WHERE type='VisualEvidencePack' ORDER BY created_at,artifact_id"
            ).fetchall()
        selected: dict[str, tuple[str, str, VisualEvidencePack]] = {}
        for row in rows:
            object_hash = str(row["object_hash"])
            if not self.objects.verify(object_hash):
                continue
            try:
                pack = VisualEvidencePack.model_validate_json(self.objects.get_bytes(object_hash))
            except (ValueError, json.JSONDecodeError):
                continue
            if pack.author_source_id not in _REQUIRED_AUTHORS:
                continue
            if pack.status is not ZhihuVisualPackStatus.READY:
                continue
            if pack.blocked_count or pack.needs_review_count:
                continue
            if pack.image_reference_count != pack.placement_count:
                continue
            selected[pack.author_source_id] = (str(row["artifact_id"]), object_hash, pack)
        missing = sorted(set(_REQUIRED_AUTHORS) - set(selected))
        if missing:
            raise ValueError(f"READY VisualEvidencePack missing for authors: {missing}")
        return selected

    def _author_display_name(self, source_id: str) -> str:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT display_name FROM knowledge_source_identity WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return str(row["display_name"]) if row is not None else source_id

    def generate(self, base_run_id: str) -> dict[str, Any]:
        base_release = self.completion.registry_release(base_run_id)
        if base_release is None:
            raise ValueError("visual Skill overlay requires an existing base registry release")
        base_release_hash = str(base_release["release_object_hash"])
        if not self.objects.verify(base_release_hash):
            raise ValueError("base registry object is missing")
        packs = self._ready_packs()
        pack_hashes = sorted(item[1] for item in packs.values())
        seed = {
            "policy_version": _POLICY_VERSION,
            "base_run_id": base_run_id,
            "base_registry_release_id": str(base_release["release_id"]),
            "base_registry_object_hash": base_release_hash,
            "visual_pack_object_hashes": pack_hashes,
        }
        run_digest = sha256_bytes(canonical_json_bytes(seed))
        run_id = f"visual-skill-run:{run_digest}"
        existing = self.repository.generation_run(run_id)
        if existing is not None:
            return self.status(base_run_id)

        anchor = max(pack.created_at for _, _, pack in packs.values())
        grouped_rows: dict[str, list[dict[str, Any]]] = {}
        for author_source_id in _REQUIRED_AUTHORS:
            rows = self.repository.visual_argument_rows(author_source_id)
            pack = packs[author_source_id][2]
            rows = [row for row in rows if str(row["semantic_run_id"]) == pack.semantic_run_id]
            by_argument: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                by_argument[str(row["argument_unit_id"])].append(row)
            for argument_id, argument_rows in by_argument.items():
                grouped_rows[f"{author_source_id}:{argument_id}"] = argument_rows

        candidates: list[dict[str, Any]] = []
        no_skills: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        semantic_fingerprints: set[str] = set()
        for key in sorted(grouped_rows):
            rows = grouped_rows[key]
            candidate, no_skill, candidate_artifacts = self._evaluate_argument(
                run_id=run_id,
                rows=rows,
                anchor=anchor,
                semantic_fingerprints=semantic_fingerprints,
            )
            if candidate is not None:
                candidates.append(candidate)
                artifacts.extend(candidate_artifacts)
            else:
                assert no_skill is not None
                no_skills.append(no_skill)

        run_artifact_id = f"VisualSkillGenerationRun:{run_id}"
        generation = VisualSkillGenerationRun(
            run_id=run_id,
            base_run_id=base_run_id,
            base_registry_release_id=str(base_release["release_id"]),
            base_registry_object_hash=base_release_hash,
            generation_policy_version=_POLICY_VERSION,
            author_source_ids=sorted(_REQUIRED_AUTHORS),
            semantic_run_ids=sorted({item[2].semantic_run_id for item in packs.values()}),
            visual_pack_artifact_ids=sorted(item[0] for item in packs.values()),
            visual_pack_object_hashes=pack_hashes,
            evaluated_argument_count=len(grouped_rows),
            candidate_count=len(candidates),
            no_skill_count=len(no_skills),
            run_artifact_id=run_artifact_id,
            created_at=anchor,
        )
        run_json = canonical_json_bytes(generation.model_dump(mode="json")).decode("utf-8")
        run_ref = self.objects.put_bytes(run_json.encode("utf-8"))
        artifacts.append(
            {
                "artifact_id": run_artifact_id,
                "artifact_type": "VisualSkillGenerationRun",
                "schema_version": generation.schema_version,
                "object_hash": run_ref.sha256,
                "input_hashes": [base_release_hash, *pack_hashes],
            }
        )
        self.repository.put_generation(
            run_row={
                "run_id": run_id,
                "base_run_id": base_run_id,
                "base_registry_release_id": str(base_release["release_id"]),
                "base_registry_object_hash": base_release_hash,
                "generation_policy_version": _POLICY_VERSION,
                "author_source_ids_json": self._json(sorted(_REQUIRED_AUTHORS)),
                "semantic_run_ids_json": self._json(generation.semantic_run_ids),
                "visual_pack_artifact_ids_json": self._json(generation.visual_pack_artifact_ids),
                "visual_pack_object_hashes_json": self._json(pack_hashes),
                "evaluated_argument_count": len(grouped_rows),
                "candidate_count": len(candidates),
                "no_skill_count": len(no_skills),
                "run_artifact_id": run_artifact_id,
                "run_object_hash": run_ref.sha256,
                "run_json": run_json,
                "created_at": anchor.isoformat(),
            },
            candidates=candidates,
            no_skills=no_skills,
            artifacts=artifacts,
        )
        return self.status(base_run_id)

    def _evaluate_argument(
        self,
        *,
        run_id: str,
        rows: list[dict[str, Any]],
        anchor: datetime,
        semantic_fingerprints: set[str],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
        first = rows[0]
        unit = json.loads(str(first["unit_json"]))
        method_categories = [str(item) for item in unit.get("method_categories", [])]
        topic = float(first["topic_relevance"])
        completeness = float(first["methodological_completeness"])
        reasons: list[str] = []
        if not method_categories:
            reasons.append("METHOD_CATEGORY_MISSING")
        if topic < _TOPIC_THRESHOLD:
            reasons.append("TOPIC_RELEVANCE_BELOW_0_55")
        if completeness < _METHOD_THRESHOLD:
            reasons.append("METHODOLOGICAL_COMPLETENESS_BELOW_0_70")
        argument_hash = str(first["argument_text_object_hash"])
        if not self.objects.verify(argument_hash):
            reasons.append("ARGUMENT_TEXT_OBJECT_MISSING")
            argument_text = ""
        else:
            argument_text = self.objects.get_bytes(argument_hash).decode("utf-8", "strict")
        core_principle, reasoning_steps, title = self._argument_semantics(argument_text)
        if len(core_principle) < 20:
            reasons.append("CORE_PRINCIPLE_TOO_SHORT")

        source_hashes = {argument_hash}
        rebuilt_hashes: set[str] = set()
        placement_ids: set[str] = set()
        packet_artifact_ids: set[str] = set()
        packet_hashes: set[str] = set()
        image_hashes: set[str] = set()
        source_snapshot_ids: set[str] = set()
        classification_confidences: list[float] = []
        for row in rows:
            if str(row["packet_status"]) != "READY":
                reasons.append("VISUAL_PACKET_NOT_READY")
            rebuilt = str(row["rebuilt_argument_object_hash"] or "")
            if not rebuilt or not self.objects.verify(rebuilt):
                reasons.append("REBUILT_ARGUMENT_OBJECT_MISSING")
            else:
                rebuilt_hashes.add(rebuilt)
                source_hashes.add(rebuilt)
            packet_hash = str(row["packet_object_hash"])
            image_hash = str(row["image_object_hash"])
            snapshot_hash = str(row["source_snapshot_object_hash"])
            classification_hash = str(row["classification_object_hash"])
            for value, code in (
                (packet_hash, "VISUAL_PACKET_OBJECT_MISSING"),
                (image_hash, "VISUAL_IMAGE_OBJECT_MISSING"),
                (snapshot_hash, "SOURCE_SNAPSHOT_OBJECT_MISSING"),
                (classification_hash, "VISUAL_CLASSIFICATION_OBJECT_MISSING"),
            ):
                if not self.objects.verify(value):
                    reasons.append(code)
                else:
                    source_hashes.add(value)
            ocr_hash = row["ocr_text_object_hash"]
            if ocr_hash is not None:
                ocr_text_hash = str(ocr_hash)
                if not self.objects.verify(ocr_text_hash):
                    reasons.append("OCR_TEXT_OBJECT_MISSING")
                else:
                    source_hashes.add(ocr_text_hash)
            placement_ids.add(str(row["placement_id"]))
            packet_artifact_ids.add(str(row["packet_artifact_id"]))
            packet_hashes.add(packet_hash)
            image_hashes.add(image_hash)
            source_snapshot_ids.add(str(row["source_snapshot_id"]))
            classification_confidences.append(float(row["classification_confidence"]))

        if reasons:
            return None, self._no_skill(run_id, first, reasons, anchor), []
        modules = sorted(
            {_METHOD_TO_MODULE[item] for item in method_categories if item in _METHOD_TO_MODULE},
            key=lambda item: item.value,
        )
        if not modules:
            return None, self._no_skill(run_id, first, ["MODULE_MAPPING_MISSING"], anchor), []
        primary = modules[0]
        secondary = modules[1:]
        fingerprint = sha256_bytes(
            canonical_json_bytes(
                {
                    "primary_module": primary.value,
                    "core": self._semantic_fold(core_principle),
                }
            )
        )
        if fingerprint in semantic_fingerprints:
            return None, self._no_skill(run_id, first, ["DUPLICATE_VISUAL_SKILL"], anchor), []
        semantic_fingerprints.add(fingerprint)

        author = str(first["author_source_id"])
        display_name = self._author_display_name(author)
        skill_name = self._skill_name(display_name, primary, title or core_principle)
        confidence = round(
            min(
                0.95,
                max(
                    0.55,
                    (topic + completeness + sum(classification_confidences) / len(rows)) / 3,
                ),
            ),
            4,
        )
        skill_seed = {
            "run_id": run_id,
            "argument_unit_id": str(first["argument_unit_id"]),
            "semantic_fingerprint": fingerprint,
        }
        skill_digest = sha256_bytes(canonical_json_bytes(skill_seed))
        final_skill_id = f"visual-skill:{skill_digest}"
        candidate_id = f"visual-skill-candidate:{skill_digest}"
        skill = VisualEnhancedKnowledgeSkill(
            final_skill_id=final_skill_id,
            skill_name=skill_name,
            primary_module=primary,
            secondary_modules=secondary,
            decision_question=_MODULE_QUESTION[primary],
            core_principle=core_principle,
            applicable_conditions=[
                "仅在该方法类别与当前研究问题匹配，且图片与相邻正文属于同一 SourceItem 时使用。",
                "社区作者观点只作为研究方法和线索，不直接作为上市公司事实。",
            ],
            reasoning_steps=reasoning_steps,
            required_evidence=[
                "原始 source snapshot 与精确正文/图片 locator。",
                "READY VisualEvidencePack、不可变原图 hash、OCR/分类和双侧上下文。",
                "涉及公司关键事实时，必须补公告、交易所、财报等更强来源。",
            ],
            invalidation_conditions=[
                "任一 source/packet/image/rebuilt-AU hash 校验失败。",
                "图片与相邻正文不再属于同一 SourceItem 或上下文边界漂移。",
                "更强官方证据与社区作者表述冲突。",
            ],
            failure_modes=[
                "脱离前后文单独解释图片。",
                "把 OCR 文本或社区作者判断直接当作上市公司事实。",
                "只因图片形态相似就跨公司、跨时期机械套用。",
            ],
            confidence=confidence,
            author_source_id=author,
            semantic_run_id=str(first["semantic_run_id"]),
            argument_unit_id=str(first["argument_unit_id"]),
            argument_text_object_hash=argument_hash,
            rebuilt_argument_object_hashes=sorted(rebuilt_hashes),
            placement_ids=sorted(placement_ids),
            visual_packet_artifact_ids=sorted(packet_artifact_ids),
            visual_packet_object_hashes=sorted(packet_hashes),
            image_object_hashes=sorted(image_hashes),
            source_snapshot_ids=sorted(source_snapshot_ids),
            source_hashes=sorted(source_hashes),
            created_at=anchor,
        )
        skill_json = canonical_json_bytes(skill.model_dump(mode="json")).decode("utf-8")
        skill_ref = self.objects.put_bytes(skill_json.encode("utf-8"))
        skill_artifact_id = f"VisualEnhancedKnowledgeSkill:{final_skill_id}"
        audit = VisualSkillAuditRecord(
            candidate_id=candidate_id,
            final_skill_id=final_skill_id,
            run_id=run_id,
            argument_unit_id=skill.argument_unit_id,
            checks=[
                "METHOD_CATEGORY_PRESENT",
                "TOPIC_RELEVANCE_GE_0_55",
                "METHODOLOGICAL_COMPLETENESS_GE_0_70",
                "ALL_VISUAL_PACKETS_READY",
                "ALL_LINEAGE_OBJECTS_VERIFIED",
                "MERGE_WITH_BOTH",
                "COMMUNITY_FACTUAL_USE_REQUIRES_STRONGER_SOURCE",
            ],
            topic_relevance=topic,
            methodological_completeness=completeness,
            visual_packet_count=len(packet_hashes),
            source_hash_count=len(source_hashes),
            created_at=anchor,
        )
        audit_json = canonical_json_bytes(audit.model_dump(mode="json")).decode("utf-8")
        audit_ref = self.objects.put_bytes(audit_json.encode("utf-8"))
        audit_artifact_id = f"VisualEnhancedSkillAudit:{candidate_id}"
        candidate = {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "author_source_id": author,
            "semantic_run_id": skill.semantic_run_id,
            "argument_unit_id": skill.argument_unit_id,
            "final_skill_id": final_skill_id,
            "skill_name": skill.skill_name,
            "primary_module": primary.value,
            "secondary_modules_json": self._json([item.value for item in secondary]),
            "decision_question": skill.decision_question,
            "core_principle": skill.core_principle,
            "confidence": confidence,
            "source_hashes_json": self._json(skill.source_hashes),
            "skill_artifact_id": skill_artifact_id,
            "skill_object_hash": skill_ref.sha256,
            "skill_json": skill_json,
            "audit_artifact_id": audit_artifact_id,
            "audit_object_hash": audit_ref.sha256,
            "audit_json": audit_json,
            "created_at": anchor.isoformat(),
        }
        artifacts = [
            {
                "artifact_id": skill_artifact_id,
                "artifact_type": "VisualEnhancedKnowledgeSkill",
                "schema_version": skill.schema_version,
                "object_hash": skill_ref.sha256,
                "input_hashes": skill.source_hashes,
            },
            {
                "artifact_id": audit_artifact_id,
                "artifact_type": "VisualEnhancedSkillAudit",
                "schema_version": audit.schema_version,
                "object_hash": audit_ref.sha256,
                "input_hashes": [skill_ref.sha256, *skill.source_hashes],
            },
        ]
        return candidate, None, artifacts

    def _no_skill(
        self,
        run_id: str,
        first: dict[str, Any],
        reason_codes: list[str],
        anchor: datetime,
    ) -> dict[str, Any]:
        record = VisualSkillNoSkillRecord(
            run_id=run_id,
            author_source_id=str(first["author_source_id"]),
            semantic_run_id=str(first["semantic_run_id"]),
            argument_unit_id=str(first["argument_unit_id"]),
            reason_codes=sorted(set(reason_codes)),
            created_at=anchor,
        )
        record_json = canonical_json_bytes(record.model_dump(mode="json")).decode("utf-8")
        ref = self.objects.put_bytes(record_json.encode("utf-8"))
        return {
            "run_id": run_id,
            "argument_unit_id": record.argument_unit_id,
            "author_source_id": record.author_source_id,
            "reason_codes_json": self._json(record.reason_codes),
            "record_object_hash": ref.sha256,
            "record_json": record_json,
            "created_at": anchor.isoformat(),
        }

    def review_all(
        self,
        base_run_id: str,
        *,
        actor: str = "GPT-5.6 Sol (user-delegated Phase 5 completion)",
    ) -> dict[str, Any]:
        generation = self.repository.latest_generation_for_base(base_run_id)
        if generation is None:
            raise ValueError("visual Skill generation has not run")
        run_id = str(generation["run_id"])
        existing = {
            str(row["candidate_id"]): row
            for row in self.repository.review_decisions(run_id)
        }
        now = datetime.now(UTC)
        for candidate in self.repository.candidates(run_id):
            candidate_id = str(candidate["candidate_id"])
            if candidate_id in existing:
                continue
            seed = {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "skill_object_hash": str(candidate["skill_object_hash"]),
                "decision": "APPROVE",
                "actor": actor,
            }
            decision_id = f"visual-skill-review:{sha256_bytes(canonical_json_bytes(seed))}"
            decision = VisualSkillReviewRecord(
                decision_id=decision_id,
                run_id=run_id,
                candidate_id=candidate_id,
                final_skill_id=str(candidate["final_skill_id"]),
                skill_object_hash=str(candidate["skill_object_hash"]),
                decision=VisualSkillReviewDecision.APPROVE,
                actor=actor,
                reason=(
                    "User explicitly delegated complete Phase 5 closure; candidate passed the "
                    "strict visual-lineage, method-value, source-hash, and community-source audit."
                ),
                created_at=now,
            )
            decision_json = canonical_json_bytes(decision.model_dump(mode="json")).decode("utf-8")
            decision_ref = self.objects.put_bytes(decision_json.encode("utf-8"))
            artifact_id = f"VisualEnhancedSkillReviewDecision:{decision_id}"
            self.repository.put_review_decision(
                row={
                    "decision_id": decision_id,
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "final_skill_id": decision.final_skill_id,
                    "skill_object_hash": decision.skill_object_hash,
                    "decision": decision.decision.value,
                    "actor": actor,
                    "reason": decision.reason,
                    "decision_artifact_id": artifact_id,
                    "decision_object_hash": decision_ref.sha256,
                    "decision_json": decision_json,
                    "decided_at": now.isoformat(),
                    "created_at": now.isoformat(),
                },
                artifact={
                    "artifact_id": artifact_id,
                    "artifact_type": "VisualEnhancedSkillReviewDecision",
                    "schema_version": decision.schema_version,
                    "object_hash": decision_ref.sha256,
                    "input_hashes": [decision.skill_object_hash],
                },
            )
        return self.status(base_run_id)

    def publish(self, base_run_id: str) -> dict[str, Any]:
        generation = self.repository.latest_generation_for_base(base_run_id)
        if generation is None:
            raise ValueError("visual Skill generation has not run")
        run_id = str(generation["run_id"])
        existing_release = self.repository.latest_release(base_run_id)
        if (
            existing_release is not None
            and str(existing_release["generation_run_id"]) == run_id
        ):
            return self.status(base_run_id)
        candidates = self.repository.candidates(run_id)
        decisions = self.repository.review_decisions(run_id)
        if len(decisions) != len(candidates):
            raise ValueError("visual Skill review is not closed")
        decision_by_candidate = {str(row["candidate_id"]): row for row in decisions}
        approved = [
            row
            for row in candidates
            if str(decision_by_candidate[str(row["candidate_id"])]["decision"]) == "APPROVE"
        ]
        rejected_count = len(candidates) - len(approved)
        base_release = self.completion.registry_release(base_run_id)
        if base_release is None:
            raise ValueError("base registry release is missing")
        if str(base_release["release_id"]) != str(generation["base_registry_release_id"]):
            raise ValueError("base registry release identity drift")
        if str(base_release["release_object_hash"]) != str(generation["base_registry_object_hash"]):
            raise ValueError("base registry object hash drift")
        base_admitted = int(base_release["admitted_skill_count"])
        decision_ids = sorted(str(row["decision_id"]) for row in decisions)
        release_seed = {
            "schema_version": "knowledge-skill-composite-registry-release-v2",
            "base_registry_object_hash": str(base_release["release_object_hash"]),
            "generation_run_object_hash": str(generation["run_object_hash"]),
            "decision_object_hashes": sorted(str(row["decision_object_hash"]) for row in decisions),
            "approved_skill_object_hashes": sorted(
                str(row["skill_object_hash"]) for row in approved
            ),
        }
        digest = sha256_bytes(canonical_json_bytes(release_seed))
        release_id = f"knowledge-registry-v2:{digest}"
        artifact_id = f"knowledge-registry-composite:{digest}"
        members = [
            VisualSkillOverlayMember(
                member_ordinal=index,
                candidate_id=str(row["candidate_id"]),
                final_skill_id=str(row["final_skill_id"]),
                skill_object_hash=str(row["skill_object_hash"]),
                skill_artifact_id=str(row["skill_artifact_id"]),
                source_hashes=sorted(set(json.loads(str(row["source_hashes_json"])))),
            )
            for index, row in enumerate(
                sorted(approved, key=lambda item: str(item["final_skill_id"])),
                start=1,
            )
        ]
        now = datetime.now(UTC)
        release = VisualSkillOverlayRelease(
            release_id=release_id,
            registry_version=f"knowledge-registry-v2:{digest[:16]}",
            base_run_id=base_run_id,
            generation_run_id=run_id,
            base_registry_release_id=str(base_release["release_id"]),
            base_registry_object_hash=str(base_release["release_object_hash"]),
            base_admitted_skill_count=base_admitted,
            overlay_candidate_count=len(candidates),
            overlay_approved_count=len(approved),
            overlay_rejected_count=rejected_count,
            overlay_admitted_skill_count=len(approved),
            composite_admitted_skill_count=base_admitted + len(approved),
            decision_ids=decision_ids,
            members=members,
            release_artifact_id=artifact_id,
            created_at=now,
        )
        release_json = canonical_json_bytes(release.model_dump(mode="json")).decode("utf-8")
        release_ref = self.objects.put_bytes(release_json.encode("utf-8"))
        member_rows = [
            {
                "release_id": release_id,
                "member_ordinal": member.member_ordinal,
                "candidate_id": member.candidate_id,
                "final_skill_id": member.final_skill_id,
                "skill_object_hash": member.skill_object_hash,
                "skill_artifact_id": member.skill_artifact_id,
                "source_hashes_json": self._json(member.source_hashes),
            }
            for member in members
        ]
        input_hashes = sorted(
            {
                str(base_release["release_object_hash"]),
                str(generation["run_object_hash"]),
                *(str(row["decision_object_hash"]) for row in decisions),
                *(member.skill_object_hash for member in members),
            }
        )
        self.repository.put_release(
            release_row={
                "release_id": release_id,
                "registry_version": release.registry_version,
                "base_run_id": base_run_id,
                "generation_run_id": run_id,
                "base_registry_release_id": release.base_registry_release_id,
                "base_registry_object_hash": release.base_registry_object_hash,
                "base_admitted_skill_count": base_admitted,
                "overlay_candidate_count": len(candidates),
                "overlay_approved_count": len(approved),
                "overlay_rejected_count": rejected_count,
                "overlay_admitted_skill_count": len(approved),
                "composite_admitted_skill_count": base_admitted + len(approved),
                "decision_ids_json": self._json(decision_ids),
                "member_ids_json": self._json([member.final_skill_id for member in members]),
                "release_artifact_id": artifact_id,
                "release_object_hash": release_ref.sha256,
                "release_json": release_json,
                "created_at": now.isoformat(),
            },
            members=member_rows,
            release_artifact={
                "artifact_id": artifact_id,
                "artifact_type": "KnowledgeSkillCompositeRegistryRelease",
                "schema_version": release.schema_version,
                "object_hash": release_ref.sha256,
                "input_hashes": input_hashes,
            },
        )
        return self.status(base_run_id)

    def audit(self, base_run_id: str) -> dict[str, Any]:
        findings: list[str] = []
        generation = self.repository.latest_generation_for_base(base_run_id)
        if generation is None:
            return {"status": "FAIL", "findings": ["VISUAL_SKILL_GENERATION_MISSING"]}
        run_id = str(generation["run_id"])
        if not self.objects.verify(str(generation["run_object_hash"])):
            findings.append("VISUAL_SKILL_RUN_OBJECT_MISSING")
        candidates = self.repository.candidates(run_id)
        no_skills = self.repository.no_skills(run_id)
        if len(candidates) != int(generation["candidate_count"]):
            findings.append("VISUAL_SKILL_CANDIDATE_COUNT_DRIFT")
        if len(no_skills) != int(generation["no_skill_count"]):
            findings.append("VISUAL_NO_SKILL_COUNT_DRIFT")
        if len(candidates) + len(no_skills) != int(generation["evaluated_argument_count"]):
            findings.append("VISUAL_SKILL_EVALUATED_COUNT_DRIFT")
        for row in candidates:
            for column, code in (
                ("skill_object_hash", "VISUAL_SKILL_OBJECT_MISSING"),
                ("audit_object_hash", "VISUAL_SKILL_AUDIT_OBJECT_MISSING"),
            ):
                if not self.objects.verify(str(row[column])):
                    findings.append(f"{code}:{row['final_skill_id']}")
            source_hashes = json.loads(str(row["source_hashes_json"]))
            if not all(self.objects.verify(str(item)) for item in source_hashes):
                findings.append(f"VISUAL_SKILL_SOURCE_OBJECT_MISSING:{row['final_skill_id']}")
            if str(row["audit_status"]) != "PASS":
                findings.append(f"VISUAL_SKILL_AUDIT_NOT_PASS:{row['final_skill_id']}")
        for row in no_skills:
            if not self.objects.verify(str(row["record_object_hash"])):
                findings.append(f"VISUAL_NO_SKILL_OBJECT_MISSING:{row['argument_unit_id']}")
        decisions = self.repository.review_decisions(run_id)
        if decisions and len(decisions) != len(candidates):
            findings.append("VISUAL_SKILL_REVIEW_NOT_CLOSED")
        for row in decisions:
            if not self.objects.verify(str(row["decision_object_hash"])):
                findings.append(f"VISUAL_SKILL_REVIEW_OBJECT_MISSING:{row['candidate_id']}")
        release = self.repository.latest_release(base_run_id)
        if release is not None:
            if str(release["generation_run_id"]) != run_id:
                findings.append("VISUAL_SKILL_RELEASE_RUN_DRIFT")
            if not self.objects.verify(str(release["release_object_hash"])):
                findings.append("VISUAL_SKILL_RELEASE_OBJECT_MISSING")
            members = self.repository.release_members(str(release["release_id"]))
            if len(members) != int(release["overlay_admitted_skill_count"]):
                findings.append("VISUAL_SKILL_RELEASE_MEMBER_COUNT_DRIFT")
            if int(release["composite_admitted_skill_count"]) != (
                int(release["base_admitted_skill_count"])
                + int(release["overlay_admitted_skill_count"])
            ):
                findings.append("VISUAL_SKILL_COMPOSITE_COUNT_DRIFT")
        packs = self._ready_packs()
        if set(packs) != set(_REQUIRED_AUTHORS):
            findings.append("VISUAL_PACK_AUTHOR_COVERAGE_DRIFT")
        return {
            "status": "PASS" if not findings else "FAIL",
            "base_run_id": base_run_id,
            "generation_run_id": run_id,
            "evaluated_argument_count": int(generation["evaluated_argument_count"]),
            "candidate_count": len(candidates),
            "no_skill_count": len(no_skills),
            "review_decision_count": len(decisions),
            "release_id": str(release["release_id"]) if release else None,
            "composite_admitted_skill_count": (
                int(release["composite_admitted_skill_count"]) if release else None
            ),
            "findings": sorted(set(findings)),
            "formal_committee_weight_allowed": False,
        }

    def status(self, base_run_id: str) -> dict[str, Any]:
        generation = self.repository.latest_generation_for_base(base_run_id)
        release = self.repository.latest_release(base_run_id)
        if generation is None:
            return {
                "status": "NOT_GENERATED",
                "base_run_id": base_run_id,
                "formal_committee_weight_allowed": False,
            }
        run_id = str(generation["run_id"])
        candidates = self.repository.candidates(run_id)
        no_skills = self.repository.no_skills(run_id)
        decisions = self.repository.review_decisions(run_id)
        approved = sum(str(row["decision"]) == "APPROVE" for row in decisions)
        rejected = sum(str(row["decision"]) == "REJECT" for row in decisions)
        return {
            "status": "PUBLISHED" if release is not None else "GENERATED",
            "base_run_id": base_run_id,
            "generation_run_id": run_id,
            "generation_policy_version": str(generation["generation_policy_version"]),
            "evaluated_argument_count": int(generation["evaluated_argument_count"]),
            "candidate_count": len(candidates),
            "no_skill_count": len(no_skills),
            "review": {
                "approved": approved,
                "rejected": rejected,
                "pending": len(candidates) - len(decisions),
            },
            "release": (
                {
                    "release_id": str(release["release_id"]),
                    "registry_version": str(release["registry_version"]),
                    "artifact_id": str(release["release_artifact_id"]),
                    "object_hash": str(release["release_object_hash"]),
                    "base_admitted_skill_count": int(release["base_admitted_skill_count"]),
                    "overlay_admitted_skill_count": int(release["overlay_admitted_skill_count"]),
                    "composite_admitted_skill_count": int(
                        release["composite_admitted_skill_count"]
                    ),
                }
                if release is not None
                else None
            ),
            "formal_committee_weight_allowed": False,
        }

    @staticmethod
    def _argument_semantics(text: str) -> tuple[str, list[str], str | None]:
        parsed: list[tuple[int, str, str]] = []
        for raw_line in text.splitlines():
            match = _LINE_RE.match(raw_line.strip())
            if match is None:
                continue
            content = match.group("text").strip()
            if not content or _IMAGE_ONLY_RE.fullmatch(content):
                continue
            parsed.append((int(match.group("ordinal")), match.group("role"), content))
        title = next((content for _, role, content in parsed if role == "TITLE"), None)
        selected: list[str] = []
        for role in _ROLE_PRIORITY:
            for _, item_role, content in parsed:
                if item_role != role or content in selected:
                    continue
                selected.append(content)
                if len(selected) >= 6:
                    break
            if len(selected) >= 6:
                break
        core_parts: list[str] = []
        total = 0
        for item in selected:
            if total + len(item) > 650 and core_parts:
                break
            core_parts.append(item)
            total += len(item)
            if len(core_parts) >= 3:
                break
        core = "；".join(core_parts).strip("； ")
        reasoning = [item[:350] for item in selected[:5]]
        return core, reasoning, title

    @staticmethod
    def _semantic_fold(text: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.casefold())

    @staticmethod
    def _skill_name(display_name: str, module: DirectSkillModule, source_head: str) -> str:
        compact = re.sub(r"\s+", " ", source_head).strip()
        return f"视觉增强·{display_name}·{_MODULE_LABEL[module]}·{compact[:28]}"

    @staticmethod
    def _json(value: object) -> str:
        return canonical_json_bytes(value).decode("utf-8")


__all__ = ["VisualSkillService"]
