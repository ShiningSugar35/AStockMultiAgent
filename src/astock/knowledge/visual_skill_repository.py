"""Append-only repository for visual-enhanced Zhihu Skill generation and release."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore, utc_now_text


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class VisualSkillRepository:
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
        encoded_inputs = _json(sorted(set(input_hashes)))
        existing = connection.execute(
            "SELECT type,schema_version,object_hash,input_hashes_json "
            "FROM artifact_registry WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        expected = (artifact_type, schema_version, object_hash, encoded_inputs)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"visual Skill artifact collision: {artifact_id}")
            return
        connection.execute(
            "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
            "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
            (artifact_id, *expected, utc_now_text()),
        )

    def visual_argument_rows(self, author_source_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT p.author_source_id,p.source_snapshot_id,"
                "snapshot.object_hash AS source_snapshot_object_hash,"
                "a.run_id AS semantic_run_id,a.argument_unit_id,a.text_object_hash "
                "AS argument_text_object_hash,a.topic_relevance,"
                "a.methodological_completeness,a.unit_json,r.rebuilt_argument_object_hash,"
                "p.placement_id,packet.packet_artifact_id,packet.packet_object_hash,"
                "packet.packet_status,asset.image_object_hash,ocr.attempt_status AS ocr_status,"
                "ocr.ocr_text_object_hash,classification.visual_type,"
                "classification.confidence AS classification_confidence,"
                "classification.classification_object_hash "
                "FROM knowledge_zhihu_visual_argument_rebuild r "
                "JOIN knowledge_zhihu_visual_placement p ON p.placement_id=r.placement_id "
                "JOIN knowledge_zhihu_visual_packet packet ON packet.placement_id=p.placement_id "
                "JOIN knowledge_zhihu_visual_asset asset ON asset.asset_id=p.asset_id "
                "JOIN knowledge_zhihu_visual_ocr_attempt ocr "
                "ON ocr.placement_id=p.placement_id "
                "JOIN knowledge_zhihu_visual_classification classification "
                "ON classification.placement_id=p.placement_id "
                "JOIN source_snapshot_index snapshot ON snapshot.snapshot_id=p.source_snapshot_id "
                "JOIN knowledge_argument_unit a ON a.argument_unit_id=r.argument_unit_id "
                "WHERE p.author_source_id=? AND packet.packet_status='READY' "
                "AND r.rebuild_status='READY' ORDER BY a.argument_unit_id,p.placement_id",
                (author_source_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def generation_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_visual_skill_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_generation_for_base(self, base_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_visual_skill_run WHERE base_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (base_run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def candidates(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_visual_skill_candidate WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def no_skills(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_visual_skill_no_skill WHERE run_id=? "
                "ORDER BY argument_unit_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def review_decisions(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_visual_skill_review_decision WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_release(self, base_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_visual_skill_release WHERE base_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (base_run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_release_any(self) -> dict[str, Any] | None:
        """Return the latest immutable visual-composite registry release."""

        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_visual_skill_release ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def release(self, release_id: str) -> dict[str, Any] | None:
        """Return one immutable visual-composite registry release by identity."""

        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_visual_skill_release WHERE release_id=?",
                (release_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def release_members(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_visual_skill_member WHERE release_id=? "
                "ORDER BY member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def put_generation(
        self,
        *,
        run_row: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        no_skills: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> bool:
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT run_object_hash,run_json,candidate_count,no_skill_count "
                "FROM knowledge_visual_skill_run WHERE run_id=?",
                (run_row["run_id"],),
            ).fetchone()
            if existing is not None:
                expected = (
                    run_row["run_object_hash"],
                    run_row["run_json"],
                    run_row["candidate_count"],
                    run_row["no_skill_count"],
                )
                if tuple(existing) != expected:
                    raise ValueError(f"visual Skill generation collision: {run_row['run_id']}")
                return True

            for artifact in artifacts:
                self._register_artifact(
                    connection,
                    artifact_id=str(artifact["artifact_id"]),
                    artifact_type=str(artifact["artifact_type"]),
                    schema_version=str(artifact["schema_version"]),
                    object_hash=str(artifact["object_hash"]),
                    input_hashes=list(artifact["input_hashes"]),
                )

            connection.execute(
                "INSERT INTO knowledge_visual_skill_run("
                "run_id,base_run_id,base_registry_release_id,base_registry_object_hash,"
                "generation_policy_version,author_source_ids_json,semantic_run_ids_json,"
                "visual_pack_artifact_ids_json,visual_pack_object_hashes_json,"
                "evaluated_argument_count,candidate_count,no_skill_count,run_artifact_id,"
                "run_object_hash,run_json,formal_committee_weight_allowed,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (
                    run_row["run_id"],
                    run_row["base_run_id"],
                    run_row["base_registry_release_id"],
                    run_row["base_registry_object_hash"],
                    run_row["generation_policy_version"],
                    run_row["author_source_ids_json"],
                    run_row["semantic_run_ids_json"],
                    run_row["visual_pack_artifact_ids_json"],
                    run_row["visual_pack_object_hashes_json"],
                    run_row["evaluated_argument_count"],
                    run_row["candidate_count"],
                    run_row["no_skill_count"],
                    run_row["run_artifact_id"],
                    run_row["run_object_hash"],
                    run_row["run_json"],
                    run_row["created_at"],
                ),
            )
            for candidate in candidates:
                connection.execute(
                    "INSERT INTO knowledge_visual_skill_candidate("
                    "candidate_id,run_id,author_source_id,semantic_run_id,argument_unit_id,"
                    "final_skill_id,skill_name,primary_module,secondary_modules_json,"
                    "decision_question,core_principle,confidence,source_hashes_json,"
                    "skill_artifact_id,skill_object_hash,skill_json,audit_artifact_id,"
                    "audit_object_hash,audit_json,audit_status,"
                    "formal_committee_weight_allowed,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PASS',0,?)",
                    (
                        candidate["candidate_id"],
                        candidate["run_id"],
                        candidate["author_source_id"],
                        candidate["semantic_run_id"],
                        candidate["argument_unit_id"],
                        candidate["final_skill_id"],
                        candidate["skill_name"],
                        candidate["primary_module"],
                        candidate["secondary_modules_json"],
                        candidate["decision_question"],
                        candidate["core_principle"],
                        candidate["confidence"],
                        candidate["source_hashes_json"],
                        candidate["skill_artifact_id"],
                        candidate["skill_object_hash"],
                        candidate["skill_json"],
                        candidate["audit_artifact_id"],
                        candidate["audit_object_hash"],
                        candidate["audit_json"],
                        candidate["created_at"],
                    ),
                )
            for record in no_skills:
                connection.execute(
                    "INSERT INTO knowledge_visual_skill_no_skill("
                    "run_id,argument_unit_id,author_source_id,reason_codes_json,"
                    "record_object_hash,record_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        record["run_id"],
                        record["argument_unit_id"],
                        record["author_source_id"],
                        record["reason_codes_json"],
                        record["record_object_hash"],
                        record["record_json"],
                        record["created_at"],
                    ),
                )
        return False

    def put_review_decision(
        self,
        *,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> bool:
        with self.state.transaction() as connection:
            candidate = connection.execute(
                "SELECT run_id,final_skill_id,skill_object_hash,audit_status "
                "FROM knowledge_visual_skill_candidate WHERE candidate_id=?",
                (row["candidate_id"],),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"unknown visual Skill candidate: {row['candidate_id']}")
            if str(candidate["run_id"]) != str(row["run_id"]):
                raise ValueError("visual Skill review run mismatch")
            if str(candidate["audit_status"]) != "PASS":
                raise ValueError("visual Skill review requires PASS audit")
            if str(candidate["final_skill_id"]) != str(row["final_skill_id"]):
                raise ValueError("visual Skill review final_skill_id drift")
            if str(candidate["skill_object_hash"]) != str(row["skill_object_hash"]):
                raise ValueError("visual Skill review object hash drift")

            existing = connection.execute(
                "SELECT decision_id,decision_object_hash,decision_json "
                "FROM knowledge_visual_skill_review_decision WHERE candidate_id=?",
                (row["candidate_id"],),
            ).fetchone()
            expected = (
                row["decision_id"],
                row["decision_object_hash"],
                row["decision_json"],
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError(f"visual Skill review collision: {row['candidate_id']}")
                return True

            self._register_artifact(
                connection,
                artifact_id=str(artifact["artifact_id"]),
                artifact_type=str(artifact["artifact_type"]),
                schema_version=str(artifact["schema_version"]),
                object_hash=str(artifact["object_hash"]),
                input_hashes=list(artifact["input_hashes"]),
            )
            connection.execute(
                "INSERT INTO knowledge_visual_skill_review_decision("
                "decision_id,run_id,candidate_id,final_skill_id,skill_object_hash,decision,"
                "actor,reason,decision_artifact_id,decision_object_hash,decision_json,"
                "formal_committee_weight_allowed,decided_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    row["decision_id"],
                    row["run_id"],
                    row["candidate_id"],
                    row["final_skill_id"],
                    row["skill_object_hash"],
                    row["decision"],
                    row["actor"],
                    row["reason"],
                    row["decision_artifact_id"],
                    row["decision_object_hash"],
                    row["decision_json"],
                    row["decided_at"],
                    row["created_at"],
                ),
            )
        return False

    def put_release(
        self,
        *,
        release_row: Mapping[str, Any],
        members: Sequence[Mapping[str, Any]],
        release_artifact: Mapping[str, Any],
    ) -> bool:
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT release_id,release_object_hash,release_json "
                "FROM knowledge_visual_skill_release WHERE generation_run_id=?",
                (release_row["generation_run_id"],),
            ).fetchone()
            expected = (
                release_row["release_id"],
                release_row["release_object_hash"],
                release_row["release_json"],
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError(
                        f"visual Skill release collision: {release_row['generation_run_id']}"
                    )
                return True

            self._register_artifact(
                connection,
                artifact_id=str(release_artifact["artifact_id"]),
                artifact_type=str(release_artifact["artifact_type"]),
                schema_version=str(release_artifact["schema_version"]),
                object_hash=str(release_artifact["object_hash"]),
                input_hashes=list(release_artifact["input_hashes"]),
            )
            connection.execute(
                "INSERT INTO knowledge_visual_skill_release("
                "release_id,registry_version,base_run_id,generation_run_id,"
                "base_registry_release_id,base_registry_object_hash,base_admitted_skill_count,"
                "overlay_candidate_count,overlay_approved_count,overlay_rejected_count,"
                "overlay_admitted_skill_count,composite_admitted_skill_count,decision_ids_json,"
                "member_ids_json,release_artifact_id,release_object_hash,release_json,"
                "formal_committee_weight_allowed,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (
                    release_row["release_id"],
                    release_row["registry_version"],
                    release_row["base_run_id"],
                    release_row["generation_run_id"],
                    release_row["base_registry_release_id"],
                    release_row["base_registry_object_hash"],
                    release_row["base_admitted_skill_count"],
                    release_row["overlay_candidate_count"],
                    release_row["overlay_approved_count"],
                    release_row["overlay_rejected_count"],
                    release_row["overlay_admitted_skill_count"],
                    release_row["composite_admitted_skill_count"],
                    release_row["decision_ids_json"],
                    release_row["member_ids_json"],
                    release_row["release_artifact_id"],
                    release_row["release_object_hash"],
                    release_row["release_json"],
                    release_row["created_at"],
                ),
            )
            for member in members:
                connection.execute(
                    "INSERT INTO knowledge_visual_skill_member("
                    "release_id,member_ordinal,candidate_id,final_skill_id,skill_object_hash,"
                    "skill_artifact_id,admission_basis,source_hashes_json) "
                    "VALUES(?,?,?,?,?,?,'APPROVED',?)",
                    (
                        member["release_id"],
                        member["member_ordinal"],
                        member["candidate_id"],
                        member["final_skill_id"],
                        member["skill_object_hash"],
                        member["skill_artifact_id"],
                        member["source_hashes_json"],
                    ),
                )
        return False

    def overlay_skill_rows(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT member.member_ordinal,candidate.final_skill_id,"
                "candidate.skill_object_hash,candidate.skill_artifact_id,"
                "member.admission_basis,member.source_hashes_json,"
                "'READY_FOR_SHADOW' AS status,candidate.skill_name,candidate.primary_module,"
                "candidate.decision_question,candidate.core_principle,"
                "candidate.secondary_modules_json,candidate.skill_json,"
                "'VISUAL_OVERLAY' AS skill_origin "
                "FROM knowledge_visual_skill_member member "
                "JOIN knowledge_visual_skill_candidate candidate "
                "ON candidate.candidate_id=member.candidate_id WHERE member.release_id=? "
                "ORDER BY member.member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["VisualSkillRepository"]
