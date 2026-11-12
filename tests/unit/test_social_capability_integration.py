"""Regression tests for optional social sentiment research across investor intents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.investor_orchestration.capabilities import _BASE_NODES, CapabilityPlanner
from astock.investor_orchestration.domain_contracts import DomainContractAudit
from astock.investor_orchestration.models import (
    CapabilityRequirement,
    InvestorRequestEnvelope,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier, output_model
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.social_sources.analysis import summarize_social_research
from astock.social_sources.models import (
    SocialLead,
    SocialPlatform,
    SocialPlatformCoverage,
    SocialQueryKind,
    SocialSearchResult,
)


@pytest.mark.parametrize(
    ("intent", "side_effect", "expected"),
    [
        (RequestIntent.RESEARCH, SideEffectClass.READ, CapabilityRequirement.CONDITIONAL),
        (RequestIntent.FULL_RESEARCH_RECOMMENDATION, SideEffectClass.READ,
         CapabilityRequirement.CONDITIONAL),
        (RequestIntent.MONITOR, SideEffectClass.READ, CapabilityRequirement.CONDITIONAL),
        (RequestIntent.PAPER_PREPARE, SideEffectClass.PT_PREPARE,
         CapabilityRequirement.CONDITIONAL),
        (RequestIntent.PAPER_CONFIRM, SideEffectClass.PT_CONFIRM,
         CapabilityRequirement.CONDITIONAL),
        (RequestIntent.PAPER_STATUS, SideEffectClass.READ,
         CapabilityRequirement.CONDITIONAL),
        (RequestIntent.ACCOUNT_FACT_WRITE, SideEffectClass.EA_WRITE, None),
    ],
)
def test_all_investment_intents_route_social_without_new_economic_authority(
    tmp_path: Path,
    intent: RequestIntent,
    side_effect: SideEffectClass,
    expected: CapabilityRequirement | None,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    request = InvestorRequestEnvelope(
        request_id=f"social-{intent.value}",
        question_time=datetime(2026, 11, 10, 9, 15, tzinfo=UTC),
        user_timezone="Asia/Shanghai",
        raw_text="立讯精密投资和风险分析",
        normalized_intent=intent,
        side_effect=side_effect,
        entity_ids=("XSHE:002475",),
        idempotency_key=f"social:{intent.value}",
    )
    preflight = InvestorSessionPreflightService(store).build(request)
    plan = CapabilityPlanner().plan(request, preflight)
    nodes = {node.capability_id: node for node in plan.nodes}
    social = nodes.get("SOCIAL_SENTIMENT")
    if expected is None:
        assert social is None
    else:
        assert social is not None
        assert social.requirement is expected
        assert social.output_schema == "SocialSearchResult"
        assert social.side_effect is SideEffectClass.READ
        assert "ENTITY_IDENTITY" in social.dependencies
        assert "SOCIAL_SENTIMENT" not in nodes.get(
            "FULL_RESEARCH_GATE", social
        ).dependencies
    assert output_model("SocialSearchResult") is SocialSearchResult


@pytest.mark.parametrize("symbol", ["600521", ""])
def test_social_domain_rejects_a_registered_other_issuer_result(
    tmp_path: Path, symbol: str,
) -> None:
    """A valid source hash cannot authorize community posts for a different issuer."""
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    now = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    request = InvestorRequestEnvelope(
        request_id="social-identity-negative",
        question_time=now,
        user_timezone="Asia/Shanghai",
        raw_text="分析立讯精密",
        normalized_intent=RequestIntent.RESEARCH,
        side_effect=SideEffectClass.READ,
        entity_ids=("XSHE:002475",),
        idempotency_key="social:identity-negative",
    )
    lead = SocialLead(
        lead_id="zhihu:other-company",
        platform=SocialPlatform.ZHIHU,
        symbol=symbol,
        company_id="600521",
        company_name="华海药业",
        url="https://www.zhihu.com/question/100/answer/200",
        title="华海药业近期经营讨论",
        text="这是一条真实来源但不属于本次标的的社区线索。",
        published_at=now - timedelta(hours=1),
        captured_at=now,
        snapshot_id="snapshot:other",
    )
    result = SocialSearchResult(
        result_artifact_id="SocialSearchResult:other",
        receipt_id="SocialSearchReceipt:other",
        snapshot_id="snapshot:other",
        snapshot_ids=("snapshot:other",),
        leads=(lead,),
        coverage=(SocialPlatformCoverage(
            platform=SocialPlatform.ZHIHU,
            start=now - timedelta(days=7),
            end=now,
            query_count=1,
            completed_query_count=1,
            raw_item_count=1,
            accepted_count=1,
            filtered_time_count=0,
            filtered_content_count=0,
            duplicate_count=0,
            complete=True,
            checked_through=now,
            snapshot_ids=("snapshot:other",),
        ),),
        complete=True,
    )
    with pytest.raises(ValueError, match="another security"):
        DomainContractAudit(RegisteredOutputVerifier(store)).check(
            _BASE_NODES["SOCIAL_SENTIMENT"],
            result.result_artifact_id,
            result,
            request,
        )


def test_investor_summary_keeps_source_coverage_and_grouped_arguments() -> None:
    now = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    positives = SocialLead(
        lead_id="xueqiu:1",
        platform=SocialPlatform.XUEQIU,
        symbol="002475", company_id="002475", company_name="立讯精密",
        url="https://xueqiu.com/123/1",
        title="立讯精密新订单增长",
        text="立讯精密新订单增长，但仍需核实公司披露。",
        published_at=now - timedelta(hours=2),
        captured_at=now, matched_queries=("立讯精密",),
        query_kinds=(SocialQueryKind.COMPANY,),
        sentiment_hint="POSITIVE_HINT",
        snapshot_id="snapshot:s1",
    )
    negatives = positives.model_copy(update={
        "lead_id": "zhihu:1", "platform": SocialPlatform.ZHIHU,
        "url": "https://www.zhihu.com/question/1/answer/2",
        "title": "立讯精密减持风险", "text": "立讯精密减持风险待核实。",
        "sentiment_hint": "NEGATIVE_HINT", "snapshot_id": "snapshot:s2",
    })
    coverage = tuple(SocialPlatformCoverage(
        platform=platform,
        start=now - timedelta(days=7), end=now,
        query_count=1, completed_query_count=1,
        raw_item_count=1, accepted_count=1,
        filtered_time_count=0, filtered_content_count=0,
        duplicate_count=0, complete=True, checked_through=now,
        snapshot_ids=(f"snapshot:s{i}",),
    ) for i, platform in enumerate(SocialPlatform, start=1))
    result = SocialSearchResult(
        result_artifact_id="SocialSearchResult:test",
        receipt_id="SocialSearchReceipt:test",
        snapshot_id="snapshot:result",
        snapshot_ids=("snapshot:s1", "snapshot:s2"),
        leads=(positives, negatives), coverage=coverage, complete=True,
    )
    report = summarize_social_research(result)
    assert report["complete"] is True
    assert report["discussion_groups"]["positive_discussion"]["count"] == 1
    assert report["discussion_groups"]["negative_or_risk_discussion"]["count"] == 1
    assert report["formal_investment_authority"] is False
    assert len(report["channels"]) == 2
    assert report["source_result_artifact_id"] == result.result_artifact_id
