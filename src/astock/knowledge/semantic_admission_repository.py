"""Append-only persistence for the owner-reviewed semantic admission overlay."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore, utc_now_text


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


class SemanticAdmissionRepository:
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
        inputs = _json(sorted(set(input_hashes)))
        existing = connection.execute(
            "SELECT type,schema_version,object_hash,input_hashes_json "
            "FROM artifact_registry WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        expected = (artifact_type, schema_version, object_hash, inputs)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"semantic admission artifact collision: {artifact_id}")
            return
        connection.execute(
            "INSERT INTO artifact_registry(artifact_id,type,schema_version,object_hash,"
            "input_hashes_json,created_at) VALUES(?,?,?,?,?,?)",
            (artifact_id, *expected, utc_now_text()),
        )

    def run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_run(self, base_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_run WHERE base_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (base_run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def skills(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_skill WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mappings(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_group_candidate WHERE run_id=? "
                "ORDER BY final_skill_id,ordinal",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def decisions(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_decision WHERE run_id=? "
                "ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_release(self, base_run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_release WHERE base_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (base_run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def release(self, release_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_release WHERE release_id=?",
                (release_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def members(self, release_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_semantic_admission_member WHERE release_id=? "
                "ORDER BY member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def overlay_skill_rows(self, release_id: str) -> list[dict[str, object]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT m.member_ordinal,m.final_skill_id,m.skill_object_hash,"
                "m.skill_artifact_id,m.admission_basis,m.source_hashes_json,"
                "s.skill_name,s.primary_module,s.secondary_modules_json,"
                "s.decision_question,s.core_principle,s.skill_json,"
                "'READY_FOR_SHADOW' AS status "
                "FROM knowledge_semantic_admission_member m "
                "JOIN knowledge_semantic_admission_skill s "
                "ON s.final_skill_id=m.final_skill_id "
                "WHERE m.release_id=? ORDER BY m.member_ordinal",
                (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def candidate_ids(self, values: Sequence[str]) -> set[str]:
        if not values:
            return set()
        result: set[str] = set()
        with closing(self.state.connect()) as connection:
            for offset in range(0, len(values), 500):
                batch = list(values[offset : offset + 500])
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT candidate_id FROM knowledge_semantic_candidate "
                    f"WHERE candidate_id IN ({placeholders})",
                    tuple(batch),
                ).fetchall()
                result.update(str(row["candidate_id"]) for row in rows)
        return result

    def put_generation(
        self,
        *,
        run_row: Mapping[str, Any],
        skill_rows: Sequence[Mapping[str, Any]],
        mapping_rows: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.state.transaction() as connection:
            for artifact in artifacts:
                self._register_artifact(
                    connection,
                    artifact_id=str(artifact["artifact_id"]),
                    artifact_type=str(artifact["artifact_type"]),
                    schema_version=str(artifact["schema_version"]),
                    object_hash=str(artifact["object_hash"]),
                    input_hashes=[str(value) for value in artifact["input_hashes"]],
                )
            existing_run = connection.execute(
                "SELECT run_object_hash FROM knowledge_semantic_admission_run WHERE run_id=?",
                (run_row["run_id"],),
            ).fetchone()
            if existing_run is None:
                columns = (
                    "run_id",
                    "base_run_id",
                    "parent_audited_release_id",
                    "parent_audited_object_hash",
                    "owner_policy_artifact_hash",
                    "admitted_groups_artifact_hash",
                    "raw_candidate_count",
                    "exact_group_count",
                    "admitted_group_count",
                    "rejected_group_count",
                    "author_source_ids_json",
                    "run_artifact_id",
                    "run_object_hash",
                    "run_json",
                    "formal_committee_weight_allowed",
                    "created_at",
                )
                connection.execute(
                    f"INSERT INTO knowledge_semantic_admission_run({','.join(columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})",
                    tuple(run_row[column] for column in columns),
                )
            elif str(existing_run["run_object_hash"]) != str(run_row["run_object_hash"]):
                raise ValueError("semantic admission run collision")

            columns = (
                "final_skill_id",
                "run_id",
                "group_id",
                "skill_name",
                "primary_module",
                "secondary_modules_json",
                "decision_question",
                "core_principle",
                "family",
                "industry_scope",
                "holding_horizon",
                "method_categories_json",
                "candidate_ids_json",
                "argument_unit_ids_json",
                "source_snapshot_ids_json",
                "source_hashes_json",
                "disposition",
                "skill_artifact_id",
                "skill_object_hash",
                "skill_json",
                "formal_committee_weight_allowed",
                "created_at",
            )
            for row in skill_rows:
                existing_skill = connection.execute(
                    "SELECT skill_object_hash FROM knowledge_semantic_admission_skill "
                    "WHERE final_skill_id=?",
                    (row["final_skill_id"],),
                ).fetchone()
                if existing_skill is None:
                    connection.execute(
                        f"INSERT INTO knowledge_semantic_admission_skill({','.join(columns)}) "
                        f"VALUES({','.join('?' for _ in columns)})",
                        tuple(row[column] for column in columns),
                    )
                elif str(existing_skill["skill_object_hash"]) != str(row["skill_object_hash"]):
                    raise ValueError("semantic admission Skill collision")

            for row in mapping_rows:
                existing_mapping = connection.execute(
                    "SELECT final_skill_id FROM knowledge_semantic_admission_group_candidate "
                    "WHERE run_id=? AND candidate_id=?",
                    (row["run_id"], row["candidate_id"]),
                ).fetchone()
                if existing_mapping is None:
                    connection.execute(
                        "INSERT INTO knowledge_semantic_admission_group_candidate("
                        "run_id,final_skill_id,candidate_id,ordinal) VALUES(?,?,?,?)",
                        (
                            row["run_id"],
                            row["final_skill_id"],
                            row["candidate_id"],
                            row["ordinal"],
                        ),
                    )
                elif str(existing_mapping["final_skill_id"]) != str(row["final_skill_id"]):
                    raise ValueError("semantic admission candidate mapped to another Skill")

    def put_decision(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> None:
        with self.state.transaction() as connection:
            self._register_artifact(
                connection,
                artifact_id=str(artifact["artifact_id"]),
                artifact_type=str(artifact["artifact_type"]),
                schema_version=str(artifact["schema_version"]),
                object_hash=str(artifact["object_hash"]),
                input_hashes=[str(value) for value in artifact["input_hashes"]],
            )
            existing = connection.execute(
                "SELECT decision_object_hash FROM knowledge_semantic_admission_decision "
                "WHERE final_skill_id=?",
                (row["final_skill_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["decision_object_hash"]) != str(row["decision_object_hash"]):
                    raise ValueError("semantic admission decision collision")
                return
            columns = (
                "decision_id",
                "run_id",
                "final_skill_id",
                "skill_object_hash",
                "decision",
                "actor",
                "reason",
                "decision_artifact_id",
                "decision_object_hash",
                "decision_json",
                "formal_committee_weight_allowed",
                "decided_at",
                "created_at",
            )
            connection.execute(
                f"INSERT INTO knowledge_semantic_admission_decision({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
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
                self._register_artifact(
                    connection,
                    artifact_id=str(artifact["artifact_id"]),
                    artifact_type=str(artifact["artifact_type"]),
                    schema_version=str(artifact["schema_version"]),
                    object_hash=str(artifact["object_hash"]),
                    input_hashes=[str(value) for value in artifact["input_hashes"]],
                )
            existing = connection.execute(
                "SELECT release_object_hash FROM knowledge_semantic_admission_release "
                "WHERE release_id=?",
                (release_row["release_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["release_object_hash"]) != str(release_row["release_object_hash"]):
                    raise ValueError("semantic admission release collision")
                return
            columns = (
                "release_id",
                "registry_version",
                "base_run_id",
                "admission_run_id",
                "parent_audited_release_id",
                "parent_audited_object_hash",
                "parent_active_skill_count",
                "raw_candidate_count",
                "exact_group_count",
                "approved_skill_count",
                "rejected_skill_count",
                "active_skill_count",
                "decision_ids_json",
                "member_ids_json",
                "audit_report_artifact_id",
                "audit_report_object_hash",
                "release_artifact_id",
                "release_object_hash",
                "release_json",
                "formal_committee_weight_allowed",
                "created_at",
            )
            connection.execute(
                f"INSERT INTO knowledge_semantic_admission_release({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(release_row[column] for column in columns),
            )
            for member in members:
                connection.execute(
                    "INSERT INTO knowledge_semantic_admission_member("
                    "release_id,member_ordinal,final_skill_id,skill_object_hash,"
                    "skill_artifact_id,admission_basis,source_hashes_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        member["release_id"],
                        member["member_ordinal"],
                        member["final_skill_id"],
                        member["skill_object_hash"],
                        member["skill_artifact_id"],
                        member["admission_basis"],
                        member["source_hashes_json"],
                    ),
                )


__all__ = ["SemanticAdmissionRepository"]
