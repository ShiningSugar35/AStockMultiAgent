from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from astock.knowledge.semantic_embedding import (
    _write_local_model_manifest,
    install_local_model,
    verify_local_model,
)
from astock.knowledge.semantic_storage import (
    ParquetSemanticStore,
    SemanticArgumentLineage,
    SemanticVectorRecord,
)
from astock.schemas import (
    BookMethodCategory,
    EmbeddingModelManifest,
    SemanticArgumentScore,
    SemanticEmbeddingContract,
    SemanticEmbeddingView,
    SemanticScreenDecision,
)


def test_model_manifest_excludes_cache_and_never_resigns_tampered_weights(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    (model / ".cache" / "huggingface").mkdir(parents=True)
    (model / ".cache" / "huggingface" / "download.json").write_text(
        "volatile",
        encoding="utf-8",
    )
    (model / "model.safetensors").write_bytes(b"approved-weight")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")

    manifest = _write_local_model_manifest(model)
    assert all(".cache" not in Path(path).parts for path in manifest.files)
    assert verify_local_model(model) == manifest

    (model / "model.safetensors").write_bytes(b"tampered-weight")
    with pytest.raises(ValueError, match="inference asset hash mismatch"):
        install_local_model(model)


def _manifest_payload(contract: str) -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "manifest_id": f"semantic-embedding:{contract.casefold()}",
        "model_id": "recorded/legacy",
        "model_revision": "fixed",
        "model_asset_sha256": "1" * 64,
        "tokenizer_asset_sha256": "2" * 64,
        "dimension": 3,
        "normalized": True,
        "local_only": True,
        "embedding_contract_version": contract,
        "anchor_config_sha256": "3" * 64,
        "threshold_config_sha256": "4" * 64,
        "created_at": datetime(2026, 7, 22, tzinfo=UTC),
    }


def test_v3_manifest_requires_exact_ordered_view_contract() -> None:
    manifest = EmbeddingModelManifest.model_validate(
        {
            **_manifest_payload("PARAGRAPH_AUX_ARGUMENT_FINAL_V3"),
            "embedding_views": [
                "PARAGRAPH_CURRENT",
                "PARAGRAPH_LOCAL_CONTEXT",
                "ARGUMENT_UNIT",
                "METHOD_PROTOTYPE",
            ],
            "auxiliary_views": [
                "PARAGRAPH_CURRENT",
                "PARAGRAPH_LOCAL_CONTEXT",
            ],
            "decision_view": "ARGUMENT_UNIT",
            "method_prototype_count": 14,
        }
    )
    assert manifest.embedding_contract_version is (
        SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
    )
    assert manifest.embedding_views == [
        SemanticEmbeddingView.PARAGRAPH_CURRENT,
        SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
        SemanticEmbeddingView.ARGUMENT_UNIT,
        SemanticEmbeddingView.METHOD_PROTOTYPE,
    ]
    assert manifest.auxiliary_views == [
        SemanticEmbeddingView.PARAGRAPH_CURRENT,
        SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
    ]
    assert manifest.decision_view is SemanticEmbeddingView.ARGUMENT_UNIT
    assert manifest.method_prototype_count == 14

    with pytest.raises(ValueError, match="views or order"):
        EmbeddingModelManifest.model_validate(
            {
                **manifest.model_dump(mode="json"),
                "embedding_views": list(reversed(manifest.embedding_views)),
            }
        )


