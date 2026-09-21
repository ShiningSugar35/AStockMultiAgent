"""Append-only repository for reviewed semantic-candidate Skill overlays."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore, utc_now_text


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class SemanticSkillRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _artifact(
        connection: Any,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: Sequence[str],
    ) -> None:
        inputs = _json(sorted(set(input_hashes)))
        existing = connection.execute(
            "SELECT type,schema_version,object_hash,input_hashes_json "
            "FROM artifact_registry WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        expected = (artifact_type, schema_version, object_hash, inputs)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"semantic Skill artifact collision: {artifact_id}")
            return
        connection.execute(
            "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
            "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
            (artifact_id, *expected, utc_now_text()),
        )

    def raw_candidates(self, llm_batch_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_candidate WHERE llm_batch_id=? "
                "ORDER BY candidate_id",
                (llm_batch_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                refs = connection.execute(
                    "SELECT ordinal,argument_unit_id FROM knowledge_semantic_candidate_au_ref "
                    "WHERE candidate_id=? ORDER BY ordinal",
                    (item["candidate_id"],),
                ).fetchall()
                item["argument_unit_ids"] = [str(ref["argument_unit_id"]) for ref in refs]
                result.append(item)
        return result

    def argument_rows(self, argument_unit_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not argument_unit_ids:
            return {}
        placeholders = ",".join("?" for _ in argument_unit_ids)
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                f"SELECT argument_unit_id,unit_json,text_object_hash FROM knowledge_argument_unit "
                f"WHERE argument_unit_id IN ({placeholders})",
                tuple(argument_unit_ids),
            ).fetchall()
        return {str(row["argument_unit_id"]): dict(row) for row in rows}

    def paragraph_rows(self, paragraph_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not paragraph_ids:
            return {}
        placeholders = ",".join("?" for _ in paragraph_ids)
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                f"SELECT paragraph_id,unit_json,text_object_hash FROM knowledge_paragraph_unit "
                f"WHERE paragraph_id IN ({placeholders})",
                tuple(paragraph_ids),
            ).fetchall()
        return {str(row["paragraph_id"]): dict(row) for row in rows}

    def generation_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_run WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_generation(self, base_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_run WHERE base_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (base_run_id,),
            ).fetchone()
        return dict(row) if row else None

    def groups(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_group WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def group_candidates(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_group_candidate WHERE run_id=? "
                "ORDER BY group_id,ordinal",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def decisions(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_review_decision WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_release(self, base_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_release WHERE base_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (base_run_id,),
            ).fetchone()
        return dict(row) if row else None

    def release(self, release_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_release WHERE release_id=?",
                (release_id,),
            ).fetchone()
        return dict(row) if row else None

    def release_members(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_skill_member WHERE release_id=? "
                "ORDER BY member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def overlay_skill_rows(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT m.member_ordinal,m.final_skill_id,g.skill_name,g.primary_module,"
                "g.secondary_modules_json,g.decision_question,g.core_principle,"
                "m.source_hashes_json,m.skill_artifact_id,m.skill_object_hash,g.skill_json,"
                "'SEMANTIC_REVIEW_APPROVED' AS admission_basis,'READY_FOR_SHADOW' AS status "
                "FROM knowledge_semantic_skill_member m "
                "JOIN knowledge_semantic_skill_group g ON g.group_id=m.group_id "
                "WHERE m.release_id=? ORDER BY m.member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def put_generation(
        self,
        *,
        run_row: Mapping[str, Any],
        group_rows: Sequence[Mapping[str, Any]],
        mappings: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.state.transaction() as connection:
            for artifact in artifacts:
                self._artifact(
                    connection,
                    artifact_id=str(artifact["artifact_id"]),
                    artifact_type=str(artifact["artifact_type"]),
                    schema_version=str(artifact["schema_version"]),
                    object_hash=str(artifact["object_hash"]),
                    input_hashes=[str(item) for item in artifact["input_hashes"]],
                )
            existing = connection.execute(
                "SELECT run_object_hash FROM knowledge_semantic_skill_run WHERE run_id=?",
                (run_row["run_id"],),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO knowledge_semantic_skill_run("
                    "run_id,base_run_id,parent_registry_release_id,parent_registry_object_hash,"
                    "semantic_run_id,llm_batch_id,generation_policy_version,raw_candidate_count,"
                    "effective_skill_count,author_source_ids_json,run_artifact_id,run_object_hash,"
                    "run_json,formal_committee_weight_allowed,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(run_row[key] for key in (
                        "run_id","base_run_id","parent_registry_release_id",
                        "parent_registry_object_hash","semantic_run_id","llm_batch_id",
                        "generation_policy_version","raw_candidate_count","effective_skill_count",
                        "author_source_ids_json","run_artifact_id","run_object_hash","run_json",
                        "formal_committee_weight_allowed","created_at",
                    )),
                )
            elif str(existing["run_object_hash"]) != str(run_row["run_object_hash"]):
                raise ValueError("semantic Skill generation run collision")
            for row in group_rows:
                existing_group = connection.execute(
                    "SELECT skill_object_hash FROM knowledge_semantic_skill_group WHERE group_id=?",
                    (row["group_id"],),
                ).fetchone()
                if existing_group is None:
                    columns = (
                        "group_id","run_id","final_skill_id","skill_name","primary_module",
                        "secondary_modules_json","decision_question","core_principle",
                        "method_categories_json","applicable_industries_json","holding_horizon_json",
                        "candidate_ids_json","argument_unit_ids_json","author_source_ids_json",
                        "source_hashes_json","confidence","skill_artifact_id","skill_object_hash",
                        "skill_json","generation_audit_artifact_id","generation_audit_object_hash",
                        "generation_audit_json","generation_audit_status",
                        "formal_committee_weight_allowed","created_at",
                    )
                    connection.execute(
                        f"INSERT INTO knowledge_semantic_skill_group({','.join(columns)}) "
                        f"VALUES({','.join('?' for _ in columns)})",
                        tuple(row[key] for key in columns),
                    )
                elif str(existing_group["skill_object_hash"]) != str(row["skill_object_hash"]):
                    raise ValueError("semantic Skill group collision")
            for row in mappings:
                connection.execute(
                    "INSERT OR IGNORE INTO knowledge_semantic_skill_group_candidate("
                    "run_id,group_id,candidate_id,ordinal) VALUES(?,?,?,?)",
                    (row["run_id"],row["group_id"],row["candidate_id"],row["ordinal"]),
                )

    def put_decision(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> None:
        with self.state.transaction() as connection:
            self._artifact(
                connection,
                artifact_id=str(artifact["artifact_id"]),
                artifact_type=str(artifact["artifact_type"]),
                schema_version=str(artifact["schema_version"]),
                object_hash=str(artifact["object_hash"]),
                input_hashes=[str(item) for item in artifact["input_hashes"]],
            )
            existing = connection.execute(
                "SELECT decision_object_hash FROM knowledge_semantic_skill_review_decision "
                "WHERE group_id=?",
                (row["group_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["decision_object_hash"]) != str(row["decision_object_hash"]):
                    raise ValueError("semantic Skill review decision collision")
                return
            columns = (
                "decision_id","run_id","group_id","final_skill_id","skill_object_hash",
                "decision","actor","reason","decision_artifact_id","decision_object_hash",
                "decision_json","formal_committee_weight_allowed","decided_at","created_at",
            )
            connection.execute(
                f"INSERT INTO knowledge_semantic_skill_review_decision({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(row[key] for key in columns),
            )

    def put_release(
        self,
        *,
        release_row: Mapping[str, Any],
        members: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.state.transaction() as connection:
            for artifact in artifacts:
                self._artifact(
                    connection,
                    artifact_id=str(artifact["artifact_id"]),
                    artifact_type=str(artifact["artifact_type"]),
                    schema_version=str(artifact["schema_version"]),
                    object_hash=str(artifact["object_hash"]),
                    input_hashes=[str(item) for item in artifact["input_hashes"]],
                )
            existing = connection.execute(
                "SELECT release_object_hash FROM knowledge_semantic_skill_release "
                "WHERE release_id=?",
                (release_row["release_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["release_object_hash"]) != str(release_row["release_object_hash"]):
                    raise ValueError("semantic Skill release collision")
                return
            columns = (
                "release_id","registry_version","base_run_id","generation_run_id",
                "parent_registry_release_id","parent_registry_object_hash",
                "parent_admitted_skill_count","raw_candidate_count","effective_skill_count",
                "overlay_approved_count","overlay_rejected_count","overlay_admitted_skill_count",
                "composite_admitted_skill_count","decision_ids_json","member_ids_json",
                "audit_report_artifact_id","audit_report_object_hash","release_artifact_id",
                "release_object_hash","release_json","formal_committee_weight_allowed","created_at",
            )
            connection.execute(
                f"INSERT INTO knowledge_semantic_skill_release({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(release_row[key] for key in columns),
            )
            for row in members:
                connection.execute(
                    "INSERT INTO knowledge_semantic_skill_member("
                    "release_id,member_ordinal,group_id,final_skill_id,skill_object_hash,"
                    "skill_artifact_id,admission_basis,source_hashes_json)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        row["release_id"],row["member_ordinal"],row["group_id"],
                        row["final_skill_id"],row["skill_object_hash"],row["skill_artifact_id"],
                        row["admission_basis"],row["source_hashes_json"],
                    ),
                )


__all__ = ["SemanticSkillRepository"]
