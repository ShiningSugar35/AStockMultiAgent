from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.research.presentation import (
    ResponseGateway,
    audit_public_answer,
    classify_response_mode,
    extract_fact_fingerprint,
    normalize_public_text,
)
from astock.schemas.presentation import (
    BudgetStatus,
    ConclusionStrength,
    DeveloperDiagnosticsInput,
    FactEquivalenceStatus,
    FactFingerprint,
    InvestorPresentationModel,
    PresentationAudit,
    ResearchNarrativeBundle,
    ResponseContext,
    ResponseDetail,
    ResponseMode,
    ResponseTaskType,
)
from astock.schemas.research_acquisition import (
    InvestorResearchState,
    InvestorResearchView,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)

_NORMAL_INVESTOR_PROMPTS = [
    f"{prefix}{name}现在怎么看？"
    for prefix in ("请分析", "想了解", "帮我看看", "为什么")
    for name in (
        "贵州茅台",
        "中国海油",
        "宁德时代",
        "工商银行",
        "沪深300",
        "我的持仓",
        "这个组合",
        "当前估值",
        "行业景气",
        "未来半年风险",
    )
]

_DEVELOPER_PROMPTS = [
    f"请{term}并说明原因"
    for term in (
        "调试",
        "排查系统",
        "看日志",
        "查看日志",
        "查看状态码",
        "查看错误码",
        "检查工件",
        "检查数据库",
        "排查 SQLite",
        "排查 provider",
        "查看接口错误",
        "查看 traceback",
        "查看 stack trace",
        "检查 CLI",
        "检查 schema",
        "核对哈希",
        "核对 hash",
        "排查系统日志",
        "检查 artifact",
        "排查数据库错误",
        "调试接口错误",
    )
]

_NEGATED_DIAGNOSTIC_PROMPTS = [
    "不要看日志，直接告诉我这只股票还能不能持有",
    "不用看日志，请给投资结论",
    "无需看日志，只分析基本面",
    "不需要调试，按普通投资者模式回答",
    "不用调试这套程序，分析贵州茅台",
    "无需调试，先说估值是否合理",
    "不要调试，告诉我最大风险",
    "别进入开发者模式，正常回答",
    "不要进入开发者模式，为什么今天下跌",
    "不用进入开发者模式，请看组合风险",
    "无需进入开发者模式，给我简明结论",
    "不是让你调试，我只是问为什么这只股票下跌",
]

_SYSTEM_ERROR_TEXTS = [
    "provider timeout",
    "数据库连接失败",
    "Traceback: upstream request failed",
    "接口错误：500",
    "系统日志异常",
    "schema validation failed",
    "migration failed",
    "SQL execution error",
]

_BAD_STYLE_TEXTS = [
    "综上所述，公司盈利仍然稳定。",
    "值得注意的是，公司盈利仍然稳定。",
    "需要强调的是，公司盈利仍然稳定。",
    "毋庸置疑，公司盈利仍然稳定。",
    "从某种意义上说，公司盈利仍然稳定。",
    "不难发现，公司盈利仍然稳定。",
    "可以看出，公司盈利仍然稳定。",
    "作为一个 AI，我认为公司盈利稳定。",
    "以下是为您整理的公司结论。",
    "该能力可以赋能投资研究。",
    "这是关键抓手。",
    "这是全方位判断。",
    "这是多维度判断。",
    "需要深度挖掘。",
    "先跑 research-plan 再判断。",
    "当前状态是 NEEDS_INFO。",
    "artifact_id=abc123。",
    "使用 sqlite 查询。",
    "provider 是 baostock。",
    "Authorization: Bearer [REDACTED_SECRET]",
]

_GOOD_STYLE_TEXTS = [
    f"结论：{name}目前更适合{action}。核心依据是盈利趋势与估值匹配，最大风险是需求低于预期。"
    for name in ("贵州茅台", "中国海油", "宁德时代", "工商银行", "中国移动")
    for action in ("继续观察", "持有", "等待更好价格", "控制仓位")
]

_FACT_TEXTS = [
    f"综上所述，{code}在2026年8月{day}日的参考值为{value}元，建议持有 [S{day}]。"
    for day, (code, value) in enumerate(
        [
            ("600519", "1400.00"),
            ("600938", "27.30"),
            ("300750", "215.60"),
            ("601398", "7.20"),
            ("600941", "108.50"),
            ("601857", "9.10"),
            ("601088", "38.20"),
            ("600036", "42.60"),
            ("000858", "128.00"),
            ("000333", "73.10"),
            ("002594", "105.30"),
            ("601318", "55.20"),
            ("600900", "30.10"),
            ("600276", "51.80"),
            ("601899", "24.60"),
            ("600030", "28.30"),
            ("000001", "12.30"),
            ("601166", "21.40"),
            ("600887", "31.50"),
            ("600309", "68.90"),
        ],
        start=1,
    )
]

