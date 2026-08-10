"""Append-only persistence for final knowledge admission and Zhihu visual completion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore, utc_now_text
from astock.schemas.knowledge_completion import (
    DirectKnowledgeSkillReviewDecision,
    KnowledgeSkillRegistryRelease,
)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class KnowledgeCompletionRepository:
    """Persist review decisions, immutable registries, and visual packets."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _register_artifact(
        connection: Any,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: Sequence[str],
    ) -> None:
        serialized = _json(sorted(set(input_hashes)))
        existing = connection.execute(
            "SELECT type,schema_version,object_hash,input_hashes_json "
            "FROM artifact_registry WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        expected = (artifact_type, schema_version, object_hash, serialized)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"Artifact identity collision: {artifact_id}")
            return
        connection.execute(
            "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
            "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
            (artifact_id, *expected, utc_now_text()),
        )

    def review_targets(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT final_skill_id,skill_name,skill_object_hash,status,primary_module,"
                "decision_question,core_principle,uncertainty_reason,skill_json "
                "FROM knowledge_direct_final_skill WHERE run_id=? "
                "AND status='NEEDS_USER_REVIEW' ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def final_skill_by_name(self, run_id: str, skill_name: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT final_skill_id,skill_name,skill_object_hash,status,primary_module,"
                "decision_question,core_principle,uncertainty_reason,skill_json "
                "FROM knowledge_direct_final_skill WHERE run_id=? AND skill_name=?",
                (run_id, skill_name),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"duplicate direct skill name in one run: {skill_name}")
        return dict(rows[0]) if rows else None

    def all_final_rows(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_direct_final_skill WHERE run_id=? ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_hashes(self, run_id: str, final_skill_id: str) -> list[str]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT source_object_hash FROM knowledge_direct_final_source_ref "
                "WHERE run_id=? AND final_skill_id=? ORDER BY ref_ordinal",
                (run_id, final_skill_id),
            ).fetchall()
        return sorted({str(row["source_object_hash"]) for row in rows})

    def source_ref_rows(self, run_id: str, final_skill_id: str) -> list[dict[str, Any]]:
        """Return immutable fragment and slice bindings for one final Skill."""

        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT ref_ordinal,source_id,fragment_object_hash,source_object_hash,"
                "slice_hash,start_offset,end_offset FROM knowledge_direct_final_source_ref "
                "WHERE run_id=? AND final_skill_id=? ORDER BY ref_ordinal",
                (run_id, final_skill_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def review_decisions(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_direct_review_decision WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def put_review_decision(
        self,
        decision: DirectKnowledgeSkillReviewDecision,
        *,
        artifact_id: str,
        object_hash: str,
        decision_json: str,
    ) -> bool:
        """Insert one decision. Return True only for an exact idempotent replay."""

        payload = decision.model_dump(mode="json")
        expected_row = (
            decision.run_id,
            decision.final_skill_id,
            decision.skill_object_hash,
            decision.decision.value,
            decision.actor,
            decision.decided_at.isoformat(),
            decision.reason,
            0,
            artifact_id,
            object_hash,
            decision_json,
        )
        with self.state.transaction() as connection:
            target = connection.execute(
                "SELECT skill_object_hash,status FROM knowledge_direct_final_skill "
                "WHERE run_id=? AND final_skill_id=?",
                (decision.run_id, decision.final_skill_id),
            ).fetchone()
            run = connection.execute(
                "SELECT stage FROM knowledge_direct_run WHERE run_id=?",
                (decision.run_id,),
            ).fetchone()
            if target is None or run is None:
                raise KeyError(f"unknown direct review target: {decision.final_skill_id}")
            if str(run["stage"]) != "FINALIZED":
                raise ValueError("direct review requires a finalized source run")
            if str(target["status"]) != "NEEDS_USER_REVIEW":
                raise ValueError("only NEEDS_USER_REVIEW skills may receive review decisions")
            if str(target["skill_object_hash"]) != decision.skill_object_hash:
                raise ValueError("direct review skill object hash drift")

            existing = connection.execute(
                "SELECT run_id,final_skill_id,skill_object_hash,decision,actor,decided_at,"
                "reason,formal_committee_weight_allowed,decision_artifact_id,"
                "decision_object_hash,decision_json FROM knowledge_direct_review_decision "
                "WHERE run_id=? AND final_skill_id=?",
                (decision.run_id, decision.final_skill_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != expected_row:
                    raise ValueError(
                        f"knowledge review decision collision: {decision.final_skill_id}"
                    )
                return True

            self._register_artifact(
                connection,
                artifact_id=artifact_id,
                artifact_type="DirectKnowledgeSkillReviewDecision",
                schema_version=decision.schema_version,
                object_hash=object_hash,
                input_hashes=[decision.skill_object_hash],
            )
            connection.execute(
                "INSERT INTO knowledge_direct_review_decision("
                "decision_id,run_id,final_skill_id,skill_object_hash,decision,actor,"
                "decided_at,reason,formal_committee_weight_allowed,decision_artifact_id,"
                "decision_object_hash,decision_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (
                    decision.decision_id,
                    payload["run_id"],
                    payload["final_skill_id"],
                    payload["skill_object_hash"],
                    payload["decision"],
                    payload["actor"],
                    decision.decided_at.isoformat(),
                    payload["reason"],
                    artifact_id,
                    object_hash,
                    decision_json,
                    utc_now_text(),
                ),
            )
        return False

    def completion_status(self, run_id: str) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            run = connection.execute(
                "SELECT stage FROM knowledge_direct_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown direct run: {run_id}")
            skill_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM knowledge_direct_final_skill "
                    "WHERE run_id=? GROUP BY status",
                    (run_id,),
                ).fetchall()
            }
            decision_counts = {
                str(row["decision"]): int(row["count"])
                for row in connection.execute(
                    "SELECT decision,COUNT(*) AS count FROM knowledge_direct_review_decision "
                    "WHERE run_id=? GROUP BY decision",
                    (run_id,),
                ).fetchall()
            }
            release = connection.execute(
                "SELECT release_id,registry_version,release_artifact_id,release_object_hash "
                "FROM knowledge_skill_registry_release WHERE run_id=?",
                (run_id,),
            ).fetchone()
        ready = skill_counts.get("READY_FOR_SHADOW", 0)
        needs = skill_counts.get("NEEDS_USER_REVIEW", 0)
        approved = decision_counts.get("APPROVE", 0)
        rejected = decision_counts.get("REJECT", 0)
        pending = needs - approved - rejected
        if pending < 0:
            raise ValueError("knowledge completion decision counts exceed review targets")
        return {
            "run_id": run_id,
            "source_run_stage": str(run["stage"]),
            "total_skill_count": ready + needs,
            "ready_skill_count": ready,
            "needs_user_review_count": needs,
            "pending_review_count": pending,
            "approved_count": approved,
            "rejected_count": rejected,
            "review_closed": pending == 0,
            "registry_version": str(release["registry_version"]) if release else None,
            "registry_release_id": str(release["release_id"]) if release else None,
            "registry_artifact_id": str(release["release_artifact_id"]) if release else None,
            "registry_object_hash": str(release["release_object_hash"]) if release else None,
            "formal_committee_weight_allowed": False,
        }

    def registry_release(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_skill_registry_release WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_published_run_id(self) -> str | None:
        """Return the latest immutable base run that has a published registry."""

        with closing(self.state.connect()) as connection:
            visual = connection.execute(
                "SELECT base_run_id FROM knowledge_visual_skill_release ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if visual is not None:
                return str(visual["base_run_id"])
            direct = connection.execute(
                "SELECT run_id FROM knowledge_skill_registry_release ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return str(direct["run_id"]) if direct is not None else None

    def registry_members(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_skill_registry_member WHERE release_id=? "
                "ORDER BY member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def publish_registry(
        self,
        release: KnowledgeSkillRegistryRelease,
        *,
        release_object_hash: str,
        release_json: str,
        skill_inputs: Mapping[str, Sequence[str]],
        input_hashes: Sequence[str],
    ) -> bool:
        """Publish one immutable admitted registry; exact repeats are idempotent."""

        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT release_id,registry_version,release_object_hash,release_json "
                "FROM knowledge_skill_registry_release WHERE run_id=?",
                (release.run_id,),
            ).fetchone()
            expected_existing = (
                release.release_id,
                release.registry_version,
                release_object_hash,
                release_json,
            )
            if existing is not None:
                if tuple(existing) != expected_existing:
                    raise ValueError(f"knowledge registry collision for run: {release.run_id}")
                return True

            for member in release.members:
                member_inputs = list(skill_inputs.get(member.final_skill_id, ()))
                if sorted(set(member_inputs)) != member.source_hashes:
                    raise ValueError(
                        f"registry source hash drift for skill: {member.final_skill_id}"
                    )
                self._register_artifact(
                    connection,
                    artifact_id=member.skill_artifact_id,
                    artifact_type="KnowledgeSkill",
                    schema_version="direct-source-final-skill-v1",
                    object_hash=member.skill_object_hash,
                    input_hashes=member.source_hashes,
                )

            self._register_artifact(
                connection,
                artifact_id=release.release_artifact_id,
                artifact_type="KnowledgeSkillRegistryRelease",
                schema_version=release.schema_version,
                object_hash=release_object_hash,
                input_hashes=input_hashes,
            )
            connection.execute(
                "INSERT INTO knowledge_skill_registry_release("
                "release_id,registry_version,run_id,total_skill_count,ready_skill_count,"
                "approved_skill_count,rejected_skill_count,admitted_skill_count,"
                "decision_ids_json,member_ids_json,formal_committee_weight_allowed,"
                "release_artifact_id,release_object_hash,release_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (
                    release.release_id,
                    release.registry_version,
                    release.run_id,
                    release.total_skill_count,
                    release.ready_skill_count,
                    release.approved_skill_count,
                    release.rejected_skill_count,
                    release.admitted_skill_count,
                    _json(release.decision_ids),
                    _json([item.final_skill_id for item in release.members]),
                    release.release_artifact_id,
                    release_object_hash,
                    release_json,
                    release.created_at.isoformat(),
                ),
            )
            for member in release.members:
                connection.execute(
                    "INSERT INTO knowledge_skill_registry_member("
                    "release_id,member_ordinal,run_id,final_skill_id,skill_object_hash,"
                    "skill_artifact_id,admission_basis,source_hashes_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        release.release_id,
                        member.member_ordinal,
                        release.run_id,
                        member.final_skill_id,
                        member.skill_object_hash,
                        member.skill_artifact_id,
                        member.admission_basis.value,
                        _json(member.source_hashes),
                    ),
                )
        return False

    def eligible_skill_rows(self, run_id: str) -> list[dict[str, Any]]:
        release = self.registry_release(run_id)
        if release is None:
            return []
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT m.member_ordinal,m.final_skill_id,m.skill_object_hash,"
                "m.skill_artifact_id,m.admission_basis,m.source_hashes_json,"
                "s.status,s.skill_name,s.primary_module,s.decision_question,s.core_principle,"
                "s.secondary_modules_json,s.skill_json "
                "FROM knowledge_skill_registry_member m "
                "JOIN knowledge_direct_final_skill s "
                "ON s.run_id=m.run_id AND s.final_skill_id=m.final_skill_id "
                "WHERE m.release_id=? ORDER BY m.member_ordinal",
                (release["release_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def direct_source_coverage(self, run_id: str) -> dict[str, Any]:
        """Return immutable source identities and frozen direct-run coverage counts."""

        with closing(self.state.connect()) as connection:
            run = connection.execute(
                "SELECT stage,frozen_source_count,frozen_batch_count,manifest_object_hash,"
                "finalized_at FROM knowledge_direct_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown direct run: {run_id}")
            sources = connection.execute(
                "SELECT source_id,source_kind,source_file_hash FROM knowledge_direct_source "
                "WHERE run_id=? ORDER BY source_id",
                (run_id,),
            ).fetchall()
        return {
            "stage": str(run["stage"]),
            "frozen_source_count": int(run["frozen_source_count"]),
            "frozen_batch_count": int(run["frozen_batch_count"]),
            "manifest_object_hash": str(run["manifest_object_hash"]),
            "finalized_at": (str(run["finalized_at"]) if run["finalized_at"] is not None else None),
            "sources": [dict(row) for row in sources],
        }

    def skill_source_chain(self, run_id: str) -> list[dict[str, Any]]:
        """Return hash-only source lineage for every final direct Skill."""

        with closing(self.state.connect()) as connection:
            skills = connection.execute(
                "SELECT final_skill_id,skill_object_hash,status FROM knowledge_direct_final_skill "
                "WHERE run_id=? ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
            refs = connection.execute(
                "SELECT final_skill_id,ref_ordinal,source_id,source_object_hash "
                "FROM knowledge_direct_final_source_ref WHERE run_id=? "
                "ORDER BY final_skill_id,ref_ordinal",
                (run_id,),
            ).fetchall()
        refs_by_skill: dict[str, list[dict[str, Any]]] = {}
        for row in refs:
            refs_by_skill.setdefault(str(row["final_skill_id"]), []).append(
                {
                    "ref_ordinal": int(row["ref_ordinal"]),
                    "source_id": str(row["source_id"]),
                    "source_object_hash": str(row["source_object_hash"]),
                }
            )
        return [
            {
                "final_skill_id": str(row["final_skill_id"]),
                "skill_object_hash": str(row["skill_object_hash"]),
                "status": str(row["status"]),
                "source_refs": refs_by_skill.get(str(row["final_skill_id"]), []),
            }
            for row in skills
        ]

    def visual_author_coverage(self) -> list[dict[str, Any]]:
        """Aggregate only visual placements that have actually been frozen locally."""

        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT p.author_source_id,COUNT(DISTINCT p.source_item_id) AS source_item_count,"
                "COUNT(*) AS placement_count,COUNT(DISTINCT p.asset_id) AS asset_count,"
                "SUM(CASE WHEN packet.packet_status='READY' THEN 1 ELSE 0 END) AS ready_count,"
                "SUM(CASE WHEN packet.packet_status='NEEDS_REVIEW' THEN 1 ELSE 0 END) "
                "AS needs_review_count,"
                "SUM(CASE WHEN packet.packet_id IS NULL THEN 1 ELSE 0 END) AS unresolved_count "
                "FROM knowledge_zhihu_visual_placement p "
                "LEFT JOIN knowledge_zhihu_visual_packet packet "
                "ON packet.placement_id=p.placement_id GROUP BY p.author_source_id "
                "ORDER BY p.author_source_id"
            ).fetchall()
        return [
            {
                "author_source_id": str(row["author_source_id"]),
                "source_item_count": int(row["source_item_count"]),
                "placement_count": int(row["placement_count"]),
                "asset_count": int(row["asset_count"]),
                "ready_count": int(row["ready_count"] or 0),
                "needs_review_count": int(row["needs_review_count"] or 0),
                "unresolved_count": int(row["unresolved_count"] or 0),
            }
            for row in rows
        ]

    def put_visual_capture(
        self,
        *,
        asset: Mapping[str, Any],
        placement: Mapping[str, Any],
        ocr: Mapping[str, Any],
        classification: Mapping[str, Any],
        contexts: Sequence[Mapping[str, Any]],
        rebuilds: Sequence[Mapping[str, Any]],
        packet: Mapping[str, Any],
        packet_input_hashes: Sequence[str],
    ) -> bool:
        """Atomically materialize one immutable, fully linked visual packet."""

        with self.state.transaction() as connection:
            existing_packet = connection.execute(
                "SELECT placement_id,packet_object_hash,packet_json FROM "
                "knowledge_zhihu_visual_packet WHERE packet_id=?",
                (packet["packet_id"],),
            ).fetchone()
            if existing_packet is not None:
                expected = (
                    placement["placement_id"],
                    packet["packet_object_hash"],
                    packet["packet_json"],
                )
                if tuple(existing_packet) != expected:
                    raise ValueError(f"Zhihu visual packet collision: {packet['packet_id']}")
                return True

            existing_asset = connection.execute(
                "SELECT image_object_hash,image_mime,byte_size FROM "
                "knowledge_zhihu_visual_asset WHERE asset_id=?",
                (asset["asset_id"],),
            ).fetchone()
            asset_expected = (
                asset["image_object_hash"],
                asset["image_mime"],
                asset["byte_size"],
            )
            if existing_asset is None:
                connection.execute(
                    "INSERT INTO knowledge_zhihu_visual_asset("
                    "asset_id,image_object_hash,image_mime,byte_size,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (*((asset["asset_id"],) + asset_expected), utc_now_text()),
                )
            elif tuple(existing_asset) != asset_expected:
                raise ValueError(f"Zhihu visual asset collision: {asset['asset_id']}")

            connection.execute(
                "INSERT INTO knowledge_zhihu_visual_placement("
                "placement_id,source_snapshot_id,source_item_id,author_source_id,content_id,"
                "asset_id,url_hash,host_fingerprint,path_fingerprint,redirect_chain_hash,"
                "redirect_count,dom_path,image_ordinal,standalone,merge_policy,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,'MERGE_WITH_BOTH',?)",
                (
                    placement["placement_id"],
                    placement["source_snapshot_id"],
                    placement["source_item_id"],
                    placement["author_source_id"],
                    placement["content_id"],
                    asset["asset_id"],
                    placement["url_hash"],
                    placement["host_fingerprint"],
                    placement["path_fingerprint"],
                    placement["redirect_chain_hash"],
                    placement["redirect_count"],
                    placement["dom_path"],
                    placement["image_ordinal"],
                    utc_now_text(),
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_zhihu_visual_ocr_attempt("
                "placement_id,attempt_status,engine_version,ocr_text_object_hash,confidence,"
                "failure_reason,ocr_record_object_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    placement["placement_id"],
                    ocr["attempt_status"],
                    ocr["engine_version"],
                    ocr.get("ocr_text_object_hash"),
                    ocr.get("confidence"),
                    ocr.get("failure_reason"),
                    ocr["ocr_record_object_hash"],
                    utc_now_text(),
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_zhihu_visual_classification("
                "placement_id,visual_type,classifier_version,confidence,"
                "classification_object_hash,created_at) VALUES(?,?,?,?,?,?)",
                (
                    placement["placement_id"],
                    classification["visual_type"],
                    classification["classifier_version"],
                    classification["confidence"],
                    classification["classification_object_hash"],
                    utc_now_text(),
                ),
            )
            for context in contexts:
                connection.execute(
                    "INSERT INTO knowledge_zhihu_visual_context("
                    "placement_id,context_role,paragraph_id,paragraph_ordinal,text_object_hash) "
                    "VALUES(?,?,?,?,?)",
                    (
                        placement["placement_id"],
                        context["context_role"],
                        context["paragraph_id"],
                        context["paragraph_ordinal"],
                        context["text_object_hash"],
                    ),
                )
            for rebuild in rebuilds:
                connection.execute(
                    "INSERT INTO knowledge_zhihu_visual_argument_rebuild("
                    "placement_id,argument_unit_id,previous_argument_object_hash,"
                    "rebuilt_argument_object_hash,rebuild_status,reason,"
                    "rebuild_record_object_hash) VALUES(?,?,?,?,?,?,?)",
                    (
                        placement["placement_id"],
                        rebuild["argument_unit_id"],
                        rebuild["previous_argument_object_hash"],
                        rebuild.get("rebuilt_argument_object_hash"),
                        rebuild["rebuild_status"],
                        rebuild.get("reason"),
                        rebuild["rebuild_record_object_hash"],
                    ),
                )
            self._register_artifact(
                connection,
                artifact_id=packet["packet_artifact_id"],
                artifact_type="ZhihuVisualCapturePacket",
                schema_version="zhihu-visual-capture-result-v1",
                object_hash=packet["packet_object_hash"],
                input_hashes=packet_input_hashes,
            )
            connection.execute(
                "INSERT INTO knowledge_zhihu_visual_packet("
                "packet_id,placement_id,packet_status,reason_code,stages_json,"
                "packet_artifact_id,packet_object_hash,packet_json,"
                "formal_committee_weight_allowed,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,?)",
                (
                    packet["packet_id"],
                    placement["placement_id"],
                    packet["packet_status"],
                    packet["reason_code"],
                    _json(packet["stages"]),
                    packet["packet_artifact_id"],
                    packet["packet_object_hash"],
                    packet["packet_json"],
                    utc_now_text(),
                ),
            )
        return False

    def visual_status(self) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            packets = {
                str(row["packet_status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT packet_status,COUNT(*) AS count FROM "
                    "knowledge_zhihu_visual_packet GROUP BY packet_status"
                ).fetchall()
            }
            placements = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_zhihu_visual_placement"
                ).fetchone()[0]
            )
            assets = int(
                connection.execute("SELECT COUNT(*) FROM knowledge_zhihu_visual_asset").fetchone()[
                    0
                ]
            )
        return {
            "asset_count": assets,
            "placement_count": placements,
            "packet_status_counts": packets,
            "formal_committee_weight_allowed": False,
        }

    def artifact_object_hash(self, artifact_id: str) -> str | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return str(row["object_hash"]) if row is not None else None


__all__ = ["KnowledgeCompletionRepository"]
