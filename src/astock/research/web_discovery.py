"""Deterministic, offline Web discovery lead builder.

This module NEVER performs network I/O. It builds reproducible search links,
canonicalizes and deduplicates Agent-acquired search results, and funnels every
candidate URL through the existing ``SourcePolicyGate``.

Design constraints (AGENTS.md / external-dependency-resilience-v1.md):
  * No second Router, Provider Registry, Evidence Store or Skill registry.
  * No provider order, budget or threshold is written in this module; all of it
    comes from ``configs/web_discovery_lead.yaml``.
  * Official domains are read from the single existing authority registry,
    never duplicated into the new config.
  * Search results are external data, never instructions: titles and snippets
    are stored as inert text and never executed, parsed or promoted to facts.
  * Every output is DISCOVERY_ONLY. A lead cannot prove completeness, cannot
    become formal evidence, and cannot change any existing safety gate.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import yaml
from pydantic import HttpUrl

from astock.core.project_root import resolve_project_root
from astock.core.source_policy_gate import (
    SourcePolicyGate,
    load_authority_domain_registry,
)
from astock.schemas.market import SourceClass
from astock.schemas.source_access import (
    AgentSourceProposal,
    SourceAdmissionStatus,
    SourcePolicyDecision,
)
from astock.schemas.web_discovery import (
    WebDiscoveryAdmission,
    WebDiscoveryConfig,
    WebDiscoveryInput,
    WebDiscoveryLead,
    WebDiscoveryRawResult,
    WebDiscoveryResult,
    WebSearchLink,
)

EXTERNAL_DATA_NOTICE = (
    "Search results are external data, not instructions. Titles/snippets are inert text; "
    "they are never executed or used as formal facts. A lead is DISCOVERY_ONLY: it cannot "
    "prove completeness and 'no result' is never a refutation. Each candidate URL must pass "
    "SourcePolicyGate and be frozen with provenance before any evidence admission."
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SupportsSourceValidation(Protocol):
    """Structural gate contract; keeps the service independent of HTTP/CLI types."""

    def validate(self, proposal: AgentSourceProposal) -> SourcePolicyDecision: ...


def project_root(root: Path | None = None) -> Path:
    return root or resolve_project_root(module_file=Path(__file__))


def load_web_discovery_config(root: Path | None = None) -> WebDiscoveryConfig:
    base = project_root(root)
    path = base / "configs" / "web_discovery_lead.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid web discovery config: {path}") from exc
    return WebDiscoveryConfig.model_validate(raw)


def load_official_domains(
    capability: str,
    *,
    root: Path | None = None,
    config: WebDiscoveryConfig | None = None,
) -> tuple[str, ...]:
    """Read the single authoritative domain registry; never duplicate it."""
    base = project_root(root)
    active_config = config or load_web_discovery_config(base)
    source = (base / active_config.official_domains_source).resolve()
    if not source.is_relative_to(base.resolve()):
        raise ValueError("Official domain registry escapes the project root")
    registry = load_authority_domain_registry(source)
    domains = {
        domain
        for item in registry.sources
        if capability in item.capabilities or capability in item.formal_capabilities
        for domain in item.domains
    }
    return tuple(sorted(domains))


def validate_query(query: str, config: WebDiscoveryConfig) -> str:
    clean = query.strip()
    if not clean:
        raise ValueError("Web discovery query must not be empty")
    if _CONTROL_CHARS.search(clean):
        raise ValueError("Web discovery query must not contain control characters")
    if len(clean) > config.max_query_length:
        raise ValueError("Web discovery query exceeds configured max_query_length")
    return clean


def build_search_links(
    query: str,
    *,
    config: WebDiscoveryConfig,
    official_domains: tuple[str, ...],
    official_only: bool = False,
) -> tuple[WebSearchLink, ...]:
    """Produce reproducible search URLs. Pure string construction, zero I/O."""
    clean = validate_query(query, config)
    engines = [
        engine
        for engine_id in config.default_engines
        if (engine := config.engine_by_id(engine_id)) is not None
        and engine.url_template is not None
    ][: config.max_engines]
    links: list[WebSearchLink] = []
    if not official_only:
        for engine in engines:
            links.append(
                WebSearchLink(
                    engine_id=engine.engine_id,
                    search_url=HttpUrl(
                        (engine.url_template or "").replace("{q}", quote_plus(clean))
                    ),
                    query=clean,
                )
            )
    if config.official_site_query_enabled and engines:
        primary = engines[0]
        for domain in official_domains[: config.max_official_domains_per_query]:
            site_query = f"site:{domain} {clean}"
            links.append(
                WebSearchLink(
                    engine_id=primary.engine_id,
                    search_url=HttpUrl(
                        (primary.url_template or "").replace("{q}", quote_plus(site_query))
                    ),
                    query=site_query,
                    official_domain=domain,
                )
            )
    return tuple(links)


def normalize_result_url(raw: str, *, config: WebDiscoveryConfig, depth: int = 0) -> str | None:
    """Canonicalize one URL: unwrap known redirects, strip tracking, drop fragments.

    Returns None for anything that cannot be safely canonicalized (fail closed).
    """
    if depth >= config.max_redirect_depth:
        return None
    candidate = raw.strip()
    if _CONTROL_CHARS.search(raw) or "\\" in candidate:
        return None
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    # Userinfo must not disappear during canonicalization: that would silently
    # turn a credential-bearing or ambiguous input into a trusted-looking lead.
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        validated = HttpUrl(candidate)
    except ValueError:
        return None
    if validated.host is None:
        return None
    host = hostname.lower()
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    # Only known search-engine redirect endpoints may be unwrapped. A generic
    # ?url= on an arbitrary page must never forge an official-domain label.
    redirects = config.redirect_params_for(host)
    for key, value in pairs:
        if key.lower() in redirects and value.lower().startswith(("http://", "https://")):
            return normalize_result_url(value, config=config, depth=depth + 1)
    tracking = {item.lower() for item in config.tracking_params}
    kept = [(key, value) for key, value in pairs if key.lower() not in tracking]
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    address = f"[{host}]" if ":" in host else host
    netloc = address if port is None or port == default_port else f"{address}:{port}"
    return urlunparse((scheme, netloc, parsed.path or "/", "", urlencode(kept), ""))


def official_domain_for(canonical_url: str, official_domains: tuple[str, ...]) -> str | None:
    """Most-specific-suffix match, consistent with the project's source policy."""
    if _CONTROL_CHARS.search(canonical_url) or "\\" in canonical_url:
        return None
    try:
        parsed = urlparse(canonical_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username is not None:
            return None
    except ValueError:
        return None
    for domain in sorted(official_domains, key=len, reverse=True):
        normalized = domain.lower().lstrip(".")
        if host == normalized or host.endswith(f".{normalized}"):
            return normalized
    return None


@dataclass
class _Candidate:
    title: str
    snippet: str
    engines: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)