_SECRET_OR_PATH_TEXTS = [
    "Authorization: Bearer [REDACTED_SECRET]",
    "Cookie: session=[REDACTED_SECRET]",
    "password=[REDACTED_SECRET]",
    "api_key=[REDACTED_SECRET]",
    r"报告位于 C:\Users\alice\Documents\private.docx",
    "报告位于 /home/alice/private/private.docx",
]

_CANONICAL_SOURCE = "\n".join(
    [
        "主体：贵州茅台（600519）",
        "结论：当前估值偏高，更适合持有而不是追涨。",
        "结论强度：高",
        "估值与赔率：参考价1400.00元，预期收益区间8%至12%。",
        "最大风险：需求低于预期可能压低盈利中枢。",
        "改变判断的条件：2026年9月30日15:00后盈利超预期且估值回落。",
        "数据截至：2026年8月31日 15:00",
        "来源：[S1]；[S2]",
    ]
)
_CANONICAL_PHRASES = [
    "贵州茅台（600519）",
    "当前估值偏高，更适合持有而不是追涨。",
    "参考价1400.00元，预期收益区间8%至12%。",
    "需求低于预期可能压低盈利中枢。",
    "2026年9月30日15:00后盈利超预期且估值回落。",
    "2026年8月31日 15:00",
    "[S1]",
    "[S2]",
]
_DRIFT_CASES = [
    ("贵州茅台（600519）", "中国海油（600519）", "entity"),
    ("600519", "600938", "security_code"),
    ("1400.00元", "1500.00元", "valuation_number"),
    ("2026年9月30日", "2026年10月31日", "change_date"),
    ("15:00后盈利", "16:00后盈利", "change_time"),
    ("更适合持有", "更适合卖出", "direction"),
    ("[S2]", "[S3]", "citation"),
    ("结论强度：高", "结论强度：中", "conclusion_strength"),
    ("参考价1400.00元，预期收益区间8%至12%。", "", "valuation_deleted"),
    ("最大风险：需求低于预期可能压低盈利中枢。", "", "risk_deleted"),
    (
        "改变判断的条件：2026年9月30日15:00后盈利超预期且估值回落。",
        "",
        "change_condition_deleted",
    ),
    ("数据截至：2026年8月31日 15:00", "", "data_time_deleted"),
]


def _required_fingerprint() -> FactFingerprint:
    return extract_fact_fingerprint(
        _CANONICAL_SOURCE,
        known_entities=["贵州茅台（600519）"],
        known_phrases=_CANONICAL_PHRASES,
    )


@pytest.mark.parametrize("prompt", _NORMAL_INVESTOR_PROMPTS)
def test_normal_investment_prompts_stay_investor_mode(prompt: str) -> None:
    assert classify_response_mode(prompt) is ResponseMode.INVESTOR


@pytest.mark.parametrize("prompt", _DEVELOPER_PROMPTS)
def test_explicit_diagnostics_enter_developer_mode(prompt: str) -> None:
    assert classify_response_mode(prompt) is ResponseMode.DEVELOPER


@pytest.mark.parametrize("prompt", _NEGATED_DIAGNOSTIC_PROMPTS)
def test_negated_diagnostics_stay_investor_mode(prompt: str) -> None:
    assert classify_response_mode(prompt) is ResponseMode.INVESTOR


@pytest.mark.parametrize("message", _SYSTEM_ERROR_TEXTS)
def test_error_text_without_user_diagnostic_action_stays_investor_mode(
    message: str,
) -> None:
    assert classify_response_mode(message) is ResponseMode.INVESTOR


def test_later_affirmative_diagnostic_request_wins_over_earlier_negation() -> None:
    request = "不要看日志，但请排查接口错误并给出关联号"
    assert classify_response_mode(request) is ResponseMode.DEVELOPER


@pytest.mark.parametrize(
    "prompt",
    [
        "为什么这只股票最近下跌？",
        "为什么估值这么高？",
        "为什么组合回撤扩大？",
        "为什么公司利润下降？",
        "为什么今天没有交易？",
    ],
)
def test_plain_why_never_implies_developer_mode(prompt: str) -> None:
    assert classify_response_mode(prompt) is ResponseMode.INVESTOR


