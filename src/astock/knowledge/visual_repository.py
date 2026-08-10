"""Read-side repository helpers for real Zhihu visual completion."""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

from astock.core.state import StateStore


class ZhihuVisualRepository:
    """Keep SQLite knowledge lookups on the knowledge side of the architecture."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def select_semantic_run(
        self,
        author_source_id: str,
        *,
        pipeline_version: str,
    ) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT r.run_id,r.author_source_id,r.pipeline_version,r.stage,r.run_json,"
                "r.started_at,COUNT(item.item_id) AS item_count,"
                "SUM(CASE WHEN item.content_type IN ('answers','articles','thoughts') "
                "THEN 1 ELSE 0 END) AS zhihu_item_count "
                "FROM knowledge_semantic_run r LEFT JOIN knowledge_semantic_content_item item "
                "ON item.run_id=r.run_id WHERE r.author_source_id=? AND r.pipeline_version=? "
                "GROUP BY r.run_id",
                (author_source_id, pipeline_version),
            ).fetchall()
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for row in rows:
            payload = json.loads(str(row["run_json"]))
            stage = str(row["stage"])
            item_count = int(row["item_count"] or 0)
            zhihu_item_count = int(row["zhihu_item_count"] or 0)
            if stage in {"PLANNED", "INPUT_FROZEN", "PARAGRAPHIZED", "KEYWORD_SCREENED", "FAILED"}:
                continue
            if item_count == 0 or zhihu_item_count != item_count:
                continue
            candidates.append(
                (
                    zhihu_item_count,
                    str(row["started_at"]),
                    {
                        "run_id": str(row["run_id"]),
                        "author_source_id": str(row["author_source_id"]),
                        "pipeline_version": str(row["pipeline_version"]),
                        "stage": stage,
                        "started_at": str(row["started_at"]),
                        "run": payload,
                    },
                )
            )
        if not candidates:
            raise ValueError(
                f"no materialized semantic run for {author_source_id} / {pipeline_version}"
            )
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    def semantic_items(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT item.item_id,item.author_source_id,item.content_type,item.content_id,"
                "item.content_version_id,item.source_snapshot_id,item.source_object_hash,"
                "item.normalized_object_hash,item.item_json,"
                "snapshot.object_hash AS source_snapshot_object_hash "
                "FROM knowledge_semantic_content_item item "
                "JOIN source_snapshot_index snapshot "
                "ON snapshot.snapshot_id=item.source_snapshot_id WHERE item.run_id=? "
                "ORDER BY item.content_type,item.content_id,item.item_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def paragraphs_by_item(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT paragraph_id,run_id,item_id,author_source_id,content_id,ordinal,"
                "text_object_hash,primary_role,standalone_distillable,merge_action,unit_json "
                "FROM knowledge_paragraph_unit WHERE run_id=? ORDER BY item_id,ordinal",
                (run_id,),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["item_id"]), []).append(dict(row))
        return grouped

    def argument_bindings_by_paragraph(
        self,
        run_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT ref.paragraph_id,a.argument_unit_id,a.text_object_hash,a.unit_json,"
                "a.status,a.start_ordinal,a.end_ordinal FROM knowledge_argument_unit a "
                "JOIN knowledge_argument_unit_paragraph_ref ref "
                "ON ref.argument_unit_id=a.argument_unit_id WHERE a.run_id=? "
                "ORDER BY ref.paragraph_id,a.argument_unit_id",
                (run_id,),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["paragraph_id"]), []).append(dict(row))
        return grouped

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT snapshot_id,source_id,object_hash,fetch_status "
                "FROM source_snapshot_index WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown source snapshot: {snapshot_id}")
        return dict(row)

    def paragraphs(self, item_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT paragraph_id,run_id,item_id,author_source_id,content_id,ordinal,"
                "text_object_hash,primary_role,standalone_distillable,merge_action,unit_json "
                "FROM knowledge_paragraph_unit WHERE item_id=? ORDER BY ordinal",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def argument_bindings(self, paragraph_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT a.argument_unit_id,a.text_object_hash,a.unit_json,a.status,"
                "a.start_ordinal,a.end_ordinal FROM knowledge_argument_unit a "
                "JOIN knowledge_argument_unit_paragraph_ref r "
                "ON r.argument_unit_id=a.argument_unit_id WHERE r.paragraph_id=? "
                "ORDER BY a.argument_unit_id",
                (paragraph_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def packet_for_placement(self, placement_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT packet.packet_id,packet.packet_status,packet.reason_code,"
                "packet.packet_artifact_id,packet.packet_object_hash,"
                "placement.placement_id,placement.asset_id,asset.image_object_hash,"
                "ocr.attempt_status AS ocr_status,class.visual_type "
                "FROM knowledge_zhihu_visual_placement placement "
                "JOIN knowledge_zhihu_visual_asset asset ON asset.asset_id=placement.asset_id "
                "JOIN knowledge_zhihu_visual_packet packet "
                "ON packet.placement_id=placement.placement_id "
                "JOIN knowledge_zhihu_visual_ocr_attempt ocr "
                "ON ocr.placement_id=placement.placement_id "
                "JOIN knowledge_zhihu_visual_classification class "
                "ON class.placement_id=placement.placement_id "
                "WHERE placement.placement_id=?",
                (placement_id,),
            ).fetchone()
        return dict(row) if row is not None else None


__all__ = ["ZhihuVisualRepository"]
