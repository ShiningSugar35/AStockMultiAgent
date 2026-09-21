"""Pure bridge from verified discovery theses into the existing ResearchSeed funnel.

This module owns no company facts and writes no Candidate records. It produces
standard ResearchSeed objects with a narrow VERIFIED_DISCOVERY_THESIS marker;
the shared promotion/scan lane may admit that marker as a RESEARCH_SEED_PRIOR
without upgrading legacy keyword-based EXPERT_SKILL seeds.
"""

from __future__ import annotations

from dataclasses import dataclass

from astock.candidates.discovery_theses import (
    DiscoveryThesisResult,
    qualified_company_theses,
)
from astock.core.hashing import content_hash
from astock.schemas.knowledge_completion import KnowledgeConditionState
from astock.schemas.market import Market
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedOrigin

VERIFIED_DISCOVERY_MARKER = "VERIFIED_DISCOVERY_THESIS"


@dataclass(frozen=True)
class DiscoverySeedCompanyContext:
    company_id: str
    market: Market
    name: str
    industry_label: str | None = None
    source_snapshot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.company_id) != 6 or not self.company_id.isdigit():
            raise ValueError("discovery seed context requires a six-digit company id")
        if not self.name.strip():
            raise ValueError("discovery seed context requires a company name")
        if self.source_snapshot_ids != tuple(sorted(set(self.source_snapshot_ids))):
            raise ValueError("discovery seed source snapshots must be sorted unique")


@dataclass(frozen=True)
class DiscoverySeedBudgetResult:
    seeds: tuple[ResearchSeed, ...]
    market_count: int
    long_horizon_value_count: int
    breadth_count: int
    verified_discovery_count: int
    existing_candidate_count: int
    dropped_legacy_expert_only_count: int

    def __post_init__(self) -> None:
        company_ids = [seed.company_id for seed in self.seeds]
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("discovery seed budget contains duplicate companies")


def _verified_results(
    company_id: str,
    results: tuple[DiscoveryThesisResult, ...],
) -> tuple[DiscoveryThesisResult, ...]:
    qualified = qualified_company_theses(results)
    allowed = set(qualified.get(company_id, ()))
    if not allowed:
        return ()
    selected = {
        result.thesis_id: result
        for result in results
        if result.company_id == company_id
        and result.thesis_id in allowed
        and result.state is KnowledgeConditionState.SATISFIED
    }
    return tuple(selected[key] for key in sorted(selected))


def build_verified_discovery_seed(
    *,
    context: DiscoverySeedCompanyContext,
    results: tuple[DiscoveryThesisResult, ...],
    active_registry_hash: str,
    discovery_method_catalog_hash: str,
    research_priority_score: float = 0.75,
) -> ResearchSeed | None:
    """Build one standard ResearchSeed from distinct fully-satisfied hypotheses."""

    verified = _verified_results(context.company_id, results)
    if not verified:
        return None
    if not 0 <= research_priority_score <= 1:
        raise ValueError("discovery research priority must be within 0..1")
    thesis_ids = tuple(result.thesis_id for result in verified)
    channels = tuple(sorted({result.channel.value for result in verified}))
    method_ids = tuple(
        sorted(
            {
                method_id
                for result in verified
                for method_id, _condition_id in result.method_refs
            }
        )
    )
    reason_codes = {
        VERIFIED_DISCOVERY_MARKER,
        *(f"DISCOVERY_THESIS:{thesis_id}" for thesis_id in thesis_ids),
        *(f"DISCOVERY_CHANNEL:{channel}" for channel in channels),
        *(f"DISCOVERY_METHOD:{method_id}" for method_id in method_ids),
    }
    seed_identity = {
        "company_id": context.company_id,
        "market": context.market.value,
        "thesis_ids": thesis_ids,
        "active_registry_hash": active_registry_hash,
        "discovery_method_catalog_hash": discovery_method_catalog_hash,
    }
    return ResearchSeed(
        seed_id=f"research-seed:skill-discovery:{content_hash(seed_identity)}",
        company_id=context.company_id,
        market=context.market,
        name=context.name,
        origins=[ResearchSeedOrigin.EXPERT_SKILL],
        research_priority_score=research_priority_score,
        industry_label=context.industry_label,
        expert_author_source_ids=[],
        expert_domain_names=[f"DISCOVERY_{channel}" for channel in channels],
        expert_domain_support_skill_ids=list(method_ids),
        reason_codes=sorted(reason_codes),
        source_snapshot_ids=list(context.source_snapshot_ids),
    )


