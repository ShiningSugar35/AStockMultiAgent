from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
import yaml

from astock.core.object_store import ObjectStore
from astock.knowledge.config import load_distillation_rules
from astock.knowledge.semantic_funnel import (
    load_semantic_funnel_config,
    local_context_paragraph_ids,
    method_keyword_terms,
    paragraphize_zhihu_content,
)
from astock.schemas import (
    ArgumentRelationType,
    ArgumentUnitStatus,
    KeywordScreenDecision,
    RhetoricalRole,
    ZhihuContentCompleteness,
    ZhihuContentRecord,
    ZhihuContentType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = PROJECT_ROOT / "tests" / "fixtures" / "knowledge" / "argument_golden_cases.yaml"


class GoldenCase(TypedDict):
    id: str
    paragraphs: list[str]
    expected_roles: list[list[str]]
    expected_relations: list[str]
    html: NotRequired[str]
    title: NotRequired[str]
    expected_screen: NotRequired[str]
    expected_argument_ranges: NotRequired[list[list[int]]]
    expected_statuses: NotRequired[list[str]]
    expected_standalone: NotRequired[list[bool]]


def _cases() -> list[GoldenCase]:
    payload = cast(
        dict[str, object],
        yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8")),
    )
    return cast(list[GoldenCase], payload["cases"])


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_argument_golden_cases(
    tmp_path: Path,
    case: GoldenCase,
) -> None:
    objects = ObjectStore(tmp_path / "objects")
    html = case.get("html")
    if html is None:
        html = "".join(f"<p>{paragraph}</p>" for paragraph in case["paragraphs"])
    body = objects.put_bytes(str(html).encode("utf-8"))
    metadata = objects.put_json({"case_id": case["id"]})
    title = case.get("title")
    record = ZhihuContentRecord(
        version_id=f"version:{case['id']}",
        author_source_id="zhihu:test-author",
        content_id=f"content:{case['id']}",
        content_type=ZhihuContentType.ANSWERS,
        canonical_url=f"https://www.zhihu.com/question/1/answer/{case['id']}",
        title=title if title else None,
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        body_object_sha256=body.sha256,
        metadata_sha256=metadata.sha256,
        raw_source_snapshot_id=f"snapshot:{case['id']}",
        content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
    )
    config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    keyword_terms = method_keyword_terms(
        load_distillation_rules(
            PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
        )
    )
    result = paragraphize_zhihu_content(
        record,
        run_id="semantic-run:golden",
        object_store=objects,
        config=config,
        keyword_terms=keyword_terms,
    )

    expected_screen = KeywordScreenDecision(str(case.get("expected_screen", "CANDIDATE")))
    assert result.screen.decision is expected_screen
    if expected_screen is KeywordScreenDecision.EXCLUDED_DERIVED:
        assert not result.argument_units
        return

    expected_roles = case["expected_roles"]
    assert len(result.paragraphs) == len(expected_roles)
    for paragraph, expected in zip(result.paragraphs, expected_roles, strict=True):
        assert {RhetoricalRole(role) for role in expected}.issubset(
            paragraph.rhetorical_roles
        )
    expected_ranges = case.get(
        "expected_argument_ranges",
        [[1, len(result.paragraphs)]],
    )
    assert [
        [argument.start_ordinal, argument.end_ordinal]
        for argument in result.argument_units
    ] == expected_ranges
    relation_types = {relation.relation_type for relation in result.relations}
    assert {
        ArgumentRelationType(relation) for relation in case["expected_relations"]
    }.issubset(relation_types)
    if "expected_statuses" in case:
        assert [argument.status for argument in result.argument_units] == [
            ArgumentUnitStatus(status) for status in case["expected_statuses"]
        ]
    if "expected_standalone" in case:
        assert [
            argument.standalone_distillable for argument in result.argument_units
        ] == case["expected_standalone"]


def test_local_context_is_previous_one_current_following_two(tmp_path: Path) -> None:
    case = next(case for case in _cases() if case["id"] == "two_argument_boundaries")
    objects = ObjectStore(tmp_path / "objects")
    html = "".join(f"<p>{paragraph}</p>" for paragraph in case["paragraphs"])
    body = objects.put_bytes(html.encode("utf-8"))
    metadata = objects.put_json({"case_id": case["id"]})
    record = ZhihuContentRecord(
        version_id="version:context",
        author_source_id="zhihu:test-author",
        content_id="content:context",
        content_type=ZhihuContentType.ARTICLES,
        canonical_url="https://zhuanlan.zhihu.com/p/1",
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        body_object_sha256=body.sha256,
        metadata_sha256=metadata.sha256,
        raw_source_snapshot_id="snapshot:context",
        content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
    )
    result = paragraphize_zhihu_content(
        record,
        run_id="semantic-run:context",
        object_store=objects,
        config=load_semantic_funnel_config(
            PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
        ),
        keyword_terms=method_keyword_terms(
            load_distillation_rules(
                PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
            )
        ),
    )
    paragraph_ids = [paragraph.paragraph_id for paragraph in result.paragraphs]
    assert local_context_paragraph_ids(list(result.paragraphs), 2) == paragraph_ids[:4]
    assert local_context_paragraph_ids(list(result.paragraphs), 4) == paragraph_ids[2:4]
