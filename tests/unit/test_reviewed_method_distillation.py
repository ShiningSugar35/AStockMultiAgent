from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.reviewed_distillation import (
    build_distillation_batch_input,
    distill_au,
    export_batch_context_file,
    validate_batch_manifest,
    validate_natural_language_output,
)
from astock.schemas.books import BookMethodCategory
from astock.schemas.knowledge_semantics import (
    ParagraphLocator,
    ParagraphMergeAction,
    ParagraphUnit,
    RhetoricalRole,
)
from astock.schemas.reviewed_distillation import (
    DistillationAUContext,
    DistillationBatchInput,
    DistillationBatchManifest,
    DistillationBatchManifestEntry,
    DistilledSourceRef,
    MechanicalDraft,
    MethodRuleDraft,
    RuleDraftOrigin,
    RuleDraftStatus,
)
from astock.schemas.reviewed_knowledge import (
    ReviewedArgumentStatus,
    ReviewedArgumentUnit,
    ReviewedParagraphRef,
)

HASH = "a" * 64
RUN_ID = "reviewed-distill:b0"
FROZEN_AT = datetime(2026, 7, 1, tzinfo=UTC)
SOURCE_RUN_ID = "semantic-run:fixture"


def _source_ref(
    *,
    argument_unit_id: str = "au:test:001",
    text_object_sha256: str = HASH,
) -> DistilledSourceRef:
    return DistilledSourceRef(
        argument_unit_id=argument_unit_id,
        paragraph_ids=["paragraph:1"],
        page_numbers=[1],
        text_object_sha256=text_object_sha256,
    )


def _context(
    *,
    text: str,
    topics: list[str] | None = None,
    mechanical_status: RuleDraftStatus | None = None,
    source_refs: list[DistilledSourceRef] | None = None,
) -> DistillationAUContext:
    return DistillationAUContext(
        run_id=RUN_ID,
        argument_unit_id="au:test:001",
        title="method distillation regression",
        text=text,
        topics=topics or ["\u65b9\u6cd5"],
        source_refs=source_refs or [_source_ref()],
        mechanical_draft=(
            MechanicalDraft(status=mechanical_status)
            if mechanical_status is not None
            else None
        ),
    )


def _ready_text() -> str:
    return (
        "Context paragraph one establishes scope. "
        "Context paragraph two establishes provenance. "
        "Context paragraph three records assumptions. "
        "Context paragraph four records review boundaries. "
        "\u5982\u679c\u91d1\u878d\u884c\u4e1a\u73b0\u91d1\u6d41"
        "\u6301\u7eed\u6539\u5584\uff0c\u5219\u6536\u5165\u589e\u957f"
        "\u83b7\u5f97\u786e\u8ba4\u3002 "
        "\u6570\u636e\u663e\u793aROE\u4e0b\u964d\uff0c\u4e14"
        "\u8d22\u52a1\u98ce\u9669\u6269\u5927\u3002 "
        "\u73b0\u91d1\u6d41\u8fde\u7eed\u4e0b\u964d\u65f6"
        "\u89c4\u5219\u5931\u6548\u3002 "
        "\u8d44\u91d1\u94fe\u8fdd\u7ea6\u662f\u5df2\u77e5"
        "\u5931\u8d25\u6a21\u5f0f\u3002 "
        "\u89c2\u5bdf\u5468\u671f\u4e3a3-6\u4e2a\u6708\uff0c"
        "\u5e76\u572812\u4e2a\u6708\u540e\u590d\u6838\u3002"
    )


def test_distill_au_rejects_mechanical_draft_ready_result() -> None:
    with pytest.raises(ValueError, match="must remain mechanical drafts"):
        MechanicalDraft(status=RuleDraftStatus.READY_FOR_SHADOW)

    rule = distill_au(
        _context(
            text=_ready_text(),
            mechanical_status=RuleDraftStatus.MECHANICAL_DRAFT,
        )
    )

    assert rule.status == RuleDraftStatus.MECHANICAL_DRAFT
    assert "mechanical_draft_needs_manual_review" in rule.uncertainty_reason