@pytest.mark.parametrize("text", _BAD_STYLE_TEXTS)
def test_bad_style_or_internal_output_fails(text: str) -> None:
    audit = audit_public_answer(
        text,
        context=ResponseContext(task_type=ResponseTaskType.COMPANY_QUICK_VIEW),
    )
    assert audit.status == "FAIL"
    assert audit.safe_to_send is False


@pytest.mark.parametrize("text", _GOOD_STYLE_TEXTS)
def test_plain_chinese_investor_text_passes(text: str) -> None:
    audit = audit_public_answer(
        text,
        context=ResponseContext(task_type=ResponseTaskType.COMPANY_QUICK_VIEW),
    )
    assert audit.status == "PASS"
    assert audit.safe_to_send is True
    assert audit.budget_status is BudgetStatus.WITHIN_BUDGET


@pytest.mark.parametrize("text", _FACT_TEXTS)
def test_normalization_preserves_critical_financial_facts(text: str) -> None:
    normalized = normalize_public_text(text)
    assert extract_fact_fingerprint(normalized) == extract_fact_fingerprint(text)
    assert "综上所述" not in normalized


@pytest.mark.parametrize("text", _SECRET_OR_PATH_TEXTS)
def test_secret_or_private_path_is_explicitly_reported(text: str) -> None:
    audit = audit_public_answer(text)
    assert audit.status == "FAIL"
    assert audit.secret_exposed or audit.private_path_exposed
    assert audit.safe_to_send is False


@pytest.mark.parametrize("old,new,_label", _DRIFT_CASES)
def test_every_locked_fact_deletion_or_change_fails(
    old: str,
    new: str,
    _label: str,
) -> None:
    output = _CANONICAL_SOURCE.replace(old, new)
    audit = audit_public_answer(
        output,
        source_text=_CANONICAL_SOURCE,
        required_fingerprint=_required_fingerprint(),
        context=ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH),
    )
    assert audit.status == "FAIL"
    assert audit.fact_equivalence_status is FactEquivalenceStatus.FAIL
    assert audit.fact_drift_detected is True
    assert (
        audit.required_content_preserved is False
        or "PUBLIC_FACT_ADDED" in audit.finding_codes
    )


def test_canonical_contract_accepts_legacy_aliases_but_serializes_one_shape() -> None:
    fingerprint = FactFingerprint.model_validate({"directions": ["持有"]})
    narrative = ResearchNarrativeBundle.model_validate(
        {
            "subject": "600519",
            "conclusion": "当前更适合持有。",
        }
    )
    context = ResponseContext(requested_detail=ResponseDetail.SHORT)

    assert fingerprint.schema_version == "fact-fingerprint-v2"
    assert fingerprint.direction_terms == ["持有"]
    assert fingerprint.directions == ["持有"]
    assert "direction_terms" in fingerprint.model_dump()
    assert "directions" not in fingerprint.model_dump()
    assert narrative.schema_version == "research-narrative-bundle-v1"
    assert narrative.headline == "当前更适合持有。"
    assert narrative.conclusion == narrative.headline
    assert "headline" in narrative.model_dump()
    assert "conclusion" not in narrative.model_dump()
    assert context.schema_version == "response-context-v1"
    assert context.requested_detail is ResponseDetail.SHORT


def test_conflicting_legacy_and_canonical_fields_are_rejected() -> None:
    with pytest.raises(ValueError):
        FactFingerprint.model_validate(
            {
                "direction_terms": ["持有"],
                "directions": ["卖出"],
            }
        )
    with pytest.raises(ValueError):
        ResearchNarrativeBundle.model_validate(
            {
                "subject": "600519",
                "headline": "当前适合持有。",
                "conclusion": "当前适合卖出。",
            }
        )


def test_presentation_audit_contract_exposes_all_required_statuses() -> None:
    fields = set(PresentationAudit.model_fields)
    assert {
        "fact_equivalence_status",
        "secret_exposed",
        "private_path_exposed",
        "internal_implementation_exposed",
        "budget_status",
        "required_content_preserved",
    }.issubset(fields)


def test_system_error_does_not_switch_a_plain_user_request_to_developer_mode() -> None:
    context = ResponseGateway().context(
        "为什么这只股票下跌？",
        system_error_present=True,
    )
    assert context.mode is ResponseMode.INVESTOR
    assert context.system_error_present is True


