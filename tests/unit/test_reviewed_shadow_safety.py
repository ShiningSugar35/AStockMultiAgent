from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.reviewed_repository import ReviewedKnowledgeRepository
from astock.knowledge.reviewed_service import (
    INSUFFICIENT_SOURCE,
    _build_rule_draft,
    _rule_cluster_status,
)
from astock.schemas import (
    BookMethodCategory,
    CandidateSelectionCategory,
    CandidateSelectionSkill,
    MethodRule,
    ParagraphLocator,
    PositionLifecycleCategory,
    PositionLifecycleSkill,
    ReviewApplicationStatus,
    ReviewArgumentTarget,
    ReviewDecision,
    ReviewedArgumentStatus,
    ReviewedArgumentUnit,
    ReviewedAuthorSkillCoverage,
    ReviewedCoverageReport,
    ReviewedParagraphRef,
    ReviewedRunStage,
    ReviewedSemanticRun,
    ReviewedShadowBundle,
    ReviewedSkillStatus,
    ReviewedSourceRef,
    ReviewParagraphRange,
    ReviewVerdict,
    RhetoricalRole,
    SourceCoverageState,
)

HASH = "a" * 64
RUN_ID = "reviewed-run:shadow-test"
AUTHOR_SOURCE_ID = "author:test"


def _locator(*, page: int) -> ParagraphLocator:
    return ParagraphLocator(
        locator_type="BOOK_PDF_NATIVE_TEXT_BLOCK",
        source_snapshot_id=f"snapshot:{page}",
        source_object_sha256=HASH,
        content_id=f"book:page:{page}",
        page_number=page,
        char_start=0,
        char_end=24,
    )


def _paragraph_ref(
    *,
    page: int,
    ordinal: int,
    head: str,
    source_paragraph_id: str,
    item_id: str,
    source_snapshot_id: str | None = None,
) -> ReviewedParagraphRef:
    return ReviewedParagraphRef(
        ref_ordinal=ordinal,
        source_paragraph_id=source_paragraph_id,
        item_id=item_id,
        content_id=f"book:page:{page}",
        page_number=page,
        paragraph_ordinal=ordinal,
        paragraph_head=head,
        text_object_sha256=HASH,
        rhetorical_role=RhetoricalRole.CLAIM,
        rhetorical_roles=[RhetoricalRole.CLAIM],
        source_snapshot_id=source_snapshot_id or f"snapshot:{page}",
        locator=_locator(page=page),
        visual_evidence_ids=[],
        visual_chart_unit_ids=[],
    )


def _argument(
    *,
    argument_id: str,
    status: ReviewedArgumentStatus,
    decision_id: str,
) -> ReviewedArgumentUnit:
    paragraph = _paragraph_ref(
        page=1,
        ordinal=1,
        head="paragraph head",
        source_paragraph_id="paragraph:1:1",
        item_id="item:1",
    )
    return ReviewedArgumentUnit(
        argument_unit_id=argument_id,
        run_id=RUN_ID,
        decision_ids=[decision_id],
        author_source_id=AUTHOR_SOURCE_ID,
        title=f"argument {argument_id}",
        paragraph_refs=[paragraph],
        start_locator=paragraph.locator,
        end_locator=paragraph.locator,
        text_object_sha256=HASH,
        rhetorical_roles=[RhetoricalRole.CLAIM],
        relations=[],
        method_categories=[BookMethodCategory.BUSINESS_MODEL],
        topic_relevance=0.72,
        methodological_completeness=0.84,
        standalone_distillable=status is ReviewedArgumentStatus.READY,
        status=status,
        source_argument_unit_ids=[argument_id],
        source_snapshot_ids=[paragraph.source_snapshot_id],
        reason_codes=["AGENT"],
    )