def _merge_seed(base: ResearchSeed, overlay: ResearchSeed) -> ResearchSeed:
    if base.company_id != overlay.company_id or base.market is not overlay.market:
        raise ValueError("cannot merge discovery seeds across company or market identities")
    update = {
        "origins": sorted(set(base.origins) | set(overlay.origins), key=lambda item: item.value),
        "research_priority_score": max(
            base.research_priority_score,
            overlay.research_priority_score,
        ),
        "expert_author_source_ids": sorted(
            set(base.expert_author_source_ids) | set(overlay.expert_author_source_ids)
        ),
        "expert_domain_names": sorted(
            set(base.expert_domain_names) | set(overlay.expert_domain_names)
        ),
        "expert_domain_support_skill_ids": sorted(
            set(base.expert_domain_support_skill_ids)
            | set(overlay.expert_domain_support_skill_ids)
        ),
        "reason_codes": sorted(set(base.reason_codes) | set(overlay.reason_codes)),
        "source_snapshot_ids": sorted(
            set(base.source_snapshot_ids) | set(overlay.source_snapshot_ids)
        ),
    }
    return base.model_copy(update=update)


def _deduplicate_base(seeds: tuple[ResearchSeed, ...]) -> list[ResearchSeed]:
    ordered: list[ResearchSeed] = []
    index: dict[str, int] = {}
    for seed in seeds:
        position = index.get(seed.company_id)
        if position is None:
            index[seed.company_id] = len(ordered)
            ordered.append(seed)
        else:
            ordered[position] = _merge_seed(ordered[position], seed)
    return ordered


def _is_legacy_expert_only(seed: ResearchSeed) -> bool:
    return (
        seed.origins == [ResearchSeedOrigin.EXPERT_SKILL]
        and VERIFIED_DISCOVERY_MARKER not in seed.reason_codes
    )


def _is_verified_discovery(seed: ResearchSeed) -> bool:
    return (
        ResearchSeedOrigin.EXPERT_SKILL in seed.origins
        and VERIFIED_DISCOVERY_MARKER in seed.reason_codes
    )


