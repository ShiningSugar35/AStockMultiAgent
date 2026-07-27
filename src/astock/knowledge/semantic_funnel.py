"""Paragraph-preserving, argument-aware semantic funnel for allowlisted knowledge."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import yaml

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.schemas import (
    ArgumentRelation,
    ArgumentRelationType,
    ArgumentUnit,
    ArgumentUnitStatus,
    BookContentClass,
    BookMethodCategory,
    DistillationClassRuleSet,
    KeywordScreenDecision,
    KeywordScreenResult,
    ParagraphLocator,
    ParagraphMergeAction,
    ParagraphUnit,
    ParagraphUnitKind,
    RhetoricalRole,
    SemanticContentItem,
    SemanticFunnelConfig,
    ZhihuContentRecord,
)

_PIPELINE_VERSION = "knowledge-semantic-funnel-three-view-v3"
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "caption",
    "dd",
    "div",
    "dt",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "td",
    "th",
}
_IGNORED_TAGS = {"script", "style", "noscript"}
_WHITESPACE = re.compile(r"\s+")
_QUESTION_END = re.compile(r"[?？]\s*$")
_RHETORICAL_QUESTION = re.compile(r"(?:难道|岂不|何尝|谁会|怎么可能).{0,40}[?？]$")
_PRONOUN_START = re.compile(r"^(?:这|其|该|上述|前者|后者|这种|这样|它|他们|这些|由此)")
_EVIDENCE = re.compile(
    r"(?:数据显示|根据|财报|公告|统计|同比|环比|占比|亿元|万元|%|％|ROE|"
    r"毛利率|现金流|存货|应收|负债率|收入|利润)",
    re.IGNORECASE,
)
_STORY_EXAMPLE = re.compile(r"(?:过去(?:一次|曾)|曾经|有一次|案例中)")
_CLAIM = re.compile(
    r"(?:我认为|我们认为|判断(?:是|为|：|:)|核心是|关键是|本质是|"
    r"意味着|可以看出|值得注意)"
)
_OPERATIONAL = re.compile(
    r"(?:应当|应该|必须|不要|只有.+才|需要满足|可以加仓|可以买入|应卖出|应退出)"
)
_EXPLANATION = re.compile(r"(?:取决于|依赖|结合|在于|由.+决定|需要考察)")
_MARKET_OBSERVATION = re.compile(
    r"(?:(?:A股|市场|股价|指数|板块).{0,20}"
    r"(?:上涨|下跌|涨停|跌停|波动|走强|走弱|牛市|熊市)|"
    r"(?:上涨|下跌|涨停|跌停).{0,20}(?:A股|市场|股价|指数|板块))",
    re.IGNORECASE,
)
_RISK = re.compile(r"(?:风险|回撤|止损|永久损失|仓位控制|失效|证伪)")
_CASUAL_ONLY = re.compile(r"^(?:哈哈+|谢谢|晚安|早安|周末愉快|收到|赞同)[!！。\s]*$")
_PROMOTION_TERM_ACTION = re.compile(
    r"^(?:(?:欢迎|请|记得|务必|必须)?"
    r"(?:点击|扫码|关注|购买|领取|订阅|加入|私信|打赏))"
)
_STRONG_PROMOTION_TERM = re.compile(
    r"^(?:点击|扫码|购买|领取|订阅|加入|私信|打赏)"
)
_EMBEDDED_CTA_PREFIX = re.compile(r"^(?:欢迎|请|记得|务必|必须)")
_CTA_PREFIX_CONTEXT = re.compile(
    r"(?:欢迎|请|记得|务必|必须|立即|马上|赶快|别忘了|扫码)"
    r"(?:大家|各位|你|您|尽快|及时|直接|先)?(?:来|去|再|并|然后)?\s*$"
)
_PROMOTION_CONTINUATION = re.compile(
    r"^[^。！？；;，、：:,\n]{0,16}"
    r"(?:领取|购买|订阅|加入|私信|打赏|获取|查看|下载)"
)
_BUSINESS_PROMOTION_PREFIX = re.compile(
    r"(?:公司|企业|平台|业务|功能|渠道|投资者|研究|分析|统计|测算|"
    r"指标|数据|模型|系统)(?:的|通过|采用|使用|涉及|中|里|对)?\s*$"
)
_BUSINESS_PROMOTION_SUFFIX = re.compile(
    r"^\s*(?:率|功能|业务|收入|用户|成本|渠道|流程|技术|模式|数据|"
    r"指标|转化|环节|场景|类(?:公司|企业)|公司|企业|股票)"
)
_CLAUSE_BOUNDARY = re.compile(r"[。！？；;，、：:,\n]+")

_METHOD_BY_CLASS = {
    BookContentClass.STOCK_SELECTION: BookMethodCategory.STOCK_SELECTION,
    BookContentClass.BUSINESS_MODEL: BookMethodCategory.BUSINESS_MODEL,
    BookContentClass.INDUSTRY: BookMethodCategory.INDUSTRY,
    BookContentClass.VALUATION: BookMethodCategory.VALUATION,
    BookContentClass.FINANCIAL_QUALITY: BookMethodCategory.FINANCIAL_QUALITY,
    BookContentClass.ENTRY: BookMethodCategory.ENTRY,
    BookContentClass.HOLDING_VALIDATION: BookMethodCategory.HOLDING,
    BookContentClass.ADD: BookMethodCategory.ADD,
    BookContentClass.TRIM: BookMethodCategory.TRIM,
    BookContentClass.EXIT: BookMethodCategory.EXIT,
    BookContentClass.RISK_CONTROL: BookMethodCategory.RISK,
    BookContentClass.FAILURE_CASE: BookMethodCategory.FAILURE_CASE,
    BookContentClass.COUNTEREVIDENCE_INVALIDATION: (
        BookMethodCategory.COUNTEREVIDENCE_INVALIDATION
    ),
    BookContentClass.REVIEW_METHOD: BookMethodCategory.REVIEW,
}


@dataclass(frozen=True, slots=True)
class ParagraphizedContent:
    item: SemanticContentItem
    paragraphs: tuple[ParagraphUnit, ...]
    screen: KeywordScreenResult
    argument_units: tuple[ArgumentUnit, ...]
    relations: tuple[ArgumentRelation, ...]


@dataclass(frozen=True, slots=True)
class _VisibleBlock:
    text: str
    char_start: int
    char_end: int
    dom_path: str


def load_semantic_funnel_config(path: Path) -> SemanticFunnelConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SemanticFunnelConfig.model_validate(payload)


def method_keyword_terms(
    rules: DistillationClassRuleSet,
) -> dict[BookMethodCategory, tuple[str, ...]]:
    grouped: dict[BookMethodCategory, set[str]] = {
        category: set() for category in BookMethodCategory
    }
    for content_class, terms in rules.content_class_terms.items():
        method = _METHOD_BY_CLASS.get(content_class)
        if method is not None:
            grouped[method].update(term for term in terms if term.strip())
    return {
        category: tuple(sorted(terms, key=lambda term: (term.casefold(), term)))
        for category, terms in grouped.items()
    }


def paragraphize_zhihu_content(
    record: ZhihuContentRecord,
    *,
    run_id: str,
    object_store: ObjectStore,
    config: SemanticFunnelConfig,
    keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
) -> ParagraphizedContent:
    raw = object_store.get_bytes(record.body_object_sha256).decode("utf-8")
    body_html = _decode_content_html(raw)
    body_source = object_store.put_bytes(body_html.encode("utf-8"))
    blocks = _visible_blocks(body_html)
    paragraph_specs: list[tuple[str, str, int, int, str]] = []
    title = record.question_title or record.title
    if title:
        normalized_title = _normalize(title)
        if normalized_title:
            title_object = object_store.put_bytes(normalized_title.encode("utf-8"))
            paragraph_specs.append(
                (
                    normalized_title,
                    title_object.sha256,
                    0,
                    len(normalized_title),
                    "metadata/question_title" if record.question_title else "metadata/title",
                )
            )
    paragraph_specs.extend(
        (block.text, body_source.sha256, block.char_start, block.char_end, block.dom_path)
        for block in blocks
    )
    input_identity = {
        "run_id": run_id,
        "content_version_id": record.version_id,
        "source_snapshot_id": record.raw_source_snapshot_id,
        "source_object_sha256": record.body_object_sha256,
        "paragraphizer_version": config.paragraphizer_version,
    }
    item_id = f"semantic-item:{content_hash(input_identity)}"
    paragraphs: list[ParagraphUnit] = []
    for ordinal, (text, source_hash, start, end, dom_path) in enumerate(
        paragraph_specs,
        start=1,
    ):
        text_object = object_store.put_bytes(text.encode("utf-8"))
        paragraph_id = "paragraph-unit:" + content_hash(
            {
                "item_id": item_id,
                "ordinal": ordinal,
                "text_object_sha256": text_object.sha256,
                "dom_path": dom_path,
            }
        )
        hits = _keyword_hits(text, keyword_terms)
        paragraph = _paragraph_contract(
            paragraph_id=paragraph_id,
            run_id=run_id,
            record=record,
            ordinal=ordinal,
            text=text,
            text_hash=text_object.sha256,
            source_hash=source_hash,
            char_start=start,
            char_end=end,
            dom_path=dom_path,
            config=config,
            hits=hits,
        )
        paragraphs.append(paragraph)
    normalized_item = object_store.put_bytes(
        "\n".join(
            object_store.get_bytes(paragraph.text_object_sha256).decode("utf-8")
            for paragraph in paragraphs
        ).encode("utf-8")
    )
    item = SemanticContentItem(
        item_id=item_id,
        run_id=run_id,
        author_source_id=record.author_source_id,
        content_type=record.content_type.value,
        content_id=record.content_id,
        content_version_id=record.version_id,
        source_snapshot_id=record.raw_source_snapshot_id,
        source_object_sha256=record.body_object_sha256,
        normalized_object_sha256=normalized_item.sha256,
        paragraph_ids=[paragraph.paragraph_id for paragraph in paragraphs],
        title_paragraph_id=(paragraphs[0].paragraph_id if title and paragraphs else None),
    )
    method_eligible_paragraphs = [
        paragraph for paragraph in paragraphs if not _derived_exclusion(paragraph)
    ]
    matched = _aggregate_keyword_hits(method_eligible_paragraphs, keyword_terms)
    matched_paragraph_ids = [
        paragraph.paragraph_id
        for paragraph in method_eligible_paragraphs
        if paragraph.matched_keyword_terms
    ]
    decision = (
        KeywordScreenDecision.CANDIDATE
        if any(matched.values())
        else KeywordScreenDecision.EXCLUDED_DERIVED
    )
    screen_payload = {
        "run_id": run_id,
        "item_id": item_id,
        "decision": decision.value,
        "matched_terms_by_category": {
            category.value: list(terms) for category, terms in matched.items()
        },
        "matched_paragraph_ids": matched_paragraph_ids,
        "keyword_rule_version": config.keyword_rule_version,
    }
    screen_object = object_store.put_json(screen_payload)
    screen = KeywordScreenResult(
        screen_id=f"keyword-screen:{content_hash(screen_payload)}",
        run_id=run_id,
        item_id=item_id,
        decision=decision,
        matched_terms_by_category=matched,
        matched_paragraph_ids=matched_paragraph_ids,
        keyword_rule_version=config.keyword_rule_version,
        result_object_sha256=screen_object.sha256,
    )
    argument_units: list[ArgumentUnit] = []
    relations: list[ArgumentRelation] = []
    if decision is KeywordScreenDecision.CANDIDATE:
        argument_units, relations = build_argument_units(
            item,
            paragraphs,
            object_store=object_store,
            config=config,
            keyword_terms=keyword_terms,
        )
    return ParagraphizedContent(
        item=item,
        paragraphs=tuple(paragraphs),
        screen=screen,
        argument_units=tuple(argument_units),
        relations=tuple(relations),
    )


def build_argument_units(
    item: SemanticContentItem,
    paragraphs: list[ParagraphUnit],
    *,
    object_store: ObjectStore,
    config: SemanticFunnelConfig,
    keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
) -> tuple[list[ArgumentUnit], list[ArgumentRelation]]:
    if [paragraph.ordinal for paragraph in paragraphs] != list(range(1, len(paragraphs) + 1)):
        raise ValueError("paragraph ordinals must be contiguous before argument building")
    texts = {
        paragraph.paragraph_id: object_store.get_bytes(paragraph.text_object_sha256).decode("utf-8")
        for paragraph in paragraphs
    }
    groups: list[list[ParagraphUnit]] = []
    current: list[ParagraphUnit] = []
    for paragraph in paragraphs:
        if _derived_exclusion(paragraph):
            if current:
                groups.append(current)
                current = []
            groups.append([paragraph])
            continue
        if current and _starts_new_argument(current, paragraph, texts, config):
            groups.append(current)
            current = []
        current.append(paragraph)
    if current:
        groups.append(current)

    all_relations: list[ArgumentRelation] = []
    units: list[ArgumentUnit] = []
    for group in groups:
        relations = _relations_for_group(item, group, config)
        all_relations.extend(relations)
        group_text = "\n".join(
            f"[{paragraph.ordinal}|{paragraph.primary_role.value}] "
            f"{texts[paragraph.paragraph_id]}"
            for paragraph in group
        )
        text_object = object_store.put_bytes(group_text.encode("utf-8"))
        excluded = all(_derived_exclusion(paragraph) for paragraph in group)
        completeness = _argument_completeness(group, relations)
        topic_relevance = _argument_topic_relevance(group)
        dependencies_closed = _dependencies_closed(group, relations)
        context_only = all(
            paragraph.primary_role
            in {
                RhetoricalRole.TITLE,
                RhetoricalRole.BACKGROUND,
                RhetoricalRole.MARKET_OBSERVATION,
                RhetoricalRole.QUESTION,
                RhetoricalRole.TRANSITION,
            }
            for paragraph in group
        )
        boundary_confidence = _boundary_confidence(group, dependencies_closed)
        maximum_chars = config.maximum_argument_unit_chars
        oversize = len(group_text) > maximum_chars
        visual_review_reasons = sorted(
            {
                reason
                for paragraph in group
                if paragraph.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
                for reason in paragraph.visual_reason_codes
            }
        )
        visual_review_required = bool(visual_review_reasons)
        standalone = (
            not excluded
            and not context_only
            and dependencies_closed
            and not oversize
            and not visual_review_required
            and completeness
            >= float(config.argument_builder["standalone_methodological_threshold"])
            and boundary_confidence
            >= float(config.argument_builder["review_boundary_threshold"])
        )
        status = (
            ArgumentUnitStatus.DERIVED_EXCLUDED
            if excluded
            else ArgumentUnitStatus.READY
            if standalone
            else ArgumentUnitStatus.NEEDS_REVIEW
        )
        methods = _methods_for_group(group, keyword_terms)
        identity = {
            "run_id": item.run_id,
            "item_id": item.item_id,
            "paragraph_ids": [paragraph.paragraph_id for paragraph in group],
            "builder_version": config.argument_builder_version,
        }
        reasons = [
            "CONTIGUOUS_SAME_ITEM_ARGUMENT",
            "DEPENDENCIES_CLOSED" if dependencies_closed else "DANGLING_CONTEXT_DEPENDENCY",
        ]
        if context_only:
            reasons.append("CONTEXT_ONLY_ARGUMENT")
        if excluded:
            reasons.append("DERIVED_NON_METHOD_CONTENT")
        if oversize:
            reasons.append("AU_OVERSIZE_REVIEW_REQUIRED")
        if visual_review_required:
            reasons.extend(["VISUAL_REVIEW_REQUIRED", *visual_review_reasons])
        units.append(
            ArgumentUnit(
                argument_unit_id=f"argument-unit:{content_hash(identity)}",
                run_id=item.run_id,
                author_source_id=item.author_source_id,
                content_type=item.content_type,
                content_id=item.content_id,
                source_snapshot_ids=[item.source_snapshot_id],
                paragraph_ids=[paragraph.paragraph_id for paragraph in group],
                relation_ids=[relation.relation_id for relation in relations],
                start_ordinal=group[0].ordinal,
                end_ordinal=group[-1].ordinal,
                text_object_sha256=text_object.sha256,
                rhetorical_roles=sorted(
                    {role for paragraph in group for role in paragraph.rhetorical_roles},
                    key=lambda role: role.value,
                ),
                status=status,
                standalone_distillable=standalone,
                topic_relevance=topic_relevance,
                methodological_completeness=completeness,
                boundary_confidence=boundary_confidence,
                method_categories=methods,
                reason_codes=sorted(set(reasons)),
                builder_version=config.argument_builder_version,
            )
        )
    _validate_partition(paragraphs, units)
    return units, all_relations


def local_context_paragraph_ids(
    paragraphs: list[ParagraphUnit],
    current_ordinal: int,
) -> list[str]:
    """Return the exact previous-1/current/following-2 same-item window."""

    if not paragraphs:
        raise ValueError("local context requires at least one paragraph")
    ordered = sorted(paragraphs, key=lambda paragraph: paragraph.ordinal)
    item_keys = {
        (
            paragraph.run_id,
            paragraph.author_source_id,
            paragraph.content_type,
            paragraph.content_id,
            paragraph.content_version_id,
            paragraph.locator.source_snapshot_id,
        )
        for paragraph in ordered
    }
    if len(item_keys) != 1:
        raise ValueError("local context cannot cross SourceItem boundaries")
    ordinals = [paragraph.ordinal for paragraph in ordered]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("local context paragraph ordinals must be unique")
    try:
        current_index = ordinals.index(current_ordinal)
    except ValueError:
        raise ValueError("current paragraph ordinal is outside the item") from None
    start = max(0, current_index - 1)
    end = min(len(ordered), current_index + 3)
    return [paragraph.paragraph_id for paragraph in ordered[start:end]]


def _paragraph_contract(
    *,
    paragraph_id: str,
    run_id: str,
    record: ZhihuContentRecord,
    ordinal: int,
    text: str,
    text_hash: str,
    source_hash: str,
    char_start: int,
    char_end: int,
    dom_path: str,
    config: SemanticFunnelConfig,
    hits: dict[BookMethodCategory, tuple[str, ...]],
) -> ParagraphUnit:
    roles = _rhetorical_roles(text, dom_path, config)
    primary = roles[0]
    depends_previous, depends_next, merge_action, standalone, reasons = _dependency_contract(
        text,
        roles,
    )
    matched_terms = sorted({term for values in hits.values() for term in values})
    topic = min(1.0, 0.55 + 0.08 * len(matched_terms)) if matched_terms else 0.0
    completeness = _paragraph_completeness(roles, standalone)
    return ParagraphUnit(
        paragraph_id=paragraph_id,
        run_id=run_id,
        author_source_id=record.author_source_id,
        content_type=record.content_type.value,
        content_id=record.content_id,
        content_version_id=record.version_id,
        ordinal=ordinal,
        locator=ParagraphLocator(
            locator_type=(
                "ZHIHU_QUESTION_TITLE"
                if dom_path == "metadata/question_title"
                else "ZHIHU_TITLE"
                if dom_path == "metadata/title"
                else "ZHIHU_VISIBLE_BLOCK"
            ),
            source_snapshot_id=record.raw_source_snapshot_id,
            source_object_sha256=source_hash,
            content_id=record.content_id,
            dom_path=dom_path,
            char_start=char_start,
            char_end=char_end,
        ),
        text_object_sha256=text_hash,
        normalized_char_count=len(text),
        primary_role=primary,
        rhetorical_roles=roles,
        role_scores={
            role.value: round(max(0.55, 0.95 - index * 0.08), 6)
            for index, role in enumerate(roles)
        },
        standalone_distillable=standalone,
        context_value=_context_value(roles),
        depends_on_previous=depends_previous,
        depends_on_next=depends_next,
        merge_action=merge_action,
        topic_relevance=topic,
        methodological_completeness=completeness,
        matched_keyword_terms=matched_terms,
        reason_codes=reasons,
        role_rule_version=config.role_rule_version,
    )


def _rhetorical_roles(
    text: str,
    dom_path: str,
    config: SemanticFunnelConfig,
) -> list[RhetoricalRole]:
    folded = text.casefold()
    is_title = dom_path.startswith("metadata/") or bool(
        re.search(r"/h[1-6](?:$|\[)", dom_path)
    )
    is_question = any(term.casefold() in folded for term in config.question_terms) or bool(
        _QUESTION_END.search(text)
    )
    is_rhetorical_question = bool(_RHETORICAL_QUESTION.search(text))
    folded_casual_terms = {term.casefold() for term in config.casual_terms}
    casual_hit = (
        bool(_CASUAL_ONLY.match(text))
        or folded.strip() in folded_casual_terms
        or (
            len(text) <= 80
            and any(folded.strip().startswith(term) for term in folded_casual_terms)
        )
    )
    marketing_hit = _promotion_intent(
        text,
        config.marketing_terms,
    )
    operational_hit = bool(_OPERATIONAL.search(text))
    claim_hit = bool(_CLAIM.search(text))
    answer_hit = any(term.casefold() in folded for term in config.answer_terms)
    explanation_hit = bool(_EXPLANATION.search(text))
    evidence_hit = bool(_EVIDENCE.search(text))
    story_example_hit = bool(_STORY_EXAMPLE.search(text))
    example_hit = story_example_hit or any(
        term.casefold() in folded for term in config.example_terms
    )
    counter_hit = any(term.casefold() in folded for term in config.counter_terms)
    conclusion_hit = any(
        term.casefold() in folded for term in config.conclusion_terms
    )
    risk_hit = bool(_RISK.search(text))
    transition_hit = (
        any(text.startswith(term) for term in config.transition_terms)
        and len(text) <= 40
    )

    if marketing_hit:
        return [RhetoricalRole.MARKETING]
    if casual_hit:
        return [RhetoricalRole.CASUAL_CHAT]
    if is_title:
        return (
            [RhetoricalRole.TITLE, RhetoricalRole.QUESTION]
            if is_question
            else [RhetoricalRole.TITLE]
        )
    if transition_hit:
        return [RhetoricalRole.TRANSITION]
    if is_question:
        return (
            [RhetoricalRole.QUESTION, RhetoricalRole.CLAIM]
            if is_rhetorical_question
            else [RhetoricalRole.QUESTION]
        )
    if conclusion_hit:
        if operational_hit:
            return [RhetoricalRole.OPERATIONAL_RULE, RhetoricalRole.CONCLUSION]
        return (
            [RhetoricalRole.CONCLUSION, RhetoricalRole.RISK]
            if risk_hit
            else [RhetoricalRole.CONCLUSION]
        )
    if claim_hit:
        return [RhetoricalRole.CLAIM]
    if answer_hit:
        return (
            [RhetoricalRole.CAUSAL_REASON, RhetoricalRole.EXPLANATION]
            if operational_hit or explanation_hit
            else [RhetoricalRole.CAUSAL_REASON]
        )
    if example_hit:
        return (
            [RhetoricalRole.EXAMPLE, RhetoricalRole.RISK]
            if story_example_hit and risk_hit
            else [RhetoricalRole.EXAMPLE]
        )
    if operational_hit:
        return [RhetoricalRole.OPERATIONAL_RULE]
    if counter_hit:
        return [RhetoricalRole.COUNTERARGUMENT]
    if evidence_hit:
        return (
            [RhetoricalRole.EVIDENCE, RhetoricalRole.RISK]
            if risk_hit
            else [RhetoricalRole.EVIDENCE]
        )
    if risk_hit:
        return [RhetoricalRole.RISK]
    if _MARKET_OBSERVATION.search(text):
        return [RhetoricalRole.MARKET_OBSERVATION]
    return [RhetoricalRole.BACKGROUND]


def _promotion_intent(text: str, marketing_terms: list[str]) -> bool:
    for clause in _CLAUSE_BOUNDARY.split(text):
        if not clause.strip():
            continue
        folded_clause = clause.casefold()
        for configured_term in marketing_terms:
            term = configured_term.strip().casefold()
            if not term or _PROMOTION_TERM_ACTION.search(term) is None:
                continue
            start = 0
            while (index := folded_clause.find(term, start)) >= 0:
                end = index + len(term)
                before = clause[:index]
                after = clause[end:]
                prefixed = bool(
                    _EMBEDDED_CTA_PREFIX.search(term)
                    or _CTA_PREFIX_CONTEXT.search(before[-20:])
                )
                continued = bool(_PROMOTION_CONTINUATION.search(after))
                strong_term = bool(_STRONG_PROMOTION_TERM.search(term))
                business_usage = bool(
                    _BUSINESS_PROMOTION_PREFIX.search(before[-24:])
                    or _BUSINESS_PROMOTION_SUFFIX.search(after)
                )
                if (
                    (prefixed or continued or strong_term)
                    and (prefixed or not business_usage)
                ):
                    return True
                start = index + 1
    return False


def _dependency_contract(
    text: str,
    roles: list[RhetoricalRole],
) -> tuple[bool, bool, ParagraphMergeAction, bool, list[str]]:
    role_set = set(roles)
    previous = bool(_PRONOUN_START.search(text))
    following = False
    reasons: list[str] = []
    if RhetoricalRole.QUESTION in role_set:
        following = True
        reasons.append("QUESTION_REQUIRES_ANSWER_CONTEXT")
    if RhetoricalRole.TITLE in role_set or RhetoricalRole.BACKGROUND in role_set:
        following = True
        reasons.append("CONTEXT_REQUIRES_FOLLOWING_CLAIM")
    if RhetoricalRole.TRANSITION in role_set:
        previous = True
        following = True
        reasons.append("TRANSITION_REQUIRES_BOTH_SIDES")
    self_contained_claim = bool(
        role_set & {RhetoricalRole.CLAIM, RhetoricalRole.OPERATIONAL_RULE}
    )
    if (
        role_set & {RhetoricalRole.EVIDENCE, RhetoricalRole.CONCLUSION}
        and not self_contained_claim
    ):
        previous = True
        reasons.append("SUPPORT_OR_CONCLUSION_REQUIRES_PRIOR_CLAIM")
    if RhetoricalRole.EXAMPLE in role_set and not self_contained_claim:
        if _STORY_EXAMPLE.search(text) and not _PRONOUN_START.search(text):
            following = True
            reasons.append("OPENING_STORY_REQUIRES_FOLLOWING_INTERPRETATION")
        else:
            previous = True
            reasons.append("EXAMPLE_REQUIRES_ARGUMENT_CONTEXT")
    if RhetoricalRole.CAUSAL_REASON in role_set and text.startswith(
        ("因为", "原因是", "本质上", "首先", "其次", "一方面", "另一方面")
    ):
        previous = True
        reasons.append("ANSWER_CONNECTOR_REQUIRES_PRIOR_CONTEXT")
    if _PRONOUN_START.search(text):
        reasons.append("ANAPHORA_REQUIRES_PREVIOUS_CONTEXT")
    method_roles = {
        RhetoricalRole.CLAIM,
        RhetoricalRole.OPERATIONAL_RULE,
        RhetoricalRole.EXPLANATION,
        RhetoricalRole.CAUSAL_REASON,
        RhetoricalRole.EVIDENCE,
        RhetoricalRole.EXAMPLE,
        RhetoricalRole.COUNTERARGUMENT,
        RhetoricalRole.CONCLUSION,
        RhetoricalRole.RISK,
    }
    excluded = bool(
        role_set & {RhetoricalRole.MARKETING, RhetoricalRole.CASUAL_CHAT}
    ) and not bool(role_set & method_roles)
    complete_claim = bool(
        role_set & {RhetoricalRole.OPERATIONAL_RULE, RhetoricalRole.CLAIM}
    ) and (
        bool(
            role_set
            & {
                RhetoricalRole.EXPLANATION,
                RhetoricalRole.CAUSAL_REASON,
                RhetoricalRole.CONCLUSION,
            }
        )
        or len(text) >= 120
    )
    standalone = complete_claim and not previous and not following and not excluded
    if excluded:
        action = ParagraphMergeAction.DERIVED_EXCLUDE
        reasons.append("NON_METHOD_DERIVED_EXCLUSION")
    elif standalone:
        action = ParagraphMergeAction.KEEP_AS_ARGUMENT
        reasons.append("SELF_CONTAINED_METHOD_STATEMENT")
    elif previous and following:
        action = ParagraphMergeAction.MERGE_WITH_BOTH
    elif previous:
        action = ParagraphMergeAction.MERGE_WITH_PREVIOUS
    elif following:
        action = ParagraphMergeAction.MERGE_WITH_FOLLOWING
    else:
        action = ParagraphMergeAction.NEEDS_REVIEW
        reasons.append("STANDALONE_COMPLETENESS_UNCERTAIN")
    return previous, following, action, standalone, sorted(set(reasons))


def _relations_for_group(
    item: SemanticContentItem,
    group: list[ParagraphUnit],
    config: SemanticFunnelConfig,
) -> list[ArgumentRelation]:
    relations: list[ArgumentRelation] = []
    for left, right in zip(group, group[1:], strict=False):
        relations.append(
            _relation(
                item,
                left,
                right,
                ArgumentRelationType.CONTINUATION,
                config,
                0.7,
            )
        )
    questions = [p for p in group if RhetoricalRole.QUESTION in p.rhetorical_roles]
    answers = [
        p
        for p in group
        if set(p.rhetorical_roles)
        & {
            RhetoricalRole.CLAIM,
            RhetoricalRole.EXPLANATION,
            RhetoricalRole.CAUSAL_REASON,
            RhetoricalRole.OPERATIONAL_RULE,
        }
        and RhetoricalRole.QUESTION not in p.rhetorical_roles
        and p.ordinal > min((q.ordinal for q in questions), default=10**9)
    ]
    if questions and answers:
        answer = answers[0]
        relations.extend(
            _relation(item, question, answer, ArgumentRelationType.QUESTION_ANSWER, config, 0.9)
            for question in questions
            if question.ordinal < answer.ordinal
        )
    claims = [
        p
        for p in group
        if RhetoricalRole.CLAIM in p.rhetorical_roles
    ]
    anchor = claims[0] if claims else next(
        (
            p
            for p in group
            if p.primary_role
            not in {
                RhetoricalRole.TITLE,
                RhetoricalRole.QUESTION,
                RhetoricalRole.EXAMPLE,
                RhetoricalRole.CONCLUSION,
                RhetoricalRole.TRANSITION,
            }
        ),
        group[0],
    )
    for paragraph in group:
        if paragraph.paragraph_id == anchor.paragraph_id:
            continue
        role_set = set(paragraph.rhetorical_roles)
        if RhetoricalRole.EVIDENCE in role_set:
            relations.append(
                _relation(
                    item,
                    anchor,
                    paragraph,
                    ArgumentRelationType.CLAIM_EVIDENCE,
                    config,
                    0.85,
                )
            )
        if role_set & {RhetoricalRole.EXPLANATION, RhetoricalRole.CAUSAL_REASON}:
            relations.append(
                _relation(
                    item,
                    anchor,
                    paragraph,
                    ArgumentRelationType.CLAIM_EXPLANATION,
                    config,
                    0.82,
                )
            )
        if RhetoricalRole.EXAMPLE in role_set:
            relations.append(
                _relation(item, paragraph, anchor, ArgumentRelationType.EXAMPLE_OF, config, 0.82)
            )
        if RhetoricalRole.COUNTERARGUMENT in role_set:
            relations.append(
                _relation(item, paragraph, anchor, ArgumentRelationType.COUNTER_TO, config, 0.8)
            )
        if RhetoricalRole.CONCLUSION in role_set:
            relations.append(
                _relation(item, paragraph, anchor, ArgumentRelationType.CONCLUSION_OF, config, 0.86)
            )
    unique: dict[str, ArgumentRelation] = {relation.relation_id: relation for relation in relations}
    return sorted(unique.values(), key=lambda relation: relation.relation_id)


def _relation(
    item: SemanticContentItem,
    source: ParagraphUnit,
    target: ParagraphUnit,
    relation_type: ArgumentRelationType,
    config: SemanticFunnelConfig,
    confidence: float,
) -> ArgumentRelation:
    identity = {
        "run_id": item.run_id,
        "item_id": item.item_id,
        "source": source.paragraph_id,
        "target": target.paragraph_id,
        "relation_type": relation_type.value,
        "rule_version": config.relation_rule_version,
    }
    return ArgumentRelation(
        relation_id=f"argument-relation:{content_hash(identity)}",
        run_id=item.run_id,
        content_id=item.content_id,
        source_paragraph_id=source.paragraph_id,
        target_paragraph_id=target.paragraph_id,
        relation_type=relation_type,
        confidence=confidence,
        reason_codes=[f"RULE_{relation_type.value}"],
        relation_rule_version=config.relation_rule_version,
    )


def _starts_new_argument(
    current: list[ParagraphUnit],
    paragraph: ParagraphUnit,
    texts: dict[str, str],
    config: SemanticFunnelConfig,
) -> bool:
    if (
        paragraph.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
        or current[-1].paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
    ):
        return False
    if paragraph.primary_role is RhetoricalRole.TITLE:
        return True
    current_roles = {role for item in current for role in item.rhetorical_roles}
    paragraph_roles = set(paragraph.rhetorical_roles)
    unanswered_question = RhetoricalRole.QUESTION in current_roles and not bool(
        current_roles
        & {
            RhetoricalRole.CLAIM,
            RhetoricalRole.EXPLANATION,
            RhetoricalRole.CAUSAL_REASON,
            RhetoricalRole.OPERATIONAL_RULE,
        }
    )
    if unanswered_question:
        return False
    if paragraph.depends_on_previous or paragraph.primary_role in {
        RhetoricalRole.EVIDENCE,
        RhetoricalRole.EXAMPLE,
        RhetoricalRole.CONCLUSION,
        RhetoricalRole.COUNTERARGUMENT,
        RhetoricalRole.TRANSITION,
    }:
        return False
    if RhetoricalRole.QUESTION in paragraph_roles and current_roles & {
        RhetoricalRole.CLAIM,
        RhetoricalRole.EXPLANATION,
        RhetoricalRole.CAUSAL_REASON,
        RhetoricalRole.EVIDENCE,
        RhetoricalRole.OPERATIONAL_RULE,
    }:
        return True
    similarity = _character_ngram_jaccard(
        texts[current[-1].paragraph_id],
        texts[paragraph.paragraph_id],
    )
    completed = bool(
        current_roles & {RhetoricalRole.CONCLUSION, RhetoricalRole.OPERATIONAL_RULE}
    )
    new_claim = bool(paragraph_roles & {RhetoricalRole.CLAIM, RhetoricalRole.QUESTION})
    prior_chars = sum(len(texts[item.paragraph_id]) for item in current)
    maximum_chars = config.maximum_argument_unit_chars
    supported = bool(
        current_roles & {RhetoricalRole.CLAIM, RhetoricalRole.OPERATIONAL_RULE}
    ) and bool(
        current_roles
        & {
            RhetoricalRole.EXPLANATION,
            RhetoricalRole.CAUSAL_REASON,
            RhetoricalRole.EVIDENCE,
            RhetoricalRole.EXAMPLE,
        }
    )
    if prior_chars >= maximum_chars and supported and new_claim:
        return True
    prefer_merge = bool(config.argument_builder["prefer_merge_on_uncertainty"])
    return completed and new_claim and (similarity < 0.08 or not prefer_merge)


def _argument_completeness(
    group: list[ParagraphUnit],
    relations: list[ArgumentRelation],
) -> float:
    roles = {role for paragraph in group for role in paragraph.rhetorical_roles}
    score = 0.0
    has_question_answer = any(
        relation.relation_type is ArgumentRelationType.QUESTION_ANSWER
        for relation in relations
    )
    evidence_count = sum(
        RhetoricalRole.EVIDENCE in paragraph.rhetorical_roles
        for paragraph in group
    )
    has_method_statement = bool(
        roles
        & {
            RhetoricalRole.CLAIM,
            RhetoricalRole.OPERATIONAL_RULE,
            RhetoricalRole.CAUSAL_REASON,
            RhetoricalRole.EXPLANATION,
        }
    )
    has_bounded_support_chain = (
        evidence_count >= 2
        or (
            len(group) > 1
            and bool(roles & {RhetoricalRole.EXAMPLE})
        )
    )
    has_assertion = (
        has_method_statement
        or has_question_answer
        or has_bounded_support_chain
    )
    if has_assertion:
        score += 0.55
    if roles & {RhetoricalRole.EXPLANATION, RhetoricalRole.CAUSAL_REASON}:
        score += 0.20
    if roles & {RhetoricalRole.EVIDENCE, RhetoricalRole.EXAMPLE}:
        score += 0.20
    if roles & {
        RhetoricalRole.CONCLUSION,
        RhetoricalRole.RISK,
        RhetoricalRole.COUNTERARGUMENT,
    }:
        score += 0.15
    if _dependencies_closed(group, relations):
        score += 0.15
    if not has_assertion:
        score = min(score, 0.35)
    if roles.issubset(
        {
            RhetoricalRole.TITLE,
            RhetoricalRole.BACKGROUND,
            RhetoricalRole.MARKET_OBSERVATION,
            RhetoricalRole.QUESTION,
            RhetoricalRole.TRANSITION,
        }
    ):
        score = min(score, 0.20)
    return round(min(1.0, score), 6)


def _dependencies_closed(
    group: list[ParagraphUnit],
    relations: list[ArgumentRelation],
) -> bool:
    ordinals = {paragraph.paragraph_id: paragraph.ordinal for paragraph in group}
    for paragraph in group:
        previous_closed = any(
            paragraph.paragraph_id in {
                relation.source_paragraph_id,
                relation.target_paragraph_id,
            }
            and ordinals[
                relation.target_paragraph_id
                if relation.source_paragraph_id == paragraph.paragraph_id
                else relation.source_paragraph_id
            ]
            < paragraph.ordinal
            for relation in relations
        )
        next_closed = any(
            paragraph.paragraph_id in {
                relation.source_paragraph_id,
                relation.target_paragraph_id,
            }
            and ordinals[
                relation.target_paragraph_id
                if relation.source_paragraph_id == paragraph.paragraph_id
                else relation.source_paragraph_id
            ]
            > paragraph.ordinal
            for relation in relations
        )
        forward_support_closed = (
            paragraph.ordinal == group[0].ordinal
            and RhetoricalRole.EVIDENCE in paragraph.rhetorical_roles
            and any(
                other.ordinal > paragraph.ordinal
                and set(other.rhetorical_roles)
                & {
                    RhetoricalRole.CLAIM,
                    RhetoricalRole.OPERATIONAL_RULE,
                    RhetoricalRole.EVIDENCE,
                }
                for other in group
            )
        )
        if (
            paragraph.depends_on_previous
            and not previous_closed
            and not forward_support_closed
        ):
            return False
        if paragraph.depends_on_next and not next_closed:
            return False
    return True


def _argument_topic_relevance(group: list[ParagraphUnit]) -> float:
    scores = [paragraph.topic_relevance for paragraph in group]
    return round(max(scores, default=0.0), 6)


def _boundary_confidence(group: list[ParagraphUnit], dependencies_closed: bool) -> float:
    if not dependencies_closed:
        return 0.4
    if len(group) == 1:
        return 0.75 if group[0].standalone_distillable else 0.5
    return 0.82


def _methods_for_group(
    group: list[ParagraphUnit],
    keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
) -> list[BookMethodCategory]:
    matched = {term for paragraph in group for term in paragraph.matched_keyword_terms}
    return sorted(
        [
            category
            for category, terms in keyword_terms.items()
            if matched.intersection(terms)
        ],
        key=lambda category: category.value,
    )


def _paragraph_completeness(roles: list[RhetoricalRole], standalone: bool) -> float:
    role_set = set(roles)
    if standalone:
        return 0.8
    if role_set & {RhetoricalRole.MARKETING, RhetoricalRole.CASUAL_CHAT}:
        return 0.0
    if RhetoricalRole.QUESTION in role_set:
        return 0.1
    if role_set & {RhetoricalRole.TITLE, RhetoricalRole.BACKGROUND, RhetoricalRole.TRANSITION}:
        return 0.15
    if role_set & {RhetoricalRole.EVIDENCE, RhetoricalRole.EXAMPLE}:
        return 0.3
    if role_set & {RhetoricalRole.CLAIM, RhetoricalRole.OPERATIONAL_RULE}:
        return 0.5
    return 0.2


def _context_value(roles: list[RhetoricalRole]) -> float:
    role_set = set(roles)
    if role_set & {RhetoricalRole.MARKETING, RhetoricalRole.CASUAL_CHAT}:
        return 0.0
    if role_set & {
        RhetoricalRole.QUESTION,
        RhetoricalRole.EVIDENCE,
        RhetoricalRole.EXAMPLE,
        RhetoricalRole.COUNTERARGUMENT,
        RhetoricalRole.CONCLUSION,
    }:
        return 0.9
    if role_set & {RhetoricalRole.BACKGROUND, RhetoricalRole.MARKET_OBSERVATION}:
        return 0.7
    if RhetoricalRole.TRANSITION in role_set:
        return 0.6
    return 0.5


def _keyword_hits(
    text: str,
    keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
) -> dict[BookMethodCategory, tuple[str, ...]]:
    folded = text.casefold()
    return {
        category: tuple(term for term in terms if term.casefold() in folded)
        for category, terms in keyword_terms.items()
    }


def _aggregate_keyword_hits(
    paragraphs: Iterable[ParagraphUnit],
    keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
) -> dict[BookMethodCategory, list[str]]:
    matched = {term for paragraph in paragraphs for term in paragraph.matched_keyword_terms}
    return {
        category: [term for term in terms if term in matched]
        for category, terms in keyword_terms.items()
    }


def _derived_exclusion(paragraph: ParagraphUnit) -> bool:
    return paragraph.merge_action is ParagraphMergeAction.DERIVED_EXCLUDE


def _validate_partition(
    paragraphs: list[ParagraphUnit],
    argument_units: list[ArgumentUnit],
) -> None:
    flattened = [paragraph_id for unit in argument_units for paragraph_id in unit.paragraph_ids]
    expected = [paragraph.paragraph_id for paragraph in paragraphs]
    if flattened != expected:
        raise ValueError("argument units must be an ordered, non-overlapping paragraph partition")


def _character_ngram_jaccard(left: str, right: str, size: int = 2) -> float:
    def grams(value: str) -> set[str]:
        folded = _normalize(value).casefold()
        return {folded[index : index + size] for index in range(max(0, len(folded) - size + 1))}

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _decode_content_html(raw: str) -> str:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, dict) and isinstance(decoded.get("content_html"), str):
        return str(decoded["content_html"])
    return html.escape(" ".join(_string_leaves(decoded)))


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [child for item in value for child in _string_leaves(item)]
    if isinstance(value, dict):
        return [child for key in sorted(value) for child in _string_leaves(value[key])]
    return []


def _visible_blocks(value: str) -> list[_VisibleBlock]:
    parser = _VisibleBlockParser(value)
    parser.feed(value)
    parser.close()
    return parser.finish()


class _VisibleBlockParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0, *(match.end() for match in re.finditer(r"\n", source))]
        self.parts: list[str] = []
        self.start: int | None = None
        self.end: int | None = None
        self.block_tag = "text"
        self.ignored_depth = 0
        self.blocks: list[_VisibleBlock] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded in _IGNORED_TAGS:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if folded in _BLOCK_TAGS or folded == "br":
            self._flush()
            self.block_tag = folded
        if folded == "img":
            alt = next((value for name, value in attrs if name.casefold() == "alt" and value), None)
            start = self._position()
            token = self.get_starttag_text() or "<img>"
            self._flush()
            label = _normalize(f"[图片: {alt}]" if alt else "[图片]")
            self.blocks.append(
                _VisibleBlock(
                    text=label,
                    char_start=start,
                    char_end=start + len(token),
                    dom_path=f"visible-block[{len(self.blocks) + 1}]/img",
                )
            )

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded in _IGNORED_TAGS:
            if self.ignored_depth:
                self.ignored_depth -= 1
            return
        if not self.ignored_depth and folded in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.ignored_depth or not data:
            return
        start = self._position()
        self.parts.append(data)
        self.start = start if self.start is None else min(self.start, start)
        self.end = start + len(data) if self.end is None else max(self.end, start + len(data))

    def handle_entityref(self, name: str) -> None:
        self._entity(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._entity(f"&#{name};")

    def finish(self) -> list[_VisibleBlock]:
        self._flush()
        return self.blocks

    def _entity(self, token: str) -> None:
        if self.ignored_depth:
            return
        start = self._position()
        self.parts.append(html.unescape(token))
        self.start = start if self.start is None else min(self.start, start)
        self.end = start + len(token) if self.end is None else max(self.end, start + len(token))

    def _position(self) -> int:
        line, offset = self.getpos()
        return self.line_offsets[line - 1] + offset

    def _flush(self) -> None:
        if self.start is None or self.end is None:
            return
        text = _normalize("".join(self.parts))
        if text:
            self.blocks.append(
                _VisibleBlock(
                    text=text,
                    char_start=self.start,
                    char_end=self.end,
                    dom_path=f"visible-block[{len(self.blocks) + 1}]/{self.block_tag}",
                )
            )
        self.parts.clear()
        self.start = None
        self.end = None
        self.block_tag = "text"


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value))
    return _WHITESPACE.sub(" ", value).strip()


__all__ = [
    "ParagraphizedContent",
    "build_argument_units",
    "load_semantic_funnel_config",
    "local_context_paragraph_ids",
    "method_keyword_terms",
    "paragraphize_zhihu_content",
]
