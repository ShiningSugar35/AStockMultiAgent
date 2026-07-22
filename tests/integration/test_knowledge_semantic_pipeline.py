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
from astock.schemas import (
    LocalEmbeddingAssetManifest,
    SemanticFunnelRun,
    SemanticLlmBatchStatus,
    SemanticRunStage,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    assert plan["comments_included"] is False
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


def test_semantic_embedding_writes_three_required_views_and_keeps_scores_separate(
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
    metadata = objects.put_json({"source": "synthetic-embedding"})
    knowledge.register_content(
        ZhihuContentRecord(
            version_id="version:embedding",
            author_source_id="zhihu:test-author",
            content_id="answer:embedding",
            content_type=ZhihuContentType.ANSWERS,
            canonical_url="https://www.zhihu.com/question/1/answer/3",
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
    fixed_texts: list[str] = [
        anchor
        for category in config.method_anchors.values()
        for anchor in category
    ]
    for paragraphs in paragraph_groups.values():
        by_id = {paragraph.paragraph_id: paragraph for paragraph in paragraphs}
        fixed_texts.extend(
            objects.get_bytes(paragraph.text_object_sha256).decode("utf-8")
            for paragraph in paragraphs
        )
        for paragraph in paragraphs:
            fixed_texts.append(
                "\n".join(
                    f"[{by_id[paragraph_id].ordinal}] "
                    f"{objects.get_bytes(by_id[paragraph_id].text_object_sha256).decode('utf-8')}"
                    for paragraph_id in local_context_paragraph_ids(
                        paragraphs,
                        paragraph.ordinal,
                    )
                )
            )
    fixed_texts.extend(
        objects.get_bytes(argument.text_object_sha256).decode("utf-8")
        for argument in repository.argument_units(argument_execution.run.run_id)
    )
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
    backend = RecordedEmbeddingBackend(
        {text: (1.0, 0.0, 0.0) for text in fixed_texts}
    )
    execution = SemanticEmbeddingService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        config,
        backend,
        asset,
    ).run(argument_execution.run.run_id)

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
    assert execution.vector_count == 22
    assert execution.score_count == 2
    assert execution.keep_count == 2
    repeated_embedding = SemanticEmbeddingService(
        repository,
        objects,
        ParquetSemanticStore(tmp_path / "parquet"),
        config,
        backend,
        asset,
    ).run(argument_execution.run.run_id)
    assert repeated_embedding == execution
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
    assert len(lines[0]["paragraphs"]) == 2
    assert lines[0]["argument_unit_id"].startswith("argument-unit:")
    assert all(item["text"] for item in lines[0]["paragraphs"])
    assert packet.batch.exported_argument_count == 1
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
    outside_reference["candidates"][0]["evidence_paragraph_ids"] = [
        "paragraph-unit:outside"
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