def merge_discovery_seed_tranche(
    base_seeds: tuple[ResearchSeed, ...],
    discovery_seeds: tuple[ResearchSeed, ...],
    *,
    max_total: int,
    max_market: int,
    max_long_horizon_value: int,
    max_breadth: int,
    max_verified_discovery: int,
    max_existing_candidate: int,
) -> DiscoverySeedBudgetResult:
    """Merge verified discovery without replacing the blind tranche or inflating votes."""

    budgets = (
        max_total,
        max_market,
        max_long_horizon_value,
        max_breadth,
        max_verified_discovery,
        max_existing_candidate,
    )
    if min(budgets) < 0:
        raise ValueError("discovery Seed budgets must be non-negative")
    base = _deduplicate_base(base_seeds)
    by_company = {seed.company_id: seed for seed in base}
    base_verified_ids = {
        seed.company_id for seed in base if _is_verified_discovery(seed)
    }

    discovery_by_company: dict[str, ResearchSeed] = {}
    ignored_unverified = 0
    for seed in discovery_seeds:
        if not _is_verified_discovery(seed):
            ignored_unverified += 1
            continue
        existing = discovery_by_company.get(seed.company_id)
        discovery_by_company[seed.company_id] = (
            seed if existing is None else _merge_seed(existing, seed)
        )

    remaining_discovery_slots = max(0, max_verified_discovery - len(base_verified_ids))
    ranked_new_discovery = sorted(
        (
            seed
            for company_id, seed in discovery_by_company.items()
            if company_id not in base_verified_ids
        ),
        key=lambda seed: (-seed.research_priority_score, seed.company_id),
    )
    allowed_new_discovery = ranked_new_discovery[:remaining_discovery_slots]
    allowed_discovery_by_company = {
        seed.company_id: seed for seed in allowed_new_discovery
    }
    for company_id in base_verified_ids:
        overlay = discovery_by_company.get(company_id)
        if overlay is not None:
            allowed_discovery_by_company[company_id] = overlay

    new_discovery: list[ResearchSeed] = []
    for company_id, seed in sorted(allowed_discovery_by_company.items()):
        existing = by_company.get(company_id)
        if existing is not None:
            by_company[company_id] = _merge_seed(existing, seed)
        else:
            new_discovery.append(seed)

    normalized_base = [by_company[seed.company_id] for seed in base]
    selected: list[ResearchSeed] = []
    selected_ids: set[str] = set()

    def take(values: list[ResearchSeed], limit: int) -> None:
        added = 0
        for seed in values:
            if added >= limit or len(selected) >= max_total:
                break
            if seed.company_id in selected_ids:
                continue
            selected.append(seed)
            selected_ids.add(seed.company_id)
            added += 1

    take(
        [seed for seed in normalized_base if ResearchSeedOrigin.MARKET in seed.origins],
        max_market,
    )
    # Promotion currently consumes the first 60 Seeds. Keep the frozen blind market
    # tranche first, then place verified discovery before the remaining non-blind
    # tranches so an evidence-verified discovery seat cannot be structurally pushed
    # outside Promotion@60 by the 80-Seed staging budget.
    discovery_pool = sorted(
        [
            *[seed for seed in normalized_base if _is_verified_discovery(seed)],
            *new_discovery,
        ],
        key=lambda seed: (-seed.research_priority_score, seed.company_id),
    )
    take(discovery_pool, max_verified_discovery)
    take(
        [
            seed
            for seed in normalized_base
            if ResearchSeedOrigin.LONG_HORIZON_VALUE in seed.origins
        ],
        max_long_horizon_value,
    )
    take(
        [
            seed
            for seed in normalized_base
            if ResearchSeedOrigin.BREADTH_CHALLENGER in seed.origins
        ],
        max_breadth,
    )
    take(
        [
            seed
            for seed in normalized_base
            if ResearchSeedOrigin.EXISTING_CANDIDATE in seed.origins
        ],
        max_existing_candidate,
    )

    dropped_legacy = ignored_unverified + sum(
        _is_legacy_expert_only(seed) and seed.company_id not in selected_ids
        for seed in normalized_base
    )
    return DiscoverySeedBudgetResult(
        seeds=tuple(selected),
        market_count=sum(ResearchSeedOrigin.MARKET in seed.origins for seed in selected),
        long_horizon_value_count=sum(
            ResearchSeedOrigin.LONG_HORIZON_VALUE in seed.origins for seed in selected
        ),
        breadth_count=sum(
            ResearchSeedOrigin.BREADTH_CHALLENGER in seed.origins for seed in selected
        ),
        verified_discovery_count=sum(_is_verified_discovery(seed) for seed in selected),
        existing_candidate_count=sum(
            ResearchSeedOrigin.EXISTING_CANDIDATE in seed.origins for seed in selected
        ),
        dropped_legacy_expert_only_count=dropped_legacy,
    )


__all__ = [
    "DiscoverySeedBudgetResult",
    "DiscoverySeedCompanyContext",
    "VERIFIED_DISCOVERY_MARKER",
    "build_verified_discovery_seed",
    "merge_discovery_seed_tranche",
]
