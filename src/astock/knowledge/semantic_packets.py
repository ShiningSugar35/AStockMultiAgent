"""Offline OpenCode/DeepSeek packets built only from complete ArgumentUnits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from astock.core.atomic import atomic_write_bytes
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.knowledge.semantic_repository import SemanticFunnelRepository
from astock.knowledge.semantic_storage import ParquetSemanticStore
from astock.schemas import (
    BookMethodCategory,
    SemanticArgumentPacket,
    SemanticArgumentScore,
    SemanticDeepSeekResult,
    SemanticLlmBatch,
    SemanticLlmBatchStatus,
    SemanticLlmDecision,
    SemanticPacketParagraph,
    SemanticRunStage,
    SemanticScreenDecision,
    SemanticSkillCandidate,
)


@dataclass(frozen=True, slots=True)
class SemanticPacketExecution:
    batch: SemanticLlmBatch
    batch_directory: Path
    packet_file: Path
    result_schema_file: Path
    manifest_file: Path
    held_back_calibration_count: int
    held_back_structural_count: int
    held_back_oversize_count: int


class SemanticPacketService:
    def __init__(
        self,
        repository: SemanticFunnelRepository,
        object_store: ObjectStore,
        parquet_store: ParquetSemanticStore,
        runtime_root: Path,
        prompt_file: Path,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.parquet_store = parquet_store
        self.runtime_root = runtime_root.resolve()
        self.prompt_file = prompt_file.resolve()

    def export(self, run_id: str) -> SemanticPacketExecution:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.stage not in {
            SemanticRunStage.EMBEDDING_SCREENED,
            SemanticRunStage.DEEPSEEK_PACKET_READY,
        }:
            raise ValueError("semantic run is not ready for an offline DeepSeek packet")
        registration = self.repository.embedding_registration(run_id)
        if registration is None:
            raise ValueError("semantic run has no registered embedding result")
        directory = self.parquet_store.run_directory(
            run.author_source_id,
            run_id,
            registration.manifest.manifest_id,
        )
        scores_path = directory / "scores.parquet"
        if (
            not scores_path.is_file()
            or sha256_bytes(scores_path.read_bytes()) != registration.score_parquet_sha256
        ):
            raise ValueError("semantic score Parquet is missing or does not match SQLite")
        scores = _read_scores(scores_path)
        arguments = {
            argument.argument_unit_id: argument
            for argument in self.repository.argument_units(run_id)
        }
        exportable = {
            score.argument_unit_id: score
            for score in scores
            if score.decision
            in {SemanticScreenDecision.KEEP, SemanticScreenDecision.NEEDS_REVIEW}
            and score.argument_unit_id in arguments
            and arguments[score.argument_unit_id].standalone_distillable
            and arguments[score.argument_unit_id].status.value == "READY"
        }
        held_back = sum(
            score.decision is SemanticScreenDecision.CALIBRATION_REQUIRED
            for score in scores
        )
        structurally_held = [
            arguments[score.argument_unit_id]
            for score in scores
            if score.argument_unit_id in arguments
            and score.decision
            in {SemanticScreenDecision.KEEP, SemanticScreenDecision.NEEDS_REVIEW}
            and not (
                arguments[score.argument_unit_id].standalone_distillable
                and arguments[score.argument_unit_id].status.value == "READY"
            )
        ]
        held_back_oversize = sum(
            "AU_OVERSIZE_REVIEW_REQUIRED" in argument.reason_codes
            for argument in structurally_held
        )
        if not exportable:
            raise ValueError("no calibrated or review-band ArgumentUnits are exportable")
        paragraphs = {
            paragraph.paragraph_id: paragraph
            for group in self.repository.paragraph_groups(run_id).values()
            for paragraph in group
        }
        relations = {
            relation.relation_id: relation
            for relation in self.repository.argument_relations(run_id)
        }
        packets: list[SemanticArgumentPacket] = []
        for argument in arguments.values():
            score = exportable.get(argument.argument_unit_id)
            if score is None:
                continue
            argument_paragraphs = [paragraphs[item] for item in argument.paragraph_ids]
            core = {
                "argument_unit_id": argument.argument_unit_id,
                "run_id": argument.run_id,
                "author_source_id": argument.author_source_id,
                "content_type": argument.content_type,
                "content_id": argument.content_id,
                "source_snapshot_ids": argument.source_snapshot_ids,
                "argument_text_sha256": argument.text_object_sha256,
                "paragraphs": [
                    SemanticPacketParagraph(
                        paragraph_id=paragraph.paragraph_id,
                        ordinal=paragraph.ordinal,
                        text=self.object_store.get_bytes(
                            paragraph.text_object_sha256
                        ).decode("utf-8"),
                        text_object_sha256=paragraph.text_object_sha256,
                        primary_role=paragraph.primary_role,
                        rhetorical_roles=paragraph.rhetorical_roles,
                        standalone_distillable=paragraph.standalone_distillable,
                        depends_on_previous=paragraph.depends_on_previous,
                        depends_on_next=paragraph.depends_on_next,
                        merge_action=paragraph.merge_action,
                        created_at=run.started_at,
                    ).model_dump(mode="json")
                    for paragraph in argument_paragraphs
                ],
                "relations": [
                    relations[relation_id].model_dump(mode="json")
                    for relation_id in argument.relation_ids
                ],
                "topic_relevance": score.topic_relevance,
                "methodological_completeness": score.methodological_completeness,
                "category_scores": {
                    category.value: value
                    for category, value in score.category_scores.items()
                },
                "selected_categories": [
                    category.value for category in score.selected_categories
                ],
                "semantic_decision": score.decision.value,
            }
            packets.append(
                SemanticArgumentPacket.model_validate(
                    {
                        **core,
                        "input_sha256": content_hash(core),
                        "created_at": run.started_at,
                    }
                )
            )
        packet_bytes = b"".join(
            canonical_json_bytes(packet.model_dump(mode="json")) + b"\n"
            for packet in packets
        )
        packet_object = self.object_store.put_bytes(packet_bytes)
        schema_bytes = canonical_json_bytes(SemanticDeepSeekResult.model_json_schema())
        schema_hash = sha256_bytes(schema_bytes)
        prompt_bytes = self.prompt_file.read_bytes()
        prompt_hash = sha256_bytes(prompt_bytes)
        input_manifest = {
            "schema_version": "semantic-deepseek-input-manifest-v1",
            "run_id": run_id,
            "embedding_manifest_id": registration.manifest.manifest_id,
            "packet_object_sha256": packet_object.sha256,
            "result_schema_sha256": schema_hash,
            "prompt_sha256": prompt_hash,
            "argument_inputs": [
                {
                    "argument_unit_id": packet.argument_unit_id,
                    "input_sha256": packet.input_sha256,
                }
                for packet in packets
            ],
            "held_back_policy": "CALIBRATION_REQUIRED_NOT_EXPORTED_NOT_DELETED",
            "held_back_calibration_count": held_back,
            "held_back_structural_count": len(structurally_held),
            "held_back_oversize_count": held_back_oversize,
        }
        input_manifest_object = self.object_store.put_json(input_manifest)
        batch_identity = {
            "run_id": run_id,
            "packet_object_sha256": packet_object.sha256,
            "prompt_sha256": prompt_hash,
            "result_schema_sha256": schema_hash,
            "input_manifest_sha256": input_manifest_object.sha256,
            "model_id": "deepseek-v4-flash",
        }
        now = run.started_at
        batch = SemanticLlmBatch(
            batch_id=f"semantic-llm-batch:{content_hash(batch_identity)}",
            run_id=run_id,
            provider="opencode-manual",
            model_id="deepseek-v4-flash",
            prompt_sha256=prompt_hash,
            result_schema_sha256=schema_hash,
            input_manifest_sha256=input_manifest_object.sha256,
            packet_object_sha256=packet_object.sha256,
            status=SemanticLlmBatchStatus.PACKET_READY,
            exported_argument_count=len(packets),
            imported_result_count=0,
            updated_at=now,
            created_at=now,
        )
        batch_directory = (
            self.runtime_root
            / "knowledge_semantic_packets"
            / batch.batch_id.rsplit(":", maxsplit=1)[-1]
        )
        packet_file = batch_directory / "packet.jsonl"
        schema_file = batch_directory / "result-schema.json"
        manifest_file = batch_directory / "manifest.json"
        atomic_write_bytes(packet_file, packet_bytes)
        atomic_write_bytes(schema_file, schema_bytes + b"\n")
        atomic_write_bytes(
            manifest_file,
            canonical_json_bytes(
                {
                    **input_manifest,
                    "batch_id": batch.batch_id,
                    "provider": batch.provider,
                    "model_id": batch.model_id,
                    "expected_result_file": "deepseek-results.jsonl",
                }
            )
            + b"\n",
        )
        self.repository.register_llm_batch(batch)
        self.repository.save_run(
            run.model_copy(update={"stage": SemanticRunStage.DEEPSEEK_PACKET_READY})
        )
        return SemanticPacketExecution(
            batch=batch,
            batch_directory=batch_directory,
            packet_file=packet_file,
            result_schema_file=schema_file,
            manifest_file=manifest_file,
            held_back_calibration_count=held_back,
            held_back_structural_count=len(structurally_held),
            held_back_oversize_count=held_back_oversize,
        )

    def stage_results(self, batch_id: str, result_file: Path) -> SemanticLlmBatch:
        batch = self.repository.get_llm_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        self._verify_batch_contract(batch)
        if batch.status not in {
            SemanticLlmBatchStatus.PACKET_READY,
            SemanticLlmBatchStatus.RESULT_STAGED,
        }:
            raise ValueError("semantic LLM batch cannot stage results at its current status")
        response_bytes = result_file.read_bytes()
        results = self._validate_result_bytes(batch, response_bytes)
        response_object = self.object_store.put_bytes(response_bytes)
        if batch.status is SemanticLlmBatchStatus.RESULT_STAGED:
            if batch.response_object_sha256 != response_object.sha256:
                raise ValueError("semantic LLM batch response is immutable once staged")
            self._advance_result_stage(batch.run_id)
            return batch
        staged = batch.model_copy(
            update={
                "response_object_sha256": response_object.sha256,
                "status": SemanticLlmBatchStatus.RESULT_STAGED,
                "imported_result_count": len(results),
                "updated_at": datetime.now(UTC),
            }
        )
        self.repository.register_llm_batch(staged)
        self._advance_result_stage(batch.run_id)
        return staged

    def import_results(self, batch_id: str) -> tuple[SemanticLlmBatch, int]:
        batch = self.repository.get_llm_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        self._verify_batch_contract(batch)
        if batch.status is SemanticLlmBatchStatus.IMPORTED:
            return batch, self.repository.finalize_imported_batch(batch)
        if (
            batch.status is not SemanticLlmBatchStatus.RESULT_STAGED
            or batch.response_object_sha256 is None
        ):
            raise ValueError("semantic LLM batch has no staged result")
        response_bytes = self.object_store.get_bytes(batch.response_object_sha256)
        results = self._validate_result_bytes(batch, response_bytes)
        packets = self._packet_index(batch)
        candidates: list[SemanticSkillCandidate] = []
        for result in results:
            if result.decision is not SemanticLlmDecision.KEEP:
                continue
            packet = packets[result.argument_unit_id]
            paragraph_ids = {paragraph.paragraph_id for paragraph in packet.paragraphs}
            for ordinal, draft in enumerate(result.candidates, start=1):
                if not set(draft.evidence_paragraph_ids).issubset(paragraph_ids):
                    raise ValueError("DeepSeek candidate cites a paragraph outside its argument")
                payload = {
                    "schema_version": "semantic-skill-candidate-payload-v1",
                    "argument_unit_id": result.argument_unit_id,
                    "source_snapshot_ids": packet.source_snapshot_ids,
                    "input_sha256": result.input_sha256,
                    "title": draft.title,
                    "method_summary": draft.method_summary,
                    "applicability": draft.applicability,
                    "counterevidence": draft.counterevidence,
                    "invalidation_conditions": draft.invalidation_conditions,
                    "evidence_paragraph_ids": draft.evidence_paragraph_ids,
                    "method_categories": [
                        category.value for category in result.method_categories
                    ],
                    "model_confidence": result.confidence,
                    "reason_codes": result.reason_codes,
                    "llm_batch_id": batch.batch_id,
                    "llm_response_object_sha256": batch.response_object_sha256,
                }
                payload_object = self.object_store.put_json(payload)
                identity = {
                    "batch_id": batch.batch_id,
                    "argument_unit_id": result.argument_unit_id,
                    "ordinal": ordinal,
                    "payload_object_sha256": payload_object.sha256,
                }
                candidates.append(
                    SemanticSkillCandidate(
                        candidate_id=f"semantic-skill-candidate:{content_hash(identity)}",
                        run_id=batch.run_id,
                        author_source_id=packet.author_source_id,
                        argument_unit_ids=[result.argument_unit_id],
                        method_categories=result.method_categories,
                        payload_object_sha256=payload_object.sha256,
                        prompt_sha256=batch.prompt_sha256,
                        model_id=batch.model_id,
                        llm_batch_id=batch.batch_id,
                        llm_response_object_sha256=batch.response_object_sha256,
                        created_at=batch.created_at,
                    )
                )
        imported = batch.model_copy(
            update={
                "status": SemanticLlmBatchStatus.IMPORTED,
                "imported_result_count": len(results),
                "updated_at": datetime.now(UTC),
            }
        )
        self.repository.import_candidates(imported, candidates)
        return imported, len(candidates)

    def _verify_batch_contract(self, batch: SemanticLlmBatch) -> None:
        schema_hash = sha256_bytes(
            canonical_json_bytes(SemanticDeepSeekResult.model_json_schema())
        )
        prompt_hash = sha256_bytes(self.prompt_file.read_bytes())
        if (
            schema_hash != batch.result_schema_sha256
            or prompt_hash != batch.prompt_sha256
        ):
            raise ValueError("semantic LLM batch prompt or schema contract changed")

    def _advance_result_stage(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.stage is not SemanticRunStage.DEEPSEEK_RESULT_STAGED:
            self.repository.save_run(
                run.model_copy(update={"stage": SemanticRunStage.DEEPSEEK_RESULT_STAGED})
            )

    def _packet_index(
        self,
        batch: SemanticLlmBatch,
    ) -> dict[str, SemanticArgumentPacket]:
        packet_bytes = self.object_store.get_bytes(batch.packet_object_sha256)
        packets = [
            SemanticArgumentPacket.model_validate_json(line)
            for line in packet_bytes.splitlines()
            if line.strip()
        ]
        by_id = {packet.argument_unit_id: packet for packet in packets}
        if len(by_id) != len(packets) or len(packets) != batch.exported_argument_count:
            raise ValueError("registered semantic packet is incomplete or duplicated")
        return by_id

    def _validate_result_bytes(
        self,
        batch: SemanticLlmBatch,
        response_bytes: bytes,
    ) -> list[SemanticDeepSeekResult]:
        try:
            results = [
                SemanticDeepSeekResult.model_validate_json(line)
                for line in response_bytes.splitlines()
                if line.strip()
            ]
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("invalid DeepSeek semantic JSONL result") from exc
        packets = self._packet_index(batch)
        by_id = {result.argument_unit_id: result for result in results}
        if len(by_id) != len(results):
            raise ValueError("DeepSeek semantic results contain duplicate arguments")
        if set(by_id) != set(packets):
            raise ValueError("DeepSeek semantic results must cover every exported argument once")
        for argument_unit_id, result in by_id.items():
            packet = packets[argument_unit_id]
            if result.input_sha256 != packet.input_sha256:
                raise ValueError("DeepSeek semantic result input hash mismatch")
            paragraph_ids = {paragraph.paragraph_id for paragraph in packet.paragraphs}
            if any(
                not set(candidate.evidence_paragraph_ids).issubset(paragraph_ids)
                for candidate in result.candidates
            ):
                raise ValueError("DeepSeek semantic result cites an unknown paragraph")
        return [by_id[argument_unit_id] for argument_unit_id in sorted(by_id)]


def _read_scores(path: Path) -> list[SemanticArgumentScore]:
    rows = pq.ParquetFile(path).read().to_pylist()
    return [
        SemanticArgumentScore(
            score_id=str(row["score_id"]),
            run_id=str(row["run_id"]),
            argument_unit_id=str(row["argument_unit_id"]),
            embedding_manifest_id=str(row["embedding_manifest_id"]),
            topic_relevance=float(row["topic_relevance"]),
            methodological_completeness=float(row["methodological_completeness"]),
            category_scores={
                BookMethodCategory(category): float(value)
                for category, value in json.loads(
                    str(row["category_scores_json"])
                ).items()
            },
            selected_categories=[
                BookMethodCategory(category) for category in row["selected_categories"]
            ],
            decision=SemanticScreenDecision(str(row["decision"])),
            reason_codes=[str(value) for value in row["reason_codes"]],
        )
        for row in rows
    ]


__all__ = ["SemanticPacketExecution", "SemanticPacketService"]
