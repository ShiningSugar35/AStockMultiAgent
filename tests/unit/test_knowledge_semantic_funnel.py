from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
import yaml

from astock.core.object_store import ObjectStore
from astock.knowledge.config import load_distillation_rules
from astock.knowledge.semantic_funnel import (
    ParagraphizedContent,
    load_semantic_funnel_config,
    local_context_paragraph_ids,
    method_keyword_terms,
    paragraphize_zhihu_content,
)
from astock.schemas import (
    ArgumentRelationType,
    ArgumentUnitStatus,
    KeywordScreenDecision,
    ParagraphMergeAction,
    RhetoricalRole,
    SemanticFunnelConfig,
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
    scope: str
    expected_argument_ranges: list[list[int]]
    expected_question_answer_links: list[list[int]]
    valuable_argument_ranges: list[list[int]]
    valuable_annotation: str
    html: NotRequired[str]
    title: NotRequired[str]
    expected_screen: NotRequired[str]
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
    expected_roles = case["expected_roles"]
    assert len(result.paragraphs) == len(expected_roles)
    for paragraph, expected in zip(result.paragraphs, expected_roles, strict=True):
        assert {RhetoricalRole(role) for role in expected} == set(
            paragraph.rhetorical_roles
        )
    if expected_screen is KeywordScreenDecision.EXCLUDED_DERIVED:
        assert not result.argument_units
        return
    expected_ranges = case["expected_argument_ranges"]
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


def _paragraphize_overlap_text(
    tmp_path: Path,
    *,
    text: str,
    case_id: str,
    config: SemanticFunnelConfig | None = None,
) -> ParagraphizedContent:
    objects = ObjectStore(tmp_path / f"objects-{case_id}")
    body = objects.put_bytes(f"<p>{text}</p>".encode())
    metadata = objects.put_json({"case_id": case_id})
    record = ZhihuContentRecord(
        version_id=f"version:{case_id}",
        author_source_id="zhihu:test-author",
        content_id=f"content:{case_id}",
        content_type=ZhihuContentType.ANSWERS,
        canonical_url=f"https://www.zhihu.com/question/1/answer/{case_id}",
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        body_object_sha256=body.sha256,
        metadata_sha256=metadata.sha256,
        raw_source_snapshot_id=f"snapshot:{case_id}",
        content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
    )
    active_config = config or load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    return paragraphize_zhihu_content(
        record,
        run_id=f"semantic-run:{case_id}",
        object_store=objects,
        config=active_config,
        keyword_terms=method_keyword_terms(
            load_distillation_rules(
                PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
            )
        ),
    )


@pytest.mark.parametrize(
    ("text", "expected_role"),
    [
        (
            "因此必须扫码关注公众号购买付费估值课程。",
            RhetoricalRole.MARKETING,
        ),
        (
            "欢迎关注公众号，领取股票池估值课程。",
            RhetoricalRole.MARKETING,
        ),
        (
            "哈哈，今天股价下跌，股票估值真有趣。",
            RhetoricalRole.CASUAL_CHAT,
        ),
        (
            "关注公众号即可获取估值课程。",
            RhetoricalRole.MARKETING,
        ),
    ],
)
def test_derived_overlap_cannot_become_a_method_candidate(
    tmp_path: Path,
    text: str,
    expected_role: RhetoricalRole,
) -> None:
    result = _paragraphize_overlap_text(
        tmp_path,
        text=text,
        case_id=f"derived-overlap-{expected_role.value.lower()}",
    )

    assert len(result.paragraphs) == 1
    paragraph = result.paragraphs[0]
    assert paragraph.rhetorical_roles == [expected_role]
    assert paragraph.merge_action is ParagraphMergeAction.DERIVED_EXCLUDE
    assert paragraph.matched_keyword_terms
    assert result.screen.decision is KeywordScreenDecision.EXCLUDED_DERIVED
    assert not any(result.screen.matched_terms_by_category.values())
    assert result.screen.matched_paragraph_ids == []
    assert not result.argument_units


@pytest.mark.parametrize(
    "text",
    [
        "我认为广告收入是公司商业模式的核心，关键是客户粘性和毛利率。",
        "付费用户增长取决于客户粘性和竞争优势。",
        "课程类公司的盈利模式取决于续费率和获客成本。",
        "应该关注现金流与利润是否匹配。",
        "线下门店商业模式采用扫码支付，关键是支付转化率和获客成本。",
        "购买课程类公司股票时，关键是分析盈利模式和续费率。",
        "投资者应该关注公众号业务的广告收入和客户粘性。",
        "关注公众号，分析广告收入和商业模式。",
        "点击链接率是衡量广告转化效率的商业模式指标。",
        "私信领取功能带来的付费用户增长取决于客户粘性。",
    ],
)
def test_ambiguous_business_terms_remain_method_eligible(
    tmp_path: Path,
    text: str,
) -> None:
    result = _paragraphize_overlap_text(
        tmp_path,
        text=text,
        case_id=f"legitimate-overlap-{len(text)}",
    )

    assert len(result.paragraphs) == 1
    paragraph = result.paragraphs[0]
    assert RhetoricalRole.MARKETING not in paragraph.rhetorical_roles
    assert paragraph.merge_action is not ParagraphMergeAction.DERIVED_EXCLUDE
    assert paragraph.matched_keyword_terms
    assert result.screen.decision is KeywordScreenDecision.CANDIDATE
    assert result.screen.matched_paragraph_ids == [paragraph.paragraph_id]
    assert result.argument_units
    assert all(
        argument.status is not ArgumentUnitStatus.DERIVED_EXCLUDED
        for argument in result.argument_units
    )


def test_marketing_terms_configuration_is_authoritative(tmp_path: Path) -> None:
    base_config = load_semantic_funnel_config(
        PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
    )
    configured_cta = base_config.model_copy(
        update={
            "role_rule_version": "investment-rhetorical-role-config-authority-positive",
            "marketing_terms": ["点击海报"],
        }
    )
    promoted = _paragraphize_overlap_text(
        tmp_path,
        text="请点击海报了解股票估值课程。",
        case_id="configured-marketing-positive",
        config=configured_cta,
    )
    assert promoted.paragraphs[0].rhetorical_roles == [RhetoricalRole.MARKETING]
    assert (
        promoted.paragraphs[0].merge_action
        is ParagraphMergeAction.DERIVED_EXCLUDE
    )
    assert promoted.screen.decision is KeywordScreenDecision.EXCLUDED_DERIVED
    assert not promoted.argument_units

    unrelated_config = base_config.model_copy(
        update={
            "role_rule_version": "investment-rhetorical-role-config-authority-negative",
            "marketing_terms": ["现金流"],
        }
    )
    researched = _paragraphize_overlap_text(
        tmp_path,
        text="投资者应该关注公众号业务的现金流和广告收入。",
        case_id="configured-marketing-negative",
        config=unrelated_config,
    )
    assert RhetoricalRole.MARKETING not in researched.paragraphs[0].rhetorical_roles
    assert (
        researched.paragraphs[0].merge_action
        is not ParagraphMergeAction.DERIVED_EXCLUDE
    )
    assert researched.screen.decision is KeywordScreenDecision.CANDIDATE
    assert researched.argument_units


def test_long_promotional_paragraph_is_still_derived_excluded(
    tmp_path: Path,
) -> None:
    text = (
        "核心是先建立完整的估值框架并持续检验现金流、竞争优势、财务质量和风险，"
        "研究过程中还要记录假设、反证、行业供需、客户粘性、资本开支和安全边际，"
        "只有把这些因素放入同一分析链条才能减少片面判断并提高复盘质量，"
        "同时还要区分短期波动与长期价值变化，并审计数据口径和可得时间。"
        "欢迎关注公众号即可获取估值课程。"
    )
    assert len(text) > 120
    result = _paragraphize_overlap_text(
        tmp_path,
        text=text,
        case_id="long-promotional-overlap",
    )

    paragraph = result.paragraphs[0]
    assert paragraph.rhetorical_roles == [RhetoricalRole.MARKETING]
    assert paragraph.merge_action is ParagraphMergeAction.DERIVED_EXCLUDE
    assert result.screen.decision is KeywordScreenDecision.EXCLUDED_DERIVED
    assert not result.argument_units


def test_long_public_account_business_research_remains_eligible(
    tmp_path: Path,
) -> None:
    text = (
        "投资者应该关注公众号业务的广告收入和客户粘性，"
        "还要分析付费用户增长、内容成本、获客效率、毛利率和现金流质量，"
        "并结合行业竞争优势、商业模式、续费率和资本开支持续验证盈利假设，"
        "公众号业务本身只是研究对象而不是推广邀请，"
        "研究结论还必须回到可验证数据，避免把推广词汇误当作投资证据。"
    )
    assert len(text) > 120
    result = _paragraphize_overlap_text(
        tmp_path,
        text=text,
        case_id="long-legitimate-public-account-research",
    )

    paragraph = result.paragraphs[0]
    assert RhetoricalRole.MARKETING not in paragraph.rhetorical_roles
    assert paragraph.merge_action is not ParagraphMergeAction.DERIVED_EXCLUDE
    assert result.screen.decision is KeywordScreenDecision.CANDIDATE
    assert result.argument_units


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
