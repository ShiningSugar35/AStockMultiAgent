"""Low-cost research seeds from market snapshots, existing candidates, and expert Skills."""

from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import log1p
from pathlib import Path
from typing import Any, Protocol

import yaml

from astock.candidates.repository import CandidateRepository
from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.visual_skill_repository import VisualSkillRepository
from astock.schemas.market import Market
from astock.schemas.research_seeds import (
    ExpertDomainEvidence,
    ExpertDomainProfile,
    ResearchSeed,
    ResearchSeedOrigin,
    ResearchSeedReport,
    ResearchSeedRequest,
    ResearchSeedStatus,
)

_CURRENT_LIVE_TOLERANCE = timedelta(minutes=15)
_EQUITY_MARKETS = (Market.XSHG, Market.XSHE, Market.BJSE)


class SeedMarketProvider(Protocol):
    def fetch_seed_snapshot(
        self, market: Market, *, live: bool = False
    ) -> tuple[dict[str, object], Any]: ...

    def fetch_industry_boards(self, *, live: bool = False) -> tuple[dict[str, object], Any]: ...

    def fetch_industry_constituents(
        self, board_code: str, *, live: bool = False
    ) -> tuple[dict[str, object], Any]: ...


@dataclass(frozen=True, slots=True)
class _RawMarketRow:
    company_id: str
    market: Market
    name: str
    price: float
    amount_cny: float
    turnover_rate: float
    float_market_cap_cny: float
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class _MarketRow:
    company_id: str
    market: Market
    name: str
    price: float
    amount_cny: float
    turnover_rate: float
    float_market_cap_cny: float
    market_score: float
    snapshot_id: str


@dataclass(slots=True)
class _SeedAccumulator:
    company_id: str
    market: Market
    name: str
    priority: float = 0.0
    market_score: float | None = None
    price: float | None = None
    amount_cny: float | None = None
    turnover_rate: float | None = None
    float_market_cap_cny: float | None = None
    candidate_version_id: str | None = None
    candidate_strength: str | None = None
    origins: set[ResearchSeedOrigin] = field(default_factory=set)
    authors: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    support_skills: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    snapshot_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _DomainAliasGroup:
    board_contains: tuple[str, ...]
    skill_terms: tuple[str, ...]


