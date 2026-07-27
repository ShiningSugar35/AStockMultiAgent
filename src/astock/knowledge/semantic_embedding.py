"""Local-only auxiliary Paragraph views and complete-ArgumentUnit decisions."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from astock.core.atomic import atomic_write_text
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.knowledge.semantic_funnel import local_context_paragraph_ids
from astock.knowledge.semantic_repository import SemanticFunnelRepository
from astock.knowledge.semantic_storage import (
    ParquetSemanticStore,
    SemanticArgumentLineage,
    SemanticParquetWrite,
    SemanticVectorRecord,
)
from astock.schemas import (
    ArgumentUnit,
    ArgumentUnitStatus,
    BookMethodCategory,
    EmbeddingModelManifest,
    LocalEmbeddingAssetManifest,
    ParagraphUnit,
    SemanticArgumentScore,
    SemanticEmbeddingContract,
    SemanticEmbeddingView,
    SemanticFunnelConfig,
    SemanticRunStage,
    SemanticScreenDecision,
)

MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
MODEL_REPOSITORY_URL = "https://huggingface.co/BAAI/bge-small-zh-v1.5"
MODEL_LICENSE = "MIT"
MODEL_DIMENSION = 512
MODEL_MAXIMUM_TOKENS = 512
_MANIFEST_FILE = "astock-model-manifest.json"


@dataclass(frozen=True, slots=True)
class EncodedText:
    vector: tuple[float, ...]
    token_count: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class SemanticEmbeddingExecution:
    manifest: EmbeddingModelManifest
    parquet: SemanticParquetWrite
    vector_count: int
    score_count: int
    keep_count: int
    review_count: int
    calibration_required_count: int


class EmbeddingBackend(Protocol):
    dimension: int

    def encode(self, texts: list[str]) -> list[EncodedText]: ...


class RecordedEmbeddingBackend:
    """Test-only backend that refuses every unrecorded input."""

    def __init__(self, vectors_by_text: dict[str, tuple[float, ...]]) -> None:
        if not vectors_by_text:
            raise ValueError("recorded embedding backend requires fixed vectors")
        dimensions = {len(vector) for vector in vectors_by_text.values()}
        if len(dimensions) != 1:
            raise ValueError("recorded embedding vectors must share one dimension")
        self.dimension = next(iter(dimensions))
        self.vectors_by_text = {
            text: _normalized(vector) for text, vector in vectors_by_text.items()
        }

    def encode(self, texts: list[str]) -> list[EncodedText]:
        missing = [text for text in texts if text not in self.vectors_by_text]
        if missing:
            raise ValueError("recorded embedding input was not explicitly fixed")
        return [
            EncodedText(
                vector=self.vectors_by_text[text],
                token_count=max(1, len(text)),
                chunk_count=1,
            )
            for text in texts
        ]


class SentenceTransformerBackend:
    """CPU backend that windows every overlength input without silent truncation."""

    def __init__(
        self,
        model_directory: Path,
        *,
        batch_size: int = 16,
    ) -> None:
        manifest = verify_local_model(model_directory)
        module = importlib.import_module("sentence_transformers")
        model_class = module.SentenceTransformer
        self.model = model_class(
            str(model_directory),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
        dimension = self.model.get_embedding_dimension()
        if int(dimension) != manifest.dimension:
            raise ValueError("local embedding model dimension does not match its manifest")
        self.dimension = int(dimension)
        self.batch_size = batch_size
        self.maximum_content_tokens = min(
            int(self.model.max_seq_length) - 2,
            manifest.maximum_model_tokens - 2,
        )
        if self.maximum_content_tokens < 1:
            raise ValueError("embedding model token window is invalid")

    def encode(self, texts: list[str]) -> list[EncodedText]:
        chunk_texts: list[str] = []
        chunk_weights: list[int] = []
        chunk_ranges: list[tuple[int, int]] = []
        token_counts: list[int] = []
        for text in texts:
            token_ids = list(
                self.model.tokenizer.encode(
                    text,
                    add_special_tokens=False,
                    truncation=False,
                    verbose=False,
                )
            )
            if not token_ids:
                token_ids = list(
                    self.model.tokenizer.encode(
                        " ",
                        add_special_tokens=False,
                        truncation=False,
                        verbose=False,
                    )
                )
            start = len(chunk_texts)
            for offset in range(0, len(token_ids), self.maximum_content_tokens):
                chunk = token_ids[offset : offset + self.maximum_content_tokens]
                chunk_texts.append(
                    str(
                        self.model.tokenizer.decode(
                            chunk,
                            skip_special_tokens=True,
                            clean_up_tokenization_spaces=False,
                        )
                    )
                )
                chunk_weights.append(len(chunk))
            chunk_ranges.append((start, len(chunk_texts)))
            token_counts.append(len(token_ids))
        if not chunk_texts:
            return []
        encoded = np.asarray(
            self.model.encode(
                chunk_texts,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            ),
            dtype=np.float32,
        )
        results: list[EncodedText] = []
        for token_count, (start, end) in zip(token_counts, chunk_ranges, strict=True):
            weights = np.asarray(chunk_weights[start:end], dtype=np.float32)
            vector = np.average(encoded[start:end], axis=0, weights=weights)
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            results.append(
                EncodedText(
                    vector=tuple(float(value) for value in vector),
                    token_count=token_count,
                    chunk_count=end - start,
                )
            )
        return results


class SemanticEmbeddingService:
    def __init__(
        self,
        repository: SemanticFunnelRepository,
        object_store: ObjectStore,
        parquet_store: ParquetSemanticStore,
        config: SemanticFunnelConfig,
        backend: EmbeddingBackend,
        asset_manifest: LocalEmbeddingAssetManifest,
    ) -> None:
        if backend.dimension != asset_manifest.dimension:
            raise ValueError("embedding backend and asset manifest dimensions differ")
        self.repository = repository
        self.object_store = object_store
        self.parquet_store = parquet_store
        self.config = config
        self.backend = backend
        self.asset_manifest = asset_manifest

    def run(self, run_id: str) -> SemanticEmbeddingExecution:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.stage not in {
            SemanticRunStage.ARGUMENT_UNITS_BUILT,
            SemanticRunStage.EMBEDDING_READY,
            SemanticRunStage.EMBEDDING_SCREENED,
        }:
            raise ValueError("semantic run is not ready for embedding")
        active_contract = self.config.embedding_contract_version
        if run.embedding_contract_version is not active_contract:
            raise ValueError("semantic embedding run and configuration contracts diverge")
        if active_contract is not SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3:
            raise ValueError("legacy semantic embedding contracts are read-only")
        anchor_payload = {
            category.value: values
            for category, values in self.config.method_anchors.items()
        }
        anchor_object = self.object_store.put_json(anchor_payload)
        threshold_payload = self.config.semantic_screen
        threshold_object = self.object_store.put_json(threshold_payload)
        manifest_identity = {
            "run_id": run_id,
            "model_id": self.asset_manifest.model_id,
            "model_revision": self.asset_manifest.model_revision,
            "model_asset_sha256": self.asset_manifest.bundle_sha256,
            "anchor_config_sha256": anchor_object.sha256,
            "threshold_config_sha256": threshold_object.sha256,
            "embedding_contract_version": (
                active_contract.value
            ),
            "embedding_views": [
                SemanticEmbeddingView.PARAGRAPH_CURRENT.value,
                SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT.value,
                SemanticEmbeddingView.ARGUMENT_UNIT.value,
                SemanticEmbeddingView.METHOD_PROTOTYPE.value,
            ],
            "auxiliary_views": [
                SemanticEmbeddingView.PARAGRAPH_CURRENT.value,
                SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT.value,
            ],
            "decision_view": SemanticEmbeddingView.ARGUMENT_UNIT.value,
            "method_prototype_count": len(BookMethodCategory),
        }
        manifest = EmbeddingModelManifest(
            schema_version="3.0",
            manifest_id=f"semantic-embedding:{content_hash(manifest_identity)}",
            model_id=self.asset_manifest.model_id,
            model_revision=self.asset_manifest.model_revision,
            model_asset_sha256=self.asset_manifest.bundle_sha256,
            tokenizer_asset_sha256=_tokenizer_hash(self.asset_manifest),
            dimension=self.asset_manifest.dimension,
            normalized=True,
            local_only=True,
            embedding_contract_version=active_contract,
            embedding_views=[
                SemanticEmbeddingView.PARAGRAPH_CURRENT,
                SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
                SemanticEmbeddingView.ARGUMENT_UNIT,
                SemanticEmbeddingView.METHOD_PROTOTYPE,
            ],
            auxiliary_views=[
                SemanticEmbeddingView.PARAGRAPH_CURRENT,
                SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
            ],
            decision_view=SemanticEmbeddingView.ARGUMENT_UNIT,
            method_prototype_count=len(BookMethodCategory),
            anchor_config_sha256=anchor_object.sha256,
            threshold_config_sha256=threshold_object.sha256,
            calibration_manifest_sha256=None,
            created_at=run.started_at,
        )
        candidate_paragraph_groups = self.repository.paragraph_groups(
            run_id,
            candidate_only=True,
        )
        arguments = [
            argument
            for argument in self.repository.argument_units(run_id)
            if argument.status is not ArgumentUnitStatus.DERIVED_EXCLUDED
        ]
        paragraph_groups = _paragraph_groups_for_retained_arguments(
            self.repository,
            run_id,
            candidate_paragraph_groups,
            arguments,
        )
        argument_lineages = _argument_lineages(arguments, paragraph_groups)
        vectors: list[SemanticVectorRecord] = []
        prototype_vectors = self._prototype_vectors(run_id, manifest, vectors)
        paragraph_item = {
            paragraph.paragraph_id: item_id
            for item_id, paragraphs in paragraph_groups.items()
            for paragraph in paragraphs
        }
        argument_texts = [self._text(argument.text_object_sha256) for argument in arguments]
        argument_vectors = self._encode_in_blocks(argument_texts)
        scores: list[SemanticArgumentScore] = []
        for paragraphs in paragraph_groups.values():
            paragraph_lookup = {
                paragraph.paragraph_id: paragraph for paragraph in paragraphs
            }
            for paragraph in paragraphs:
                paragraph_text = self._text(paragraph.text_object_sha256)
                vectors.append(
                    _vector_record(
                        run_id=run_id,
                        manifest_id=manifest.manifest_id,
                        view=SemanticEmbeddingView.PARAGRAPH_CURRENT,
                        entity_id=paragraph.paragraph_id,
                        item_id=paragraph_item[paragraph.paragraph_id],
                        content_id=paragraph.content_id,
                        source_snapshot_ids=(paragraph.locator.source_snapshot_id,),
                        source_object_sha256s=(paragraph.locator.source_object_sha256,),
                        input_object_sha256=paragraph.text_object_sha256,
                        encoded=self._encode_in_blocks([paragraph_text])[0],
                    )
                )
                context_ids = local_context_paragraph_ids(
                    paragraphs,
                    paragraph.ordinal,
                )
                context_paragraphs = [
                    paragraph_lookup[paragraph_id] for paragraph_id in context_ids
                ]
                context_text = "\n".join(
                    f"[paragraph ordinal={context_paragraph.ordinal}] "
                    f"{self._text(context_paragraph.text_object_sha256)}"
                    for context_paragraph in context_paragraphs
                )
                context_object = self.object_store.put_bytes(
                    context_text.encode("utf-8")
                )
                vectors.append(
                    _vector_record(
                        run_id=run_id,
                        manifest_id=manifest.manifest_id,
                        view=SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
                        entity_id=paragraph.paragraph_id,
                        item_id=paragraph_item[paragraph.paragraph_id],
                        content_id=paragraph.content_id,
                        source_snapshot_ids=(paragraph.locator.source_snapshot_id,),
                        source_object_sha256s=(paragraph.locator.source_object_sha256,),
                        input_object_sha256=context_object.sha256,
                        encoded=self._encode_in_blocks([context_text])[0],
                    )
                )
        for argument, encoded in zip(arguments, argument_vectors, strict=True):
            lineage = argument_lineages[argument.argument_unit_id]
            vectors.append(
                _vector_record(
                    run_id=run_id,
                    manifest_id=manifest.manifest_id,
                    view=SemanticEmbeddingView.ARGUMENT_UNIT,
                    entity_id=argument.argument_unit_id,
                    item_id=lineage.item_id,
                    content_id=lineage.content_id,
                    source_snapshot_ids=lineage.source_snapshot_ids,
                    source_object_sha256s=lineage.source_object_sha256s,
                    input_object_sha256=argument.text_object_sha256,
                    encoded=encoded,
                )
            )
            scores.append(self._score_argument(argument, encoded, manifest, prototype_vectors))
        parquet = self.parquet_store.write(
            author_source_id=run.author_source_id,
            run_id=run_id,
            embedding_manifest_id=manifest.manifest_id,
            vectors=vectors,
            scores=scores,
            manifest=manifest,
            candidate_paragraph_ids=set(paragraph_item),
            argument_unit_ids={
                argument.argument_unit_id for argument in arguments
            },
            argument_lineages=argument_lineages,
        )
        manifest_object = self.object_store.put_json(manifest.model_dump(mode="json"))
        self.repository.register_embedding(
            run,
            manifest,
            vector_parquet_hash=parquet.vectors_sha256,
            score_parquet_hash=parquet.scores_sha256,
            manifest_object_hash=manifest_object.sha256,
        )
        return SemanticEmbeddingExecution(
            manifest=manifest,
            parquet=parquet,
            vector_count=len(vectors),
            score_count=len(scores),
            keep_count=sum(score.decision is SemanticScreenDecision.KEEP for score in scores),
            review_count=sum(
                score.decision is SemanticScreenDecision.NEEDS_REVIEW for score in scores
            ),
            calibration_required_count=sum(
                score.decision is SemanticScreenDecision.CALIBRATION_REQUIRED
                for score in scores
            ),
        )

    def _prototype_vectors(
        self,
        run_id: str,
        manifest: EmbeddingModelManifest,
        records: list[SemanticVectorRecord],
    ) -> dict[BookMethodCategory, tuple[float, ...]]:
        result: dict[BookMethodCategory, tuple[float, ...]] = {}
        for category in BookMethodCategory:
            anchors = self.config.method_anchors[category]
            encoded = self.backend.encode(anchors)
            prototype = _normalized(
                tuple(
                    float(value)
                    for value in np.mean(
                        np.asarray([item.vector for item in encoded], dtype=np.float32),
                        axis=0,
                    )
                )
            )
            anchor_object = self.object_store.put_json(
                {"category": category.value, "anchors": anchors}
            )
            combined = EncodedText(
                vector=prototype,
                token_count=sum(item.token_count for item in encoded),
                chunk_count=sum(item.chunk_count for item in encoded),
            )
            records.append(
                _vector_record(
                    run_id=run_id,
                    manifest_id=manifest.manifest_id,
                    view=SemanticEmbeddingView.METHOD_PROTOTYPE,
                    entity_id=f"method-prototype:{category.value}",
                    item_id=None,
                    content_id=None,
                    source_snapshot_ids=(),
                    source_object_sha256s=(),
                    input_object_sha256=anchor_object.sha256,
                    encoded=combined,
                )
            )
            result[category] = prototype
        return result

    def _encode_in_blocks(
        self,
        texts: list[str],
        *,
        block_size: int = 512,
    ) -> list[EncodedText]:
        encoded: list[EncodedText] = []
        for start in range(0, len(texts), block_size):
            encoded.extend(self.backend.encode(texts[start : start + block_size]))
        return encoded

    def _score_argument(
        self,
        argument: ArgumentUnit,
        encoded: EncodedText,
        manifest: EmbeddingModelManifest,
        prototypes: dict[BookMethodCategory, tuple[float, ...]],
    ) -> SemanticArgumentScore:
        unit = argument
        category_scores = {
            category: round(_cosine(encoded.vector, prototype), 8)
            for category, prototype in prototypes.items()
        }
        top = max(category_scores.values(), default=0.0)
        keep_threshold = float(self.config.semantic_screen["exploratory_keep_threshold"])
        review_threshold = float(self.config.semantic_screen["exploratory_review_threshold"])
        selected = sorted(
            [
                category
                for category, score in category_scores.items()
                if score >= keep_threshold
            ],
            key=lambda category: category.value,
        )
        if top >= keep_threshold:
            decision = SemanticScreenDecision.KEEP
            reason = "SEMANTIC_TOPIC_ABOVE_EXPLORATORY_KEEP"
        elif top >= review_threshold:
            decision = SemanticScreenDecision.NEEDS_REVIEW
            reason = "SEMANTIC_TOPIC_IN_EXPLORATORY_REVIEW_BAND"
        else:
            decision = SemanticScreenDecision.CALIBRATION_REQUIRED
            reason = "UNCALIBRATED_LOW_SIMILARITY_NOT_AUTO_EXCLUDED"
        identity = {
            "run_id": unit.run_id,
            "argument_unit_id": unit.argument_unit_id,
            "embedding_manifest_id": manifest.manifest_id,
        }
        return SemanticArgumentScore(
            score_id=f"semantic-score:{content_hash(identity)}",
            run_id=unit.run_id,
            argument_unit_id=unit.argument_unit_id,
            embedding_manifest_id=manifest.manifest_id,
            topic_relevance=round(top, 8),
            methodological_completeness=unit.methodological_completeness,
            category_scores=category_scores,
            selected_categories=selected,
            decision=decision,
            reason_codes=[reason, "METHODOLOGICAL_COMPLETENESS_STRUCTURAL_ONLY"],
        )

    def _text(self, object_hash: str) -> str:
        return self.object_store.get_bytes(object_hash).decode("utf-8")


def install_local_model(model_directory: Path) -> LocalEmbeddingAssetManifest:
    """Download one fixed, official revision and freeze all local file hashes."""

    if (model_directory / _MANIFEST_FILE).is_file():
        try:
            return verify_local_model(model_directory)
        except ValueError as exc:
            previous = LocalEmbeddingAssetManifest.model_validate_json(
                (model_directory / _MANIFEST_FILE).read_text(encoding="utf-8")
            )
            if (
                previous.model_id != MODEL_ID
                or previous.model_revision != MODEL_REVISION
                or previous.dimension != MODEL_DIMENSION
            ):
                raise
            legacy_inference_files = {
                path: digest
                for path, digest in previous.files.items()
                if ".cache" not in Path(path).parts
            }
            if _file_hashes(model_directory) != legacy_inference_files:
                raise ValueError(
                    "local semantic model inference asset hash mismatch"
                ) from exc
            return _write_local_model_manifest(model_directory)
    module = importlib.import_module("huggingface_hub")
    snapshot_download = module.snapshot_download
    model_directory.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=str(model_directory),
        allow_patterns=[
            "*.json",
            "*.txt",
            "*.model",
            "*.safetensors",
            "LICENSE*",
            "1_Pooling/**",
        ],
    )
    return _write_local_model_manifest(model_directory)


def _write_local_model_manifest(model_directory: Path) -> LocalEmbeddingAssetManifest:
    files = _file_hashes(model_directory)
    manifest = LocalEmbeddingAssetManifest(
        model_id=MODEL_ID,
        model_revision=MODEL_REVISION,
        repository_url=MODEL_REPOSITORY_URL,
        license_id=MODEL_LICENSE,
        dimension=MODEL_DIMENSION,
        maximum_model_tokens=MODEL_MAXIMUM_TOKENS,
        files=files,
        bundle_sha256=content_hash(files),
    )
    atomic_write_text(
        model_directory / _MANIFEST_FILE,
        canonical_json_bytes(manifest.model_dump(mode="json")).decode("utf-8") + "\n",
    )
    return verify_local_model(model_directory)


def verify_local_model(model_directory: Path) -> LocalEmbeddingAssetManifest:
    manifest_path = model_directory / _MANIFEST_FILE
    if not manifest_path.is_file():
        raise FileNotFoundError(f"local semantic model manifest not found: {manifest_path}")
    manifest = LocalEmbeddingAssetManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if (
        manifest.model_id != MODEL_ID
        or manifest.model_revision != MODEL_REVISION
        or manifest.dimension != MODEL_DIMENSION
    ):
        raise ValueError("local semantic model identity is not the approved fixed asset")
    actual = _file_hashes(model_directory)
    if actual != manifest.files or content_hash(actual) != manifest.bundle_sha256:
        raise ValueError("local semantic model asset hash mismatch")
    return manifest


def default_model_directory(runtime_root: Path) -> Path:
    return runtime_root / "models" / "huggingface" / "BAAI__bge-small-zh-v1.5" / MODEL_REVISION


def _file_hashes(model_directory: Path) -> dict[str, str]:
    return {
        path.relative_to(model_directory).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(model_directory.rglob("*"))
        if path.is_file()
        and path.name != _MANIFEST_FILE
        and ".cache" not in path.relative_to(model_directory).parts
    }


def _tokenizer_hash(manifest: LocalEmbeddingAssetManifest) -> str:
    tokenizer_files = {
        path: digest
        for path, digest in manifest.files.items()
        if any(
            token in Path(path).name.casefold()
            for token in ("tokenizer", "vocab", "special_tokens")
        )
    }
    if not tokenizer_files:
        raise ValueError("local model manifest has no tokenizer assets")
    return content_hash(tokenizer_files)


def _vector_record(
    *,
    run_id: str,
    manifest_id: str,
    view: SemanticEmbeddingView,
    entity_id: str,
    item_id: str | None,
    content_id: str | None,
    source_snapshot_ids: tuple[str, ...],
    source_object_sha256s: tuple[str, ...],
    input_object_sha256: str,
    encoded: EncodedText,
) -> SemanticVectorRecord:
    identity = {
        "run_id": run_id,
        "manifest_id": manifest_id,
        "view": view.value,
        "entity_id": entity_id,
        "input_object_sha256": input_object_sha256,
    }
    return SemanticVectorRecord(
        vector_id=f"semantic-vector:{content_hash(identity)}",
        run_id=run_id,
        embedding_manifest_id=manifest_id,
        view=view,
        entity_id=entity_id,
        item_id=item_id,
        content_id=content_id,
        source_snapshot_ids=source_snapshot_ids,
        source_object_sha256s=source_object_sha256s,
        input_object_sha256=input_object_sha256,
        vector=encoded.vector,
        token_count=encoded.token_count,
        chunk_count=encoded.chunk_count,
    )


def _argument_lineages(
    arguments: list[ArgumentUnit],
    paragraph_groups: dict[str, list[ParagraphUnit]],
) -> dict[str, SemanticArgumentLineage]:
    paragraph_index: dict[str, tuple[str, ParagraphUnit]] = {}
    for item_id, paragraphs in paragraph_groups.items():
        if not item_id.strip():
            raise ValueError("argument lineage requires a non-empty SourceItem id")
        for paragraph in paragraphs:
            if paragraph.paragraph_id in paragraph_index:
                raise ValueError("argument lineage paragraph ids must be globally unique")
            paragraph_index[paragraph.paragraph_id] = (item_id, paragraph)
    lineages: dict[str, SemanticArgumentLineage] = {}
    for argument in arguments:
        if argument.argument_unit_id in lineages:
            raise ValueError("argument lineage argument ids must be unique")
        missing = [
            paragraph_id
            for paragraph_id in argument.paragraph_ids
            if paragraph_id not in paragraph_index
        ]
        if missing:
            raise ValueError("argument lineage references a missing paragraph")
        argument_paragraphs = [
            paragraph_index[paragraph_id] for paragraph_id in argument.paragraph_ids
        ]
        item_ids = {item_id for item_id, _paragraph in argument_paragraphs}
        if len(item_ids) != 1:
            raise ValueError("argument lineage crosses SourceItem boundaries")
        paragraphs = [paragraph for _item_id, paragraph in argument_paragraphs]
        if any(paragraph.run_id != argument.run_id for paragraph in paragraphs):
            raise ValueError("argument lineage crosses semantic run boundaries")
        if any(
            paragraph.content_id != argument.content_id
            or paragraph.locator.content_id != argument.content_id
            for paragraph in paragraphs
        ):
            raise ValueError("argument lineage crosses content boundaries")
        source_snapshot_ids = tuple(
            sorted({paragraph.locator.source_snapshot_id for paragraph in paragraphs})
        )
        if source_snapshot_ids != tuple(sorted(argument.source_snapshot_ids)):
            raise ValueError("argument lineage snapshot set does not match the argument")
        lineages[argument.argument_unit_id] = SemanticArgumentLineage(
            item_id=next(iter(item_ids)),
            content_id=argument.content_id,
            source_snapshot_ids=source_snapshot_ids,
            source_object_sha256s=tuple(
                sorted(
                    {
                        paragraph.locator.source_object_sha256
                        for paragraph in paragraphs
                    }
                )
            ),
        )
    return lineages


def _paragraph_groups_for_retained_arguments(
    repository: SemanticFunnelRepository,
    run_id: str,
    candidate_paragraph_groups: dict[str, list[ParagraphUnit]],
    arguments: list[ArgumentUnit],
) -> dict[str, list[ParagraphUnit]]:
    if any(argument.run_id != run_id for argument in arguments):
        raise ValueError("retained argument belongs to another semantic run")
    _validate_paragraph_group_run(run_id, candidate_paragraph_groups)
    required_paragraph_ids = {
        paragraph_id
        for argument in arguments
        for paragraph_id in argument.paragraph_ids
    }
    available_candidate_ids = {
        paragraph.paragraph_id
        for paragraphs in candidate_paragraph_groups.values()
        for paragraph in paragraphs
    }
    missing_paragraph_ids = required_paragraph_ids - available_candidate_ids
    if not missing_paragraph_ids:
        return candidate_paragraph_groups

    all_paragraph_groups = repository.paragraph_groups(run_id)
    supplemental_item_ids = {
        item_id
        for item_id, paragraphs in all_paragraph_groups.items()
        if missing_paragraph_ids
        & {paragraph.paragraph_id for paragraph in paragraphs}
    }
    supplemental_paragraph_groups = {
        item_id: all_paragraph_groups[item_id]
        for item_id in supplemental_item_ids
    }
    _validate_paragraph_group_run(run_id, supplemental_paragraph_groups)
    selected_item_ids = set(candidate_paragraph_groups) | supplemental_item_ids
    paragraph_groups = {
        item_id: (
            all_paragraph_groups[item_id]
            if item_id in supplemental_item_ids
            else candidate_paragraph_groups[item_id]
        )
        for item_id in all_paragraph_groups
        if item_id in selected_item_ids
    }
    paragraph_groups.update(
        {
            item_id: paragraphs
            for item_id, paragraphs in candidate_paragraph_groups.items()
            if item_id not in paragraph_groups
        }
    )
    return paragraph_groups


def _validate_paragraph_group_run(
    run_id: str,
    paragraph_groups: dict[str, list[ParagraphUnit]],
) -> None:
    for item_id, paragraphs in paragraph_groups.items():
        if not item_id.strip():
            raise ValueError("paragraph group requires a non-empty SourceItem id")
        if any(paragraph.run_id != run_id for paragraph in paragraphs):
            raise ValueError("paragraph group belongs to another semantic run")


def _normalized(vector: tuple[float, ...]) -> tuple[float, ...]:
    values = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(values))
    if norm <= 0.0:
        raise ValueError("embedding vectors must have a positive norm")
    return tuple(float(value) for value in values / norm)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("cosine vectors must share one dimension")
    value = float(np.dot(np.asarray(left), np.asarray(right)))
    return min(1.0, max(0.0, value))


__all__ = [
    "MODEL_ID",
    "MODEL_REVISION",
    "EncodedText",
    "EmbeddingBackend",
    "RecordedEmbeddingBackend",
    "SemanticEmbeddingExecution",
    "SemanticEmbeddingService",
    "SentenceTransformerBackend",
    "default_model_directory",
    "install_local_model",
    "verify_local_model",
]