def _method_rule(
    *, rule_id: str, status: ReviewedSkillStatus, argument_id: str
) -> MethodRule:
    return MethodRule(
        rule_id=f"method-rule:{rule_id}",
        run_id=RUN_ID,
        semantic_signature_sha256=hashlib.sha256(rule_id.encode("utf-8")).hexdigest(),
        decision_question=f"Decision question for {rule_id}",
        applicable_conditions=[f"Condition for {rule_id}"],
        reasoning_steps=["Reasoning based on paragraph evidence."],
        required_evidence=[f"Evidence for {rule_id}"],
        positive_signals=[f"Positive signal for {rule_id}"],
        negative_signals=[f"Negative signal for {rule_id}"],
        invalidation_conditions=[f"Invalidation for {rule_id}"],
        known_failure_modes=[f"Failure mode for {rule_id}"],
        applicable_industries=["manufacturing"],
        holding_horizon=["12 months"],
        method_categories=[BookMethodCategory.BUSINESS_MODEL],
        source_refs=[
            ReviewedSourceRef(
                argument_unit_id=argument_id,
                paragraph_ids=["paragraph:1:1"],
                page_numbers=[1],
                text_object_sha256=HASH,
            )
        ],
        status=status,
    )


def _ready_candidate_skill(*, skill_id: str, rule_ids: list[str]) -> CandidateSelectionSkill:
    return CandidateSelectionSkill(
        skill_id=skill_id,
        run_id=RUN_ID,
        category=CandidateSelectionCategory.BUSINESS_MODEL,
        rule_ids=rule_ids,
        source_argument_unit_ids=["au:ready"],
        coverage_state=SourceCoverageState.COVERED,
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        shadow_enabled=True,
        formal_committee_weight_allowed=False,
    )


def _needs_candidate_skill(*, skill_id: str, rule_ids: list[str]) -> CandidateSelectionSkill:
    return CandidateSelectionSkill(
        skill_id=skill_id,
        run_id=RUN_ID,
        category=CandidateSelectionCategory.VALUATION,
        rule_ids=rule_ids,
        source_argument_unit_ids=["au:needs"],
        coverage_state=SourceCoverageState.AUTHOR_SILENT,
        status=ReviewedSkillStatus.NEEDS_USER_REVIEW,
        shadow_enabled=False,
        formal_committee_weight_allowed=False,
    )


def _lifecycle_ready() -> PositionLifecycleSkill:
    return PositionLifecycleSkill(
        skill_id="skill:lifecycle-ready",
        run_id=RUN_ID,
        category=PositionLifecycleCategory.ENTRY,
        rule_ids=["method-rule:ready"],
        source_argument_unit_ids=["au:ready"],
        coverage_state=SourceCoverageState.COVERED,
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        shadow_enabled=True,
        formal_committee_weight_allowed=False,
    )


def _seed_source_run(state: StateStore) -> str:
    run_id = "source-run:test"
    with state.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_semantic_run("
            "run_id,author_source_id,input_manifest_hash,pipeline_version,"
            "stage,run_json,started_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                AUTHOR_SOURCE_ID,
                HASH,
                "knowledge-funnel-v1",
                "COMPLETE",
                "{}",
                datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
                datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
            ),
        )
    return run_id