class ResearchSeedService:
    """Build research-only seeds without creating candidates, decisions, or orders."""

    def __init__(
        self,
        *,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        provider: SeedMarketProvider,
    ) -> None:
        self.project_root = project_root
        self.state = state
        self.objects = objects
        self.provider = provider
        self.candidates = CandidateRepository(state)
        self.visual_skills = VisualSkillRepository(state)
        self.alias_groups = self._load_alias_groups(
            project_root / "configs" / "research_seed_domains.yaml"
        )
        self.author_names = self._load_author_names(
            project_root / "configs" / "knowledge_sources.yaml"
        )

    def generate(self, request: ResearchSeedRequest) -> ResearchSeedReport:
        if request.live and not self._is_current(request.as_of):
            return self._persist(
                self._empty_report(
                    request,
                    warnings=["LIVE_RESEARCH_SEEDS_REQUIRE_CURRENT_AS_OF"],
                )
            )

        warnings: set[str] = set()
        source_snapshots: dict[str, str] = {}
        source_hashes: set[str] = set()
        cutoff = request.as_of
        market_rows: dict[str, _MarketRow] = {}
        accumulators: dict[str, _SeedAccumulator] = {}

        def fetch_market(market: Market) -> tuple[Market, Any, list[_MarketRow]]:
            payload, snapshot = self.provider.fetch_seed_snapshot(market, live=request.live)
            raw = self._parse_market_rows(payload, market, snapshot.snapshot_id)
            scored = self._score_market_rows(
                raw,
                minimum_amount=request.minimum_amount_cny,
                minimum_float_cap=request.minimum_float_market_cap_cny,
            )
            return market, snapshot, scored

        workers = min(request.market_fetch_workers, len(_EQUITY_MARKETS))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_market, market): market for market in _EQUITY_MARKETS}
            for future in as_completed(futures):
                market = futures[future]
                try:
                    _, snapshot, scored = future.result()
                    cutoff = max(cutoff, snapshot.available_to_system_at)
                    source_snapshots[snapshot.snapshot_id] = snapshot.object_sha256
                    source_hashes.add(snapshot.object_sha256)
                    market_rows.update({item.company_id: item for item in scored})
                except (AStockError, OSError, RuntimeError, ValueError):
                    warnings.add(f"MARKET_SEED_SNAPSHOT_UNAVAILABLE:{market.value}")

        for row in sorted(
            market_rows.values(),
            key=lambda item: (-item.market_score, item.company_id),
        )[: request.max_market_seeds]:
            accumulator = self._accumulator(accumulators, row)
            accumulator.origins.add(ResearchSeedOrigin.MARKET)
            accumulator.priority = max(accumulator.priority, 0.60 + 0.25 * row.market_score)
            accumulator.reasons.add("LIQUID_SCALE_RESEARCH_SEED")
            accumulator.snapshot_ids.add(row.snapshot_id)

        if request.include_existing_candidates:
            for row in self.candidates.research_ready_records(
                as_of=cutoff,
                limit=request.max_total_seeds,
            ):
                company_id = str(row["company_id"])
                market_row = market_rows.get(company_id)
                market = (
                    market_row.market
                    if market_row is not None
                    else self._market_from_instrument_id(str(row["instrument_id"]))
                )
                name = market_row.name if market_row is not None else company_id
                accumulator = accumulators.setdefault(
                    company_id,
                    _SeedAccumulator(company_id=company_id, market=market, name=name),
                )
                if market_row is not None:
                    self._apply_market(accumulator, market_row)
                strength = str(row["strength"])
                accumulator.origins.add(ResearchSeedOrigin.EXISTING_CANDIDATE)
                accumulator.candidate_version_id = str(row["candidate_version_id"])
                accumulator.candidate_strength = strength
                accumulator.priority = max(
                    accumulator.priority,
                    0.99 if strength == "STRONG" else 0.94,
                )
                accumulator.reasons.add("EXISTING_RESEARCH_READY_CANDIDATE")
                record_hash = str(row["record_object_hash"])
                if self.objects.verify(record_hash):
                    source_hashes.add(record_hash)

        release = self.visual_skills.latest_release_any()
        profiles: list[ExpertDomainProfile] = []
        if release is None:
            warnings.add("EXPERT_SKILL_REGISTRY_UNAVAILABLE")
        else:
            release_hash = str(release["release_object_hash"])
            release_id = str(release["release_id"])
            if not self.objects.verify(release_hash):
                warnings.add("EXPERT_SKILL_REGISTRY_OBJECT_UNAVAILABLE")
                release = None
            else:
                source_hashes.add(release_hash)
                try:
                    board_payload, board_snapshot = self.provider.fetch_industry_boards(
                        live=request.live
                    )
                    cutoff = max(cutoff, board_snapshot.available_to_system_at)
                    source_snapshots[board_snapshot.snapshot_id] = board_snapshot.object_sha256
                    source_hashes.add(board_snapshot.object_sha256)
                    boards = self._parse_boards(board_payload)
                    profiles = self._expert_profiles(
                        release_id=release_id,
                        rows=self.visual_skills.overlay_skill_rows(release_id),
                        boards=boards,
                        request=request,
                    )
                    self._apply_expert_seeds(
                        profiles=profiles,
                        market_rows=market_rows,
                        accumulators=accumulators,
                        request=request,
                        source_snapshots=source_snapshots,
                        source_hashes=source_hashes,
                    )
                except (AStockError, OSError, RuntimeError, ValueError):
                    warnings.add("EXPERT_DOMAIN_MARKET_TAXONOMY_UNAVAILABLE")

        all_seeds = [self._finalize_seed(item, request.as_of) for item in accumulators.values()]
        all_seeds.sort(key=lambda item: (-item.research_priority_score, item.company_id))
        blind = [item for item in all_seeds if ResearchSeedOrigin.MARKET in item.origins][
            : min(request.max_market_seeds, request.max_total_seeds)
        ]
        selected_ids = {item.seed_id for item in blind}
        fill = [item for item in all_seeds if item.seed_id not in selected_ids]
        seeds = [*blind, *fill[: max(0, request.max_total_seeds - len(blind))]]
        seeds.sort(key=lambda item: (-item.research_priority_score, item.company_id))
        status = ResearchSeedStatus.READY if seeds else ResearchSeedStatus.NEEDS_INFO
        if not market_rows:
            warnings.add("CURRENT_MARKET_SEED_UNIVERSE_UNAVAILABLE")
        if release is not None and not profiles:
            warnings.add("NO_EXPERT_DOMAIN_PASSED_SKILL_COUNT_GATE")

        report_id = "research-seeds:" + content_hash(
            {
                "request": request.model_dump(mode="json", exclude={"created_at"}),
                "data_cutoff_at": cutoff.isoformat(),
                "registry_release_id": str(release["release_id"]) if release else None,
                "source_hashes": sorted(source_hashes),
                "seed_ids": [item.seed_id for item in seeds],
                "profiles": [item.model_dump(mode="json") for item in profiles],
            }
        )
        report = ResearchSeedReport(
            report_id=report_id,
            as_of=request.as_of,
            data_cutoff_at=cutoff,
            status=status,
            registry_release_id=str(release["release_id"]) if release else None,
            registry_release_object_hash=(str(release["release_object_hash"]) if release else None),
            profiles=profiles,
            seeds=seeds,
            source_snapshot_ids=sorted(source_snapshots),
            source_object_hashes=sorted(source_hashes),
            warning_codes=sorted(warnings),
            market_seed_count=sum(ResearchSeedOrigin.MARKET in item.origins for item in seeds),
            expert_seed_count=sum(
                ResearchSeedOrigin.EXPERT_SKILL in item.origins for item in seeds
            ),
            existing_candidate_seed_count=sum(
                ResearchSeedOrigin.EXISTING_CANDIDATE in item.origins for item in seeds
            ),
            created_at=request.as_of,
        )
        return self._persist(report)

    def status(self) -> dict[str, object]:
        checkpoint = self.state.get_checkpoint("research-seeds", "latest")
        if checkpoint is None:
            return {"status": "NOT_RUN"}
        return {
            "status": checkpoint["status"],
            "artifact_id": checkpoint["cursor"].get("artifact_id"),
            "report_id": checkpoint["cursor"].get("report_id"),
            "object_hash": checkpoint.get("object_hash"),
        }

    def audit(self, artifact_id: str) -> dict[str, object]:
        findings: set[str] = set()
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "ResearchSeedReport":
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_RESEARCH_SEED_REPORT"],
            }
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            findings.add("RESEARCH_SEED_REPORT_OBJECT_UNAVAILABLE")
            report = None
        else:
            report = ResearchSeedReport.model_validate_json(self.objects.get_bytes(object_hash))
        if report is not None:
            for snapshot_id in report.source_snapshot_ids:
                snapshot = self.state.get_snapshot(snapshot_id)
                if snapshot is None:
                    findings.add("RESEARCH_SEED_SOURCE_SNAPSHOT_MISSING")
                    continue
                if (
                    snapshot.object_sha256 not in report.source_object_hashes
                    or not self.objects.verify(snapshot.object_sha256)
                ):
                    findings.add("RESEARCH_SEED_SOURCE_SNAPSHOT_DRIFT")
            if report.registry_release_id is not None:
                release = self.visual_skills.release(report.registry_release_id)
                if (
                    release is None
                    or str(release["release_object_hash"]) != report.registry_release_object_hash
                ):
                    findings.add("RESEARCH_SEED_REGISTRY_RELEASE_DRIFT")
            profile_authors = {item.author_source_id for item in report.profiles}
            profile_domains = {
                domain.board_name for profile in report.profiles for domain in profile.domains
            }
            profile_skill_ids = {
                skill_id
                for profile in report.profiles
                for domain in profile.domains
                for skill_id in domain.support_skill_ids
            }
            report_snapshot_ids = set(report.source_snapshot_ids)
            for seed in report.seeds:
                if not set(seed.source_snapshot_ids).issubset(report_snapshot_ids):
                    findings.add("RESEARCH_SEED_SNAPSHOT_LINEAGE_DRIFT")
                if ResearchSeedOrigin.EXPERT_SKILL in seed.origins:
                    if not set(seed.expert_author_source_ids).issubset(profile_authors):
                        findings.add("RESEARCH_SEED_EXPERT_AUTHOR_DRIFT")
                    if not set(seed.expert_domain_names).issubset(profile_domains):
                        findings.add("RESEARCH_SEED_EXPERT_DOMAIN_DRIFT")
                    if not set(seed.expert_domain_support_skill_ids).issubset(profile_skill_ids):
                        findings.add("RESEARCH_SEED_EXPERT_SKILL_DRIFT")
                if ResearchSeedOrigin.EXISTING_CANDIDATE in seed.origins:
                    if seed.candidate_version_id is None:
                        findings.add("RESEARCH_SEED_CANDIDATE_LINEAGE_MISSING")
                    else:
                        candidate = self.candidates.get_candidate_version(seed.candidate_version_id)
                        if (
                            candidate is None
                            or str(candidate["company_id"]) != seed.company_id
                            or str(candidate["lifecycle_status"]) != "RESEARCH_READY"
                            or str(candidate["record_object_hash"])
                            not in report.source_object_hashes
                        ):
                            findings.add("RESEARCH_SEED_CANDIDATE_LINEAGE_DRIFT")
            if report.recommendation_allowed or report.paper_ledger_write_allowed:
                findings.add("RESEARCH_SEED_AUTHORITY_DRIFT")
        for input_hash in record["input_hashes"]:
            if not self.objects.verify(str(input_hash)):
                findings.add("RESEARCH_SEED_INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "object_hash": object_hash,
            "finding_codes": sorted(findings),
            "recommendation_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    def _apply_expert_seeds(
        self,
        *,
        profiles: list[ExpertDomainProfile],
        market_rows: dict[str, _MarketRow],
        accumulators: dict[str, _SeedAccumulator],
        request: ResearchSeedRequest,
        source_snapshots: dict[str, str],
        source_hashes: set[str],
    ) -> None:
        constituent_cache: dict[str, tuple[list[dict[str, object]], str]] = {}
        for profile in profiles:
            candidates: dict[str, tuple[float, ExpertDomainEvidence, str]] = {}
            max_count = max((item.matched_skill_count for item in profile.domains), default=1)
            for domain in profile.domains:
                if domain.board_code not in constituent_cache:
                    payload, snapshot = self.provider.fetch_industry_constituents(
                        domain.board_code,
                        live=request.live,
                    )
                    source_snapshots[snapshot.snapshot_id] = snapshot.object_sha256
                    source_hashes.add(snapshot.object_sha256)
                    constituent_cache[domain.board_code] = (
                        self._constituent_rows(payload, domain.board_code),
                        snapshot.snapshot_id,
                    )
                rows, snapshot_id = constituent_cache[domain.board_code]
                domain_strength = min(1.0, domain.matched_skill_count / max_count)
                for raw in rows:
                    company_id = str(raw.get("f12") or "")
                    market_row = market_rows.get(company_id)
                    if market_row is None:
                        continue
                    if (
                        market_row.amount_cny < request.minimum_amount_cny
                        or market_row.float_market_cap_cny < request.minimum_float_market_cap_cny
                    ):
                        continue
                    blind_score = 0.60 + 0.25 * market_row.market_score
                    score = min(
                        1.0,
                        blind_score
                        + request.expert_overlay_max_priority_bonus * domain_strength,
                    )
                    previous = candidates.get(company_id)
                    if previous is None or score > previous[0]:
                        candidates[company_id] = (score, domain, snapshot_id)
            ranked = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))[
                : request.max_expert_seeds_per_author
            ]
            for company_id, (score, domain, snapshot_id) in ranked:
                market_row = market_rows[company_id]
                accumulator = self._accumulator(accumulators, market_row)
                accumulator.origins.add(ResearchSeedOrigin.EXPERT_SKILL)
                accumulator.priority = max(accumulator.priority, score)
                accumulator.authors.add(profile.author_source_id)
                accumulator.domains.add(domain.board_name)
                accumulator.support_skills.update(domain.support_skill_ids)
                accumulator.reasons.add("EXPERT_SKILL_DOMAIN_RESEARCH_SEED")
                accumulator.snapshot_ids.update({market_row.snapshot_id, snapshot_id})

    def _expert_profiles(
        self,
        *,
        release_id: str,
        rows: list[dict[str, Any]],
        boards: list[tuple[str, str]],
        request: ResearchSeedRequest,
    ) -> list[ExpertDomainProfile]:
        skills_by_author: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in rows:
            skill = json.loads(str(row["skill_json"]))
            author = str(skill["author_source_id"])
            text = self._skill_text(skill)
            skills_by_author[author].append((str(skill["final_skill_id"]), text))
        profiles: list[ExpertDomainProfile] = []
        for author, skills in sorted(skills_by_author.items()):
            domains: list[ExpertDomainEvidence] = []
            for board_code, board_name in boards:
                terms = self._terms_for_board(board_name)
                support = sorted(
                    skill_id
                    for skill_id, text in skills
                    if any(term and term in text for term in terms)
                )
                share = len(support) / len(skills)
                if len(support) < request.minimum_domain_skill_count:
                    continue
                domains.append(
                    ExpertDomainEvidence(
                        board_code=board_code,
                        board_name=board_name,
                        matched_skill_count=len(support),
                        author_skill_count=len(skills),
                        skill_share=share,
                        support_skill_ids=support[:30],
                        created_at=request.as_of,
                    )
                )
            domains.sort(
                key=lambda item: (-item.matched_skill_count, -item.skill_share, item.board_name)
            )
            deduplicated: list[ExpertDomainEvidence] = []
            support_signatures: set[tuple[str, ...]] = set()
            for domain in domains:
                signature = tuple(domain.support_skill_ids)
                if signature in support_signatures:
                    continue
                support_signatures.add(signature)
                deduplicated.append(domain)
                if len(deduplicated) >= request.max_domains_per_author:
                    break
            domains = deduplicated
            confidence = min(1.0, sum(item.skill_share for item in domains)) if domains else 0.0
            profiles.append(
                ExpertDomainProfile(
                    author_source_id=author,
                    display_name=self.author_names.get(author, author),
                    registry_release_id=release_id,
                    total_admitted_skill_count=len(skills),
                    domains=domains,
                    profile_confidence=confidence,
                    created_at=request.as_of,
                )
            )
        return profiles

    def _parse_market_rows(
        self,
        payload: dict[str, object],
        market: Market,
        snapshot_id: str,
    ) -> list[_RawMarketRow]:
        request = payload.get("_astock_request")
        if not isinstance(request, dict) or request.get("market") != market.value:
            raise ValueError("research-seed market snapshot provenance mismatch")
        rows = self._payload_rows(payload)
        result: list[_RawMarketRow] = []
        for row in rows:
            company_id = str(row.get("f12") or "")
            name = str(row.get("f14") or "").strip()
            if len(company_id) != 6 or not company_id.isdigit() or not name:
                continue
            if self._excluded_name(name):
                continue
            price = self._number(row.get("f2"))
            amount = self._number(row.get("f6"))
            turnover = self._number(row.get("f8"))
            float_cap = self._number(row.get("f21"))
            if price <= 0 or amount < 0 or turnover < 0 or float_cap < 0:
                continue
            result.append(
                _RawMarketRow(
                    company_id=company_id,
                    market=market,
                    name=name,
                    price=price,
                    amount_cny=amount,
                    turnover_rate=turnover,
                    float_market_cap_cny=float_cap,
                    snapshot_id=snapshot_id,
                )
            )
        return result

    @staticmethod
    def _score_market_rows(
        rows: list[_RawMarketRow],
        *,
        minimum_amount: float,
        minimum_float_cap: float,
    ) -> list[_MarketRow]:
        eligible = [
            row
            for row in rows
            if row.amount_cny >= minimum_amount and row.float_market_cap_cny >= minimum_float_cap
        ]
        if not eligible:
            return []
        amount_values = [log1p(row.amount_cny) for row in eligible]
        cap_values = [log1p(row.float_market_cap_cny) for row in eligible]
        turnover_values = [min(row.turnover_rate, 20.0) for row in eligible]
        amount_rank = ResearchSeedService._percentile_ranks(amount_values)
        cap_rank = ResearchSeedService._percentile_ranks(cap_values)
        turnover_rank = ResearchSeedService._percentile_ranks(turnover_values)
        result: list[_MarketRow] = []
        for index, row in enumerate(eligible):
            score = 0.50 * amount_rank[index] + 0.35 * cap_rank[index] + 0.15 * turnover_rank[index]
            result.append(
                _MarketRow(
                    company_id=row.company_id,
                    market=row.market,
                    name=row.name,
                    price=row.price,
                    amount_cny=row.amount_cny,
                    turnover_rate=row.turnover_rate,
                    float_market_cap_cny=row.float_market_cap_cny,
                    market_score=min(1.0, max(0.0, score)),
                    snapshot_id=row.snapshot_id,
                )
            )
        return result

    @staticmethod
    def _percentile_ranks(values: list[float]) -> list[float]:
        if len(values) == 1:
            return [1.0]
        ordered = sorted((value, index) for index, value in enumerate(values))
        ranks = [0.0] * len(values)
        position = 0
        while position < len(ordered):
            end = position + 1
            while end < len(ordered) and ordered[end][0] == ordered[position][0]:
                end += 1
            average_rank = (position + end - 1) / 2
            percentile = average_rank / (len(values) - 1)
            for _, index in ordered[position:end]:
                ranks[index] = percentile
            position = end
        return ranks

    @staticmethod
    def _parse_boards(payload: dict[str, object]) -> list[tuple[str, str]]:
        request = payload.get("_astock_request")
        if not isinstance(request, dict) or request.get("purpose") != "EXPERT_DOMAIN_TAXONOMY":
            raise ValueError("expert-domain taxonomy provenance mismatch")
        boards: list[tuple[str, str]] = []
        for row in ResearchSeedService._payload_rows(payload):
            code = str(row.get("f12") or "")
            name = str(row.get("f14") or "").strip()
            if code.startswith("BK") and code[2:].isdigit() and len(name) >= 2:
                boards.append((code, name))
        if not boards:
            raise ValueError("industry-board taxonomy is empty")
        return sorted(set(boards), key=lambda item: item[0])

    @staticmethod
    def _constituent_rows(
        payload: dict[str, object],
        expected_board_code: str,
    ) -> list[dict[str, object]]:
        request = payload.get("_astock_request")
        if (
            not isinstance(request, dict)
            or request.get("purpose") != "EXPERT_DOMAIN_CONSTITUENTS"
            or request.get("board_code") != expected_board_code
        ):
            raise ValueError("expert-domain constituent provenance mismatch")
        return ResearchSeedService._payload_rows(payload)

    @staticmethod
    def _payload_rows(payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("rc") != 0:
            raise ValueError("EastMoney seed request failed")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("EastMoney seed payload lacks data")
        diff = data.get("diff")
        if isinstance(diff, dict):
            values = list(diff.values())
        elif isinstance(diff, list):
            values = diff
        else:
            raise ValueError("EastMoney seed payload lacks diff rows")
        if any(not isinstance(item, dict) for item in values):
            raise ValueError("EastMoney seed payload contains malformed rows")
        return values

    def _terms_for_board(self, board_name: str) -> set[str]:
        normalized = self._normalize(board_name)
        terms = {normalized}
        for group in self.alias_groups:
            if any(self._normalize(token) in normalized for token in group.board_contains):
                terms.update(self._normalize(token) for token in group.skill_terms)
        return {item for item in terms if len(item) >= 2}

    @staticmethod
    def _skill_text(skill: dict[str, object]) -> str:
        values: list[str] = []
        for key in (
            "skill_name",
            "decision_question",
            "core_principle",
            "applicable_conditions",
            "required_evidence",
            "positive_signals",
            "negative_signals",
        ):
            value = skill.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(str(item) for item in value)
        return ResearchSeedService._normalize(" ".join(values))

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(value.casefold().split()).replace("-", "").replace("_", "")

    @staticmethod
    def _number(value: object) -> float:
        if value is None:
            return -1.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized in {"", "-", "--"}:
                return -1.0
            try:
                return float(normalized)
            except ValueError:
                return -1.0
        return -1.0

    @staticmethod
    def _excluded_name(name: str) -> bool:
        normalized = name.upper().replace(" ", "")
        return "ST" in normalized or "退" in normalized

    @staticmethod
    def _market_from_instrument_id(instrument_id: str) -> Market:
        prefix = instrument_id.split(":", 1)[0]
        return Market(prefix)

    @staticmethod
    def _apply_market(accumulator: _SeedAccumulator, row: _MarketRow) -> None:
        accumulator.name = row.name
        accumulator.market = row.market
        accumulator.market_score = row.market_score
        accumulator.price = row.price
        accumulator.amount_cny = row.amount_cny
        accumulator.turnover_rate = row.turnover_rate
        accumulator.float_market_cap_cny = row.float_market_cap_cny
        accumulator.snapshot_ids.add(row.snapshot_id)

    @classmethod
    def _accumulator(
        cls,
        accumulators: dict[str, _SeedAccumulator],
        row: _MarketRow,
    ) -> _SeedAccumulator:
        accumulator = accumulators.setdefault(
            row.company_id,
            _SeedAccumulator(company_id=row.company_id, market=row.market, name=row.name),
        )
        cls._apply_market(accumulator, row)
        return accumulator

    @staticmethod
    def _finalize_seed(
        accumulator: _SeedAccumulator,
        created_at: datetime,
    ) -> ResearchSeed:
        boost = 0.04 * max(0, len(accumulator.origins) - 1) + 0.02 * max(
            0, len(accumulator.authors) - 1
        )
        priority = min(1.0, accumulator.priority + boost)
        seed_id = "research-seed:" + content_hash(
            {
                "company_id": accumulator.company_id,
                "origins": sorted(item.value for item in accumulator.origins),
                "candidate_version_id": accumulator.candidate_version_id,
                "authors": sorted(accumulator.authors),
                "domains": sorted(accumulator.domains),
                "support_skills": sorted(accumulator.support_skills),
                "snapshots": sorted(accumulator.snapshot_ids),
            }
        )
        return ResearchSeed(
            seed_id=seed_id,
            company_id=accumulator.company_id,
            market=accumulator.market,
            name=accumulator.name,
            origins=sorted(accumulator.origins, key=lambda item: item.value),
            research_priority_score=priority,
            market_liquidity_score=accumulator.market_score,
            current_price=accumulator.price,
            amount_cny=accumulator.amount_cny,
            turnover_rate=accumulator.turnover_rate,
            float_market_cap_cny=accumulator.float_market_cap_cny,
            candidate_version_id=accumulator.candidate_version_id,
            candidate_strength=accumulator.candidate_strength,
            expert_author_source_ids=sorted(accumulator.authors),
            expert_domain_names=sorted(accumulator.domains),
            expert_domain_support_skill_ids=sorted(accumulator.support_skills),
            reason_codes=sorted(accumulator.reasons),
            source_snapshot_ids=sorted(accumulator.snapshot_ids),
            created_at=created_at,
        )

    def _persist(self, report: ResearchSeedReport) -> ResearchSeedReport:
        ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"ResearchSeedReport:{report.report_id}"
        inputs = sorted(set(report.source_object_hashes))
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != "ResearchSeedReport"
                or str(existing["object_hash"]) != ref.sha256
                or sorted(existing["input_hashes"]) != inputs
            ):
                raise ValueError("research-seed report identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="ResearchSeedReport",
                schema_version=report.schema_version,
                object_hash=ref.sha256,
                input_hashes=inputs,
            )
        self.state.set_checkpoint(
            scope_type="research-seeds",
            scope_key="latest",
            cursor={"artifact_id": artifact_id, "report_id": report.report_id},
            status=report.status.value,
            object_hash=ref.sha256,
        )
        return report

    @staticmethod
    def _empty_report(
        request: ResearchSeedRequest,
        *,
        warnings: list[str],
    ) -> ResearchSeedReport:
        return ResearchSeedReport(
            report_id="research-seeds:"
            + content_hash(
                {
                    "request": request.model_dump(mode="json", exclude={"created_at"}),
                    "warnings": sorted(set(warnings)),
                }
            ),
            as_of=request.as_of,
            data_cutoff_at=request.as_of,
            status=ResearchSeedStatus.NEEDS_INFO,
            profiles=[],
            seeds=[],
            source_snapshot_ids=[],
            source_object_hashes=[],
            warning_codes=sorted(set(warnings)),
            market_seed_count=0,
            expert_seed_count=0,
            existing_candidate_seed_count=0,
            created_at=request.as_of,
        )

    @staticmethod
    def _is_current(as_of: datetime) -> bool:
        return abs(datetime.now(UTC) - as_of.astimezone(UTC)) <= _CURRENT_LIVE_TOLERANCE

    @staticmethod
    def _load_alias_groups(path: Path) -> tuple[_DomainAliasGroup, ...]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            "research-seed-domain-aliases-v1"
        ):
            raise ValueError("research-seed domain alias configuration is invalid")
        groups = payload.get("alias_groups")
        if not isinstance(groups, list):
            raise ValueError("research-seed domain aliases are missing")
        result: list[_DomainAliasGroup] = []
        for item in groups:
            if not isinstance(item, dict):
                raise ValueError("research-seed alias group is invalid")
            board_contains = item.get("board_contains")
            skill_terms = item.get("skill_terms")
            if (
                not isinstance(board_contains, list)
                or not isinstance(skill_terms, list)
                or not board_contains
                or not skill_terms
            ):
                raise ValueError("research-seed alias group is incomplete")
            result.append(
                _DomainAliasGroup(
                    board_contains=tuple(str(value) for value in board_contains),
                    skill_terms=tuple(str(value) for value in skill_terms),
                )
            )
        return tuple(result)

    @staticmethod
    def _load_author_names(path: Path) -> dict[str, str]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
            return {}
        return {
            str(item["source_id"]): str(item.get("display_name") or item["source_id"])
            for item in payload["sources"]
            if isinstance(item, dict) and item.get("source_id")
        }


__all__ = ["ResearchSeedService", "SeedMarketProvider"]
