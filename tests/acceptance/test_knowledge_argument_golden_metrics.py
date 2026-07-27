from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from astock.core.object_store import ObjectStore
from astock.knowledge.config import load_distillation_rules
from astock.knowledge.semantic_funnel import (
    load_semantic_funnel_config,
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
CONFIG_PATH = PROJECT_ROOT / "configs" / "knowledge_semantic_funnel.yaml"
RULES_PATH = PROJECT_ROOT / "configs" / "knowledge_distillation_rules.yaml"
GOLDEN_SCOPE = "STOCK_MARKET_CROSS_PARAGRAPH"
NEGATIVE_SCOPE = "DERIVED_NEGATIVE"
CONTEXT_ONLY_ROLES = {
    RhetoricalRole.TITLE.value,
    RhetoricalRole.BACKGROUND.value,
    RhetoricalRole.MARKET_OBSERVATION.value,
    RhetoricalRole.QUESTION.value,
    RhetoricalRole.TRANSITION.value,
}
ANSWER_ROLES = {
    RhetoricalRole.CLAIM.value,
    RhetoricalRole.EXPLANATION.value,
    RhetoricalRole.CAUSAL_REASON.value,
    RhetoricalRole.OPERATIONAL_RULE.value,
}

Range = tuple[int, int]
ScopedValue = tuple[str, int, int]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric(
    *,
    numerator: int,
    denominator: int,
    threshold: float,
    value: float | int | None,
    passed: bool,
) -> dict[str, object]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "threshold": threshold,
        "value": value,
        "status": "PASS" if passed else "FAIL",
    }


def _range_list(value: object) -> list[Range]:
    if not isinstance(value, list):
        raise TypeError("ranges must be a list")
    parsed: list[Range] = []
    for pair in value:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(ordinal, int) or isinstance(ordinal, bool) for ordinal in pair)
        ):
            raise TypeError("each range must contain exactly two integer ordinals")
        parsed.append((pair[0], pair[1]))
    return parsed


def _range_set(value: object) -> set[Range]:
    return set(_range_list(value))


