from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from astock.knowledge import (
    KnowledgeRepository,
    ZhihuManualTaskService,
    load_knowledge_sources,
)
from astock.schemas import (
    CollectionCheckpoint,
    CollectionTerminalCondition,
    KnowledgeCollectionScope,
    KnowledgeSourceRegistry,
    ZhihuCommentNode,
    ZhihuCommentPage,
    ZhihuContainerType,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
    ZhihuListingPage,
    ZhihuManualTaskStatus,
    ZhihuTransport,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _registry() -> KnowledgeSourceRegistry:
    registry = load_knowledge_sources(PROJECT_ROOT / "configs" / "knowledge_sources.yaml")
    source = next(item for item in registry.sources if item.source_id == "zhihu:mr-dang-77")
    source = source.model_copy(
        update={
            "collection_scope": KnowledgeCollectionScope(
                history_mode="FULL_ACCESSIBLE_HISTORY",
                content_types=["answers"],
                include_question_context=True,
                include_required_comment_pages=True,
                include_nested_replies=True,
                derive_author_participation_chains=True,
                incremental_updates=True,
            )
        }
    )
    return KnowledgeSourceRegistry(sources=[source])


def _content(
    content_id: str,
    completeness: ZhihuContentCompleteness,
    observed_at: datetime,
) -> ZhihuContentRecord:
    return ZhihuContentRecord(
        version_id=f"content:{content_id}:{completeness.value}",
        author_source_id="zhihu:mr-dang-77",
        content_id=content_id,
        content_type=ZhihuContentType.ANSWERS,
        canonical_url=f"https://www.zhihu.com/question/900/answer/{content_id}",
        question_id="900",
        question_title="Synthetic question",
        published_at=observed_at,
        updated_at=observed_at,
        collected_at=observed_at,
        body_object_sha256=("a" if content_id == "101" else "b") * 64,
        metadata_sha256=(
            ("c" if completeness is ZhihuContentCompleteness.LISTING_UNVERIFIED else "d")
            if content_id == "101"
            else ("8" if completeness is ZhihuContentCompleteness.LISTING_UNVERIFIED else "9")
        )
        * 64,
        raw_source_snapshot_id=f"snapshot:{content_id}:{completeness.value}",
        content_completeness=completeness,
        created_at=observed_at,
    )


def _checkpoint(
    *,
    content_id: str | None = None,
    parent_comment_id: str | None = None,
    terminal: CollectionTerminalCondition,
) -> CollectionCheckpoint:
    return CollectionCheckpoint(
        author="zhihu:mr-dang-77",
        content_type="answers",
        listing_page=0,
        listing_cursor="https://www.zhihu.com/api/v4/end",
        content_id=content_id,
        comment_parent_id=parent_comment_id,
        comment_page=0,
        comment_cursor=("https://www.zhihu.com/api/v4/comments/end" if content_id else None),
        nested_reply_cursor=(
            "https://www.zhihu.com/api/v4/replies/end" if parent_comment_id else None
        ),
        terminal_condition=terminal,
    )


def test_manual_tasks_only_pin_active_content_boundaries_and_resolve_after_recovery(
    state,
) -> None:
    repository = KnowledgeRepository(state)
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    repository.register_content(
        _content("101", ZhihuContentCompleteness.LISTING_UNVERIFIED, observed_at)
    )
    repository.register_content(
        _content("101", ZhihuContentCompleteness.DETAIL_VERIFIED, observed_at)
    )
    repository.register_content(
        _content(
            "102",
            ZhihuContentCompleteness.LISTING_UNVERIFIED,
            observed_at + timedelta(minutes=1),
        )
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:root-101",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_id="root-101",
            root_comment_id="root-101",
            collected_at=observed_at,
            child_comment_count=2,
            is_target_author=False,
            body_object_sha256="e" * 64,
            metadata_sha256="f" * 64,
            raw_source_snapshot_id="snapshot:root-101",
            created_at=observed_at,
        )
    )
    state.set_collection_checkpoint(
        _checkpoint(terminal=CollectionTerminalCondition.PAGINATION_COMPLETE),
        status="SUCCEEDED",
        object_hash="1" * 64,
    )
    state.set_collection_checkpoint(
        _checkpoint(
            content_id="101",
            terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="2" * 64,
    )
    service = ZhihuManualTaskService(state)

    first = service.refresh(_registry())

    assert {(task.response_kind, task.content_id, task.parent_comment_id) for task in first} == {
        ("CONTENT_DETAIL", "102", None),
    }
    assert all(task.status is ZhihuManualTaskStatus.OPEN for task in first)
    assert all("Synthetic question" not in task.required_action for task in first)

    repository.register_content(
        _content(
            "102",
            ZhihuContentCompleteness.DETAIL_VERIFIED,
            observed_at + timedelta(minutes=1),
        )
    )
    state.set_collection_checkpoint(
        _checkpoint(
            content_id="102",
            terminal=CollectionTerminalCondition.CONFIRMED_EMPTY,
        ),
        status="SUCCEEDED",
        object_hash="3" * 64,
    )
    for index in (1, 2):
        repository.register_comment(
            ZhihuCommentNode(
                version_id=f"comment:child-{index}",
                author_source_id="zhihu:mr-dang-77",
                content_type=ZhihuContentType.ANSWERS,
                content_id="101",
                comment_id=f"child-{index}",
                parent_comment_id="root-101",
                root_comment_id="root-101",
                collected_at=observed_at,
                is_target_author=False,
                body_object_sha256=str(index) * 64,
                metadata_sha256=str(index + 2) * 64,
                raw_source_snapshot_id=f"snapshot:child-{index}",
                created_at=observed_at,
            )
        )
    state.set_collection_checkpoint(
        _checkpoint(
            content_id="101",
            parent_comment_id="root-101",
            terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="4" * 64,
    )

    second = service.refresh(_registry())

    assert second == []
    with state.connect() as connection:
        counts = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM zhihu_manual_collection_task GROUP BY status"
            ).fetchall()
        }
    assert counts == {"RESOLVED": 1}


