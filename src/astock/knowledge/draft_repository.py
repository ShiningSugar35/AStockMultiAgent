"""SQLite metadata repository for private viewpoint and Skill drafts."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas import (
    AuthorDraftGenerationReport,
    PrivateSkillCandidateDraft,
    PrivateViewpointDraft,
)


class KnowledgeDraftRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def register_viewpoint_drafts(self, drafts: list[PrivateViewpointDraft]) -> None:
        with self.state.transaction() as connection:
            for draft in drafts:
                draft_json = canonical_json_bytes(draft.model_dump(mode="json")).decode(
                    "utf-8"
                )
                row = connection.execute(
                    "SELECT draft_json,payload_object_hash FROM private_viewpoint_draft "
                    "WHERE draft_id=?",
                    (draft.draft_id,),
                ).fetchone()
                if row is not None:
                    if (
                        str(row["draft_json"]) != draft_json
                        or str(row["payload_object_hash"])
                        != draft.payload_object_sha256
                    ):
                        raise ValueError(f"private viewpoint draft collision: {draft.draft_id}")
                    continue
                connection.execute(
                    "INSERT INTO private_viewpoint_draft("
                    "draft_id,run_id,author_source_id,method_category,source_unit_id,"
                    "source_excerpt_hash,payload_object_hash,proposition_derivation,"
                    "generation_rule_version,human_review_status,draft_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        draft.draft_id,
                        draft.run_id,
                        draft.author_source_id,
                        draft.method_category.value,
                        draft.source_unit_ids[0],
                        draft.source_excerpt_hashes[0],
                        draft.payload_object_sha256,
                        draft.proposition_derivation.value,
                        draft.generation_rule_version,
                        draft.human_review_status.value,
                        draft_json,
                        draft.created_at.isoformat(),
                    ),
                )

    def viewpoint_drafts_for_run(
        self,
        run_id: str,
        generation_rule_version: str,
    ) -> list[PrivateViewpointDraft]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT draft_json FROM private_viewpoint_draft "
                "WHERE run_id=? AND generation_rule_version=? "
                "ORDER BY method_category,draft_id",
                (run_id, generation_rule_version),
            ).fetchall()
        return [PrivateViewpointDraft.model_validate_json(row["draft_json"]) for row in rows]

    def register_skill_candidates(
        self,
        candidates: list[PrivateSkillCandidateDraft],
    ) -> None:
        with self.state.transaction() as connection:
            for candidate in candidates:
                candidate_json = canonical_json_bytes(
                    candidate.model_dump(mode="json")
                ).decode("utf-8")
                row = connection.execute(
                    "SELECT candidate_json,payload_object_hash "
                    "FROM private_skill_candidate_draft WHERE candidate_id=?",
                    (candidate.candidate_id,),
                ).fetchone()
                if row is not None:
                    if (
                        str(row["candidate_json"]) != candidate_json
                        or str(row["payload_object_hash"])
                        != candidate.payload_object_sha256
                    ):
                        raise ValueError(
                            f"private Skill candidate collision: {candidate.candidate_id}"
                        )
                    self._validate_candidate_refs(connection, candidate)
                    continue
                connection.execute(
                    "INSERT INTO private_skill_candidate_draft("
                    "candidate_id,run_id,author_source_id,target_skill,method_category,"
                    "payload_object_hash,generation_rule_version,evaluation_status,"
                    "approval_status,candidate_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate.candidate_id,
                        candidate.run_id,
                        candidate.author_source_id,
                        candidate.target_skill.value,
                        candidate.method_category.value,
                        candidate.payload_object_sha256,
                        candidate.generation_rule_version,
                        candidate.evaluation_status.value,
                        candidate.approval_status.value,
                        candidate_json,
                        candidate.created_at.isoformat(),
                    ),
                )
                connection.executemany(
                    "INSERT INTO private_skill_candidate_viewpoint_ref("
                    "candidate_id,ordinal,draft_id) VALUES(?,?,?)",
                    [
                        (candidate.candidate_id, ordinal, draft_id)
                        for ordinal, draft_id in enumerate(
                            candidate.source_viewpoint_draft_ids,
                            start=1,
                        )
                    ],
                )
                connection.executemany(
                    "INSERT INTO private_skill_candidate_unit_ref("
                    "candidate_id,ordinal,unit_id) VALUES(?,?,?)",
                    [
                        (candidate.candidate_id, ordinal, unit_id)
                        for ordinal, unit_id in enumerate(
                            candidate.source_unit_ids,
                            start=1,
                        )
                    ],
                )

    @staticmethod
    def _validate_candidate_refs(connection, candidate: PrivateSkillCandidateDraft) -> None:
        viewpoint_rows = connection.execute(
            "SELECT draft_id FROM private_skill_candidate_viewpoint_ref "
            "WHERE candidate_id=? ORDER BY ordinal",
            (candidate.candidate_id,),
        ).fetchall()
        unit_rows = connection.execute(
            "SELECT unit_id FROM private_skill_candidate_unit_ref "
            "WHERE candidate_id=? ORDER BY ordinal",
            (candidate.candidate_id,),
        ).fetchall()
        if [str(row[0]) for row in viewpoint_rows] != candidate.source_viewpoint_draft_ids:
            raise ValueError(f"private Skill viewpoint refs differ: {candidate.candidate_id}")
        if [str(row[0]) for row in unit_rows] != candidate.source_unit_ids:
            raise ValueError(f"private Skill unit refs differ: {candidate.candidate_id}")

    def skill_candidates_for_run(
        self,
        run_id: str,
        generation_rule_version: str,
    ) -> list[PrivateSkillCandidateDraft]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT candidate_json FROM private_skill_candidate_draft "
                "WHERE run_id=? AND generation_rule_version=? "
                "ORDER BY method_category,candidate_id",
                (run_id, generation_rule_version),
            ).fetchall()
        return [
            PrivateSkillCandidateDraft.model_validate_json(row["candidate_json"])
            for row in rows
        ]

    def candidate_refs_for_run(
        self,
        run_id: str,
        generation_rule_version: str,
    ) -> dict[str, tuple[list[str], list[str]]]:
        with self.state.connect() as connection:
            candidate_rows = connection.execute(
                "SELECT candidate_id FROM private_skill_candidate_draft "
                "WHERE run_id=? AND generation_rule_version=? "
                "ORDER BY candidate_id",
                (run_id, generation_rule_version),
            ).fetchall()
            result: dict[str, tuple[list[str], list[str]]] = {}
            for row in candidate_rows:
                candidate_id = str(row["candidate_id"])
                viewpoints = connection.execute(
                    "SELECT draft_id FROM private_skill_candidate_viewpoint_ref "
                    "WHERE candidate_id=? ORDER BY ordinal",
                    (candidate_id,),
                ).fetchall()
                units = connection.execute(
                    "SELECT unit_id FROM private_skill_candidate_unit_ref "
                    "WHERE candidate_id=? ORDER BY ordinal",
                    (candidate_id,),
                ).fetchall()
                result[candidate_id] = (
                    [str(item["draft_id"]) for item in viewpoints],
                    [str(item["unit_id"]) for item in units],
                )
        return result

    def register_report(
        self,
        report: AuthorDraftGenerationReport,
        *,
        object_hash: str,
    ) -> None:
        report_json = canonical_json_bytes(report.model_dump(mode="json")).decode("utf-8")
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT report_json,report_object_hash FROM author_draft_generation_report "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["report_json"]) != report_json
                    or str(row["report_object_hash"]) != object_hash
                ):
                    raise ValueError(f"draft generation report collision: {report.report_id}")
                return
            connection.execute(
                "INSERT INTO author_draft_generation_report("
                "report_id,run_id,author_source_id,viewpoint_draft_count,"
                "skill_candidate_count,generation_rule_version,human_review_status,"
                "report_object_hash,report_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.run_id,
                    report.author_source_id,
                    report.viewpoint_draft_count,
                    report.skill_candidate_count,
                    report.generation_rule_version,
                    report.human_review_status.value,
                    object_hash,
                    report_json,
                    report.created_at.isoformat(),
                ),
            )

    def latest_report(self, author_source_id: str) -> AuthorDraftGenerationReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM author_draft_generation_report "
                "WHERE author_source_id=? ORDER BY created_at DESC,report_id DESC LIMIT 1",
                (author_source_id,),
            ).fetchone()
        return AuthorDraftGenerationReport.model_validate_json(row["report_json"]) if row else None

    def report_for_run(
        self,
        run_id: str,
        generation_rule_version: str,
    ) -> AuthorDraftGenerationReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM author_draft_generation_report "
                "WHERE run_id=? AND generation_rule_version=?",
                (run_id, generation_rule_version),
            ).fetchone()
        return AuthorDraftGenerationReport.model_validate_json(row["report_json"]) if row else None

    def report_object_hash(self, report_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT report_object_hash FROM author_draft_generation_report "
                "WHERE report_id=?",
                (report_id,),
            ).fetchone()
        return str(row["report_object_hash"]) if row else None


__all__ = ["KnowledgeDraftRepository"]
