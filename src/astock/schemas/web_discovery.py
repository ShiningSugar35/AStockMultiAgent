"""Typed contracts for deterministic, offline Web discovery leads.

Every artifact produced here is DISCOVERY_ONLY. A lead is a reproducible set of
search links plus inert, normalized candidate URLs; it is never evidence, never
a completeness proof, and never receives formal authority. Candidate URLs must
still pass the existing ``SourcePolicyGate`` and be frozen with provenance
before any evidence admission (see AGENTS.md "Data and evidence").

Artifacts are plain strict BaseModel values with no implicit timestamps: the
same input must always produce byte-identical output.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from astock.schemas.source_access import SourcePolicyDecision

DISCOVERY_ONLY: Literal["DISCOVERY_ONLY"] = "DISCOVERY_ONLY"
CONTRACT_VERSION: Literal["web-discovery-lead-v1"] = "web-discovery-lead-v1"


class WebDiscoveryRedirectRule(BaseModel):
    """A known search-engine redirect endpoint allowed to be unwrapped."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    host: str = Field(min_length=1, max_length=255)
    params: tuple[str, ...] = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def _normalized_host(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or "://" in normalized or "/" in normalized or " " in normalized:
            raise ValueError("redirect rule host must be a bare hostname")
        return normalized


class WebDiscoveryEngine(BaseModel):
    """One search engine endpoint. Order and budgets always come from config."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    engine_id: str = Field(min_length=1, max_length=64)
    url_template: str | None = Field(default=None, min_length=1, max_length=512)
    independence_group: str = Field(min_length=1, max_length=64)

    @field_validator("url_template")
    @classmethod
    def _https_with_query_placeholder(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith("https://") or "{q}" not in value:
            raise ValueError("engine url_template must be https and contain {q}")
        return value


class WebDiscoveryConfig(BaseModel):
    """Versioned, strict policy for Web discovery. No thresholds live in code."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["web-discovery-lead-v1"]
    official_domains_source: str = Field(min_length=1)
    default_capability: str = Field(min_length=1, max_length=128)
    official_site_query_enabled: bool
    max_official_domains_per_query: int = Field(ge=1, le=16)
    max_engines: int = Field(ge=1, le=32)
    max_results_per_engine: int = Field(ge=1, le=200)
    max_query_length: int = Field(ge=8, le=1024)
    max_redirect_depth: int = Field(ge=1, le=8)
    max_input_bytes: int = Field(ge=1024, le=4194304)
    min_independent_hits: int = Field(ge=1, le=8)
    official_domain_rank_bonus: int = Field(ge=0, le=1000)
    independent_hit_weight: int = Field(ge=0, le=1000)
    tracking_params: tuple[str, ...]
    redirect_rules: tuple[WebDiscoveryRedirectRule, ...]
    default_engines: tuple[str, ...]
    engines: tuple[WebDiscoveryEngine, ...]

    @model_validator(mode="after")
    def _validate_registry(self) -> WebDiscoveryConfig:
        if not self.engines:
            raise ValueError("web discovery requires at least one engine")
        engine_ids = [engine.engine_id for engine in self.engines]
        if len(set(engine_ids)) != len(engine_ids):
            raise ValueError("web discovery engine_id must be unique")
        if not self.default_engines:
            raise ValueError("web discovery requires default_engines")
        unknown = set(self.default_engines) - set(engine_ids)
        if unknown:
            raise ValueError(f"default_engines references unknown engine: {sorted(unknown)}")
        if self.min_independent_hits > len({engine.independence_group for engine in self.engines}):
            raise ValueError("min_independent_hits exceeds available independence groups")
        if any(
            (engine := self.engine_by_id(item)) is None or engine.url_template is None
            for item in self.default_engines
        ):
            raise ValueError("default engines require a search URL template")
        hosts = [rule.host for rule in self.redirect_rules]
        if len(set(hosts)) != len(hosts):
            raise ValueError("redirect rule hosts must be unique")
        return self

    def engine_by_id(self, engine_id: str) -> WebDiscoveryEngine | None:
        return next((engine for engine in self.engines if engine.engine_id == engine_id), None)

    def redirect_params_for(self, host: str) -> frozenset[str]:
        for rule in self.redirect_rules:
            # Only the specifically configured redirect endpoint can unwrap.
            # A same-domain subdomain may host third-party content and must not
            # inherit the endpoint's authority to replace the source URL.
            if host == rule.host:
                return frozenset(rule.params)
        return frozenset()

    def max_input_results(self) -> int:
        return self.max_engines * self.max_results_per_engine


class WebDiscoveryRawResult(BaseModel):
    """One Agent-acquired search hit. External data, inert by contract."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    title: str = Field(default="", max_length=2000)
    snippet: str = Field(default="", max_length=4000)
    engine: str = Field(min_length=1, max_length=64)

    @field_validator("url", mode="before")
    @classmethod
    def _unambiguous_url(cls, value: object) -> object:
        # Validate before HttpUrl can discard controls or reinterpret backslashes.
        raw = str(value)
        if re.search(r"[\x00-\x20\x7f]", raw) or "\\" in raw:
            raise ValueError("search URL contains whitespace, controls or backslashes")
        try:
            parsed = urlparse(raw)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("search URL must not contain credentials")
        except ValueError as exc:
            raise ValueError("search URL has invalid authority") from exc
        return value


class WebDiscoveryInput(BaseModel):
    """Boundary-validated payload for ``web-discovery-admit``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1024)
    requested_capability: str = Field(min_length=1, max_length=128)
    expected_fact: str = Field(min_length=1, max_length=1000)
    results: tuple[WebDiscoveryRawResult, ...] = Field(default_factory=tuple)


