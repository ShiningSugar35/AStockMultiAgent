from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge import (
    EncodedText,
    KnowledgeRepository,
    ParquetSemanticStore,
    RecordedEmbeddingBackend,
    SemanticEmbeddingService,
    SemanticFunnelRepository,
    SemanticFunnelService,
    SemanticPacketService,
    load_distillation_rules,
    load_semantic_funnel_config,
    local_context_paragraph_ids,
    method_keyword_terms,
    paragraphize_zhihu_content,
)
from astock.knowledge.semantic_embedding import (
    _argument_lineages,
    _paragraph_groups_for_retained_arguments,
)
from astock.schemas import (
    BookMethodCategory,
    LocalEmbeddingAssetManifest,
    SemanticEmbeddingContract,
    SemanticEmbeddingView,
    SemanticFunnelRun,
    SemanticLlmBatchStatus,
    SemanticPacketContract,
    SemanticRunStage,
    SemanticScreenDecision,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RefusingEmbeddingBackend:
    dimension = 3

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts: list[str]) -> list[EncodedText]:
        self.calls += 1
        raise AssertionError(f"cross-run input reached the encoder: {texts!r}")


@pytest.mark.parametrize("tamper", ["paragraph_author", "relation_endpoint", "argument_run"])
def test_paragraphized_repository_rejects_cross_contract_rows_atomically(
    tmp_path: Path,
    tamper: str,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    record = ZhihuContentRecord(
        version_id="version:cross-contract",
        author_source_id="zhihu:test-author",
        content_id="answer:cross-contract",
        content_type=ZhihuContentType.ANSWERS,
        canonical_url="https://www.zhihu.com/question/1/answer/9",
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        body_object_sha256=objects.put_bytes(
            "<p>\u4e3a\u4ec0\u4e48\u4f30\u503c\u9700\u8981\u5b89\u5168\u8fb9\u9645\uff1f</p>"
            "<p>\u56e0\u4e3a\u73b0\u91d1\u6d41\u8d28\u91cf\u51b3\u5b9a\u4fdd\u5b88\u4ef7\u503c\u3002</p>".encode(
                "utf-8"
            )
        ).sha256,
        metadata_sha256=objects.put_json({"source": "cross-contract"}).sha256,
        raw_source_snapshot_id="snapshot:cross-contract",
        content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
    )
    run_id = "knowledge-semantic-run:cross-contract"
    content = paragraphize_zhihu_content(
        record,
        run_id=run_id,
        object_store=objects,
        config=config,
        keyword_terms=method_keyword_terms(
            load_distillation_rules(
                PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
            )
        ),
    )
    if tamper == "paragraph_author":
        content = replace(
            content,
            paragraphs=(
                content.paragraphs[0].model_copy(
                    update={"author_source_id": "zhihu:other-author"}
                ),
                *content.paragraphs[1:],
            ),
        )
    elif tamper == "relation_endpoint":
        content = replace(
            content,
            relations=(
                content.relations[0].model_copy(
                    update={"source_paragraph_id": "paragraph-unit:outside"}
                ),
                *content.relations[1:],
            ),
        )
    else:
        content = replace(
            content,
            argument_units=(
                content.argument_units[0].model_copy(update={"run_id": "other-run"}),
                *content.argument_units[1:],
            ),
        )
    run = SemanticFunnelRun(
        run_id=run_id,
        author_source_id=record.author_source_id,
        input_hashes=[record.body_object_sha256],
        input_manifest_sha256="1" * 64,
        pipeline_version=config.pipeline_version,
        paragraphizer_version=config.paragraphizer_version,
        role_rule_version=config.role_rule_version,
        relation_rule_version=config.relation_rule_version,
        argument_builder_version=config.argument_builder_version,
        keyword_rule_version=config.keyword_rule_version,
        rule_config_sha256="2" * 64,
        stage=SemanticRunStage.ARGUMENT_UNITS_BUILT,
        content_item_count=1,
        paragraph_count=len(content.paragraphs),
        argument_unit_count=len(content.argument_units),
        started_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    repository = SemanticFunnelRepository(state)
    with pytest.raises(ValueError, match="inconsistent|crosses"):
        repository.register_paragraphized(run, [content])
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_semantic_run").fetchone()[0] == 0


def test_semantic_pipeline_is_argument_aware_private_safe_and_idempotent(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    knowledge = KnowledgeRepository(state)
    body = objects.put_bytes(
        (
            "<p>为什么低估值不等于安全边际？</p>"
            "<p>因为估值必须结合现金流质量和增长持续性。</p>"
        ).encode()
    )
    metadata = objects.put_json({"source": "synthetic-integration"})
    record = ZhihuContentRecord(
        version_id="version:detail",
        author_source_id="zhihu:test-author",
        content_id="answer:detail",
        content_type=ZhihuContentType.ANSWERS,
        canonical_url="https://www.zhihu.com/question/1/answer/2",
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        body_object_sha256=body.sha256,
        metadata_sha256=metadata.sha256,
        raw_source_snapshot_id="snapshot:detail",
        content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
    )
    knowledge.register_content(record)
    listing_body = objects.put_bytes("<p>估值。</p>".encode())
    knowledge.register_content(
        record.model_copy(
            update={
                "version_id": "version:listing",
                "content_id": "answer:listing",
                "body_object_sha256": listing_body.sha256,
                "raw_source_snapshot_id": "snapshot:listing",
                "content_completeness": (
                    ZhihuContentCompleteness.LISTING_UNVERIFIED
                ),
            }
        )
    )
    repository = SemanticFunnelRepository(state)
    service = SemanticFunnelService(
        knowledge,
        repository,
        objects,
        load_semantic_funnel_config(
            PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
        ),
        load_distillation_rules(
            PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
        ),
    )

    plan = service.plan("zhihu:test-author")
    assert plan["eligible_content_item_count"] == 1
    assert plan["ignored_non_detail_count"] == 1
    reloaded_service = SemanticFunnelService(
        knowledge,
        repository,
        objects,
        load_semantic_funnel_config(
            PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
        ),
        load_distillation_rules(
            PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
        ),
    )
    assert reloaded_service.plan("zhihu:test-author")["input_manifest_sha256"] == plan[
        "input_manifest_sha256"
    ]
    assert reloaded_service.rule_config_sha256 == service.rule_config_sha256
    first = service.run("zhihu:test-author")
    second = service.run("zhihu:test-author")

    assert first == second
    assert first.run.stage is SemanticRunStage.ARGUMENT_UNITS_BUILT
    assert first.run.content_item_count == 1
    assert first.run.paragraph_count == 2
    assert first.run.argument_unit_count == 1
    assert first.candidate_item_count == 1
    assert first.excluded_item_count == 0
    with state.connect() as connection:
        paragraph_json = str(
            connection.execute(
                "SELECT unit_json FROM knowledge_paragraph_unit"
            ).fetchone()[0]
        )
        argument_json = str(
            connection.execute(
                "SELECT unit_json FROM knowledge_argument_unit"
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_argument_unit_paragraph_ref"
        ).fetchone()[0] == 2
    assert "低估值" not in paragraph_json
    assert "低估值" not in argument_json


def test_semantic_embedding_writes_only_complete_au_and_method_prototype_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    knowledge = KnowledgeRepository(state)
    body = objects.put_bytes(
        (
            "<p>为什么低估值不等于安全边际？</p>"
            "<p>因为估值必须结合现金流质量和增长持续性。</p>"
        ).encode()
    )
    metadata = objects.put_json({"source": "synthetic-embedding"})
    knowledge.register_content(
        ZhihuContentRecord(
            version_id="version:embedding",
            author_source_id="zhihu:test-author",
            content_id="answer:embedding",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url="https://www.zhihu.com/question/1/answer/3",
            question_title="\u4e3a\u4ec0\u4e48\u4f4e\u4f30\u503c\u4e0d\u7b49\u4e8e\u5b89\u5168\u8fb9\u9645\uff1f",
            collected_at=datetime(2026, 7, 22, tzinfo=UTC),
            body_object_sha256=body.sha256,
            metadata_sha256=metadata.sha256,
            raw_source_snapshot_id="snapshot:embedding",
            content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
        )
    )
    incomplete_body = objects.put_bytes("<p>\u4f30\u503c</p>".encode("utf-8"))
    incomplete_metadata = objects.put_json({"source": "synthetic-incomplete"})
    knowledge.register_content(
        ZhihuContentRecord(
            version_id="version:embedding-incomplete",
            author_source_id="zhihu:test-author",
            content_id="answer:embedding-incomplete",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url="https://www.zhihu.com/question/1/answer/5",
            collected_at=datetime(2026, 7, 22, tzinfo=UTC),
            body_object_sha256=incomplete_body.sha256,
            metadata_sha256=incomplete_metadata.sha256,
            raw_source_snapshot_id="snapshot:embedding-incomplete",
            content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
        )
    )
    excluded_body = objects.put_bytes(b"<p>weather and cooking only</p>")
    excluded_metadata = objects.put_json({"source": "synthetic-unrelated"})
    knowledge.register_content(
        ZhihuContentRecord(
            version_id="version:embedding-unrelated",
            author_source_id="zhihu:test-author",
            content_id="answer:embedding-unrelated",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url="https://www.zhihu.com/question/1/answer/4",
            collected_at=datetime(2026, 7, 22, tzinfo=UTC),
            body_object_sha256=excluded_body.sha256,
            metadata_sha256=excluded_metadata.sha256,
            raw_source_snapshot_id="snapshot:embedding-unrelated",
            content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
        )
    )
    repository = SemanticFunnelRepository(state)
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    argument_execution = SemanticFunnelService(
        knowledge,
        repository,
        objects,
        config,
        load_distillation_rules(
            PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
        ),
    ).run("zhihu:test-author")
    paragraph_groups = repository.paragraph_groups(argument_execution.run.run_id)
    candidate_paragraph_groups = repository.paragraph_groups(
        argument_execution.run.run_id,
        candidate_only=True,
    )
    assert len(paragraph_groups) == 3
    assert len(candidate_paragraph_groups) == 2
    active_arguments = [
        argument
        for argument in repository.argument_units(argument_execution.run.run_id)
        if argument.status.value != "DERIVED_EXCLUDED"
    ]
    complete_candidate_groups = _paragraph_groups_for_retained_arguments(
        repository,
        argument_execution.run.run_id,
        candidate_paragraph_groups,
        active_arguments,
    )
    assert complete_candidate_groups is candidate_paragraph_groups
    foreign_run_argument = active_arguments[0].model_copy(
        update={"run_id": "semantic-run:foreign"}
    )
    with pytest.raises(ValueError, match="another semantic run"):
        _paragraph_groups_for_retained_arguments(
            repository,
            argument_execution.run.run_id,
            candidate_paragraph_groups,
            [foreign_run_argument],
        )
    with pytest.raises(ValueError, match="crosses semantic run boundaries"):
        _argument_lineages([foreign_run_argument], candidate_paragraph_groups)
    first_item_id = next(iter(candidate_paragraph_groups))
    foreign_paragraph_groups = {
        item_id: [
            (
                paragraph.model_copy(update={"run_id": "semantic-run:foreign"})
                if item_id == first_item_id and index == 0
                else paragraph
            )
            for index, paragraph in enumerate(paragraphs)
        ]
        for item_id, paragraphs in candidate_paragraph_groups.items()
    }
    with pytest.raises(ValueError, match="paragraph group belongs"):
        _paragraph_groups_for_retained_arguments(
            repository,
            argument_execution.run.run_id,
            foreign_paragraph_groups,
            active_arguments,
        )
    lineages = _argument_lineages(active_arguments, candidate_paragraph_groups)
    assert any(
        len(lineage.source_snapshot_ids) == 1
        and len(lineage.source_object_sha256s) == 2
        for lineage in lineages.values()
    )
    multi_paragraph_argument = next(
        argument for argument in active_arguments if len(argument.paragraph_ids) > 1
    )
    referenced_item_id = next(
        item_id
        for item_id, paragraphs in candidate_paragraph_groups.items()
        if multi_paragraph_argument.paragraph_ids[0]
        in {paragraph.paragraph_id for paragraph in paragraphs}
    )
    other_item_paragraph = next(
        paragraph
        for item_id, paragraphs in candidate_paragraph_groups.items()
        if item_id != referenced_item_id
        for paragraph in paragraphs
    )
    lineage_tampers = [
        multi_paragraph_argument.model_copy(
            update={
                "paragraph_ids": [
                    "paragraph-unit:missing",
                    *multi_paragraph_argument.paragraph_ids[1:],
                ]
            }
        ),
        multi_paragraph_argument.model_copy(
            update={"source_snapshot_ids": ["snapshot:replacement"]}
        ),
        multi_paragraph_argument.model_copy(update={"content_id": "answer:replacement"}),
        multi_paragraph_argument.model_copy(
            update={
                "paragraph_ids": [
                    multi_paragraph_argument.paragraph_ids[0],
                    other_item_paragraph.paragraph_id,
                    *multi_paragraph_argument.paragraph_ids[2:],
                ]
            }
        ),
    ]
    unknown_lineage_groups = _paragraph_groups_for_retained_arguments(
        repository,
        argument_execution.run.run_id,
        candidate_paragraph_groups,
        [lineage_tampers[0]],
    )
    assert set(unknown_lineage_groups) == set(candidate_paragraph_groups)
    with pytest.raises(ValueError, match="lineage references a missing paragraph"):
        _argument_lineages([lineage_tampers[0]], unknown_lineage_groups)
    for tampered_argument in lineage_tampers:
        with pytest.raises(ValueError, match="lineage"):
            _argument_lineages([tampered_argument], candidate_paragraph_groups)
        assert not (tmp_path / "parquet").exists()
    window_seed = next(iter(candidate_paragraph_groups.values()))[0]
    window = [
        window_seed.model_copy(
            update={"paragraph_id": f"paragraph:window:{ordinal}", "ordinal": ordinal}
        )
        for ordinal in range(1, 5)
    ]
    assert local_context_paragraph_ids(window, 1) == [
        "paragraph:window:1",
        "paragraph:window:2",
        "paragraph:window:3",
    ]
    assert local_context_paragraph_ids(window, 2) == [
        "paragraph:window:1",
        "paragraph:window:2",
        "paragraph:window:3",
        "paragraph:window:4",
    ]
    assert local_context_paragraph_ids(window, 4) == [
        "paragraph:window:3",
        "paragraph:window:4",
    ]
    crossed_window = [
        *window[:3],
        window[3].model_copy(
            update={
                "content_id": "answer:other",
                "content_version_id": "version:other",
            }
        ),
    ]
    with pytest.raises(ValueError, match="SourceItem"):
        local_context_paragraph_ids(crossed_window, 2)
    fixed_texts: list[str] = [
        anchor
        for category in config.method_anchors.values()
        for anchor in category
    ]
    fixed_texts.extend(
        objects.get_bytes(argument.text_object_sha256).decode("utf-8")
        for argument in repository.argument_units(argument_execution.run.run_id)
    )
    for paragraph_group in candidate_paragraph_groups.values():
        paragraph_lookup = {paragraph.paragraph_id: paragraph for paragraph in paragraph_group}
        for paragraph in paragraph_group:
            paragraph_text = objects.get_bytes(paragraph.text_object_sha256).decode(
                "utf-8"
            )
            fixed_texts.append(paragraph_text)
            context_ids = local_context_paragraph_ids(
                paragraph_group,
                paragraph.ordinal,
            )
            context_text = "\n".join(
                f"[paragraph ordinal={paragraph_lookup[item].ordinal}] "
                f"{objects.get_bytes(paragraph_lookup[item].text_object_sha256).decode('utf-8')}"
                for item in context_ids
            )
            fixed_texts.append(context_text)
    files = {
        "model.safetensors": "1" * 64,
        "tokenizer.json": "2" * 64,
    }
    asset = LocalEmbeddingAssetManifest(
        model_id="recorded/test-model",
        model_revision="fixed",
        repository_url="https://example.invalid/offline-fixture",
        license_id="TEST_ONLY",
        dimension=3,
        maximum_model_tokens=32,
        files=files,
        bundle_sha256=content_hash(files),
    )
    refusing_backend = RefusingEmbeddingBackend()
    foreign_parquet_root = tmp_path / "foreign-run-parquet"

    def foreign_argument_units(_run_id: str):
        return [foreign_run_argument]

    with monkeypatch.context() as patch:
        patch.setattr(repository, "argument_units", foreign_argument_units)
        with pytest.raises(ValueError, match="another semantic run"):
            SemanticEmbeddingService(
                repository,
                objects,
                ParquetSemanticStore(foreign_parquet_root),
                config,
                refusing_backend,
                asset,
            ).run(argument_execution.run.run_id)
    assert refusing_backend.calls == 0
    assert not foreign_parquet_root.exists()
    backend = RecordedEmbeddingBackend(
        {text: (1.0, 0.0, 0.0) for text in fixed_texts}
    )
    embedding_service = SemanticEmbeddingService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        config,
        backend,
        asset,
    )
    execution = embedding_service.run(argument_execution.run.run_id)

    views = set(
        pq.ParquetFile(execution.parquet.vectors_path)
        .read(columns=["view"])
        .column(0)
        .to_pylist()
    )
    assert views == {
        "PARAGRAPH_CURRENT",
        "PARAGRAPH_LOCAL_CONTEXT",
        "ARGUMENT_UNIT",
        "METHOD_PROTOTYPE",
    }
    assert execution.manifest.embedding_views == [
        SemanticEmbeddingView.PARAGRAPH_CURRENT,
        SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
        SemanticEmbeddingView.ARGUMENT_UNIT,
        SemanticEmbeddingView.METHOD_PROTOTYPE,
    ]
    assert execution.manifest.auxiliary_views == [
        SemanticEmbeddingView.PARAGRAPH_CURRENT,
        SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
    ]
    assert execution.manifest.decision_view == SemanticEmbeddingView.ARGUMENT_UNIT
    assert execution.manifest.method_prototype_count == 14
    assert execution.manifest.embedding_contract_version is (
        SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
    )
    vector_rows = (
        pq.ParquetFile(execution.parquet.vectors_path)
        .read(
            columns=[
                "view",
                "entity_id",
                "item_id",
                "content_id",
                "source_snapshot_ids",
                "source_object_sha256s",
                "input_object_sha256",
            ]
        )
        .to_pylist()
    )
    candidate_paragraph_count = sum(
        len(paragraphs) for paragraphs in candidate_paragraph_groups.values()
    )
    assert (
        sum(row["view"] == "PARAGRAPH_CURRENT" for row in vector_rows)
        == candidate_paragraph_count
    )
    assert sum(
        row["view"] == "PARAGRAPH_LOCAL_CONTEXT" for row in vector_rows
    ) == candidate_paragraph_count
    assert sum(row["view"] == "METHOD_PROTOTYPE" for row in vector_rows) == 14
    expected_paragraphs = {
        paragraph.paragraph_id: (item_id, paragraph)
        for item_id, paragraphs in candidate_paragraph_groups.items()
        for paragraph in paragraphs
    }
    for view in ("PARAGRAPH_CURRENT", "PARAGRAPH_LOCAL_CONTEXT"):
        rows = [row for row in vector_rows if row["view"] == view]
        assert {row["entity_id"] for row in rows} == set(expected_paragraphs)
        for row in rows:
            item_id, paragraph = expected_paragraphs[row["entity_id"]]
            assert row["item_id"] == item_id
            assert row["content_id"] == paragraph.content_id
            assert row["source_snapshot_ids"] == [
                paragraph.locator.source_snapshot_id
            ]
            assert row["source_object_sha256s"] == [
                paragraph.locator.source_object_sha256
            ]
            assert len(row["input_object_sha256"]) == 64
    assert execution.vector_count == (
        2 * len(expected_paragraphs) + execution.score_count + 14
    )
    assert execution.score_count == len(active_arguments)
    assert execution.keep_count == len(active_arguments)
    score_rows = pq.ParquetFile(execution.parquet.scores_path).read().to_pylist()
    expected_categories = sorted(category.value for category in BookMethodCategory)
    assert all(
        sorted(json.loads(row["category_scores_json"])) == expected_categories
        and row["selected_categories"] == expected_categories
        for row in score_rows
    )
    first_argument = next(
        argument
        for argument in repository.argument_units(argument_execution.run.run_id)
        if argument.argument_unit_id
        in {row["argument_unit_id"] for row in score_rows}
    )
    low_score = embedding_service._score_argument(
        first_argument,
        EncodedText(vector=(0.0, 1.0, 0.0), token_count=1, chunk_count=1),
        execution.manifest,
        {category: (1.0, 0.0, 0.0) for category in BookMethodCategory},
    )
    assert low_score.selected_categories == []
    assert low_score.decision is SemanticScreenDecision.CALIBRATION_REQUIRED
    complete_fixture_output = (
        execution.vector_count,
        execution.score_count,
        execution.parquet.vectors_sha256,
        execution.parquet.scores_sha256,
    )
    repeated_embedding = SemanticEmbeddingService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        config,
        backend,
        asset,
    ).run(argument_execution.run.run_id)
    assert repeated_embedding == execution
    assert (
        repeated_embedding.vector_count,
        repeated_embedding.score_count,
        repeated_embedding.parquet.vectors_sha256,
        repeated_embedding.parquet.scores_sha256,
    ) == complete_fixture_output
    changed_backend = RecordedEmbeddingBackend(
        {text: (0.0, 1.0, 0.0) for text in fixed_texts}
    )
    with pytest.raises(ValueError, match="Parquet collision"):
        SemanticEmbeddingService(
            repository,
            objects,
            ParquetSemanticStore(tmp_path / "parquet"),
            config,
            changed_backend,
            asset,
        ).run(argument_execution.run.run_id)
    registration = repository.embedding_registration(argument_execution.run.run_id)
    assert registration is not None
    with pytest.raises(ValueError, match="embedding collision"):
        repository.register_embedding(
            argument_execution.run,
            registration.manifest,
            vector_parquet_hash="f" * 64,
            score_parquet_hash=registration.score_parquet_sha256,
            manifest_object_hash=registration.manifest_object_sha256,
        )
    score = (
        pq.ParquetFile(execution.parquet.scores_path)
        .read(columns=["topic_relevance", "methodological_completeness"])
        .to_pylist()[0]
    )
    assert score["topic_relevance"] == 1.0
    assert 0.0 <= score["methodological_completeness"] <= 1.0
    persisted = repository.get_run(argument_execution.run.run_id)
    assert persisted is not None
    assert persisted.stage is SemanticRunStage.EMBEDDING_SCREENED

    packet = SemanticPacketService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        tmp_path / "runtime",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).export(argument_execution.run.run_id)
    lines = [
        json.loads(line)
        for line in packet.packet_file.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 1
    assert (
        lines[0]["packet_contract_version"]
        == SemanticPacketContract.COMPLETE_ARGUMENT_UNIT_V2.value
    )
    assert lines[0]["argument_text"]
    packet_argument = next(
        argument
        for argument in active_arguments
        if argument.argument_unit_id == lines[0]["argument_unit_id"]
    )
    assert len(lines[0]["paragraphs"]) == len(packet_argument.paragraph_ids)
    assert lines[0]["argument_unit_id"].startswith("argument-unit:")
    assert all(item["text"] for item in lines[0]["paragraphs"])
    assert all(item["locator"] for item in lines[0]["paragraphs"])
    assert all(item["paragraph_kind"] == "TEXT" for item in lines[0]["paragraphs"])
    assert all(item["visual_evidence_ids"] == [] for item in lines[0]["paragraphs"])
    assert all(item["visual_chart_unit_ids"] == [] for item in lines[0]["paragraphs"])
    assert packet.batch.exported_argument_count == 1
    assert packet.batch.local_only is True
    assert packet.held_back_calibration_count == 0
    assert packet.held_back_structural_count == 1
    assert packet.held_back_oversize_count == 0
    forged_import = packet.batch.model_copy(
        update={
            "status": SemanticLlmBatchStatus.IMPORTED,
            "imported_result_count": packet.batch.exported_argument_count,
        }
    )
    with pytest.raises(ValueError, match="complete staged batch"):
        repository.import_candidates(forged_import, [])
    with pytest.raises(ValueError, match="persisted semantic batch"):
        repository.finalize_imported_batch(forged_import)
    repeated_packet = SemanticPacketService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        tmp_path / "runtime",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).export(argument_execution.run.run_id)
    assert repeated_packet.batch.batch_id == packet.batch.batch_id
    assert repeated_packet.batch.packet_object_sha256 == packet.batch.packet_object_sha256
    assert repeated_packet.packet_file.read_bytes() == packet.packet_file.read_bytes()
    persisted = repository.get_run(argument_execution.run.run_id)
    assert persisted is not None
    assert persisted.stage is SemanticRunStage.DEEPSEEK_PACKET_READY

    result_file = packet.batch_directory / "deepseek-results.jsonl"
    result_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "argument_unit_id": lines[0]["argument_unit_id"],
                "input_sha256": lines[0]["input_sha256"],
                "decision": "KEEP",
                "method_categories": ["VALUATION"],
                "reason_codes": ["COMPLETE_VALUATION_ARGUMENT"],
                "confidence": 0.8,
                "candidates": [
                    {
                        "schema_version": "1.0",
                        "title": "估值必须与现金流质量共同验证",
                        "method_summary": "低估值只有在现金流质量可验证时才可能形成安全边际。",
                        "applicability": ["公司估值研究"],
                        "counterevidence": ["现金流持续弱于利润"],
                        "invalidation_conditions": ["现金流质量无法验证"],
                        "evidence_paragraph_ids": [
                            item["paragraph_id"] for item in lines[0]["paragraphs"]
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    valid_payload = json.loads(result_file.read_text(encoding="utf-8"))
    unknown = json.loads(json.dumps(valid_payload))
    unknown["argument_unit_id"] = "argument-unit:unknown"
    wrong_hash = json.loads(json.dumps(valid_payload))
    wrong_hash["input_sha256"] = "0" * 64
    extra_field = json.loads(json.dumps(valid_payload))
    extra_field["unexpected"] = True
    outside_reference = json.loads(json.dumps(valid_payload))
    packet_paragraph_ids = {
        item["paragraph_id"] for item in lines[0]["paragraphs"]
    }
    cross_argument_paragraph_id = next(
        paragraph_id
        for paragraph_id in {
            paragraph.paragraph_id
            for paragraphs in paragraph_groups.values()
            for paragraph in paragraphs
        }
        if paragraph_id not in packet_paragraph_ids
    )
    outside_reference["candidates"][0]["evidence_paragraph_ids"] = [
        cross_argument_paragraph_id
    ]
    invalid_results = {
        "missing": b"",
        "duplicate": (
            json.dumps(valid_payload, ensure_ascii=False)
            + "\n"
            + json.dumps(valid_payload, ensure_ascii=False)
            + "\n"
        ).encode("utf-8"),
        "unknown": (json.dumps(unknown, ensure_ascii=False) + "\n").encode("utf-8"),
        "wrong_hash": (json.dumps(wrong_hash, ensure_ascii=False) + "\n").encode(
            "utf-8"
        ),
        "extra_field": (json.dumps(extra_field, ensure_ascii=False) + "\n").encode(
            "utf-8"
        ),
        "outside_reference": (
            json.dumps(outside_reference, ensure_ascii=False) + "\n"
        ).encode("utf-8"),
    }
    with state.connect() as connection:
        original_batch_json = str(
            connection.execute(
                "SELECT batch_json FROM knowledge_llm_batch WHERE batch_id=?",
                (packet.batch.batch_id,),
            ).fetchone()[0]
        )
    for name, invalid_bytes in invalid_results.items():
        invalid_file = packet.batch_directory / f"invalid-{name}.jsonl"
        invalid_file.write_bytes(invalid_bytes)
        with pytest.raises(ValueError):
            SemanticPacketService(
                repository,
                objects,
                ParquetSemanticStore(tmp_path / "parquet"),
                tmp_path / "runtime",
                PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
            ).stage_results(packet.batch.batch_id, invalid_file)
        with state.connect() as connection:
            assert (
                connection.execute(
                    "SELECT batch_json FROM knowledge_llm_batch WHERE batch_id=?",
                    (packet.batch.batch_id,),
                ).fetchone()[0]
                == original_batch_json
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM knowledge_semantic_candidate"
            ).fetchone()[0] == 0
    staged = SemanticPacketService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        tmp_path / "runtime",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).stage_results(packet.batch.batch_id, result_file)
    assert staged.status is SemanticLlmBatchStatus.RESULT_STAGED
    repeated_stage = SemanticPacketService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        tmp_path / "runtime",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).stage_results(packet.batch.batch_id, result_file)
    assert repeated_stage == staged
    changed_result = packet.batch_directory / "changed-results.jsonl"
    changed_payload = json.loads(result_file.read_text(encoding="utf-8"))
    changed_payload["confidence"] = 0.7
    changed_result.write_text(
        json.dumps(changed_payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="immutable"):
        SemanticPacketService(
            repository,
            objects,
            ParquetSemanticStore(tmp_path / "parquet"),
            tmp_path / "runtime",
            PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
        ).stage_results(packet.batch.batch_id, changed_result)
    changed_prompt = tmp_path / "changed-prompt.md"
    changed_prompt.write_text("changed contract", encoding="utf-8")
    with pytest.raises(ValueError, match="prompt or schema contract changed"):
        SemanticPacketService(
            repository,
            objects,
            ParquetSemanticStore(tmp_path / "parquet"),
            tmp_path / "runtime",
            changed_prompt,
        ).import_results(packet.batch.batch_id)
    imported, candidate_count = SemanticPacketService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        tmp_path / "runtime",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).import_results(packet.batch.batch_id)
    assert imported.status is SemanticLlmBatchStatus.IMPORTED
    assert candidate_count == 1
    repeated_import, repeated_candidate_count = SemanticPacketService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        tmp_path / "runtime",
        PROJECT_ROOT / "OPENCODE_DEEPSEEK_PROMPT.md",
    ).import_results(packet.batch.batch_id)
    assert repeated_import == imported
    assert repeated_candidate_count == 1
    with state.connect() as connection:
        candidate_row = connection.execute(
            "SELECT candidate_json,llm_batch_id,llm_response_object_hash "
            "FROM knowledge_semantic_candidate"
        ).fetchone()
        candidate_json = str(candidate_row["candidate_json"])
        assert candidate_row["llm_batch_id"] == packet.batch.batch_id
        assert candidate_row["llm_response_object_hash"] == staged.response_object_sha256
    assert "现金流质量" not in candidate_json
    persisted = repository.get_run(argument_execution.run.run_id)
    assert persisted is not None
    assert persisted.stage is SemanticRunStage.CANDIDATES_GENERATED