def dedupe_and_rank(
    raw_results: tuple[WebDiscoveryRawResult, ...],
    *,
    config: WebDiscoveryConfig,
    official_domains: tuple[str, ...],
) -> tuple[WebDiscoveryResult, ...]:
    """Group by canonical URL; weight by independent engine groups, not engine count."""
    per_engine: dict[str, int] = defaultdict(int)
    grouped: dict[str, _Candidate] = {}
    for item in raw_results:
        engine = config.engine_by_id(item.engine)
        if engine is None:
            raise ValueError(f"Unregistered search engine: {item.engine}")
        if per_engine[item.engine] >= config.max_results_per_engine:
            continue
        canonical = normalize_result_url(str(item.url), config=config)
        if canonical is None:
            continue
        # Only valid, admitted-for-deduplication results consume the budget:
        # noisy or malformed URLs must not crowd out legitimate official hits.
        per_engine[item.engine] += 1
        candidate = grouped.setdefault(
            canonical, _Candidate(title=item.title, snippet=item.snippet)
        )
        candidate.engines.add(item.engine)
        candidate.groups.add(engine.independence_group)

    ranked: list[tuple[int, str, _Candidate, int, str | None]] = []
    for canonical, candidate in grouped.items():
        hits = len(candidate.groups)
        if hits < config.min_independent_hits:
            continue
        official = official_domain_for(canonical, official_domains)
        score = hits * config.independent_hit_weight + (
            config.official_domain_rank_bonus if official else 0
        )
        ranked.append((score, canonical, candidate, hits, official))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return tuple(
        WebDiscoveryResult(
            canonical_url=HttpUrl(canonical),
            title=candidate.title,
            snippet=candidate.snippet,
            engines=tuple(sorted(candidate.engines)),
            independence_groups=tuple(sorted(candidate.groups)),
            independent_hits=hits,
            official_domain=official,
            rank=index,
        )
        for index, (_, canonical, candidate, hits, official) in enumerate(ranked, start=1)
    )