def test_distill_au_drops_first_four_mechanical_headings_from_reasoning() -> None:
    prefixes = (
        "\u5982\u56fe\u6240\u793a\u3002 "
        "\u4f8b\u5982\u672c\u6bb5\u8bf4\u660e\u3002 "
        "\u5148\u51b3\u6761\u4ef6\u5982\u4e0b\u3002 "
        "\u539f\u6587\u5b9a\u4e49\u5982\u4e0b\u3002 "
    )

    rule = distill_au(_context(text=prefixes + _ready_text()))

    assert rule.status == RuleDraftStatus.READY_FOR_SHADOW
    assert all("\u5982\u56fe" not in step for step in rule.reasoning_steps)
    assert all("\u4f8b\u5982" not in step for step in rule.reasoning_steps)
    assert all("\u5148\u51b3\u6761\u4ef6" not in step for step in rule.reasoning_steps)
    assert all("\u539f\u6587" not in step for step in rule.reasoning_steps)


def test_distill_au_treats_keyword_only_sentences_as_insufficient() -> None:
    text = (
        "\u5982\u679c\u6536\u5165\u589e\u957f\u653e\u7f13\uff0c"
        "\u957f\u671f\u53ef\u590d\u6838\u3002 "
        "\u6570\u636e\u5448\u73b0\u8d8b\u52bf\u3002 "
        "\u884c\u4e1a\u98ce\u9669\u4e0b\u964d\u3002 "
        "\u516d\u4e2a\u6708\u5185\u518d\u590d\u6838\u3002"
    )

    rule = distill_au(_context(text=text))

    assert rule.status == RuleDraftStatus.MECHANICAL_DRAFT
    assert any(
        reason.endswith("insufficient")
        or reason == "mechanical_keyword_only"
        or reason == "mechanical_draft_needs_manual_review"
        for reason in rule.uncertainty_reason
    )


def test_distill_au_requires_non_generic_case_normalization() -> None:
    text = (
        "\u67d0\u516c\u53f8\u7684\u4e2a\u6848\u9700\u8981"
        "\u5148\u5f52\u4e00\u5316\u3002 "
        + _ready_text()
    )

    rule = distill_au(_context(text=text))

    assert rule.status == RuleDraftStatus.NEEDS_USER_REVIEW
    assert "case_specific_text_requires_manual_normalization" in (
        rule.uncertainty_reason
    )


def test_distill_au_reports_forged_source_refs() -> None:
    source_refs = [_source_ref(), _source_ref()]

    rule = distill_au(_context(text=_ready_text(), source_refs=source_refs))

    assert rule.status == RuleDraftStatus.NEEDS_USER_REVIEW
    assert "source_refs_forged_or_duplicate_argument_unit" in (
        rule.uncertainty_reason
    )


def test_distill_au_accepts_ready_output_when_signature_hash_differs() -> None:
    source_ref = _source_ref()

    rule = distill_au(
        _context(
            text=_ready_text(),
            source_refs=[source_ref],
            topics=["\u94f6\u884c"],
        )
    )

    assert source_ref.text_object_sha256 != rule.input_object_hash
    assert rule.status == RuleDraftStatus.READY_FOR_SHADOW
    assert rule.origin == RuleDraftOrigin.CODEX_NATURAL_LANGUAGE
    assert "source_ref_hash_not_input_object_hash" not in rule.uncertainty_reason


def test_validate_output_reports_signature_mismatch_on_hash_change() -> None:
    text = _ready_text()
    context_hash = content_hash(
        {
            "run_id": "reviewed-distill:signature-mismatch",
            "argument_unit_id": "au:test:signature",
            "title": "method distillation signature test",
            "text": text,
        }
    )
    context = DistillationAUContext(
        run_id="reviewed-distill:signature-mismatch",
        argument_unit_id="au:test:signature",
        title="method distillation signature test",
        text=text,
        topics=["\u94f6\u884c"],
        source_refs=[
            _source_ref(
                argument_unit_id="au:test:signature",
                text_object_sha256=context_hash,
            )
        ],
        mechanical_draft=None,
    )
    rule = MethodRuleDraft(
        decision_question="Should the sector rule be applied?",
        applicable_conditions=["Apply when audited cash flow improves."],
        reasoning_steps=[
            "Confirm the metric trend.",
            "Compare it with the sector benchmark.",
            "Review all invalidation conditions.",
        ],
        required_evidence=["Audited cash-flow and return data."],
        positive_signals=["Cash flow and return on equity improve."],
        negative_signals=["Cash flow or return on equity declines."],
        invalidation_conditions=["The trend reverses for two reporting periods."],
        known_failure_modes=["Accounting restatement invalidates the inputs."],
        applicable_industries=["banking"],
        holding_horizon=["3-6 months"],
        source_refs=[
            _source_ref(
                argument_unit_id="au:test:signature",
                text_object_sha256="b" * 64,
            )
        ],
        status=RuleDraftStatus.READY_FOR_SHADOW,
        origin=RuleDraftOrigin.CODEX_NATURAL_LANGUAGE,
        argument_unit_id="au:test:signature",
        batch_id=1,
        input_object_hash=context_hash,
        uncertainty_reason=[],
    )

    result = validate_natural_language_output(context, rule)

    assert "source_ref_signature_mismatch" in result
    assert "source_ref_hash_not_input_object_hash" not in result


