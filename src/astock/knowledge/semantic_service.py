"""Deterministic orchestration for paragraph and argument-unit construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.knowledge.repository import KnowledgeRepository
from astock.knowledge.semantic_funnel import (
    ParagraphizedContent,
    method_keyword_terms,
    paragraphize_zhihu_content,
)
from astock.knowledge.semantic_repository import SemanticFunnelRepository
from astock.schemas import (
    DistillationClassRuleSet,
    KeywordScreenDecision,
    SemanticEmbeddingContract,
    SemanticFunnelConfig,
    SemanticFunnelRun,
    SemanticRunStage,
    ZhihuContentRecord,
    ZhihuContentType,
)


@dataclass(frozen=True, slots=True)
class SemanticFunnelExecution:
    run: SemanticFunnelRun
    candidate_item_count: int
    excluded_item_count: int
    ready_argument_count: int
    review_argument_count: int
    excluded_argument_count: int


class SemanticFunnelService:
    """Build the zero-cost argument layer before local semantic screening."""

    def __init__(
        self,
        knowledge_repository: KnowledgeRepository,
        semantic_repository: SemanticFunnelRepository,
        object_store: ObjectStore,
        config: SemanticFunnelConfig,
        keyword_rules: DistillationClassRuleSet,
    ) -> None:
        self.knowledge_repository = knowledge_repository
        self.semantic_repository = semantic_repository
        self.object_store = object_store
        self.config = config
        if config.embedding_contract_version is not (
            SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
        ):
            raise ValueError("legacy semantic funnel contracts are read-only")
        self.keyword_rules = keyword_rules
        self.keyword_terms = method_keyword_terms(keyword_rules)
        self.rule_config_sha256 = content_hash(
            {
                "semantic_funnel_config": config.model_dump(
                    mode="json", exclude={"created_at"}
                ),
                "keyword_rules": keyword_rules.model_dump(
                    mode="json", exclude={"created_at"}
                ),
            }
        )

    def plan(self, author_source_id: str) -> dict[str, object]:
        records, ignored = self._eligible_records(author_source_id)
        manifest = self._input_manifest(author_source_id, records)
        return {
            "author_source_id": author_source_id,
            "eligible_content_item_count": len(records),
            "ignored_non_detail_count": ignored,
            "content_types": [
                ZhihuContentType.ANSWERS.value,
                ZhihuContentType.ARTICLES.value,
                ZhihuContentType.THOUGHTS.value,
            ],
            "pipeline_version": self.config.pipeline_version,
            "embedding_contract_version": self.config.embedding_contract_version,
            "input_manifest_sha256": content_hash(manifest),
        }

    def run(self, author_source_id: str) -> SemanticFunnelExecution:
        records, _ = self._eligible_records(author_source_id)
        if not records:
            raise ValueError(
                f"no DETAIL_VERIFIED answers, articles or thoughts for {author_source_id}"
            )
        manifest = self._input_manifest(author_source_id, records)
        manifest_object = self.object_store.put_json(manifest)
        run_identity = {
            "author_source_id": author_source_id,
            "input_manifest_sha256": manifest_object.sha256,
            "pipeline_version": self.config.pipeline_version,
            "paragraphizer_version": self.config.paragraphizer_version,
            "role_rule_version": self.config.role_rule_version,
            "relation_rule_version": self.config.relation_rule_version,
            "argument_builder_version": self.config.argument_builder_version,
            "keyword_rule_version": self.config.keyword_rule_version,
            "embedding_contract_version": self.config.embedding_contract_version,
        }
        run_id = f"knowledge-semantic-run:{content_hash(run_identity)}"
        existing = self.semantic_repository.get_run(run_id)
        if existing is not None and existing.stage in {
            SemanticRunStage.ARGUMENT_UNITS_BUILT,
            SemanticRunStage.EMBEDDING_READY,
            SemanticRunStage.EMBEDDING_SCREENED,
            SemanticRunStage.DEEPSEEK_PACKET_READY,
            SemanticRunStage.DEEPSEEK_RESULT_STAGED,
            SemanticRunStage.IMPORT_VALIDATED,
            SemanticRunStage.CANDIDATES_GENERATED,
            SemanticRunStage.AUDITED,
            SemanticRunStage.PENDING_HUMAN_REVIEW,
        }:
            counts = self.semantic_repository.counts(run_id)
            expected = {
                "content_item_count": existing.content_item_count,
                "paragraph_count": existing.paragraph_count,
                "argument_unit_count": existing.argument_unit_count,
            }
            if counts != expected:
                raise ValueError("persisted semantic stage counters do not match SQLite")
            return self._execution_from_repository(existing)
        started_at = existing.started_at if existing else datetime.now(UTC)
        input_hashes = sorted(
            {
                digest
                for record in records
                for digest in (record.body_object_sha256, record.metadata_sha256)
            }
        )
        frozen = SemanticFunnelRun(
            run_id=run_id,
            author_source_id=author_source_id,
            input_hashes=input_hashes,
            input_manifest_sha256=manifest_object.sha256,
            pipeline_version=self.config.pipeline_version,
            paragraphizer_version=self.config.paragraphizer_version,
            role_rule_version=self.config.role_rule_version,
            relation_rule_version=self.config.relation_rule_version,
            argument_builder_version=self.config.argument_builder_version,
            keyword_rule_version=self.config.keyword_rule_version,
            embedding_contract_version=self.config.embedding_contract_version,
            rule_config_sha256=self.rule_config_sha256,
            stage=SemanticRunStage.INPUT_FROZEN,
            content_item_count=0,
            paragraph_count=0,
            argument_unit_count=0,
            started_at=started_at,
        )
        self.semantic_repository.save_run(frozen)
        contents = [
            paragraphize_zhihu_content(
                record,
                run_id=run_id,
                object_store=self.object_store,
                config=self.config,
                keyword_terms=self.keyword_terms,
            )
            for record in records
        ]
        completed = frozen.model_copy(
            update={
                "stage": SemanticRunStage.ARGUMENT_UNITS_BUILT,
                "content_item_count": len(contents),
                "paragraph_count": sum(len(content.paragraphs) for content in contents),
                "argument_unit_count": sum(
                    len(content.argument_units) for content in contents
                ),
            }
        )
        self.semantic_repository.register_paragraphized(completed, contents)
        return _execution(completed, contents)

    def _eligible_records(
        self,
        author_source_id: str,
    ) -> tuple[list[ZhihuContentRecord], int]:
        records: list[ZhihuContentRecord] = []
        ignored = 0
        for content_type in (
            ZhihuContentType.ANSWERS,
            ZhihuContentType.ARTICLES,
            ZhihuContentType.THOUGHTS,
        ):
            latest_any = self.knowledge_repository.latest_content_records(
                author_source_id,
                content_type,
            )
            latest_detail = self.knowledge_repository.latest_detail_content_records(
                author_source_id,
                content_type,
            )
            records.extend(latest_detail)
            ignored += len(latest_any) - len(latest_detail)
        return (
            sorted(
                records,
                key=lambda record: (
                    record.content_type.value,
                    record.content_id,
                    record.version_id,
                ),
            ),
            ignored,
        )

    def _input_manifest(
        self,
        author_source_id: str,
        records: list[ZhihuContentRecord],
    ) -> dict[str, object]:
        return {
            "schema_version": "knowledge-semantic-input-v3",
            "author_source_id": author_source_id,
            "content_policy": "DETAIL_VERIFIED_ANSWERS_ARTICLES_THOUGHTS_ONLY",
            "embedding_contract_version": self.config.embedding_contract_version,
            "rule_config_sha256": self.rule_config_sha256,
            "records": [
                {
                    "content_type": record.content_type.value,
                    "content_id": record.content_id,
                    "version_id": record.version_id,
                    "source_snapshot_id": record.raw_source_snapshot_id,
                    "body_object_sha256": record.body_object_sha256,
                    "metadata_sha256": record.metadata_sha256,
                }
                for record in records
            ],
        }

    def _execution_from_repository(
        self,
        run: SemanticFunnelRun,
    ) -> SemanticFunnelExecution:
        summary = self.semantic_repository.summary(run.run_id)
        return SemanticFunnelExecution(run=run, **summary)


def _execution(
    run: SemanticFunnelRun,
    contents: list[ParagraphizedContent],
) -> SemanticFunnelExecution:
    return SemanticFunnelExecution(
        run=run,
        candidate_item_count=sum(
            content.screen.decision is KeywordScreenDecision.CANDIDATE
            for content in contents
        ),
        excluded_item_count=sum(
            content.screen.decision is KeywordScreenDecision.EXCLUDED_DERIVED
            for content in contents
        ),
        ready_argument_count=sum(
            argument.status.value == "READY"
            for content in contents
            for argument in content.argument_units
        ),
        review_argument_count=sum(
            argument.status.value == "NEEDS_REVIEW"
            for content in contents
            for argument in content.argument_units
        ),
        excluded_argument_count=sum(
            argument.status.value == "DERIVED_EXCLUDED"
            for content in contents
            for argument in content.argument_units
        ),
    )


__all__ = ["SemanticFunnelExecution", "SemanticFunnelService"]
