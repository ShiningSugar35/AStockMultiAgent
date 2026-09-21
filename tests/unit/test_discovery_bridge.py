from __future__ import annotations

from astock.candidates.discovery_bridge import (
    VERIFIED_DISCOVERY_MARKER,
    DiscoverySeedCompanyContext,
    build_verified_discovery_seed,
    merge_discovery_seed_tranche,
)
from astock.candidates.discovery_theses import (
    DiscoveryChannel,
    DiscoveryThesisResult,
)
from astock.schemas.knowledge_completion import KnowledgeConditionState
from astock.schemas.market import Market
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedOrigin


def _result(
    company_id: str,
    thesis_id: str,
    *,
    state: KnowledgeConditionState = KnowledgeConditionState.SATISFIED,
    channel: DiscoveryChannel = DiscoveryChannel.EVENT,
    method_id: str = "EventToAlphaSkill",
) -> DiscoveryThesisResult:
    return DiscoveryThesisResult(
        company_id=company_id,
        family_id=f"family:{thesis_id}",
        channel=channel,
        thesis_id=thesis_id,
        state=state,
        conditions=(),
        method_refs=((method_id, f"condition:{thesis_id}"),),
        satisfied_families=(f"economic:{thesis_id}",)
        if state is KnowledgeConditionState.SATISFIED
        else (),
        dependency_fingerprint=("d" if state is KnowledgeConditionState.SATISFIED else "e") * 64,
    )


def _base_seed(
    company_id: str,
    origin: ResearchSeedOrigin,
    *,
    priority: float = 0.5,
    marker: bool = False,
) -> ResearchSeed:
    reasons = [f"BASE:{origin.value}"]
    if marker:
        reasons.append(VERIFIED_DISCOVERY_MARKER)
    return ResearchSeed(
        seed_id=f"seed:{company_id}:{origin.value}",
        company_id=company_id,
        market=Market.XSHG,
        name=f"Company {company_id}",
        origins=[origin],
        research_priority_score=priority,
        reason_codes=sorted(reasons),
    )


def _discovery_seed(company_id: str, *, priority: float = 0.75) -> ResearchSeed:
    return ResearchSeed(
        seed_id=f"discovery:{company_id}",
        company_id=company_id,
        market=Market.XSHG,
        name=f"Discovery {company_id}",
        origins=[ResearchSeedOrigin.EXPERT_SKILL],
        research_priority_score=priority,
        expert_domain_names=["DISCOVERY_EVENT"],
        expert_domain_support_skill_ids=["EventToAlphaSkill"],
        reason_codes=[
            "DISCOVERY_CHANNEL:EVENT",
            "DISCOVERY_METHOD:EventToAlphaSkill",
            f"DISCOVERY_THESIS:thesis:{company_id}",
            VERIFIED_DISCOVERY_MARKER,
        ],
    )


def _merge(
    base: tuple[ResearchSeed, ...],
    discovery: tuple[ResearchSeed, ...],
    **overrides: int,
):
    budgets = {
        "max_total": 80,
        "max_market": 40,
        "max_long_horizon_value": 16,
        "max_breadth": 12,
        "max_verified_discovery": 8,
        "max_existing_candidate": 4,
    }
    budgets.update(overrides)
    return merge_discovery_seed_tranche(base, discovery, **budgets)


def test_only_fully_satisfied_distinct_hypotheses_create_a_seed() -> None:
    context = DiscoverySeedCompanyContext(
        company_id="600001",
        market=Market.XSHG,
        name="Example",
        source_snapshot_ids=("snapshot:a",),
    )
    first = _result("600001", "thesis:a")
    duplicate = _result("600001", "thesis:a")
    second = _result(
        "600001",
        "thesis:b",
        channel=DiscoveryChannel.BOTTLENECK,
        method_id="IndustryBottleneckSkill",
    )
    seed = build_verified_discovery_seed(
        context=context,
        results=(first, duplicate, second),
        active_registry_hash="a" * 64,
        discovery_method_catalog_hash="b" * 64,
    )

    assert seed is not None
    assert seed.origins == [ResearchSeedOrigin.EXPERT_SKILL]
    assert VERIFIED_DISCOVERY_MARKER in seed.reason_codes
    assert sum(code.startswith("DISCOVERY_THESIS:") for code in seed.reason_codes) == 2
    assert seed.expert_domain_support_skill_ids == [
        "EventToAlphaSkill",
        "IndustryBottleneckSkill",
    ]
    assert seed.source_snapshot_ids == ["snapshot:a"]


