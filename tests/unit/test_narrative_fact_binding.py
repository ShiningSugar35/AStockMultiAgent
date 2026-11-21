"""Semantic binding regressions through the actual public gateway."""
from __future__ import annotations

import pytest

from astock.research import presentation
from astock.schemas.presentation import (
    NarrativeFactBinding,
    ResearchNarrativeBundle,
    ResponseContext,
    ResponseTaskType,
)

STATEMENT = "甲公司2026年收入可能为10亿元，若需求下降则不加仓。[S1]"


def binding() -> NarrativeFactBinding:
    return NarrativeFactBinding(
        entity="甲公司", metric="收入", period="2026年", unit="亿元", value="10",
        qualifiers=["可能", "若需求下降则不加仓"], citations=["[S1]"], statement=STATEMENT,
    )


@pytest.mark.parametrize("changed", [
    STATEMENT.replace("甲公司", "乙公司"),
    STATEMENT.replace("2026年", "2025年"),
    STATEMENT.replace("亿元", "万元"),
    STATEMENT.replace("不加仓", "加仓"),
    STATEMENT.replace("可能", "必然"),
    STATEMENT.replace("若需求下降则不加仓", "不加仓会导致需求下降"),
    STATEMENT.replace("[S1]", "[S2]"),
])
def test_bound_meaning_is_not_a_number_set(changed: str) -> None:
    # Preserve every original token elsewhere: the old bag-of-tokens check alone
    # cannot prove that the value still belongs to the right claim.
    audit = presentation.audit_public_answer(
        changed, source_text=STATEMENT + "\n乙公司2025年收入为10万元。[S2]",
        fact_bindings=[binding()],
        context=ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH),
    )
    assert not audit.safe_to_send
    assert audit.fact_drift_detected
    assert "PUBLIC_FACT_BINDING_CHANGED" in audit.finding_codes


def test_gateway_uses_frozen_input_not_editor_output(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。", reasons=[STATEMENT],
        fact_bindings=[binding()], citations=["[S1]"],
    )
    monkeypatch.setattr(
        presentation, "_polish_narrative",
        lambda narrative, **_: narrative.model_copy(
            update={"reasons": [STATEMENT.replace("不加仓", "加仓")]}
        ),
    )
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=original
    )
    assert result.safe_fallback_used
    assert result.audit.safe_to_send
    assert STATEMENT in result.text
    assert "若需求下降则加仓" not in result.text


def test_rendered_reason_swap_is_rejected_at_public_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    narrative = ResearchNarrativeBundle(
        subject="两家公司", headline="继续观察。",
        reasons=["甲公司收入10亿元。", "乙公司收入20亿元。"],
    )
    renderer = presentation._render_investor_text

    def swapped(payload, policy=None):
        return renderer(payload, policy).replace("甲公司收入10", "乙公司收入10").replace(
            "乙公司收入20", "甲公司收入20"
        )

    monkeypatch.setattr(presentation, "_render_investor_text", swapped)
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=narrative
    )
    assert result.safe_fallback_used
    assert "乙公司收入10" not in result.text
    assert "甲公司收入20" not in result.text


def test_binding_cannot_claim_components_absent_from_its_statement() -> None:
    with pytest.raises(ValueError, match="components"):
        binding().model_validate({**binding().model_dump(), "period": "2028年"})

@pytest.mark.parametrize("original, changed", [
    ("甲公司2026年收入可能为10亿元。[S1]", "乙公司2026年收入可能为10亿元。[S1]"),
    ("甲公司2026年收入可能为10亿元。[S1]", "甲公司2025年收入可能为10亿元。[S1]"),
    ("甲公司2026年收入可能为10亿元。[S1]", "甲公司2026年收入可能为10万元。[S1]"),
    ("若需求下降则不加仓。", "若需求下降则加仓。"),
    ("收入可能为10亿元。", "收入必然为10亿元。"),
    ("若需求下降则不加仓。", "不加仓会导致需求下降。"),
    ("甲公司2026年收入可能为10亿元。[S1]", "甲公司2026年收入可能为10亿元。[S2]"),
])
def test_legacy_producer_freezes_source_before_editor(
    monkeypatch: pytest.MonkeyPatch, original: str, changed: str,
) -> None:
    narrative = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。",
        reasons=[original, "乙公司2025年收入可能为20万元。[S2]"],
    )
    assert not narrative.fact_bindings
    monkeypatch.setattr(
        presentation, "_polish_narrative",
        lambda source, **_: source.model_copy(update={"reasons": [changed, source.reasons[1]]}),
    )
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=narrative,
    )
    assert result.safe_fallback_used
    assert result.audit.safe_to_send
    assert original in result.text
    assert changed not in result.text