def test_manual_task_carries_raw_failure_evidence_and_unchanged_timestamp(state) -> None:
    repository = KnowledgeRepository(state)
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    repository.register_content(
        _content("101", ZhihuContentCompleteness.LISTING_UNVERIFIED, observed_at)
    )
    scope_id = state.upsert_collection_scope(
        author_id="zhihu:mr-dang-77",
        content_type="detail:answers:101",
        status="ACCESS_RESTRICTED",
        last_cursor="https://www.zhihu.com/api/v4/answers/101",
        terminal_condition="ACCESS_RESTRICTED",
    )
    state.record_collection_gap(
        scope_id=scope_id,
        cursor={
            "content_id": "101",
            "detail_url": "https://www.zhihu.com/api/v4/answers/101",
            "source_snapshot_id": "snapshot:access-denied",
        },
        failure_class="ACCESS_RESTRICTED",
        retryable=False,
        status="OPEN",
    )
    service = ZhihuManualTaskService(state)

    first = service.refresh(_registry())
    detail = next(task for task in first if task.response_kind == "CONTENT_DETAIL")
    assert detail.failure_class == "ACCESS_RESTRICTED"
    assert detail.source_snapshot_id == "snapshot:access-denied"
    assert detail.last_cursor == "https://www.zhihu.com/api/v4/answers/101"

    second = service.refresh(_registry())
    repeated = next(task for task in second if task.task_id == detail.task_id)
    assert repeated.updated_at == detail.updated_at


def test_refresh_resolves_open_retired_interaction_and_column_tasks(state) -> None:
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    service = ZhihuManualTaskService(state)
    retired = [
        service._task(
            observed_at,
            source_id="zhihu:mr-dang-77",
            content_type="columns",
            response_kind="COLUMN_LISTING",
            public_url="https://www.zhihu.com/people/mr-dang-77",
            failure_class="COLUMN_ENUMERATION_NOT_VERIFIED",
            required_action="Historical column task.",
        ),
        service._task(
            observed_at,
            source_id="zhihu:mr-dang-77",
            content_type="answers",
            response_kind="ROOT_COMMENTS",
            content_id="101",
            public_url="https://www.zhihu.com/question/900/answer/101",
            failure_class="ROOT_COMMENTS_NOT_COMPLETE",
            required_action="Historical root interaction task.",
        ),
        service._task(
            observed_at,
            source_id="zhihu:mr-dang-77",
            content_type="answers",
            response_kind="CHILD_COMMENTS",
            content_id="101",
            parent_comment_id="root-101",
            public_url="https://www.zhihu.com/question/900/answer/101",
            failure_class="CHILD_REPLIES_NOT_COMPLETE",
            required_action="Historical child interaction task.",
        ),
    ]
    service._persist({task.task_id: task for task in retired}, observed_at)
    state.set_collection_checkpoint(
        _checkpoint(terminal=CollectionTerminalCondition.CONFIRMED_EMPTY),
        status="SUCCEEDED",
        object_hash="5" * 64,
    )

    assert len(service.list_open()) == 3
    assert service.refresh(_registry()) == []
    with state.connect() as connection:
        statuses = connection.execute(
            "SELECT response_kind,status FROM zhihu_manual_collection_task "
            "ORDER BY response_kind"
        ).fetchall()
    assert [tuple(row) for row in statuses] == [
        ("CHILD_COMMENTS", "RESOLVED"),
        ("COLUMN_LISTING", "RESOLVED"),
        ("ROOT_COMMENTS", "RESOLVED"),
    ]