class WebSearchLink(BaseModel):
    """A reproducible search URL. Producing it never performs network I/O.

    Value objects carry no timestamp: identical input must produce identical output.
    """

    model_config = ConfigDict(extra="forbid")

    engine_id: str
    search_url: HttpUrl
    query: str
    official_domain: str | None = None


class WebDiscoveryResult(BaseModel):
    """A normalized, deduplicated candidate URL with independence provenance."""

    model_config = ConfigDict(extra="forbid")

    canonical_url: HttpUrl
    title: str
    snippet: str
    engines: tuple[str, ...]
    independence_groups: tuple[str, ...]
    independent_hits: int = Field(ge=1)
    official_domain: str | None = None
    rank: int = Field(ge=1)


class WebDiscoveryLead(BaseModel):
    """The only artifact this capability emits: a non-formal discovery lead."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["web-discovery-lead-v1"] = CONTRACT_VERSION
    lead_id: str = Field(min_length=1)
    query: str
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_capability: str
    expected_fact: str
    engine_ids: tuple[str, ...]
    official_domains: tuple[str, ...]
    links: tuple[WebSearchLink, ...]
    results: tuple[WebDiscoveryResult, ...]
    completeness: Literal["DISCOVERY_ONLY"] = DISCOVERY_ONLY
    formal_use_allowed: Literal[False] = False
    exhaustive_proof_allowed: Literal[False] = False
    admission_status: Literal["DISCOVERY_ONLY"] = DISCOVERY_ONLY
    provenance_note: str


class WebDiscoveryAdmission(BaseModel):
    """Decisions from the existing SourcePolicyGate, one per candidate URL.

    ``discovery_allowed_urls`` means "allowed as a lead only", NOT "admitted as
    evidence". ``formal_eligible`` inside a decision is a static property of the
    registered official domain; consumers must judge admission by
    ``admission_status`` only.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["web-discovery-lead-v1"] = CONTRACT_VERSION
    lead_id: str
    decisions: tuple[SourcePolicyDecision, ...]
    discovery_allowed_urls: tuple[str, ...]
    rejected_urls: tuple[str, ...]
    formal_fact_count: Literal[0] = 0
    completeness: Literal["DISCOVERY_ONLY"] = DISCOVERY_ONLY


__all__ = [
    "CONTRACT_VERSION",
    "DISCOVERY_ONLY",
    "WebDiscoveryAdmission",
    "WebDiscoveryConfig",
    "WebDiscoveryEngine",
    "WebDiscoveryInput",
    "WebDiscoveryLead",
    "WebDiscoveryRawResult",
    "WebDiscoveryRedirectRule",
    "WebDiscoveryResult",
    "WebSearchLink",
]
