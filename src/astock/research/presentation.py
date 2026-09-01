"""Unified public presentation gateway with a strict developer-noise boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import PurePath, PureWindowsPath

from astock.research.internal_vocabulary import internal_vocabulary_terms
from astock.research.presentation_policy import PresentationPolicy, load_presentation_policy
from astock.schemas.presentation import (
    BudgetStatus,
    ConclusionStrength,
    DeveloperDiagnosticsInput,
    DeveloperDiagnosticsModel,
    FactEquivalenceStatus,
    FactFingerprint,
    InvestorPresentationModel,
    PresentationAudit,
    PublicReportReference,
    RenderedResponse,
    ResearchNarrativeBundle,
    ResponseChannel,
    ResponseContext,
    ResponseDetail,
    ResponseMode,
    ResponseTaskType,
)
from astock.schemas.research_acquisition import (
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
    InvestorAnswerAudit,
    InvestorGapCategory,
    InvestorResearchState,
    InvestorResearchView,
)
from astock.schemas.research_runtime import ResearchRunReport, ResearchRunStatus

_GAP_MESSAGES: dict[InvestorGapCategory, str] = {
    InvestorGapCategory.EVIDENCE: (
        "有些会影响投资判断的关键事实还需要从公司公告或正式报告进一步核实。"
    ),
    InvestorGapCategory.FINANCIAL: "最新一期财务数据还需要和公司正式披露逐项核对。",
    InvestorGapCategory.FUNDAMENTAL_MODEL: (
        "目前还缺少足够可靠的依据来把未来盈利和合理估值区间算得足够稳健。"
    ),
    InvestorGapCategory.BASE_CASE: "核心投资逻辑、反方情形和关键假设还需要进一步收敛。",
    InvestorGapCategory.SPECIALIST: "个别会影响结论的专项问题仍需补充证据。",
    InvestorGapCategory.KNOWLEDGE: "现有材料还不足以覆盖一个关键判断维度。",
    InvestorGapCategory.COMMITTEE: "现有证据还不足以支持明确的买入时点结论。",
    InvestorGapCategory.EXECUTION_READINESS: (
        "如果要给出具体买入价、止损或卖出条件，还需要核实最新交易条件。"
    ),
    InvestorGapCategory.GENERAL: "还有一项会影响投资判断的材料尚未完成核实。",
}

_ANSWER_POLICY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "INTERNAL_PROTOCOL_TERM_EXPOSED",
        (
            r"\bmarketpriceanchor\b",
            r"\bclassifiedtradeprotocol\b",
            r"\binstrumentreferencerelease\b",
            r"\bfrozenevidencepack\b",
            r"\bevidencepack\b",
            r"\bbasecase(?:pack)?\b",
            r"\btradingclassification\b",
            r"\binvestment committee\b",
            r"投资委员会",
            r"投委会",
            r"(?<![A-Za-z])PIT(?![A-Za-z])",
        ),
    ),
    (
        "CLI_OR_PIPELINE_EXPOSED",
        (
            r"research-plan",
            r"research-acquire-current",
            r"research-run-company",
            r"uv run astock",
            r"current_stage",
            r"\bworkflow\b",
            r"\bskill\b",
        ),
    ),
    (
        "RAW_MACHINE_STATE_EXPOSED",
        (
            r"needs_info",
            r"claim_ids_required",
            r"evidence_pack_required",
            r"trade_protocol_outcome",
            r"approve_simulation",
            r"paper_ledger",
            r"broker_execution",
        ),
    ),
    (
        "STORAGE_OR_ARTIFACT_TERM_EXPOSED",
        (
            r"artifact_id",
            r"object_hash",
            r"snapshot_id",
            r"\bsqlite\b",
            r"\bmigration\b",
            r"source_snapshot",
            r"manifest_object",
        ),
    ),
    (
        "PROVIDER_DIAGNOSTIC_EXPOSED",
        (
            r"\bbaostock\b",
            r"eastmoney-reference",
            r"eastmoney-financial",
            r"sina-financial",
            r"instrument identity",
        ),
    ),
    (
        "DEVELOPER_META_EXPOSED",
        (
            r"这套系统",
            r"当前系统",
            r"系统的风格",
            r"系统设计",
            r"调试工程师",
            r"内部实现",
            r"后台日志",
            r"正式链",
            r"\bphase\s*\d+\b",
        ),
    ),
)

_SECRET_PATTERNS = (
    r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?\S+",
    r"(?i)bearer\s+[A-Za-z0-9._~+\-/]+=*",
    r"(?i)cookie\s*[:=]\s*\S+",
    r"(?i)(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*\S+",
)
_PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:\\Users\\[^\\\s]+|/home/[^/\s]+|/Users/[^/\s]+)"
)
_URL_PATTERN = re.compile(r"https?://[^\s)）]+", flags=re.IGNORECASE)
_DIRECTION_TERMS = (
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "持有",
    "观望",
    "回避",
    "看多",
    "看空",
    "等待",
    "控制仓位",
)
_STRENGTH_LABELS: dict[ConclusionStrength, str] = {
    ConclusionStrength.UNSPECIFIED: "未标注",
    ConclusionStrength.NOT_CERTIFIED: "未认证",
    ConclusionStrength.LOW: "低",
    ConclusionStrength.MODERATE: "中",
    ConclusionStrength.HIGH: "高",
}
_LEADING_FILLERS = (
    "综上所述",
    "值得注意的是",
    "需要强调的是",
    "从某种意义上说",
    "不难发现",
    "可以看出",
)
_INTERNAL_FINDING_CODES = {
    "INTERNAL_PROTOCOL_TERM_EXPOSED",
    "CLI_OR_PIPELINE_EXPOSED",
    "RAW_MACHINE_STATE_EXPOSED",
    "STORAGE_OR_ARTIFACT_TERM_EXPOSED",
    "PROVIDER_DIAGNOSTIC_EXPOSED",
    "DYNAMIC_INTERNAL_VOCABULARY_EXPOSED",
    "DEVELOPER_META_EXPOSED",
}
_FINGERPRINT_FIELDS = (
    "entities",
    "security_codes",
    "numbers",
    "dates",
    "times",
    "direction_terms",
    "conclusion_strength_terms",
    "citations",
)
_NEGATION_PREFIX_PATTERN = re.compile(
    r"(?:不|不要|不用|无需|无须|别|不必|不需要|不是让你|"
    r"无需进入|别进入|不要进入)\s*$"
)
_DIAGNOSTIC_ACTION_PREFIX_PATTERN = re.compile(
    r"(?:请|麻烦|帮我|我要|我想|需要|请你)?"
    r"(?:查看|看看|看|检查|排查|调试|核对|输出|显示|读取|定位|诊断)\s*$"
)
_SELF_ACTION_DIAGNOSTIC_TERMS = (
    "调试",
    "排查系统",
    "系统排查",
    "看日志",
    "查看日志",
)


class ResponseGateway:
    """Single allowlisted user-facing exit over existing frozen research facts."""

    def __init__(self, policy: PresentationPolicy | None = None) -> None:
        self.policy = policy or load_presentation_policy()

    def context(
        self,
        request_text: str,
        *,
        task_type: ResponseTaskType = ResponseTaskType.COMPANY_QUICK_VIEW,
        channel: ResponseChannel = ResponseChannel.CHAT,
        requested_detail: ResponseDetail = ResponseDetail.STANDARD,
        explicit_mode: ResponseMode | None = None,
        system_error_present: bool = False,
    ) -> ResponseContext:
        mode = classify_response_mode(
            request_text,
            explicit_mode=explicit_mode,
            policy=self.policy,
        )
        diagnostic = mode is ResponseMode.DEVELOPER
        resolved_task_type = (
            ResponseTaskType.DEVELOPER_DIAGNOSTIC if diagnostic else task_type
        )
        if mode is ResponseMode.REPORT:
            resolved_task_type = ResponseTaskType.FORMAL_REPORT
        return ResponseContext(
            mode=mode,
            channel=channel,
            task_type=resolved_task_type,
            requested_detail=requested_detail,
            request_text=request_text,
            locale=self.policy.locale,
            diagnostic_intent_detected=diagnostic,
            system_error_present=system_error_present,
            broker_execution_allowed=False,
        )

    def render(
        self,
        context: ResponseContext,
        *,
        narrative: ResearchNarrativeBundle | None = None,
        diagnostics: DeveloperDiagnosticsInput | None = None,
    ) -> RenderedResponse:
        if context.mode is ResponseMode.DEVELOPER:
            return self._render_developer(context, diagnostics)
        if narrative is None:
            raise ValueError("Investor/Report presentation requires a narrative bundle")

        payload = _investor_payload(narrative, context=context, policy=self.policy)
        text = normalize_public_text(_render_investor_text(payload), policy=self.policy)
        required = _required_fingerprint_for_payload(payload, narrative.locked_facts)
        audit = audit_public_answer(
            text,
            context=context,
            policy=self.policy,
            source_text=_narrative_source_text(narrative),
            required_fingerprint=required,
        )
        if audit.safe_to_send:
            return RenderedResponse(
                mode=context.mode,
                task_type=context.task_type,
                text=text,
                payload=payload,
                audit=audit,
                safe_fallback_used=False,
                raw_draft_exposed=False,
            )

        safe_payload = InvestorPresentationModel(
            subject=_safe_fallback_subject(
                narrative.subject,
                context=context,
                policy=self.policy,
            ),
            headline=self.policy.safe_fallback_text,
            conclusion_strength=ConclusionStrength.NOT_CERTIFIED,
        )
        safe_text = _render_investor_text(safe_payload)
        safe_audit = audit_public_answer(
            safe_text,
            context=context,
            policy=self.policy,
        ).model_copy(update={"budget_status": BudgetStatus.SAFE_FALLBACK})
        return RenderedResponse(
            mode=context.mode,
            task_type=context.task_type,
            text=safe_text,
            payload=safe_payload,
            audit=safe_audit,
            safe_fallback_used=True,
            raw_draft_exposed=False,
        )

    def _render_developer(
        self,
        context: ResponseContext,
        diagnostics: DeveloperDiagnosticsInput | None,
    ) -> RenderedResponse:
        if diagnostics is None:
            raise ValueError("Developer Mode requires allowlisted diagnostics input")
        payload = _developer_payload(diagnostics)
        text = _render_developer_text(payload)
        audit = audit_developer_answer(text, context=context, policy=self.policy)
        fallback_used = False
        if not audit.safe_to_send:
            payload = DeveloperDiagnosticsModel(
                user_impact="当前诊断内容未通过公开安全检查。",
                failure_class="DIAGNOSTIC_REDACTED",
                correlation_id=redact_sensitive_text(diagnostics.correlation_id),
                next_action="请在本机受控日志中按关联号继续排查。",
            )
            text = _render_developer_text(payload)
            audit = audit_developer_answer(
                text,
                context=context,
                policy=self.policy,
            ).model_copy(update={"budget_status": BudgetStatus.SAFE_FALLBACK})
            fallback_used = True
        return RenderedResponse(
            mode=context.mode,
            task_type=context.task_type,
            text=text,
            payload=payload,
            audit=audit,
            safe_fallback_used=fallback_used,
            raw_draft_exposed=False,
        )


def classify_response_mode(
    request_text: str,
    *,
    explicit_mode: ResponseMode | None = None,
    policy: PresentationPolicy | None = None,
) -> ResponseMode:
    """Default to Investor Mode; only affirmative user intent enables diagnostics."""

    if explicit_mode is not None:
        return explicit_mode
    selected = policy or load_presentation_policy()
    normalized = re.sub(r"\s+", "", request_text.casefold())
    for negated_phrase in selected.diagnostic_negation_terms:
        normalized = normalized.replace(
            re.sub(r"\s+", "", negated_phrase.casefold()),
            "",
        )
    for term in selected.diagnostic_intent_terms:
        candidate = re.sub(r"\s+", "", term.casefold())
        start = normalized.find(candidate)
        while start >= 0:
            prefix = normalized[max(0, start - 12) : start]
            negated = _NEGATION_PREFIX_PATTERN.search(prefix) is not None
            self_action = candidate in _SELF_ACTION_DIAGNOSTIC_TERMS
            action_requested = (
                _DIAGNOSTIC_ACTION_PREFIX_PATTERN.search(prefix) is not None
            )
            if not negated and (self_action or action_requested):
                return ResponseMode.DEVELOPER
            start = normalized.find(candidate, start + len(candidate))
    return selected.default_mode


def narrative_from_investor_view(view: InvestorResearchView) -> ResearchNarrativeBundle:
    strength = {
        InvestorResearchState.DECISION_READY: ConclusionStrength.HIGH,
        InvestorResearchState.EVIDENCE_READY: ConclusionStrength.MODERATE,
        InvestorResearchState.EVIDENCE_STILL_COLLECTING: ConclusionStrength.NOT_CERTIFIED,
        InvestorResearchState.DECISION_NOT_CERTIFIED: ConclusionStrength.NOT_CERTIFIED,
    }[view.state]
    return ResearchNarrativeBundle(
        subject=view.company_id,
        task_type=ResponseTaskType.COMPANY_QUICK_VIEW,
        headline=view.headline,
        conclusion_strength=strength,
        reasons=list(view.plain_language_gaps),
        change_conditions=[view.next_step],
        data_as_of=view.created_at,
    )


def investor_view_from_run(
    report: ResearchRunReport,
    *,
    include_execution_readiness: bool = False,
) -> InvestorResearchView:
    if report.status is ResearchRunStatus.COMPLETE:
        return InvestorResearchView(
            company_id=report.company_id,
            state=InvestorResearchState.DECISION_READY,
            headline="现有材料已经比较完整，可以直接讨论公司质量、估值、赔率与主要风险。",
            plain_language_gaps=[],
            next_step="直接给出投资逻辑、合理估值区间、关键风险和需要继续观察的条件。",
            created_at=report.created_at,
        )

    messages: list[str] = []
    categories = report.investor_gap_categories or [InvestorGapCategory.GENERAL]
    for category in categories:
        if (
            category is InvestorGapCategory.EXECUTION_READINESS
            and not include_execution_readiness
        ):
            continue
        message = _GAP_MESSAGES[category]
        if message not in messages:
            messages.append(message)
    if not messages:
        messages.append("还有一项会影响买入时点判断的关键信息需要进一步核实。")
    return InvestorResearchView(
        company_id=report.company_id,
        state=InvestorResearchState.DECISION_NOT_CERTIFIED,
        headline=(
            "目前还缺少少量会影响买入时点判断的关键信息，"
            "我会先继续补齐可自动获取的公开资料。"
        ),
        plain_language_gaps=messages,
        next_step=(
            "先继续核对自动数据源和权威网页资料；只有自动渠道都无法完成时，"
            "再一次性列出需要你协助提供的材料。"
        ),
        created_at=report.created_at,
    )


def investor_view_from_acquisition(
    report: CurrentResearchAcquisitionReport,
) -> InvestorResearchView:
    if report.status in {
        CurrentResearchAcquisitionStatus.READY,
        CurrentResearchAcquisitionStatus.DEGRADED,
    }:
        gaps = []
        if report.status is CurrentResearchAcquisitionStatus.DEGRADED:
            gaps.append("核心资料已经够用，但还有少量辅助信息值得继续核实。")
        return InvestorResearchView(
            company_id=report.company_id,
            state=InvestorResearchState.EVIDENCE_READY,
            headline="当前分析所需的核心公开资料已经拿到，可以继续判断盈利趋势和估值是否有吸引力。",
            plain_language_gaps=gaps,
            next_step="继续分析盈利驱动、估值区间、关键催化、主要风险和可能推翻判断的条件。",
            created_at=report.decision_as_of,
        )
    gaps = [item.research_question for item in report.external_research_needs]
    if report.manual_actions:
        gaps.extend(item.instruction for item in report.manual_actions)
    return InvestorResearchView(
        company_id=report.company_id,
        state=InvestorResearchState.EVIDENCE_STILL_COLLECTING,
        headline="还有几项影响判断的公开资料没有核实完，我会继续从权威来源补齐。",
        plain_language_gaps=gaps or ["仍有公开研究材料需要补齐。"],
        next_step=(
            "优先从交易所、法定披露平台、公司官网和监管机构继续核实；"
            "只有这些自动渠道也无法解决时，再向你一次性请求协助。"
        ),
        created_at=report.decision_as_of,
    )


def audit_public_answer(
    text: str,
    *,
    context: ResponseContext | None = None,
    policy: PresentationPolicy | None = None,
    source_text: str | None = None,
    required_fingerprint: FactFingerprint | None = None,
    allowed_private_paths: Iterable[str] = (),
) -> PresentationAudit:
    selected = policy or load_presentation_policy()
    resolved_context = context or ResponseContext(
        task_type=ResponseTaskType.DEEP_RESEARCH
    )
    budget = selected.budget(resolved_context.task_type)
    findings: set[str] = set()
    stripped = text.strip()
    budget_exceeded = len(stripped) > budget.max_chars
    if not stripped:
        findings.add("EMPTY_PUBLIC_ANSWER")
    if budget_exceeded:
        findings.add("PUBLIC_ANSWER_TOO_LONG")
    bullet_count = sum(
        line.lstrip().startswith(("- ", "* ", "• "))
        for line in stripped.splitlines()
    )
    if bullet_count > selected.max_bullets:
        findings.add("PUBLIC_ANSWER_TOO_MANY_BULLETS")
    heading_levels = [
        len(match.group(1))
        for line in stripped.splitlines()
        if (match := re.match(r"^(#+)\s+", line.strip())) is not None
    ]
    if heading_levels and max(heading_levels) > selected.max_heading_level:
        findings.add("PUBLIC_ANSWER_HEADING_TOO_DEEP")
    if _has_semantic_repetition(stripped, selected.semantic_duplicate_threshold):
        findings.add("PUBLIC_ANSWER_REPETITIVE")
    if any(
        item.casefold() in stripped.casefold()
        for item in selected.forbidden_expressions
    ):
        findings.add("CHINESE_STYLE_FORBIDDEN_EXPRESSION")
    if _english_density(stripped, selected) > selected.english_density_threshold:
        findings.add("CHINESE_STYLE_EXCESSIVE_ENGLISH")
    if re.search(
        r"(?:!{2,}|！{2,}|\?{2,}|？{2,}|-{3,}|={3,}|→{3,}|>{3,})",
        stripped,
    ):
        findings.add("CHINESE_STYLE_SYMBOL_PILE")
    if re.search(r"[\u4e00-\u9fff][,:;][\u4e00-\u9fff]", stripped):
        findings.add("CHINESE_STYLE_HALF_WIDTH_PUNCTUATION")

    secret_exposed = _contains_secret(stripped)
    if secret_exposed:
        findings.add("SECRET_OR_CREDENTIAL_EXPOSED")
    path_scan_text = _remove_allowed_paths(stripped, allowed_private_paths)
    private_path_exposed = _PRIVATE_PATH_PATTERN.search(path_scan_text) is not None
    if private_path_exposed:
        findings.add("PRIVATE_USER_PATH_EXPOSED")

    scan_text = _URL_PATTERN.sub(" ", stripped)
    for code, patterns in _ANSWER_POLICY_PATTERNS:
        if any(
            re.search(pattern, scan_text, flags=re.IGNORECASE)
            for pattern in patterns
        ):
            findings.add(code)
    lowered = scan_text.casefold()
    if any(term.casefold() in lowered for term in internal_vocabulary_terms()):
        findings.add("DYNAMIC_INTERNAL_VOCABULARY_EXPOSED")
    internal_exposed = bool(findings.intersection(_INTERNAL_FINDING_CODES))

    checked = source_text is not None or required_fingerprint is not None
    known_entities = required_fingerprint.entities if required_fingerprint else ()
    known_phrases = required_fingerprint.locked_phrases if required_fingerprint else ()
    output_fingerprint = extract_fact_fingerprint(
        stripped,
        known_entities=known_entities,
        known_phrases=known_phrases,
    )
    no_unexpected_facts = True
    if source_text is not None:
        allowed = extract_fact_fingerprint(
            source_text,
            known_entities=known_entities,
        )
        no_unexpected_facts = _fingerprint_contains(
            allowed,
            output_fingerprint,
            include_locked_phrases=False,
        )
        if not no_unexpected_facts:
            findings.add("PUBLIC_FACT_ADDED")
    required_preserved = True
    if required_fingerprint is not None:
        required_preserved = _fingerprint_contains(
            output_fingerprint,
            required_fingerprint,
        )
        if not required_preserved:
            findings.add("PUBLIC_REQUIRED_FACT_MISSING")
    fact_drift = not no_unexpected_facts or not required_preserved
    if fact_drift:
        findings.add("PUBLIC_FACT_DRIFT")

    ordered = sorted(findings)
    equivalence = (
        FactEquivalenceStatus.NOT_CHECKED
        if not checked
        else FactEquivalenceStatus.FAIL
        if fact_drift
        else FactEquivalenceStatus.PASS
    )
    return PresentationAudit(
        status="FAIL" if ordered else "PASS",
        finding_codes=ordered,
        character_count=len(stripped),
        character_budget=budget.max_chars,
        budget_status=(
            BudgetStatus.EXCEEDED
            if budget_exceeded
            else BudgetStatus.WITHIN_BUDGET
        ),
        fact_equivalence_status=equivalence,
        fact_drift_detected=fact_drift,
        required_content_preserved=required_preserved,
        secret_exposed=secret_exposed,
        private_path_exposed=private_path_exposed,
        internal_implementation_exposed=internal_exposed,
        safe_to_send=not ordered,
        raw_answer_echoed=False,
    )


def audit_developer_answer(
    text: str,
    *,
    context: ResponseContext | None = None,
    policy: PresentationPolicy | None = None,
) -> PresentationAudit:
    selected = policy or load_presentation_policy()
    resolved_context = context or ResponseContext(
        mode=ResponseMode.DEVELOPER,
        task_type=ResponseTaskType.DEVELOPER_DIAGNOSTIC,
        diagnostic_intent_detected=True,
    )
    budget = selected.budget(resolved_context.task_type)
    stripped = text.strip()
    findings: set[str] = set()
    if not stripped:
        findings.add("EMPTY_DEVELOPER_ANSWER")
    budget_exceeded = len(stripped) > budget.max_chars
    if budget_exceeded:
        findings.add("DEVELOPER_ANSWER_TOO_LONG")
    secret_exposed = _contains_secret(stripped)
    private_path_exposed = _PRIVATE_PATH_PATTERN.search(stripped) is not None
    if secret_exposed:
        findings.add("SECRET_OR_CREDENTIAL_EXPOSED")
    if private_path_exposed:
        findings.add("PRIVATE_USER_PATH_EXPOSED")
    ordered = sorted(findings)
    return PresentationAudit(
        status="FAIL" if ordered else "PASS",
        finding_codes=ordered,
        character_count=len(stripped),
        character_budget=budget.max_chars,
        budget_status=(
            BudgetStatus.EXCEEDED
            if budget_exceeded
            else BudgetStatus.WITHIN_BUDGET
        ),
        fact_equivalence_status=FactEquivalenceStatus.NOT_CHECKED,
        fact_drift_detected=False,
        required_content_preserved=True,
        secret_exposed=secret_exposed,
        private_path_exposed=private_path_exposed,
        internal_implementation_exposed=False,
        safe_to_send=not ordered,
        raw_answer_echoed=False,
    )


def audit_investor_answer(text: str) -> InvestorAnswerAudit:
    """Backwards-compatible audit surface backed by the unified policy."""

    audit = audit_public_answer(
        text,
        context=ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH),
    )
    mapping = {
        "EMPTY_PUBLIC_ANSWER": "EMPTY_INVESTOR_ANSWER",
        "PUBLIC_ANSWER_TOO_LONG": "INVESTOR_ANSWER_TOO_LONG",
        "PUBLIC_ANSWER_TOO_MANY_BULLETS": "INVESTOR_ANSWER_TOO_MANY_BULLETS",
        "PUBLIC_ANSWER_REPETITIVE": "INVESTOR_ANSWER_REPETITIVE",
    }
    finding_codes = sorted({mapping.get(code, code) for code in audit.finding_codes})
    return InvestorAnswerAudit(
        status="FAIL" if finding_codes else "PASS",
        finding_codes=finding_codes,
        internal_implementation_exposed=(
            audit.internal_implementation_exposed
            or audit.secret_exposed
            or audit.private_path_exposed
        ),
        developer_meta_exposed="DEVELOPER_META_EXPOSED" in finding_codes,
    )


def normalize_public_text(
    text: str,
    *,
    policy: PresentationPolicy | None = None,
) -> str:
    """Clean deterministic style only when all critical fact tokens stay identical."""

    selected = policy or load_presentation_policy()
    before = extract_fact_fingerprint(text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    for filler in _LEADING_FILLERS:
        normalized = re.sub(
            rf"(?m)(^|[。！？!?]\s*){re.escape(filler)}[，,:：]?\s*",
            r"\1",
            normalized,
        )
    normalized = re.sub(
        r"(?<=[\u4e00-\u9fff]),(?=[\u4e00-\u9fff])",
        "，",
        normalized,
    )
    normalized = re.sub(
        r"(?<=[\u4e00-\u9fff]):(?=[\u4e00-\u9fff])",
        "：",
        normalized,
    )
    normalized = re.sub(
        r"(?<=[\u4e00-\u9fff]);(?=[\u4e00-\u9fff])",
        "；",
        normalized,
    )
    normalized = re.sub(r"！{2,}", "！", normalized)
    normalized = re.sub(r"？{2,}", "？", normalized)
    normalized = _drop_duplicate_lines(
        normalized,
        selected.semantic_duplicate_threshold,
    ).strip()
    after = extract_fact_fingerprint(normalized)
    return normalized if before == after else text.strip()


def extract_fact_fingerprint(
    text: str,
    *,
    known_entities: Iterable[str] = (),
    known_phrases: Iterable[str] = (),
) -> FactFingerprint:
    strength_terms = [
        label
        for strength, label in _STRENGTH_LABELS.items()
        if strength is not ConclusionStrength.UNSPECIFIED
        and re.search(rf"结论强度[:：]\s*{re.escape(label)}(?:\s|$|[。；，])", text)
    ]
    return FactFingerprint(
        entities=_ordered_unique(
            entity for entity in known_entities if entity and entity in text
        ),
        security_codes=_ordered_unique(
            re.findall(r"(?<!\d)\d{6}(?!\d)", text)
        ),
        numbers=_ordered_unique(
            re.findall(
                r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:%|％|倍|元|万元|亿元|股)?",
                text,
            )
        ),
        dates=_ordered_unique(
            re.findall(
                r"(?:20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?"
                r"|20\d{2}年\d{1,2}月\d{1,2}日)",
                text,
            )
        ),
        times=_ordered_unique(
            re.findall(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)", text)
        ),
        direction_terms=_ordered_unique(
            term for term in _DIRECTION_TERMS if term in text
        ),
        conclusion_strength_terms=_ordered_unique(strength_terms),
        citations=_ordered_unique(
            re.findall(
                r"\[[A-Za-z][A-Za-z0-9_-]{0,23}\]|https?://[^\s)）]+",
                text,
            )
        ),
        locked_phrases=_ordered_unique(
            phrase
            for phrase in known_phrases
            if phrase and _normalized_phrase_present(text, phrase)
        ),
    )


def redact_sensitive_text(text: str) -> str:
    value = text
    for pattern in _SECRET_PATTERNS:
        value = re.sub(pattern, "[REDACTED]", value)
    return _PRIVATE_PATH_PATTERN.sub("[PRIVATE_PATH]", value)


def _developer_payload(
    diagnostics: DeveloperDiagnosticsInput,
) -> DeveloperDiagnosticsModel:
    return DeveloperDiagnosticsModel(
        user_impact=redact_sensitive_text(diagnostics.user_impact),
        failure_class=redact_sensitive_text(diagnostics.failure_class),
        correlation_id=redact_sensitive_text(diagnostics.correlation_id),
        stage=(redact_sensitive_text(diagnostics.stage) if diagnostics.stage else None),
        next_action=(
            redact_sensitive_text(diagnostics.next_action)
            if diagnostics.next_action
            else None
        ),
    )


def _investor_payload(
    narrative: ResearchNarrativeBundle,
    *,
    context: ResponseContext,
    policy: PresentationPolicy,
) -> InvestorPresentationModel:
    budget = policy.budget(context.task_type)
    reason_limit = budget.max_reasons
    if context.requested_detail is ResponseDetail.SHORT:
        reason_limit = min(reason_limit, 2)
    reasons = _dedupe_semantic_items(
        narrative.reasons,
        policy.semantic_duplicate_threshold,
    )[:reason_limit]
    payload = InvestorPresentationModel(
        subject=narrative.subject,
        headline=normalize_public_text(narrative.headline, policy=policy),
        conclusion_strength=narrative.conclusion_strength,
        valuation_or_odds=[
            normalize_public_text(item, policy=policy)
            for item in _dedupe_semantic_items(
                narrative.valuation_or_odds,
                policy.semantic_duplicate_threshold,
            )
        ],
        reasons=[normalize_public_text(item, policy=policy) for item in reasons],
        risk=(
            normalize_public_text(narrative.risks[0], policy=policy)
            if narrative.risks
            else None
        ),
        change_condition=(
            normalize_public_text(narrative.change_conditions[0], policy=policy)
            if narrative.change_conditions
            else None
        ),
        data_as_of=_format_as_of(narrative.data_as_of),
        citations=_dedupe_items(narrative.citations),
        report_reference=_safe_report_reference(narrative.report_path),
    )
    while payload.reasons and len(_render_investor_text(payload)) > budget.max_chars:
        payload = payload.model_copy(update={"reasons": payload.reasons[:-1]})
    return payload


def _render_investor_text(payload: InvestorPresentationModel) -> str:
    lines = [f"主体：{payload.subject}", f"结论：{payload.headline}"]
    if payload.conclusion_strength is not ConclusionStrength.UNSPECIFIED:
        lines.append(
            f"结论强度：{_STRENGTH_LABELS[payload.conclusion_strength]}"
        )
    if payload.valuation_or_odds:
        lines.append("估值与赔率：" + "；".join(payload.valuation_or_odds))
    if payload.reasons:
        lines.append("主要依据：")
        lines.extend(f"- {item}" for item in payload.reasons)
    if payload.risk:
        lines.append(f"最大风险：{payload.risk}")
    if payload.change_condition:
        lines.append(f"改变判断的条件：{payload.change_condition}")
    if payload.data_as_of:
        lines.append(f"数据截至：{payload.data_as_of}")
    if payload.report_reference:
        lines.append(
            f"{payload.report_reference.label}：{payload.report_reference.file_name}"
        )
    if payload.citations:
        lines.append("来源：" + "；".join(payload.citations))
    return "\n".join(lines)


def _render_developer_text(payload: DeveloperDiagnosticsModel) -> str:
    lines = [f"影响：{payload.user_impact}", f"故障分类：{payload.failure_class}"]
    if payload.stage:
        lines.append(f"阶段：{payload.stage}")
    lines.append(f"关联号：{payload.correlation_id}")
    if payload.next_action:
        lines.append(f"下一步：{payload.next_action}")
    return "\n".join(lines)


def _required_fingerprint_for_payload(
    payload: InvestorPresentationModel,
    extra: FactFingerprint | None,
) -> FactFingerprint:
    phrases = [
        payload.subject,
        payload.headline,
        *payload.valuation_or_odds,
        payload.risk or "",
        payload.change_condition or "",
        payload.data_as_of or "",
        *payload.citations,
        payload.report_reference.file_name if payload.report_reference else "",
    ]
    automatic = extract_fact_fingerprint(
        _render_investor_text(payload),
        known_entities=[payload.subject],
        known_phrases=phrases,
    )
    return _merge_fingerprints(automatic, extra) if extra else automatic


def _narrative_source_text(narrative: ResearchNarrativeBundle) -> str:
    values = [
        narrative.subject,
        narrative.headline,
        f"结论强度：{_STRENGTH_LABELS[narrative.conclusion_strength]}",
        *narrative.valuation_or_odds,
        *narrative.reasons,
        *narrative.risks,
        *narrative.change_conditions,
        *narrative.citations,
    ]
    if narrative.data_as_of is not None:
        values.append(_format_as_of(narrative.data_as_of) or "")
    if narrative.report_path:
        values.extend(
            [narrative.report_path, _safe_report_file_name(narrative.report_path)]
        )
    return "\n".join(value for value in values if value)


def _format_as_of(value: object) -> str | None:
    if value is None:
        return None
    if not hasattr(value, "strftime"):
        return str(value)
    return value.strftime("%Y年%m月%d日 %H:%M")  # type: ignore[union-attr]


def _safe_fallback_subject(
    value: str,
    *,
    context: ResponseContext,
    policy: PresentationPolicy,
) -> str:
    candidate = redact_sensitive_text(value)
    if candidate != value:
        return "研究对象"
    audit = audit_public_answer(
        f"主体：{candidate}",
        context=context,
        policy=policy,
    )
    return candidate if audit.safe_to_send else "研究对象"


def _safe_report_reference(value: str | None) -> PublicReportReference | None:
    if not value:
        return None
    file_name = _safe_report_file_name(value)
    return PublicReportReference(file_name=file_name) if file_name else None


def _safe_report_file_name(value: str) -> str:
    windows_name = PureWindowsPath(value).name
    posix_name = PurePath(value.replace("\\", "/")).name
    candidate = windows_name or posix_name
    candidate = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", candidate).strip(" .")
    return candidate[:255] or "研究报告"


def _contains_secret(text: str) -> bool:
    return any(re.search(pattern, text) is not None for pattern in _SECRET_PATTERNS)


def _remove_allowed_paths(text: str, paths: Iterable[str]) -> str:
    result = text
    for value in sorted(
        {str(item) for item in paths if str(item)},
        key=len,
        reverse=True,
    ):
        result = result.replace(value, "<ALLOWED_REPORT_PATH>")
    return result


def _english_density(text: str, policy: PresentationPolicy) -> float:
    masked = _URL_PATTERN.sub(" ", text)
    masked = re.sub(r"\[[A-Za-z0-9_-]{1,24}\]", " ", masked)
    for term in policy.protected_terms:
        masked = re.sub(re.escape(term), " ", masked, flags=re.IGNORECASE)
    latin = len(re.findall(r"[A-Za-z]", masked))
    chinese = len(re.findall(r"[\u4e00-\u9fff]", masked))
    denominator = latin + chinese
    return latin / denominator if denominator else 0.0


def _has_semantic_repetition(text: str, threshold: float) -> bool:
    sentences = [_semantic_normalize(item) for item in _sentence_units(text)]
    sentences = [item for item in sentences if len(item) >= 12][:32]
    for index, left in enumerate(sentences):
        for right in sentences[index + 1 :]:
            if left == right:
                return True
            if SequenceMatcher(a=left, b=right, autojunk=False).ratio() >= threshold:
                return True
    return False


def _drop_duplicate_lines(text: str, threshold: float) -> str:
    lines: list[str] = []
    seen: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("- ", "* ", "• ", "#")):
            lines.append(line)
            continue
        signature = _semantic_normalize(stripped)
        if len(signature) < 12:
            lines.append(line)
            continue
        if any(
            signature == previous
            or SequenceMatcher(a=signature, b=previous, autojunk=False).ratio()
            >= threshold
            for previous in seen[-16:]
        ):
            continue
        seen.append(signature)
        lines.append(line)
    return "\n".join(lines)


def _sentence_units(text: str) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*• ")
        if not stripped:
            continue
        result.extend(
            part.strip()
            for part in re.split(r"[。！？!?；;]+", stripped)
            if part.strip()
        )
    return result


def _semantic_normalize(text: str) -> str:
    value = text.casefold()
    value = re.sub(
        r"^(?:主体|结论|结论强度|总结|建议|主要依据|估值与赔率|"
        r"最大风险|风险|原因|判断)[:：\s]+",
        "",
        value,
    )
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)


def _normalized_phrase_present(text: str, phrase: str) -> bool:
    return _semantic_normalize(phrase) in _semantic_normalize(text)


def _fingerprint_contains(
    parent: FactFingerprint,
    child: FactFingerprint,
    *,
    include_locked_phrases: bool = True,
) -> bool:
    fields: list[str] = list(_FINGERPRINT_FIELDS)
    if include_locked_phrases:
        fields.append("locked_phrases")
    return all(
        set(getattr(child, field)).issubset(set(getattr(parent, field)))
        for field in fields
    )


def _merge_fingerprints(
    left: FactFingerprint,
    right: FactFingerprint,
) -> FactFingerprint:
    return FactFingerprint(
        entities=_ordered_unique([*left.entities, *right.entities]),
        security_codes=_ordered_unique(
            [*left.security_codes, *right.security_codes]
        ),
        numbers=_ordered_unique([*left.numbers, *right.numbers]),
        dates=_ordered_unique([*left.dates, *right.dates]),
        times=_ordered_unique([*left.times, *right.times]),
        direction_terms=_ordered_unique(
            [*left.direction_terms, *right.direction_terms]
        ),
        conclusion_strength_terms=_ordered_unique(
            [
                *left.conclusion_strength_terms,
                *right.conclusion_strength_terms,
            ]
        ),
        citations=_ordered_unique([*left.citations, *right.citations]),
        locked_phrases=_ordered_unique(
            [*left.locked_phrases, *right.locked_phrases]
        ),
    )


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _dedupe_items(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    signatures: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        signature = _semantic_normalize(text)
        if signature in signatures:
            continue
        signatures.add(signature)
        result.append(text)
    return result


def _dedupe_semantic_items(
    values: Iterable[str],
    threshold: float,
) -> list[str]:
    result: list[str] = []
    signatures: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        signature = _semantic_normalize(text)
        if any(
            signature == previous
            or SequenceMatcher(a=signature, b=previous, autojunk=False).ratio()
            >= threshold
            for previous in signatures
        ):
            continue
        signatures.append(signature)
        result.append(text)
    return result


__all__ = [
    "ResponseGateway",
    "audit_developer_answer",
    "audit_investor_answer",
    "audit_public_answer",
    "classify_response_mode",
    "extract_fact_fingerprint",
    "investor_view_from_acquisition",
    "investor_view_from_run",
    "narrative_from_investor_view",
    "normalize_public_text",
    "redact_sensitive_text",
]
