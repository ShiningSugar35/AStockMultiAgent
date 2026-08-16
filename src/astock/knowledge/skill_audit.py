"""Append-only evidence-backed governance for admitted knowledge Skills."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore, utc_now_text
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
from astock.knowledge.visual_skill_repository import VisualSkillRepository
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_completion import KnowledgeAdmissionBasis
from astock.schemas.knowledge_skill_audit import (
    AuditedKnowledgeSkillRegistryMember,
    AuditedKnowledgeSkillRegistryRelease,
    CuratedResearchSkill,
    ExternalEvidenceDefinition,
    KnowledgeSkillAuditDecision,
    KnowledgeSkillAuditReport,
    KnowledgeSkillAuditRun,
    KnowledgeSkillAuditStatus,
    KnowledgeSkillAuditVerdict,
    KnowledgeSkillOrigin,
)

_ABSOLUTE_RE = re.compile(r"必然|一定|绝对|稳赚|无风险|必涨|必跌|guarantee|always|certain", re.I)
_NUMERIC_RE = re.compile(
    r"(?:\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*万|\d+(?:\.\d+)?\s*倍|\d+\s*(?:天|日|周|月|年))",
    re.I,
)
_PERSONAL_FINANCE_RE = re.compile(
    r"毕业|教育支出|学费|全职投资|本金.*(?:万|百万|千万)|生活资本", re.I
)
_UNVERIFIABLE_INTENT_RE = re.compile(
    r"主力意图|庄家|洗盘|吸筹|出货|故意为之|国家队.*(?:护盘|托底)", re.I
)
_DIRECT_TRADE_RE = re.compile(r"买入|卖出|加仓|减仓|清仓|抄底|追涨|大胆买|出重手", re.I)
_DATED_RE = re.compile(r"20\d{2}年|20\d{2}[-/.]|今年|本周|明天|特朗普|拜登|关税|美联储", re.I)
_SPECIFIC_LEVEL_RE = re.compile(
    r"\d{3,5}\s*点|(?:上证|恒生|沪指|黄金|原油|收益率).{0,18}\d{2,5}", re.I
)


class KnowledgeSkillAuditRepository:
    """Read/write index around immutable audit/release objects."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def run(self, audit_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_skill_audit_run WHERE audit_run_id=?",
                (audit_run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_run(self, source_run_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM knowledge_skill_audit_run"
        params: tuple[object, ...] = ()
        if source_run_id is not None:
            query += " WHERE source_run_id=?"
            params = (source_run_id,)
        query += " ORDER BY rowid DESC LIMIT 1"
        with closing(self.state.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def decisions(self, audit_run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_skill_audit_decision WHERE audit_run_id=? "
                "ORDER BY source_skill_id",
                (audit_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_release(self, source_run_id: str) -> dict[str, Any] | None:
        try:
            with closing(self.state.connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM knowledge_skill_audited_registry_release "
                    "WHERE source_run_id=? ORDER BY rowid DESC LIMIT 1",
                    (source_run_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return None
            raise
        return dict(row) if row is not None else None

    def release(self, release_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_skill_audited_registry_release WHERE release_id=?",
                (release_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def release_members(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_skill_audited_registry_member WHERE release_id=? "
                "ORDER BY member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def selection_rows(self, release_id: str) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for member in self.release_members(release_id):
            payload = json.loads(str(member["selection_row_json"]))
            if not isinstance(payload, dict):
                raise ValueError("audited registry selection row is not an object")
            result.append({str(key): value for key, value in payload.items()})
        return result

    def retired_tombstone(self, source_skill_id: str) -> dict[str, Any] | None:
        try:
            with closing(self.state.connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM knowledge_retired_skill_tombstone WHERE source_skill_id=?",
                    (source_skill_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return None
            raise
        return dict(row) if row is not None else None

    def retired_tombstone_count(self, audit_run_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM knowledge_retired_skill_tombstone"
        params: tuple[object, ...] = ()
        if audit_run_id is not None:
            query += " WHERE audit_run_id=?"
            params = (audit_run_id,)
        try:
            with closing(self.state.connect()) as connection:
                row = connection.execute(query, params).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return 0
            raise
        return int(row[0]) if row is not None else 0


class KnowledgeSkillAuditService:
    def __init__(self, state: StateStore, objects: ObjectStore, project_root: Path) -> None:
        self.state = state
        self.objects = objects
        self.root = project_root.resolve()
        self.completion = KnowledgeCompletionRepository(state)
        self.visual = VisualSkillRepository(state)
        self.provider = RepositoryKnowledgeSkillProvider(self.completion, objects)
        self.repository = KnowledgeSkillAuditRepository(state)
        (
            self.audit_policy,
            self.policy_hash,
            self.evidence,
            self.evidence_catalog_hash,
            self.curated_skills,
        ) = self._load_config()

    def _load_config(
        self,
    ) -> tuple[
        dict[str, Any],
        str,
        dict[str, ExternalEvidenceDefinition],
        str,
        list[dict[str, Any]],
    ]:
        policy_path = self.root / "configs" / "knowledge_skill_audit_policy.yaml"
        evidence_path = self.root / "configs" / "knowledge_skill_external_evidence.yaml"
        curated_path = self.root / "configs" / "curated_research_skills.yaml"
        policy_raw = _load_yaml(policy_path)
        evidence_raw = _load_yaml(evidence_path)
        curated_raw = _load_yaml(curated_path)
        if policy_raw.get("schema_version") != "knowledge-skill-audit-policy-v1":
            raise ValueError("Unsupported knowledge Skill audit policy")
        if evidence_raw.get("schema_version") != "knowledge-skill-external-evidence-v1":
            raise ValueError("Unsupported knowledge Skill evidence catalog")
        if curated_raw.get("schema_version") != "curated-research-skills-v1":
            raise ValueError("Unsupported curated research Skill catalog")
        if policy_raw.get("formal_committee_weight_allowed") is not False:
            raise ValueError("knowledge Skill audit policy cannot grant committee weight")
        if policy_raw.get("paper_ledger_write_allowed") is not False:
            raise ValueError("knowledge Skill audit policy cannot write paper ledger")
        if policy_raw.get("broker_execution_allowed") is not False:
            raise ValueError("knowledge Skill audit policy cannot enable broker execution")

        evidence_payload = evidence_raw.get("sources")
        if not isinstance(evidence_payload, dict) or not evidence_payload:
            raise ValueError("knowledge Skill evidence catalog is empty")
        evidence: dict[str, ExternalEvidenceDefinition] = {}
        for evidence_id, raw in evidence_payload.items():
            if not isinstance(raw, dict):
                raise ValueError("external evidence definition must be an object")
            evidence[str(evidence_id)] = ExternalEvidenceDefinition.model_validate(
                {
                    "evidence_id": str(evidence_id),
                    "title": str(raw["title"]),
                    "authority": str(raw["authority"]),
                    "source_type": str(raw["source_type"]),
                    "url": str(raw["url"]),
                    "tags": sorted({str(item) for item in raw.get("tags", [])}),
                    "limitation": str(raw["limitation"]),
                }
            )
        curated_payload = curated_raw.get("skills")
        if not isinstance(curated_payload, list):
            raise ValueError("curated research Skill catalog must contain a list")
        curated_skills = [dict(item) for item in curated_payload if isinstance(item, dict)]
        if len(curated_skills) != len(curated_payload):
            raise ValueError("curated research Skill item must be an object")

        combined_policy = {"audit_policy": policy_raw, "curated_skills": curated_raw}
        policy_ref = self.objects.put_json(combined_policy)
        evidence_ref = self.objects.put_json(evidence_raw)
        for evidence_ids in policy_raw.get("module_evidence", {}).values():
            self._validate_evidence_ids(evidence_ids, evidence)
        evidence_routes = policy_raw.get("skill_evidence_routes", [])
        if not isinstance(evidence_routes, list):
            raise ValueError("knowledge Skill evidence routes must be a list")
        for route in evidence_routes:
            if not isinstance(route, dict):
                raise ValueError("knowledge Skill evidence route must be an object")
            if not str(route.get("route_id", "")).strip():
                raise ValueError("knowledge Skill evidence route requires route_id")
            keywords = route.get("keywords")
            if not isinstance(keywords, list) or not any(str(item).strip() for item in keywords):
                raise ValueError("knowledge Skill evidence route requires keywords")
            self._validate_evidence_ids(route.get("evidence_ids", []), evidence)
        visual_default = policy_raw.get("visual_default", {})
        self._validate_evidence_ids(visual_default.get("evidence_ids", []), evidence)
        for template in policy_raw.get("replacement_templates", {}).values():
            self._validate_evidence_ids(template.get("evidence_ids", []), evidence)
        for curated in curated_skills:
            self._validate_evidence_ids(curated.get("evidence_ids", []), evidence)
        return policy_raw, policy_ref.sha256, evidence, evidence_ref.sha256, curated_skills

    @staticmethod
    def _validate_evidence_ids(
        values: object,
        catalog: dict[str, ExternalEvidenceDefinition],
    ) -> None:
        if not isinstance(values, list) or len(set(map(str, values))) < 2:
            raise ValueError(
                "every knowledge Skill audit decision requires at least two evidence IDs"
            )
        missing = sorted({str(item) for item in values} - set(catalog))
        if missing:
            raise ValueError(f"unknown knowledge Skill evidence IDs: {missing}")

    def _source_rows(self, run_id: str) -> tuple[Any, list[dict[str, object]]]:
        provider_status = self.provider.status(run_id)
        ready_codes = {"COMPOSITE_REGISTRY_READY", "AUDITED_REGISTRY_READY"}
        if provider_status.reason_code not in ready_codes:
            raise ValueError("source knowledge registry is not ready")
        if provider_status.reason_code == "AUDITED_REGISTRY_READY":
            # A new audit always re-audits the immutable pre-audit composite release, not itself.
            provider_status = self.provider.status(run_id, prefer_audited=False)
        if provider_status.reason_code != "COMPOSITE_REGISTRY_READY":
            raise ValueError("historical composite registry is not ready")
        rows = self.provider.source_composite_rows(run_id, provider_status)
        return provider_status, rows

    def plan(self, source_run_id: str | None = None) -> KnowledgeSkillAuditRun:
        run_id = source_run_id or self.provider.default_run_id()
        if run_id is None:
            raise ValueError("no published knowledge registry is available")
        status, source_rows = self._source_rows(run_id)
        expected = int(self.audit_policy["expected_source_skill_count"])
        if len(source_rows) != expected or status.eligible_skill_count != expected:
            raise ValueError("knowledge Skill audit source count differs from active policy")
        identity = {
            "source_run_id": run_id,
            "source_registry_release_id": status.registry_release_id,
            "source_registry_object_hash": status.registry_object_hash,
            "policy_hash": self.policy_hash,
            "evidence_catalog_hash": self.evidence_catalog_hash,
            "expected_skill_count": expected,
        }
        audit_run_id = f"knowledge-skill-audit:{content_hash(identity)}"
        existing = self.repository.run(audit_run_id)
        if existing is not None:
            return self._run_model(existing)
        created_at = datetime.now(UTC)
        run = KnowledgeSkillAuditRun(
            created_at=created_at,
            audit_run_id=audit_run_id,
            source_run_id=run_id,
            source_registry_release_id=str(status.registry_release_id),
            source_registry_object_hash=str(status.registry_object_hash),
            policy_hash=self.policy_hash,
            evidence_catalog_hash=self.evidence_catalog_hash,
            expected_skill_count=expected,
            decision_count=0,
            status=KnowledgeSkillAuditStatus.PLANNED,
        )
        ref = self.objects.put_json(run.model_dump(mode="json"))
        artifact_id = f"knowledge-skill-audit-plan:{content_hash(run)}"
        timestamp = created_at.isoformat()
        with self.state.transaction() as connection:
            _register_artifact(
                connection,
                artifact_id=artifact_id,
                artifact_type="KnowledgeSkillAuditRun",
                schema_version=run.schema_version,
                object_hash=ref.sha256,
                input_hashes=[
                    run.source_registry_object_hash,
                    run.policy_hash,
                    run.evidence_catalog_hash,
                ],
            )
            connection.execute(
                "INSERT INTO knowledge_skill_audit_run("
                "audit_run_id,source_run_id,source_registry_release_id,source_registry_object_hash,"
                "policy_hash,evidence_catalog_hash,expected_skill_count,decision_count,status,"
                "run_artifact_id,run_object_hash,run_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.audit_run_id,
                    run.source_run_id,
                    run.source_registry_release_id,
                    run.source_registry_object_hash,
                    run.policy_hash,
                    run.evidence_catalog_hash,
                    run.expected_skill_count,
                    0,
                    run.status.value,
                    artifact_id,
                    ref.sha256,
                    canonical_json_bytes(run.model_dump(mode="json")).decode("utf-8"),
                    timestamp,
                    timestamp,
                ),
            )
        return run

    def run(self, audit_run_id: str) -> KnowledgeSkillAuditRun:
        row = self.repository.run(audit_run_id)
        if row is None:
            raise ValueError("knowledge Skill audit run does not exist")
        if str(row["status"]) in {"DECISIONS_COMPLETE", "PUBLISHED"}:
            return self._run_model(row)
        run = self._run_model(row)
        source_status, source_rows = self._source_rows(run.source_run_id)
        if (
            source_status.registry_release_id != run.source_registry_release_id
            or source_status.registry_object_hash != run.source_registry_object_hash
        ):
            raise ValueError("knowledge Skill source registry drifted after audit planning")
        if len(source_rows) != run.expected_skill_count:
            raise ValueError("knowledge Skill source membership drifted after audit planning")
        self._validate_policy_overrides(source_rows)

        prepared: list[
            tuple[KnowledgeSkillAuditDecision, str, CuratedResearchSkill | None, str | None]
        ] = []
        for source in sorted(source_rows, key=lambda item: str(item["final_skill_id"])):
            prepared.append(self._prepare_decision(run, source))

        timestamp = utc_now_text()
        with self.state.transaction() as connection:
            for decision, decision_hash, replacement, replacement_hash in prepared:
                if replacement is not None and replacement_hash is not None:
                    replacement_artifact_id = str(decision.replacement_skill_artifact_id)
                    _register_artifact(
                        connection,
                        artifact_id=replacement_artifact_id,
                        artifact_type="RevisedKnowledgeSkill",
                        schema_version=replacement.schema_version,
                        object_hash=replacement_hash,
                        input_hashes=[
                            decision.source_skill_object_hash,
                            run.policy_hash,
                            run.evidence_catalog_hash,
                        ],
                    )
                decision_artifact_id = decision.decision_id
                _register_artifact(
                    connection,
                    artifact_id=decision_artifact_id,
                    artifact_type="KnowledgeSkillAuditDecision",
                    schema_version=decision.schema_version,
                    object_hash=decision_hash,
                    input_hashes=sorted(
                        {
                            decision.source_skill_object_hash,
                            run.policy_hash,
                            run.evidence_catalog_hash,
                            *(
                                [str(decision.replacement_skill_object_hash)]
                                if decision.replacement_skill_object_hash
                                else []
                            ),
                        }
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_skill_audit_decision("
                    "decision_id,audit_run_id,source_skill_id,source_skill_object_hash,"
                    "source_skill_artifact_id,skill_origin,verdict,premise_scope,risk_codes_json,"
                    "conflict_groups_json,external_evidence_ids_json,rationale,replacement_skill_id,"
                    "replacement_skill_object_hash,replacement_skill_artifact_id,decision_object_hash,"
                    "decision_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision.decision_id,
                        decision.audit_run_id,
                        decision.source_skill_id,
                        decision.source_skill_object_hash,
                        decision.source_skill_artifact_id,
                        decision.skill_origin.value,
                        decision.verdict.value,
                        decision.premise_scope,
                        _json(decision.risk_codes),
                        _json(decision.conflict_groups),
                        _json(decision.external_evidence_ids),
                        decision.rationale,
                        decision.replacement_skill_id,
                        decision.replacement_skill_object_hash,
                        decision.replacement_skill_artifact_id,
                        decision_hash,
                        canonical_json_bytes(decision.model_dump(mode="json")).decode("utf-8"),
                        decision.created_at.isoformat(),
                    ),
                )
            connection.execute(
                "UPDATE knowledge_skill_audit_run SET decision_count=?,status=?,updated_at=? "
                "WHERE audit_run_id=?",
                (
                    len(prepared),
                    KnowledgeSkillAuditStatus.DECISIONS_COMPLETE.value,
                    timestamp,
                    audit_run_id,
                ),
            )
        refreshed = self.repository.run(audit_run_id)
        assert refreshed is not None
        return self._run_model(refreshed)

    def _prepare_decision(
        self,
        run: KnowledgeSkillAuditRun,
        source: dict[str, object],
    ) -> tuple[KnowledgeSkillAuditDecision, str, CuratedResearchSkill | None, str | None]:
        source_skill_id = str(source["final_skill_id"])
        source_hash = str(source["skill_object_hash"])
        if not self.objects.verify(source_hash):
            raise ValueError(f"source knowledge Skill object missing: {source_skill_id}")
        if self.completion.artifact_object_hash(str(source["skill_artifact_id"])) != source_hash:
            raise ValueError(f"source knowledge Skill artifact drift: {source_skill_id}")
        skill_json = str(source["skill_json"])
        if sha256_bytes(skill_json.encode("utf-8")) != source_hash:
            raise ValueError(f"source knowledge Skill JSON drift: {source_skill_id}")
        payload = json.loads(skill_json)
        if not isinstance(payload, dict):
            raise ValueError("source Skill payload must be an object")
        origin = KnowledgeSkillOrigin(str(source.get("skill_origin", "DIRECT")))
        module = DirectSkillModule(str(source["primary_module"]))
        text = " ".join(
            [
                str(source.get("skill_name", "")),
                str(source.get("decision_question", "")),
                str(source.get("core_principle", "")),
                " ".join(map(str, payload.get("reasoning_steps", []))),
            ]
        )
        risk_codes = self._risk_codes(text, origin)
        conflict_groups = self._conflict_groups(text)
        premise_scope = self._premise_scope(module, payload, text)
        replacement: CuratedResearchSkill | None = None
        replacement_hash: str | None = None
        replacement_id: str | None = None
        replacement_artifact_id: str | None = None

        if origin is KnowledgeSkillOrigin.VISUAL_OVERLAY:
            template_key = self.audit_policy.get("visual_revise_templates", {}).get(source_skill_id)
            if template_key:
                verdict = KnowledgeSkillAuditVerdict.REVISE
                risk_codes.extend(["SOURCE_FACTUAL_OR_GENERALITY_ERROR", "REPLACEMENT_REQUIRED"])
            else:
                verdict = KnowledgeSkillAuditVerdict.RETIRE
                risk_codes.extend(self.audit_policy["visual_default"]["risk_codes"])
        elif source_skill_id in set(self.audit_policy.get("direct_retire_skill_ids", [])):
            verdict = KnowledgeSkillAuditVerdict.RETIRE
            template_key = None
            risk_codes.extend(
                [
                    "OUT_OF_RESEARCH_DOMAIN_OR_UNSUPPORTED_THRESHOLD",
                    "RETIRE_FROM_ACTIVE_SKILLS",
                ]
            )
        else:
            template_key = self.audit_policy.get("direct_revise_templates", {}).get(source_skill_id)
            verdict = (
                KnowledgeSkillAuditVerdict.REVISE
                if template_key
                else KnowledgeSkillAuditVerdict.KEEP_SCOPED
            )
            if verdict is KnowledgeSkillAuditVerdict.REVISE:
                risk_codes.extend(["OVERGENERALIZED_OR_LOGICALLY_UNSAFE", "REPLACEMENT_REQUIRED"])
            else:
                risk_codes.append("PREMISE_SCOPED_HEURISTIC")

        if verdict is KnowledgeSkillAuditVerdict.REVISE:
            replacement = self._replacement_skill(
                run,
                source,
                payload,
                str(template_key),
            )
            replacement_ref = self.objects.put_json(replacement.model_dump(mode="json"))
            replacement_hash = replacement_ref.sha256
            replacement_id = replacement.skill_id
            replacement_artifact_id = f"knowledge-skill-revision:{replacement.skill_id}"
            evidence_ids = replacement.external_evidence_ids
            rationale = (
                "The original source remains immutable, but the original formulation is too broad, "
                "factually simplified, or logically unsafe. The audited registry uses an "
                "evidence-backed replacement with the original source lineage preserved."
            )
        elif verdict is KnowledgeSkillAuditVerdict.RETIRE:
            if origin is KnowledgeSkillOrigin.VISUAL_OVERLAY:
                evidence_ids = sorted(
                    {str(item) for item in self.audit_policy["visual_default"]["evidence_ids"]}
                )
                rationale = str(self.audit_policy["visual_default"]["rationale"])
            else:
                evidence_ids = self._evidence_for_skill(module, text)
                rationale = (
                    "The immutable source is preserved, but this item is personal-finance "
                    "guidance, an unsupported market-timing rule, or a fixed threshold "
                    "without a portable research basis."
                )
        else:
            evidence_ids = self._evidence_for_skill(module, text)
            rationale = (
                "The core method is usable only under the source Skill's explicit conditions. "
                "External evidence supports the process discipline but not an unconditional "
                "return or timing claim."
            )

        evidence_ids = sorted(set(evidence_ids))
        self._validate_evidence_ids(evidence_ids, self.evidence)
        risk_codes = sorted(set(risk_codes))
        conflict_groups = sorted(set(conflict_groups))
        identity = {
            "audit_run_id": run.audit_run_id,
            "source_skill_id": source_skill_id,
            "source_skill_object_hash": source_hash,
            "verdict": verdict.value,
            "premise_scope": premise_scope,
            "risk_codes": risk_codes,
            "conflict_groups": conflict_groups,
            "external_evidence_ids": evidence_ids,
            "replacement_skill_id": replacement_id,
            "replacement_skill_object_hash": replacement_hash,
        }
        decision_id = f"knowledge-skill-audit-decision:{content_hash(identity)}"
        decision = KnowledgeSkillAuditDecision(
            created_at=run.created_at,
            audit_run_id=run.audit_run_id,
            decision_id=decision_id,
            source_skill_id=source_skill_id,
            source_skill_object_hash=source_hash,
            source_skill_artifact_id=str(source["skill_artifact_id"]),
            skill_origin=origin,
            verdict=verdict,
            premise_scope=premise_scope,
            risk_codes=risk_codes,
            conflict_groups=conflict_groups,
            external_evidence_ids=evidence_ids,
            rationale=rationale,
            replacement_skill_id=replacement_id,
            replacement_skill_object_hash=replacement_hash,
            replacement_skill_artifact_id=replacement_artifact_id,
        )
        decision_ref = self.objects.put_json(decision.model_dump(mode="json"))
        return decision, decision_ref.sha256, replacement, replacement_hash

    def _replacement_skill(
        self,
        run: KnowledgeSkillAuditRun,
        source: dict[str, object],
        payload: dict[str, object],
        template_key: str,
    ) -> CuratedResearchSkill:
        raw_templates = self.audit_policy.get("replacement_templates")
        if not isinstance(raw_templates, dict) or template_key not in raw_templates:
            raise ValueError(f"unknown knowledge Skill replacement template: {template_key}")
        template = raw_templates[template_key]
        if not isinstance(template, dict):
            raise ValueError("knowledge Skill replacement template must be an object")
        evidence_ids = sorted({str(item) for item in template["evidence_ids"]})
        self._validate_evidence_ids(evidence_ids, self.evidence)
        source_hashes = sorted(
            {
                *json.loads(str(source["source_hashes_json"])),
                run.evidence_catalog_hash,
            }
        )
        secondary_values = json.loads(str(source["secondary_modules_json"]))
        secondary = sorted(
            {DirectSkillModule(str(item)) for item in secondary_values}, key=lambda item: item.value
        )
        replacement_seed = {
            "source_skill_id": str(source["final_skill_id"]),
            "source_skill_object_hash": str(source["skill_object_hash"]),
            "template": template_key,
            "policy_hash": run.policy_hash,
            "evidence_catalog_hash": run.evidence_catalog_hash,
        }
        skill_id = f"revised-skill:{content_hash(replacement_seed)}"
        conditions = _string_list(payload.get("applicable_conditions"))
        if not conditions:
            conditions = [
                "Use only when the original Skill's decision context is explicitly satisfied."
            ]
        invalidation = _string_list(payload.get("invalidation_conditions"))
        if not invalidation:
            invalidation = [
                "Material evidence contradicts the audited premise or changes its decision context."
            ]
        shadow_only = template_key in {
            "TECHNICAL_HYPOTHESIS_VALIDATION",
            "OOS_EVIDENCE_NOT_REALIZED_PNL",
            "SENTIMENT_REVIEW_NOT_SIGNAL",
            "CYCLE_EVIDENCE_NOT_PRICE_LOW",
            "NO_FIXED_RETURN_THRESHOLD",
        }
        return CuratedResearchSkill(
            created_at=run.created_at,
            skill_id=skill_id,
            skill_name=str(template["skill_name"]),
            primary_module=DirectSkillModule(str(source["primary_module"])),
            secondary_modules=secondary,
            decision_question=str(source["decision_question"]),
            core_principle=str(template["core_principle"]),
            applicable_conditions=conditions,
            reasoning_steps=[
                "Confirm the original Skill's applicable premise and time horizon.",
                "Test the revised proposition against the bound external evidence and "
                "current official facts.",
                "Use the revised method only while its explicit invalidation conditions "
                "remain false.",
            ],
            invalidation_conditions=invalidation,
            external_evidence_ids=evidence_ids,
            source_skill_id=str(source["final_skill_id"]),
            source_skill_object_hash=str(source["skill_object_hash"]),
            source_skill_artifact_id=str(source["skill_artifact_id"]),
            source_hashes=source_hashes,
            shadow_or_prospective_only=shadow_only,
        )

    def _curated_models(self, created_at: datetime) -> list[CuratedResearchSkill]:
        models: list[CuratedResearchSkill] = []
        for raw in self.curated_skills:
            evidence_ids = sorted({str(item) for item in raw["evidence_ids"]})
            self._validate_evidence_ids(evidence_ids, self.evidence)
            curated_seed = {
                "curated_skill_id": str(raw["curated_skill_id"]),
                "policy_hash": self.policy_hash,
                "evidence_catalog_hash": self.evidence_catalog_hash,
            }
            skill_id = f"curated-skill:{content_hash(curated_seed)}"
            models.append(
                CuratedResearchSkill(
                    created_at=created_at,
                    skill_id=skill_id,
                    skill_name=str(raw["skill_name"]),
                    primary_module=DirectSkillModule(str(raw["primary_module"])),
                    decision_question=str(raw["decision_question"]),
                    core_principle=str(raw["core_principle"]),
                    applicable_conditions=[str(item) for item in raw["applicable_conditions"]],
                    reasoning_steps=[str(item) for item in raw["reasoning_steps"]],
                    invalidation_conditions=[str(item) for item in raw["invalidation_conditions"]],
                    external_evidence_ids=evidence_ids,
                    source_hashes=[self.evidence_catalog_hash],
                    shadow_or_prospective_only=bool(raw["shadow_or_prospective_only"]),
                )
            )
        ids = [item.skill_id for item in models]
        if len(ids) != len(set(ids)):
            raise ValueError("curated research Skill IDs collide")
        return sorted(models, key=lambda item: item.skill_id)

    def audit(self, audit_run_id: str) -> KnowledgeSkillAuditReport:
        run_row = self.repository.run(audit_run_id)
        if run_row is None:
            raise ValueError("knowledge Skill audit run does not exist")
        run = self._run_model(run_row)
        decisions = self.repository.decisions(audit_run_id)
        missing_evidence = 0
        broken_objects = 0
        active_count = 0
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in decisions:
            try:
                decision = KnowledgeSkillAuditDecision.model_validate_json(
                    str(row["decision_json"])
                )
            except ValueError:
                broken_objects += 1
                continue
            if not self.objects.verify(str(row["decision_object_hash"])):
                broken_objects += 1
            if self.completion.artifact_object_hash(decision.decision_id) != str(
                row["decision_object_hash"]
            ):
                broken_objects += 1
            tombstone = self.repository.retired_tombstone(decision.source_skill_id)
            if decision.verdict is KnowledgeSkillAuditVerdict.RETIRE and tombstone is not None:
                if str(tombstone["source_skill_object_hash"]) != decision.source_skill_object_hash:
                    broken_objects += 1
            elif not self.objects.verify(decision.source_skill_object_hash):
                broken_objects += 1
            missing_evidence += len(set(decision.external_evidence_ids) - set(self.evidence))
            if decision.verdict is not KnowledgeSkillAuditVerdict.RETIRE:
                active_count += 1
                for group in decision.conflict_groups:
                    groups[group].append(row)
            if decision.verdict is KnowledgeSkillAuditVerdict.REVISE:
                replacement_hash = str(decision.replacement_skill_object_hash)
                if not self.objects.verify(replacement_hash):
                    broken_objects += 1
                if (
                    self.completion.artifact_object_hash(
                        str(decision.replacement_skill_artifact_id)
                    )
                    != replacement_hash
                ):
                    broken_objects += 1
        unresolved = 0
        for group_rows in groups.values():
            broad = [row for row in group_rows if str(row["verdict"]) == "KEEP"]
            if len(broad) > 1:
                unresolved += 1
            scopes = Counter(str(row["premise_scope"]) for row in group_rows)
            if any(count > 1 for count in scopes.values()):
                same_scope_rows = [
                    row
                    for scope, count in scopes.items()
                    if count > 1
                    for row in group_rows
                    if str(row["premise_scope"]) == scope
                ]
                if any(str(row["verdict"]) == "KEEP" for row in same_scope_rows):
                    unresolved += 1
        findings: list[str] = []
        if len(decisions) != run.expected_skill_count:
            findings.append("SOURCE_SKILL_DECISION_COUNT_MISMATCH")
        if missing_evidence:
            findings.append("EXTERNAL_EVIDENCE_GAP")
        if broken_objects:
            findings.append("AUDIT_OBJECT_OR_ARTIFACT_DRIFT")
        if unresolved:
            findings.append("UNRESOLVED_SAME_PREMISE_CONFLICT")
        report = KnowledgeSkillAuditReport(
            audit_run_id=audit_run_id,
            status="PASS" if not findings else "FAIL",
            source_skill_count=run.expected_skill_count,
            decision_count=len(decisions),
            active_skill_count=active_count + len(self._curated_models(run.created_at)),
            conflict_group_count=len(groups),
            unresolved_same_premise_conflict_count=unresolved,
            missing_external_evidence_count=missing_evidence,
            broken_object_count=broken_objects,
            finding_codes=sorted(set(findings)),
        )
        return report

    def publish(self, audit_run_id: str) -> AuditedKnowledgeSkillRegistryRelease:
        audit = self.audit(audit_run_id)
        if audit.status != "PASS":
            raise ValueError("knowledge Skill audit must PASS before registry publication")
        run_row = self.repository.run(audit_run_id)
        assert run_row is not None
        run = self._run_model(run_row)
        existing = self.repository.latest_release(run.source_run_id)
        if existing is not None and str(existing["audit_run_id"]) == audit_run_id:
            return AuditedKnowledgeSkillRegistryRelease.model_validate_json(
                str(existing["release_json"])
            )
        _source_status, source_rows = self._source_rows(run.source_run_id)
        source_by_id = {str(row["final_skill_id"]): row for row in source_rows}
        decisions = [
            KnowledgeSkillAuditDecision.model_validate_json(str(row["decision_json"]))
            for row in self.repository.decisions(audit_run_id)
        ]
        verdict_counts = Counter(item.verdict for item in decisions)
        prepared_members: list[tuple[dict[str, object], AuditedKnowledgeSkillRegistryMember]] = []
        for decision in decisions:
            if decision.verdict is KnowledgeSkillAuditVerdict.RETIRE:
                continue
            source = source_by_id[decision.source_skill_id]
            if decision.verdict in {
                KnowledgeSkillAuditVerdict.KEEP,
                KnowledgeSkillAuditVerdict.KEEP_SCOPED,
            }:
                selection_row = dict(source)
                member = AuditedKnowledgeSkillRegistryMember(
                    member_ordinal=1,
                    effective_skill_id=decision.source_skill_id,
                    effective_skill_object_hash=decision.source_skill_object_hash,
                    effective_skill_artifact_id=decision.source_skill_artifact_id,
                    source_skill_id=decision.source_skill_id,
                    decision_id=decision.decision_id,
                    skill_origin=decision.skill_origin,
                    admission_basis=f"AUDIT_{decision.verdict.value}",
                    source_hashes=sorted(set(json.loads(str(source["source_hashes_json"])))),
                )
            else:
                replacement = CuratedResearchSkill.model_validate_json(
                    self.objects.get_bytes(str(decision.replacement_skill_object_hash))
                )
                selection_row = self._selection_row(replacement, KnowledgeSkillOrigin.REVISED)
                member = AuditedKnowledgeSkillRegistryMember(
                    member_ordinal=1,
                    effective_skill_id=replacement.skill_id,
                    effective_skill_object_hash=str(decision.replacement_skill_object_hash),
                    effective_skill_artifact_id=str(decision.replacement_skill_artifact_id),
                    source_skill_id=decision.source_skill_id,
                    decision_id=decision.decision_id,
                    skill_origin=KnowledgeSkillOrigin.REVISED,
                    admission_basis="AUDIT_REVISED",
                    source_hashes=replacement.source_hashes,
                )
            prepared_members.append((selection_row, member))

        curated_artifacts: list[tuple[CuratedResearchSkill, str, str]] = []
        for skill in self._curated_models(run.created_at):
            ref = self.objects.put_json(skill.model_dump(mode="json"))
            artifact_id = f"knowledge-skill-curated:{skill.skill_id}"
            curated_artifacts.append((skill, ref.sha256, artifact_id))
            prepared_members.append(
                (
                    self._selection_row(
                        skill,
                        KnowledgeSkillOrigin.CURATED,
                        object_hash=ref.sha256,
                        artifact_id=artifact_id,
                    ),
                    AuditedKnowledgeSkillRegistryMember(
                        member_ordinal=1,
                        effective_skill_id=skill.skill_id,
                        effective_skill_object_hash=ref.sha256,
                        effective_skill_artifact_id=artifact_id,
                        skill_origin=KnowledgeSkillOrigin.CURATED,
                        admission_basis="CURATED_EXTERNAL_EVIDENCE",
                        source_hashes=skill.source_hashes,
                    ),
                )
            )
        prepared_members.sort(key=lambda item: item[1].effective_skill_id)
        members = [
            member.model_copy(update={"member_ordinal": ordinal})
            for ordinal, (_row, member) in enumerate(prepared_members, start=1)
        ]
        release_seed = {
            "audit_run_id": audit_run_id,
            "source_registry_object_hash": run.source_registry_object_hash,
            "policy_hash": run.policy_hash,
            "evidence_catalog_hash": run.evidence_catalog_hash,
            "members": [
                {
                    "effective_skill_id": item.effective_skill_id,
                    "effective_skill_object_hash": item.effective_skill_object_hash,
                    "decision_id": item.decision_id,
                }
                for item in members
            ],
        }
        release_id = f"knowledge-audited-registry:{content_hash(release_seed)}"
        release_artifact_id = f"knowledge-audited-registry-release:{release_id}"
        release = AuditedKnowledgeSkillRegistryRelease(
            release_id=release_id,
            audit_run_id=audit_run_id,
            source_run_id=run.source_run_id,
            source_registry_release_id=run.source_registry_release_id,
            source_registry_object_hash=run.source_registry_object_hash,
            policy_hash=run.policy_hash,
            evidence_catalog_hash=run.evidence_catalog_hash,
            source_skill_count=run.expected_skill_count,
            decision_count=len(decisions),
            keep_count=verdict_counts[KnowledgeSkillAuditVerdict.KEEP],
            keep_scoped_count=verdict_counts[KnowledgeSkillAuditVerdict.KEEP_SCOPED],
            revise_count=verdict_counts[KnowledgeSkillAuditVerdict.REVISE],
            retire_count=verdict_counts[KnowledgeSkillAuditVerdict.RETIRE],
            curated_count=len(curated_artifacts),
            active_skill_count=len(members),
            members=members,
            release_artifact_id=release_artifact_id,
        )
        release_ref = self.objects.put_json(release.model_dump(mode="json"))
        timestamp = release.created_at.isoformat()
        with self.state.transaction() as connection:
            for skill, object_hash, artifact_id in curated_artifacts:
                _register_artifact(
                    connection,
                    artifact_id=artifact_id,
                    artifact_type="CuratedResearchSkill",
                    schema_version=skill.schema_version,
                    object_hash=object_hash,
                    input_hashes=[run.evidence_catalog_hash, run.policy_hash],
                )
            _register_artifact(
                connection,
                artifact_id=release_artifact_id,
                artifact_type="AuditedKnowledgeSkillRegistryRelease",
                schema_version=release.schema_version,
                object_hash=release_ref.sha256,
                input_hashes=[
                    run.source_registry_object_hash,
                    run.policy_hash,
                    run.evidence_catalog_hash,
                    *sorted({item.effective_skill_object_hash for item in members}),
                ],
            )
            connection.execute(
                "INSERT INTO knowledge_skill_audited_registry_release("
                "release_id,audit_run_id,source_run_id,source_registry_release_id,"
                "source_registry_object_hash,policy_hash,evidence_catalog_hash,source_skill_count,"
                "decision_count,keep_count,keep_scoped_count,revise_count,retire_count,"
                "curated_count,active_skill_count,release_artifact_id,release_object_hash,"
                "release_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    release.release_id,
                    release.audit_run_id,
                    release.source_run_id,
                    release.source_registry_release_id,
                    release.source_registry_object_hash,
                    release.policy_hash,
                    release.evidence_catalog_hash,
                    release.source_skill_count,
                    release.decision_count,
                    release.keep_count,
                    release.keep_scoped_count,
                    release.revise_count,
                    release.retire_count,
                    release.curated_count,
                    release.active_skill_count,
                    release.release_artifact_id,
                    release_ref.sha256,
                    canonical_json_bytes(release.model_dump(mode="json")).decode("utf-8"),
                    timestamp,
                ),
            )
            for (selection_row, _old_member), member in zip(prepared_members, members, strict=True):
                connection.execute(
                    "INSERT INTO knowledge_skill_audited_registry_member("
                    "release_id,member_ordinal,effective_skill_id,effective_skill_object_hash,"
                    "effective_skill_artifact_id,source_skill_id,decision_id,skill_origin,"
                    "admission_basis,source_hashes_json,selection_row_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        release.release_id,
                        member.member_ordinal,
                        member.effective_skill_id,
                        member.effective_skill_object_hash,
                        member.effective_skill_artifact_id,
                        member.source_skill_id,
                        member.decision_id,
                        member.skill_origin.value,
                        member.admission_basis,
                        _json(member.source_hashes),
                        canonical_json_bytes(selection_row).decode("utf-8"),
                    ),
                )
            connection.execute(
                "UPDATE knowledge_skill_audit_run SET status=?,updated_at=? WHERE audit_run_id=?",
                (KnowledgeSkillAuditStatus.PUBLISHED.value, timestamp, audit_run_id),
            )
        return release

    def prune_retired(
        self,
        audit_run_id: str | None = None,
        *,
        confirm: bool = False,
    ) -> dict[str, object]:
        """Physically remove retired Skill payloads while retaining compact audit tombstones."""
        if not confirm:
            raise ValueError("retired Skill compaction requires explicit confirmation")
        row = self.repository.run(audit_run_id) if audit_run_id else self.repository.latest_run()
        if row is None:
            raise ValueError("knowledge Skill audit run does not exist")
        run_id = str(row["audit_run_id"])
        if str(row["status"]) != KnowledgeSkillAuditStatus.PUBLISHED.value:
            raise ValueError("only a published knowledge Skill audit may be compacted")
        release = self.repository.latest_release(str(row["source_run_id"]))
        if release is None or str(release["audit_run_id"]) != run_id:
            raise ValueError("latest audited registry does not match the compaction audit")
        report = self.audit(run_id)
        if report.status != "PASS":
            raise ValueError("knowledge Skill audit must PASS before retired Skill compaction")

        retire_rows = [
            item
            for item in self.repository.decisions(run_id)
            if str(item["verdict"]) == KnowledgeSkillAuditVerdict.RETIRE.value
        ]
        object_hashes: set[str] = set()
        artifact_ids: set[str] = set()
        removed_direct = 0
        removed_visual = 0
        timestamp = utc_now_text()
        with self.state.transaction() as connection:
            for item in retire_rows:
                skill_id = str(item["source_skill_id"])
                skill_hash = str(item["source_skill_object_hash"])
                artifact_id = str(item["source_skill_artifact_id"])
                origin = str(item["skill_origin"])
                connection.execute(
                    "INSERT OR IGNORE INTO knowledge_retired_skill_tombstone("
                    "source_skill_id,source_skill_object_hash,skill_origin,audit_run_id,"
                    "source_skill_artifact_id,removed_object_bytes,compacted_at) "
                    "VALUES(?,?,?,?,?,0,?)",
                    (skill_id, skill_hash, origin, run_id, artifact_id, timestamp),
                )
                object_hashes.add(skill_hash)
                artifact_ids.add(artifact_id)
                if origin == KnowledgeSkillOrigin.DIRECT.value:
                    direct = connection.execute(
                        "SELECT run_id FROM knowledge_direct_final_skill "
                        "WHERE final_skill_id=? AND skill_object_hash=?",
                        (skill_id, skill_hash),
                    ).fetchone()
                    if direct is None:
                        continue
                    source_run_id = str(direct[0])
                    connection.execute(
                        "DELETE FROM knowledge_skill_registry_member WHERE final_skill_id=?",
                        (skill_id,),
                    )
                    for table in (
                        "knowledge_direct_final_visual_ref",
                        "knowledge_direct_final_source_ref",
                        "knowledge_direct_final_to_candidate_contribution",
                        "knowledge_direct_final_skill_module",
                    ):
                        connection.execute(
                            f"DELETE FROM {table} WHERE run_id=? AND final_skill_id=?",
                            (source_run_id, skill_id),
                        )
                    connection.execute(
                        "DELETE FROM knowledge_direct_final_skill "
                        "WHERE run_id=? AND final_skill_id=? AND skill_object_hash=?",
                        (source_run_id, skill_id, skill_hash),
                    )
                    removed_direct += 1
                elif origin == KnowledgeSkillOrigin.VISUAL_OVERLAY.value:
                    candidate = connection.execute(
                        "SELECT candidate_id,skill_artifact_id,audit_artifact_id,audit_object_hash "
                        "FROM knowledge_visual_skill_candidate WHERE final_skill_id=?",
                        (skill_id,),
                    ).fetchone()
                    if candidate is None:
                        continue
                    artifact_ids.update((str(candidate[1]), str(candidate[2])))
                    object_hashes.add(str(candidate[3]))
                    reviews = connection.execute(
                        "SELECT decision_artifact_id,decision_object_hash "
                        "FROM knowledge_visual_skill_review_decision WHERE final_skill_id=?",
                        (skill_id,),
                    ).fetchall()
                    for review in reviews:
                        artifact_ids.add(str(review[0]))
                        object_hashes.add(str(review[1]))
                    connection.execute(
                        "DELETE FROM knowledge_visual_skill_member WHERE final_skill_id=?",
                        (skill_id,),
                    )
                    connection.execute(
                        "DELETE FROM knowledge_visual_skill_review_decision WHERE final_skill_id=?",
                        (skill_id,),
                    )
                    connection.execute(
                        "DELETE FROM knowledge_visual_skill_candidate WHERE final_skill_id=?",
                        (skill_id,),
                    )
                    removed_visual += 1
            for artifact_id in sorted(artifact_ids):
                connection.execute(
                    "DELETE FROM artifact_registry WHERE artifact_id=?", (artifact_id,)
                )

        removed_bytes = 0
        removed_objects = 0
        removed_sizes: dict[str, int] = {}
        with closing(self.state.connect()) as connection:
            for object_hash in sorted(object_hashes):
                still_registered = connection.execute(
                    "SELECT 1 FROM artifact_registry WHERE object_hash=? LIMIT 1", (object_hash,)
                ).fetchone()
                if still_registered is not None:
                    continue
                path = self.objects.path_for(object_hash)
                if not path.exists():
                    continue
                size = path.stat().st_size
                path.unlink()
                removed_sizes[object_hash] = size
                removed_bytes += size
                removed_objects += 1
        with self.state.transaction() as connection:
            for item in retire_rows:
                skill_id = str(item["source_skill_id"])
                source_hash = str(item["source_skill_object_hash"])
                connection.execute(
                    "UPDATE knowledge_retired_skill_tombstone SET removed_object_bytes=? "
                    "WHERE source_skill_id=?",
                    (removed_sizes.get(source_hash, 0), skill_id),
                )

        return {
            "status": "COMPACTED",
            "audit_run_id": run_id,
            "retired_skill_count": len(retire_rows),
            "removed_direct_skill_rows": removed_direct,
            "removed_visual_skill_rows": removed_visual,
            "removed_object_count": removed_objects,
            "removed_object_bytes": removed_bytes,
            "active_skill_count": int(release["active_skill_count"]),
            "database_vacuum_required_to_reclaim_free_pages": True,
        }

    def status(self, audit_run_id: str | None = None) -> dict[str, object]:
        row = self.repository.run(audit_run_id) if audit_run_id else self.repository.latest_run()
        if row is None:
            return {"status": "NOT_RUN"}
        decisions = self.repository.decisions(str(row["audit_run_id"]))
        counts = Counter(str(item["verdict"]) for item in decisions)
        release = self.repository.latest_release(str(row["source_run_id"]))
        return {
            "audit_run_id": str(row["audit_run_id"]),
            "status": str(row["status"]),
            "source_run_id": str(row["source_run_id"]),
            "source_registry_release_id": str(row["source_registry_release_id"]),
            "source_registry_object_hash": str(row["source_registry_object_hash"]),
            "expected_skill_count": int(row["expected_skill_count"]),
            "decision_count": len(decisions),
            "verdict_counts": dict(sorted(counts.items())),
            "audited_release_id": str(release["release_id"]) if release else None,
            "active_skill_count": int(release["active_skill_count"]) if release else None,
        }

    def decision_list(
        self,
        audit_run_id: str,
        verdict: KnowledgeSkillAuditVerdict | None = None,
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for row in self.repository.decisions(audit_run_id):
            if verdict is not None and str(row["verdict"]) != verdict.value:
                continue
            result.append(
                {
                    "decision_id": str(row["decision_id"]),
                    "source_skill_id": str(row["source_skill_id"]),
                    "skill_origin": str(row["skill_origin"]),
                    "verdict": str(row["verdict"]),
                    "premise_scope": str(row["premise_scope"]),
                    "risk_codes": json.loads(str(row["risk_codes_json"])),
                    "conflict_groups": json.loads(str(row["conflict_groups_json"])),
                    "external_evidence_ids": json.loads(str(row["external_evidence_ids_json"])),
                    "rationale": str(row["rationale"]),
                    "replacement_skill_id": row["replacement_skill_id"],
                }
            )
        return result

    def _selection_row(
        self,
        skill: CuratedResearchSkill,
        origin: KnowledgeSkillOrigin,
        *,
        object_hash: str | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, object]:
        skill_json = canonical_json_bytes(skill.model_dump(mode="json")).decode("utf-8")
        resolved_hash = object_hash or sha256_bytes(skill_json.encode("utf-8"))
        resolved_artifact = artifact_id or f"knowledge-skill-revision:{skill.skill_id}"
        return {
            "member_ordinal": 0,
            "final_skill_id": skill.skill_id,
            "skill_object_hash": resolved_hash,
            "skill_artifact_id": resolved_artifact,
            "admission_basis": KnowledgeAdmissionBasis.READY.value,
            "source_hashes_json": _json(skill.source_hashes),
            "status": "READY_FOR_SHADOW",
            "skill_name": skill.skill_name,
            "primary_module": skill.primary_module.value,
            "decision_question": skill.decision_question,
            "core_principle": skill.core_principle,
            "secondary_modules_json": _json([item.value for item in skill.secondary_modules]),
            "skill_json": skill_json,
            "skill_origin": origin.value,
        }

    def _validate_policy_overrides(self, source_rows: list[dict[str, object]]) -> None:
        origin_by_id = {
            str(row["final_skill_id"]): str(row.get("skill_origin", "DIRECT"))
            for row in source_rows
        }
        source_ids = set(origin_by_id)
        retire_ids = {str(item) for item in self.audit_policy.get("direct_retire_skill_ids", [])}
        direct_revise_raw = self.audit_policy.get("direct_revise_templates", {})
        visual_revise_raw = self.audit_policy.get("visual_revise_templates", {})
        if not isinstance(direct_revise_raw, dict) or not isinstance(visual_revise_raw, dict):
            raise ValueError("knowledge Skill audit revision maps must be objects")
        direct_revise_ids = {str(item) for item in direct_revise_raw}
        visual_revise_ids = {str(item) for item in visual_revise_raw}
        unknown = sorted((retire_ids | direct_revise_ids | visual_revise_ids) - source_ids)
        if unknown:
            raise ValueError(
                f"knowledge Skill audit policy references unknown source IDs: {unknown}"
            )
        overlap = (retire_ids & direct_revise_ids) | (retire_ids & visual_revise_ids)
        if overlap:
            raise ValueError(
                f"knowledge Skill audit policy has retire/revise overlap: {sorted(overlap)}"
            )
        wrong_direct = sorted(
            item for item in retire_ids | direct_revise_ids if origin_by_id[item] != "DIRECT"
        )
        wrong_visual = sorted(
            item for item in visual_revise_ids if origin_by_id[item] != "VISUAL_OVERLAY"
        )
        if wrong_direct or wrong_visual:
            raise ValueError(
                "knowledge Skill audit policy origin mismatch: "
                f"direct={wrong_direct}, visual={wrong_visual}"
            )

    def _module_evidence(self, module: DirectSkillModule) -> list[str]:
        raw = self.audit_policy.get("module_evidence", {}).get(module.value)
        if not isinstance(raw, list):
            raise ValueError(f"knowledge Skill audit module evidence missing: {module.value}")
        values = sorted({str(item) for item in raw})
        self._validate_evidence_ids(values, self.evidence)
        return values

    def _evidence_for_skill(self, module: DirectSkillModule, text: str) -> list[str]:
        """Bind common propositions to topic-specific evidence before module fallback."""
        folded = text.casefold()
        raw_routes = self.audit_policy.get("skill_evidence_routes", [])
        if not isinstance(raw_routes, list):
            raise ValueError("knowledge Skill evidence routes must be a list")
        for raw in raw_routes:
            if not isinstance(raw, dict):
                continue
            keywords = [str(item).strip().casefold() for item in raw.get("keywords", [])]
            if any(keyword and keyword in folded for keyword in keywords):
                values = sorted({str(item) for item in raw.get("evidence_ids", [])})
                self._validate_evidence_ids(values, self.evidence)
                return values
        return self._module_evidence(module)

    def _risk_codes(self, text: str, origin: KnowledgeSkillOrigin) -> list[str]:
        codes: list[str] = []
        if _ABSOLUTE_RE.search(text):
            codes.append("OVERCERTAINTY_OR_ABSOLUTE_CLAIM")
        if _NUMERIC_RE.search(text):
            codes.append("FIXED_THRESHOLD_OR_HORIZON")
        if _PERSONAL_FINANCE_RE.search(text):
            codes.append("PERSONAL_FINANCE_SCOPE")
        if _UNVERIFIABLE_INTENT_RE.search(text):
            codes.append("UNVERIFIABLE_ACTOR_INTENT")
        if _DIRECT_TRADE_RE.search(text):
            codes.append("DIRECT_ACTION_FROM_NARRATIVE")
        if origin is KnowledgeSkillOrigin.VISUAL_OVERLAY and _DATED_RE.search(text):
            codes.append("TIME_SPECIFIC_MARKET_COMMENTARY")
        if origin is KnowledgeSkillOrigin.VISUAL_OVERLAY and _SPECIFIC_LEVEL_RE.search(text):
            codes.append("POINT_OR_PRICE_SPECIFIC_RULE")
        return sorted(set(codes))

    def _conflict_groups(self, text: str) -> list[str]:
        result: list[str] = []
        raw_groups = self.audit_policy.get("conflict_groups", {})
        if not isinstance(raw_groups, dict):
            return result
        folded = text.casefold()
        for group, raw_terms in raw_groups.items():
            if not isinstance(raw_terms, list):
                continue
            if any(str(term).casefold() in folded for term in raw_terms):
                result.append(str(group))
        return sorted(set(result))

    @staticmethod
    def _premise_scope(
        module: DirectSkillModule,
        payload: dict[str, object],
        text: str,
    ) -> str:
        conditions = _string_list(payload.get("applicable_conditions"))
        horizon = "UNSPECIFIED"
        folded = text.casefold()
        if any(term in folded for term in ("日内", "超短", "短线", "intraday")):
            horizon = "SHORT"
        elif any(term in folded for term in ("长期", "长持", "多年", "long term", "long-term")):
            horizon = "LONG"
        elif any(term in folded for term in ("中期", "波段", "swing")):
            horizon = "MEDIUM"
        condition_text = " | ".join(conditions[:6]) if conditions else "ORIGINAL_DECISION_CONTEXT"
        return f"module={module.value}; horizon={horizon}; conditions={condition_text}"

    @staticmethod
    def _run_model(row: dict[str, Any]) -> KnowledgeSkillAuditRun:
        payload = json.loads(str(row["run_json"]))
        payload["decision_count"] = int(row["decision_count"])
        payload["status"] = str(row["status"])
        return KnowledgeSkillAuditRun.model_validate(payload)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid YAML: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return {str(key): value for key, value in payload.items()}


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _register_artifact(
    connection: Any,
    *,
    artifact_id: str,
    artifact_type: str,
    schema_version: str,
    object_hash: str,
    input_hashes: list[str],
) -> None:
    encoded_inputs = _json(sorted(set(input_hashes)))
    existing = connection.execute(
        "SELECT type,schema_version,object_hash,input_hashes_json FROM artifact_registry "
        "WHERE artifact_id=?",
        (artifact_id,),
    ).fetchone()
    expected = (artifact_type, schema_version, object_hash, encoded_inputs)
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError(f"knowledge Skill audit artifact collision: {artifact_id}")
        return
    connection.execute(
        "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
        "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
        (artifact_id, *expected, utc_now_text()),
    )


__all__ = ["KnowledgeSkillAuditRepository", "KnowledgeSkillAuditService"]
