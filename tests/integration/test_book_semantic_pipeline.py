from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
import pymupdf

from astock.books import PrivatePdfIngestService
from astock.books.visual_repository import BookVisualRepository
from astock.books.visual_semantics import (
    BookVisualSemanticService,
    load_book_visual_distillation_config,
)
from astock.books.visuals import BookVisualService
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.documents.ocr import OcrResult
from astock.knowledge import (
    ParquetSemanticStore,
    RecordedEmbeddingBackend,
    SemanticEmbeddingService,
    SemanticFunnelRepository,
    SemanticPacketService,
    load_semantic_funnel_config,
    local_context_paragraph_ids,
)
from astock.schemas import (
    ArgumentRelationType,
    ArgumentUnitStatus,
    BookMethodCategory,
    BookVisualRunStage,
    ImageOcrStatus,
    KeywordScreenResult,
    LocalEmbeddingAssetManifest,
    ParagraphMergeAction,
    ParagraphUnitKind,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class StaticOcr:
    result: OcrResult
    name: str = "recorded-ocr"
    version: str = "1"
    calls: int = 0

    def recognize(self, image_bytes: bytes) -> OcrResult:
        assert image_bytes
        self.calls += 1
        return self.result


@dataclass
class SequenceStaticOcr:
    results: list[OcrResult]
    name: str = "recorded-ocr"
    version: str = "1"
    calls: int = 0

    def recognize(self, image_bytes: bytes) -> OcrResult:
        assert image_bytes
        result = self.results[self.calls]
        self.calls += 1
        return result


def _chart_png() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=220, height=100)
    page.draw_line((20, 80), (100, 20), color=(0, 0, 0), width=3)
    page.insert_text((20, 95), "profit chart", fontsize=10)
    data = page.get_pixmap(alpha=False).tobytes("png")
    document.close()
    return data


def _book(path: Path, *, with_context: bool) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    if with_context:
        page.insert_textbox(
            pymupdf.Rect(50, 50, 550, 95),
            "Claim: profit quality supports this method.",
            fontsize=12,
        )
    page.insert_image(
        pymupdf.Rect(80, 160, 500, 360),
        stream=_chart_png(),
    )
    if with_context:
        page.insert_textbox(
            pymupdf.Rect(50, 430, 550, 490),
            "Therefore the profit method conclusion follows.",
            fontsize=12,
        )
    path.write_bytes(document.tobytes())
    document.close()


def _two_page_book(path: Path) -> None:
    document = pymupdf.open()
    for page_number in (1, 2):
        page = document.new_page(width=600, height=800)
        keyword = " profit" if page_number == 2 else ""
        page.insert_textbox(
            pymupdf.Rect(50, 50, 550, 95),
            f"Claim:{keyword} quality supports this method.",
            fontsize=12,
        )
        page.insert_image(
            pymupdf.Rect(80, 160, 500, 360),
            stream=_chart_png(),
        )
        page.insert_textbox(
            pymupdf.Rect(50, 430, 550, 490),
            f"Therefore the{keyword} method conclusion follows.",
            fontsize=12,
        )
    path.write_bytes(document.tobytes())
    document.close()