def test_gateway_projects_complete_investor_fields_and_safe_report_reference() -> None:
    gateway = ResponseGateway()
    context = gateway.context("现在能买吗？")
    narrative = ResearchNarrativeBundle(
        subject="贵州茅台（600519）",
        headline="当前价格没有明显安全边际，更适合等待。",
        conclusion_strength=ConclusionStrength.MODERATE,
        valuation_or_odds=["参考价1400.00元，预期收益区间8%至12%。"],
        reasons=["盈利仍然稳定。", "估值已经反映较高增长预期。"],
        risks=["如果需求弱于预期，盈利与估值可能同时下修。"],
        change_conditions=["盈利超预期且估值回到更有安全边际的位置。"],
        data_as_of=NOW,
        citations=["[S1]", "[S2]"],
        report_path=r"C:\Users\alice\Documents\贵州茅台研究报告.docx",
    )

    rendered = gateway.render(context, narrative=narrative)

    assert rendered.safe_fallback_used is False
    assert rendered.audit.status == "PASS"
    assert rendered.audit.fact_equivalence_status is FactEquivalenceStatus.PASS
    assert isinstance(rendered.payload, InvestorPresentationModel)
    assert rendered.payload.subject == narrative.subject
    assert rendered.payload.conclusion_strength is ConclusionStrength.MODERATE
    assert rendered.payload.valuation_or_odds == narrative.valuation_or_odds
    assert rendered.payload.citations == narrative.citations
    assert rendered.payload.report_reference is not None
    assert rendered.payload.report_reference.file_name == "贵州茅台研究报告.docx"
    assert "C:\\Users\\alice" not in rendered.text
    for required in (
        narrative.subject,
        narrative.headline,
        narrative.valuation_or_odds[0],
        narrative.risks[0],
        narrative.change_conditions[0],
        "2026年08月31日 15:00",
        "[S1]",
        "[S2]",
    ):
        assert required in rendered.text


def test_length_reduction_only_removes_noncritical_reasons() -> None:
    gateway = ResponseGateway()
    narrative = ResearchNarrativeBundle(
        subject="贵州茅台（600519）",
        headline="当前估值偏高，更适合持有等待。",
        conclusion_strength=ConclusionStrength.HIGH,
        valuation_or_odds=["参考价1400.00元，预期收益区间8%至12%。"],
        reasons=[
            f"第{index}条辅助理由：" + "经营数据仍需持续观察。" * 12
            for index in range(12)
        ],
        risks=["需求低于预期可能压低盈利中枢。"],
        change_conditions=["2026年9月30日15:00后盈利超预期且估值回落。"],
        data_as_of=NOW,
        citations=["[S1]", "[S2]"],
    )

    rendered = gateway.render(gateway.context("简要回答"), narrative=narrative)

    assert rendered.safe_fallback_used is False
    assert len(rendered.text) <= rendered.audit.character_budget
    assert isinstance(rendered.payload, InvestorPresentationModel)
    assert len(rendered.payload.reasons) < len(narrative.reasons)
    assert rendered.payload.risk == narrative.risks[0]
    assert rendered.payload.change_condition == narrative.change_conditions[0]
    assert rendered.payload.data_as_of == "2026年08月31日 15:00"
    assert rendered.payload.citations == narrative.citations
    assert rendered.payload.valuation_or_odds == narrative.valuation_or_odds


def test_mandatory_content_over_budget_uses_safe_fallback_without_echo() -> None:
    unsafe_marker = "不得回显的原始风险"
    narrative = ResearchNarrativeBundle(
        subject="600519",
        headline="当前结论仍需谨慎。",
        risks=[unsafe_marker + "，" + "风险内容很长。" * 160],
        change_conditions=["盈利和估值同时改善后再复核。"],
        data_as_of=NOW,
        citations=["[S1]"],
    )

    gateway = ResponseGateway()
    rendered = gateway.render(gateway.context("请简要回答"), narrative=narrative)

    assert rendered.safe_fallback_used is True
    assert rendered.audit.budget_status is BudgetStatus.SAFE_FALLBACK
    assert unsafe_marker not in rendered.text
    assert "600519" in rendered.text
    assert rendered.raw_draft_exposed is False