def test_distill_au_accepts_codex_natural_language_rule() -> None:
    text = _ready_text()
    context_hash = content_hash(
        {
            "run_id": RUN_ID,
            "argument_unit_id": "au:test:strict",
            "title": "method distillation regression",
            "text": text,
        }
    )
    source_ref = _source_ref(
        argument_unit_id="au:test:strict",
        text_object_sha256=context_hash,
    )
    context = DistillationAUContext(
        run_id=RUN_ID,
        argument_unit_id="au:test:strict",
        title="method distillation regression",
        text=text,
        topics=["\u8d22\u52a1\u8d28\u91cf"],
        source_refs=[source_ref],
    )
    rule = MethodRuleDraft(
        decision_question="How should the sector rule be applied?",
        applicable_conditions=["Apply when audited cash flow improves."],
        reasoning_steps=[
            "Confirm the metric trend.",
            "Compare it with the sector benchmark.",
            "Review all invalidation conditions.",
        ],
        required_evidence=["Audited cash-flow and return data."],
        positive_signals=["Cash flow and return on equity improve."],
        negative_signals=["Cash flow or return on equity declines."],
        invalidation_conditions=["The trend reverses for two reporting periods."],
        known_failure_modes=["Accounting restatement invalidates the inputs."],
        applicable_industries=["financial services"],
        holding_horizon=["3-6 months", "12 months"],
        source_refs=[source_ref],
        status=RuleDraftStatus.READY_FOR_SHADOW,
        origin=RuleDraftOrigin.CODEX_NATURAL_LANGUAGE,
        argument_unit_id=context.argument_unit_id,
        batch_id=1,
        input_object_hash=context_hash,
        uncertainty_reason=[],
    )

    assert validate_natural_language_output(context, rule) == []


def test_validate_batch_manifest_catches_duplicate_and_missing_au_ids() -> None:
    manifest = DistillationBatchManifest(
        generated_by="worker-b-recovery",
        reviewed_run_id=RUN_ID,
        total_au=2,
        batches=[
            DistillationBatchManifestEntry(batch_id=1, au_count=1, au_ids=["au:1"]),
            DistillationBatchManifestEntry(batch_id=2, au_count=1, au_ids=["au:1"]),
        ],
    )

    result = validate_batch_manifest(manifest, expected_au_ids={"au:1", "au:2"})

    assert result.duplicate_au_ids == ["au:1"]
    assert result.missing_au_ids == ["au:2"]
    assert result.is_complete is False
    assert result.processed_count == 1
    assert result.expected_count == 2


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _paragraph(
    object_store: ObjectStore,
    *,
    paragraph_id: str,
    item_id: str,
    content_id: str,
    page_number: int,
    ordinal: int,
    text: str,
) -> tuple[ParagraphUnit, str]:
    text_hash = object_store.put_bytes(text.encode("utf-8")).sha256
    locator = ParagraphLocator(
        locator_type="PDF_TEXT",
        source_snapshot_id=f"snapshot:{item_id}",
        source_object_sha256="c" * 64,
        content_id=content_id,
        page_number=page_number,
        char_start=0,
        char_end=len(text),
        created_at=FROZEN_AT,
    )
    return (
        ParagraphUnit(
            paragraph_id=paragraph_id,
            run_id=SOURCE_RUN_ID,
            author_source_id="author:fixture",
            content_type="BOOK",
            content_id=content_id,
            content_version_id=f"version:{content_id}",
            ordinal=ordinal,
            locator=locator,
            text_object_sha256=text_hash,
            normalized_char_count=len(text),
            primary_role=RhetoricalRole.CLAIM,
            rhetorical_roles=[RhetoricalRole.CLAIM],
            role_scores={RhetoricalRole.CLAIM.value: 1.0},
            standalone_distillable=True,
            context_value=1.0,
            depends_on_previous=False,
            depends_on_next=False,
            merge_action=ParagraphMergeAction.KEEP_AS_ARGUMENT,
            topic_relevance=1.0,
            methodological_completeness=1.0,
            matched_keyword_terms=["cash-flow"],
            reason_codes=["fixture"],
            role_rule_version="fixture-v1",
            created_at=FROZEN_AT,
        ),
        item_id,
    )