def _validate_fixture(
    raw_cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], int]:
    errors: list[str] = []
    case_ids = [str(case.get("id", "")) for case in raw_cases]
    if len(case_ids) != len(set(case_ids)):
        errors.append("duplicate_case_id")

    valid_roles = {role.value for role in RhetoricalRole}
    valid_relations = {relation.value for relation in ArgumentRelationType}
    valuable_count = 0
    required = {
        "id",
        "scope",
        "expected_roles",
        "expected_argument_ranges",
        "expected_question_answer_links",
        "valuable_argument_ranges",
        "valuable_annotation",
        "expected_relations",
    }
    for case in raw_cases:
        case_id = str(case.get("id", "<missing>"))
        if not isinstance(case.get("id"), str) or not case_id:
            errors.append("invalid_case_id")
        missing = sorted(required - set(case))
        if missing:
            errors.append(f"{case_id}:missing_labels:{','.join(missing)}")
            continue
        scope = case["scope"]
        if scope not in {GOLDEN_SCOPE, NEGATIVE_SCOPE}:
            errors.append(f"{case_id}:invalid_scope:{scope}")
        annotation = case["valuable_annotation"]
        if not isinstance(annotation, str) or not annotation.strip():
            errors.append(f"{case_id}:missing_valuable_annotation")
        roles = case["expected_roles"]
        if not isinstance(roles, list) or not roles:
            errors.append(f"{case_id}:invalid_expected_roles")
            continue
        paragraph_count = len(roles)
        for ordinal, paragraph_roles in enumerate(roles, start=1):
            if not isinstance(paragraph_roles, list) or not paragraph_roles:
                errors.append(f"{case_id}:missing_gold_roles:{ordinal}")
                continue
            if any(not isinstance(role, str) for role in paragraph_roles):
                errors.append(f"{case_id}:invalid_gold_role_shape:{ordinal}")
                continue
            role_set = set(paragraph_roles)
            if len(role_set) != len(paragraph_roles):
                errors.append(f"{case_id}:duplicate_gold_role:{ordinal}")
            unknown_roles = sorted(role_set - valid_roles)
            if unknown_roles:
                errors.append(
                    f"{case_id}:invalid_gold_roles:{ordinal}:{','.join(unknown_roles)}"
                )

        relations = case["expected_relations"]
        if not isinstance(relations, list) or any(
            not isinstance(relation, str) for relation in relations
        ):
            errors.append(f"{case_id}:invalid_expected_relations")
            relation_set: set[str] = set()
        else:
            relation_set = set(relations)
            if len(relation_set) != len(relations):
                errors.append(f"{case_id}:duplicate_expected_relation")
            unknown_relations = sorted(relation_set - valid_relations)
            if unknown_relations:
                errors.append(
                    f"{case_id}:invalid_expected_relations:"
                    f"{','.join(unknown_relations)}"
                )
        try:
            expected_screen = KeywordScreenDecision(
                str(case.get("expected_screen", "CANDIDATE"))
            )
        except ValueError:
            errors.append(f"{case_id}:invalid_expected_screen")
            expected_screen = KeywordScreenDecision.NEEDS_REVIEW
        try:
            gold_range_list = _range_list(case["expected_argument_ranges"])
            valuable_range_list = _range_list(case["valuable_argument_ranges"])
            qa_edge_list = _range_list(case["expected_question_answer_links"])
            gold_ranges = set(gold_range_list)
            valuable_ranges = set(valuable_range_list)
            qa_edges = set(qa_edge_list)
        except (IndexError, TypeError, ValueError):
            errors.append(f"{case_id}:invalid_range_or_link_shape")
            continue
        if len(gold_ranges) != len(gold_range_list):
            errors.append(f"{case_id}:duplicate_argument_range")
        if len(valuable_ranges) != len(valuable_range_list):
            errors.append(f"{case_id}:duplicate_valuable_range")
        if len(qa_edges) != len(qa_edge_list):
            errors.append(f"{case_id}:duplicate_qa_link")

        if expected_screen is KeywordScreenDecision.EXCLUDED_DERIVED:
            if scope != NEGATIVE_SCOPE:
                errors.append(f"{case_id}:excluded_case_wrong_scope")
            if gold_ranges:
                errors.append(f"{case_id}:excluded_case_has_argument_ranges")
            if valuable_ranges:
                errors.append(f"{case_id}:excluded_case_has_valuable_ranges")
            if qa_edges:
                errors.append(f"{case_id}:excluded_case_has_qa_links")
            if relation_set:
                errors.append(f"{case_id}:excluded_case_has_relations")
        else:
            if scope != GOLDEN_SCOPE:
                errors.append(f"{case_id}:measurement_case_wrong_scope")
            flattened = [
                ordinal
                for start, end in sorted(gold_ranges)
                for ordinal in range(start, end + 1)
                if start <= end
            ]
            if flattened != list(range(1, paragraph_count + 1)):
                errors.append(f"{case_id}:ranges_not_exact_partition")
            if not valuable_ranges.issubset(gold_ranges):
                errors.append(f"{case_id}:valuable_range_not_gold_range")
            valuable_count += len(valuable_ranges)

        for source, target in qa_edges:
            if (
                source < 1
                or target < 1
                or source >= target
                or target > paragraph_count
            ):
                errors.append(f"{case_id}:invalid_qa_reference:{source}:{target}")
                continue
            if RhetoricalRole.QUESTION.value not in roles[source - 1]:
                errors.append(f"{case_id}:qa_source_not_question:{source}")
            if not set(roles[target - 1]).intersection(ANSWER_ROLES):
                errors.append(f"{case_id}:qa_target_not_answer:{target}")
            if not any(
                start <= source < target <= end for start, end in gold_ranges
            ):
                errors.append(f"{case_id}:qa_edge_crosses_gold_range:{source}:{target}")

    scoped = [case for case in raw_cases if case.get("scope") == GOLDEN_SCOPE]
    if len(scoped) < 20:
        errors.append(f"scope_case_count:{len(scoped)}<20")
    if valuable_count < 20:
        errors.append(f"valuable_range_count:{valuable_count}<20")
    return scoped, sorted(errors), valuable_count


def _build_result(
    case: dict[str, Any],
    *,
    objects: ObjectStore,
    config: Any,
    keyword_terms: Any,
) -> Any:
    html = case.get("html")
    if html is None:
        html = "".join(f"<p>{paragraph}</p>" for paragraph in case["paragraphs"])
    body = objects.put_bytes(str(html).encode("utf-8"))
    metadata = objects.put_json({"case_id": case["id"]})
    record = ZhihuContentRecord(
        version_id=f"version:{case['id']}",
        author_source_id="zhihu:test-author",
        content_id=f"content:{case['id']}",
        content_type=ZhihuContentType.ANSWERS,
        canonical_url=f"https://www.zhihu.com/question/1/answer/{case['id']}",
        title=case.get("title"),
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
        body_object_sha256=body.sha256,
        metadata_sha256=metadata.sha256,
        raw_source_snapshot_id=f"snapshot:{case['id']}",
        content_completeness=ZhihuContentCompleteness.DETAIL_VERIFIED,
    )
    return paragraphize_zhihu_content(
        record,
        run_id="semantic-run:argument-golden-metrics",
        object_store=objects,
        config=config,
        keyword_terms=keyword_terms,
    )