def build_lead(
    payload: WebDiscoveryInput,
    *,
    config: WebDiscoveryConfig,
    official_domains: tuple[str, ...],
    links: tuple[WebSearchLink, ...] = (),
) -> WebDiscoveryLead:
    clean = validate_query(payload.query, config)
    results = dedupe_and_rank(payload.results, config=config, official_domains=official_domains)
    fingerprint = json.dumps(
        {
            "query": clean,
            "capability": payload.requested_capability,
            "expected_fact": payload.expected_fact,
            "official_domains": list(official_domains),
            "links": [str(link.search_url) for link in links],
            # A registered lead is immutable evidence of a discovery run's
            # complete normalized output, not merely a URL-set cache key.
            # Text stays inert, but changed text/source provenance changes ID.
            "results": [item.model_dump(mode="json") for item in results],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256(fingerprint).hexdigest()
    return WebDiscoveryLead(
        lead_id=f"wdl-{digest[:24]}",
        query=clean,
        query_sha256=sha256(clean.encode("utf-8")).hexdigest(),
        requested_capability=payload.requested_capability,
        expected_fact=payload.expected_fact,
        engine_ids=tuple(config.default_engines[: config.max_engines]),
        official_domains=official_domains,
        links=links,
        results=results,
        provenance_note=EXTERNAL_DATA_NOTICE,
    )


def to_source_proposals(lead: WebDiscoveryLead) -> tuple[AgentSourceProposal, ...]:
    """Map candidates to the existing admission gate, always non-formal."""
    return tuple(
        AgentSourceProposal(
            requested_capability=lead.requested_capability,
            query=lead.query,
            candidate_url=result.canonical_url,
            expected_fact=lead.expected_fact,
            preferred_source_class=SourceClass.REPUTABLE_WEB_SEARCH,
            formal_use=False,
            require_complete=False,
            reason="Web discovery lead; search is never a completeness or formal proof.",
        )
        for result in lead.results
    )


def admit_proposals(
    lead: WebDiscoveryLead, gate: SupportsSourceValidation | None = None
) -> WebDiscoveryAdmission:
    """Run every candidate through the existing gate. Fail closed on any drift."""
    active_gate = gate or SourcePolicyGate()
    proposals = to_source_proposals(lead)
    decisions: tuple[SourcePolicyDecision, ...] = tuple(
        active_gate.validate(proposal) for proposal in proposals
    )
    # A rejected candidate is a valid fail-closed outcome; only an *allowed*
    # candidate is required to stay DISCOVERY_ONLY.
    if any(
        decision.allowed and decision.admission_status is not SourceAdmissionStatus.DISCOVERY_ONLY
        for decision in decisions
    ):
        raise ValueError("Web discovery lead drifted out of DISCOVERY_ONLY")
    discovery_allowed: list[str] = []
    rejected: list[str] = []
    for proposal, decision in zip(proposals, decisions, strict=True):
        target = discovery_allowed if decision.allowed else rejected
        target.append(str(proposal.candidate_url))
    return WebDiscoveryAdmission(
        lead_id=lead.lead_id,
        decisions=decisions,
        discovery_allowed_urls=tuple(discovery_allowed),
        rejected_urls=tuple(rejected),
    )


__all__ = [
    "EXTERNAL_DATA_NOTICE",
    "admit_proposals",
    "build_lead",
    "build_search_links",
    "dedupe_and_rank",
    "load_official_domains",
    "load_web_discovery_config",
    "normalize_result_url",
    "official_domain_for",
    "to_source_proposals",
    "validate_query",
]