def _argument(
    object_store: ObjectStore,
    *,
    argument_id: str,
    paragraphs: list[tuple[ParagraphUnit, str]],
    status: ReviewedArgumentStatus,
) -> ReviewedArgumentUnit:
    body = "\n".join(
        object_store.get_bytes(paragraph.text_object_sha256).decode("utf-8")
        for paragraph, _item_id in paragraphs
    )
    body_hash = object_store.put_bytes(body.encode("utf-8")).sha256
    refs = [
        ReviewedParagraphRef(
            ref_ordinal=index,
            source_paragraph_id=paragraph.paragraph_id,
            item_id=item_id,
            content_id=paragraph.content_id,
            page_number=paragraph.locator.page_number or 1,
            paragraph_ordinal=paragraph.ordinal,
            paragraph_head=body[:40],
            text_object_sha256=paragraph.text_object_sha256,
            rhetorical_role=paragraph.primary_role,
            rhetorical_roles=paragraph.rhetorical_roles,
            source_snapshot_id=paragraph.locator.source_snapshot_id,
            locator=paragraph.locator,
            created_at=FROZEN_AT,
        )
        for index, (paragraph, item_id) in enumerate(paragraphs, start=1)
    ]
    return ReviewedArgumentUnit(
        argument_unit_id=argument_id,
        run_id=RUN_ID,
        decision_ids=[f"decision:{argument_id}"],
        author_source_id="author:fixture",
        title=f"title {argument_id}",
        paragraph_refs=refs,
        start_locator=refs[0].locator,
        end_locator=refs[-1].locator,
        text_object_sha256=body_hash,
        rhetorical_roles=[RhetoricalRole.CLAIM],
        relations=[],
        method_categories=[BookMethodCategory.FINANCIAL_QUALITY],
        topic_relevance=1.0,
        methodological_completeness=1.0,
        standalone_distillable=status is ReviewedArgumentStatus.READY,
        status=status,
        source_argument_unit_ids=[f"source:{argument_id}"],
        source_snapshot_ids=list(
            dict.fromkeys(ref.source_snapshot_id for ref in refs)
        ),
        reason_codes=["fixture"],
        created_at=FROZEN_AT,
    )