def test_renderer_cannot_remove_sources_by_inflating_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    narrative = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。", reasons=["若需求下降则不加仓。"],
    )
    renderer = presentation._render_investor_text

    def inflated(payload, policy=None):
        return renderer(payload, policy) + ("说明。" * 5000 if payload.reasons else "")

    monkeypatch.setattr(presentation, "_render_investor_text", inflated)
    gateway = presentation.ResponseGateway()
    context = ResponseContext(task_type=ResponseTaskType.COMPANY_QUICK_VIEW)
    selected = presentation._investor_payload(narrative, context=context, policy=gateway.policy)
    assert selected.reasons == narrative.reasons
    result = gateway.render(context, narrative=narrative)
    assert result.safe_fallback_used or narrative.reasons[0] in result.text


@pytest.mark.parametrize("added", [
    "据此，乙公司在2026年的营业收入记为10亿元。[S1]",
    "甲公司2026年净利润可能为10亿元。[S1]",
    "甲公司2026年收入必然为10亿元。[S1]",
    "乙公司已排除经营风险。",
])
def test_preserved_original_cannot_license_an_added_claim(added: str) -> None:
    audit = presentation.audit_public_answer(
        STATEMENT + "\n" + added, source_text=STATEMENT, fact_bindings=[binding()],
    )
    assert not audit.safe_to_send and audit.fact_drift_detected
    assert "PUBLIC_UNBOUND_STATEMENT" in audit.finding_codes


@pytest.mark.parametrize("typed", [False, True])
def test_gateway_rejects_appended_claim_with_existing_numbers(
    monkeypatch: pytest.MonkeyPatch, typed: bool,
) -> None:
    source = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。", reasons=[STATEMENT],
        fact_bindings=[binding()] if typed else [], citations=["[S1]"],
    )
    added = "乙公司2026年收入为10亿元。[S1]"
    monkeypatch.setattr(
        presentation, "_polish_narrative",
        lambda narrative, **_: narrative.model_copy(update={"risks": [added]}),
    )
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=source,
    )
    assert result.safe_fallback_used and result.audit.safe_to_send
    assert STATEMENT in result.text and added not in result.text


def test_renderer_cannot_append_an_unbound_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。", reasons=[STATEMENT], citations=["[S1]"],
    )
    renderer = presentation._render_investor_text
    added = "乙公司2026年收入为10亿元。[S1]"

    def augmented(payload, policy=None):
        return renderer(payload, policy) + ("\n" + added if payload.reasons else "")

    monkeypatch.setattr(presentation, "_render_investor_text", augmented)
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=source,
    )
    assert result.safe_fallback_used and result.audit.safe_to_send
    assert added not in result.text


def test_frozen_source_allows_existing_layout_and_term_explanations() -> None:
    source = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。",
        reasons=["甲公司2026年DCF估值为10元。[S1]"],
        risks=["现金流可能低于预期。"], change_conditions=["需求持续下滑。"],
        citations=["[S1]"],
    )
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=source,
    )
    assert result.audit.safe_to_send and not result.safe_fallback_used
    assert "DCF指将未来现金流折算成今天的价值" in result.text


@pytest.mark.parametrize("original, changed", [
    ("需求已恢复？", "需求已恢复。"),
    ("管理层声称「需求已恢复」。", "管理层声称需求已恢复。"),
])
def test_legacy_source_keeps_question_and_quote_boundaries(
    monkeypatch: pytest.MonkeyPatch, original: str, changed: str,
) -> None:
    source = ResearchNarrativeBundle(
        subject="甲公司", headline="继续观察。", reasons=[original],
    )
    monkeypatch.setattr(
        presentation, "_polish_narrative",
        lambda narrative, **_: narrative.model_copy(update={"reasons": [changed]}),
    )
    result = presentation.ResponseGateway().render(
        ResponseContext(task_type=ResponseTaskType.DEEP_RESEARCH), narrative=source,
    )
    assert result.safe_fallback_used and result.audit.safe_to_send
    assert original in result.text and changed not in result.text