def test_unknown_not_satisfied_or_conflicting_thesis_never_creates_verified_seed() -> None:
    context = DiscoverySeedCompanyContext(
        company_id="600001",
        market=Market.XSHG,
        name="Example",
    )
    for state in (
        KnowledgeConditionState.UNKNOWN,
        KnowledgeConditionState.NOT_SATISFIED,
        KnowledgeConditionState.NOT_APPLICABLE,
    ):
        assert (
            build_verified_discovery_seed(
                context=context,
                results=(_result("600001", "thesis:a", state=state),),
                active_registry_hash="a" * 64,
                discovery_method_catalog_hash="b" * 64,
            )
            is None
        )

    assert (
        build_verified_discovery_seed(
            context=context,
            results=(
                _result("600001", "thesis:a"),
                _result(
                    "600001",
                    "thesis:a",
                    state=KnowledgeConditionState.UNKNOWN,
                ),
            ),
            active_registry_hash="a" * 64,
            discovery_method_catalog_hash="b" * 64,
        )
        is None
    )


def test_seed_budget_preserves_all_channel_caps_and_drops_legacy_expert_only() -> None:
    market = tuple(
        _base_seed(f"600{i:03d}", ResearchSeedOrigin.MARKET)
        for i in range(41)
    )
    value = tuple(
        _base_seed(f"601{i:03d}", ResearchSeedOrigin.LONG_HORIZON_VALUE)
        for i in range(17)
    )
    breadth = tuple(
        _base_seed(f"602{i:03d}", ResearchSeedOrigin.BREADTH_CHALLENGER)
        for i in range(13)
    )
    existing = tuple(
        _base_seed(f"603{i:03d}", ResearchSeedOrigin.EXISTING_CANDIDATE)
        for i in range(5)
    )
    legacy_expert = (
        _base_seed("605000", ResearchSeedOrigin.EXPERT_SKILL),
    )
    discovery = tuple(
        _discovery_seed(f"604{i:03d}", priority=0.90 - i / 100)
        for i in range(10)
    )

    result = _merge(
        (*market, *value, *breadth, *existing, *legacy_expert),
        discovery,
    )

    assert len(result.seeds) == 80
    assert result.market_count == 40
    assert result.long_horizon_value_count == 16
    assert result.breadth_count == 12
    assert result.verified_discovery_count == 8
    assert result.existing_candidate_count == 4
    assert result.dropped_legacy_expert_only_count == 1
    assert "605000" not in {seed.company_id for seed in result.seeds}
    assert "600040" not in {seed.company_id for seed in result.seeds}
    assert "601016" not in {seed.company_id for seed in result.seeds}
    assert "602012" not in {seed.company_id for seed in result.seeds}
    assert "603004" not in {seed.company_id for seed in result.seeds}
    assert all(ResearchSeedOrigin.MARKET in seed.origins for seed in result.seeds[:40])
    promotion_ids = {seed.company_id for seed in result.seeds[:60]}
    assert {f"604{i:03d}" for i in range(8)} <= promotion_ids


def test_discovery_overlap_consumes_one_discovery_budget_but_not_an_extra_seed_seat() -> None:
    base = tuple(
        _base_seed(f"600{i:03d}", ResearchSeedOrigin.MARKET)
        for i in range(40)
    )
    discovery = (
        _discovery_seed("600000", priority=0.99),
        *tuple(
            _discovery_seed(f"604{i:03d}", priority=0.90 - i / 100)
            for i in range(8)
        ),
    )

    result = _merge(
        base,
        discovery,
        max_long_horizon_value=0,
        max_breadth=0,
        max_existing_candidate=0,
    )

    assert result.market_count == 40
    assert result.verified_discovery_count == 8
    assert len(result.seeds) == 47
    overlap = next(seed for seed in result.seeds if seed.company_id == "600000")
    assert ResearchSeedOrigin.MARKET in overlap.origins
    assert ResearchSeedOrigin.EXPERT_SKILL in overlap.origins
    assert VERIFIED_DISCOVERY_MARKER in overlap.reason_codes


def test_base_verified_discovery_over_budget_is_trimmed_not_request_blocking() -> None:
    base = tuple(_discovery_seed(f"604{i:03d}") for i in range(9))

    result = _merge(
        base,
        (),
        max_market=0,
        max_long_horizon_value=0,
        max_breadth=0,
        max_existing_candidate=0,
    )

    assert result.verified_discovery_count == 8
    assert len(result.seeds) == 8


def test_unverified_expert_seed_is_ignored_without_blocking_verified_tranche() -> None:
    unverified = _base_seed("605001", ResearchSeedOrigin.EXPERT_SKILL)
    verified = _discovery_seed("604001")

    result = _merge(
        (),
        (unverified, verified),
        max_market=0,
        max_long_horizon_value=0,
        max_breadth=0,
        max_existing_candidate=0,
    )

    assert [seed.company_id for seed in result.seeds] == ["604001"]
    assert result.verified_discovery_count == 1
    assert result.dropped_legacy_expert_only_count == 1
