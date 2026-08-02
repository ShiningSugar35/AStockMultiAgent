"""Deterministic direct-source packet, import, finalize, and shadow gates."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from astock.core.errors import DataQualityError, StorageError
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.direct_source_distillation_repository import (
    DirectSourceDistillationRepository,
)
from astock.schemas.direct_source_distillation import (
    DirectCandidateSourceRef,
    DirectCandidateVisualRef,
    DirectDedupManifest,
    DirectDocxBatchLocator,
    DirectFinalSkillDraft,
    DirectNormalizedBatchOutput,
    DirectPdfBatchLocator,
    DirectRawSkillCandidate,
    DirectRunInitManifest,
    DirectSkillModule,
    DirectSkillStatus,
    DirectSolBatchOutput,
    DirectSourceKind,
    parse_direct_source_locator,
)

_NORMALIZATION_CONTRACT = (
    "Unicode NFKC; collapse contiguous whitespace to U+0020; strip"
)
_SERIALIZATION_CONTRACT = {
    DirectSourceKind.PDF: (
        "UTF-8 page=<1-based page>\\n<normalized text>, joined with \\n\\f\\n"
    ),
    DirectSourceKind.DOCX: (
        "UTF-8 paragraph=<1-based paragraph>\\n<normalized text>, "
        "joined with \\n\\f\\n"
    ),
}
_OFFSET_CONTRACT = {
    DirectSourceKind.PDF: (
        "0-based half-open Python code-point offsets in normalized page text"
    ),
    DirectSourceKind.DOCX: (
        "0-based half-open Python code-point offsets in normalized paragraph text"
    ),
}
_SEMANTIC_FIELDS = (
    "skill_name",
    "primary_module",
    "secondary_modules",
    "decision_question",
    "core_principle",
    "applicable_conditions",
    "reasoning_steps",
    "required_evidence",
    "positive_signals",
    "negative_signals",
    "invalidation_conditions",
    "failure_modes",
    "confidence",
    "status",
    "uncertainty_reason",
)
_LIST_SEMANTIC_FIELDS = (
    "applicable_conditions",
    "reasoning_steps",
    "required_evidence",
    "positive_signals",
    "negative_signals",
    "invalidation_conditions",
    "failure_modes",
)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json_bytes(data: bytes) -> object:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DataQualityError(f"invalid strict UTF-8 JSON: {exc}") from exc


def _validate_model[ModelT: BaseModel](
    model_type: type[ModelT],
    value: object,
) -> ModelT:
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise DataQualityError(
            f"{model_type.__name__} validation failed: {exc}"
        ) from exc


def _canonical_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=False)


def _canonical_hash(model: BaseModel) -> str:
    return sha256_bytes(canonical_json_bytes(_canonical_payload(model)))


def _locator_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_kind": row["source_kind"],
        "unit_index": row["unit_index"],
        "start_offset": row["start_offset"],
        "end_offset": row["end_offset"],
    }


def _semantic_payload(model: BaseModel) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    return {field: payload[field] for field in _SEMANTIC_FIELDS}


def _source_ref_key(ref: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        ref["source_file_hash"],
        ref["source_object_hash"],
        ref["original_locator"],
    )


class DirectSourceDistillationService:
    """Run direct-source distillation without reviewed AU or embedding dependencies."""

    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store
        self.repository = DirectSourceDistillationRepository(state)

    @staticmethod
    def load_json_file(path: Path) -> object:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DataQualityError(
                f"cannot read direct-source JSON file: {path.name}"
            ) from exc
        return _strict_json_bytes(data)

    def init_file(self, path: Path) -> dict[str, Any]:
        value = self.load_json_file(path)
        return self.init(_validate_model(DirectRunInitManifest, value))

    def init(
        self,
        manifest: DirectRunInitManifest | Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(manifest, DirectRunInitManifest):
            manifest = _validate_model(DirectRunInitManifest, manifest)
        source_by_id = {source.source_id: source for source in manifest.sources}
        for source in manifest.sources:
            self._require_object(
                source.source_file_hash,
                f"source file {source.source_id}",
            )
        resolved_visuals: dict[str, list[dict[str, Any]]] = {}
        for batch in manifest.batches:
            source = source_by_id[batch.source_id]
            current_units = {
                fragment.locator.unit_index for fragment in batch.current_fragments
            }
            empty_units = {
                empty.locator.unit_index for empty in batch.audited_empty_units
            }
            source_unit_start = batch.source_unit_start or min(current_units)
            source_unit_end = batch.source_unit_end or max(current_units)
            expected_units = set(range(source_unit_start, source_unit_end + 1))
            if current_units.intersection(empty_units) or current_units.union(
                empty_units
            ) != expected_units:
                raise DataQualityError(
                    "non-empty fragments and audited empty units do not exactly cover "
                    f"the frozen chapter range: {batch.batch_id}"
                )
            for empty in batch.audited_empty_units:
                self._require_object(
                    empty.object_hash,
                    f"audited empty source unit {batch.batch_id}:{empty.locator.unit_index}",
                )
            for fragment in (
                batch.context_before
                + batch.current_fragments
                + batch.context_after
            ):
                self._validate_text_locator(
                    fragment.object_hash,
                    fragment.locator.start_offset,
                    fragment.locator.end_offset,
                    f"fragment {fragment.fragment_id}",
                )
            visuals: list[dict[str, Any]] = []
            for evidence_id in batch.visual_evidence_ids:
                try:
                    visual = self.repository.resolve_visual_evidence(evidence_id)
                except (KeyError, ValueError) as exc:
                    raise DataQualityError(str(exc)) from exc
                if visual["source_kind"] != source.source_kind.value:
                    raise DataQualityError(
                        f"visual evidence kind is outside batch scope: {evidence_id}"
                    )
                if int(visual["unit_index"]) not in current_units:
                    raise DataQualityError(
                        f"visual evidence unit is outside current chapter: {evidence_id}"
                    )
                self._require_object(
                    str(visual["object_hash"]),
                    f"visual evidence {evidence_id}",
                )
                visuals.append(visual)
            resolved_visuals[batch.batch_id] = visuals
        payload = _canonical_payload(manifest)
        input_hash = sha256_bytes(canonical_json_bytes(payload))
        object_ref = self.object_store.put_json(payload)
        existing = self.repository.get_run(manifest.run_id)
        if existing is not None and (
            sha256_bytes(str(existing["manifest_json"]).encode("utf-8"))
            != object_ref.sha256
            or existing["manifest_object_hash"] != object_ref.sha256
        ):
            raise DataQualityError(
                "stored direct run manifest JSON/object hash mismatch"
            )
        replay = self.repository.initialize(
            manifest,
            input_hash=input_hash,
            manifest_object_hash=object_ref.sha256,
            resolved_visuals=resolved_visuals,
        )
        run = self.repository.get_run(manifest.run_id)
        return {
            "status": "INITIALIZED",
            "run_id": manifest.run_id,
            "stage": run["stage"] if run is not None else "UNKNOWN",
            "input_hash": input_hash,
            "manifest_object_hash": object_ref.sha256,
            "frozen_source_count": len(manifest.sources),
            "frozen_batch_count": len(manifest.batches),
            "idempotent_replay": replay,
            "formal_committee_weight_allowed": False,
        }

    def packet_export(self, run_id: str, batch_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown direct run: {run_id}")
        scope = self.repository.batch_scope(run_id, batch_id)
        batch = scope["batch"]
        grouped: dict[str, list[dict[str, Any]]] = {
            "CONTEXT_BEFORE": [],
            "CURRENT": [],
            "CONTEXT_AFTER": [],
        }
        for row in scope["fragments"]:
            text = self._validate_text_locator(
                str(row["object_hash"]),
                int(row["start_offset"]),
                int(row["end_offset"]),
                f"fragment {row['fragment_id']}",
            )
            grouped[str(row["context_role"])].append(
                {
                    "fragment_id": row["fragment_id"],
                    "object_hash": row["object_hash"],
                    "locator": _locator_from_row(row),
                    "text": text[
                        int(row["start_offset"]) : int(row["end_offset"])
                    ],
                }
            )
        if not grouped["CURRENT"]:
            raise DataQualityError("direct packet has no current chapter body")
        if (
            len(grouped["CONTEXT_BEFORE"]) > 2
            or len(grouped["CONTEXT_AFTER"]) > 2
        ):
            raise DataQualityError(
                "direct packet context exceeds the one-to-two unit limit"
            )
        source_kind = DirectSourceKind(str(batch["source_kind"]))
        batch_text_object_hash = self._batch_text_hash(
            source_kind,
            grouped["CURRENT"],
        )
        visuals: list[dict[str, Any]] = []
        for row in scope["visuals"]:
            self._require_object(
                str(row["object_hash"]),
                f"visual evidence {row['evidence_id']}",
            )
            try:
                evidence_locator = json.loads(row["evidence_locator_json"])
            except json.JSONDecodeError as exc:
                raise DataQualityError(
                    f"invalid frozen visual locator: {row['evidence_id']}"
                ) from exc
            visuals.append(
                {
                    "evidence_id": row["evidence_id"],
                    "object_hash": row["object_hash"],
                    "source_kind": row["source_kind"],
                    "unit_index": row["unit_index"],
                    "evidence_locator": evidence_locator,
                }
            )
        packet_core = {
            "schema_version": "direct-source-packet-v1",
            "run_id": run_id,
            "batch_id": batch_id,
            "source_id": batch["source_id"],
            "source_kind": batch["source_kind"],
            "source_file_hash": batch["source_file_hash"],
            "chapter_unit_id": batch["chapter_unit_id"],
            "batch_text_object_hash": batch_text_object_hash,
            "hash_contract": self._hash_contract(source_kind),
            "context_before": grouped["CONTEXT_BEFORE"],
            "chapter_body": grouped["CURRENT"],
            "context_after": grouped["CONTEXT_AFTER"],
            "visual_evidence_refs": visuals,
            "formal_committee_weight_allowed": False,
            "reviewed_argument_units_used": False,
            "embedding_used": False,
        }
        packet_hash = sha256_bytes(canonical_json_bytes(packet_core))
        packet = dict(packet_core) | {"packet_hash": packet_hash}
        object_ref = self.object_store.put_json(packet)
        replay = self.repository.record_packet(
            run_id,
            batch_id,
            packet_hash=packet_hash,
            packet_object_hash=object_ref.sha256,
            batch_text_object_hash=batch_text_object_hash,
        )
        return dict(packet) | {
            "packet_object_hash": object_ref.sha256,
            "idempotent_replay": replay,
        }

    def batch_import_file(
        self,
        run_id: str,
        batch_id: str,
        path: Path,
    ) -> dict[str, Any]:
        value = self.load_json_file(path)
        output = _validate_model(DirectSolBatchOutput, value)
        if output.batch_id != batch_id:
            raise DataQualityError(
                "CLI batch identity does not match the Sol output"
            )
        if not isinstance(value, Mapping):
            raise DataQualityError("Sol batch output must be a JSON object")
        return self.batch_import(
            run_id,
            batch_id,
            output,
            raw_payload=value,
        )

    def batch_import(
        self,
        run_id: str,
        batch_id: str,
        output: DirectSolBatchOutput | Mapping[str, Any],
        *,
        raw_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(output, DirectSolBatchOutput):
            public = output
        else:
            raw_payload = output
            public = _validate_model(DirectSolBatchOutput, output)
        if public.batch_id != batch_id:
            raise DataQualityError(
                "CLI batch identity does not match the Sol output"
            )
        scope = self.repository.batch_scope(run_id, batch_id)
        batch = scope["batch"]
        if batch["stage"] == "FROZEN":
            raise DataQualityError(
                "packet-export must run before batch-import"
            )
        public_payload: Mapping[str, Any] = (
            raw_payload if raw_payload is not None else _canonical_payload(public)
        )
        import_input_hash = sha256_bytes(canonical_json_bytes(public_payload))
        if (
            batch["stage"] == "IMPORTED"
            and batch["import_input_hash"] != import_input_hash
        ):
            raise ValueError(
                f"direct batch content collision: {run_id}/{batch_id}"
            )
        self._validate_public_identity(public, scope)
        normalized = self._normalize_batch(run_id, public, scope)
        candidate_hashes: dict[str, str] = {}
        for candidate in normalized.skills:
            candidate_ref = self.object_store.put_json(
                _canonical_payload(candidate)
            )
            candidate_hashes[candidate.candidate_id] = candidate_ref.sha256
        output_ref = self.object_store.put_json(public_payload)
        if output_ref.sha256 != import_input_hash:
            raise DataQualityError("public Sol object hash is not canonical")
        replay = self.repository.import_batch(
            normalized,
            import_input_hash=import_input_hash,
            import_object_hash=output_ref.sha256,
            candidate_object_hashes=candidate_hashes,
        )
        run = self.repository.get_run(run_id)
        return {
            "status": "IMPORTED",
            "run_id": run_id,
            "run_stage": run["stage"] if run is not None else "UNKNOWN",
            "batch_id": batch_id,
            "candidate_count": len(normalized.skills),
            "candidate_ids": [
                candidate.candidate_id for candidate in normalized.skills
            ],
            "import_input_hash": import_input_hash,
            "import_object_hash": output_ref.sha256,
            "idempotent_replay": replay,
            "formal_committee_weight_allowed": False,
        }

    def dry_convert_user_batch(
        self,
        output: DirectSolBatchOutput | Mapping[str, Any],
        *,
        run_id: str = "direct-source-dry-conversion",
    ) -> dict[str, Any]:
        """Read-only structural conversion gate for an existing public Sol JSON."""

        public = (
            output
            if isinstance(output, DirectSolBatchOutput)
            else _validate_model(DirectSolBatchOutput, output)
        )
        self._validate_hash_contract(public)
        source_ref_count = 0
        candidate_ids: list[str] = []
        for ordinal, skill in enumerate(public.skills, start=1):
            candidate_ids.append(
                self._candidate_id(
                    run_id,
                    public.batch_id,
                    ordinal,
                    skill,
                )
            )
            for source_ref in skill.source_refs:
                parse_direct_source_locator(
                    DirectSourceKind(source_ref.source_kind),
                    source_ref.locator,
                )
                source_ref_count += 1
        visual_objects_verified = 0
        for evidence_id in public.visual_evidence_refs:
            try:
                visual = self.repository.resolve_visual_evidence(evidence_id)
            except (KeyError, ValueError) as exc:
                raise DataQualityError(str(exc)) from exc
            self._require_object(
                str(visual["object_hash"]),
                f"visual evidence {evidence_id}",
            )
            visual_objects_verified += 1
        return {
            "status": "DRY_CONVERTIBLE",
            "batch_id": public.batch_id,
            "source_kind": public.source_kind.value,
            "skill_count": len(public.skills),
            "candidate_ids": candidate_ids,
            "source_ref_count": source_ref_count,
            "visual_evidence_count": len(public.visual_evidence_refs),
            "visual_objects_verified": visual_objects_verified,
            "source_slice_recomputation": "DEFERRED_TO_FROZEN_BATCH_IMPORT",
            "writes_performed": False,
        }

    def _validate_public_identity(
        self,
        public: DirectSolBatchOutput,
        scope: Mapping[str, Any],
    ) -> None:
        batch = scope["batch"]
        if public.source_kind.value != batch["source_kind"]:
            raise DataQualityError(
                "Sol source kind does not match the frozen batch"
            )
        if public.source_file_hash != batch["source_file_hash"]:
            raise DataQualityError(
                "Sol source file hash does not match the frozen batch"
            )
        if public.batch_text_object_hash != batch["batch_text_object_hash"]:
            raise DataQualityError(
                "Sol batch text hash does not match the frozen packet"
            )
        self._validate_hash_contract(public)
        current = [
            row
            for row in scope["fragments"]
            if row["context_role"] == "CURRENT"
        ]
        units = [int(row["unit_index"]) for row in current]
        if public.source_kind is DirectSourceKind.PDF:
            expected_locator: BaseModel = DirectPdfBatchLocator(
                page_start=min(units),
                page_end=max(units),
            )
        else:
            expected_locator = DirectDocxBatchLocator(
                start_paragraph=min(units),
                end_paragraph=max(units),
            )
        if public.locator.model_dump(mode="json") != expected_locator.model_dump(
            mode="json"
        ):
            raise DataQualityError(
                "Sol top-level locator does not match current frozen units"
            )
        frozen_visuals = [
            str(row["evidence_id"]) for row in scope["visuals"]
        ]
        if public.visual_evidence_refs != frozen_visuals:
            raise DataQualityError(
                "Sol visual evidence list does not match the frozen batch"
            )
        self._require_object(
            public.source_file_hash,
            f"source file {batch['source_id']}",
        )

    def _validate_hash_contract(
        self,
        public: DirectSolBatchOutput,
    ) -> None:
        expected = self._hash_contract(public.source_kind)
        actual = public.hash_contract.model_dump(mode="json")
        if actual != expected:
            raise DataQualityError(
                "Sol hash contract does not match the supported frozen-text contract"
            )

    @staticmethod
    def _hash_contract(source_kind: DirectSourceKind) -> dict[str, str]:
        return {
            "algorithm": "SHA-256",
            "normalization": _NORMALIZATION_CONTRACT,
            "batch_serialization": _SERIALIZATION_CONTRACT[source_kind],
            "source_locator_offsets": _OFFSET_CONTRACT[source_kind],
        }

    def _normalize_batch(
        self,
        run_id: str,
        public: DirectSolBatchOutput,
        scope: Mapping[str, Any],
    ) -> DirectNormalizedBatchOutput:
        batch = scope["batch"]
        current_fragments = [
            row
            for row in scope["fragments"]
            if row["context_role"] == "CURRENT"
        ]
        visual_by_id = {
            str(row["evidence_id"]): row for row in scope["visuals"]
        }
        candidates: list[DirectRawSkillCandidate] = []
        for ordinal, skill in enumerate(public.skills, start=1):
            source_refs: list[DirectCandidateSourceRef] = []
            candidate_visual_ids: list[str] = []
            for source_ref in skill.source_refs:
                parsed = parse_direct_source_locator(
                    DirectSourceKind(source_ref.source_kind),
                    source_ref.locator,
                )
                matches = [
                    row
                    for row in current_fragments
                    if row["source_kind"] == parsed.source_kind.value
                    and int(row["unit_index"]) == parsed.unit_index
                    and int(row["start_offset"]) <= parsed.start_offset
                    and int(row["end_offset"]) >= parsed.end_offset
                ]
                if len(matches) != 1:
                    raise DataQualityError(
                        "source locator must resolve to exactly one current "
                        f"frozen fragment: {source_ref.locator}"
                    )
                fragment = matches[0]
                text = self._validate_text_locator(
                    str(fragment["object_hash"]),
                    parsed.start_offset,
                    parsed.end_offset,
                    f"source ref {source_ref.locator}",
                )
                cited_text = text[parsed.start_offset : parsed.end_offset]
                slice_hash = sha256_bytes(cited_text.encode("utf-8"))
                if slice_hash != source_ref.source_object_hash:
                    raise DataQualityError(
                        f"source slice hash mismatch: {source_ref.locator}"
                    )
                if not cited_text.startswith(source_ref.paragraph_head):
                    raise DataQualityError(
                        f"paragraph_head does not match source slice: "
                        f"{source_ref.locator}"
                    )
                for evidence_id in source_ref.visual_evidence_ids:
                    if evidence_id not in visual_by_id:
                        raise DataQualityError(
                            "source ref uses visual evidence outside frozen "
                            f"batch: {evidence_id}"
                        )
                    if evidence_id not in candidate_visual_ids:
                        candidate_visual_ids.append(evidence_id)
                source_refs.append(
                    DirectCandidateSourceRef(
                        batch_id=public.batch_id,
                        source_id=str(batch["source_id"]),
                        source_file_hash=public.source_file_hash,
                        chapter_unit_id=str(batch["chapter_unit_id"]),
                        fragment_id=str(fragment["fragment_id"]),
                        fragment_object_hash=str(fragment["object_hash"]),
                        source_object_hash=source_ref.source_object_hash,
                        slice_hash=slice_hash,
                        locator=parsed,
                        original_locator=source_ref.locator,
                        paragraph_head=source_ref.paragraph_head,
                        visual_evidence_ids=source_ref.visual_evidence_ids,
                    )
                )
            visual_refs: list[DirectCandidateVisualRef] = []
            for evidence_id in candidate_visual_ids:
                row = visual_by_id[evidence_id]
                try:
                    locator = json.loads(str(row["evidence_locator_json"]))
                except json.JSONDecodeError as exc:
                    raise DataQualityError(
                        f"invalid frozen visual locator: {evidence_id}"
                    ) from exc
                if not isinstance(locator, (Mapping, list)):
                    raise DataQualityError(
                        f"frozen visual locator is not structured JSON: "
                        f"{evidence_id}"
                    )
                self._require_object(
                    str(row["object_hash"]),
                    f"visual evidence {evidence_id}",
                )
                visual_refs.append(
                    DirectCandidateVisualRef(
                        batch_id=public.batch_id,
                        evidence_id=evidence_id,
                        object_hash=str(row["object_hash"]),
                        source_kind=DirectSourceKind(str(row["source_kind"])),
                        unit_index=int(row["unit_index"]),
                        evidence_locator=(
                            dict(locator)
                            if isinstance(locator, Mapping)
                            else locator
                        ),
                    )
                )
            candidate_id = self._candidate_id(
                run_id,
                public.batch_id,
                ordinal,
                skill,
            )
            candidates.append(
                DirectRawSkillCandidate(
                    **_semantic_payload(skill),
                    candidate_id=candidate_id,
                    chapter_unit_id=str(batch["chapter_unit_id"]),
                    sol_version_id=public.sol_distillation_version,
                    source_refs=source_refs,
                    visual_refs=visual_refs,
                )
            )
        return DirectNormalizedBatchOutput(
            run_id=run_id,
            batch_id=public.batch_id,
            source_id=str(batch["source_id"]),
            chapter_unit_id=str(batch["chapter_unit_id"]),
            source_file_hash=public.source_file_hash,
            batch_text_object_hash=public.batch_text_object_hash,
            sol_version_id=public.sol_distillation_version,
            sol_version_hash=sha256_bytes(
                public.sol_distillation_version.encode("utf-8")
            ),
            skills=candidates,
            no_skill_reason=public.no_skill_reason,
            visual_evidence_ids=public.visual_evidence_refs,
        )

    @staticmethod
    def _candidate_id(
        run_id: str,
        batch_id: str,
        ordinal: int,
        skill: BaseModel,
    ) -> str:
        identity = {
            "run_id": run_id,
            "batch_id": batch_id,
            "skill_ordinal": ordinal,
            "skill": _canonical_payload(skill),
        }
        return "direct-candidate:" + sha256_bytes(
            canonical_json_bytes(identity)
        )

    @staticmethod
    def _batch_text_hash(
        source_kind: DirectSourceKind,
        fragments: Sequence[Mapping[str, Any]],
    ) -> str:
        label = "page" if source_kind is DirectSourceKind.PDF else "paragraph"
        serialized = "\n\f\n".join(
            f"{label}={fragment['locator']['unit_index']}\n{fragment['text']}"
            for fragment in fragments
        )
        return sha256_bytes(serialized.encode("utf-8"))

    def finalize_file(self, run_id: str, path: Path) -> dict[str, Any]:
        manifest = _validate_model(
            DirectDedupManifest,
            self.load_json_file(path),
        )
        if manifest.run_id != run_id:
            raise DataQualityError(
                "CLI run identity does not match the dedup manifest"
            )
        return self.finalize(manifest)

    def finalize(
        self,
        manifest: DirectDedupManifest | Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(manifest, DirectDedupManifest):
            manifest = _validate_model(DirectDedupManifest, manifest)
        if (
            sha256_bytes(manifest.sol_version.encode("utf-8"))
            != manifest.sol_version_hash
        ):
            raise DataQualityError("dedup manifest Sol version hash mismatch")
        manifest_hash = _canonical_hash(manifest)
        existing = self.repository.get_dedup_manifest(manifest.run_id)
        if existing is not None:
            if (
                existing["manifest_id"] != manifest.manifest_id
                or existing["manifest_hash"] != manifest_hash
            ):
                raise ValueError(
                    f"direct finalize collision: {manifest.run_id}"
                )
            if (
                sha256_bytes(str(existing["manifest_json"]).encode("utf-8"))
                != manifest_hash
                or existing["manifest_object_hash"] != manifest_hash
            ):
                raise DataQualityError(
                    "stored dedup manifest JSON/object hash mismatch"
                )
            self._require_object(
                str(existing["manifest_object_hash"]),
                f"dedup manifest {manifest.manifest_id}",
            )
            self.repository.record_finalize_replay(manifest.run_id)
            return {
                "status": "FINALIZED",
                "run_id": manifest.run_id,
                "manifest_id": manifest.manifest_id,
                "manifest_hash": manifest_hash,
                "idempotent_replay": True,
                "formal_committee_weight_allowed": False,
            }
        run = self.repository.get_run(manifest.run_id)
        if run is None:
            raise KeyError(f"unknown direct run: {manifest.run_id}")
        if run["stage"] != "BATCHES_IMPORTED":
            raise DataQualityError(
                "all frozen direct chapters must be imported before finalize"
            )
        stored = self.repository.candidate_payloads(manifest.run_id)
        candidates: dict[str, DirectRawSkillCandidate] = {}
        for candidate_id, record in stored.items():
            candidate = _validate_model(
                DirectRawSkillCandidate,
                record["payload"],
            )
            if _canonical_hash(candidate) != record["object_hash"]:
                raise DataQualityError(
                    f"raw candidate object hash mismatch: {candidate_id}"
                )
            self._require_object(
                str(record["object_hash"]),
                f"raw candidate {candidate_id}",
            )
            candidates[candidate_id] = candidate
        supplied = {
            candidate_id
            for final_skill in manifest.final_skills
            for candidate_id in final_skill.candidate_ids
        }
        if supplied != set(candidates):
            missing = sorted(set(candidates) - supplied)
            unknown = sorted(supplied - set(candidates))
            raise DataQualityError(
                "dedup contributions do not cover raw candidates; "
                f"missing={missing}, unknown={unknown}"
            )
        final_records: list[dict[str, Any]] = []
        for final_skill in manifest.final_skills:
            contributors = [
                candidates[candidate_id]
                for candidate_id in final_skill.candidate_ids
            ]
            self._validate_merge(final_skill, contributors)
            source_refs = self._merged_source_refs(contributors)
            visual_refs = self._merged_visual_refs(contributors)
            if (
                final_skill.status is DirectSkillStatus.READY_FOR_SHADOW
                and not source_refs
            ):
                raise DataQualityError(
                    "READY final skill has no merged source refs"
                )
            payload = final_skill.model_dump(mode="json") | {
                "source_refs": source_refs,
                "visual_refs": visual_refs,
                "formal_committee_weight_allowed": False,
            }
            object_ref = self.object_store.put_json(payload)
            final_records.append(
                {
                    "payload": final_skill.model_dump(mode="json"),
                    "source_refs": source_refs,
                    "visual_refs": visual_refs,
                    "object_hash": object_ref.sha256,
                    "json": canonical_json_bytes(payload).decode("utf-8"),
                }
            )
        manifest_ref = self.object_store.put_json(
            _canonical_payload(manifest)
        )
        if manifest_ref.sha256 != manifest_hash:
            raise DataQualityError("dedup manifest hash is not canonical")
        all_skill_ids = [
            item.final_skill_id for item in manifest.final_skills
        ]
        shadow_skill_ids = [
            item.final_skill_id
            for item in manifest.final_skills
            if item.status is DirectSkillStatus.READY_FOR_SHADOW
        ]
        bundle_identity = {
            "run_id": manifest.run_id,
            "manifest_id": manifest.manifest_id,
            "all_skill_ids": all_skill_ids,
            "shadow_skill_ids": shadow_skill_ids,
        }
        bundle_id = "direct-shadow-bundle:" + sha256_bytes(
            canonical_json_bytes(bundle_identity)
        )
        bundle = {
            "schema_version": "direct-source-shadow-bundle-v1",
            "bundle_id": bundle_id,
            **bundle_identity,
            "formal_committee_weight_allowed": False,
        }
        bundle_ref = self.object_store.put_json(bundle)
        self.repository.finalize(
            manifest,
            manifest_hash=manifest_hash,
            manifest_object_hash=manifest_ref.sha256,
            final_records=final_records,
            bundle=bundle,
            bundle_object_hash=bundle_ref.sha256,
        )
        return {
            "status": "FINALIZED",
            "run_id": manifest.run_id,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest_hash,
            "final_skill_count": len(all_skill_ids),
            "shadow_skill_count": len(shadow_skill_ids),
            "non_ready_skill_count": len(all_skill_ids)
            - len(shadow_skill_ids),
            "bundle_id": bundle_id,
            "bundle_object_hash": bundle_ref.sha256,
            "idempotent_replay": False,
            "formal_committee_weight_allowed": False,
        }

    @staticmethod
    def _validate_merge(
        final_skill: DirectFinalSkillDraft,
        contributors: Sequence[DirectRawSkillCandidate],
    ) -> None:
        final_semantics = _semantic_payload(final_skill)
        contributor_semantics = [
            _semantic_payload(candidate) for candidate in contributors
        ]
        if len(contributors) == 1:
            if final_semantics != contributor_semantics[0]:
                raise DataQualityError(
                    "single-candidate final skill must preserve every "
                    "user semantic field exactly"
                )
            return
        modules = {
            candidate.primary_module.value for candidate in contributors
        }
        for candidate in contributors:
            modules.update(
                module.value for module in candidate.secondary_modules
            )
        final_primary = str(final_semantics["primary_module"])
        final_secondaries = {
            str(module) for module in final_semantics["secondary_modules"]
        }
        if final_primary not in modules or final_secondaries != (
            modules - {final_primary}
        ):
            raise DataQualityError(
                "merged final module roles must preserve the contributor "
                "module union"
            )
        if (
            any(
                candidate.status is DirectSkillStatus.NEEDS_USER_REVIEW
                for candidate in contributors
            )
            and final_skill.status is DirectSkillStatus.READY_FOR_SHADOW
        ):
            raise DataQualityError(
                "READY final skill cannot absorb NEEDS_USER_REVIEW evidence"
            )
        for scalar in (
            "skill_name",
            "decision_question",
            "core_principle",
            "confidence",
        ):
            if final_semantics[scalar] not in {
                item[scalar] for item in contributor_semantics
            }:
                raise DataQualityError(
                    f"merged final {scalar} is not preserved from a contributor"
                )
        for field in _LIST_SEMANTIC_FIELDS:
            contributor_items = {
                item
                for semantic in contributor_semantics
                for item in semantic[field]
            }
            if not contributor_items.issubset(set(final_semantics[field])):
                raise DataQualityError(
                    f"merged final {field} drops contributor semantics"
                )

    @staticmethod
    def _merged_source_refs(
        contributors: Sequence[DirectRawSkillCandidate],
    ) -> list[dict[str, Any]]:
        merged: dict[tuple[object, ...], dict[str, Any]] = {}
        for candidate in contributors:
            for source_ref in candidate.source_refs:
                payload = source_ref.model_dump(mode="json")
                merged.setdefault(_source_ref_key(payload), payload)
        return list(merged.values())

    @staticmethod
    def _merged_visual_refs(
        contributors: Sequence[DirectRawSkillCandidate],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for candidate in contributors:
            for visual_ref in candidate.visual_refs:
                payload = visual_ref.model_dump(mode="json")
                merged.setdefault(visual_ref.evidence_id, payload)
        return list(merged.values())

    def status(self, run_id: str) -> dict[str, Any]:
        stats = self.repository.statistics(run_id)
        module_counts = {
            module.value: int(
                stats["module_counts"].get(module.value, 0)
            )
            for module in DirectSkillModule
        }
        raw_module_counts = {
            module.value: int(
                stats["raw_module_counts"].get(module.value, 0)
            )
            for module in DirectSkillModule
        }
        return {
            "status": stats["stage"],
            **stats,
            "module_counts": (
                module_counts
                if stats["stage"] == "FINALIZED"
                else raw_module_counts
            ),
            "final_module_counts": module_counts,
            "raw_module_counts": raw_module_counts,
            "formal_committee_weight_allowed": False,
        }

    def shadow_context(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown direct run: {run_id}")
        if run["stage"] != "FINALIZED":
            raise DataQualityError(
                "direct shadow context requires a FINALIZED database run"
            )
        bundle_row = self.repository.shadow_bundle(run_id)
        if bundle_row is None:
            raise DataQualityError("direct shadow bundle is missing")
        try:
            all_ids_column = json.loads(bundle_row["all_skill_ids_json"])
            shadow_ids_column = json.loads(
                bundle_row["shadow_skill_ids_json"]
            )
            bundle_json = json.loads(bundle_row["bundle_json"])
        except json.JSONDecodeError as exc:
            raise DataQualityError(
                "direct shadow bundle JSON is invalid"
            ) from exc
        if (
            bundle_json.get("all_skill_ids") != all_ids_column
            or bundle_json.get("shadow_skill_ids") != shadow_ids_column
            or bundle_json.get("bundle_id") != bundle_row["bundle_id"]
            or bundle_json.get("formal_committee_weight_allowed") is not False
        ):
            raise DataQualityError(
                "direct shadow bundle JSON/column membership mismatch"
            )
        if (
            sha256_bytes(str(bundle_row["bundle_json"]).encode("utf-8"))
            != bundle_row["bundle_object_hash"]
            or not self.object_store.verify(
                str(bundle_row["bundle_object_hash"])
            )
        ):
            raise DataQualityError(
                "direct shadow bundle JSON/object hash mismatch"
            )
        final_rows = self.repository.final_rows(run_id)
        by_id = {
            str(row["final_skill_id"]): row for row in final_rows
        }
        if set(all_ids_column) != set(by_id):
            raise DataQualityError(
                "direct shadow all_skill_ids do not match final database rows"
            )
        skills: list[dict[str, Any]] = []
        for skill_id in shadow_ids_column:
            row = by_id.get(skill_id)
            if (
                row is None
                or row["status"]
                != DirectSkillStatus.READY_FOR_SHADOW.value
            ):
                raise DataQualityError(
                    "direct shadow membership contains a non-ready DB skill"
                )
            try:
                payload = json.loads(row["skill_json"])
            except json.JSONDecodeError as exc:
                raise DataQualityError(
                    f"invalid final skill JSON: {skill_id}"
                ) from exc
            if (
                payload.get("status")
                != DirectSkillStatus.READY_FOR_SHADOW.value
                or payload.get("formal_committee_weight_allowed") is not False
            ):
                raise DataQualityError(
                    "direct shadow membership contains an unsafe JSON skill"
                )
            if (
                sha256_bytes(str(row["skill_json"]).encode("utf-8"))
                != row["skill_object_hash"]
                or not self.object_store.verify(
                    str(row["skill_object_hash"])
                )
            ):
                raise DataQualityError(
                    "direct shadow skill JSON/object hash mismatch"
                )
            skills.append(payload)
        return {
            "status": "READY",
            "run_id": run_id,
            "bundle_id": bundle_row["bundle_id"],
            "all_skill_ids": all_ids_column,
            "shadow_skill_ids": shadow_ids_column,
            "skills": skills,
            "non_ready_count": len(all_ids_column) - len(shadow_ids_column),
            "formal_committee_weight_allowed": False,
        }

    def audit(self, run_id: str) -> dict[str, Any]:
        findings: list[str] = []
        try:
            stats = self.status(run_id)
        except KeyError:
            return {
                "status": "FAIL",
                "run_id": run_id,
                "findings": ["RUN_NOT_FOUND"],
                "integrity_check": self.state.integrity_check(),
                "foreign_key_check": 0,
            }
        object_rows: list[tuple[str, str]] = []
        slice_rows: list[Mapping[str, Any]] = []
        with self.state.connect() as connection:
            integrity_row = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            lineage_queries = {
                "CANDIDATE_SOURCE_LINEAGE_MISMATCH": (
                    "SELECT COUNT(*) "
                    "FROM knowledge_direct_candidate_source_ref r "
                    "JOIN knowledge_direct_chapter_fragment f "
                    "ON f.run_id=r.run_id AND f.batch_id=r.batch_id "
                    "AND f.fragment_id=r.fragment_id "
                    "JOIN knowledge_direct_chapter_batch b "
                    "ON b.run_id=r.run_id AND b.batch_id=r.batch_id "
                    "WHERE r.run_id=? AND ("
                    "r.fragment_object_hash<>f.object_hash "
                    "OR r.source_kind<>f.source_kind "
                    "OR r.unit_index<>f.unit_index "
                    "OR r.start_offset<f.start_offset "
                    "OR r.end_offset>f.end_offset "
                    "OR r.source_object_hash<>r.slice_hash "
                    "OR r.chapter_unit_id<>b.chapter_unit_id "
                    "OR r.source_id<>b.source_id)"
                ),
                "CANDIDATE_VISUAL_LINEAGE_MISMATCH": (
                    "SELECT COUNT(*) "
                    "FROM knowledge_direct_candidate_visual_ref r "
                    "JOIN knowledge_direct_chapter_visual_ref v "
                    "ON v.run_id=r.run_id AND v.batch_id=r.batch_id "
                    "AND v.evidence_id=r.evidence_id "
                    "WHERE r.run_id=? AND ("
                    "r.object_hash<>v.object_hash "
                    "OR r.source_kind<>v.source_kind "
                    "OR r.unit_index<>v.unit_index "
                    "OR r.evidence_locator_json<>v.evidence_locator_json)"
                ),
                "FINAL_SOURCE_LINEAGE_MISMATCH": (
                    "SELECT COUNT(*) "
                    "FROM knowledge_direct_final_source_ref r "
                    "JOIN knowledge_direct_chapter_fragment f "
                    "ON f.run_id=r.run_id AND f.batch_id=r.batch_id "
                    "AND f.fragment_id=r.fragment_id "
                    "JOIN knowledge_direct_chapter_batch b "
                    "ON b.run_id=r.run_id AND b.batch_id=r.batch_id "
                    "WHERE r.run_id=? AND ("
                    "r.fragment_object_hash<>f.object_hash "
                    "OR r.source_kind<>f.source_kind "
                    "OR r.unit_index<>f.unit_index "
                    "OR r.start_offset<f.start_offset "
                    "OR r.end_offset>f.end_offset "
                    "OR r.source_object_hash<>r.slice_hash "
                    "OR r.chapter_unit_id<>b.chapter_unit_id "
                    "OR r.source_id<>b.source_id)"
                ),
                "FINAL_VISUAL_LINEAGE_MISMATCH": (
                    "SELECT COUNT(*) "
                    "FROM knowledge_direct_final_visual_ref r "
                    "JOIN knowledge_direct_chapter_visual_ref v "
                    "ON v.run_id=r.run_id AND v.batch_id=r.batch_id "
                    "AND v.evidence_id=r.evidence_id "
                    "WHERE r.run_id=? AND ("
                    "r.object_hash<>v.object_hash "
                    "OR r.source_kind<>v.source_kind "
                    "OR r.unit_index<>v.unit_index "
                    "OR r.evidence_locator_json<>v.evidence_locator_json)"
                ),
            }
            for finding_code, query in lineage_queries.items():
                if connection.execute(query, (run_id,)).fetchone()[0]:
                    findings.append(finding_code)
            run_row = connection.execute(
                "SELECT manifest_object_hash,manifest_json "
                "FROM knowledge_direct_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run_row is not None:
                object_rows.append(
                    ("RUN_MANIFEST", run_row["manifest_object_hash"])
                )
                if (
                    sha256_bytes(
                        str(run_row["manifest_json"]).encode("utf-8")
                    )
                    != run_row["manifest_object_hash"]
                ):
                    findings.append("RUN_MANIFEST_OBJECT_HASH_MISMATCH")
            for row in connection.execute(
                "SELECT source_id,source_file_hash "
                "FROM knowledge_direct_source WHERE run_id=?",
                (run_id,),
            ):
                object_rows.append(
                    (f"SOURCE:{row['source_id']}", row["source_file_hash"])
                )
            for row in connection.execute(
                "SELECT fragment_id,object_hash "
                "FROM knowledge_direct_chapter_fragment WHERE run_id=?",
                (run_id,),
            ):
                object_rows.append(
                    (f"FRAGMENT:{row['fragment_id']}", row["object_hash"])
                )
            for row in connection.execute(
                "SELECT evidence_id,object_hash "
                "FROM knowledge_direct_chapter_visual_ref WHERE run_id=?",
                (run_id,),
            ):
                object_rows.append(
                    (f"VISUAL:{row['evidence_id']}", row["object_hash"])
                )
            slice_rows.extend(
                dict(row)
                for row in connection.execute(
                    "SELECT candidate_id,fragment_object_hash,"
                    "source_object_hash,start_offset,end_offset "
                    "FROM knowledge_direct_candidate_source_ref "
                    "WHERE run_id=?",
                    (run_id,),
                )
            )
            self._audit_batches(connection, run_id, findings, object_rows)
            self._audit_candidates(
                connection,
                run_id,
                findings,
                object_rows,
            )
            self._audit_final(
                connection,
                run_id,
                findings,
                object_rows,
            )
        for row in slice_rows:
            label = f"CANDIDATE_SLICE:{row['candidate_id']}"
            try:
                text = self._load_text(
                    str(row["fragment_object_hash"]),
                    label,
                )
                start = int(row["start_offset"])
                end = int(row["end_offset"])
                actual = sha256_bytes(text[start:end].encode("utf-8"))
                if actual != row["source_object_hash"]:
                    findings.append(f"SOURCE_SLICE_HASH_MISMATCH:{label}")
            except DataQualityError:
                findings.append(f"SOURCE_SLICE_INVALID:{label}")
        integrity = (
            str(integrity_row[0])
            if integrity_row is not None
            else "unknown"
        )
        if integrity != "ok":
            findings.append("SQLITE_INTEGRITY_FAILED")
        if foreign_keys:
            findings.append("SQLITE_FOREIGN_KEY_FAILED")
        for label, object_hash in object_rows:
            if not self.object_store.verify(str(object_hash)):
                findings.append(f"OBJECT_MISSING_OR_CORRUPT:{label}")
        if stats["stage"] == "FINALIZED":
            try:
                self.shadow_context(run_id)
            except (DataQualityError, KeyError):
                findings.append("SHADOW_CONTEXT_INVALID")
        return {
            **stats,
            "status": "PASS" if not findings else "FAIL",
            "findings": sorted(set(findings)),
            "integrity_check": integrity,
            "foreign_key_check": len(foreign_keys),
            "audited_object_count": len(object_rows),
            "network_used": False,
            "external_model_called": False,
            "embedding_executed": False,
            "reviewed_argument_units_used": False,
            "formal_committee_weight_allowed": False,
        }

    def _audit_batches(
        self,
        connection: Any,
        run_id: str,
        findings: list[str],
        object_rows: list[tuple[str, str]],
    ) -> None:
        for row in connection.execute(
            "SELECT batch_id,packet_hash,packet_object_hash,"
            "import_input_hash,import_object_hash "
            "FROM knowledge_direct_chapter_batch WHERE run_id=?",
            (run_id,),
        ):
            if row["packet_object_hash"]:
                object_rows.append(
                    (f"PACKET:{row['batch_id']}", row["packet_object_hash"])
                )
                try:
                    packet = _strict_json_bytes(
                        self.object_store.get_bytes(
                            str(row["packet_object_hash"])
                        )
                    )
                    if not isinstance(packet, Mapping):
                        raise DataQualityError(
                            "packet object is not a JSON object"
                        )
                    packet_core = dict(packet)
                    stored_packet_hash = packet_core.pop(
                        "packet_hash",
                        None,
                    )
                    if (
                        stored_packet_hash != row["packet_hash"]
                        or sha256_bytes(canonical_json_bytes(packet_core))
                        != row["packet_hash"]
                    ):
                        findings.append(
                            f"PACKET_HASH_MISMATCH:{row['batch_id']}"
                        )
                except (DataQualityError, StorageError):
                    findings.append(f"PACKET_INVALID:{row['batch_id']}")
            if row["import_object_hash"]:
                object_rows.append(
                    (f"IMPORT:{row['batch_id']}", row["import_object_hash"])
                )
                if row["import_input_hash"] != row["import_object_hash"]:
                    findings.append(
                        f"IMPORT_HASH_MISMATCH:{row['batch_id']}"
                    )
                try:
                    public = _validate_model(
                        DirectSolBatchOutput,
                        _strict_json_bytes(
                            self.object_store.get_bytes(
                                str(row["import_object_hash"])
                            )
                        ),
                    )
                    if public.batch_id != row["batch_id"]:
                        findings.append(
                            f"IMPORT_BATCH_MISMATCH:{row['batch_id']}"
                        )
                except (DataQualityError, StorageError):
                    findings.append(f"IMPORT_INVALID:{row['batch_id']}")

    def _audit_candidates(
        self,
        connection: Any,
        run_id: str,
        findings: list[str],
        object_rows: list[tuple[str, str]],
    ) -> None:
        for row in connection.execute(
            "SELECT candidate_id,candidate_object_hash,candidate_json,status,"
            "source_ref_count,visual_ref_count "
            "FROM knowledge_direct_raw_sol_candidate WHERE run_id=?",
            (run_id,),
        ):
            candidate_id = str(row["candidate_id"])
            object_rows.append(
                (f"CANDIDATE:{candidate_id}", row["candidate_object_hash"])
            )
            if (
                sha256_bytes(str(row["candidate_json"]).encode("utf-8"))
                != row["candidate_object_hash"]
            ):
                findings.append(
                    f"CANDIDATE_OBJECT_HASH_MISMATCH:{candidate_id}"
                )
            try:
                candidate = _validate_model(
                    DirectRawSkillCandidate,
                    _strict_json_bytes(
                        str(row["candidate_json"]).encode("utf-8")
                    ),
                )
            except DataQualityError:
                findings.append(f"CANDIDATE_JSON_INVALID:{candidate_id}")
                continue
            if candidate.status.value != row["status"]:
                findings.append(
                    f"CANDIDATE_STATUS_MISMATCH:{candidate_id}"
                )
            source_count = connection.execute(
                "SELECT COUNT(*) "
                "FROM knowledge_direct_candidate_source_ref "
                "WHERE run_id=? AND candidate_id=?",
                (run_id, candidate_id),
            ).fetchone()[0]
            visual_count = connection.execute(
                "SELECT COUNT(*) "
                "FROM knowledge_direct_candidate_visual_ref "
                "WHERE run_id=? AND candidate_id=?",
                (run_id, candidate_id),
            ).fetchone()[0]
            if source_count != row["source_ref_count"]:
                findings.append(
                    f"CANDIDATE_SOURCE_COUNT_MISMATCH:{candidate_id}"
                )
            if visual_count != row["visual_ref_count"]:
                findings.append(
                    f"CANDIDATE_VISUAL_COUNT_MISMATCH:{candidate_id}"
                )

    def _audit_final(
        self,
        connection: Any,
        run_id: str,
        findings: list[str],
        object_rows: list[tuple[str, str]],
    ) -> None:
        manifest = connection.execute(
            "SELECT manifest_id,manifest_hash,manifest_object_hash,"
            "manifest_json "
            "FROM knowledge_direct_sol_confirmed_dedup_manifest "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if manifest is not None:
            object_rows.append(
                (
                    f"DEDUP:{manifest['manifest_id']}",
                    manifest["manifest_object_hash"],
                )
            )
            if (
                sha256_bytes(
                    str(manifest["manifest_json"]).encode("utf-8")
                )
                != manifest["manifest_hash"]
                or manifest["manifest_hash"]
                != manifest["manifest_object_hash"]
            ):
                findings.append("DEDUP_MANIFEST_OBJECT_HASH_MISMATCH")
        for row in connection.execute(
            "SELECT final_skill_id,status,skill_object_hash,skill_json,"
            "source_ref_count,visual_ref_count "
            "FROM knowledge_direct_final_skill WHERE run_id=?",
            (run_id,),
        ):
            skill_id = str(row["final_skill_id"])
            object_rows.append(
                (f"FINAL:{skill_id}", row["skill_object_hash"])
            )
            if (
                sha256_bytes(str(row["skill_json"]).encode("utf-8"))
                != row["skill_object_hash"]
            ):
                findings.append(f"FINAL_OBJECT_HASH_MISMATCH:{skill_id}")
            try:
                payload = _strict_json_bytes(
                    str(row["skill_json"]).encode("utf-8")
                )
            except DataQualityError:
                findings.append(f"FINAL_JSON_INVALID:{skill_id}")
                continue
            if (
                not isinstance(payload, Mapping)
                or payload.get("status") != row["status"]
            ):
                findings.append(f"FINAL_STATUS_MISMATCH:{skill_id}")
            source_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_final_source_ref "
                "WHERE run_id=? AND final_skill_id=?",
                (run_id, skill_id),
            ).fetchone()[0]
            visual_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_direct_final_visual_ref "
                "WHERE run_id=? AND final_skill_id=?",
                (run_id, skill_id),
            ).fetchone()[0]
            if source_count != row["source_ref_count"]:
                findings.append(f"FINAL_SOURCE_COUNT_MISMATCH:{skill_id}")
            if visual_count != row["visual_ref_count"]:
                findings.append(f"FINAL_VISUAL_COUNT_MISMATCH:{skill_id}")
        bundle = connection.execute(
            "SELECT bundle_id,bundle_object_hash,bundle_json "
            "FROM knowledge_direct_shadow_bundle WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if bundle is not None:
            object_rows.append(
                (
                    f"BUNDLE:{bundle['bundle_id']}",
                    bundle["bundle_object_hash"],
                )
            )
            if (
                sha256_bytes(str(bundle["bundle_json"]).encode("utf-8"))
                != bundle["bundle_object_hash"]
            ):
                findings.append("SHADOW_BUNDLE_OBJECT_HASH_MISMATCH")

    def _require_object(self, object_hash: str, label: str) -> bytes:
        try:
            return self.object_store.get_bytes(object_hash)
        except StorageError as exc:
            raise DataQualityError(
                f"missing or corrupt immutable {label}"
            ) from exc

    def _load_text(self, object_hash: str, label: str) -> str:
        raw = self._require_object(object_hash, label)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DataQualityError(f"{label} is not UTF-8 text") from exc

    def _validate_text_locator(
        self,
        object_hash: str,
        start_offset: int,
        end_offset: int,
        label: str,
    ) -> str:
        text = self._load_text(object_hash, label)
        if (
            start_offset < 0
            or end_offset > len(text)
            or end_offset <= start_offset
        ):
            raise DataQualityError(
                f"{label} locator is outside the immutable text object"
            )
        return text


__all__ = ["DirectSourceDistillationService"]