def _seed_reviewed_run(repository: ReviewedKnowledgeRepository, source_run_id: str) -> None:
    repository.save_run(
        ReviewedSemanticRun(
            run_id=RUN_ID,
            source_run_id=source_run_id,
            author_source_id=AUTHOR_SOURCE_ID,
            review_workbook_sha256=HASH,
            source_pdf_sha256=HASH,
            input_manifest_sha256=HASH,
            pipeline_version="reviewed-book-skill-distillation-v2",
            stage=ReviewedRunStage.COMPLETE,
            review_record_count=1,
            reviewed_argument_count=2,
            unresolved_count=0,
            started_at=datetime(2024, 1, 1, tzinfo=UTC),
            finished_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )


def _seed_coverage(repository: ReviewedKnowledgeRepository) -> None:
    repository.save_coverage(
        ReviewedCoverageReport(
            report_id="coverage:shadow-test",
            run_id=RUN_ID,
            coverage_status="COMPLETE",
            review_record_count=1,
            mapped_record_count=1,
            reviewed_argument_count=2,
            visual_argument_count=0,
            visual_ref_count=0,
            unresolved_excel_rows=[],
            source_run_unchanged=True,
            source_embedding_reused=False,
            source_skill_reused=False,
            foreign_key_check_passed=True,
            integrity_check_passed=True,
            acceptance_statistics={},
        )
    )


def _seed_base_distillation(repository: ReviewedKnowledgeRepository) -> None:
    _seed_coverage(repository)


def _ready_bundle(
    *,
    all_rule_ids: list[str],
    shadow_rule_ids: list[str],
    ready_skill_ids: list[str],
    needs_skill_ids: list[str] | None = None,
) -> ReviewedShadowBundle:
    return ReviewedShadowBundle(
        bundle_id="bundle:shadow-test",
        run_id=RUN_ID,
        ready_skill_ids=ready_skill_ids,
        needs_user_review_skill_ids=needs_skill_ids or [],
        all_rule_ids=all_rule_ids,
        shadow_rule_ids=shadow_rule_ids,
        source_argument_unit_ids=["au:ready", "au:needs"],
        formal_committee_weight_allowed=False,
    )


def _seed_rules_arguments(
    repository: ReviewedKnowledgeRepository,
    argument_ids: list[str],
    source_argument_ids: list[str],
) -> None:
    decision_rows = [
        ReviewDecision(
            decision_id=f"decision:{argument_id}",
            run_id=RUN_ID,
            excel_row=idx + 2,
            source_argument_unit_id=source_argument_id,
            verdict=ReviewVerdict.PASS,
            application_status=ReviewApplicationStatus.APPLIED,
            targets=[
                ReviewArgumentTarget(
                    title=f"decision {argument_id}",
                    ranges=[
                        ReviewParagraphRange(
                            start_page=1,
                            start_paragraph_ordinal=1,
                            end_page=1,
                            end_paragraph_ordinal=1,
                        )
                    ],
                    topics=["BUSINESS_MODEL"],
                )
            ],
            corrected_topics=[],
            uncertainty_reason=None,
            review_conclusion_sha256=HASH,
        )
        for idx, (argument_id, source_argument_id) in enumerate(
            zip(argument_ids, source_argument_ids, strict=False), start=0
        )
    ]
    repository.save_decisions(decision_rows)
    arguments = []
    for idx, argument_id in enumerate(argument_ids, start=1):
        paragraph = _paragraph_ref(
            page=idx,
            ordinal=1,
            head=f"paragraph head {argument_id}",
            source_paragraph_id=f"paragraph:{idx}:1",
            item_id=f"item:{idx}",
        )
        arguments.append(
            _argument(
                argument_id=argument_id,
                status=ReviewedArgumentStatus.READY,
                decision_id=f"decision:{argument_id}",
            ).model_copy(
                update={
                    "paragraph_refs": [paragraph],
                    "start_locator": paragraph.locator,
                    "end_locator": paragraph.locator,
                }
            )
        )
    repository.save_arguments(
        arguments=arguments,
        visual_lookup={},
    )


def _seed_source_units(state: StateStore, source_run_id: str, argument_ids: list[str]) -> list[str]:
    source_ids: list[str] = []
    now = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    with state.transaction() as connection:
        for idx, argument_id in enumerate(argument_ids, start=1):
            source_id = f"source-au:{argument_id}"
            item_id = f"item:{idx}"
            paragraph_id = f"paragraph:{idx}:1"
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_semantic_content_item("
                "item_id,run_id,author_source_id,content_type,content_id,"
                "content_version_id,source_snapshot_id,source_object_hash,"
                "normalized_object_hash,paragraph_count,item_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    source_run_id,
                    AUTHOR_SOURCE_ID,
                    "BOOK",
                    f"book:page:{idx}",
                    f"snapshot:{idx}",
                    f"snapshot:{idx}",
                    HASH,
                    HASH,
                    1,
                    "{}",
                    now,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_paragraph_unit("
                "paragraph_id,run_id,item_id,author_source_id,content_id,"
                "ordinal,text_object_hash,primary_role,standalone_distillable,"
                "merge_action,unit_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    paragraph_id,
                    source_run_id,
                    item_id,
                    AUTHOR_SOURCE_ID,
                    f"book:page:{idx}",
                    1,
                    HASH,
                    "CLAIM",
                    1,
                    "KEEP",
                    "{}",
                    now,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_argument_unit("
                "argument_unit_id,run_id,item_id,author_source_id,content_id,"
                "start_ordinal,end_ordinal,text_object_hash,status,"
                "topic_relevance,methodological_completeness,unit_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    source_run_id,
                    item_id,
                    AUTHOR_SOURCE_ID,
                    f"book:page:{idx}",
                    1,
                    1,
                    HASH,
                    "READY",
                    0.8,
                    0.8,
                    "{}",
                    now,
                ),
            )
            source_ids.append(source_id)
    return source_ids


def _default_author_coverage() -> ReviewedAuthorSkillCoverage:
    return ReviewedAuthorSkillCoverage(
        coverage_id="coverage:author:shadow-test",
        run_id=RUN_ID,
        author_source_id=AUTHOR_SOURCE_ID,
        candidate_selection={
            item: SourceCoverageState.COVERED for item in CandidateSelectionCategory
        },
        position_lifecycle={
            item: SourceCoverageState.COVERED for item in PositionLifecycleCategory
        },
        source_argument_count=2,
        ready_for_shadow_count=2,
        needs_user_review_count=2,
    )


def _tamper_rule_status_json(
    state: StateStore,
    *,
    rule_id: str,
    new_status: str,
) -> None:
    with state.connect() as connection:
        row = connection.execute(
            "SELECT rule_json FROM knowledge_method_rule WHERE rule_id=?",
            (rule_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"missing rule id: {rule_id}")
    rule_json = json.loads(row["rule_json"])
    rule_json["status"] = new_status
    with state.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_method_rule SET rule_json=? WHERE rule_id=?",
            (json.dumps(rule_json, ensure_ascii=False), rule_id),
        )


def test_shadow_context_filters_non_ready_rules_and_counts_them(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    source_run_id = _seed_source_run(state)
    repository = ReviewedKnowledgeRepository(state, object_store)
    _seed_reviewed_run(repository, source_run_id)
    _seed_base_distillation(repository)

    ready_rule = _method_rule(
        rule_id="ready",
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        argument_id="au:ready",
    )
    needs_rule = _method_rule(
        rule_id="needs",
        status=ReviewedSkillStatus.NEEDS_USER_REVIEW,
        argument_id="au:needs",
    )
    ready_skill = _ready_candidate_skill(
        skill_id="skill:ready-business",
        rule_ids=[ready_rule.rule_id, needs_rule.rule_id],
    )
    needs_skill = _needs_candidate_skill(
        skill_id="skill:needs-business",
        rule_ids=[needs_rule.rule_id],
    )
    source_ids = _seed_source_units(state, source_run_id, ["au:ready", "au:needs"])
    _seed_rules_arguments(repository, ["au:ready", "au:needs"], source_ids)

    repository.save_distillation(
        cards=(),
        rules=[ready_rule, needs_rule],
        candidate_skills=[ready_skill, needs_skill],
        lifecycle_skills=[_lifecycle_ready()],
        author_coverage=_default_author_coverage(),
        shadow_bundle=_ready_bundle(
            all_rule_ids=[ready_rule.rule_id, needs_rule.rule_id],
            shadow_rule_ids=[ready_rule.rule_id],
            ready_skill_ids=[ready_skill.skill_id],
        ),
    )

    context = repository.shadow_context(RUN_ID)
    context = cast(dict[str, Any], context)
    assert context["run_id"] == RUN_ID
    assert context["non_ready_rule_count"] == 1
    skills = {item["skill_id"] for item in cast(list[dict[str, Any]], context["skills"])}
    rules = cast(list[dict[str, Any]], context["rules"])
    assert skills == {ready_skill.skill_id}
    assert len(rules) == 1
    assert rules[0]["rule_id"] == ready_rule.rule_id
    assert rules[0]["status"] == ReviewedSkillStatus.READY_FOR_SHADOW.value


def test_shadow_context_filters_database_status_mismatch_for_rule(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    source_run_id = _seed_source_run(state)
    repository = ReviewedKnowledgeRepository(state, object_store)
    _seed_reviewed_run(repository, source_run_id)
    _seed_base_distillation(repository)

    rule = _method_rule(
        rule_id="ready",
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        argument_id="au:ready",
    )
    ready_skill = _ready_candidate_skill(
        skill_id="skill:ready-business",
        rule_ids=[rule.rule_id],
    )
    source_ids = _seed_source_units(state, source_run_id, ["au:ready"])
    _seed_rules_arguments(repository, ["au:ready"], source_ids)
    repository.save_distillation(
        cards=(),
        rules=[rule],
        candidate_skills=[ready_skill],
        lifecycle_skills=[],
        author_coverage=_default_author_coverage(),
        shadow_bundle=_ready_bundle(
            all_rule_ids=[rule.rule_id],
            shadow_rule_ids=[rule.rule_id],
            ready_skill_ids=[ready_skill.skill_id],
        ),
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_method_rule SET status=? WHERE rule_id=?",
            (ReviewedSkillStatus.NEEDS_USER_REVIEW.value, rule.rule_id),
        )

    context = repository.shadow_context(RUN_ID)
    assert context["rules"] == []
    assert context["non_ready_rule_count"] == 1


def test_shadow_context_filters_json_status_mismatch_for_rule(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    source_run_id = _seed_source_run(state)
    repository = ReviewedKnowledgeRepository(state, object_store)
    _seed_reviewed_run(repository, source_run_id)
    _seed_base_distillation(repository)

    rule = _method_rule(
        rule_id="ready",
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        argument_id="au:ready",
    )
    ready_skill = _ready_candidate_skill(skill_id="skill:ready-business", rule_ids=[rule.rule_id])
    source_ids = _seed_source_units(state, source_run_id, ["au:ready"])
    _seed_rules_arguments(repository, ["au:ready"], source_ids)
    repository.save_distillation(
        cards=(),
        rules=[rule],
        candidate_skills=[ready_skill],
        lifecycle_skills=[],
        author_coverage=_default_author_coverage(),
        shadow_bundle=_ready_bundle(
            all_rule_ids=[rule.rule_id],
            shadow_rule_ids=[rule.rule_id],
            ready_skill_ids=[ready_skill.skill_id],
        ),
    )
    _tamper_rule_status_json(
        state,
        rule_id=rule.rule_id,
        new_status=ReviewedSkillStatus.NEEDS_USER_REVIEW.value,
    )

    context = repository.shadow_context(RUN_ID)
    assert context["rules"] == []
    assert context["non_ready_rule_count"] == 1


def test_shadow_context_returns_empty_for_no_ready_skills(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    source_run_id = _seed_source_run(state)
    repository = ReviewedKnowledgeRepository(state, object_store)
    _seed_reviewed_run(repository, source_run_id)
    _seed_base_distillation(repository)

    rule = _method_rule(
        rule_id="ready",
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        argument_id="au:needs",
    )
    needs_skill = _needs_candidate_skill(
        skill_id="skill:needs-business",
        rule_ids=[rule.rule_id],
    )
    source_ids = _seed_source_units(state, source_run_id, ["au:needs"])
    _seed_rules_arguments(repository, ["au:needs"], source_ids)

    repository.save_distillation(
        cards=(),
        rules=[rule],
        candidate_skills=[needs_skill],
        lifecycle_skills=[],
        author_coverage=_default_author_coverage(),
        shadow_bundle=_ready_bundle(
            all_rule_ids=[rule.rule_id],
            shadow_rule_ids=[],
            ready_skill_ids=[],
            needs_skill_ids=[needs_skill.skill_id],
        ),
    )

    context = repository.shadow_context(RUN_ID)
    assert context["skills"] == []
    assert context["rules"] == []
    assert context["non_ready_rule_count"] == 0


def test_shadow_context_ignores_forged_json_skill_manifest(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    source_run_id = _seed_source_run(state)
    repository = ReviewedKnowledgeRepository(state, object_store)
    _seed_reviewed_run(repository, source_run_id)
    _seed_base_distillation(repository)

    rule = _method_rule(
        rule_id="ready",
        status=ReviewedSkillStatus.READY_FOR_SHADOW,
        argument_id="au:ready",
    )
    ready_skill = _ready_candidate_skill(skill_id="skill:ready-business", rule_ids=[rule.rule_id])
    source_ids = _seed_source_units(state, source_run_id, ["au:ready"])
    _seed_rules_arguments(repository, ["au:ready"], source_ids)
    repository.save_distillation(
        cards=(),
        rules=[rule],
        candidate_skills=[ready_skill],
        lifecycle_skills=[],
        author_coverage=_default_author_coverage(),
        shadow_bundle=_ready_bundle(
            all_rule_ids=[rule.rule_id],
            shadow_rule_ids=[rule.rule_id],
            ready_skill_ids=[ready_skill.skill_id],
        ),
    )
    with state.transaction() as connection:
        row = connection.execute(
            "SELECT manifest_json FROM knowledge_reviewed_skill WHERE skill_id=?",
            (ready_skill.skill_id,),
        ).fetchone()
        if row is None:
            raise AssertionError("missing reviewed skill")
        forged = json.loads(row["manifest_json"])
        forged["status"] = ReviewedSkillStatus.NEEDS_USER_REVIEW.value
        connection.execute(
            "UPDATE knowledge_reviewed_skill SET manifest_json=? WHERE skill_id=?",
            (json.dumps(forged, ensure_ascii=False), ready_skill.skill_id),
        )

    context = repository.shadow_context(RUN_ID)
    context = cast(dict[str, Any], context)
    assert context["skills"] == []
    assert {item["rule_id"] for item in cast(list[dict[str, Any]], context["rules"])} == {
        rule.rule_id
    }


def test_rule_cluster_status_marks_uncertainty_as_needs_review() -> None:
    argument = _argument(
        argument_id="au:ready",
        status=ReviewedArgumentStatus.READY,
        decision_id="decision:ready",
    )
    _build_rule_draft(argument=argument, text="too short")
    ready_like = _build_rule_draft(
        argument=argument,
        text=(
            "If financial stability weakens over several cycles, re-check this point. "
            "If management statements are no longer credible, the thesis should be downgraded."
        ),
    )
    empty_fields = {
        "applicable_conditions": [INSUFFICIENT_SOURCE],
        "reasoning_steps": ["reasoning unavailable"],
        "required_evidence": ["evidence check needed"],
        "positive_signals": ["positive signal"],
        "negative_signals": ["negative signal"],
        "invalidation_conditions": ["invalidating trend"],
        "known_failure_modes": ["wrong cycle"],
        "applicable_industries": ["industrial goods"],
        "holding_horizon": ["1-3 years"],
    }
    assert (
        _rule_cluster_status(cluster=(ready_like,), field_values=empty_fields)
        is ReviewedSkillStatus.NEEDS_USER_REVIEW
    )
