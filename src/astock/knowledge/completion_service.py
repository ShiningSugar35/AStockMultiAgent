"""Final knowledge review, immutable registry publication, and Zhihu visual completion."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.schemas.knowledge_completion import (
    DirectKnowledgeSkillReviewBatch,
    DirectKnowledgeSkillReviewDecision,
    DirectKnowledgeSkillReviewReceipt,
    KnowledgeAdmissionBasis,
    KnowledgeCompletionStatus,
    KnowledgeReviewDecision,
    KnowledgeSkillRegistryMember,
    KnowledgeSkillRegistryRelease,
    KnowledgeSkillRegistryReleaseRecord,
    ZhihuArgumentRebuildStatus,
    ZhihuVisualCaptureRequest,
    ZhihuVisualCaptureResult,
    ZhihuVisualOcrStatus,
    ZhihuVisualPacketStatus,
    ZhihuVisualStage,
    ZhihuVisualType,
)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class KnowledgeCompletionService:
    """Close direct Skill review without mutating the frozen 243-Skill run."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.repository = KnowledgeCompletionRepository(state)

    def _validate_source_lineage(
        self,
        run_id: str,
        final_skill_id: str,
        *,
        fragment_cache: dict[str, str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Validate slice hashes against immutable fragment objects and locators."""

        refs = self.repository.source_ref_rows(run_id, final_skill_id)
        source_hashes = sorted({str(row["source_object_hash"]) for row in refs})
        findings: list[str] = []
        cache = fragment_cache if fragment_cache is not None else {}
        for row in refs:
            ref_key = f"{final_skill_id}:{row['ref_ordinal']}"
            source_hash = str(row["source_object_hash"])
            if source_hash != str(row["slice_hash"]):
                findings.append(f"SOURCE_SLICE_COLUMN_DRIFT:{ref_key}")

            fragment_hash = str(row["fragment_object_hash"])
            if fragment_hash not in cache:
                try:
                    fragment_bytes = self.objects.get_bytes(fragment_hash)
                except StorageError:
                    findings.append(f"SOURCE_FRAGMENT_OBJECT_MISSING:{ref_key}")
                    continue
                try:
                    cache[fragment_hash] = fragment_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    findings.append(f"SOURCE_FRAGMENT_UTF8_INVALID:{ref_key}")
                    continue
            fragment_text = cache[fragment_hash]
            start_offset = int(row["start_offset"])
            end_offset = int(row["end_offset"])
            if (
                start_offset < 0
                or end_offset > len(fragment_text)
                or end_offset <= start_offset
            ):
                findings.append(f"SOURCE_LOCATOR_OUT_OF_BOUNDS:{ref_key}")
                continue
            recomputed = sha256_bytes(
                fragment_text[start_offset:end_offset].encode("utf-8")
            )
            if recomputed != source_hash:
                findings.append(f"SOURCE_SLICE_HASH_MISMATCH:{ref_key}")
        return source_hashes, findings

    @staticmethod
    def load_review_batch(path: Path) -> DirectKnowledgeSkillReviewBatch:
        return DirectKnowledgeSkillReviewBatch.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def review_plan(self, batch: DirectKnowledgeSkillReviewBatch) -> dict[str, object]:
        targets = self.repository.review_targets(batch.run_id)
        if len(targets) != batch.expected_pending_count:
            raise ValueError(
                "review target count drift: "
                f"expected {batch.expected_pending_count}, found {len(targets)}"
            )
        by_name = {str(row["skill_name"]): row for row in targets}
        requested_names = {item.skill_name for item in batch.decisions}
        target_names = set(by_name)
        if requested_names != target_names:
            missing = sorted(target_names - requested_names)
            extra = sorted(requested_names - target_names)
            raise ValueError(
                "review batch does not exactly cover pending skills; "
                f"missing={missing}, extra={extra}"
            )
        planned = []
        for spec in batch.decisions:
            target = by_name[spec.skill_name]
            planned.append(
                {
                    "final_skill_id": str(target["final_skill_id"]),
                    "skill_name": spec.skill_name,
                    "skill_object_hash": str(target["skill_object_hash"]),
                    "decision": spec.decision.value,
                    "reason": spec.reason,
                    "uncertainty_reason": str(target["uncertainty_reason"]),
                }
            )
        counts = {
            decision.value: sum(1 for item in batch.decisions if item.decision is decision)
            for decision in KnowledgeReviewDecision
        }
        return {
            "schema_version": batch.schema_version,
            "run_id": batch.run_id,
            "actor": batch.actor,
            "reviewed_at": batch.reviewed_at.isoformat(),
            "target_count": len(targets),
            "decision_counts": counts,
            "decisions": planned,
            "formal_committee_weight_allowed": False,
        }

    def apply_review_batch(
        self, batch: DirectKnowledgeSkillReviewBatch
    ) -> dict[str, object]:
        plan = self.review_plan(batch)
        receipts: list[DirectKnowledgeSkillReviewReceipt] = []
        for spec in batch.decisions:
            target = self.repository.final_skill_by_name(batch.run_id, spec.skill_name)
            if target is None:
                raise ValueError(f"review target disappeared: {spec.skill_name}")
            skill_hash = str(target["skill_object_hash"])
            if not self.objects.verify(skill_hash):
                raise ValueError(f"review target object is unavailable: {spec.skill_name}")
            seed = {
                "schema_version": "direct-knowledge-review-decision-v1",
                "run_id": batch.run_id,
                "final_skill_id": str(target["final_skill_id"]),
                "skill_object_hash": skill_hash,
                "decision": spec.decision.value,
                "actor": batch.actor,
                "decided_at": batch.reviewed_at.isoformat(),
                "reason": spec.reason,
                "formal_committee_weight_allowed": False,
            }
            identity = sha256_bytes(canonical_json_bytes(seed))
            decision = DirectKnowledgeSkillReviewDecision(
                decision_id=f"knowledge-review:{identity}",
                run_id=batch.run_id,
                final_skill_id=str(target["final_skill_id"]),
                skill_object_hash=skill_hash,
                decision=spec.decision,
                actor=batch.actor,
                decided_at=batch.reviewed_at,
                reason=spec.reason,
            )
            payload = decision.model_dump(mode="json")
            object_ref = self.objects.put_json(payload)
            artifact_id = f"knowledge-review-artifact:{identity}"
            replay = self.repository.put_review_decision(
                decision,
                artifact_id=artifact_id,
                object_hash=object_ref.sha256,
                decision_json=_json(payload),
            )
            receipts.append(
                DirectKnowledgeSkillReviewReceipt(
                    decision=decision,
                    artifact_id=artifact_id,
                    object_hash=object_ref.sha256,
                    idempotent_replay=replay,
                )
            )
        status = self.status(batch.run_id)
        return {
            "status": "REVIEW_CLOSED" if status.review_closed else "NEEDS_INFO",
            "plan": plan,
            "receipts": [item.model_dump(mode="json") for item in receipts],
            "completion": status.model_dump(mode="json"),
            "formal_committee_weight_allowed": False,
        }

    def apply_review_file(self, path: Path) -> dict[str, object]:
        return self.apply_review_batch(self.load_review_batch(path))

    def status(self, run_id: str) -> KnowledgeCompletionStatus:
        return KnowledgeCompletionStatus.model_validate(
            self.repository.completion_status(run_id)
        )

    def publish_registry(self, run_id: str) -> KnowledgeSkillRegistryReleaseRecord:
        existing = self.repository.registry_release(run_id)
        if existing is not None:
            release = KnowledgeSkillRegistryRelease.model_validate_json(
                str(existing["release_json"])
            )
            object_hash = str(existing["release_object_hash"])
            release_json = str(existing["release_json"])
            if sha256_bytes(release_json.encode("utf-8")) != object_hash:
                raise ValueError("existing knowledge registry JSON hash drift")
            if not self.objects.verify(object_hash):
                raise ValueError("existing knowledge registry object is unavailable")
            if (
                self.repository.artifact_object_hash(str(existing["release_artifact_id"]))
                != object_hash
            ):
                raise ValueError("existing knowledge registry artifact drift")
            return KnowledgeSkillRegistryReleaseRecord(
                release=release,
                object_hash=object_hash,
                idempotent_replay=True,
            )

        status = self.status(run_id)
        if status.source_run_stage != "FINALIZED":
            raise ValueError("knowledge registry requires a finalized direct run")
        if not status.review_closed:
            raise ValueError(
                f"knowledge registry review remains open: pending={status.pending_review_count}"
            )

        decisions = self.repository.review_decisions(run_id)
        decision_by_skill = {str(row["final_skill_id"]): row for row in decisions}
        source_coverage = self.repository.direct_source_coverage(run_id)
        finalized_at = source_coverage.get("finalized_at")
        if finalized_at is None:
            raise ValueError("knowledge registry requires a finalized_at source timestamp")
        created_at = max(
            [datetime.fromisoformat(str(row["decided_at"])) for row in decisions]
            or [datetime.fromisoformat(str(finalized_at))]
        )
        members: list[KnowledgeSkillRegistryMember] = []
        fragment_cache: dict[str, str] = {}
        source_map: dict[str, list[str]] = {}
        for row in self.repository.all_final_rows(run_id):
            final_skill_id = str(row["final_skill_id"])
            skill_hash = str(row["skill_object_hash"])
            skill_status = str(row["status"])
            admission_basis: KnowledgeAdmissionBasis | None = None
            if skill_status == "READY_FOR_SHADOW":
                admission_basis = KnowledgeAdmissionBasis.READY
            elif skill_status == "NEEDS_USER_REVIEW":
                decision = decision_by_skill.get(final_skill_id)
                if decision is None:
                    raise ValueError(f"missing review decision for {final_skill_id}")
                if str(decision["skill_object_hash"]) != skill_hash:
                    raise ValueError(f"review decision hash drift for {final_skill_id}")
                if str(decision["decision"]) == KnowledgeReviewDecision.APPROVE.value:
                    admission_basis = KnowledgeAdmissionBasis.APPROVED
                elif str(decision["decision"]) != KnowledgeReviewDecision.REJECT.value:
                    raise ValueError(f"unknown review decision for {final_skill_id}")
            else:
                raise ValueError(f"unknown final skill status: {skill_status}")

            if admission_basis is None:
                continue
            skill_json = str(row["skill_json"])
            if sha256_bytes(skill_json.encode("utf-8")) != skill_hash:
                raise ValueError(f"final Skill JSON hash drift: {final_skill_id}")
            try:
                skill_payload = json.loads(skill_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"final Skill JSON is invalid: {final_skill_id}") from exc
            if not isinstance(skill_payload, dict):
                raise ValueError(f"final Skill JSON is invalid: {final_skill_id}")
            try:
                secondary_modules = json.loads(str(row["secondary_modules_json"]))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"final Skill secondary modules JSON is invalid: {final_skill_id}"
                ) from exc
            if not isinstance(secondary_modules, list):
                raise ValueError(
                    f"final Skill secondary modules JSON is invalid: {final_skill_id}"
                )
            if (
                skill_payload.get("final_skill_id") != final_skill_id
                or skill_payload.get("status") != skill_status
                or skill_payload.get("skill_name") != str(row["skill_name"])
                or skill_payload.get("primary_module") != str(row["primary_module"])
                or skill_payload.get("secondary_modules") != secondary_modules
                or skill_payload.get("decision_question") != str(row["decision_question"])
                or skill_payload.get("core_principle") != str(row["core_principle"])
                or skill_payload.get("formal_committee_weight_allowed") is not False
            ):
                raise ValueError(f"final Skill column binding drift: {final_skill_id}")
            if not self.objects.verify(skill_hash):
                raise ValueError(f"final Skill object is unavailable: {final_skill_id}")
            source_hashes, source_findings = self._validate_source_lineage(
                run_id,
                final_skill_id,
                fragment_cache=fragment_cache,
            )
            if not source_hashes:
                raise ValueError(f"admitted Skill has no source hashes: {final_skill_id}")
            if source_findings:
                raise ValueError(f"final Skill source lineage invalid: {source_findings[0]}")
            payload_source_hashes = sorted(
                {
                    str(item.get("source_object_hash"))
                    for item in skill_payload.get("source_refs", [])
                    if isinstance(item, dict) and item.get("source_object_hash")
                }
            )
            if payload_source_hashes != source_hashes:
                raise ValueError(f"final Skill source binding drift: {final_skill_id}")
            source_map[final_skill_id] = source_hashes
            members.append(
                KnowledgeSkillRegistryMember(
                    member_ordinal=1,
                    final_skill_id=final_skill_id,
                    skill_object_hash=skill_hash,
                    skill_artifact_id=f"knowledge-skill:{skill_hash}",
                    admission_basis=admission_basis,
                    source_hashes=source_hashes,
                )
            )

        members.sort(key=lambda item: item.final_skill_id)
        members = [
            item.model_copy(update={"member_ordinal": ordinal})
            for ordinal, item in enumerate(members, start=1)
        ]
        decision_ids = sorted(str(row["decision_id"]) for row in decisions)
        identity_seed = {
            "schema_version": "knowledge-skill-registry-release-v1",
            "run_id": run_id,
            "decision_ids": decision_ids,
            "members": [
                {
                    "final_skill_id": item.final_skill_id,
                    "skill_object_hash": item.skill_object_hash,
                    "admission_basis": item.admission_basis.value,
                    "source_hashes": item.source_hashes,
                }
                for item in members
            ],
            "formal_committee_weight_allowed": False,
        }
        identity = sha256_bytes(canonical_json_bytes(identity_seed))
        release = KnowledgeSkillRegistryRelease(
            release_id=f"knowledge-registry:{identity}",
            registry_version=f"knowledge-registry-v1:{identity[:16]}",
            run_id=run_id,
            total_skill_count=status.total_skill_count,
            ready_skill_count=status.ready_skill_count,
            approved_skill_count=status.approved_count,
            rejected_skill_count=status.rejected_count,
            admitted_skill_count=len(members),
            decision_ids=decision_ids,
            members=members,
            release_artifact_id=f"knowledge-registry-release:{identity}",
            created_at=created_at,
        )
        release_payload = release.model_dump(mode="json")
        object_ref = self.objects.put_json(release_payload)
        input_hashes = sorted(
            {
                *(item.skill_object_hash for item in members),
                *(str(row["decision_object_hash"]) for row in decisions),
            }
        )
        replay = self.repository.publish_registry(
            release,
            release_object_hash=object_ref.sha256,
            release_json=_json(release_payload),
            skill_inputs=source_map,
            input_hashes=input_hashes,
        )
        return KnowledgeSkillRegistryReleaseRecord(
            release=release,
            object_hash=object_ref.sha256,
            idempotent_replay=replay,
        )

    def audit(
        self,
        run_id: str,
        *,
        require_registry: bool = True,
    ) -> dict[str, object]:
        findings: list[str] = []
        status = self.status(run_id)
        fragment_cache: dict[str, str] = {}
        rows = self.repository.all_final_rows(run_id)
        if len(rows) != status.total_skill_count:
            findings.append("FINAL_SKILL_COUNT_DRIFT")
        for row in rows:
            final_skill_id = str(row["final_skill_id"])
            skill_hash = str(row["skill_object_hash"])
            skill_json = str(row["skill_json"])
            if not self.objects.verify(skill_hash):
                findings.append(f"MISSING_SKILL_OBJECT:{final_skill_id}")
            if sha256_bytes(skill_json.encode("utf-8")) != skill_hash:
                findings.append(f"SKILL_JSON_HASH_DRIFT:{final_skill_id}")
                continue
            try:
                payload = json.loads(skill_json)
            except json.JSONDecodeError:
                findings.append(f"SKILL_JSON_INVALID:{final_skill_id}")
                continue
            if not isinstance(payload, dict):
                findings.append(f"SKILL_JSON_INVALID:{final_skill_id}")
                continue
            try:
                secondary_modules = json.loads(str(row["secondary_modules_json"]))
            except json.JSONDecodeError:
                findings.append(f"SKILL_SECONDARY_MODULES_INVALID:{final_skill_id}")
                continue
            if not isinstance(secondary_modules, list):
                findings.append(f"SKILL_SECONDARY_MODULES_INVALID:{final_skill_id}")
                continue
            if (
                payload.get("final_skill_id") != final_skill_id
                or payload.get("status") != str(row["status"])
                or payload.get("skill_name") != str(row["skill_name"])
                or payload.get("primary_module") != str(row["primary_module"])
                or payload.get("secondary_modules") != secondary_modules
                or payload.get("decision_question") != str(row["decision_question"])
                or payload.get("core_principle") != str(row["core_principle"])
                or payload.get("formal_committee_weight_allowed") is not False
            ):
                findings.append(f"SKILL_COLUMN_BINDING_DRIFT:{final_skill_id}")
            payload_source_hashes = sorted(
                {
                    str(item.get("source_object_hash"))
                    for item in payload.get("source_refs", [])
                    if isinstance(item, dict) and item.get("source_object_hash")
                }
            )
            stored_source_hashes, source_findings = self._validate_source_lineage(
                run_id,
                final_skill_id,
                fragment_cache=fragment_cache,
            )
            findings.extend(source_findings)
            if payload_source_hashes != stored_source_hashes:
                findings.append(f"SKILL_SOURCE_BINDING_DRIFT:{final_skill_id}")
        decisions = self.repository.review_decisions(run_id)
        for row in decisions:
            object_hash = str(row["decision_object_hash"])
            if not self.objects.verify(object_hash):
                findings.append(f"MISSING_REVIEW_OBJECT:{row['final_skill_id']}")
            if (
                self.repository.artifact_object_hash(str(row["decision_artifact_id"]))
                != object_hash
            ):
                findings.append(f"REVIEW_ARTIFACT_DRIFT:{row['final_skill_id']}")

        release = self.repository.registry_release(run_id)
        if require_registry and status.review_closed and release is None:
            findings.append("REGISTRY_NOT_PUBLISHED")
        if release is not None:
            release_hash = str(release["release_object_hash"])
            release_json = str(release["release_json"])
            if sha256_bytes(release_json.encode("utf-8")) != release_hash:
                findings.append("REGISTRY_JSON_HASH_DRIFT")
            if not self.objects.verify(release_hash):
                findings.append("MISSING_REGISTRY_OBJECT")
            if (
                self.repository.artifact_object_hash(str(release["release_artifact_id"]))
                != release_hash
            ):
                findings.append("REGISTRY_ARTIFACT_DRIFT")
            members = self.repository.registry_members(str(release["release_id"]))
            expected = status.ready_skill_count + status.approved_count
            if len(members) != expected:
                findings.append("REGISTRY_MEMBER_COUNT_DRIFT")
            try:
                release_model = KnowledgeSkillRegistryRelease.model_validate_json(
                    release_json
                )
            except ValueError:
                findings.append("REGISTRY_JSON_INVALID")
                release_model = None
            if release_model is not None:
                row_binding = (
                    release_model.release_id == str(release["release_id"])
                    and release_model.registry_version == str(release["registry_version"])
                    and release_model.run_id == str(release["run_id"])
                    and release_model.total_skill_count == int(release["total_skill_count"])
                    and release_model.ready_skill_count == int(release["ready_skill_count"])
                    and release_model.approved_skill_count
                    == int(release["approved_skill_count"])
                    and release_model.rejected_skill_count
                    == int(release["rejected_skill_count"])
                    and release_model.admitted_skill_count
                    == int(release["admitted_skill_count"])
                    and release_model.release_artifact_id
                    == str(release["release_artifact_id"])
                    and release_model.created_at.isoformat() == str(release["created_at"])
                    and release_model.formal_committee_weight_allowed is False
                )
                if not row_binding:
                    findings.append("REGISTRY_ROW_BINDING_DRIFT")
                actual_decision_ids = sorted(
                    str(row["decision_id"]) for row in decisions
                )
                if release_model.decision_ids != actual_decision_ids:
                    findings.append("REGISTRY_DECISION_BINDING_DRIFT")
                try:
                    stored_decision_ids = json.loads(str(release["decision_ids_json"]))
                    stored_member_ids = json.loads(str(release["member_ids_json"]))
                except json.JSONDecodeError:
                    findings.append("REGISTRY_INDEX_JSON_INVALID")
                    stored_decision_ids = None
                    stored_member_ids = None
                model_member_ids = [
                    item.final_skill_id for item in release_model.members
                ]
                if stored_decision_ids != release_model.decision_ids:
                    findings.append("REGISTRY_DECISION_INDEX_DRIFT")
                if stored_member_ids != model_member_ids:
                    findings.append("REGISTRY_MEMBER_INDEX_DRIFT")
                if len(release_model.members) != len(members):
                    findings.append("REGISTRY_MEMBER_JSON_COUNT_DRIFT")
                else:
                    for model_member, member in zip(
                        release_model.members,
                        members,
                        strict=True,
                    ):
                        try:
                            stored_source_hashes = json.loads(
                                str(member["source_hashes_json"])
                            )
                        except json.JSONDecodeError:
                            findings.append(
                                "REGISTRY_MEMBER_SOURCE_JSON_INVALID:"
                                f"{member['final_skill_id']}"
                            )
                            continue
                        if (
                            model_member.member_ordinal != int(member["member_ordinal"])
                            or model_member.final_skill_id
                            != str(member["final_skill_id"])
                            or model_member.skill_object_hash
                            != str(member["skill_object_hash"])
                            or model_member.skill_artifact_id
                            != str(member["skill_artifact_id"])
                            or model_member.admission_basis.value
                            != str(member["admission_basis"])
                            or model_member.source_hashes != stored_source_hashes
                        ):
                            findings.append(
                                "REGISTRY_MEMBER_BINDING_DRIFT:"
                                f"{member['final_skill_id']}"
                            )
            for member in members:
                member_hash = str(member["skill_object_hash"])
                if not self.objects.verify(member_hash):
                    findings.append(
                        f"REGISTRY_SKILL_OBJECT_MISSING:{member['final_skill_id']}"
                    )
                if (
                    self.repository.artifact_object_hash(str(member["skill_artifact_id"]))
                    != member_hash
                ):
                    findings.append(
                        f"REGISTRY_SKILL_ARTIFACT_DRIFT:{member['final_skill_id']}"
                    )

        integrity_tables = (
            "artifact_registry",
            "knowledge_direct_run",
            "knowledge_direct_final_skill",
            "knowledge_direct_final_source_ref",
            "knowledge_direct_shadow_bundle",
            "knowledge_direct_review_decision",
            "knowledge_skill_registry_release",
            "knowledge_skill_registry_member",
        )
        with self.state.connect() as connection:
            integrity_results = {
                table: connection.execute(f"PRAGMA integrity_check({table})").fetchall()
                for table in integrity_tables
            }
            foreign_key_results = {
                table: connection.execute(f"PRAGMA foreign_key_check({table})").fetchall()
                for table in integrity_tables
            }
            unsafe_review = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_direct_review_decision "
                    "WHERE formal_committee_weight_allowed != 0"
                ).fetchone()[0]
            )
            unsafe_release = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_skill_registry_release "
                    "WHERE formal_committee_weight_allowed != 0"
                ).fetchone()[0]
            )
            shadow = connection.execute(
                "SELECT bundle_object_hash,bundle_json,formal_committee_weight_allowed "
                "FROM knowledge_direct_shadow_bundle WHERE run_id=?",
                (run_id,),
            ).fetchone()
        for table, result_rows in integrity_results.items():
            if len(result_rows) != 1 or str(result_rows[0][0]) != "ok":
                findings.append(f"SQLITE_INTEGRITY_FAILED:{table}")
        for table, result_rows in foreign_key_results.items():
            if result_rows:
                findings.append(f"SQLITE_FOREIGN_KEY_FAILED:{table}")
        if unsafe_review or unsafe_release:
            findings.append("FORMAL_COMMITTEE_WEIGHT_ENABLED")
        if shadow is None:
            findings.append("SOURCE_SHADOW_BUNDLE_MISSING")
        else:
            shadow_hash = str(shadow["bundle_object_hash"])
            shadow_json = str(shadow["bundle_json"])
            if (
                sha256_bytes(shadow_json.encode("utf-8")) != shadow_hash
                or not self.objects.verify(shadow_hash)
            ):
                findings.append("SOURCE_SHADOW_BUNDLE_HASH_DRIFT")
            if bool(shadow["formal_committee_weight_allowed"]):
                findings.append("SOURCE_SHADOW_BUNDLE_UNSAFE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "run_id": run_id,
            "completion": status.model_dump(mode="json"),
            "review_decision_count": len(decisions),
            "registry_member_count": (
                len(self.repository.registry_members(str(release["release_id"])))
                if release is not None
                else 0
            ),
            "source_shadow_bundle_hash": (
                str(shadow["bundle_object_hash"]) if shadow is not None else None
            ),
            "sqlite_integrity_check": (
                "ok"
                if all(
                    len(result_rows) == 1 and str(result_rows[0][0]) == "ok"
                    for result_rows in integrity_results.values()
                )
                else "failed"
            ),
            "sqlite_integrity_scope": "KNOWLEDGE_COMPLETION_TABLES",
            "sqlite_integrity_tables": list(integrity_tables),
            "foreign_key_check_count": sum(
                len(rows) for rows in foreign_key_results.values()
            ),
            "findings": findings,
            "formal_committee_weight_allowed": False,
        }

    def report(self, run_id: str) -> dict[str, object]:
        status = self.status(run_id)
        decisions = self.repository.review_decisions(run_id)
        source_coverage = self.repository.direct_source_coverage(run_id)
        source_chain = self.repository.skill_source_chain(run_id)
        visual_author_coverage = self.repository.visual_author_coverage()
        source_chain_hash = sha256_bytes(canonical_json_bytes(source_chain))
        return {
            "schema_version": "knowledge-completion-report-v1",
            "run_id": run_id,
            "status": status.model_dump(mode="json"),
            "direct_source_coverage": source_coverage,
            "skill_source_chain": {
                "skill_count": len(source_chain),
                "skills_with_source_refs": sum(
                    1 for item in source_chain if item["source_refs"]
                ),
                "binding_hash": source_chain_hash,
                "skills": source_chain,
            },
            "decisions": [
                {
                    "final_skill_id": str(row["final_skill_id"]),
                    "decision": str(row["decision"]),
                    "artifact_id": str(row["decision_artifact_id"]),
                    "object_hash": str(row["decision_object_hash"]),
                    "reason": str(row["reason"]),
                }
                for row in decisions
            ],
            "registry": {
                "version": status.registry_version,
                "release_id": status.registry_release_id,
                "artifact_id": status.registry_artifact_id,
                "object_hash": status.registry_object_hash,
                "published": status.registry_release_id is not None,
            },
            "visual_completion": {
                **self.repository.visual_status(),
                "coverage_scope": "CAPTURED_VISUAL_PLACEMENTS_ONLY",
                "authors": visual_author_coverage,
                "real_visual_completion_claimed": False,
            },
            "formal_committee_weight_allowed": False,
        }


class ZhihuVisualCompletionService:
    """Freeze already-authorized Zhihu image bytes without fetching or bypassing access controls."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.repository = KnowledgeCompletionRepository(state)

    @staticmethod
    def load_request(path: Path) -> ZhihuVisualCaptureRequest:
        return ZhihuVisualCaptureRequest.model_validate_json(path.read_text(encoding="utf-8"))

    def capture(
        self,
        request: ZhihuVisualCaptureRequest,
        image_bytes: bytes,
    ) -> ZhihuVisualCaptureResult:
        if not image_bytes:
            raise ValueError("Zhihu visual image bytes are empty")
        detected_mime = _image_mime(image_bytes)
        declared_mime = request.response_mime.split(";", 1)[0].strip().lower()
        if detected_mime is None:
            raise ValueError("Zhihu visual payload is not an allowlisted image format")
        if declared_mime != detected_mime:
            raise ValueError(
                f"Zhihu visual MIME mismatch: declared={declared_mime}, detected={detected_mime}"
            )

        parsed = urlsplit(request.image_url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        url_hash = sha256_bytes(request.image_url.encode("utf-8"))
        host_fingerprint = sha256_bytes(host.encode("utf-8"))
        path_fingerprint = sha256_bytes(path.encode("utf-8"))
        redirect_hashes = [sha256_bytes(item.encode("utf-8")) for item in request.redirect_chain]
        redirect_chain_hash = sha256_bytes(canonical_json_bytes(redirect_hashes))

        image_ref = self.objects.put_bytes(image_bytes)
        asset_id = f"zhihu-visual-asset:{image_ref.sha256}"
        asset = {
            "asset_id": asset_id,
            "image_object_hash": image_ref.sha256,
            "image_mime": detected_mime,
            "byte_size": image_ref.byte_size,
        }
        placement = {
            "placement_id": request.placement_id,
            "source_snapshot_id": request.source_snapshot_id,
            "source_item_id": request.source_item_id,
            "author_source_id": request.author_source_id,
            "content_id": request.content_id,
            "url_hash": url_hash,
            "host_fingerprint": host_fingerprint,
            "path_fingerprint": path_fingerprint,
            "redirect_chain_hash": redirect_chain_hash,
            "redirect_count": len(request.redirect_chain),
            "dom_path": request.dom_locator.dom_path,
            "image_ordinal": request.dom_locator.image_ordinal,
        }

        ocr_text_hash: str | None = None
        if request.ocr.status is ZhihuVisualOcrStatus.SUCCEEDED:
            assert request.ocr.text is not None
            ocr_text_hash = self.objects.put_bytes(request.ocr.text.encode("utf-8")).sha256
        ocr_record = {
            "schema_version": "zhihu-visual-ocr-record-v1",
            "placement_id": request.placement_id,
            "attempt_status": request.ocr.status.value,
            "engine_version": request.ocr.engine_version,
            "ocr_text_object_hash": ocr_text_hash,
            "confidence": request.ocr.confidence,
            "failure_reason": request.ocr.failure_reason,
        }
        ocr_record_ref = self.objects.put_json(ocr_record)
        ocr = {
            **ocr_record,
            "ocr_record_object_hash": ocr_record_ref.sha256,
        }

        classification_record = {
            "schema_version": "zhihu-visual-classification-v1",
            "placement_id": request.placement_id,
            "visual_type": request.classification.visual_type.value,
            "classifier_version": request.classification.classifier_version,
            "confidence": request.classification.confidence,
        }
        classification_ref = self.objects.put_json(classification_record)
        classification = {
            **classification_record,
            "classification_object_hash": classification_ref.sha256,
        }

        contexts: list[dict[str, object]] = []
        context_hashes: list[str] = []
        for role, context in (
            ("PRECEDING", request.preceding_context),
            ("FOLLOWING", request.following_context),
        ):
            text_ref = self.objects.put_bytes(context.text.encode("utf-8"))
            contexts.append(
                {
                    "context_role": role,
                    "paragraph_id": context.paragraph_id,
                    "paragraph_ordinal": context.paragraph_ordinal,
                    "text_object_hash": text_ref.sha256,
                }
            )
            context_hashes.append(text_ref.sha256)

        rebuilds: list[dict[str, object]] = []
        rebuild_hashes: list[str] = []
        unresolved_rebuild = False
        for rebuild in request.affected_argument_rebuilds:
            if not self.objects.verify(rebuild.previous_argument_object_hash):
                raise ValueError(
                    f"previous AU object is unavailable: {rebuild.argument_unit_id}"
                )
            if rebuild.rebuilt_argument_object_hash is not None and not self.objects.verify(
                rebuild.rebuilt_argument_object_hash
            ):
                raise ValueError(
                    f"rebuilt AU object is unavailable: {rebuild.argument_unit_id}"
                )
            if rebuild.status is ZhihuArgumentRebuildStatus.NEEDS_REVIEW:
                unresolved_rebuild = True
            record = {
                "schema_version": "zhihu-visual-au-rebuild-v1",
                "placement_id": request.placement_id,
                "argument_unit_id": rebuild.argument_unit_id,
                "previous_argument_object_hash": rebuild.previous_argument_object_hash,
                "rebuilt_argument_object_hash": rebuild.rebuilt_argument_object_hash,
                "rebuild_status": rebuild.status.value,
                "reason": rebuild.reason,
                "merge_policy": "MERGE_WITH_BOTH",
            }
            record_ref = self.objects.put_json(record)
            rebuild_hashes.append(record_ref.sha256)
            rebuilds.append({**record, "rebuild_record_object_hash": record_ref.sha256})

        needs_review = (
            request.ocr.status is ZhihuVisualOcrStatus.FAILED
            or (
                request.ocr.status is ZhihuVisualOcrStatus.NO_TEXT
                and request.classification.visual_type is not ZhihuVisualType.DECORATIVE
            )
            or unresolved_rebuild
            or request.classification.visual_type is ZhihuVisualType.OTHER
            or request.classification.confidence < 0.5
        )
        packet_status = (
            ZhihuVisualPacketStatus.NEEDS_REVIEW
            if needs_review
            else ZhihuVisualPacketStatus.READY
        )
        reason_code = "VISUAL_REVIEW_REQUIRED" if needs_review else "PACKET_READY"
        stages = [
            ZhihuVisualStage.IMAGE_URL_INVENTORIED,
            ZhihuVisualStage.ACCESS_POLICY_VERIFIED,
            ZhihuVisualStage.IMAGE_SNAPSHOT_FROZEN,
            ZhihuVisualStage.DOM_LOCATED,
            ZhihuVisualStage.OCR_ATTEMPTED,
            ZhihuVisualStage.VISUAL_CLASSIFIED,
            ZhihuVisualStage.CONTEXT_ASSEMBLED,
            ZhihuVisualStage.AFFECTED_AU_REBUILT,
            (
                ZhihuVisualStage.REVIEW_REQUIRED
                if needs_review
                else ZhihuVisualStage.PACKET_READY
            ),
        ]
        packet_seed = {
            "schema_version": "zhihu-visual-capture-packet-v1",
            "placement_id": request.placement_id,
            "asset_id": asset_id,
            "image_object_hash": image_ref.sha256,
            "url_hash": url_hash,
            "host_fingerprint": host_fingerprint,
            "path_fingerprint": path_fingerprint,
            "redirect_chain_hash": redirect_chain_hash,
            "ocr_record_object_hash": ocr_record_ref.sha256,
            "classification_object_hash": classification_ref.sha256,
            "context_hashes": sorted(context_hashes),
            "rebuild_record_hashes": sorted(rebuild_hashes),
            "packet_status": packet_status.value,
            "reason_code": reason_code,
            "stages": [item.value for item in stages],
            "standalone": False,
            "merge_policy": "MERGE_WITH_BOTH",
            "formal_committee_weight_allowed": False,
        }
        packet_identity = sha256_bytes(canonical_json_bytes(packet_seed))
        packet_id = f"zhihu-visual-packet:{packet_identity}"
        packet_artifact_id = f"zhihu-visual-packet-artifact:{packet_identity}"
        packet_ref = self.objects.put_json({**packet_seed, "packet_id": packet_id})
        stored_result = ZhihuVisualCaptureResult(
            packet_id=packet_id,
            placement_id=request.placement_id,
            asset_id=asset_id,
            image_object_hash=image_ref.sha256,
            image_mime=detected_mime,
            url_hash=url_hash,
            host_fingerprint=host_fingerprint,
            path_fingerprint=path_fingerprint,
            redirect_chain_hash=redirect_chain_hash,
            packet_status=packet_status,
            reason_code=reason_code,
            stages=stages,
            packet_artifact_id=packet_artifact_id,
            packet_object_hash=packet_ref.sha256,
            idempotent_replay=False,
        )
        stored_payload = stored_result.model_dump(mode="json")
        replay = self.repository.put_visual_capture(
            asset=asset,
            placement=placement,
            ocr=ocr,
            classification=classification,
            contexts=contexts,
            rebuilds=rebuilds,
            packet={
                "packet_id": packet_id,
                "packet_status": packet_status.value,
                "reason_code": reason_code,
                "stages": [item.value for item in stages],
                "packet_artifact_id": packet_artifact_id,
                "packet_object_hash": packet_ref.sha256,
                "packet_json": _json(stored_payload),
            },
            packet_input_hashes=[
                image_ref.sha256,
                ocr_record_ref.sha256,
                classification_ref.sha256,
                *context_hashes,
                *rebuild_hashes,
            ],
        )
        return stored_result.model_copy(update={"idempotent_replay": replay})

    def capture_file(self, request_file: Path, image_file: Path) -> ZhihuVisualCaptureResult:
        return self.capture(self.load_request(request_file), image_file.read_bytes())

    def status(self) -> dict[str, object]:
        return self.repository.visual_status()


__all__ = ["KnowledgeCompletionService", "ZhihuVisualCompletionService"]