def _insert_batch_fixture(
    state: StateStore,
    arguments: list[
        tuple[ReviewedArgumentUnit, list[tuple[ParagraphUnit, str]]]
    ],
) -> None:
    with state.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO knowledge_reviewed_semantic_run("
            "run_id,source_run_id,author_source_id,review_workbook_hash,"
            "source_pdf_hash,input_manifest_hash,pipeline_version,stage,"
            "review_record_count,reviewed_argument_count,unresolved_count,"
            "run_object_hash,run_json,started_at,finished_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                RUN_ID,
                SOURCE_RUN_ID,
                "author:fixture",
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "fixture-v1",
                "ARGUMENTS_BUILT",
                len(arguments),
                len(arguments),
                0,
                "1" * 64,
                "{}",
                FROZEN_AT.isoformat(),
                None,
            ),
        )
        inserted_paragraphs: set[str] = set()
        for argument, paragraphs in arguments:
            for paragraph, item_id in paragraphs:
                if paragraph.paragraph_id in inserted_paragraphs:
                    continue
                inserted_paragraphs.add(paragraph.paragraph_id)
                connection.execute(
                    "INSERT INTO knowledge_paragraph_unit("
                    "paragraph_id,run_id,item_id,author_source_id,content_id,"
                    "ordinal,text_object_hash,primary_role,"
                    "standalone_distillable,merge_action,unit_json,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        paragraph.paragraph_id,
                        paragraph.run_id,
                        item_id,
                        paragraph.author_source_id,
                        paragraph.content_id,
                        paragraph.ordinal,
                        paragraph.text_object_sha256,
                        paragraph.primary_role.value,
                        int(paragraph.standalone_distillable),
                        paragraph.merge_action.value,
                        paragraph.model_dump_json(),
                        FROZEN_AT.isoformat(),
                    ),
                )
            encoded = argument.model_dump_json()
            connection.execute(
                "INSERT INTO knowledge_reviewed_argument_unit("
                "argument_unit_id,run_id,decision_id,author_source_id,title,"
                "text_object_hash,status,topic_relevance,"
                "methodological_completeness,standalone_distillable,"
                "method_categories_json,rhetorical_roles_json,lineage_json,"
                "unit_object_hash,unit_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    argument.argument_unit_id,
                    argument.run_id,
                    argument.decision_ids[0],
                    argument.author_source_id,
                    argument.title,
                    argument.text_object_sha256,
                    argument.status.value,
                    argument.topic_relevance,
                    argument.methodological_completeness,
                    int(argument.standalone_distillable),
                    _json([item.value for item in argument.method_categories]),
                    _json([item.value for item in argument.rhetorical_roles]),
                    _json(
                        {
                            "decision_ids": argument.decision_ids,
                            "source_argument_unit_ids": (
                                argument.source_argument_unit_ids
                            ),
                            "source_snapshot_ids": argument.source_snapshot_ids,
                        }
                    ),
                    sha256_bytes(encoded.encode("utf-8")),
                    encoded,
                    FROZEN_AT.isoformat(),
                ),
            )
            for ref in argument.paragraph_refs:
                connection.execute(
                    "INSERT INTO knowledge_reviewed_argument_paragraph_ref("
                    "argument_unit_id,ref_ordinal,source_paragraph_id,item_id,"
                    "content_id,page_number,paragraph_ordinal,paragraph_head,"
                    "text_object_hash,rhetorical_role,source_snapshot_id,"
                    "locator_json,visual_evidence_ids_json,"
                    "visual_chart_unit_ids_json"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        argument.argument_unit_id,
                        ref.ref_ordinal,
                        ref.source_paragraph_id,
                        ref.item_id,
                        ref.content_id,
                        ref.page_number,
                        ref.paragraph_ordinal,
                        ref.paragraph_head,
                        ref.text_object_sha256,
                        ref.rhetorical_role.value,
                        ref.source_snapshot_id,
                        ref.locator.model_dump_json(),
                        _json(ref.visual_evidence_ids),
                        _json(ref.visual_chart_unit_ids),
                    ),
                )


@pytest.fixture
def batch_fixture(
    tmp_path: Path,
) -> tuple[StateStore, ObjectStore, dict[str, ReviewedArgumentUnit]]:
    state = StateStore(tmp_path / "state.sqlite")
    state.migrate()
    object_store = ObjectStore(tmp_path / "objects")
    paragraph_z = _paragraph(
        object_store,
        paragraph_id="paragraph:z",
        item_id="item:first",
        content_id="content:first",
        page_number=3,
        ordinal=2,
        text="first cross-page paragraph",
    )
    paragraph_a = _paragraph(
        object_store,
        paragraph_id="paragraph:a",
        item_id="item:second",
        content_id="content:second",
        page_number=1,
        ordinal=1,
        text="second cross-page paragraph",
    )
    paragraph_two = _paragraph(
        object_store,
        paragraph_id="paragraph:two",
        item_id="item:third",
        content_id="content:third",
        page_number=7,
        ordinal=1,
        text="review-required paragraph",
    )
    argument_one = _argument(
        object_store,
        argument_id="au:one",
        paragraphs=[paragraph_z, paragraph_a],
        status=ReviewedArgumentStatus.READY,
    )
    argument_two = _argument(
        object_store,
        argument_id="au:two",
        paragraphs=[paragraph_two],
        status=ReviewedArgumentStatus.NEEDS_USER_REVIEW,
    )
    arguments = {
        argument_one.argument_unit_id: argument_one,
        argument_two.argument_unit_id: argument_two,
    }
    _insert_batch_fixture(
        state,
        [
            (argument_one, [paragraph_z, paragraph_a]),
            (argument_two, [paragraph_two]),
        ],
    )
    return state, object_store, arguments