def _build_report(tmp_path: Path) -> dict[str, object]:
    payload = cast(
        dict[str, object],
        yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8")),
    )
    raw_cases = cast(list[dict[str, Any]], payload["cases"])
    cases, contract_errors, valuable_count = _validate_fixture(raw_cases)
    config = load_semantic_funnel_config(CONFIG_PATH)
    keyword_terms = method_keyword_terms(load_distillation_rules(RULES_PATH))
    objects = ObjectStore(tmp_path / "objects")

    gold_qa: set[ScopedValue] = set()
    predicted_qa: set[ScopedValue] = set()
    gold_ranges: set[ScopedValue] = set()
    predicted_ranges: set[ScopedValue] = set()
    gold_boundaries: set[tuple[str, int]] = set()
    predicted_boundaries: set[tuple[str, int]] = set()
    gold_valuable: set[ScopedValue] = set()
    eligible_valuable: set[ScopedValue] = set()
    eligible_count = 0
    leakage_count = 0
    case_differences: list[dict[str, object]] = []

    for case in cases:
        case_id = str(case["id"])
        result = _build_result(
            case,
            objects=objects,
            config=config,
            keyword_terms=keyword_terms,
        )
        ordinals = {
            paragraph.paragraph_id: paragraph.ordinal
            for paragraph in result.paragraphs
        }
        expected_ranges = _range_set(case["expected_argument_ranges"])
        actual_ranges = {
            (argument.start_ordinal, argument.end_ordinal)
            for argument in result.argument_units
        }
        expected_qa = _range_set(case["expected_question_answer_links"])
        actual_qa = {
            (
                ordinals[relation.source_paragraph_id],
                ordinals[relation.target_paragraph_id],
            )
            for relation in result.relations
            if relation.relation_type is ArgumentRelationType.QUESTION_ANSWER
        }
        valuable_ranges = _range_set(case["valuable_argument_ranges"])
        eligible_ranges = {
            (argument.start_ordinal, argument.end_ordinal)
            for argument in result.argument_units
            if argument.status is ArgumentUnitStatus.READY
            and argument.standalone_distillable
        }
        gold_roles = cast(list[list[str]], case["expected_roles"])
        actual_roles = [
            {role.value for role in paragraph.rhetorical_roles}
            for paragraph in result.paragraphs
        ]
        role_missing = {
            ordinal: sorted(set(expected) - actual)
            for ordinal, (expected, actual) in enumerate(
                zip(gold_roles, actual_roles, strict=True),
                start=1,
            )
            if set(expected) - actual
        }
        role_extra = {
            ordinal: sorted(actual - set(expected))
            for ordinal, (expected, actual) in enumerate(
                zip(gold_roles, actual_roles, strict=True),
                start=1,
            )
            if actual - set(expected)
        }
        leakage_ranges = {
            range_
            for range_ in eligible_ranges
            if {
                role
                for ordinal in range(range_[0], range_[1] + 1)
                for role in gold_roles[ordinal - 1]
            }.issubset(CONTEXT_ONLY_ROLES)
        }

        gold_qa.update((case_id, source, target) for source, target in expected_qa)
        predicted_qa.update((case_id, source, target) for source, target in actual_qa)
        gold_ranges.update((case_id, start, end) for start, end in expected_ranges)
        predicted_ranges.update((case_id, start, end) for start, end in actual_ranges)
        gold_valuable.update(
            (case_id, start, end) for start, end in valuable_ranges
        )
        eligible_valuable.update(
            (case_id, start, end)
            for start, end in valuable_ranges & eligible_ranges
        )
        final_ordinal = len(gold_roles)
        gold_boundaries.update(
            (case_id, end) for _, end in expected_ranges if end < final_ordinal
        )
        predicted_boundaries.update(
            (case_id, end) for _, end in actual_ranges if end < final_ordinal
        )
        eligible_count += len(eligible_ranges)
        leakage_count += len(leakage_ranges)
        actual_case_boundaries = {
            end for _, end in actual_ranges if end < final_ordinal
        }
        gold_case_boundaries = {
            end for _, end in expected_ranges if end < final_ordinal
        }
        case_differences.append(
            {
                "case_id": case_id,
                "qa_missing": sorted(expected_qa - actual_qa),
                "qa_extra": sorted(actual_qa - expected_qa),
                "range_missing": sorted(expected_ranges - actual_ranges),
                "range_extra": sorted(actual_ranges - expected_ranges),
                "role_missing_by_ordinal": role_missing,
                "role_extra_by_ordinal": role_extra,
                "valuable_not_ready_standalone": sorted(
                    valuable_ranges - eligible_ranges
                ),
                "false_split_boundaries": sorted(
                    actual_case_boundaries - gold_case_boundaries
                ),
                "false_merge_boundaries": sorted(
                    gold_case_boundaries - actual_case_boundaries
                ),
                "context_only_leakage_ranges": sorted(leakage_ranges),
            }
        )

    qa_union = gold_qa | predicted_qa
    range_union = gold_ranges | predicted_ranges
    false_splits = predicted_boundaries - gold_boundaries
    false_merges = gold_boundaries - predicted_boundaries
    denominators = {
        "qa_exact_edge_jaccard": len(qa_union),
        "au_exact_range_jaccard": len(range_union),
        "valuable_ready_standalone_recall": len(gold_valuable),
        "false_split_count": len(predicted_boundaries),
        "false_merge_count": len(gold_boundaries),
        "context_only_role_leakage_ratio": eligible_count,
    }
    contract_errors.extend(
        f"zero_denominator:{name}"
        for name, denominator in denominators.items()
        if denominator == 0
    )

    qa_value = len(gold_qa & predicted_qa) / len(qa_union) if qa_union else None
    range_value = (
        len(gold_ranges & predicted_ranges) / len(range_union)
        if range_union
        else None
    )
    valuable_value = (
        len(eligible_valuable) / len(gold_valuable)
        if gold_valuable
        else None
    )
    leakage_value = leakage_count / eligible_count if eligible_count else None
    metrics = {
        "qa_exact_edge_jaccard": _metric(
            numerator=len(gold_qa & predicted_qa),
            denominator=len(qa_union),
            threshold=1.0,
            value=qa_value,
            passed=qa_value == 1.0,
        ),
        "au_exact_range_jaccard": _metric(
            numerator=len(gold_ranges & predicted_ranges),
            denominator=len(range_union),
            threshold=1.0,
            value=range_value,
            passed=range_value == 1.0,
        ),
        "valuable_ready_standalone_recall": _metric(
            numerator=len(eligible_valuable),
            denominator=len(gold_valuable),
            threshold=0.9,
            value=valuable_value,
            passed=valuable_value is not None and valuable_value >= 0.9,
        ),
        "false_split_count": _metric(
            numerator=len(false_splits),
            denominator=len(predicted_boundaries),
            threshold=0,
            value=len(false_splits),
            passed=not false_splits,
        ),
        "false_merge_count": _metric(
            numerator=len(false_merges),
            denominator=len(gold_boundaries),
            threshold=0,
            value=len(false_merges),
            passed=not false_merges,
        ),
        "context_only_role_leakage_ratio": _metric(
            numerator=leakage_count,
            denominator=eligible_count,
            threshold=0.0,
            value=leakage_value,
            passed=leakage_value == 0.0,
        ),
    }
    passed = not contract_errors and all(
        metric["status"] == "PASS" for metric in metrics.values()
    )
    return {
        "schema_version": "knowledge-argument-golden-metrics-v1",
        "fixture_sha256": _sha256(GOLDEN_PATH),
        "config_sha256": _sha256(CONFIG_PATH),
        "builder_version": config.argument_builder_version,
        "scope": GOLDEN_SCOPE,
        "case_ids": [str(case["id"]) for case in cases],
        "contract": {
            "raw_case_count": len(raw_cases),
            "case_count": len(cases),
            "negative_case_count": len(raw_cases) - len(cases),
            "valuable_range_count": valuable_count,
            "errors": sorted(contract_errors),
        },
        "metrics": metrics,
        "case_differences": case_differences,
        "status": "PASS" if passed else "FAIL",
    }


def test_knowledge_argument_golden_metrics(tmp_path: Path) -> None:
    report = _build_report(tmp_path)
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    print(rendered)
    assert report["status"] == "PASS", rendered