@pytest.mark.parametrize(
    "contract",
    [
        SemanticEmbeddingContract.LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1,
        SemanticEmbeddingContract.ARGUMENT_UNIT_ONLY_V2,
    ],
)
def test_legacy_manifests_parse_but_storage_rejects_without_writing(
    tmp_path: Path,
    contract: SemanticEmbeddingContract,
) -> None:
    manifest = EmbeddingModelManifest.model_validate(_manifest_payload(contract.value))
    assert manifest.embedding_contract_version is contract
    vector = SemanticVectorRecord(
        vector_id="vector:legacy",
        run_id="run:legacy",
        embedding_manifest_id=manifest.manifest_id,
        view=SemanticEmbeddingView.ARGUMENT_UNIT,
        entity_id="argument:legacy",
        item_id="item:legacy",
        content_id="content:legacy",
        source_snapshot_ids=("snapshot:legacy",),
        source_object_sha256s=("6" * 64,),
        input_object_sha256="5" * 64,
        vector=(1.0, 0.0, 0.0),
        token_count=1,
        chunk_count=1,
    )
    parquet_root = tmp_path / contract.value
    with pytest.raises(ValueError, match="active semantic contract"):
        ParquetSemanticStore(parquet_root).write(
            author_source_id="source:legacy",
            run_id="run:legacy",
            embedding_manifest_id=manifest.manifest_id,
            vectors=[vector],
            scores=[],
            manifest=manifest,
            candidate_paragraph_ids=set(),
            argument_unit_ids={"argument:legacy"},
            argument_lineages={},
        )
    assert not parquet_root.exists()


def _active_manifest() -> EmbeddingModelManifest:
    return EmbeddingModelManifest.model_validate(
        {
            **_manifest_payload("PARAGRAPH_AUX_ARGUMENT_FINAL_V3"),
            "embedding_views": [
                "PARAGRAPH_CURRENT",
                "PARAGRAPH_LOCAL_CONTEXT",
                "ARGUMENT_UNIT",
                "METHOD_PROTOTYPE",
            ],
            "auxiliary_views": [
                "PARAGRAPH_CURRENT",
                "PARAGRAPH_LOCAL_CONTEXT",
            ],
            "decision_view": "ARGUMENT_UNIT",
            "method_prototype_count": 14,
        }
    )


def _active_write_payload() -> tuple[
    EmbeddingModelManifest,
    list[SemanticVectorRecord],
    list[SemanticArgumentScore],
    dict[str, SemanticArgumentLineage],
]:
    manifest = _active_manifest()

    def vector(
        view: SemanticEmbeddingView,
        entity_id: str,
        *,
        item_id: str | None,
        content_id: str | None,
        snapshots: tuple[str, ...],
        hashes: tuple[str, ...],
    ) -> SemanticVectorRecord:
        return SemanticVectorRecord(
            vector_id=f"vector:{view.value}:{entity_id}",
            run_id="run:v3",
            embedding_manifest_id=manifest.manifest_id,
            view=view,
            entity_id=entity_id,
            item_id=item_id,
            content_id=content_id,
            source_snapshot_ids=snapshots,
            source_object_sha256s=hashes,
            input_object_sha256="5" * 64,
            vector=(1.0, 0.0, 0.0),
            token_count=1,
            chunk_count=1,
        )

    paragraph_provenance = {
        "item_id": "item:one",
        "content_id": "content:one",
        "snapshots": ("snapshot:one",),
        "hashes": ("6" * 64,),
    }
    vectors = [
        vector(
            SemanticEmbeddingView.PARAGRAPH_CURRENT,
            "paragraph:one",
            **paragraph_provenance,
        ),
        vector(
            SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
            "paragraph:one",
            **paragraph_provenance,
        ),
        vector(
            SemanticEmbeddingView.ARGUMENT_UNIT,
            "argument:one",
            item_id="item:one",
            content_id="content:one",
            snapshots=("snapshot:one",),
            hashes=("6" * 64, "7" * 64),
        ),
        *[
            vector(
                SemanticEmbeddingView.METHOD_PROTOTYPE,
                f"method-prototype:{category.value}",
                item_id=None,
                content_id=None,
                snapshots=(),
                hashes=(),
            )
            for category in BookMethodCategory
        ],
    ]
    scores = [
        SemanticArgumentScore(
            score_id="score:one",
            run_id="run:v3",
            argument_unit_id="argument:one",
            embedding_manifest_id=manifest.manifest_id,
            topic_relevance=1.0,
            methodological_completeness=1.0,
            category_scores={category: 0.0 for category in BookMethodCategory},
            selected_categories=[],
            decision=SemanticScreenDecision.CALIBRATION_REQUIRED,
            reason_codes=["TEST"],
        )
    ]
    lineages = {
        "argument:one": SemanticArgumentLineage(
            item_id="item:one",
            content_id="content:one",
            source_snapshot_ids=("snapshot:one",),
            source_object_sha256s=("6" * 64, "7" * 64),
        )
    }
    return manifest, vectors, scores, lineages