def test_build_batch_context_preserves_requested_and_lineage_order(
    batch_fixture: tuple[
        StateStore,
        ObjectStore,
        dict[str, ReviewedArgumentUnit],
    ],
) -> None:
    state, object_store, arguments = batch_fixture

    batch = build_distillation_batch_input(
        state=state,
        object_store=object_store,
        reviewed_run_id=RUN_ID,
        batch_id=7,
        au_ids=["au:two", "au:one"],
    )

    assert [item.argument_unit_id for item in batch.arguments] == [
        "au:two",
        "au:one",
    ]
    first_argument = batch.arguments[1]
    assert first_argument.text == object_store.get_bytes(
        arguments["au:one"].text_object_sha256
    ).decode("utf-8")
    assert first_argument.topics == [BookMethodCategory.FINANCIAL_QUALITY.value]
    assert first_argument.source_refs[0].paragraph_ids == [
        "paragraph:z",
        "paragraph:a",
    ]
    assert first_argument.source_refs[0].page_numbers == [1, 3]
    assert (
        first_argument.source_refs[0].text_object_sha256
        == arguments["au:one"].text_object_sha256
    )
    assert first_argument.mechanical_draft is not None
    assert (
        first_argument.mechanical_draft.status
        is RuleDraftStatus.MECHANICAL_DRAFT
    )

    with pytest.raises(ValueError, match="must be unique"):
        build_distillation_batch_input(
            state=state,
            object_store=object_store,
            reviewed_run_id=RUN_ID,
            batch_id=7,
            au_ids=["au:one", "au:one"],
        )
    with pytest.raises(ValueError, match="AUs missing"):
        build_distillation_batch_input(
            state=state,
            object_store=object_store,
            reviewed_run_id=RUN_ID,
            batch_id=7,
            au_ids=["au:missing"],
        )


def test_build_batch_context_rejects_forged_paragraph_lineage(
    batch_fixture: tuple[
        StateStore,
        ObjectStore,
        dict[str, ReviewedArgumentUnit],
    ],
) -> None:
    state, object_store, _arguments = batch_fixture
    with state.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE knowledge_reviewed_argument_paragraph_ref "
            "SET source_paragraph_id=? "
            "WHERE argument_unit_id=? AND ref_ordinal=1",
            ("paragraph:forged", "au:one"),
        )

    with pytest.raises(ValueError, match="paragraph projection mismatch"):
        build_distillation_batch_input(
            state=state,
            object_store=object_store,
            reviewed_run_id=RUN_ID,
            batch_id=1,
            au_ids=["au:one"],
        )


def test_build_batch_context_rejects_object_hash_mismatch(
    batch_fixture: tuple[
        StateStore,
        ObjectStore,
        dict[str, ReviewedArgumentUnit],
    ],
) -> None:
    state, object_store, arguments = batch_fixture
    object_store.path_for(
        arguments["au:one"].paragraph_refs[0].text_object_sha256
    ).write_bytes(b"tampered")

    with pytest.raises(StorageError, match="verification failed"):
        build_distillation_batch_input(
            state=state,
            object_store=object_store,
            reviewed_run_id=RUN_ID,
            batch_id=1,
            au_ids=["au:one"],
        )


def test_export_batch_context_file_is_byte_idempotent(
    batch_fixture: tuple[
        StateStore,
        ObjectStore,
        dict[str, ReviewedArgumentUnit],
    ],
    tmp_path: Path,
) -> None:
    state, object_store, _arguments = batch_fixture
    output_dir = tmp_path / "codex-inputs"

    first_path, first_hash = export_batch_context_file(
        state=state,
        object_store=object_store,
        reviewed_run_id=RUN_ID,
        batch_id=12,
        au_ids=["au:one", "au:two"],
        output_dir=output_dir,
    )
    first_bytes = first_path.read_bytes()
    second_path, second_hash = export_batch_context_file(
        state=state,
        object_store=object_store,
        reviewed_run_id=RUN_ID,
        batch_id=12,
        au_ids=["au:one", "au:two"],
        output_dir=output_dir,
    )

    assert first_path == second_path
    assert first_hash == second_hash == sha256_bytes(first_bytes)
    assert second_path.read_bytes() == first_bytes
    exported = DistillationBatchInput.model_validate_json(first_bytes)
    assert exported.schema_version == "1.0"
    assert [item.argument_unit_id for item in exported.arguments] == [
        "au:one",
        "au:two",
    ]