def test_refresh_creates_one_non_actionable_column_observation_gate(state) -> None:
    registry = _registry()
    source = registry.sources[0]
    source = source.model_copy(
        update={
            "collection_scope": source.collection_scope.model_copy(
                update={"container_types": [ZhihuContainerType.COLUMNS]}
            )
        }
    )
    state.set_collection_checkpoint(
        _checkpoint(terminal=CollectionTerminalCondition.CONFIRMED_EMPTY),
        status="SUCCEEDED",
        object_hash="6" * 64,
    )

    tasks = ZhihuManualTaskService(state).refresh(
        KnowledgeSourceRegistry(sources=[source])
    )

    assert len(tasks) == 1
    task = tasks[0]
    assert (task.content_type, task.response_kind) == ("columns", "COLUMN_LISTING")
    assert task.failure_class == "COLUMN_ENUMERATION_NOT_VERIFIED"
    assert task.public_url == source.profile_url
    assert task.content_id is None
    assert task.parent_comment_id is None
    assert task.last_cursor is None
    assert task.source_snapshot_id is None


def test_historical_child_count_mismatch_does_not_create_an_active_task(
    state,
) -> None:
    repository = KnowledgeRepository(state)
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    repository.register_content(
        _content("101", ZhihuContentCompleteness.LISTING_UNVERIFIED, observed_at)
    )
    repository.register_content(
        _content("101", ZhihuContentCompleteness.DETAIL_VERIFIED, observed_at)
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:root-101",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_id="root-101",
            root_comment_id="root-101",
            collected_at=observed_at,
            child_comment_count=2,
            is_target_author=False,
            body_object_sha256="e" * 64,
            metadata_sha256="f" * 64,
            raw_source_snapshot_id="snapshot:root-101",
            created_at=observed_at,
        )
    )
    state.set_collection_checkpoint(
        _checkpoint(terminal=CollectionTerminalCondition.PAGINATION_COMPLETE),
        status="SUCCEEDED",
        object_hash="1" * 64,
    )
    state.set_collection_checkpoint(
        _checkpoint(
            content_id="101",
            terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="2" * 64,
    )
    state.set_collection_checkpoint(
        _checkpoint(
            content_id="101",
            parent_comment_id="root-101",
            terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="3" * 64,
    )

    tasks = ZhihuManualTaskService(state).refresh(_registry())

    assert tasks == []


def test_terminal_listing_total_mismatch_remains_an_explicit_counted_task(state) -> None:
    repository = KnowledgeRepository(state)
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    for content_id in ("101", "102"):
        repository.register_content(
            _content(content_id, ZhihuContentCompleteness.LISTING_UNVERIFIED, observed_at)
        )
    repository.register_listing_page(
        ZhihuListingPage(
            page_id="listing:count-mismatch",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            listing_page=0,
            request_url=(
                "https://www.zhihu.com/api/v4/members/mr-dang-77/answers?limit=20&offset=0"
            ),
            is_end=True,
            content_ids=["101", "102"],
            reported_total=3,
            source_snapshot_id="snapshot:listing-count-mismatch",
            raw_object_sha256="7" * 64,
            transport=ZhihuTransport.CHROME,
            http_status=200,
            response_structure_version="fixture",
            fetched_at=observed_at,
            created_at=observed_at,
        )
    )
    state.set_collection_checkpoint(
        _checkpoint(terminal=CollectionTerminalCondition.PAGINATION_COMPLETE),
        status="SUCCEEDED",
        object_hash="7" * 64,
    )

    tasks = ZhihuManualTaskService(state).refresh(_registry())

    listing = next(task for task in tasks if task.response_kind == "LISTING")
    assert listing.failure_class == "LISTING_TOTAL_MISMATCH"
    assert (listing.expected_count, listing.collected_count) == (3, 2)
    assert listing.source_snapshot_id == "snapshot:listing-count-mismatch"


def test_historical_interaction_total_mismatch_does_not_create_an_active_task(state) -> None:
    repository = KnowledgeRepository(state)
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    repository.register_content(
        _content("101", ZhihuContentCompleteness.LISTING_UNVERIFIED, observed_at)
    )
    repository.register_content(
        _content("101", ZhihuContentCompleteness.DETAIL_VERIFIED, observed_at)
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:only-root",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_id="root-1",
            root_comment_id="root-1",
            collected_at=observed_at,
            child_comment_count=0,
            is_target_author=False,
            body_object_sha256="e" * 64,
            metadata_sha256="f" * 64,
            raw_source_snapshot_id="snapshot:root-1",
            created_at=observed_at,
        )
    )
    repository.register_comment_page(
        ZhihuCommentPage(
            page_id="comment-page:count-mismatch",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_page=0,
            request_url=(
                "https://www.zhihu.com/api/v4/comment_v5/answers/101/root_comment"
            ),
            is_end=True,
            reported_total=2,
            comment_ids=["root-1"],
            source_snapshot_id="snapshot:root-count-mismatch",
            raw_object_sha256="6" * 64,
            transport=ZhihuTransport.CHROME,
            http_status=200,
            response_structure_version="fixture",
            fetched_at=observed_at,
            created_at=observed_at,
        )
    )
    state.set_collection_checkpoint(
        _checkpoint(terminal=CollectionTerminalCondition.PAGINATION_COMPLETE),
        status="SUCCEEDED",
        object_hash="1" * 64,
    )
    state.set_collection_checkpoint(
        _checkpoint(
            content_id="101",
            terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="2" * 64,
    )

    tasks = ZhihuManualTaskService(state).refresh(_registry())

    assert tasks == []


def test_historical_interaction_pages_never_reenter_the_active_task_queue(state) -> None:
    repository = KnowledgeRepository(state)
    observed_at = datetime(2026, 7, 18, tzinfo=UTC)
    repository.register_content(
        _content("101", ZhihuContentCompleteness.LISTING_UNVERIFIED, observed_at)
    )
    repository.register_content(
        _content("101", ZhihuContentCompleteness.DETAIL_VERIFIED, observed_at)
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:root-with-child",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_id="root-1",
            root_comment_id="root-1",
            collected_at=observed_at,
            child_comment_count=1,
            is_target_author=False,
            body_object_sha256="1" * 64,
            metadata_sha256="2" * 64,
            raw_source_snapshot_id="snapshot:root-with-child",
            created_at=observed_at,
        )
    )
    repository.register_comment(
        ZhihuCommentNode(
            version_id="comment:preview-child",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_id="child-1",
            root_comment_id="root-1",
            parent_comment_id="root-1",
            collected_at=observed_at,
            child_comment_count=0,
            is_target_author=False,
            body_object_sha256="3" * 64,
            metadata_sha256="4" * 64,
            raw_source_snapshot_id="snapshot:preview-child",
            created_at=observed_at,
        )
    )
    repository.register_comment_page(
        ZhihuCommentPage(
            page_id="comment-page:root-plus-child-total",
            author_source_id="zhihu:mr-dang-77",
            content_type=ZhihuContentType.ANSWERS,
            content_id="101",
            comment_page=0,
            request_url="https://www.zhihu.com/api/v4/comment_v5/answers/101/root_comment",
            is_end=True,
            reported_total=2,
            comment_ids=["root-1", "child-1"],
            source_snapshot_id="snapshot:root-plus-child-total",
            raw_object_sha256="5" * 64,
            transport=ZhihuTransport.CHROME,
            http_status=200,
            response_structure_version="fixture",
            fetched_at=observed_at,
            created_at=observed_at,
        )
    )
    for checkpoint, object_hash in (
        (_checkpoint(terminal=CollectionTerminalCondition.PAGINATION_COMPLETE), "6" * 64),
        (
            _checkpoint(
                content_id="101",
                terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
            ),
            "7" * 64,
        ),
    ):
        state.set_collection_checkpoint(checkpoint, status="SUCCEEDED", object_hash=object_hash)

    before_child_terminal = ZhihuManualTaskService(state).refresh(_registry())

    assert before_child_terminal == []

    state.set_collection_checkpoint(
        _checkpoint(
            content_id="101",
            parent_comment_id="root-1",
            terminal=CollectionTerminalCondition.PAGINATION_COMPLETE,
        ),
        status="SUCCEEDED",
        object_hash="8" * 64,
    )
    after_child_terminal = ZhihuManualTaskService(state).refresh(_registry())

    assert after_child_terminal == []
