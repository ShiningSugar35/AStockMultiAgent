"""SQLite repository for independent direct-source Skill distillation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore, utc_now_text
from astock.schemas.direct_source_distillation import (
    DirectChapterBatchDefinition,
    DirectDedupManifest,
    DirectNormalizedBatchOutput,
    DirectRunInitManifest,
)


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _semantic_columns(payload: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        payload["skill_name"],
        payload["decision_question"],
        payload["core_principle"],
        _json(payload["applicable_conditions"]),
        _json(payload["reasoning_steps"]),
        len(payload["reasoning_steps"]),
        _json(payload["required_evidence"]),
        _json(payload["positive_signals"]),
        _json(payload["negative_signals"]),
        _json(payload["invalidation_conditions"]),
        _json(payload["failure_modes"]),
        payload["confidence"],
    )


class DirectSourceDistillationRepository:
    """Persist every durable direct-source state transition in SQLite."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_direct_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_visual_evidence(self, evidence_id: str) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='book_image_evidence'"
            ).fetchone()
            if table is None:
                raise ValueError("book_image_evidence table is unavailable")
            requested = connection.execute(
                "SELECT evidence_id,page_number,bbox_json,image_object_hash,"
                "duplicate_of_evidence_id,evidence_object_hash "
                "FROM book_image_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if requested is None:
                raise KeyError(f"unknown visual evidence id: {evidence_id}")
            current = requested
            visited: set[str] = set()
            while current["image_object_hash"] is None:
                current_id = str(current["evidence_id"])
                if current_id in visited:
                    raise ValueError(f"visual evidence duplicate cycle: {evidence_id}")
                visited.add(current_id)
                duplicate_id = current["duplicate_of_evidence_id"]
                if duplicate_id is None:
                    raise ValueError(
                        f"visual evidence has no immutable image object: {evidence_id}"
                    )
                current = connection.execute(
                    "SELECT evidence_id,page_number,bbox_json,image_object_hash,"
                    "duplicate_of_evidence_id,evidence_object_hash "
                    "FROM book_image_evidence WHERE evidence_id=?",
                    (duplicate_id,),
                ).fetchone()
                if current is None:
                    raise ValueError(
                        f"visual evidence duplicate target is missing: {evidence_id}"
                    )
        try:
            locator = json.loads(requested["bbox_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid visual evidence bbox JSON: {evidence_id}") from exc
        if not isinstance(locator, (Mapping, list)):
            raise ValueError(
                f"visual evidence bbox is not structured JSON: {evidence_id}"
            )
        return {
            "evidence_id": evidence_id,
            "object_hash": str(current["image_object_hash"]),
            "source_kind": "PDF",
            "unit_index": int(requested["page_number"]),
            "evidence_locator": (
                dict(locator) if isinstance(locator, Mapping) else locator
            ),
        }

    def initialize(
        self,
        manifest: DirectRunInitManifest,
        *,
        input_hash: str,
        manifest_object_hash: str,
        resolved_visuals: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> bool:
        payload = manifest.model_dump(mode="json")
        now = utc_now_text()
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT input_hash,manifest_object_hash FROM knowledge_direct_run WHERE run_id=?",
                (manifest.run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["input_hash"] != input_hash
                    or existing["manifest_object_hash"] != manifest_object_hash
                ):
                    raise ValueError(f"direct run collision: {manifest.run_id}")
                connection.execute(
                    "UPDATE knowledge_direct_run "
                    "SET init_replay_count=init_replay_count+1,updated_at=? WHERE run_id=?",
                    (now, manifest.run_id),
                )
                return True
            input_owner = connection.execute(
                "SELECT run_id FROM knowledge_direct_run WHERE input_hash=?",
                (input_hash,),
            ).fetchone()
            if input_owner is not None:
                raise ValueError(
                    "direct run input hash is already bound to "
                    f"{input_owner['run_id']}"
                )
            connection.execute(
                "INSERT INTO knowledge_direct_run("
                "run_id,input_hash,pipeline_version,stage,frozen_source_count,"
                "frozen_batch_count,manifest_object_hash,manifest_json,"
                "formal_committee_weight_allowed,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (
                    manifest.run_id,
                    input_hash,
                    manifest.pipeline_version,
                    "INITIALIZED",
                    len(manifest.sources),
                    len(manifest.batches),
                    manifest_object_hash,
                    _json(payload),
                    now,
                    now,
                ),
            )
            for source in manifest.sources:
                connection.execute(
                    "INSERT INTO knowledge_direct_source("
                    "run_id,source_id,source_kind,source_file_hash,created_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        manifest.run_id,
                        source.source_id,
                        source.source_kind.value,
                        source.source_file_hash,
                        now,
                    ),
                )
            for batch in manifest.batches:
                self._insert_batch(
                    connection,
                    manifest.run_id,
                    batch,
                    resolved_visuals.get(batch.batch_id, ()),
                    now,
                )
        return False

    @staticmethod
    def _insert_batch(
        connection: sqlite3.Connection,
        run_id: str,
        batch: DirectChapterBatchDefinition,
        visuals: Sequence[Mapping[str, Any]],
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO knowledge_direct_chapter_batch("
            "run_id,batch_id,source_id,chapter_unit_id,batch_ordinal,stage,"
            "created_at,updated_at"
            ") VALUES(?,?,?,?,?,'FROZEN',?,?)",
            (
                run_id,
                batch.batch_id,
                batch.source_id,
                batch.chapter_unit_id,
                batch.ordinal,
                now,
                now,
            ),
        )
        groups = (
            ("CONTEXT_BEFORE", batch.context_before),
            ("CURRENT", batch.current_fragments),
            ("CONTEXT_AFTER", batch.context_after),
        )
        for role, fragments in groups:
            for ordinal, fragment in enumerate(fragments, start=1):
                locator = fragment.locator
                connection.execute(
                    "INSERT INTO knowledge_direct_chapter_fragment("
                    "run_id,batch_id,fragment_id,context_role,fragment_ordinal,"
                    "object_hash,source_kind,unit_index,start_offset,end_offset,locator_json"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        batch.batch_id,
                        fragment.fragment_id,
                        role,
                        ordinal,
                        fragment.object_hash,
                        locator.source_kind.value,
                        locator.unit_index,
                        locator.start_offset,
                        locator.end_offset,
                        _json(locator.model_dump(mode="json")),
                    ),
                )
        for ordinal, visual in enumerate(visuals, start=1):
            connection.execute(
                "INSERT INTO knowledge_direct_chapter_visual_ref("
                "run_id,batch_id,evidence_id,visual_ordinal,object_hash,"
                "source_kind,unit_index,evidence_locator_json"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    batch.batch_id,
                    visual["evidence_id"],
                    ordinal,
                    visual["object_hash"],
                    visual["source_kind"],
                    visual["unit_index"],
                    _json(visual["evidence_locator"]),
                ),
            )

    def batch_scope(self, run_id: str, batch_id: str) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            batch = connection.execute(
                "SELECT b.*,s.source_kind,s.source_file_hash "
                "FROM knowledge_direct_chapter_batch b "
                "JOIN knowledge_direct_source s "
                "ON s.run_id=b.run_id AND s.source_id=b.source_id "
                "WHERE b.run_id=? AND b.batch_id=?",
                (run_id, batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(f"unknown direct batch: {run_id}/{batch_id}")
            fragments = connection.execute(
                "SELECT * FROM knowledge_direct_chapter_fragment "
                "WHERE run_id=? AND batch_id=? "
                "ORDER BY CASE context_role "
                "WHEN 'CONTEXT_BEFORE' THEN 1 WHEN 'CURRENT' THEN 2 ELSE 3 END,"
                "fragment_ordinal",
                (run_id, batch_id),
            ).fetchall()
            visuals = connection.execute(
                "SELECT * FROM knowledge_direct_chapter_visual_ref "
                "WHERE run_id=? AND batch_id=? ORDER BY visual_ordinal",
                (run_id, batch_id),
            ).fetchall()
        return {
            "batch": dict(batch),
            "fragments": [dict(row) for row in fragments],
            "visuals": [dict(row) for row in visuals],
        }

    def record_packet(
        self,
        run_id: str,
        batch_id: str,
        *,
        packet_hash: str,
        packet_object_hash: str,
        batch_text_object_hash: str,
    ) -> bool:
        now = utc_now_text()
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT stage,packet_hash,packet_object_hash,batch_text_object_hash "
                "FROM knowledge_direct_chapter_batch WHERE run_id=? AND batch_id=?",
                (run_id, batch_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown direct batch: {run_id}/{batch_id}")
            if row["stage"] in {"PACKET_EXPORTED", "IMPORTED"}:
                if (
                    row["packet_hash"] != packet_hash
                    or row["packet_object_hash"] != packet_object_hash
                    or row["batch_text_object_hash"] != batch_text_object_hash
                ):
                    raise ValueError(f"direct packet collision: {run_id}/{batch_id}")
                connection.execute(
                    "UPDATE knowledge_direct_chapter_batch "
                    "SET packet_replay_count=packet_replay_count+1,updated_at=? "
                    "WHERE run_id=? AND batch_id=?",
                    (now, run_id, batch_id),
                )
                return True
            connection.execute(
                "UPDATE knowledge_direct_chapter_batch "
                "SET stage='PACKET_EXPORTED',packet_hash=?,packet_object_hash=?,"
                "batch_text_object_hash=?,updated_at=? "
                "WHERE run_id=? AND batch_id=? AND stage='FROZEN'",
                (
                    packet_hash,
                    packet_object_hash,
                    batch_text_object_hash,
                    now,
                    run_id,
                    batch_id,
                ),
            )
            connection.execute(
                "UPDATE knowledge_direct_run "
                "SET stage='PACKETS_EXPORTING',updated_at=? "
                "WHERE run_id=? AND stage='INITIALIZED'",
                (now, run_id),
            )
        return False

    def import_batch(
        self,
        output: DirectNormalizedBatchOutput,
        *,
        import_input_hash: str,
        import_object_hash: str,
        candidate_object_hashes: Mapping[str, str],
    ) -> bool:
        now = utc_now_text()
        with self.state.transaction() as connection:
            batch = connection.execute(
                "SELECT stage,batch_text_object_hash,import_input_hash,import_object_hash "
                "FROM knowledge_direct_chapter_batch WHERE run_id=? AND batch_id=?",
                (output.run_id, output.batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(f"unknown direct batch: {output.run_id}/{output.batch_id}")
            if batch["stage"] == "IMPORTED":
                if (
                    batch["import_input_hash"] != import_input_hash
                    or batch["import_object_hash"] != import_object_hash
                ):
                    raise ValueError(
                        f"direct batch content collision: {output.run_id}/{output.batch_id}"
                    )
                connection.execute(
                    "UPDATE knowledge_direct_chapter_batch "
                    "SET import_replay_count=import_replay_count+1,updated_at=? "
                    "WHERE run_id=? AND batch_id=?",
                    (now, output.run_id, output.batch_id),
                )
                return True
            if batch["stage"] != "PACKET_EXPORTED":
                raise ValueError("direct batch must be packet-exported before import")
            if batch["batch_text_object_hash"] != output.batch_text_object_hash:
                raise ValueError("direct batch text hash changed before import")
            for candidate in output.skills:
                payload = candidate.model_dump(mode="json")
                secondary_modules = [
                    item.value for item in candidate.secondary_modules
                ]
                connection.execute(
                    "INSERT INTO knowledge_direct_raw_sol_candidate("
                    "run_id,batch_id,candidate_id,chapter_unit_id,sol_version_id,"
                    "primary_module,secondary_modules_json,secondary_module_count,status,"
                    "skill_name,decision_question,core_principle,"
                    "applicable_conditions_json,reasoning_steps_json,reasoning_step_count,"
                    "required_evidence_json,positive_signals_json,negative_signals_json,"
                    "invalidation_conditions_json,failure_modes_json,confidence,"
                    "source_ref_count,visual_ref_count,uncertainty_reason,"
                    "formal_committee_weight_allowed,candidate_object_hash,"
                    "candidate_json,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                    (
                        output.run_id,
                        output.batch_id,
                        candidate.candidate_id,
                        candidate.chapter_unit_id,
                        candidate.sol_version_id,
                        candidate.primary_module.value,
                        _json(secondary_modules),
                        len(secondary_modules),
                        candidate.status.value,
                        *_semantic_columns(payload),
                        len(candidate.source_refs),
                        len(candidate.visual_refs),
                        candidate.uncertainty_reason,
                        candidate_object_hashes[candidate.candidate_id],
                        _json(payload),
                        now,
                    ),
                )
                for ordinal, source_ref in enumerate(candidate.source_refs, start=1):
                    locator = source_ref.locator
                    connection.execute(
                        "INSERT INTO knowledge_direct_candidate_source_ref("
                        "run_id,batch_id,candidate_id,ref_ordinal,source_id,source_file_hash,"
                        "chapter_unit_id,fragment_id,fragment_object_hash,"
                        "source_object_hash,slice_hash,"
                        "source_kind,unit_index,start_offset,end_offset,locator_json,"
                        "original_locator,paragraph_head,visual_evidence_ids_json"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            output.run_id,
                            output.batch_id,
                            candidate.candidate_id,
                            ordinal,
                            source_ref.source_id,
                            source_ref.source_file_hash,
                            source_ref.chapter_unit_id,
                            source_ref.fragment_id,
                            source_ref.fragment_object_hash,
                            source_ref.source_object_hash,
                            source_ref.slice_hash,
                            locator.source_kind.value,
                            locator.unit_index,
                            locator.start_offset,
                            locator.end_offset,
                            _json(locator.model_dump(mode="json")),
                            source_ref.original_locator,
                            source_ref.paragraph_head,
                            _json(source_ref.visual_evidence_ids),
                        ),
                    )
                for ordinal, visual_ref in enumerate(candidate.visual_refs, start=1):
                    connection.execute(
                        "INSERT INTO knowledge_direct_candidate_visual_ref("
                        "run_id,batch_id,candidate_id,ref_ordinal,evidence_id,object_hash,"
                        "source_kind,unit_index,evidence_locator_json"
                        ") VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            output.run_id,
                            output.batch_id,
                            candidate.candidate_id,
                            ordinal,
                            visual_ref.evidence_id,
                            visual_ref.object_hash,
                            visual_ref.source_kind.value,
                            visual_ref.unit_index,
                            _json(visual_ref.evidence_locator),
                        ),
                    )
            connection.execute(
                "UPDATE knowledge_direct_chapter_batch "
                "SET stage='IMPORTED',import_input_hash=?,import_object_hash=?,"
                "imported_candidate_count=?,no_skill_reason=?,imported_at=?,updated_at=? "
                "WHERE run_id=? AND batch_id=? AND stage='PACKET_EXPORTED'",
                (
                    import_input_hash,
                    import_object_hash,
                    len(output.skills),
                    output.no_skill_reason,
                    now,
                    now,
                    output.run_id,
                    output.batch_id,
                ),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_chapter_batch "
                "WHERE run_id=? AND stage<>'IMPORTED'",
                (output.run_id,),
            ).fetchone()[0]
            if remaining == 0:
                connection.execute(
                    "UPDATE knowledge_direct_run "
                    "SET stage='BATCHES_IMPORTED',updated_at=? WHERE run_id=?",
                    (now, output.run_id),
                )
        return False

    def candidate_payloads(self, run_id: str) -> dict[str, dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT candidate_id,candidate_json,candidate_object_hash "
                "FROM knowledge_direct_raw_sol_candidate "
                "WHERE run_id=? ORDER BY batch_id,candidate_id",
                (run_id,),
            ).fetchall()
        return {
            str(row["candidate_id"]): {
                "payload": json.loads(row["candidate_json"]),
                "object_hash": row["candidate_object_hash"],
            }
            for row in rows
        }

    def get_dedup_manifest(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_direct_sol_confirmed_dedup_manifest "
                "WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_finalize_replay(self, run_id: str) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_direct_sol_confirmed_dedup_manifest "
                "SET finalize_replay_count=finalize_replay_count+1 WHERE run_id=?",
                (run_id,),
            )

    def finalize(
        self,
        manifest: DirectDedupManifest,
        *,
        manifest_hash: str,
        manifest_object_hash: str,
        final_records: Sequence[Mapping[str, Any]],
        bundle: Mapping[str, Any],
        bundle_object_hash: str,
    ) -> None:
        now = utc_now_text()
        with self.state.transaction() as connection:
            run = connection.execute(
                "SELECT stage FROM knowledge_direct_run WHERE run_id=?",
                (manifest.run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown direct run: {manifest.run_id}")
            if run["stage"] != "BATCHES_IMPORTED":
                raise ValueError("all frozen direct chapters must be imported before finalize")
            unimported = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_chapter_batch "
                "WHERE run_id=? AND stage<>'IMPORTED'",
                (manifest.run_id,),
            ).fetchone()[0]
            if unimported:
                raise ValueError("all frozen direct chapters must be imported before finalize")
            if connection.execute(
                "SELECT 1 FROM knowledge_direct_sol_confirmed_dedup_manifest "
                "WHERE run_id=?",
                (manifest.run_id,),
            ).fetchone():
                raise ValueError(f"direct run already finalized: {manifest.run_id}")
            connection.execute(
                "INSERT INTO knowledge_direct_sol_confirmed_dedup_manifest("
                "run_id,manifest_id,manifest_hash,manifest_object_hash,embedding_usage,"
                "sol_confirmed,sol_version,sol_version_hash,manifest_json,created_at"
                ") VALUES(?,?,?,?,?,1,?,?,?,?)",
                (
                    manifest.run_id,
                    manifest.manifest_id,
                    manifest_hash,
                    manifest_object_hash,
                    manifest.embedding_usage,
                    manifest.sol_version,
                    manifest.sol_version_hash,
                    _json(manifest.model_dump(mode="json")),
                    now,
                ),
            )
            for record in final_records:
                self._insert_final_record(
                    connection,
                    manifest.run_id,
                    manifest.manifest_id,
                    record,
                    now,
                )
            connection.execute(
                "INSERT INTO knowledge_direct_shadow_bundle("
                "run_id,manifest_id,bundle_id,all_skill_ids_json,shadow_skill_ids_json,"
                "all_skill_count,shadow_skill_count,non_ready_skill_count,"
                "formal_committee_weight_allowed,bundle_object_hash,bundle_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                (
                    manifest.run_id,
                    manifest.manifest_id,
                    bundle["bundle_id"],
                    _json(bundle["all_skill_ids"]),
                    _json(bundle["shadow_skill_ids"]),
                    len(bundle["all_skill_ids"]),
                    len(bundle["shadow_skill_ids"]),
                    len(bundle["all_skill_ids"]) - len(bundle["shadow_skill_ids"]),
                    bundle_object_hash,
                    _json(bundle),
                    now,
                ),
            )
            connection.execute(
                "UPDATE knowledge_direct_run "
                "SET stage='FINALIZED',updated_at=?,finalized_at=? WHERE run_id=?",
                (now, now, manifest.run_id),
            )

    @staticmethod
    def _insert_final_record(
        connection: sqlite3.Connection,
        run_id: str,
        manifest_id: str,
        record: Mapping[str, Any],
        now: str,
    ) -> None:
        payload = record["payload"]
        secondary_modules = payload["secondary_modules"]
        connection.execute(
            "INSERT INTO knowledge_direct_final_skill("
            "run_id,manifest_id,final_skill_id,status,skill_name,primary_module,"
            "secondary_modules_json,secondary_module_count,decision_question,"
            "core_principle,applicable_conditions_json,reasoning_steps_json,"
            "reasoning_step_count,required_evidence_json,positive_signals_json,"
            "negative_signals_json,invalidation_conditions_json,failure_modes_json,"
            "confidence,module_count,contribution_count,source_ref_count,visual_ref_count,"
            "uncertainty_reason,formal_committee_weight_allowed,skill_object_hash,"
            "skill_json,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (
                run_id,
                manifest_id,
                payload["final_skill_id"],
                payload["status"],
                payload["skill_name"],
                payload["primary_module"],
                _json(secondary_modules),
                len(secondary_modules),
                *_semantic_columns(payload)[1:],
                1 + len(secondary_modules),
                len(payload["candidate_ids"]),
                len(record["source_refs"]),
                len(record["visual_refs"]),
                payload["uncertainty_reason"],
                record["object_hash"],
                record["json"],
                now,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_direct_final_skill_module("
            "run_id,final_skill_id,module_ordinal,module_role,module"
            ") VALUES(?,?,1,'PRIMARY',?)",
            (run_id, payload["final_skill_id"], payload["primary_module"]),
        )
        for ordinal, module in enumerate(secondary_modules, start=1):
            connection.execute(
                "INSERT INTO knowledge_direct_final_skill_module("
                "run_id,final_skill_id,module_ordinal,module_role,module"
                ") VALUES(?,?,?,'SECONDARY',?)",
                (run_id, payload["final_skill_id"], ordinal, module),
            )
        for ordinal, candidate_id in enumerate(payload["candidate_ids"], start=1):
            connection.execute(
                "INSERT INTO knowledge_direct_final_to_candidate_contribution("
                "run_id,final_skill_id,contribution_ordinal,candidate_id) VALUES(?,?,?,?)",
                (run_id, payload["final_skill_id"], ordinal, candidate_id),
            )
        for ordinal, source_ref in enumerate(record["source_refs"], start=1):
            locator = source_ref["locator"]
            connection.execute(
                "INSERT INTO knowledge_direct_final_source_ref("
                "run_id,final_skill_id,ref_ordinal,batch_id,source_id,source_file_hash,"
                "chapter_unit_id,fragment_id,fragment_object_hash,source_object_hash,"
                "slice_hash,source_kind,"
                "unit_index,start_offset,end_offset,locator_json,original_locator,"
                "paragraph_head,visual_evidence_ids_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    payload["final_skill_id"],
                    ordinal,
                    source_ref["batch_id"],
                    source_ref["source_id"],
                    source_ref["source_file_hash"],
                    source_ref["chapter_unit_id"],
                    source_ref["fragment_id"],
                    source_ref["fragment_object_hash"],
                    source_ref["source_object_hash"],
                    source_ref["slice_hash"],
                    locator["source_kind"],
                    locator["unit_index"],
                    locator["start_offset"],
                    locator["end_offset"],
                    _json(locator),
                    source_ref["original_locator"],
                    source_ref["paragraph_head"],
                    _json(source_ref["visual_evidence_ids"]),
                ),
            )
        for ordinal, visual_ref in enumerate(record["visual_refs"], start=1):
            connection.execute(
                "INSERT INTO knowledge_direct_final_visual_ref("
                "run_id,final_skill_id,ref_ordinal,batch_id,evidence_id,object_hash,"
                "source_kind,unit_index,evidence_locator_json"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    payload["final_skill_id"],
                    ordinal,
                    visual_ref["batch_id"],
                    visual_ref["evidence_id"],
                    visual_ref["object_hash"],
                    visual_ref["source_kind"],
                    visual_ref["unit_index"],
                    _json(visual_ref["evidence_locator"]),
                ),
            )

    def final_rows(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_direct_final_skill "
                "WHERE run_id=? ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_bundle(self, run_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_direct_shadow_bundle WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def statistics(self, run_id: str) -> dict[str, Any]:
        with closing(self.state.connect()) as connection:
            run = connection.execute(
                "SELECT * FROM knowledge_direct_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown direct run: {run_id}")
            batch_rows = connection.execute(
                "SELECT stage,COUNT(*) AS count,SUM(packet_replay_count) AS packet_replays,"
                "SUM(import_replay_count) AS import_replays "
                "FROM knowledge_direct_chapter_batch WHERE run_id=? GROUP BY stage",
                (run_id,),
            ).fetchall()
            status_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM knowledge_direct_final_skill "
                "WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
            candidate_status_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM knowledge_direct_raw_sol_candidate "
                "WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
            module_rows = connection.execute(
                "SELECT module,COUNT(*) AS count FROM knowledge_direct_final_skill_module "
                "WHERE run_id=? GROUP BY module",
                (run_id,),
            ).fetchall()
            raw_module_rows = connection.execute(
                "SELECT module,COUNT(*) AS count FROM ("
                "SELECT primary_module AS module FROM knowledge_direct_raw_sol_candidate "
                "WHERE run_id=? UNION ALL "
                "SELECT j.value AS module FROM knowledge_direct_raw_sol_candidate c,"
                "json_each(c.secondary_modules_json) j WHERE c.run_id=?"
                ") GROUP BY module",
                (run_id, run_id),
            ).fetchall()
            zero_source = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_final_skill "
                "WHERE run_id=? AND source_ref_count=0",
                (run_id,),
            ).fetchone()[0]
            visual_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_final_visual_ref WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            raw_zero_source = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_raw_sol_candidate "
                "WHERE run_id=? AND source_ref_count=0",
                (run_id,),
            ).fetchone()[0]
            raw_visual_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_candidate_visual_ref WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            uncertainty_rows = connection.execute(
                "SELECT final_skill_id,uncertainty_reason "
                "FROM knowledge_direct_final_skill "
                "WHERE run_id=? AND status='NEEDS_USER_REVIEW' ORDER BY final_skill_id",
                (run_id,),
            ).fetchall()
            raw_uncertainty_rows = connection.execute(
                "SELECT candidate_id,uncertainty_reason "
                "FROM knowledge_direct_raw_sol_candidate "
                "WHERE run_id=? AND status='NEEDS_USER_REVIEW' ORDER BY candidate_id",
                (run_id,),
            ).fetchall()
            manifest = connection.execute(
                "SELECT embedding_usage,sol_confirmed,sol_version,sol_version_hash,"
                "finalize_replay_count "
                "FROM knowledge_direct_sol_confirmed_dedup_manifest WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return {
            "run_id": run_id,
            "stage": run["stage"],
            "frozen_source_count": run["frozen_source_count"],
            "frozen_batch_count": run["frozen_batch_count"],
            "batch_stage_counts": {
                str(row["stage"]): row["count"] for row in batch_rows
            },
            "raw_candidate_status_counts": {
                str(row["status"]): row["count"] for row in candidate_status_rows
            },
            "final_status_counts": {
                str(row["status"]): row["count"] for row in status_rows
            },
            "module_counts": {
                str(row["module"]): row["count"] for row in module_rows
            },
            "raw_module_counts": {
                str(row["module"]): row["count"] for row in raw_module_rows
            },
            "raw_zero_source_candidate_count": raw_zero_source,
            "raw_visual_ref_count": raw_visual_count,
            "zero_source_skill_count": zero_source,
            "visual_ref_count": visual_count,
            "uncertainties": [dict(row) for row in uncertainty_rows],
            "raw_uncertainties": [dict(row) for row in raw_uncertainty_rows],
            "idempotency": {
                "init_replay_count": run["init_replay_count"],
                "packet_replay_count": sum(
                    int(row["packet_replays"] or 0) for row in batch_rows
                ),
                "import_replay_count": sum(
                    int(row["import_replays"] or 0) for row in batch_rows
                ),
                "finalize_replay_count": (
                    int(manifest["finalize_replay_count"]) if manifest is not None else 0
                ),
            },
            "dedup": (
                None
                if manifest is None
                else {
                    "embedding_usage": manifest["embedding_usage"],
                    "sol_confirmed": bool(manifest["sol_confirmed"]),
                    "sol_version": manifest["sol_version"],
                    "sol_version_hash": manifest["sol_version_hash"],
                }
            ),
        }


__all__ = ["DirectSourceDistillationRepository"]