def _nonvisual_review_plus_ready_book(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_textbox(
        pymupdf.Rect(50, 50, 550, 110),
        "Historical background without a selected research topic.",
        fontsize=12,
    )
    page = document.new_page(width=600, height=800)
    page.insert_textbox(
        pymupdf.Rect(50, 50, 550, 95),
        "Claim: profit quality supports this method.",
        fontsize=12,
    )
    page.insert_image(
        pymupdf.Rect(80, 160, 500, 360),
        stream=_chart_png(),
    )
    page.insert_textbox(
        pymupdf.Rect(50, 430, 550, 490),
        "Therefore the profit method conclusion follows.",
        fontsize=12,
    )
    path.write_bytes(document.tobytes())
    document.close()


def _recorded_embedding(
    tmp_path: Path,
    *,
    suffix: str,
    objects: ObjectStore,
    semantic: SemanticFunnelRepository,
    semantic_run_id: str,
):
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    paragraph_groups = semantic.paragraph_groups(semantic_run_id)
    arguments = semantic.argument_units(semantic_run_id)
    fixed_texts = [
        anchor
        for anchors in config.method_anchors.values()
        for anchor in anchors
    ]
    fixed_texts.extend(
        objects.get_bytes(argument.text_object_sha256).decode("utf-8")
        for argument in arguments
    )
    for paragraph_group in paragraph_groups.values():
        paragraph_lookup = {
            paragraph.paragraph_id: paragraph for paragraph in paragraph_group
        }
        for paragraph in paragraph_group:
            fixed_texts.append(
                objects.get_bytes(paragraph.text_object_sha256).decode("utf-8")
            )
            fixed_texts.append(
                "\n".join(
                    f"[paragraph ordinal={paragraph_lookup[paragraph_id].ordinal}] "
                    f"{objects.get_bytes(paragraph_lookup[paragraph_id].text_object_sha256).decode('utf-8')}"
                    for paragraph_id in local_context_paragraph_ids(
                        paragraph_group,
                        paragraph.ordinal,
                    )
                )
            )
    asset_files = {
        "model.safetensors": "1" * 64,
        "tokenizer.json": "2" * 64,
    }
    asset = LocalEmbeddingAssetManifest(
        model_id=f"recorded/{suffix}",
        model_revision="fixed",
        repository_url="https://example.invalid/offline-fixture",
        license_id="TEST_ONLY",
        dimension=3,
        maximum_model_tokens=32,
        files=asset_files,
        bundle_sha256=content_hash(asset_files),
    )
    parquet = ParquetSemanticStore(tmp_path / f"parquet-{suffix}")
    execution = SemanticEmbeddingService(
        semantic,
        objects,
        parquet,
        config,
        RecordedEmbeddingBackend(
            {text: (1.0, 0.0, 0.0) for text in fixed_texts}
        ),
        asset,
    ).run(semantic_run_id)
    return config, parquet, execution


def _materialize(
    tmp_path: Path,
    state,
    *,
    suffix: str,
    with_context: bool,
    confidence: float,
    include_keyword_terms: bool = True,
):
    objects = ObjectStore(tmp_path / f"objects-{suffix}")
    path = tmp_path / f"{suffix}.pdf"
    _book(path, with_context=with_context)
    manifest = PrivatePdfIngestService(objects, state).ingest(
        path,
        source_id=f"book:test:{suffix}",
        display_name=f"Book semantic fixture {suffix}",
        author_source_id="author:test-book",
        file_version="v1",
        ocr_enabled=False,
    ).manifest
    engine = StaticOcr(OcrResult("profit chart trend", confidence))
    visual_service = BookVisualService(
        state,
        objects,
        load_book_visual_distillation_config(
            PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
        ),
        ocr_engine=engine,
    )
    execution = visual_service.run(manifest.manifest_id)
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    keyword_terms = {
        category: (
            ("profit",)
            if include_keyword_terms
            and category is BookMethodCategory.FINANCIAL_QUALITY
            else ()
        )
        for category in BookMethodCategory
    }
    semantic_run, refs = BookVisualSemanticService(state, objects).materialize(
        execution.run.run_id,
        config=config,
        keyword_terms=keyword_terms,
    )
    return objects, visual_service, execution, semantic_run, refs


def test_claim_chart_conclusion_become_one_argument_with_exact_visual_lineage(
    tmp_path: Path,
    state,
) -> None:
    objects, visual_service, execution, semantic_run, refs = _materialize(
        tmp_path,
        state,
        suffix="claim-chart-conclusion",
        with_context=True,
        confidence=0.96,
    )
    assert execution.run.stage is BookVisualRunStage.CHARTS_CLASSIFIED
    assert len(refs) == 1

    semantic = SemanticFunnelRepository(state)
    paragraphs = [
        paragraph
        for group in semantic.paragraph_groups(semantic_run.run_id).values()
        for paragraph in group
    ]
    arguments = semantic.argument_units(semantic_run.run_id)
    relations = semantic.argument_relations(semantic_run.run_id)
    visual = next(
        paragraph
        for paragraph in paragraphs
        if paragraph.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
    )
    argument = next(
        argument
        for argument in arguments
        if visual.paragraph_id in argument.paragraph_ids
    )

    assert len(argument.paragraph_ids) == 3
    assert argument.status is ArgumentUnitStatus.READY
    assert visual.standalone_distillable is False
    assert visual.merge_action is ParagraphMergeAction.MERGE_WITH_BOTH
    assert visual.locator.page_number == 1
    assert visual.locator.bbox is not None
    assert all(
        paragraph.locator.source_object_sha256
        == execution.run.raw_object_sha256
        for paragraph in paragraphs
    )
    assert refs[0].paragraph_id == visual.paragraph_id
    assert refs[0].argument_unit_id == argument.argument_unit_id
    assert {
        relation.relation_type
        for relation in relations
        if relation.relation_id in argument.relation_ids
    } >= {
        ArgumentRelationType.CONTINUATION,
        ArgumentRelationType.CLAIM_EVIDENCE,
        ArgumentRelationType.CONCLUSION_OF,
    }
    assert set(refs[0].relation_ids).issubset(argument.relation_ids)
    assert objects.get_bytes(visual.text_object_sha256).decode("utf-8") == (
        "profit chart trend"
    )

    repeated_run, repeated_refs = BookVisualSemanticService(
        state,
        objects,
    ).materialize(
        execution.run.run_id,
        config=load_semantic_funnel_config(
            PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
        ),
        keyword_terms={
            category: (
                ("profit",)
                if category is BookMethodCategory.FINANCIAL_QUALITY
                else ()
            )
            for category in BookMethodCategory
        },
    )
    assert repeated_run == semantic_run
    assert repeated_refs == refs

    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    paragraph_group = next(iter(semantic.paragraph_groups(semantic_run.run_id).values()))
    paragraph_lookup = {
        paragraph.paragraph_id: paragraph for paragraph in paragraph_group
    }
    fixed_texts = [
        anchor
        for anchors in config.method_anchors.values()
        for anchor in anchors
    ]
    fixed_texts.extend(
        objects.get_bytes(argument.text_object_sha256).decode("utf-8")
        for argument in arguments
    )
    for paragraph in paragraph_group:
        fixed_texts.append(
            objects.get_bytes(paragraph.text_object_sha256).decode("utf-8")
        )
        fixed_texts.append(
            "\n".join(
                f"[paragraph ordinal={paragraph_lookup[paragraph_id].ordinal}] "
                f"{objects.get_bytes(paragraph_lookup[paragraph_id].text_object_sha256).decode('utf-8')}"
                for paragraph_id in local_context_paragraph_ids(
                    paragraph_group,
                    paragraph.ordinal,
                )
            )
        )
    asset_files = {
        "model.safetensors": "1" * 64,
        "tokenizer.json": "2" * 64,
    }
    asset = LocalEmbeddingAssetManifest(
        model_id="recorded/book-visual-model",
        model_revision="fixed",
        repository_url="https://example.invalid/offline-fixture",
        license_id="TEST_ONLY",
        dimension=3,
        maximum_model_tokens=32,
        files=asset_files,
        bundle_sha256=content_hash(asset_files),
    )
    parquet = ParquetSemanticStore(tmp_path / "parquet-book")
    embedding = SemanticEmbeddingService(
        semantic,
        objects,
        parquet,
        config,
        RecordedEmbeddingBackend(
            {text: (1.0, 0.0, 0.0) for text in fixed_texts}
        ),
        asset,
    ).run(semantic_run.run_id)
    assert embedding.score_count == 1
    packet = SemanticPacketService(
        semantic,
        objects,
        parquet,
        tmp_path / "runtime-book",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).export(semantic_run.run_id)
    packet_payload = json.loads(
        packet.packet_file.read_text(encoding="utf-8").splitlines()[0]
    )
    packet_visual = next(
        item
        for item in packet_payload["paragraphs"]
        if item["paragraph_kind"] == "VISUAL_EVIDENCE"
    )
    assert packet_visual["locator"]["bbox"]
    assert (
        packet_visual["locator"]["source_object_sha256"]
        == execution.run.raw_object_sha256
    )
    assert packet_visual["visual_evidence_ids"] == visual.visual_evidence_ids
    assert packet_visual["visual_chart_unit_ids"] == visual.visual_chart_unit_ids
    assert packet_visual["visual_quality_status"] == "SUCCESS"
    assert packet_visual["visual_reason_codes"] == []

    report = visual_service.audit(execution.run.run_id)
    assert report.source_pages == 1
    assert report.image_pages == 1
    assert report.image_placements == 1
    assert report.processed_placements == 1
    assert report.affected_argument_unit_count == 1
    assert report.image_only_ready_candidate_count == 0
    audited_run = BookVisualRepository(state).get_run(execution.run.run_id)
    assert audited_run is not None and audited_run.finished_at is not None
    assert audited_run.finished_at > audited_run.started_at
    assert report.created_at == audited_run.finished_at
    assert visual_service.audit(execution.run.run_id) == report
    assert BookVisualRepository(state).get_run(execution.run.run_id) == audited_run


def test_image_only_and_low_confidence_visual_arguments_require_review(
    tmp_path: Path,
    state,
) -> None:
    _, image_only_service, image_only_execution, image_only_run, image_only_refs = (
        _materialize(
            tmp_path,
            state,
            suffix="image-only",
            with_context=False,
            confidence=0.96,
        )
    )
    assert len(image_only_refs) == 1
    assert image_only_execution.ocr_results[0].status is ImageOcrStatus.SUCCESS
    semantic = SemanticFunnelRepository(state)
    image_only_arguments = semantic.argument_units(image_only_run.run_id)
    assert len(image_only_arguments) == 1
    assert image_only_arguments[0].status is ArgumentUnitStatus.NEEDS_REVIEW
    assert image_only_arguments[0].standalone_distillable is False
    assert "DANGLING_CONTEXT_DEPENDENCY" in image_only_arguments[0].reason_codes
    image_only_report = image_only_service.audit(image_only_execution.run.run_id)
    assert image_only_report.image_only_ready_candidate_count == 0

    _, visual_service, execution, semantic_run, refs = _materialize(
        tmp_path,
        state,
        suffix="low-confidence-context",
        with_context=True,
        confidence=0.40,
    )
    assert len(refs) == 1
    assert execution.ocr_results[0].status is ImageOcrStatus.LOW_CONFIDENCE
    semantic = SemanticFunnelRepository(state)
    arguments = semantic.argument_units(semantic_run.run_id)
    assert len(arguments) == 1
    assert arguments[0].status is ArgumentUnitStatus.NEEDS_REVIEW
    assert arguments[0].standalone_distillable is False
    assert "VISUAL_REVIEW_REQUIRED" in arguments[0].reason_codes
    report = visual_service.audit(execution.run.run_id)
    assert report.low_confidence == 1
    assert report.image_only_ready_candidate_count == 0


def test_no_keyword_visual_page_keeps_complete_three_view_and_packet_lineage(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects-no-keyword")
    path = tmp_path / "no-keyword-plus-ready.pdf"
    _two_page_book(path)
    manifest = PrivatePdfIngestService(objects, state).ingest(
        path,
        source_id="book:test:no-keyword-plus-ready",
        display_name="Book no-keyword lineage fixture",
        author_source_id="author:test-book",
        file_version="v1",
        ocr_enabled=False,
    ).manifest
    visual_service = BookVisualService(
        state,
        objects,
        load_book_visual_distillation_config(
            PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
        ),
        ocr_engine=SequenceStaticOcr(
            [
                OcrResult("chart trend", 0.96),
                OcrResult("profit chart trend", 0.96),
            ]
        ),
    )
    visual_execution = visual_service.run(manifest.manifest_id)
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    keyword_terms = {
        category: (
            ("profit",)
            if category is BookMethodCategory.FINANCIAL_QUALITY
            else ()
        )
        for category in BookMethodCategory
    }
    semantic_run, refs = BookVisualSemanticService(state, objects).materialize(
        visual_execution.run.run_id,
        config=config,
        keyword_terms=keyword_terms,
    )
    assert len(refs) == 2
    with state.connect() as connection:
        screens = [
            KeywordScreenResult.model_validate_json(row["screen_json"])
            for row in connection.execute(
                "SELECT screen_json FROM knowledge_keyword_screen WHERE run_id=? "
                "ORDER BY item_id",
                (semantic_run.run_id,),
            ).fetchall()
        ]
    lineage_screen = next(
        screen for screen in screens if screen.candidate_reason_codes
    )
    assert lineage_screen.candidate_reason_codes == [
        "BOOK_VISUAL_ARGUMENT_LINEAGE"
    ]

    semantic = SemanticFunnelRepository(state)
    candidate_groups = semantic.paragraph_groups(
        semantic_run.run_id,
        candidate_only=True,
    )
    assert len(candidate_groups) == 2
    assert sum(len(group) for group in candidate_groups.values()) == 6
    arguments = semantic.argument_units(semantic_run.run_id)
    no_keyword_argument = next(
        argument
        for argument in arguments
        if "BOOK_VISUAL_NO_METHOD_KEYWORD_REVIEW_REQUIRED"
        in argument.reason_codes
    )
    assert no_keyword_argument.status is ArgumentUnitStatus.NEEDS_REVIEW
    no_keyword_visual = next(
        paragraph
        for group in candidate_groups.values()
        for paragraph in group
        if paragraph.paragraph_id in no_keyword_argument.paragraph_ids
        and paragraph.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
    )
    assert no_keyword_visual.standalone_distillable is False
    assert (
        "BOOK_VISUAL_NO_METHOD_KEYWORD_REVIEW_REQUIRED"
        in no_keyword_visual.visual_reason_codes
    )
    assert (
        no_keyword_visual.locator.source_object_sha256
        == manifest.raw_object_sha256
    )

    _, parquet, embedding = _recorded_embedding(
        tmp_path,
        suffix="no-keyword-lineage",
        objects=objects,
        semantic=semantic,
        semantic_run_id=semantic_run.run_id,
    )
    assert embedding.score_count == 2
    packet = SemanticPacketService(
        semantic,
        objects,
        parquet,
        tmp_path / "runtime-no-keyword",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).export(semantic_run.run_id)
    assert packet.batch.exported_argument_count == 1
    assert packet.held_back_structural_count == 1
    payload = json.loads(
        packet.packet_file.read_text(encoding="utf-8").splitlines()[0]
    )
    assert all(
        paragraph["locator"]["source_object_sha256"]
        == manifest.raw_object_sha256
        for paragraph in payload["paragraphs"]
    )


def test_nonvisual_review_argument_gets_same_run_lineage_without_becoming_candidate(
    tmp_path: Path,
    state,
) -> None:
    objects = ObjectStore(tmp_path / "objects-nonvisual-review")
    path = tmp_path / "nonvisual-review-plus-ready.pdf"
    _nonvisual_review_plus_ready_book(path)
    manifest = PrivatePdfIngestService(objects, state).ingest(
        path,
        source_id="book:test:nonvisual-review-plus-ready",
        display_name="Book nonvisual review lineage fixture",
        author_source_id="author:test-book",
        file_version="v1",
        ocr_enabled=False,
    ).manifest
    visual_execution = BookVisualService(
        state,
        objects,
        load_book_visual_distillation_config(
            PROJECT_ROOT / "configs" / "book_visual_distillation.yaml"
        ),
        ocr_engine=SequenceStaticOcr([OcrResult("profit chart trend", 0.96)]),
    ).run(manifest.manifest_id)
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    semantic_run, refs = BookVisualSemanticService(state, objects).materialize(
        visual_execution.run.run_id,
        config=config,
        keyword_terms={
            category: (
                ("profit",)
                if category is BookMethodCategory.FINANCIAL_QUALITY
                else ()
            )
            for category in BookMethodCategory
        },
    )
    assert len(refs) == 1

    semantic = SemanticFunnelRepository(state)
    paragraph_groups = semantic.paragraph_groups(semantic_run.run_id)
    candidate_groups = semantic.paragraph_groups(
        semantic_run.run_id,
        candidate_only=True,
    )
    assert len(paragraph_groups) == 2
    assert len(candidate_groups) == 1
    nonvisual_item_id, nonvisual_paragraphs = next(
        (item_id, paragraphs)
        for item_id, paragraphs in paragraph_groups.items()
        if paragraphs[0].locator.page_number == 1
    )
    assert nonvisual_item_id not in candidate_groups
    assert all(
        paragraph.paragraph_kind is ParagraphUnitKind.TEXT
        for paragraph in nonvisual_paragraphs
    )
    nonvisual_argument = next(
        argument
        for argument in semantic.argument_units(semantic_run.run_id)
        if set(argument.paragraph_ids)
        == {paragraph.paragraph_id for paragraph in nonvisual_paragraphs}
    )
    assert nonvisual_argument.status is ArgumentUnitStatus.NEEDS_REVIEW
    assert nonvisual_argument.standalone_distillable is False

    _, parquet, embedding = _recorded_embedding(
        tmp_path,
        suffix="nonvisual-review-lineage",
        objects=objects,
        semantic=semantic,
        semantic_run_id=semantic_run.run_id,
    )
    assert embedding.score_count == 2
    vector_rows = (
        pq.ParquetFile(embedding.parquet.vectors_path)
        .read(columns=["view", "entity_id", "item_id"])
        .to_pylist()
    )
    for paragraph in nonvisual_paragraphs:
        assert {
            row["view"]
            for row in vector_rows
            if row["entity_id"] == paragraph.paragraph_id
        } == {"PARAGRAPH_CURRENT", "PARAGRAPH_LOCAL_CONTEXT"}
        assert {
            row["item_id"]
            for row in vector_rows
            if row["entity_id"] == paragraph.paragraph_id
        } == {nonvisual_item_id}
    assert {
        row["view"]
        for row in vector_rows
        if row["entity_id"] == nonvisual_argument.argument_unit_id
    } == {"ARGUMENT_UNIT"}

    packet = SemanticPacketService(
        semantic,
        objects,
        parquet,
        tmp_path / "runtime-nonvisual-review",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).export(semantic_run.run_id)
    assert packet.batch.exported_argument_count == 1
    assert packet.held_back_structural_count == 1
    with state.connect() as connection:
        candidate_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_semantic_candidate WHERE run_id=?",
            (semantic_run.run_id,),
        ).fetchone()[0]
    assert candidate_count == 0
