"""Manual, deterministic distillation of reviewed AUs into MethodRule drafts."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from astock.core.atomic import atomic_create_bytes
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.knowledge_semantics import ParagraphUnit
from astock.schemas.reviewed_distillation import (
    DistillationAUContext,
    DistillationBatchInput,
    DistillationBatchManifest,
    DistillationBatchManifestValidation,
    DistillationBatchOutput,
    DistilledSourceRef,
    MechanicalDraft,
    MethodRuleDraft,
    RuleDraftOrigin,
    RuleDraftStatus,
)
from astock.schemas.reviewed_knowledge import ReviewedArgumentUnit

_SPACE = re.compile(r"\s+")
_SENTENCE = re.compile(
    r"(?<=[。！？.!?；;])\s+|"
    r"(?:(?<!\\d)\.(?!\\d))|\n+",
    re.ASCII,
)
_PUNCT = re.compile(r"[，。；;！？!?,.（）()【】\\[\\]「」“”『』\"]+")

_CONDITION_MARKERS = (
    "如果",
    "若",
    "倘若",
    "当",
    "只要",
    "一旦",
)
_EVIDENCE_MARKERS = (
    "数据",
    "财报",
    "盈利",
    "收入",
    "现金流",
    "ROE",
    "ROA",
    "估值",
    "市盈",
    "营收",
    "增长",
    "回报",
    "净利",
)
_POSITIVE_MARKERS = (
    "提高",
    "改善",
    "增长",
    "增强",
    "稳定",
    "转正",
    "兑现",
    "确认",
    "扩大",
)
_NEGATIVE_MARKERS = (
    "下降",
    "下滑",
    "恶化",
    "缩水",
    "减弱",
    "风险",
    "波动",
    "失守",
)
_INVALIDATION_MARKERS = (
    "失效",
    "转弱",
    "连续",
    "停止",
    "不再",
    "破位",
    "逆转",
    "反向",
    "否定",
)
_FAILURE_MARKERS = (
    "破产",
    "退市",
    "财务造假",
    "杠杆率",
    "违约",
    "资金链",
    "崩盘",
)
_INDUSTRY_MARKERS = (
    "行业",
    "制造",
    "医疗",
    "金融",
    "消费",
    "地产",
    "新能源",
    "半导体",
    "软件",
    "零售",
    "交通",
    "化工",
    "农业",
)
_HORIZON_MARKERS = (
    "短期",
    "中期",
    "长期",
    "季度",
    "年度",
    "一年",
    "一年以内",
    "3-6个月",
    "12个月",
    "24个月",
)
_GENERIC_MARKERS = ("一般", "通常", "多数", "任何", "普遍", "多数情况下", "通常会")
_COMPANY_SPECIFIC_MARKERS = (
    "该公司",
    "这只公司",
    "该股票",
    "这只股票",
    "某公司",
    "本公司",
    "该企业",
)
_MECH_MARKERS = (
    "如图",
    "例如",
    "先决条件",
    "原文",
)
_MECHANICAL_PREFIX_MAX_LEN = 24
_DOMAIN_HINT_TOKENS = (
    "营收",
    "收入",
    "现金流",
    "净利",
    "利润",
    "ROE",
    "ROA",
    "市盈",
    "估值",
    "毛利",
    "毛利率",
    "同比",
    "环比",
    "杠杆率",
    "行业",
    "制造",
    "金融",
    "软件",
    "零售",
    "新能源",
    "半导体",
    "交通",
    "化工",
    "农业",
    "回报",
    "负债",
    "市值",
    "财报",
    "资产",
    "违约",
    "资金链",
    "市净",
)


@dataclass(frozen=True, slots=True)
class _DraftHints:
    decision_question: str
    conditions: tuple[str, ...]
    evidence: tuple[str, ...]
    positive: tuple[str, ...]
    negative: tuple[str, ...]
    invalidation: tuple[str, ...]
    failure: tuple[str, ...]
    industries: tuple[str, ...]
    horizons: tuple[str, ...]


def distill_au(
    context: DistillationAUContext,
    *,
    batch_id: int = 1,
) -> MethodRuleDraft:
    normalized = _normalize_text(context.text)
    sentences = _drop_mechanical_prefix(_split_sentences(normalized))
    hints = _collect_hints(sentences)
    uncertainty: list[str] = []
    reason_flags = _source_ref_audit(context, context.source_refs)
    uncertainty.extend(reason_flags)

    if context.mechanical_draft is not None:
        if context.mechanical_draft.status is RuleDraftStatus.MECHANICAL_DRAFT:
            uncertainty.append("mechanical_draft_needs_manual_review")

    if len(sentences) < 2:
        uncertainty.append("insufficient_text_length")

    if _looks_case_specific_without_generalization(context.title, context.text, context.topics):
        uncertainty.append("case_specific_text_requires_manual_normalization")

    if not context.topics:
        uncertainty.append("no_topic_context")

    if not hints.conditions:
        uncertainty.append("applicable_conditions_insufficient")
    if not hints.evidence:
        uncertainty.append("required_evidence_insufficient")
    if not (hints.positive or hints.negative):
        uncertainty.append("signal_coverage_insufficient")
    if not (hints.invalidation or hints.failure):
        uncertainty.append("invalidation_or_failure_insufficient")
    if not hints.industries:
        uncertainty.append("industry_scope_insufficient")
    if not hints.horizons:
        uncertainty.append("holding_horizon_insufficient")

    status = (
        RuleDraftStatus.READY_FOR_SHADOW
        if not uncertainty
        else RuleDraftStatus.NEEDS_USER_REVIEW
    )

    source_refs = context.source_refs if context.source_refs else []
    if status is RuleDraftStatus.NEEDS_USER_REVIEW and source_refs:
        source_refs = _dedupe_source_refs(source_refs)

    reasoning_steps = _build_reasoning_steps(
        context=context,
        hints=hints,
        status=status,
    )

    required_evidence = _ensure_minimum(
        hints.evidence,
        fallback_reason="required_evidence_insufficient",
        uncertainty=uncertainty,
    )
    conditions = _ensure_minimum(
        hints.conditions,
        fallback_reason="applicable_conditions_insufficient",
        uncertainty=uncertainty,
    )
    positive = _ensure_minimum(
        hints.positive,
        fallback_reason="positive_signal_insufficient",
        uncertainty=uncertainty,
    )
    negative = _ensure_minimum(
        hints.negative,
        fallback_reason="negative_signal_insufficient",
        uncertainty=uncertainty,
    )
    invalidation = _ensure_minimum(
        hints.invalidation,
        fallback_reason="invalidation_condition_insufficient",
        uncertainty=uncertainty,
    )
    failure = _ensure_minimum(
        hints.failure,
        fallback_reason="failure_mode_insufficient",
        uncertainty=uncertainty,
    )
    industries = _ensure_minimum(
        hints.industries,
        fallback_reason="industry_scope_insufficient",
        uncertainty=uncertainty,
    )
    horizons = _ensure_minimum(
        hints.horizons,
        fallback_reason="holding_horizon_insufficient",
        uncertainty=uncertainty,
    )

    if status is RuleDraftStatus.READY_FOR_SHADOW:
        source_refs = _dedupe_source_refs(source_refs)
        if not source_refs:
            status = RuleDraftStatus.NEEDS_USER_REVIEW
            uncertainty.append("source_refs_missing")
    if status is RuleDraftStatus.READY_FOR_SHADOW:
        validation_reasons = validate_natural_language_output(
            context,
            _proposed_rule(
                context=context,
                status=RuleDraftStatus.READY_FOR_SHADOW,
                batch_id=batch_id,
                source_refs=source_refs,
                uncertainty=uncertainty,
                hints=hints,
                reasoning_steps=reasoning_steps,
                required_evidence=required_evidence,
                conditions=conditions,
                positive=positive,
                negative=negative,
                invalidation=invalidation,
                failure=failure,
                industries=industries,
                horizons=horizons,
            ),
        )
        if validation_reasons:
            status = RuleDraftStatus.MECHANICAL_DRAFT
            uncertainty.extend(validation_reasons)
    if (
        status is RuleDraftStatus.NEEDS_USER_REVIEW
        and "case_specific_text_requires_manual_normalization" not in uncertainty
        and _is_mechanical_draft_candidate(uncertainty)
    ):
        status = RuleDraftStatus.MECHANICAL_DRAFT
    if status is RuleDraftStatus.MECHANICAL_DRAFT:
        if not uncertainty:
            uncertainty.append("mechanical_keyword_only")
        source_refs = _dedupe_source_refs(context.source_refs)

    return MethodRuleDraft(
        decision_question=_build_decision_question(context.title, context.topics),
        applicable_conditions=list(conditions),
        reasoning_steps=reasoning_steps,
        required_evidence=list(required_evidence),
        positive_signals=list(positive),
        negative_signals=list(negative),
        invalidation_conditions=list(invalidation),
        known_failure_modes=list(failure),
        applicable_industries=list(industries),
        holding_horizon=list(horizons),
        source_refs=source_refs,
        status=status,
        origin=_draft_origin_for_status(status),
        argument_unit_id=context.argument_unit_id,
        batch_id=batch_id,
        input_object_hash=_rule_input_object_hash(context),
        uncertainty_reason=uncertainty,
    )


def _proposed_rule(
    *,
    context: DistillationAUContext,
    status: RuleDraftStatus,
    batch_id: int,
    source_refs: list[DistilledSourceRef],
    uncertainty: list[str],
    hints: _DraftHints,
    reasoning_steps: list[str],
    required_evidence: tuple[str, ...],
    conditions: tuple[str, ...],
    positive: tuple[str, ...],
    negative: tuple[str, ...],
    invalidation: tuple[str, ...],
    failure: tuple[str, ...],
    industries: tuple[str, ...],
    horizons: tuple[str, ...],
) -> MethodRuleDraft:
    return MethodRuleDraft(
        decision_question=_build_decision_question(context.title, context.topics),
        applicable_conditions=list(conditions),
        reasoning_steps=reasoning_steps,
        required_evidence=list(required_evidence),
        positive_signals=list(positive),
        negative_signals=list(negative),
        invalidation_conditions=list(invalidation),
        known_failure_modes=list(failure),
        applicable_industries=list(industries),
        holding_horizon=list(horizons),
        source_refs=source_refs,
        status=status,
        origin=_draft_origin_for_status(status),
        argument_unit_id=context.argument_unit_id,
        batch_id=batch_id,
        input_object_hash=_rule_input_object_hash(context),
        uncertainty_reason=uncertainty,
    )


def distill_batch(batch: DistillationBatchInput) -> DistillationBatchOutput:
    rules = [distill_au(context, batch_id=batch.batch_id) for context in batch.arguments]
    return DistillationBatchOutput(run_id=batch.run_id, batch_id=batch.batch_id, rules=rules)


def build_distillation_batch_input(
    *,
    state: StateStore,
    object_store: ObjectStore,
    reviewed_run_id: str,
    batch_id: int,
    au_ids: Sequence[str],
) -> DistillationBatchInput:
    requested_ids = list(au_ids)
    if not requested_ids or any(not item for item in requested_ids):
        raise ValueError("distillation batch AU ids must be non-empty")
    if len(requested_ids) != len(set(requested_ids)):
        raise ValueError("distillation batch AU ids must be unique")
    if batch_id < 1:
        raise ValueError("distillation batch id must be positive")

    placeholders = ",".join("?" for _ in requested_ids)
    with closing(_read_only_connection(state)) as connection:
        connection.execute("BEGIN")
        try:
            run_row = connection.execute(
                "SELECT source_run_id,started_at "
                "FROM knowledge_reviewed_semantic_run WHERE run_id=?",
                (reviewed_run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError(f"reviewed run not found: {reviewed_run_id}")

            argument_rows = connection.execute(
                "SELECT argument_unit_id,run_id,title,text_object_hash,status,"
                "standalone_distillable,method_categories_json,rhetorical_roles_json,"
                "lineage_json,unit_object_hash,unit_json "
                "FROM knowledge_reviewed_argument_unit "
                f"WHERE run_id=? AND argument_unit_id IN ({placeholders})",
                (reviewed_run_id, *requested_ids),
            ).fetchall()
            argument_by_id = _validated_reviewed_arguments(
                argument_rows,
                reviewed_run_id=reviewed_run_id,
            )
            missing_ids = [item for item in requested_ids if item not in argument_by_id]
            if missing_ids:
                raise ValueError(
                    "reviewed batch AUs missing: " + ",".join(missing_ids)
                )

            paragraph_rows = connection.execute(
                "SELECT argument_unit_id,ref_ordinal,source_paragraph_id,item_id,"
                "content_id,page_number,paragraph_ordinal,paragraph_head,"
                "text_object_hash,rhetorical_role,source_snapshot_id,locator_json,"
                "visual_evidence_ids_json,visual_chart_unit_ids_json "
                "FROM knowledge_reviewed_argument_paragraph_ref "
                f"WHERE argument_unit_id IN ({placeholders}) "
                "ORDER BY argument_unit_id,ref_ordinal",
                tuple(requested_ids),
            ).fetchall()
            _validate_reviewed_paragraph_rows(
                argument_by_id=argument_by_id,
                paragraph_rows=paragraph_rows,
            )

            paragraph_ids = [
                ref.source_paragraph_id
                for argument in argument_by_id.values()
                for ref in argument.paragraph_refs
            ]
            paragraph_placeholders = ",".join("?" for _ in paragraph_ids)
            source_rows = connection.execute(
                "SELECT paragraph_id,run_id,item_id,author_source_id,content_id,"
                "ordinal,text_object_hash,primary_role,standalone_distillable,"
                "merge_action,unit_json FROM knowledge_paragraph_unit "
                f"WHERE paragraph_id IN ({paragraph_placeholders})",
                tuple(paragraph_ids),
            ).fetchall()
            _validate_source_paragraph_rows(
                source_run_id=str(run_row["source_run_id"]),
                argument_by_id=argument_by_id,
                source_rows=source_rows,
            )
        finally:
            connection.rollback()

    frozen_at = datetime.fromisoformat(str(run_row["started_at"]))
    contexts: list[DistillationAUContext] = []
    for argument_unit_id in requested_ids:
        argument = argument_by_id[argument_unit_id]
        text = _verified_utf8_object(
            object_store,
            argument.text_object_sha256,
            label=f"reviewed AU {argument_unit_id}",
        )
        for ref in argument.paragraph_refs:
            _verified_utf8_object(
                object_store,
                ref.text_object_sha256,
                label=f"source paragraph {ref.source_paragraph_id}",
            )
        contexts.append(
            DistillationAUContext(
                run_id=reviewed_run_id,
                argument_unit_id=argument.argument_unit_id,
                title=argument.title,
                text=text,
                topics=[item.value for item in argument.method_categories],
                source_refs=[
                    DistilledSourceRef(
                        argument_unit_id=argument.argument_unit_id,
                        paragraph_ids=[
                            ref.source_paragraph_id
                            for ref in argument.paragraph_refs
                        ],
                        page_numbers=sorted(
                            {ref.page_number for ref in argument.paragraph_refs}
                        ),
                        text_object_sha256=argument.text_object_sha256,
                        created_at=frozen_at,
                    )
                ],
                mechanical_draft=MechanicalDraft(
                    status=RuleDraftStatus.MECHANICAL_DRAFT,
                    created_at=frozen_at,
                ),
                created_at=frozen_at,
            )
        )
    return DistillationBatchInput(
        run_id=reviewed_run_id,
        batch_id=batch_id,
        arguments=contexts,
        created_at=frozen_at,
    )


def _read_only_connection(state: StateStore) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{state.path.as_uri()}?mode=ro",
        timeout=30,
        isolation_level=None,
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def export_batch_context_file(
    *,
    state: StateStore,
    object_store: ObjectStore,
    reviewed_run_id: str,
    batch_id: int,
    au_ids: Sequence[str],
    output_dir: Path = Path("runtime/codex_runs/k5-d4-r-fix/inputs"),
) -> tuple[Path, str]:
    batch = build_distillation_batch_input(
        state=state,
        object_store=object_store,
        reviewed_run_id=reviewed_run_id,
        batch_id=batch_id,
        au_ids=au_ids,
    )
    payload = canonical_json_bytes(batch.model_dump(mode="json"))
    destination = output_dir.resolve() / f"batch-{batch_id:03d}.json"
    atomic_create_bytes(destination, payload)
    try:
        stored = destination.read_bytes()
    except OSError as exc:
        raise ValueError(f"distillation batch context unreadable: {destination}") from exc
    if stored != payload:
        raise ValueError(f"distillation batch context collision: {destination}")
    return destination, sha256_bytes(payload)


def _validated_reviewed_arguments(
    rows: Sequence[sqlite3.Row],
    *,
    reviewed_run_id: str,
) -> dict[str, ReviewedArgumentUnit]:
    arguments: dict[str, ReviewedArgumentUnit] = {}
    for row in rows:
        encoded = str(row["unit_json"])
        if sha256_bytes(encoded.encode("utf-8")) != str(row["unit_object_hash"]):
            raise ValueError("reviewed AU unit JSON hash mismatch")
        argument = ReviewedArgumentUnit.model_validate_json(encoded)
        projection = (
            argument.argument_unit_id,
            argument.run_id,
            argument.title,
            argument.text_object_sha256,
            argument.status.value,
            int(argument.standalone_distillable),
            [item.value for item in argument.method_categories],
            [item.value for item in argument.rhetorical_roles],
            {
                "decision_ids": argument.decision_ids,
                "source_argument_unit_ids": argument.source_argument_unit_ids,
                "source_snapshot_ids": argument.source_snapshot_ids,
            },
        )
        stored_projection = (
            str(row["argument_unit_id"]),
            str(row["run_id"]),
            str(row["title"]),
            str(row["text_object_hash"]),
            str(row["status"]),
            int(row["standalone_distillable"]),
            json.loads(str(row["method_categories_json"])),
            json.loads(str(row["rhetorical_roles_json"])),
            json.loads(str(row["lineage_json"])),
        )
        if projection != stored_projection or argument.run_id != reviewed_run_id:
            raise ValueError(
                f"reviewed AU projection mismatch: {argument.argument_unit_id}"
            )
        if argument.argument_unit_id in arguments:
            raise ValueError(
                f"duplicate reviewed AU registration: {argument.argument_unit_id}"
            )
        arguments[argument.argument_unit_id] = argument
    return arguments


def _validate_reviewed_paragraph_rows(
    *,
    argument_by_id: dict[str, ReviewedArgumentUnit],
    paragraph_rows: Sequence[sqlite3.Row],
) -> None:
    rows_by_argument: dict[str, list[sqlite3.Row]] = {
        argument_id: [] for argument_id in argument_by_id
    }
    for row in paragraph_rows:
        argument_id = str(row["argument_unit_id"])
        if argument_id not in rows_by_argument:
            raise ValueError(f"unexpected reviewed paragraph AU: {argument_id}")
        rows_by_argument[argument_id].append(row)
    for argument_id, argument in argument_by_id.items():
        rows = rows_by_argument[argument_id]
        if len(rows) != len(argument.paragraph_refs):
            raise ValueError(f"reviewed AU paragraph coverage mismatch: {argument_id}")
        for row, ref in zip(rows, argument.paragraph_refs, strict=True):
            projection = (
                argument_id,
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
                ref.locator.model_dump(mode="json"),
                ref.visual_evidence_ids,
                ref.visual_chart_unit_ids,
            )
            stored_projection = (
                str(row["argument_unit_id"]),
                int(row["ref_ordinal"]),
                str(row["source_paragraph_id"]),
                str(row["item_id"]),
                str(row["content_id"]),
                int(row["page_number"]),
                int(row["paragraph_ordinal"]),
                str(row["paragraph_head"]),
                str(row["text_object_hash"]),
                str(row["rhetorical_role"]),
                str(row["source_snapshot_id"]),
                json.loads(str(row["locator_json"])),
                json.loads(str(row["visual_evidence_ids_json"])),
                json.loads(str(row["visual_chart_unit_ids_json"])),
            )
            if projection != stored_projection:
                raise ValueError(
                    f"reviewed AU paragraph projection mismatch: "
                    f"{argument_id}:{ref.ref_ordinal}"
                )


def _validate_source_paragraph_rows(
    *,
    source_run_id: str,
    argument_by_id: dict[str, ReviewedArgumentUnit],
    source_rows: Sequence[sqlite3.Row],
) -> None:
    source_by_id = {str(row["paragraph_id"]): row for row in source_rows}
    expected_ids = {
        ref.source_paragraph_id
        for argument in argument_by_id.values()
        for ref in argument.paragraph_refs
    }
    missing_ids = sorted(expected_ids - set(source_by_id))
    if missing_ids:
        raise ValueError(
            "reviewed source paragraphs missing: " + ",".join(missing_ids)
        )
    for argument in argument_by_id.values():
        for ref in argument.paragraph_refs:
            row = source_by_id[ref.source_paragraph_id]
            paragraph = ParagraphUnit.model_validate_json(str(row["unit_json"]))
            projection = (
                paragraph.paragraph_id,
                paragraph.run_id,
                ref.item_id,
                paragraph.author_source_id,
                paragraph.content_id,
                paragraph.ordinal,
                paragraph.text_object_sha256,
                paragraph.primary_role.value,
                int(paragraph.standalone_distillable),
                paragraph.merge_action.value,
            )
            stored_projection = (
                str(row["paragraph_id"]),
                str(row["run_id"]),
                str(row["item_id"]),
                str(row["author_source_id"]),
                str(row["content_id"]),
                int(row["ordinal"]),
                str(row["text_object_hash"]),
                str(row["primary_role"]),
                int(row["standalone_distillable"]),
                str(row["merge_action"]),
            )
            if (
                projection != stored_projection
                or paragraph.run_id != source_run_id
                or paragraph.paragraph_id != ref.source_paragraph_id
                or paragraph.content_id != ref.content_id
                or paragraph.ordinal != ref.paragraph_ordinal
                or paragraph.locator != ref.locator
                or paragraph.text_object_sha256 != ref.text_object_sha256
                or paragraph.rhetorical_roles != ref.rhetorical_roles
            ):
                raise ValueError(
                    f"source paragraph projection mismatch: "
                    f"{ref.source_paragraph_id}"
                )


def _verified_utf8_object(
    object_store: ObjectStore,
    object_hash: str,
    *,
    label: str,
) -> str:
    payload = object_store.get_bytes(object_hash)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} object is not UTF-8: {object_hash}") from exc


def validate_batch_manifest(
    manifest: DistillationBatchManifest,
    *,
    expected_au_ids: Iterable[str] | None = None,
) -> DistillationBatchManifestValidation:
    ids = [item for batch in manifest.batches for item in batch.au_ids]
    duplicate_au_ids = sorted(
        item for item, count in Counter(ids).items() if count > 1
    )
    expected = sorted(set(expected_au_ids)) if expected_au_ids is not None else []
    missing_au_ids = sorted(set(expected) - set(ids)) if expected_au_ids is not None else []
    processed_count = len(set(ids))
    return DistillationBatchManifestValidation(
        duplicate_au_ids=duplicate_au_ids,
        missing_au_ids=missing_au_ids,
        is_complete=(not duplicate_au_ids and not missing_au_ids),
        processed_count=processed_count,
        expected_count=len(expected),
    )


def to_rule_contract(rule: MethodRuleDraft) -> dict[str, object]:
    return rule.model_dump(mode="json")


def contract_json(rules: Iterable[MethodRuleDraft]) -> list[dict[str, object]]:
    return [to_rule_contract(rule) for rule in rules]


def validate_natural_language_output(
    context: DistillationAUContext,
    rule: MethodRuleDraft,
) -> list[str]:
    if rule.status is not RuleDraftStatus.READY_FOR_SHADOW:
        return []

    reasons: list[str] = []
    if rule.origin is not RuleDraftOrigin.CODEX_NATURAL_LANGUAGE:
        reasons.append("rule_origin_not_codex_natural_language")

    if rule.argument_unit_id != context.argument_unit_id:
        reasons.append("argument_unit_id_mismatch")

    if rule.input_object_hash != _rule_input_object_hash(context):
        reasons.append("input_object_hash_mismatch")

    reason_flags = _source_ref_audit(context, rule.source_refs)
    reasons.extend(reason_flags)

    if _copying_introductory_sentences(context.text, rule):
        reasons.append("introductory_sentences_copied")

    if _looks_like_keyword_fill_only(rule, context):
        reasons.append("mechanical_keyword_only")

    return sorted(set(reasons))


def _source_ref_audit(
    context: DistillationAUContext,
    source_refs: Sequence[DistilledSourceRef],
) -> list[str]:
    if not source_refs:
        return ["source_refs_missing"]
    reasons: list[str] = []
    arguments = [ref.argument_unit_id for ref in source_refs if ref.argument_unit_id]
    if len(arguments) != len(set(arguments)):
        reasons.append("source_refs_forged_or_duplicate_argument_unit")
    if context.source_refs:
        context_ref_keys = {
            _source_ref_signature(item)
            for item in context.source_refs
            if item.argument_unit_id == context.argument_unit_id
        }
        for ref in source_refs:
            if ref.argument_unit_id != context.argument_unit_id:
                reasons.append("source_ref_argument_unit_mismatch")
                continue
            signature = _source_ref_signature(ref)
            if context_ref_keys and signature not in context_ref_keys:
                reasons.append("source_ref_signature_mismatch")
    for ref in source_refs:
        if not ref.paragraph_ids or not ref.page_numbers:
            reasons.append("source_refs_incomplete_fields")
    return sorted(set(reasons))


def _rule_input_object_hash(context: DistillationAUContext) -> str:
    return content_hash(
        {
            "run_id": context.run_id,
            "argument_unit_id": context.argument_unit_id,
            "title": context.title,
            "text": _normalize_text(context.text),
        }
    )


def _copying_introductory_sentences(
    context_text: str,
    rule: MethodRuleDraft,
) -> bool:
    context_sentences = _drop_mechanical_prefix(_split_sentences(_normalize_text(context_text)))[:4]
    if len(context_sentences) < 2:
        return False
    canonical_payload = [
        _canonical_au_text(item)
        for item in (
            rule.decision_question,
            *rule.applicable_conditions,
            *rule.required_evidence,
            *rule.reasoning_steps,
            *rule.positive_signals,
            *rule.negative_signals,
            *rule.invalidation_conditions,
            *rule.known_failure_modes,
        )
    ]
    for context_sentence in context_sentences:
        normalized_context = _canonical_au_text(context_sentence)
        for payload_sentence in canonical_payload:
            if _is_similar(normalized_context, payload_sentence):
                return True
    return False


def _looks_like_keyword_fill_only(
    rule: MethodRuleDraft,
    context: DistillationAUContext,
) -> bool:
    _ = _normalize_text(context.text)
    samples = [_canonical_au_text(rule.decision_question)]
    samples.extend(_canonical_au_text(item) for item in rule.applicable_conditions[:3])
    samples.extend(_canonical_au_text(item) for item in rule.required_evidence[:3])
    samples.extend(_canonical_au_text(item) for item in rule.applicable_industries[:2])
    if not samples:
        return True
    signal_count = 0
    mechanical_count = 0
    for value in samples:
        if not value:
            continue
        signal_count += 1
        if len(value) <= 28 and any(
            marker in value for marker in _MECH_MARKERS + _GENERIC_MARKERS
        ):
            mechanical_count += 1
    if signal_count >= 4 and mechanical_count >= signal_count * 0.8:
        return True
    if signal_count and mechanical_count == signal_count:
        return True
    return False


def _is_mechanical_draft_candidate(uncertainty: Sequence[str]) -> bool:
    source_reference_blocked_reasons = {
        "source_ref_argument_unit_mismatch",
        "source_refs_forged_or_duplicate_argument_unit",
        "source_ref_signature_mismatch",
        "source_refs_forged",
    }
    if any(item in source_reference_blocked_reasons for item in uncertainty):
        return False
    mechanical_reasons = {
        "mechanical_draft_needs_manual_review",
        "applicable_conditions_insufficient",
        "required_evidence_insufficient",
        "signal_coverage_insufficient",
        "invalidation_or_failure_insufficient",
        "industry_scope_insufficient",
        "holding_horizon_insufficient",
        "no_topic_context",
        "insufficient_text_length",
    }
    return any(item in mechanical_reasons for item in uncertainty)


def _is_similar(left: str, right: str) -> bool:
    return SequenceMatcher(None, left, right).ratio() >= 0.92


def _canonical_au_text(value: str) -> str:
    normalized = _PUNCT.sub("", value)
    return _SPACE.sub(" ", normalized).strip().lower()


def _source_ref_signature(
    ref: DistilledSourceRef,
) -> tuple[str, tuple[str, ...], tuple[int, ...], str]:
    return (
        ref.argument_unit_id,
        tuple(ref.paragraph_ids),
        tuple(ref.page_numbers),
        ref.text_object_sha256,
    )


def _draft_origin_for_status(status: RuleDraftStatus) -> RuleDraftOrigin | None:
    return (
        RuleDraftOrigin.CODEX_NATURAL_LANGUAGE
        if status is RuleDraftStatus.READY_FOR_SHADOW
        else RuleDraftOrigin.MECHANICAL_DRAFT
    )


def _collect_hints(sentences: tuple[str, ...]) -> _DraftHints:
    conditions = _collect_sentences(
        sentences,
        _CONDITION_MARKERS,
        require_domain_anchor=True,
    )
    evidence = _collect_sentences(
        sentences,
        _EVIDENCE_MARKERS,
        require_domain_anchor=True,
    )
    positive = _collect_sentences(
        sentences,
        _POSITIVE_MARKERS,
        require_domain_anchor=True,
    )
    negative = _collect_sentences(
        sentences,
        _NEGATIVE_MARKERS,
        require_domain_anchor=True,
    )
    invalidation = _collect_sentences(sentences, _INVALIDATION_MARKERS)
    failure = _collect_sentences(sentences, _FAILURE_MARKERS)
    industries = _collect_sentences(sentences, _INDUSTRY_MARKERS)
    horizons = _collect_sentences(sentences, _HORIZON_MARKERS)
    return _DraftHints(
        decision_question="",
        conditions=conditions,
        evidence=evidence,
        positive=positive,
        negative=negative,
        invalidation=invalidation,
        failure=failure,
        industries=industries,
        horizons=horizons,
    )

def _collect_sentences(
    sentences: tuple[str, ...],
    terms: tuple[str, ...],
    *,
    require_domain_anchor: bool = False,
) -> tuple[str, ...]:
    hits: list[str] = []
    for sentence in sentences:
        if any(term in sentence for term in terms) and (
            not require_domain_anchor or _has_domain_anchor(sentence)
        ):
            cleaned = _summarize_sentence(sentence)
            if cleaned:
                hits.append(cleaned)
    return tuple(_dedupe(hits))


def _normalize_text(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _split_sentences(value: str) -> tuple[str, ...]:
    parts = [part.strip() for part in _SENTENCE.split(value)]
    return tuple(part for part in parts if len(part) >= 6 and part not in _MECH_MARKERS)

def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))[:6]


def _dedupe_source_refs(source_refs: Sequence[DistilledSourceRef]) -> list[DistilledSourceRef]:
    normalized: list[DistilledSourceRef] = []
    seen: set[tuple[str, tuple[str, ...], tuple[int, ...]]] = set()
    for ref in source_refs:
        signature = (
            ref.argument_unit_id,
            tuple(ref.paragraph_ids),
            tuple(ref.page_numbers),
        )
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append(ref)
    return sorted(
        normalized,
        key=lambda item: (
            item.argument_unit_id,
            item.paragraph_ids,
            item.page_numbers,
        ),
    )


def _build_decision_question(title: str, topics: list[str]) -> str:
    topic_hint = topics[0] if topics else "方法"
    base = _strip_title_noise(title)
    return f"如何基于\"{base}\"判断{topic_hint}中的可执行投资前提？"


def _strip_title_noise(value: str) -> str:
    return _PUNCT.sub("", value).strip() or "该观点"


def _build_reasoning_steps(
    *,
    context: DistillationAUContext,
    hints: _DraftHints,
    status: RuleDraftStatus,
) -> list[str]:
    conditions = hints.conditions[:2]
    evidence = hints.evidence[:2]
    if status is RuleDraftStatus.READY_FOR_SHADOW:
        first = f"先确认条件：{_format_items(conditions)}。"
        second = f"再核验证据：{_format_items(evidence)}。"
        third = "结合正负信号与失效条件完成投资决策边界。"
        return [first, second, third]
    return [
        f"先从标题与段落抽取逻辑前提：{_strip_title_noise(context.title)}。",
        "当前证据不足，建议先补齐可复验指标后再固化规则。",
    ]


def _format_items(items: Iterable[str]) -> str:
    normalized = list(items) or ["未提取到明确项"]
    return "；".join(normalized[:2])


def _ensure_minimum(
    items: tuple[str, ...],
    *,
    fallback_reason: str,
    uncertainty: list[str],
) -> tuple[str, ...]:
    if items:
        return items[:8]
    if fallback_reason not in uncertainty:
        uncertainty.append(fallback_reason)
    return (fallback_reason.replace("_", " "),)


def _summarize_sentence(sentence: str) -> str:
    normalized = _PUNCT.sub("", sentence).strip()
    if len(normalized) <= 160:
        return normalized
    return normalized[:157] + "…"


def _drop_mechanical_prefix(sentences: tuple[str, ...]) -> tuple[str, ...]:
    offset = 0
    for sentence in sentences[:4]:
        if _is_mechanical_prefix(sentence):
            offset += 1
            continue
        break
    return sentences[offset:]


def _is_mechanical_prefix(sentence: str) -> bool:
    compact = _PUNCT.sub("", sentence).strip()
    if len(compact) > _MECHANICAL_PREFIX_MAX_LEN:
        return False
    return any(compact.startswith(marker) for marker in _MECH_MARKERS)


def _has_domain_anchor(sentence: str) -> bool:
    return any(token in sentence for token in _DOMAIN_HINT_TOKENS)


def _looks_case_specific_without_generalization(
    title: str,
    text: str,
    topics: list[str],
) -> bool:
    lower = (title + " " + text).casefold()
    if any(marker in lower for marker in _GENERIC_MARKERS):
        return False
    if any(marker in lower for marker in _COMPANY_SPECIFIC_MARKERS):
        return True
    return False


def rule_signature(rule: MethodRuleDraft, *, run_id: str, argument_unit_id: str) -> str:
    return content_hash(
        {
            "run_id": run_id,
            "argument_unit_id": argument_unit_id,
            "decision_question": _PUNCT.sub("", rule.decision_question),
            "applicable_conditions": rule.applicable_conditions,
            "required_evidence": rule.required_evidence,
            "positive_signals": rule.positive_signals,
            "negative_signals": rule.negative_signals,
            "invalidation_conditions": rule.invalidation_conditions,
            "known_failure_modes": rule.known_failure_modes,
        }
    )


__all__ = [
    "build_distillation_batch_input",
    "distill_au",
    "distill_batch",
    "export_batch_context_file",
    "validate_batch_manifest",
    "to_rule_contract",
    "contract_json",
    "rule_signature",
]