def _write_active_payload(
    root: Path,
    manifest: EmbeddingModelManifest,
    vectors: list[SemanticVectorRecord],
    scores: list[SemanticArgumentScore],
    lineages: dict[str, SemanticArgumentLineage],
) -> Path:
    return ParquetSemanticStore(root).write(
        author_source_id="source:v3",
        run_id="run:v3",
        embedding_manifest_id=manifest.manifest_id,
        vectors=vectors,
        scores=scores,
        manifest=manifest,
        candidate_paragraph_ids={"paragraph:one"},
        argument_unit_ids={"argument:one"},
        argument_lineages=lineages,
    ).vectors_path


def test_argument_lineage_allows_one_snapshot_with_title_and_body_hashes(
    tmp_path: Path,
) -> None:
    manifest, vectors, scores, lineages = _active_write_payload()
    vectors_path = _write_active_payload(
        tmp_path / "parquet",
        manifest,
        vectors,
        scores,
        lineages,
    )
    argument_row = next(
        row
        for row in pq.read_table(vectors_path).to_pylist()
        if row["view"] == "ARGUMENT_UNIT"
    )
    assert argument_row["source_snapshot_ids"] == ["snapshot:one"]
    assert argument_row["source_object_sha256s"] == ["6" * 64, "7" * 64]


@pytest.mark.parametrize(
    "tamper",
    [
        "replacement",
        "missing",
        "extra",
        "hash",
        "snapshot",
        "item",
        "content",
        "empty_hashes",
        "duplicate_hashes",
        "invalid_hash",
        "empty_snapshots",
        "duplicate_snapshots",
        "blank_snapshot",
    ],
)
def test_argument_lineage_tampering_is_rejected_before_parquet_creation(
    tmp_path: Path,
    tamper: str,
) -> None:
    manifest, vectors, scores, lineages = _active_write_payload()
    argument_index = next(
        index
        for index, vector in enumerate(vectors)
        if vector.view is SemanticEmbeddingView.ARGUMENT_UNIT
    )
    lineage = lineages["argument:one"]
    if tamper == "replacement":
        lineages["argument:one"] = replace(
            lineage,
            item_id="item:replacement",
            content_id="content:replacement",
        )
    elif tamper == "missing":
        lineages = {}
    elif tamper == "extra":
        lineages["argument:extra"] = lineage
    elif tamper == "hash":
        vectors[argument_index] = replace(
            vectors[argument_index],
            source_object_sha256s=("6" * 64, "8" * 64),
        )
    elif tamper == "snapshot":
        vectors[argument_index] = replace(
            vectors[argument_index],
            source_snapshot_ids=("snapshot:replacement",),
        )
    elif tamper == "item":
        vectors[argument_index] = replace(
            vectors[argument_index],
            item_id="item:replacement",
        )
    elif tamper == "content":
        vectors[argument_index] = replace(
            vectors[argument_index],
            content_id="content:replacement",
        )
    elif tamper == "empty_hashes":
        lineages["argument:one"] = replace(lineage, source_object_sha256s=())
    elif tamper == "duplicate_hashes":
        lineages["argument:one"] = replace(
            lineage,
            source_object_sha256s=("6" * 64, "6" * 64),
        )
    elif tamper == "invalid_hash":
        lineages["argument:one"] = replace(
            lineage,
            source_object_sha256s=("not-a-hash",),
        )
    elif tamper == "empty_snapshots":
        lineages["argument:one"] = replace(lineage, source_snapshot_ids=())
    elif tamper == "duplicate_snapshots":
        lineages["argument:one"] = replace(
            lineage,
            source_snapshot_ids=("snapshot:one", "snapshot:one"),
        )
    else:
        lineages["argument:one"] = replace(
            lineage,
            source_snapshot_ids=(" ",),
        )
    parquet_root = tmp_path / "parquet"
    with pytest.raises(ValueError, match="lineage"):
        _write_active_payload(parquet_root, manifest, vectors, scores, lineages)
    assert not parquet_root.exists()
