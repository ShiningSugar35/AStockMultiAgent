"""Apply human review and distill a source-bounded, shadow-only skill library."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from astock.core.hashing import content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.review_workbook import (
    EXPECTED_REVIEW_RECORD_COUNT,
    ParsedReviewConclusion,
    interpret_review_conclusion,
    parse_review_workbook,
)
from astock.knowledge.reviewed_repository import (
    ReviewedKnowledgeRepository,
    ReviewedResult,
    SourceParagraphRecord,
)
from astock.knowledge.reviewed_storage import (
    ReviewedParquetStore,
    ReviewedScoreRow,
    ReviewedVectorRow,
)
from astock.knowledge.semantic_embedding import EmbeddingBackend
from astock.schemas import (
    ArgumentRelationType,
    ArgumentUnit,
    BookMethodCategory,
    CandidateSelectionCategory,
    CandidateSelectionSkill,
    LocalEmbeddingAssetManifest,
    MethodRule,
    PositionLifecycleCategory,
    PositionLifecycleSkill,
    ReviewApplicationStatus,
    ReviewArgumentTarget,
    ReviewDecision,
    ReviewedArgumentRelation,
    ReviewedArgumentStatus,
    ReviewedArgumentUnit,
    ReviewedAuthorSkillCoverage,
    ReviewedCoverageReport,
    ReviewedEmbeddingManifest,
    ReviewedParagraphRef,
    ReviewedRunStage,
    ReviewedSemanticRun,
    ReviewedShadowBundle,
    ReviewedSkillStatus,
    ReviewedSourceRef,
    ReviewParagraphRange,
    ReviewVerdict,
    RhetoricalRole,
    SemanticFunnelConfig,
    SemanticFunnelRun,
    SourceCoverageState,
    ViewpointCard,
)

PIPELINE_VERSION = "reviewed-book-skill-distillation-v2"
INSUFFICIENT_SOURCE = "INSUFFICIENT_SOURCE"
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；])\s*|\n+")
_SPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[\s，。；：、“”‘’《》（）()【】\[\]…\.]")
_PAGE_SUFFIX = re.compile(r":page:(\d+)$")

_TOPIC_METHOD_KEYWORDS: dict[BookMethodCategory, tuple[str, ...]] = {
    BookMethodCategory.STOCK_SELECTION: ("选股", "筛选", "候选", "标的"),
    BookMethodCategory.BUSINESS_MODEL: ("商业模式", "护城河", "竞争优势", "赚钱"),
    BookMethodCategory.INDUSTRY: ("行业", "产业链", "供需", "竞争格局", "市场份额"),
    BookMethodCategory.VALUATION: ("估值", "安全边际", "市盈率", "市净率"),
    BookMethodCategory.FINANCIAL_QUALITY: (
        "财务",
        "现金流",
        "利润质量",
        "负债",
        "应收",
        "存货",
        "roe",
    ),
    BookMethodCategory.ENTRY: ("建仓", "买入", "入场"),
    BookMethodCategory.HOLDING: ("持仓", "持有", "验证"),
    BookMethodCategory.ADD: ("加仓",),
    BookMethodCategory.TRIM: ("减仓", "降低仓位"),
    BookMethodCategory.EXIT: ("退出", "卖出", "清仓"),
    BookMethodCategory.RISK: ("风险", "止损", "回撤", "仓位", "安全"),
    BookMethodCategory.FAILURE_CASE: ("失败", "错误", "踩雷", "误区"),
    BookMethodCategory.COUNTEREVIDENCE_INVALIDATION: (
        "反证",
        "证伪",
        "失效",
        "不成立",
    ),
    BookMethodCategory.REVIEW: ("复盘", "回顾", "总结"),
}

_FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "conditions": ("如果", "当", "在", "适用", "条件", "前提", "只有", "除非"),
    "evidence": (
        "数据",
        "公告",
        "财报",
        "现金流",
        "收入",
        "利润",
        "负债",
        "价格",
        "销量",
        "份额",
        "图",
        "表",
        "验证",
    ),
    "positive": (
        "增长",
        "改善",
        "低估",
        "优势",
        "上升",
        "增加",
        "超预期",
        "安全边际",
        "确定性",
    ),
    "negative": (
        "下降",
        "恶化",
        "高估",
        "亏损",
        "负债",
        "风险",
        "回撤",
        "不及",
        "下滑",
        "减少",
    ),
    "invalidation": (
        "失效",
        "证伪",
        "不成立",
        "跌破",
        "破坏",
        "退出",
        "卖出",
        "止损",
    ),
    "failure": (
        "失败",
        "错误",
        "误区",
        "踩雷",
        "教训",
        "侥幸",
        "陷阱",
        "风险",
    ),
    "industry": (
        "银行",
        "黄金",
        "消费",
        "医药",
        "制造",
        "地产",
        "保险",
        "证券",
        "科技",
        "新能源",
        "零售",
        "周期",
        "资源",
    ),
    "horizon": ("长期", "中期", "短期", "年", "季度", "月", "周期", "时间"),
}

_CANDIDATE_MAP: dict[CandidateSelectionCategory, tuple[BookMethodCategory, ...]] = {
    CandidateSelectionCategory.BUSINESS_MODEL: (BookMethodCategory.BUSINESS_MODEL,),
    CandidateSelectionCategory.INDUSTRY_AND_VALUE_CHAIN: (BookMethodCategory.INDUSTRY,),
    CandidateSelectionCategory.FINANCIAL_QUALITY: (BookMethodCategory.FINANCIAL_QUALITY,),
    CandidateSelectionCategory.VALUATION: (BookMethodCategory.VALUATION,),
    CandidateSelectionCategory.STOCK_SELECTION: (BookMethodCategory.STOCK_SELECTION,),
    CandidateSelectionCategory.CATALYST: (),
    CandidateSelectionCategory.COUNTEREVIDENCE: (
        BookMethodCategory.COUNTEREVIDENCE_INVALIDATION,
        BookMethodCategory.FAILURE_CASE,
    ),
    CandidateSelectionCategory.RISK: (BookMethodCategory.RISK,),
}

_LIFECYCLE_MAP: dict[PositionLifecycleCategory, tuple[BookMethodCategory, ...]] = {
    PositionLifecycleCategory.ENTRY: (BookMethodCategory.ENTRY,),
    PositionLifecycleCategory.STAGED_ENTRY: (),
    PositionLifecycleCategory.HOLDING_VALIDATION: (BookMethodCategory.HOLDING,),
    PositionLifecycleCategory.ADD: (BookMethodCategory.ADD,),
    PositionLifecycleCategory.TRIM: (BookMethodCategory.TRIM,),
    PositionLifecycleCategory.EXIT: (BookMethodCategory.EXIT,),
    PositionLifecycleCategory.TIME_STOP: (),
    PositionLifecycleCategory.PRICE_STOP: (),
    PositionLifecycleCategory.THESIS_STOP: (BookMethodCategory.COUNTEREVIDENCE_INVALIDATION,),
    PositionLifecycleCategory.REVIEW: (BookMethodCategory.REVIEW,),
}

_LEXICAL_SKILL_TERMS: dict[
    CandidateSelectionCategory | PositionLifecycleCategory,
    tuple[str, ...],
] = {
    CandidateSelectionCategory.CATALYST: ("催化", "拐点", "政策信号", "事件驱动"),
    PositionLifecycleCategory.STAGED_ENTRY: ("分批建仓", "分批买入", "逐步建仓"),
    PositionLifecycleCategory.TIME_STOP: ("时间止损", "期限止损", "时间成本"),
    PositionLifecycleCategory.PRICE_STOP: ("价格止损", "止损价", "跌破", "回撤止损"),
    PositionLifecycleCategory.THESIS_STOP: (
        "论点止损",
        "逻辑失效",
        "假设不成立",
        "证伪",
    ),
}


@dataclass(frozen=True, slots=True)
class ReviewedExecution:
    result: ReviewedResult
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class _ArgumentDraft:
    decision_ids: tuple[str, ...]
    source_argument_ids: tuple[str, ...]
    title: str
    topics: tuple[str, ...]
    paragraphs: tuple[SourceParagraphRecord, ...]
    text: str
    text_object_sha256: str
    visual_conflict: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RuleDraft:
    argument: ReviewedArgumentUnit
    text: str
    source_ref: ReviewedSourceRef
    decision_question: str
    applicable_conditions: tuple[str, ...]
    reasoning_steps: tuple[str, ...]
    required_evidence: tuple[str, ...]
    positive_signals: tuple[str, ...]
    negative_signals: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    known_failure_modes: tuple[str, ...]
    applicable_industries: tuple[str, ...]
    holding_horizon: tuple[str, ...]
    uncertainty_reason: tuple[str, ...]
    status: ReviewedSkillStatus


class ReviewedBookSkillService:
    def __init__(
        self,
        *,
        state: StateStore,
        object_store: ObjectStore,
        parquet_store: ReviewedParquetStore,
        semantic_config: SemanticFunnelConfig,
        embedding_backend: EmbeddingBackend,
        embedding_asset: LocalEmbeddingAssetManifest,
    ) -> None:
        if embedding_backend.dimension != embedding_asset.dimension:
            raise ValueError("reviewed embedding backend dimension mismatch")
        self.state = state
        self.object_store = object_store
        self.repository = ReviewedKnowledgeRepository(state, object_store)
        self.parquet_store = parquet_store
        self.semantic_config = semantic_config
        self.embedding_backend = embedding_backend
        self.embedding_asset = embedding_asset

    def run(
        self,
        *,
        review_workbook: Path,
        source_pdf: Path,
        source_run_id: str,
    ) -> ReviewedExecution:
        records = parse_review_workbook(review_workbook)
        workbook_sha = sha256_bytes(review_workbook.read_bytes())
        pdf_sha = sha256_bytes(source_pdf.read_bytes())
        source_run = SemanticFunnelRun.model_validate_json(
            self.repository.source_run_json(source_run_id)
        )
        self._verify_inputs(records, pdf_sha, source_run)
        material = self.repository.source_material(source_run_id)
        source_argument_map = _source_argument_map(material.arguments)
        paragraph_map = _paragraph_map(material.paragraphs)
        self._verify_source_mapping(records, source_argument_map, paragraph_map)
        source_fingerprint_before = self.repository.source_fingerprint(source_run_id)
        manifest_payload = {
            "source_run_id": source_run_id,
            "source_run_input_manifest_sha256": source_run.input_manifest_sha256,
            "review_workbook_sha256": workbook_sha,
            "source_pdf_sha256": pdf_sha,
            "pipeline_version": PIPELINE_VERSION,
            "record_count": len(records),
            "source_run_fingerprint": source_fingerprint_before,
        }
        input_manifest = self.object_store.put_json(manifest_payload)
        run_id = f"reviewed-semantic-run:{content_hash(manifest_payload)}"
        existing = self.repository.get_run(run_id)
        if existing is not None and existing.stage in {
            ReviewedRunStage.COMPLETE,
            ReviewedRunStage.NEEDS_USER_REVIEW,
        }:
            if self.repository.source_fingerprint(source_run_id) != source_fingerprint_before:
                raise ValueError("source semantic run changed during idempotence check")
            return ReviewedExecution(
                result=self.repository.result(run_id),
                idempotent_replay=True,
            )

        started_at = existing.started_at if existing else datetime.now(UTC)
        run = ReviewedSemanticRun(
            run_id=run_id,
            source_run_id=source_run_id,
            author_source_id=source_run.author_source_id,
            review_workbook_sha256=workbook_sha,
            source_pdf_sha256=pdf_sha,
            input_manifest_sha256=input_manifest.sha256,
            pipeline_version=PIPELINE_VERSION,
            stage=ReviewedRunStage.INPUT_VERIFIED,
            review_record_count=len(records),
            reviewed_argument_count=0,
            unresolved_count=0,
            started_at=started_at,
        )
        self.repository.save_run(run)

        decisions, parsed_by_decision = self._decisions(
            run_id=run_id,
            records=records,
            source_argument_map=source_argument_map,
            paragraph_map=paragraph_map,
        )
        self.repository.save_decisions(decisions)
        for batch_ordinal, end in enumerate(range(25, len(decisions) + 25, 25), start=1):
            self.repository.save_checkpoint(
                run_id=run_id,
                stage=ReviewedRunStage.REVIEW_APPLIED.value,
                batch_ordinal=batch_ordinal,
                cursor={"processed_review_rows": min(end, len(decisions))},
            )
        unresolved_rows = sorted(
            decision.excel_row
            for decision in decisions
            if decision.application_status is ReviewApplicationStatus.NEEDS_USER_REVIEW
        )
        run = run.model_copy(
            update={
                "stage": ReviewedRunStage.REVIEW_APPLIED,
                "unresolved_count": len(unresolved_rows),
            }
        )
        self.repository.save_run(run)

        drafts = self._argument_drafts(
            decisions=decisions,
            parsed_by_decision=parsed_by_decision,
            paragraph_map=paragraph_map,
            source_arguments=material.arguments,
        )
        (
            reviewed_arguments,
            argument_vectors,
            score_rows,
            prototype_vectors,
        ) = self._build_and_score_arguments(
            run=run,
            drafts=drafts,
        )
        visual_lookup = {
            (visual.paragraph_id, visual.chart_unit_id): visual for visual in material.visuals
        }
        for offset in range(0, len(reviewed_arguments), 25):
            batch = reviewed_arguments[offset : offset + 25]
            self.repository.save_arguments(batch, visual_lookup)
            self.repository.save_checkpoint(
                run_id=run_id,
                stage=ReviewedRunStage.ARGUMENTS_BUILT.value,
                batch_ordinal=(offset // 25) + 1,
                cursor={"processed_argument_units": min(offset + 25, len(reviewed_arguments))},
            )
        run = run.model_copy(
            update={
                "stage": ReviewedRunStage.ARGUMENTS_BUILT,
                "reviewed_argument_count": len(reviewed_arguments),
            }
        )
        self.repository.save_run(run)

        cards, rules, rule_vectors = self._distill(
            run_id=run_id,
            arguments=reviewed_arguments,
            draft_by_text_hash={draft.text_object_sha256: draft for draft in drafts},
            argument_vectors=argument_vectors,
        )
        manifest_id = f"reviewed-embedding:{
            content_hash(
                {
                    'run_id': run_id,
                    'model_id': self.embedding_asset.model_id,
                    'model_revision': self.embedding_asset.model_revision,
                    'model_asset_sha256': self.embedding_asset.bundle_sha256,
                    'argument_ids': [item.argument_unit_id for item in reviewed_arguments],
                    'rule_ids': [item.rule_id for item in rules],
                }
            )
        }"
        vector_rows = [
            ReviewedVectorRow(
                entity_id=argument.argument_unit_id,
                entity_kind="REVIEWED_ARGUMENT_UNIT",
                input_object_sha256=argument.text_object_sha256,
                vector=tuple(float(value) for value in vector),
                token_count=encoded.token_count,
                chunk_count=encoded.chunk_count,
            )
            for argument, vector, encoded in argument_vectors
        ]
        method_vector_rows = [
            *[
                ReviewedVectorRow(
                    entity_id=f"method-prototype:{category.value}",
                    entity_kind="METHOD_PROTOTYPE",
                    input_object_sha256=prototype_hash,
                    vector=tuple(float(value) for value in vector),
                    token_count=encoded.token_count,
                    chunk_count=encoded.chunk_count,
                )
                for category, prototype_hash, vector, encoded in prototype_vectors
            ],
            *rule_vectors,
        ]
        parquet = self.parquet_store.write(
            author_source_id=run.author_source_id,
            run_id=run_id,
            manifest_id=manifest_id,
            vectors=vector_rows,
            scores=score_rows,
            method_vectors=method_vector_rows,
        )
        embedding_manifest = ReviewedEmbeddingManifest(
            manifest_id=manifest_id,
            run_id=run_id,
            model_id=self.embedding_asset.model_id,
            model_asset_sha256=self.embedding_asset.bundle_sha256,
            tokenizer_asset_sha256=_tokenizer_hash(self.embedding_asset),
            vector_parquet_sha256=parquet.vectors_sha256,
            score_parquet_sha256=parquet.scores_sha256,
            method_vector_parquet_sha256=parquet.method_vectors_sha256,
            source_embedding_manifest_id=(
                self.repository.source_embedding_manifest_id(source_run_id)
            ),
            source_embedding_reused=False,
            vector_count=len(vector_rows),
            score_count=len(score_rows),
            method_vector_count=len(method_vector_rows),
        )
        self.repository.save_embedding(embedding_manifest)
        run = run.model_copy(update={"stage": ReviewedRunStage.EMBEDDINGS_RECOMPUTED})
        self.repository.save_run(run)

        candidate_skills, lifecycle_skills = _build_skills(run_id, rules)
        all_skills = [*candidate_skills, *lifecycle_skills]
        ready_ids = sorted(
            skill.skill_id
            for skill in all_skills
            if skill.status is ReviewedSkillStatus.READY_FOR_SHADOW
        )
        review_skill_ids = sorted(
            skill.skill_id
            for skill in all_skills
            if skill.status is ReviewedSkillStatus.NEEDS_USER_REVIEW
        )
        author_coverage = ReviewedAuthorSkillCoverage(
            coverage_id=f"reviewed-author-coverage:{
                content_hash(
                    {
                        'run_id': run_id,
                        'candidate': {
                            item.category.value: item.coverage_state.value
                            for item in candidate_skills
                        },
                        'lifecycle': {
                            item.category.value: item.coverage_state.value
                            for item in lifecycle_skills
                        },
                    }
                )
            }",
            run_id=run_id,
            author_source_id=run.author_source_id,
            candidate_selection={item.category: item.coverage_state for item in candidate_skills},
            position_lifecycle={item.category: item.coverage_state for item in lifecycle_skills},
            source_argument_count=len(reviewed_arguments),
            ready_for_shadow_count=len(ready_ids),
            needs_user_review_count=len(review_skill_ids),
        )
        shadow_source_arguments = sorted(
            {
                argument_id
                for skill in all_skills
                if skill.status is ReviewedSkillStatus.READY_FOR_SHADOW
                for argument_id in skill.source_argument_unit_ids
            }
        )
        rule_by_id = {rule.rule_id: rule for rule in rules}
        all_rule_ids = sorted(rule_by_id)
        shadow_rule_ids = sorted(
            {
                rule_id
                for skill in all_skills
                if skill.status is ReviewedSkillStatus.READY_FOR_SHADOW
                for rule_id in skill.rule_ids
                if (
                    rule_by_id.get(rule_id, None)
                    and rule_by_id[rule_id].status is ReviewedSkillStatus.READY_FOR_SHADOW
                )
            }
        )
        shadow_bundle = ReviewedShadowBundle(
            bundle_id=f"reviewed-shadow-bundle:{
                content_hash(
                    {
                        'run_id': run_id,
                        'ready_skill_ids': ready_ids,
                        'needs_user_review_skill_ids': review_skill_ids,
                        'all_rule_ids': all_rule_ids,
                        'shadow_rule_ids': shadow_rule_ids,
                        'source_argument_unit_ids': shadow_source_arguments,
                    }
                )
            }",
            run_id=run_id,
            ready_skill_ids=ready_ids,
            needs_user_review_skill_ids=review_skill_ids,
            all_rule_ids=all_rule_ids,
            shadow_rule_ids=shadow_rule_ids,
            source_argument_unit_ids=shadow_source_arguments,
            formal_committee_weight_allowed=False,
        )
        self.repository.save_distillation(
            cards=cards,
            rules=rules,
            candidate_skills=candidate_skills,
            lifecycle_skills=lifecycle_skills,
            author_coverage=author_coverage,
            shadow_bundle=shadow_bundle,
        )
        run = run.model_copy(update={"stage": ReviewedRunStage.SKILLS_DISTILLED})
        self.repository.save_run(run)

        source_unchanged = (
            self.repository.source_fingerprint(source_run_id) == source_fingerprint_before
        )
        with self.state.connect() as connection:
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_key_passed = not foreign_key_rows
        integrity_passed = bool(integrity_row and integrity_row[0] == "ok")
        if not source_unchanged:
            raise ValueError("source semantic run was modified")
        if not foreign_key_passed or not integrity_passed:
            raise ValueError("reviewed library database validation failed")
        acceptance_statistics = _acceptance_statistics(
            records=records,
            decisions=decisions,
            parsed_by_decision=parsed_by_decision,
            arguments=reviewed_arguments,
            cards=cards,
            rules=rules,
            candidate_skills=candidate_skills,
            lifecycle_skills=lifecycle_skills,
        )
        coverage_status = "COMPLETE" if not unresolved_rows else "NEEDS_USER_REVIEW"
        coverage = ReviewedCoverageReport(
            report_id=f"reviewed-coverage:{
                content_hash(
                    {
                        'run_id': run_id,
                        'coverage_status': coverage_status,
                        'statistics': acceptance_statistics,
                        'unresolved_excel_rows': unresolved_rows,
                    }
                )
            }",
            run_id=run_id,
            coverage_status=coverage_status,
            review_record_count=len(records),
            mapped_record_count=len(decisions),
            reviewed_argument_count=len(reviewed_arguments),
            visual_argument_count=acceptance_statistics["visual_participation_count"],
            visual_ref_count=sum(
                len(ref.visual_chart_unit_ids)
                for argument in reviewed_arguments
                for ref in argument.paragraph_refs
            ),
            unresolved_excel_rows=unresolved_rows,
            source_run_unchanged=source_unchanged,
            source_embedding_reused=False,
            source_skill_reused=False,
            foreign_key_check_passed=foreign_key_passed,
            integrity_check_passed=integrity_passed,
            acceptance_statistics=acceptance_statistics,
        )
        self.repository.save_coverage(coverage)
        terminal_stage = (
            ReviewedRunStage.COMPLETE if not unresolved_rows else ReviewedRunStage.NEEDS_USER_REVIEW
        )
        run = run.model_copy(
            update={
                "stage": terminal_stage,
                "finished_at": datetime.now(UTC),
                "unresolved_count": len(unresolved_rows),
            }
        )
        self.repository.save_run(run)
        return ReviewedExecution(
            result=self.repository.result(run_id),
            idempotent_replay=False,
        )

    @staticmethod
    def _verify_inputs(
        records: Sequence[Any],
        pdf_sha: str,
        source_run: SemanticFunnelRun,
    ) -> None:
        if len(records) != EXPECTED_REVIEW_RECORD_COUNT:
            raise ValueError("review workbook must contain exactly 300 records")
        if pdf_sha not in source_run.input_hashes:
            raise ValueError("source PDF hash is absent from the semantic run inputs")
        if source_run.content_item_count < 1 or source_run.paragraph_count < 1:
            raise ValueError("source semantic run is not materialized")

    @staticmethod
    def _verify_source_mapping(
        records: Sequence[Any],
        source_argument_map: dict[tuple[int, int, int], ArgumentUnit],
        paragraph_map: dict[tuple[int, int], SourceParagraphRecord],
    ) -> None:
        missing: list[int] = []
        for record in records:
            key = (
                record.page_number,
                record.source_start_ordinal,
                record.source_end_ordinal,
            )
            if key not in source_argument_map:
                missing.append(record.excel_row)
            if any(
                (record.page_number, ordinal) not in paragraph_map
                for ordinal in range(
                    record.source_start_ordinal,
                    record.source_end_ordinal + 1,
                )
            ):
                missing.append(record.excel_row)
        if missing:
            raise ValueError(
                "review workbook rows do not map to source facts: "
                + ",".join(str(item) for item in sorted(set(missing)))
            )

    def _decisions(
        self,
        *,
        run_id: str,
        records: Sequence[Any],
        source_argument_map: dict[tuple[int, int, int], ArgumentUnit],
        paragraph_map: dict[tuple[int, int], SourceParagraphRecord],
    ) -> tuple[list[ReviewDecision], dict[str, ParsedReviewConclusion]]:
        decisions: list[ReviewDecision] = []
        parsed_by_decision: dict[str, ParsedReviewConclusion] = {}
        for record in records:
            source_argument = source_argument_map[
                (
                    record.page_number,
                    record.source_start_ordinal,
                    record.source_end_ordinal,
                )
            ]
            parsed = interpret_review_conclusion(record)
            uncertainty = parsed.uncertainty_reason
            if record.verdict is not ReviewVerdict.REJECT:
                range_error = _validate_target_ranges(parsed.targets, paragraph_map)
                anchor_error = _validate_target_anchors(parsed.targets, paragraph_map)
                uncertainty = range_error or anchor_error or uncertainty
            if record.verdict is ReviewVerdict.REJECT:
                status = ReviewApplicationStatus.EXCLUDED
            elif uncertainty:
                status = ReviewApplicationStatus.NEEDS_USER_REVIEW
            else:
                status = ReviewApplicationStatus.APPLIED
            identity = {
                "run_id": run_id,
                "excel_row": record.excel_row,
                "source_argument_unit_id": source_argument.argument_unit_id,
                "review_conclusion_sha256": sha256_bytes(record.conclusion.encode("utf-8")),
            }
            decision = ReviewDecision(
                decision_id=f"review-decision:{content_hash(identity)}",
                run_id=run_id,
                excel_row=record.excel_row,
                source_argument_unit_id=source_argument.argument_unit_id,
                verdict=record.verdict,
                application_status=status,
                targets=list(parsed.targets),
                corrected_topics=list(parsed.corrected_topics),
                uncertainty_reason=uncertainty,
                review_conclusion_sha256=identity["review_conclusion_sha256"],
            )
            decisions.append(decision)
            parsed_by_decision[decision.decision_id] = parsed
        return decisions, parsed_by_decision

    def _argument_drafts(
        self,
        *,
        decisions: Sequence[ReviewDecision],
        parsed_by_decision: dict[str, ParsedReviewConclusion],
        paragraph_map: dict[tuple[int, int], SourceParagraphRecord],
        source_arguments: Sequence[ArgumentUnit],
    ) -> list[_ArgumentDraft]:
        paragraph_to_source_arguments: dict[str, set[str]] = defaultdict(set)
        for source_argument in source_arguments:
            for paragraph_id in source_argument.paragraph_ids:
                paragraph_to_source_arguments[paragraph_id].add(source_argument.argument_unit_id)
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for decision in decisions:
            if decision.application_status is not ReviewApplicationStatus.APPLIED:
                continue
            parsed = parsed_by_decision[decision.decision_id]
            for target in parsed.targets:
                paragraphs = _expand_ranges(target.ranges, paragraph_map)
                paragraph_ids = tuple(item.unit.paragraph_id for item in paragraphs)
                group = grouped.setdefault(
                    paragraph_ids,
                    {
                        "decision_ids": [],
                        "source_argument_ids": set(),
                        "titles": [],
                        "topics": set(),
                        "paragraphs": paragraphs,
                    },
                )
                group["decision_ids"].append(decision.decision_id)
                group["source_argument_ids"].add(decision.source_argument_unit_id)
                for paragraph_id in paragraph_ids:
                    group["source_argument_ids"].update(paragraph_to_source_arguments[paragraph_id])
                group["titles"].append(target.title)
                group["topics"].update(target.topics)
        drafts: list[_ArgumentDraft] = []
        for _paragraph_ids, group in sorted(
            grouped.items(),
            key=lambda item: (
                item[1]["paragraphs"][0].unit.locator.page_number,
                item[1]["paragraphs"][0].unit.ordinal,
                item[0],
            ),
        ):
            paragraphs = tuple(group["paragraphs"])
            text = "\n".join(
                f"[第{item.unit.locator.page_number}页第{item.unit.ordinal}段|"
                f"{item.unit.primary_role.value}] {item.text.strip()}"
                for item in paragraphs
            )
            text_ref = self.object_store.put_bytes(text.encode("utf-8"))
            visual_conflict = any(
                item.unit.visual_quality_status not in {None, "PASS", "READY", "COMPLETE"}
                for item in paragraphs
            )
            reason_codes = ["HUMAN_REVIEW_APPLIED", "ORDERED_PARAGRAPH_REFS"]
            if len({item.unit.content_id for item in paragraphs}) > 1:
                reason_codes.append("CROSS_CONTENT_ORDERED_REFS")
            if any(item.unit.visual_chart_unit_ids for item in paragraphs):
                reason_codes.append("VISUAL_LINEAGE_INCLUDED")
            if visual_conflict:
                reason_codes.append("VISUAL_INTERPRETATION_REQUIRES_REVIEW")
            drafts.append(
                _ArgumentDraft(
                    decision_ids=tuple(sorted(set(group["decision_ids"]))),
                    source_argument_ids=tuple(sorted(group["source_argument_ids"])),
                    title=_most_specific_title(group["titles"]),
                    topics=tuple(sorted(group["topics"])),
                    paragraphs=paragraphs,
                    text=text,
                    text_object_sha256=text_ref.sha256,
                    visual_conflict=visual_conflict,
                    reason_codes=tuple(reason_codes),
                )
            )
        return drafts

    def _build_and_score_arguments(
        self,
        *,
        run: ReviewedSemanticRun,
        drafts: Sequence[_ArgumentDraft],
    ) -> tuple[
        list[ReviewedArgumentUnit],
        list[tuple[ReviewedArgumentUnit, np.ndarray[Any, Any], Any]],
        list[ReviewedScoreRow],
        list[tuple[BookMethodCategory, str, np.ndarray[Any, Any], Any]],
    ]:
        encoded_arguments = self.embedding_backend.encode([draft.text for draft in drafts])
        categories = list(BookMethodCategory)
        prototype_texts = [
            "；".join(self.semantic_config.method_anchors[category]) for category in categories
        ]
        encoded_prototypes = self.embedding_backend.encode(prototype_texts)
        prototype_vectors = np.asarray(
            [item.vector for item in encoded_prototypes],
            dtype=np.float32,
        )
        results: list[ReviewedArgumentUnit] = []
        argument_vectors: list[tuple[ReviewedArgumentUnit, np.ndarray[Any, Any], Any]] = []
        score_rows: list[ReviewedScoreRow] = []
        for draft, encoded in zip(drafts, encoded_arguments, strict=True):
            vector = np.asarray(encoded.vector, dtype=np.float32)
            category_scores = {
                category: float(np.dot(vector, prototype_vectors[index]))
                for index, category in enumerate(categories)
            }
            explicit_categories = _method_categories(
                draft.topics,
                draft.text,
                draft.paragraphs,
            )
            best_category_score = max(category_scores.values())
            semantic_categories = [
                category
                for category, score in category_scores.items()
                if score >= 0.40 and score >= best_category_score - 0.035
            ]
            selected = sorted(
                set((*explicit_categories, *semantic_categories)),
                key=lambda item: item.value,
            )
            if not selected:
                selected = [max(category_scores, key=category_scores.__getitem__)]
            topic_relevance = max(
                0.0,
                min(1.0, max(category_scores[category] for category in selected)),
            )
            completeness = _methodological_completeness(draft)
            identity = {
                "run_id": run.run_id,
                "decision_ids": draft.decision_ids,
                "paragraph_ids": [item.unit.paragraph_id for item in draft.paragraphs],
                "title": draft.title,
                "text_object_sha256": draft.text_object_sha256,
                "method_categories": [item.value for item in selected],
            }
            argument_unit_id = f"reviewed-argument-unit:{content_hash(identity)}"
            refs = [
                ReviewedParagraphRef(
                    ref_ordinal=index,
                    source_paragraph_id=paragraph.unit.paragraph_id,
                    item_id=paragraph.item_id,
                    content_id=paragraph.unit.content_id,
                    page_number=int(paragraph.unit.locator.page_number or 0),
                    paragraph_ordinal=paragraph.unit.ordinal,
                    paragraph_head=_paragraph_head(paragraph.text),
                    text_object_sha256=paragraph.unit.text_object_sha256,
                    rhetorical_role=paragraph.unit.primary_role,
                    rhetorical_roles=paragraph.unit.rhetorical_roles,
                    source_snapshot_id=paragraph.unit.locator.source_snapshot_id,
                    locator=paragraph.unit.locator,
                    visual_evidence_ids=paragraph.unit.visual_evidence_ids,
                    visual_chart_unit_ids=paragraph.unit.visual_chart_unit_ids,
                )
                for index, paragraph in enumerate(draft.paragraphs, start=1)
            ]
            relations = _relations(
                run_id=run.run_id,
                argument_unit_id=argument_unit_id,
                refs=refs,
            )
            roles = _unique_roles(refs)
            status = (
                ReviewedArgumentStatus.NEEDS_USER_REVIEW
                if draft.visual_conflict
                else ReviewedArgumentStatus.READY
            )
            argument = ReviewedArgumentUnit(
                argument_unit_id=argument_unit_id,
                run_id=run.run_id,
                decision_ids=list(draft.decision_ids),
                author_source_id=run.author_source_id,
                title=draft.title,
                paragraph_refs=refs,
                start_locator=refs[0].locator,
                end_locator=refs[-1].locator,
                text_object_sha256=draft.text_object_sha256,
                rhetorical_roles=roles,
                relations=relations,
                method_categories=selected,
                topic_relevance=topic_relevance,
                methodological_completeness=completeness,
                standalone_distillable=(
                    status is ReviewedArgumentStatus.READY and completeness >= 0.40
                ),
                status=status,
                source_argument_unit_ids=list(draft.source_argument_ids),
                source_snapshot_ids=sorted(
                    {item.unit.locator.source_snapshot_id for item in draft.paragraphs}
                ),
                reason_codes=list(draft.reason_codes),
            )
            results.append(argument)
            argument_vectors.append((argument, vector, encoded))
            score_rows.append(
                ReviewedScoreRow(
                    argument_unit_id=argument.argument_unit_id,
                    topic_relevance=topic_relevance,
                    methodological_completeness=completeness,
                    category_scores={
                        category.value: score for category, score in category_scores.items()
                    },
                    selected_categories=tuple(category.value for category in selected),
                )
            )
        prototypes = [
            (
                category,
                self.object_store.put_bytes(text.encode("utf-8")).sha256,
                prototype_vectors[index],
                encoded_prototypes[index],
            )
            for index, (category, text) in enumerate(zip(categories, prototype_texts, strict=True))
        ]
        return results, argument_vectors, score_rows, prototypes

    def _distill(
        self,
        *,
        run_id: str,
        arguments: Sequence[ReviewedArgumentUnit],
        draft_by_text_hash: dict[str, _ArgumentDraft],
        argument_vectors: Sequence[tuple[ReviewedArgumentUnit, np.ndarray[Any, Any], Any]],
    ) -> tuple[list[ViewpointCard], list[MethodRule], list[ReviewedVectorRow]]:
        vector_by_argument = {
            argument.argument_unit_id: vector for argument, vector, _ in argument_vectors
        }
        rule_drafts = [
            _rule_draft(
                argument,
                draft_by_text_hash[argument.text_object_sha256].text,
            )
            for argument in arguments
        ]
        clusters = _semantic_clusters(rule_drafts, vector_by_argument)
        rules: list[MethodRule] = []
        rule_texts: list[str] = []
        for cluster in clusters:
            rule = _merge_rule_cluster(run_id, cluster)
            rules.append(rule)
            rule_texts.append(_rule_embedding_text(rule))
        encoded_rules = self.embedding_backend.encode(rule_texts)
        rule_vectors: list[ReviewedVectorRow] = []
        for rule, text, encoded in zip(rules, rule_texts, encoded_rules, strict=True):
            text_hash = self.object_store.put_bytes(text.encode("utf-8")).sha256
            rule_vectors.append(
                ReviewedVectorRow(
                    entity_id=rule.rule_id,
                    entity_kind="METHOD_RULE",
                    input_object_sha256=text_hash,
                    vector=encoded.vector,
                    token_count=encoded.token_count,
                    chunk_count=encoded.chunk_count,
                )
            )
        cards: list[ViewpointCard] = []
        for argument in arguments:
            draft = next(
                item
                for item in rule_drafts
                if item.argument.argument_unit_id == argument.argument_unit_id
            )
            counterevidence = _sentences_for(
                draft.text,
                (*_FIELD_KEYWORDS["negative"], *_FIELD_KEYWORDS["invalidation"]),
            )
            failure_conditions = _sentences_for(
                draft.text,
                _FIELD_KEYWORDS["failure"],
            )
            card_status = (
                ReviewedSkillStatus.READY_FOR_SHADOW
                if (
                    argument.status is ReviewedArgumentStatus.READY
                    and counterevidence
                    and failure_conditions
                )
                else ReviewedSkillStatus.NEEDS_USER_REVIEW
            )
            card_identity = {
                "run_id": run_id,
                "argument_unit_id": argument.argument_unit_id,
                "proposition": argument.title,
                "method_category": argument.method_categories[0].value,
            }
            cards.append(
                ViewpointCard(
                    card_id=f"viewpoint-card:{content_hash(card_identity)}",
                    run_id=run_id,
                    proposition=argument.title,
                    method_category=argument.method_categories[0],
                    source_refs=[draft.source_ref],
                    counterevidence=counterevidence or [INSUFFICIENT_SOURCE],
                    failure_conditions=failure_conditions or [INSUFFICIENT_SOURCE],
                    status=card_status,
                )
            )
        return cards, rules, rule_vectors


def _source_argument_map(
    arguments: Sequence[ArgumentUnit],
) -> dict[tuple[int, int, int], ArgumentUnit]:
    result: dict[tuple[int, int, int], ArgumentUnit] = {}
    for argument in arguments:
        match = _PAGE_SUFFIX.search(argument.content_id)
        if match is None:
            continue
        key = (int(match.group(1)), argument.start_ordinal, argument.end_ordinal)
        if key in result:
            raise ValueError(f"duplicate source argument range: {key}")
        result[key] = argument
    return result


def _paragraph_map(
    paragraphs: Sequence[SourceParagraphRecord],
) -> dict[tuple[int, int], SourceParagraphRecord]:
    result: dict[tuple[int, int], SourceParagraphRecord] = {}
    for paragraph in paragraphs:
        page = paragraph.unit.locator.page_number
        if page is None:
            continue
        key = (page, paragraph.unit.ordinal)
        if key in result:
            raise ValueError(f"duplicate source paragraph locator: {key}")
        result[key] = paragraph
    return result


def _validate_target_ranges(
    targets: Sequence[ReviewArgumentTarget],
    paragraph_map: dict[tuple[int, int], SourceParagraphRecord],
) -> str | None:
    try:
        for target in targets:
            _expand_ranges(target.ranges, paragraph_map)
    except ValueError as exc:
        return str(exc)
    return None


def _validate_target_anchors(
    targets: Sequence[ReviewArgumentTarget],
    paragraph_map: dict[tuple[int, int], SourceParagraphRecord],
) -> str | None:
    for target in targets:
        for range_value in target.ranges:
            start = paragraph_map.get(
                (
                    range_value.start_page,
                    range_value.start_paragraph_ordinal,
                )
            )
            end = paragraph_map.get(
                (
                    range_value.end_page,
                    range_value.end_paragraph_ordinal,
                )
            )
            if start is None or end is None:
                continue
            for label, summary, paragraph in (
                ("START", range_value.start_summary, start),
                ("END", range_value.end_summary, end),
            ):
                candidate_texts = [paragraph.text]
                if summary and summary.startswith("图片/图表，邻近标题："):
                    page = int(paragraph.unit.locator.page_number or 0)
                    candidate_texts.extend(
                        candidate.text
                        for (candidate_page, _), candidate in paragraph_map.items()
                        if candidate_page == page
                    )
                if summary and not any(
                    _anchor_matches(summary, candidate_text) for candidate_text in candidate_texts
                ):
                    return (
                        f"{label}_ANCHOR_MISMATCH:"
                        f"page={paragraph.unit.locator.page_number},"
                        f"paragraph={paragraph.unit.ordinal},"
                        f"anchor={summary[:40]}"
                    )
    return None


def _anchor_matches(anchor: str, text: str) -> bool:
    anchor = anchor.removeprefix("图片/图表，邻近标题：").strip()
    for marker in ("…", "...", "．．．"):
        anchor = anchor.split(marker, 1)[0]
    normalized_anchor = "".join(character.casefold() for character in anchor if character.isalnum())
    normalized_text = "".join(character.casefold() for character in text if character.isalnum())
    if not normalized_anchor:
        return False
    probe = normalized_anchor[: min(18, len(normalized_anchor))]
    return probe in normalized_text


def _expand_ranges(
    ranges: Sequence[ReviewParagraphRange],
    paragraph_map: dict[tuple[int, int], SourceParagraphRecord],
) -> tuple[SourceParagraphRecord, ...]:
    selected: dict[str, SourceParagraphRecord] = {}
    pages = sorted({page for page, _ in paragraph_map})
    for range_value in ranges:
        for page in pages:
            if page < range_value.start_page or page > range_value.end_page:
                continue
            page_ordinals = sorted(
                ordinal for candidate_page, ordinal in paragraph_map if candidate_page == page
            )
            start = (
                range_value.start_paragraph_ordinal
                if page == range_value.start_page
                else page_ordinals[0]
            )
            end = (
                range_value.end_paragraph_ordinal
                if page == range_value.end_page
                else page_ordinals[-1]
            )
            for ordinal in range(start, end + 1):
                paragraph = paragraph_map.get((page, ordinal))
                if paragraph is None:
                    raise ValueError(f"MISSING_PARAGRAPH:page={page},paragraph={ordinal}")
                selected[paragraph.unit.paragraph_id] = paragraph
    if not selected:
        raise ValueError("EMPTY_REVIEWED_PARAGRAPH_RANGE")
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (
                int(item.unit.locator.page_number or 0),
                item.unit.ordinal,
                item.unit.paragraph_id,
            ),
        )
    )


def _method_categories(
    topics: Sequence[str],
    text: str,
    paragraphs: Sequence[SourceParagraphRecord],
) -> list[BookMethodCategory]:
    haystack = " ".join((*topics, text)).casefold()
    selected = {
        category
        for category, terms in _TOPIC_METHOD_KEYWORDS.items()
        if any(term.casefold() in haystack for term in terms)
    }
    for paragraph in paragraphs:
        selected.update(
            category
            for category in BookMethodCategory
            if any(
                keyword.casefold() in paragraph.text.casefold()
                for keyword in _TOPIC_METHOD_KEYWORDS[category]
            )
        )
    return sorted(selected, key=lambda item: item.value)


def _methodological_completeness(draft: _ArgumentDraft) -> float:
    roles = {role for paragraph in draft.paragraphs for role in paragraph.unit.rhetorical_roles}
    role_signals = (
        sum(
            role in roles
            for role in (
                RhetoricalRole.CLAIM,
                RhetoricalRole.EVIDENCE,
                RhetoricalRole.EXPLANATION,
                RhetoricalRole.CAUSAL_REASON,
                RhetoricalRole.CONCLUSION,
                RhetoricalRole.OPERATIONAL_RULE,
                RhetoricalRole.RISK,
            )
        )
        / 7
    )
    source_score = sum(
        paragraph.unit.methodological_completeness for paragraph in draft.paragraphs
    ) / len(draft.paragraphs)
    lexical_fields = sum(
        bool(_sentences_for(draft.text, terms)) for terms in _FIELD_KEYWORDS.values()
    ) / len(_FIELD_KEYWORDS)
    return round(
        max(0.0, min(1.0, 0.35 * source_score + 0.35 * role_signals + 0.30 * lexical_fields)),
        6,
    )


def _relations(
    *,
    run_id: str,
    argument_unit_id: str,
    refs: Sequence[ReviewedParagraphRef],
) -> list[ReviewedArgumentRelation]:
    relations: list[ReviewedArgumentRelation] = []
    for source, target in zip(refs, refs[1:], strict=False):
        relation_type = _relation_type(source, target)
        identity = {
            "run_id": run_id,
            "argument_unit_id": argument_unit_id,
            "source_ref_ordinal": source.ref_ordinal,
            "target_ref_ordinal": target.ref_ordinal,
            "relation_type": relation_type.value,
        }
        relations.append(
            ReviewedArgumentRelation(
                relation_id=f"reviewed-argument-relation:{content_hash(identity)}",
                run_id=run_id,
                argument_unit_id=argument_unit_id,
                source_ref_ordinal=source.ref_ordinal,
                target_ref_ordinal=target.ref_ordinal,
                relation_type=relation_type,
                confidence=0.9 if relation_type is not ArgumentRelationType.CONTINUATION else 0.75,
                reason_codes=["RECOMPUTED_FROM_REVIEWED_ORDER_AND_ROLES"],
            )
        )
    return relations


def _relation_type(
    source: ReviewedParagraphRef,
    target: ReviewedParagraphRef,
) -> ArgumentRelationType:
    source_roles = set(source.rhetorical_roles)
    target_roles = set(target.rhetorical_roles)
    if RhetoricalRole.QUESTION in source_roles:
        return ArgumentRelationType.QUESTION_ANSWER
    if RhetoricalRole.CLAIM in source_roles and RhetoricalRole.EVIDENCE in target_roles:
        return ArgumentRelationType.CLAIM_EVIDENCE
    if RhetoricalRole.CLAIM in source_roles and target_roles.intersection(
        {RhetoricalRole.EXPLANATION, RhetoricalRole.CAUSAL_REASON}
    ):
        return ArgumentRelationType.CLAIM_EXPLANATION
    if RhetoricalRole.EXAMPLE in target_roles:
        return ArgumentRelationType.EXAMPLE_OF
    if RhetoricalRole.COUNTERARGUMENT in target_roles:
        return ArgumentRelationType.COUNTER_TO
    if RhetoricalRole.CONCLUSION in target_roles:
        return ArgumentRelationType.CONCLUSION_OF
    return ArgumentRelationType.CONTINUATION


def _unique_roles(refs: Sequence[ReviewedParagraphRef]) -> list[RhetoricalRole]:
    seen: set[RhetoricalRole] = set()
    result: list[RhetoricalRole] = []
    for ref in refs:
        for role in ref.rhetorical_roles:
            if role not in seen:
                seen.add(role)
                result.append(role)
    return result


def _paragraph_head(text: str) -> str:
    normalized = _SPACE.sub(" ", text).strip()
    return normalized[:80] or "[EMPTY]"


def _most_specific_title(titles: Sequence[str]) -> str:
    return sorted(
        {_SPACE.sub(" ", title).strip(" ；。") for title in titles if title.strip()},
        key=lambda value: (-len(value), value),
    )[0]


def _legacy_rule_draft(argument: ReviewedArgumentUnit, text: str) -> _RuleDraft:
    fields = {name: tuple(_sentences_for(text, terms)) for name, terms in _FIELD_KEYWORDS.items()}
    sentences = _source_sentences(text)
    reasoning = tuple(sentences[:4]) or (INSUFFICIENT_SOURCE,)
    status = (
        ReviewedSkillStatus.READY_FOR_SHADOW
        if (
            argument.status is ReviewedArgumentStatus.READY
            and fields["conditions"]
            and fields["evidence"]
            and (fields["positive"] or fields["negative"])
            and (fields["invalidation"] or fields["failure"])
        )
        else ReviewedSkillStatus.NEEDS_USER_REVIEW
    )
    uncertainty_reason: tuple[str, ...] = (
        ()
        if status is ReviewedSkillStatus.READY_FOR_SHADOW
        else ("legacy_draft_unavailable",)
    )
    return _RuleDraft(
        argument=argument,
        text=text,
        source_ref=ReviewedSourceRef(
            argument_unit_id=argument.argument_unit_id,
            paragraph_ids=[ref.source_paragraph_id for ref in argument.paragraph_refs],
            page_numbers=sorted({ref.page_number for ref in argument.paragraph_refs}),
            text_object_sha256=argument.text_object_sha256,
        ),
        decision_question=f"如何判断“{argument.title}”？",
        applicable_conditions=fields["conditions"] or (INSUFFICIENT_SOURCE,),
        reasoning_steps=reasoning,
        required_evidence=fields["evidence"] or (INSUFFICIENT_SOURCE,),
        positive_signals=fields["positive"] or (INSUFFICIENT_SOURCE,),
        negative_signals=fields["negative"] or (INSUFFICIENT_SOURCE,),
        invalidation_conditions=fields["invalidation"] or (INSUFFICIENT_SOURCE,),
        known_failure_modes=fields["failure"] or (INSUFFICIENT_SOURCE,),
        applicable_industries=fields["industry"] or (INSUFFICIENT_SOURCE,),
        holding_horizon=fields["horizon"] or (INSUFFICIENT_SOURCE,),
        uncertainty_reason=uncertainty_reason,
        status=status,
    )


def _build_rule_draft(argument: ReviewedArgumentUnit, text: str) -> _RuleDraft:
    fields = {
        name: tuple(_sentences_for(text, terms))
        for name, terms in _FIELD_KEYWORDS.items()
    }
    sentences = _source_sentences(text)
    applicable_conditions = _compact_rule_items("条件", fields["conditions"])
    required_evidence = _compact_rule_items("证据", fields["evidence"])
    positive_signals = _compact_rule_items("正向", fields["positive"])
    negative_signals = _compact_rule_items("反向", fields["negative"])
    invalidation_conditions = _compact_rule_items("失效", fields["invalidation"])
    known_failure_modes = _compact_rule_items("失效案例", fields["failure"])
    applicable_industries = _compact_rule_items("行业", fields["industry"])
    holding_horizon = _compact_rule_items("时间", fields["horizon"])
    uncertainty: list[str] = []
    if argument.status is not ReviewedArgumentStatus.READY:
        uncertainty.append("来源AU尚未通过人工复核，无法直接成为规则。")
    if not applicable_conditions:
        uncertainty.append("当前文本未识别到稳定可复用的触发条件。")
    if not required_evidence:
        uncertainty.append("当前文本未识别到可核验的证据锚点。")
    if not (positive_signals or negative_signals):
        uncertainty.append("未识别支持/反对信号，规则边界不足。")
    if not (invalidation_conditions or known_failure_modes):
        uncertainty.append("未识别反例或失效条件，缺少安全约束。")
    if len(sentences) < 2:
        uncertainty.append("文本长度过短，难以稳定生成抽象规则。")
    status = (
        ReviewedSkillStatus.READY_FOR_SHADOW
        if not uncertainty
        else ReviewedSkillStatus.NEEDS_USER_REVIEW
    )
    return _RuleDraft(
        argument=argument,
        text=text,
        source_ref=ReviewedSourceRef(
            argument_unit_id=argument.argument_unit_id,
            paragraph_ids=[ref.source_paragraph_id for ref in argument.paragraph_refs],
            page_numbers=sorted({ref.page_number for ref in argument.paragraph_refs}),
            text_object_sha256=argument.text_object_sha256,
        ),
        decision_question=(
            f"在“{argument.title}”场景下，何时应执行对应的投资行动？"
        ),
        applicable_conditions=applicable_conditions or (INSUFFICIENT_SOURCE,),
        reasoning_steps=(
            _compact_rule_items(
                "推理",
                [
                    f"先确认“{argument.title}”的触发边界。",
                    (
                        "建立证据链："
                        f"{_join_with_conjunction(applicable_conditions)}；"
                        f"{_join_with_conjunction(required_evidence)}"
                    ),
                    "再提取支持/反对与失效信号，形成可执行规则判断。",
                    "未满足约束条件时不触发该规则。",
                ],
            )
        ),
        required_evidence=required_evidence or (INSUFFICIENT_SOURCE,),
        positive_signals=positive_signals or (INSUFFICIENT_SOURCE,),
        negative_signals=negative_signals or (INSUFFICIENT_SOURCE,),
        invalidation_conditions=invalidation_conditions or (INSUFFICIENT_SOURCE,),
        known_failure_modes=known_failure_modes or (INSUFFICIENT_SOURCE,),
        applicable_industries=applicable_industries or (INSUFFICIENT_SOURCE,),
        holding_horizon=holding_horizon or (INSUFFICIENT_SOURCE,),
        uncertainty_reason=tuple(uncertainty),
        status=status,
    )


def _rule_draft(argument: ReviewedArgumentUnit, text: str) -> _RuleDraft:
    return _build_rule_draft(argument, text)


def _compact_rule_items(label: str, raw_items: Sequence[str]) -> tuple[str, ...]:
    outputs = []
    for item in raw_items:
        clause = _compact_clause(item)
        if clause:
            outputs.append(f"{label}：{clause}")
    return tuple(dict.fromkeys(outputs))[:4]


def _compact_clause(sentence: str) -> str:
    text = _SPACE.sub(" ", re.sub(r"^\[[^\]]+\]\s*", "", sentence)).strip()
    if not text:
        return ""
    if len(text) > 180:
        return f"{text[:177]}..."
    return text


def _join_with_conjunction(items: Sequence[str]) -> str:
    if not items:
        return "待补充"
    if len(items) == 1:
        return items[0]
    return "，并".join(items)


def _source_sentences(text: str) -> list[str]:
    result: list[str] = []
    for raw in _SENTENCE_SPLIT.split(text):
        sentence = _SPACE.sub(" ", re.sub(r"^\[[^\]]+\]\s*", "", raw)).strip()
        if len(sentence) >= 6 and sentence not in result:
            result.append(sentence)
    return result


def _sentences_for(text: str, terms: Sequence[str]) -> list[str]:
    return [
        sentence
        for sentence in _source_sentences(text)
        if any(term.casefold() in sentence.casefold() for term in terms)
    ][:4]


def _semantic_clusters(
    drafts: Sequence[_RuleDraft],
    vector_by_argument: dict[str, np.ndarray[Any, Any]],
) -> list[list[_RuleDraft]]:
    clusters: list[list[_RuleDraft]] = []
    centroids: list[np.ndarray[Any, Any]] = []
    for draft in sorted(drafts, key=lambda item: item.argument.argument_unit_id):
        vector = vector_by_argument[draft.argument.argument_unit_id]
        category_set = set(draft.argument.method_categories)
        match_index: int | None = None
        best_similarity = -1.0
        for index, (cluster, centroid) in enumerate(zip(clusters, centroids, strict=True)):
            cluster_categories = {
                category for item in cluster for category in item.argument.method_categories
            }
            if not category_set.intersection(cluster_categories):
                continue
            similarity = float(np.dot(vector, centroid))
            if similarity >= 0.94 and similarity > best_similarity:
                match_index = index
                best_similarity = similarity
        if match_index is None:
            clusters.append([draft])
            centroids.append(vector)
            continue
        clusters[match_index].append(draft)
        centroid = np.mean(
            [vector_by_argument[item.argument.argument_unit_id] for item in clusters[match_index]],
            axis=0,
        )
        centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
        centroids[match_index] = centroid
    return clusters


def _merge_rule_cluster(run_id: str, cluster: Sequence[_RuleDraft]) -> MethodRule:
    categories = sorted(
        {category for item in cluster for category in item.argument.method_categories},
        key=lambda item: item.value,
    )
    source_refs = sorted(
        {item.source_ref.argument_unit_id: item.source_ref for item in cluster}.values(),
        key=lambda item: item.argument_unit_id,
    )
    decision_question = sorted(
        (item.decision_question for item in cluster),
        key=lambda value: (-len(value), value),
    )[0]
    field_values = {
        "applicable_conditions": _merge_source_lists(
            item.applicable_conditions for item in cluster
        ),
        "reasoning_steps": _merge_source_lists(item.reasoning_steps for item in cluster),
        "required_evidence": _merge_source_lists(item.required_evidence for item in cluster),
        "positive_signals": _merge_source_lists(item.positive_signals for item in cluster),
        "negative_signals": _merge_source_lists(item.negative_signals for item in cluster),
        "invalidation_conditions": _merge_source_lists(
            item.invalidation_conditions for item in cluster
        ),
        "known_failure_modes": _merge_source_lists(item.known_failure_modes for item in cluster),
        "applicable_industries": _merge_source_lists(
            item.applicable_industries for item in cluster
        ),
        "holding_horizon": _merge_source_lists(item.holding_horizon for item in cluster),
    }
    status = _rule_cluster_status(
        cluster=cluster,
        field_values=field_values,
    )
    signature = content_hash(
        {
            "decision_question": _PUNCTUATION.sub("", decision_question).casefold(),
            "method_categories": [item.value for item in categories],
            "source_argument_unit_ids": [item.argument_unit_id for item in source_refs],
        }
    )
    identity = {"run_id": run_id, "semantic_signature_sha256": signature}
    return MethodRule(
        rule_id=f"method-rule:{content_hash(identity)}",
        run_id=run_id,
        semantic_signature_sha256=signature,
        decision_question=decision_question,
        applicable_conditions=field_values["applicable_conditions"],
        reasoning_steps=field_values["reasoning_steps"],
        required_evidence=field_values["required_evidence"],
        positive_signals=field_values["positive_signals"],
        negative_signals=field_values["negative_signals"],
        invalidation_conditions=field_values["invalidation_conditions"],
        known_failure_modes=field_values["known_failure_modes"],
        applicable_industries=field_values["applicable_industries"],
        holding_horizon=field_values["holding_horizon"],
        method_categories=categories,
        source_refs=source_refs,
        status=status,
    )


def _rule_cluster_status(
    *,
    cluster: Sequence[_RuleDraft],
    field_values: dict[str, list[str]],
) -> ReviewedSkillStatus:
    if any(item.status is ReviewedSkillStatus.NEEDS_USER_REVIEW for item in cluster):
        return ReviewedSkillStatus.NEEDS_USER_REVIEW
    if any(
        INSUFFICIENT_SOURCE in value for value in field_values.values()
    ):
        return ReviewedSkillStatus.NEEDS_USER_REVIEW
    if any(item.uncertainty_reason for item in cluster):
        return ReviewedSkillStatus.NEEDS_USER_REVIEW
    return ReviewedSkillStatus.READY_FOR_SHADOW


def _merge_source_lists(values: Any) -> list[str]:
    flattened = [value for sequence in values for value in sequence if value != INSUFFICIENT_SOURCE]
    unique = list(dict.fromkeys(flattened))
    return unique[:8] or [INSUFFICIENT_SOURCE]


def _rule_embedding_text(rule: MethodRule) -> str:
    return "\n".join(
        (
            rule.decision_question,
            *rule.applicable_conditions,
            *rule.reasoning_steps,
            *rule.required_evidence,
            *rule.invalidation_conditions,
        )
    )


def _build_skills(
    run_id: str,
    rules: Sequence[MethodRule],
) -> tuple[list[CandidateSelectionSkill], list[PositionLifecycleSkill]]:
    candidate = [
        _candidate_skill(run_id, category, rules) for category in CandidateSelectionCategory
    ]
    lifecycle = [
        _lifecycle_skill(run_id, category, rules) for category in PositionLifecycleCategory
    ]
    return candidate, lifecycle


def _candidate_skill(
    run_id: str,
    category: CandidateSelectionCategory,
    rules: Sequence[MethodRule],
) -> CandidateSelectionSkill:
    selected = _rules_for_skill(category, _CANDIDATE_MAP[category], rules)
    coverage, status = _skill_state(selected)
    identity = {
        "run_id": run_id,
        "skill_kind": "CANDIDATE_SELECTION",
        "category": category.value,
        "rule_ids": [item.rule_id for item in selected],
    }
    return CandidateSelectionSkill(
        skill_id=f"reviewed-skill:{content_hash(identity)}",
        run_id=run_id,
        category=category,
        rule_ids=[item.rule_id for item in selected],
        source_argument_unit_ids=sorted(
            {source.argument_unit_id for rule in selected for source in rule.source_refs}
        ),
        coverage_state=coverage,
        status=status,
        shadow_enabled=status is ReviewedSkillStatus.READY_FOR_SHADOW,
        formal_committee_weight_allowed=False,
    )


def _lifecycle_skill(
    run_id: str,
    category: PositionLifecycleCategory,
    rules: Sequence[MethodRule],
) -> PositionLifecycleSkill:
    selected = _rules_for_skill(category, _LIFECYCLE_MAP[category], rules)
    coverage, status = _skill_state(selected)
    identity = {
        "run_id": run_id,
        "skill_kind": "POSITION_LIFECYCLE",
        "category": category.value,
        "rule_ids": [item.rule_id for item in selected],
    }
    return PositionLifecycleSkill(
        skill_id=f"reviewed-skill:{content_hash(identity)}",
        run_id=run_id,
        category=category,
        rule_ids=[item.rule_id for item in selected],
        source_argument_unit_ids=sorted(
            {source.argument_unit_id for rule in selected for source in rule.source_refs}
        ),
        coverage_state=coverage,
        status=status,
        shadow_enabled=status is ReviewedSkillStatus.READY_FOR_SHADOW,
        formal_committee_weight_allowed=False,
    )


def _rules_for_skill(
    category: CandidateSelectionCategory | PositionLifecycleCategory,
    method_categories: Sequence[BookMethodCategory],
    rules: Sequence[MethodRule],
) -> list[MethodRule]:
    lexical_terms = _LEXICAL_SKILL_TERMS.get(category, ())
    selected = [
        rule
        for rule in rules
        if set(method_categories).intersection(rule.method_categories)
        or (lexical_terms and any(term in _rule_embedding_text(rule) for term in lexical_terms))
    ]
    return sorted(selected, key=lambda item: item.rule_id)


def _skill_state(
    rules: Sequence[MethodRule],
) -> tuple[SourceCoverageState, ReviewedSkillStatus]:
    if not rules:
        return (
            SourceCoverageState.AUTHOR_SILENT,
            ReviewedSkillStatus.NEEDS_USER_REVIEW,
        )
    if any(rule.status is ReviewedSkillStatus.READY_FOR_SHADOW for rule in rules):
        return (
            SourceCoverageState.COVERED,
            ReviewedSkillStatus.READY_FOR_SHADOW,
        )
    return (
        SourceCoverageState.INSUFFICIENT_SOURCE,
        ReviewedSkillStatus.NEEDS_USER_REVIEW,
    )


def _acceptance_statistics(
    *,
    records: Sequence[Any],
    decisions: Sequence[ReviewDecision],
    parsed_by_decision: dict[str, ParsedReviewConclusion],
    arguments: Sequence[ReviewedArgumentUnit],
    cards: Sequence[ViewpointCard],
    rules: Sequence[MethodRule],
    candidate_skills: Sequence[CandidateSelectionSkill],
    lifecycle_skills: Sequence[PositionLifecycleSkill],
) -> dict[str, int]:
    record_by_row = {record.excel_row: record for record in records}
    same_page_adjustments = 0
    same_page_splits = 0
    cross_page_rebuilds = 0
    topic_corrections = 0
    for decision in decisions:
        record = record_by_row[decision.excel_row]
        if decision.verdict is not ReviewVerdict.MODIFY:
            continue
        parsed = parsed_by_decision[decision.decision_id]
        if tuple(parsed.corrected_topics) != tuple(record.topics):
            topic_corrections += 1
        cross_page = any(
            len(
                {
                    page
                    for range_value in target.ranges
                    for page in range(
                        range_value.start_page,
                        range_value.end_page + 1,
                    )
                }
            )
            > 1
            for target in parsed.targets
        )
        if cross_page:
            cross_page_rebuilds += 1
        elif len(parsed.targets) > 1:
            same_page_splits += 1
        else:
            same_page_adjustments += 1
    all_skills = [*candidate_skills, *lifecycle_skills]
    return {
        "review_record_count": len(records),
        "mapped_record_count": len(decisions),
        "pass_inherited_count": sum(
            decision.verdict is ReviewVerdict.PASS
            and decision.application_status is ReviewApplicationStatus.APPLIED
            for decision in decisions
        ),
        "rejected_excluded_count": sum(
            decision.application_status is ReviewApplicationStatus.EXCLUDED
            for decision in decisions
        ),
        "same_page_adjustment_count": same_page_adjustments,
        "same_page_split_count": same_page_splits,
        "cross_page_rebuild_count": cross_page_rebuilds,
        "topic_correction_count": topic_corrections,
        "visual_participation_count": sum(
            any(ref.visual_chart_unit_ids for ref in argument.paragraph_refs)
            for argument in arguments
        ),
        "needs_user_review_count": sum(
            decision.application_status is ReviewApplicationStatus.NEEDS_USER_REVIEW
            for decision in decisions
        ),
        "reviewed_argument_count": len(arguments),
        "viewpoint_card_count": len(cards),
        "method_rule_count": len(rules),
        "candidate_selection_skill_count": len(candidate_skills),
        "position_lifecycle_skill_count": len(lifecycle_skills),
        "ready_for_shadow_count": sum(
            skill.status is ReviewedSkillStatus.READY_FOR_SHADOW for skill in all_skills
        ),
        "needs_user_review_skill_count": sum(
            skill.status is ReviewedSkillStatus.NEEDS_USER_REVIEW for skill in all_skills
        ),
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
        raise ValueError("reviewed embedding asset has no tokenizer files")
    return content_hash(tokenizer_files)


__all__ = [
    "INSUFFICIENT_SOURCE",
    "PIPELINE_VERSION",
    "ReviewedBookSkillService",
    "ReviewedExecution",
]
