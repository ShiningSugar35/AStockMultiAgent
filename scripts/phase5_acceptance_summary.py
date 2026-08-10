from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
from astock.knowledge.visual_skill_service import VisualSkillService
from astock.settings import ProjectPaths

BASE_RUN_ID = "direct-source-v4:1c42336cff1a00ac1b18ee85e82f1ba63714b0a4f3c6685451e9abb8ee849eef"


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute(query).fetchall()]


def main() -> None:
    paths = ProjectPaths.discover()
    uri = Path(paths.state_db).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        visual_status = dict(
            connection.execute(
                "SELECT COUNT(*) AS placement_count,COUNT(DISTINCT p.asset_id) AS asset_count,"
                "SUM(CASE WHEN packet.packet_status='READY' THEN 1 ELSE 0 END) AS ready_count,"
                "SUM(CASE WHEN packet.packet_status='NEEDS_REVIEW' THEN 1 ELSE 0 END) "
                "AS review_count FROM knowledge_zhihu_visual_placement p "
                "JOIN knowledge_zhihu_visual_packet packet ON packet.placement_id=p.placement_id"
            ).fetchone()
        )
        authors = _rows(
            connection,
            "SELECT p.author_source_id,COUNT(*) AS placement_count,"
            "COUNT(DISTINCT p.asset_id) AS asset_count,"
            "SUM(CASE WHEN packet.packet_status='READY' THEN 1 ELSE 0 END) AS ready_count "
            "FROM knowledge_zhihu_visual_placement p "
            "JOIN knowledge_zhihu_visual_packet packet ON packet.placement_id=p.placement_id "
            "GROUP BY p.author_source_id ORDER BY p.author_source_id",
        )
        generation = dict(
            connection.execute(
                "SELECT run_id,generation_policy_version,evaluated_argument_count,"
                "candidate_count,no_skill_count,run_artifact_id,run_object_hash "
                "FROM knowledge_visual_skill_run ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        )
        skill_by_author = _rows(
            connection,
            "SELECT author_source_id,COUNT(*) AS candidate_count "
            "FROM knowledge_visual_skill_candidate WHERE run_id=("
            "SELECT run_id FROM knowledge_visual_skill_run ORDER BY rowid DESC LIMIT 1) "
            "GROUP BY author_source_id ORDER BY author_source_id",
        )
        no_skill_by_author = _rows(
            connection,
            "SELECT author_source_id,COUNT(*) AS no_skill_count "
            "FROM knowledge_visual_skill_no_skill WHERE run_id=("
            "SELECT run_id FROM knowledge_visual_skill_run ORDER BY rowid DESC LIMIT 1) "
            "GROUP BY author_source_id ORDER BY author_source_id",
        )
        release = dict(
            connection.execute(
                "SELECT release_id,registry_version,base_admitted_skill_count,"
                "overlay_candidate_count,overlay_approved_count,overlay_rejected_count,"
                "overlay_admitted_skill_count,composite_admitted_skill_count,"
                "release_artifact_id,release_object_hash "
                "FROM knowledge_visual_skill_release ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        )
        review = dict(
            connection.execute(
                "SELECT SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approved,"
                "SUM(CASE WHEN decision='REJECT' THEN 1 ELSE 0 END) AS rejected,"
                "COUNT(*) AS total FROM knowledge_visual_skill_review_decision WHERE run_id=("
                "SELECT run_id FROM knowledge_visual_skill_run ORDER BY rowid DESC LIMIT 1)"
            ).fetchone()
        )
        foreign_key_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity = [str(row[0]) for row in integrity_rows]
    finally:
        connection.close()

    state = StateStore(paths.state_db, paths.root / "migrations")
    objects = ObjectStore(paths.objects)
    provider = RepositoryKnowledgeSkillProvider(
        KnowledgeCompletionRepository(state),
        objects,
    ).status(BASE_RUN_ID)
    visual_audit = VisualSkillService(state, objects).audit(BASE_RUN_ID)
    payload = {
        "base_run_id": BASE_RUN_ID,
        "visual_status": visual_status,
        "authors": authors,
        "generation": generation,
        "skill_by_author": skill_by_author,
        "no_skill_by_author": no_skill_by_author,
        "review": review,
        "release": release,
        "provider": provider.model_dump(mode="json"),
        "visual_skill_audit": visual_audit,
        "sqlite": {
            "foreign_key_check_count": foreign_key_count,
            "integrity_check": integrity,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