def test_gateway_never_echoes_internal_investor_text_on_audit_failure() -> None:
    raw = "先跑 research-plan，当前状态是 NEEDS_INFO，artifact_id=internal-value。"
    gateway = ResponseGateway()
    rendered = gateway.render(
        gateway.context("这家公司现在能买吗？"),
        narrative=ResearchNarrativeBundle(subject="600519", headline=raw),
    )
    assert "research-plan" not in rendered.text
    assert "NEEDS_INFO" not in rendered.text
    assert "artifact_id" not in rendered.text
    assert rendered.safe_fallback_used is True
    assert rendered.raw_draft_exposed is False


def test_safe_fallback_never_echoes_an_unsafe_subject() -> None:
    gateway = ResponseGateway()
    rendered = gateway.render(
        gateway.context("请给结论"),
        narrative=ResearchNarrativeBundle(
            subject=r"C:\Users\alice\private\portfolio.json",
            headline="先跑 research-plan 后再判断。",
        ),
    )
    assert rendered.safe_fallback_used is True
    assert "alice" not in rendered.text
    assert "portfolio.json" not in rendered.text
    assert "research-plan" not in rendered.text
    assert "研究对象" in rendered.text
    assert rendered.audit.safe_to_send is True


def test_developer_mode_redacts_secret_instead_of_echoing_it() -> None:
    gateway = ResponseGateway()
    rendered = gateway.render(
        gateway.context("请看日志排查接口错误"),
        diagnostics=DeveloperDiagnosticsInput(
            user_impact="Authorization: Bearer [REDACTED_SECRET]",
            failure_class="REMOTE_5XX",
            correlation_id="corr-123",
            next_action="查看本机日志。",
        ),
    )
    assert rendered.mode is ResponseMode.DEVELOPER
    assert "[REDACTED_SECRET]" not in rendered.text
    assert "corr-123" in rendered.text
    assert rendered.audit.safe_to_send is True


def test_locked_fact_fingerprint_fails_closed_on_missing_fact() -> None:
    source = "600519在2026年8月31日的参考价为1400.00元，建议持有 [S1]。"
    locked = extract_fact_fingerprint(
        source,
        known_entities=["600519"],
        known_phrases=[source],
    )
    gateway = ResponseGateway()
    rendered = gateway.render(
        gateway.context("这家公司现在能买吗？"),
        narrative=ResearchNarrativeBundle(
            subject="600519",
            headline="当前更适合持有。",
            locked_facts=locked,
        ),
    )
    assert rendered.safe_fallback_used is True
    assert "1400.00" not in rendered.text


def test_machine_cli_contracts_are_preserved_and_public_commands_are_additive() -> None:
    source_path = PROJECT_ROOT / "src" / "astock" / "research" / "runtime_cli.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    functions = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "public_investor_payload" not in functions["research_investor_view"]
    assert "public_investor_payload" not in functions[
        "research_acquisition_investor_view"
    ]
    assert "investor_view_from_run" in functions["research_investor_view"]
    assert "investor_view_from_acquisition" in functions[
        "research_acquisition_investor_view"
    ]
    assert "public_investor_payload" in functions["research_public_view"]
    assert "public_investor_payload" in functions[
        "research_acquisition_public_view"
    ]


def test_stable_machine_view_schema_has_not_been_replaced() -> None:
    view = InvestorResearchView(
        company_id="600519",
        state=InvestorResearchState.DECISION_READY,
        headline="可以继续分析。",
        next_step="核对估值与风险。",
    )
    assert view.schema_version == "investor-research-view-v1"
    assert set(view.model_dump()) == {
        "schema_version",
        "created_at",
        "company_id",
        "state",
        "headline",
        "plain_language_gaps",
        "next_step",
        "diagnostics_available",
        "internal_codes_exposed",
        "artifact_ids_exposed",
        "paper_ledger_write_count",
        "broker_execution_allowed",
    }


def test_parameterized_contract_exceeds_minimum_and_covers_required_categories() -> None:
    total = sum(
        len(items)
        for items in (
            _NORMAL_INVESTOR_PROMPTS,
            _DEVELOPER_PROMPTS,
            _NEGATED_DIAGNOSTIC_PROMPTS,
            _SYSTEM_ERROR_TEXTS,
            _BAD_STYLE_TEXTS,
            _GOOD_STYLE_TEXTS,
            _FACT_TEXTS,
            _SECRET_OR_PATH_TEXTS,
            _DRIFT_CASES,
        )
    )
    assert total >= 150
    assert len(_NEGATED_DIAGNOSTIC_PROMPTS) >= 10
    assert len(_DRIFT_CASES) >= 10
