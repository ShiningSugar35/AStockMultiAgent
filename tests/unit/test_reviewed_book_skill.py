from __future__ import annotations

from pathlib import Path

import pytest

from astock.cli import app
from astock.knowledge.review_workbook import interpret_review_conclusion
from astock.knowledge.reviewed_repository import SourceParagraphRecord
from astock.knowledge.reviewed_service import (
    _anchor_matches,
    _build_skills,
    _expand_ranges,
)
from astock.knowledge.reviewed_storage import (
    ReviewedParquetStore,
    ReviewedScoreRow,
    ReviewedVectorRow,
)
from astock.schemas import (
    ParagraphLocator,
    ParagraphMergeAction,
    ParagraphUnit,
    ReviewParagraphRange,
    ReviewVerdict,
    ReviewWorkbookRecord,
    RhetoricalRole,
    SourceCoverageState,
)

_HASH = "1" * 64


def test_review_conclusion_builds_ordered_cross_page_and_split_targets() -> None:
    record = ReviewWorkbookRecord(
        excel_row=2,
        page_number=10,
        source_start_ordinal=2,
        source_end_ordinal=4,
        topics=["风险"],
        review_reason="边界需调整",
        image_marker="否",
        confidence=0.5,
        completeness=0.5,
        source_preview="原范围",
        conclusion=(
            "需修改：主题改为“风险、持仓”；"
            "第10页第2段~第3段【起：「甲」；止：「乙」】与"
            "第11页第1段【段首：「丙」】合并为“跨页论点”；"
            "第11页第2段~第3段【起：「丁」；止：「戊」】"
            "另立“后续论点”。"
        ),
        verdict=ReviewVerdict.MODIFY,
    )

    parsed = interpret_review_conclusion(record)

    assert parsed.uncertainty_reason is None
    assert [target.title for target in parsed.targets] == ["跨页论点", "后续论点"]
    assert [(item.start_page, item.end_page) for item in parsed.targets[0].ranges] == [
        (10, 10),
        (11, 11),
    ]
    assert parsed.corrected_topics == ("风险", "持仓")


def test_reviewed_range_keeps_cross_content_order_without_faking_ordinals() -> None:
    paragraphs = {
        (10, 2): _paragraph(page=10, ordinal=2, item_id="item-a"),
        (10, 3): _paragraph(page=10, ordinal=3, item_id="item-a"),
        (11, 1): _paragraph(page=11, ordinal=1, item_id="item-b"),
        (11, 2): _paragraph(page=11, ordinal=2, item_id="item-b"),
    }

    expanded = _expand_ranges(
        [
            ReviewParagraphRange(
                start_page=10,
                start_paragraph_ordinal=2,
                end_page=11,
                end_paragraph_ordinal=2,
            )
        ],
        paragraphs,
    )

    assert [
        (item.unit.locator.page_number, item.unit.ordinal, item.item_id) for item in expanded
    ] == [
        (10, 2, "item-a"),
        (10, 3, "item-a"),
        (11, 1, "item-b"),
        (11, 2, "item-b"),
    ]


def test_anchor_matching_normalizes_quotes_whitespace_and_ellipsis() -> None:
    assert _anchor_matches(
        "用「庄家」或「主力」这样的词汇来描述除了自己以外的投资者…",
        "用“庄家”或“主力”这样的词汇来描述除了自己以外的投资者，思考的就是怎么样打败主力。",
    )


def test_empty_author_coverage_is_explicitly_author_silent() -> None:
    candidate, lifecycle = _build_skills("reviewed-run:test", [])

    assert len(candidate) == 8
    assert len(lifecycle) == 10
    assert all(
        item.coverage_state is SourceCoverageState.AUTHOR_SILENT
        for item in (*candidate, *lifecycle)
    )
    assert all(not item.shadow_enabled for item in (*candidate, *lifecycle))
    assert all(not item.formal_committee_weight_allowed for item in (*candidate, *lifecycle))


def test_reviewed_skill_commands_are_stable_cli_entries() -> None:
    commands = {command.name for command in app.registered_commands}

    assert {
        "knowledge-reviewed-distill",
        "knowledge-reviewed-status",
        "knowledge-reviewed-audit",
        "knowledge-reviewed-shadow-context",
    } <= commands


def test_reviewed_parquet_is_idempotent_and_rejects_collision(
    tmp_path: Path,
) -> None:
    store = ReviewedParquetStore(tmp_path)
    vector = ReviewedVectorRow(
        entity_id="argument-1",
        entity_kind="REVIEWED_ARGUMENT_UNIT",
        input_object_sha256=_HASH,
        vector=(1.0, 0.0),
        token_count=1,
        chunk_count=1,
    )
    method = ReviewedVectorRow(
        entity_id="method-1",
        entity_kind="METHOD_RULE",
        input_object_sha256=_HASH,
        vector=(0.0, 1.0),
        token_count=1,
        chunk_count=1,
    )
    score = ReviewedScoreRow(
        argument_unit_id="argument-1",
        topic_relevance=0.8,
        methodological_completeness=0.7,
        category_scores={"RISK": 0.8},
        selected_categories=("RISK",),
    )
    first = store.write(
        author_source_id="author",
        run_id="run",
        manifest_id="manifest",
        vectors=[vector],
        scores=[score],
        method_vectors=[method],
    )
    second = store.write(
        author_source_id="author",
        run_id="run",
        manifest_id="manifest",
        vectors=[vector],
        scores=[score],
        method_vectors=[method],
    )
    assert first == second

    with pytest.raises(ValueError, match="collision"):
        store.write(
            author_source_id="author",
            run_id="run",
            manifest_id="manifest",
            vectors=[
                ReviewedVectorRow(
                    entity_id="argument-1",
                    entity_kind="REVIEWED_ARGUMENT_UNIT",
                    input_object_sha256=_HASH,
                    vector=(0.5, 0.5),
                    token_count=1,
                    chunk_count=1,
                )
            ],
            scores=[score],
            method_vectors=[method],
        )


def _paragraph(
    *,
    page: int,
    ordinal: int,
    item_id: str,
) -> SourceParagraphRecord:
    content_id = f"book:page:{page}"
    paragraph = ParagraphUnit(
        paragraph_id=f"paragraph:{page}:{ordinal}",
        run_id="semantic-run:test",
        author_source_id="author",
        content_type="PRIVATE_BOOK_PAGE",
        content_id=content_id,
        content_version_id=_HASH,
        ordinal=ordinal,
        locator=ParagraphLocator(
            locator_type="BOOK_PDF_NATIVE_TEXT_BLOCK",
            source_snapshot_id=f"snapshot:{page}",
            source_object_sha256=_HASH,
            content_id=content_id,
            page_number=page,
            char_start=0,
            char_end=4,
        ),
        text_object_sha256=_HASH,
        normalized_char_count=4,
        primary_role=RhetoricalRole.CLAIM,
        rhetorical_roles=[RhetoricalRole.CLAIM],
        role_scores={RhetoricalRole.CLAIM.value: 1.0},
        standalone_distillable=False,
        context_value=0.5,
        depends_on_previous=True,
        depends_on_next=False,
        merge_action=ParagraphMergeAction.MERGE_WITH_PREVIOUS,
        topic_relevance=0.5,
        methodological_completeness=0.5,
        matched_keyword_terms=[],
        reason_codes=["TEST"],
        role_rule_version="test",
    )
    return SourceParagraphRecord(
        unit=paragraph,
        item_id=item_id,
        text=f"第{page}页第{ordinal}段",
    )
