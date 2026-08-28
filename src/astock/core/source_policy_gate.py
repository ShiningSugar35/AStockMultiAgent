"""Deterministic admission gate for Agent-proposed Web/Search sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from astock.schemas import SourceClass
from astock.schemas.source_access import (
    AgentSourceProposal,
    SourceAdmissionStatus,
    SourcePolicyDecision,
)


@dataclass(frozen=True, slots=True)
class AuthoritySource:
    source_id: str
    domains: tuple[str, ...]
    source_class: SourceClass
    capabilities: frozenset[str]
    formal_capabilities: frozenset[str]
    exhaustive_capabilities: frozenset[str]
    independence_group: str


@dataclass(frozen=True, slots=True)
class AuthorityDomainRegistry:
    schema_version: str
    sources: tuple[AuthoritySource, ...]


def load_authority_domain_registry(path: Path) -> AuthorityDomainRegistry:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid authority domain registry: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "authority-domains-v1":
        raise ValueError("Unsupported authority domain registry")
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("Authority domain registry requires sources")
    sources: list[AuthoritySource] = []
    seen_ids: set[str] = set()
    seen_domains: set[str] = set()
    for item in raw_sources:
        if not isinstance(item, dict):
            raise ValueError("Authority source must be an object")
        source_id = str(item.get("source_id") or "").strip()
        domains = tuple(_normalize_domain(str(value)) for value in item.get("domains", []))
        source_class = SourceClass(str(item.get("source_class")))
        capabilities = frozenset(str(value) for value in item.get("capabilities", []))
        formal = frozenset(str(value) for value in item.get("formal_capabilities", []))
        exhaustive = frozenset(str(value) for value in item.get("exhaustive_capabilities", []))
        independence_group = str(item.get("independence_group") or "").strip()
        if not source_id or source_id in seen_ids or not domains or not capabilities:
            raise ValueError("Authority source identity/capability contract is invalid")
        if source_class is not SourceClass.PRIMARY_OFFICIAL_WEB:
            raise ValueError("Authority registry only admits PRIMARY_OFFICIAL_WEB sources")
        if not formal.issubset(capabilities) or not exhaustive.issubset(capabilities):
            raise ValueError("Authority capability subsets are invalid")
        if not independence_group:
            raise ValueError("Authority source requires independence_group")
        for domain in domains:
            if domain in seen_domains:
                raise ValueError(f"Authority domain is duplicated: {domain}")
            seen_domains.add(domain)
        seen_ids.add(source_id)
        sources.append(
            AuthoritySource(
                source_id=source_id,
                domains=domains,
                source_class=source_class,
                capabilities=capabilities,
                formal_capabilities=formal,
                exhaustive_capabilities=exhaustive,
                independence_group=independence_group,
            )
        )
    return AuthorityDomainRegistry(schema_version="authority-domains-v1", sources=tuple(sources))


class SourcePolicyGate:
    """Validate one Agent source proposal without turning discovery into evidence."""

    def __init__(self, registry: AuthorityDomainRegistry | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        self.registry = registry or load_authority_domain_registry(
            root / "configs" / "authority_domains.yaml"
        )

    def validate(self, proposal: AgentSourceProposal) -> SourcePolicyDecision:
        if proposal.require_complete and (
            proposal.candidate_url is None
            or proposal.preferred_source_class is SourceClass.REPUTABLE_WEB_SEARCH
        ):
            # Search discovery itself is never a terminal pagination/completeness proof.
            return self._reject(
                proposal,
                "SEARCH_WEB_CANNOT_PROVE_COMPLETENESS",
                source_class=proposal.preferred_source_class,
            )
        if proposal.candidate_url is None:
            if proposal.formal_use:
                return self._reject(
                    proposal,
                    "FORMAL_WEB_SOURCE_REQUIRES_EXACT_URL",
                    source_class=proposal.preferred_source_class,
                )
            return SourcePolicyDecision(
                requested_capability=proposal.requested_capability,
                allowed=True,
                source_class=proposal.preferred_source_class,
                formal_eligible=False,
                exhaustive_proof_allowed=False,
                admission_status=SourceAdmissionStatus.DISCOVERY_ONLY,
                reason_codes=["QUERY_DISCOVERY_ALLOWED"],
            )

        domain = _normalize_domain(proposal.candidate_url.host or "")
        if proposal.formal_use and proposal.candidate_url.scheme != "https":
            return self._reject(
                proposal,
                "FORMAL_WEB_SOURCE_REQUIRES_HTTPS",
                domain=domain,
                source_class=proposal.preferred_source_class,
            )
        if proposal.formal_use and (
            proposal.candidate_url.username is not None
            or proposal.candidate_url.password is not None
        ):
            return self._reject(
                proposal,
                "FORMAL_WEB_SOURCE_CANNOT_USE_URL_CREDENTIALS",
                domain=domain,
                source_class=proposal.preferred_source_class,
            )
        authority = self._authority_for(domain)
        if authority is None:
            if proposal.formal_use:
                return self._reject(
                    proposal,
                    "UNREGISTERED_FORMAL_SOURCE",
                    domain=domain,
                    source_class=proposal.preferred_source_class,
                )
            return SourcePolicyDecision(
                requested_capability=proposal.requested_capability,
                allowed=True,
                domain=domain,
                source_class=proposal.preferred_source_class,
                formal_eligible=False,
                exhaustive_proof_allowed=False,
                admission_status=SourceAdmissionStatus.DISCOVERY_ONLY,
                reason_codes=["UNREGISTERED_SOURCE_DISCOVERY_ONLY"],
            )

        if proposal.requested_capability not in authority.capabilities:
            return self._reject(
                proposal,
                "AUTHORITY_SOURCE_LACKS_CAPABILITY",
                source_id=authority.source_id,
                domain=domain,
                source_class=authority.source_class,
            )
        if (
            proposal.formal_use
            and proposal.requested_capability not in authority.formal_capabilities
        ):
            return self._reject(
                proposal,
                "CAPABILITY_NOT_FORMALLY_ELIGIBLE_ON_WEB_PATH",
                source_id=authority.source_id,
                domain=domain,
                source_class=authority.source_class,
            )
        if (
            proposal.require_complete
            and proposal.requested_capability not in authority.exhaustive_capabilities
        ):
            return self._reject(
                proposal,
                "AUTHORITY_SOURCE_LACKS_EXHAUSTIVE_PROOF_CONTRACT",
                source_id=authority.source_id,
                domain=domain,
                source_class=authority.source_class,
            )

        return SourcePolicyDecision(
            requested_capability=proposal.requested_capability,
            allowed=True,
            source_id=authority.source_id,
            domain=domain,
            source_class=authority.source_class,
            formal_eligible=(
                proposal.requested_capability in authority.formal_capabilities
            ),
            exhaustive_proof_allowed=(
                proposal.requested_capability in authority.exhaustive_capabilities
            ),
            admission_status=(
                SourceAdmissionStatus.ADMIT_AFTER_SNAPSHOT
                if proposal.formal_use
                else SourceAdmissionStatus.DISCOVERY_ONLY
            ),
            independence_group=authority.independence_group,
            reason_codes=[
                "REGISTERED_AUTHORITY_DOMAIN",
                "SNAPSHOT_AND_PROVENANCE_REQUIRED_BEFORE_EVIDENCE_ADMISSION",
            ],
        )

    def _authority_for(self, domain: str) -> AuthoritySource | None:
        for source in self.registry.sources:
            if any(
                domain == allowed or domain.endswith(f".{allowed}")
                for allowed in source.domains
            ):
                return source
        return None

    @staticmethod
    def _reject(
        proposal: AgentSourceProposal,
        reason: str,
        *,
        source_id: str | None = None,
        domain: str | None = None,
        source_class: SourceClass | None = None,
    ) -> SourcePolicyDecision:
        return SourcePolicyDecision(
            requested_capability=proposal.requested_capability,
            allowed=False,
            source_id=source_id,
            domain=domain,
            source_class=source_class or proposal.preferred_source_class,
            formal_eligible=False,
            exhaustive_proof_allowed=False,
            admission_status=SourceAdmissionStatus.REJECTED,
            reason_codes=[reason],
        )


def _normalize_domain(value: str) -> str:
    candidate = value.strip().lower().rstrip(".")
    if "://" in candidate:
        candidate = urlparse(candidate).hostname or ""
    if candidate.startswith("www."):
        candidate = candidate[4:]
    if not candidate or " " in candidate or "/" in candidate:
        raise ValueError(f"Invalid authority domain: {value}")
    return candidate


__all__ = [
    "AuthorityDomainRegistry",
    "AuthoritySource",
    "SourcePolicyGate",
    "load_authority_domain_registry",
]
