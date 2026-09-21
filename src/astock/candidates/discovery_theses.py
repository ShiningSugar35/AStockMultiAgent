"""Evidence-bound discovery hypotheses; no acquisition, trading, or fact persistence.

The caller supplies an exact active reviewed catalog and current canonical claims.
Role mappings and claim semantics still need the existing independent review: this
module checks their bindings, not the truth of arbitrary natural-language claims.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from astock.core.hashing import content_hash
from astock.schemas.evidence import (
    Claim,
    ClaimEvidenceLink,
    ClaimStatus,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceRelation,
    FactStatus,
    FetchStatus,
    ReviewerStatus,
    SourceSnapshot,
)
from astock.schemas.knowledge_completion import (
    KnowledgeConditionEvaluation,
    KnowledgeDiscoveryCatalog,
    KnowledgeDiscoveryConditionKind,
    KnowledgeDiscoveryDisposition,
)
from astock.schemas.knowledge_completion import (
    KnowledgeConditionState as State,
)

VERSION = "discovery-theses-v1"


class DiscoveryChannel(StrEnum):
    BOTTLENECK = "BOTTLENECK"
    EVENT = "EVENT"
    CYCLE = "CYCLE"


REQUIRED_ROLES: Mapping[DiscoveryChannel, tuple[str, ...]] = {
    DiscoveryChannel.BOTTLENECK: (
        "necessity",
        "supply_scarcity",
        "substitution_difficulty",
        "company_exposure",
        "profit_capture",
        "counterevidence_review",
    ),
    DiscoveryChannel.EVENT: (
        "event_occurred",
        "demand_transmission",
        "company_exposure",
        "financial_transmission",
        "verification_window",
        "counterevidence_review",
    ),
    DiscoveryChannel.CYCLE: (
        "demand",
        "price",
        "inventory",
        "capacity",
        "capital_expenditure",
        "cycle_stage",
        "company_exposure",
        "company_operating_change",
        "counterevidence_review",
    ),
}
_COMPANY_ROLES = frozenset(
    {
        "company_exposure",
        "profit_capture",
        "financial_transmission",
        "company_operating_change",
    }
)
_REVIEWED = frozenset({ReviewerStatus.AUTO_VALIDATED, ReviewerStatus.HUMAN_APPROVED})
_EXPOSURE_KINDS = frozenset({"PRODUCT", "CUSTOMER", "REVENUE", "COST", "CAPACITY"})


@dataclass(frozen=True)
class DiscoveryMethodBinding:
    role: str
    final_skill_id: str
    condition_id: str


@dataclass(frozen=True)
class DiscoveryThesisDefinition:
    """One reviewed economic hypothesis, not a vote per author or per Skill."""

    family_id: str
    channel: DiscoveryChannel
    bindings: tuple[DiscoveryMethodBinding, ...]

    def __post_init__(self) -> None:
        if not self.family_id.strip():
            raise ValueError("discovery hypothesis requires an economic family identity")
        roles = REQUIRED_ROLES[self.channel]
        if {binding.role for binding in self.bindings} != set(roles):
            raise ValueError("discovery hypothesis must map every required role exactly")
        if len(set(self.bindings)) != len(self.bindings):
            raise ValueError("duplicate discovery method binding")


class DiscoveryExternalCondition(Protocol):
    @property
    def condition_id(self) -> str: ...

    @property
    def condition_family(self) -> str: ...

    @property
    def applicability(self) -> tuple[str, ...]: ...


class DiscoveryExternalMethodCatalog(Protocol):
    @property
    def result_hash(self) -> str: ...

    def verify(self) -> None: ...

    def condition(
        self,
        skill_id: str,
        condition_id: str,
    ) -> DiscoveryExternalCondition | None: ...


@dataclass(frozen=True)
class DiscoveryThesisResult:
    company_id: str
    family_id: str
    channel: DiscoveryChannel
    thesis_id: str
    state: State
    conditions: tuple[KnowledgeConditionEvaluation, ...]
    method_refs: tuple[tuple[str, str], ...]
    satisfied_families: tuple[str, ...]
    dependency_fingerprint: str

    @property
    def coverage(self) -> dict[str, int]:
        # The necessary-condition denominator never shrinks when evidence is missing.
        return {
            state.value: sum(item.state is state for item in self.conditions) for state in State
        }

    @property
    def economic_support_count(self) -> int:
        return len(self.satisfied_families)


def _method_roles(
    definition: DiscoveryThesisDefinition,
    catalog: KnowledgeDiscoveryCatalog,
    active_registry_hash: str,
    business_profile: str,
    contract_catalog: DiscoveryExternalMethodCatalog | None = None,
) -> dict[str, tuple[str, bool]]:
    if catalog.registry_object_hash != active_registry_hash:
        raise ValueError("discovery catalog is not bound to the active audited registry")
    if (
        content_hash(catalog.model_dump(mode="json", exclude={"result_hash"}))
        != catalog.result_hash
    ):
        raise ValueError("discovery catalog content hash mismatch")
    if contract_catalog is not None:
        contract_catalog.verify()

    entries = {entry.final_skill_id: entry for entry in catalog.entries}
    mapped = {(item.final_skill_id, item.condition_id) for item in definition.bindings}
    roles: dict[str, tuple[str, bool]] = {}
    prerequisites_by_binding: dict[tuple[str, str], tuple[str, ...]] = {}

    for binding in definition.bindings:
        entry = entries.get(binding.final_skill_id)
        if entry is not None:
            if entry.disposition is not KnowledgeDiscoveryDisposition.SEMANTIC_DISCOVERY:
                raise ValueError("discovery method is not an active semantic disposition")
            matches = [
                item
                for item in entry.conditions
                if item.condition_id == binding.condition_id
            ]
            if len(matches) != 1:
                raise ValueError("discovery method condition is missing or ambiguous")
            condition = matches[0]
            if condition.condition_kind is not KnowledgeDiscoveryConditionKind.SEMANTIC_QUESTION:
                raise ValueError("a semantic role cannot execute an arbitrary numeric expression")
            prerequisites = tuple(condition.prerequisites)
        else:
            condition = (
                contract_catalog.condition(binding.final_skill_id, binding.condition_id)
                if contract_catalog is not None
                else None
            )
            if condition is None:
                raise ValueError(
                    "discovery method is not an active semantic or reviewed contract"
                )
            prerequisites = ()

        if any(
            prerequisite == binding.condition_id
            or (binding.final_skill_id, prerequisite) not in mapped
            for prerequisite in prerequisites
        ):
            raise ValueError("discovery method prerequisite is unmapped or self-dependent")
        prerequisites_by_binding[(binding.final_skill_id, binding.condition_id)] = prerequisites
        current = (
            condition.condition_family,
            business_profile in condition.applicability or "*" in condition.applicability,
        )
        if binding.role in roles and roles[binding.role] != current:
            raise ValueError("method aliases disagree on condition family or applicability")
        roles[binding.role] = current

    if len({family for family, _ in roles.values()}) != len(roles):
        raise ValueError("one economic condition cannot stand in for multiple required roles")

    pending = set(mapped)
    while pending:
        ready = {
            (skill_id, condition_id)
            for skill_id, condition_id in pending
            if not any(
                (skill_id, prerequisite) in pending
                for prerequisite in prerequisites_by_binding[(skill_id, condition_id)]
            )
        }
        if not ready:
            raise ValueError("discovery method prerequisite graph is cyclic")
        pending.difference_update(ready)
    return roles


def _qualified_relations(
    claim: Claim,
    links: Sequence[ClaimEvidenceLink],
    evidence: Mapping[str, Evidence],
    snapshots: Mapping[str, SourceSnapshot],
    verify_object: Callable[[str], bool],
) -> tuple[set[EvidenceRelation], set[str], set[str], set[str]]:
    relations: set[EvidenceRelation] = set()
    refs: set[str] = set()
    reasons: set[str] = set()
    dependencies: set[str] = set()
    for link in links:
        if link.relation is EvidenceRelation.CONTEXT or link.weight <= 0:
            continue
        if link.reviewer_status not in _REVIEWED:
            reasons.add("EVIDENCE_LINK_NOT_REVIEWED")
            continue
        item = evidence.get(link.evidence_id)
        if item is None or item.evidence_id != link.evidence_id:
            reasons.add("EVIDENCE_UNAVAILABLE")
            continue
        if claim.subject_id not in item.entity_ids:
            reasons.add("EVIDENCE_ENTITY_MISMATCH")
            continue
        refs.add(item.evidence_id)
        dependencies.add(content_hash(item.model_dump(mode="json")))
        if item.fact_status is FactStatus.CONFLICTED:
            reasons.add("MATERIAL_EVIDENCE_CONFLICT")
            continue
        if (
            item.fact_status is not FactStatus.DIRECT
            or item.evidence_grade is EvidenceGrade.COMMUNITY_LEAD
        ):
            reasons.add("EVIDENCE_IS_ONLY_A_LEAD")
            continue
        snapshot = snapshots.get(item.snapshot_id)
        if snapshot is None or snapshot.snapshot_id != item.snapshot_id:
            reasons.add("SOURCE_SNAPSHOT_UNAVAILABLE")
            continue
        dependencies.add(content_hash(snapshot.model_dump(mode="json")))
        if snapshot.fetch_status is not FetchStatus.SUCCEEDED:
            reasons.add("SOURCE_CAPTURE_NOT_SUCCEEDED")
            continue
        hashes = (snapshot.object_sha256, item.excerpt_object_sha256)
        if not all(verify_object(digest) for digest in hashes):
            reasons.add("SOURCE_BYTES_UNVERIFIED")
            continue
        # Excerpts are separately persisted raw UTF-8 bytes in the canonical Evidence contract.
        if item.excerpt_sha256 != item.excerpt_object_sha256:
            reasons.add("EXCERPT_HASH_MISMATCH")
            continue
        dependencies.update(hashes)
        relations.add(link.relation)
    return relations, refs, reasons, dependencies


def _evaluate_role(
    role: str,
    family: str,
    thesis_family_id: str,
    applicable: bool,
    company_id: str,
    industry_id: str,
    claims: Sequence[Claim],
    links_by_claim: Mapping[str, Sequence[ClaimEvidenceLink]],
    evidence: Mapping[str, Evidence],
    snapshots: Mapping[str, SourceSnapshot],
    verify_object: Callable[[str], bool],
) -> tuple[KnowledgeConditionEvaluation, set[str]]:
    if not applicable:
        return KnowledgeConditionEvaluation(
            condition_id=role,
            state=State.NOT_APPLICABLE,
            reason_codes=["METHOD_NOT_APPLICABLE_TO_BUSINESS_PROFILE"],
        ), set()
    states: set[State] = set()
    reasons: set[str] = set()
    refs: set[str] = set()
    dependencies: set[str] = set()
    allowed_subjects = {company_id} if role in _COMPANY_ROLES else {company_id, industry_id}
    for claim in claims:
        if claim.predicate != f"discovery:{role}" or claim.subject_id not in allowed_subjects:
            continue
        if (
            claim.object_json.get("thesis_family_id") != thesis_family_id
            or claim.object_json.get("condition_family") != family
        ):
            reasons.add("CLAIM_HYPOTHESIS_BINDING_MISMATCH")
            continue
        dependencies.add(content_hash(claim.model_dump(mode="json")))
        if claim.status is ClaimStatus.CONFLICTED:
            reasons.add("MATERIAL_EVIDENCE_CONFLICT")
            continue
        if claim.status is not ClaimStatus.VALIDATED or claim.claim_type is ClaimType.OPINION:
            reasons.add("CLAIM_NOT_VALIDATED")
            continue
        raw_state = claim.object_json.get("condition_state")
        if raw_state not in (State.SATISFIED.value, State.NOT_SATISFIED.value):
            reasons.add("CLAIM_CONDITION_UNVERIFIED")
            continue
        if role == "counterevidence_review":
            if claim.claim_type is not ClaimType.INFERENCE:
                reasons.add("COUNTEREVIDENCE_REVIEW_NOT_INFERENCE")
                continue
            material_refutation_found = claim.object_json.get("material_refutation_found")
            if type(material_refutation_found) is not bool:
                reasons.add("COUNTEREVIDENCE_REVIEW_RESULT_MISSING")
                continue
            if (
                raw_state == State.SATISFIED.value and material_refutation_found
            ) or (
                raw_state == State.NOT_SATISFIED.value and not material_refutation_found
            ):
                reasons.add("COUNTEREVIDENCE_REVIEW_STATE_MISMATCH")
                continue
        if (
            role == "event_occurred"
            and raw_state == State.SATISFIED.value
            and (
                claim.claim_type is not ClaimType.FACT
                or claim.object_json.get("actuality") != "OCCURRED"
            )
        ):
            reasons.add("EVENT_NOT_OBSERVED_AS_OCCURRED")
            continue
        if (
            role == "company_exposure"
            and raw_state == State.SATISFIED.value
            and (
                not isinstance(claim.object_json.get("exposure_kind"), str)
                or claim.object_json.get("exposure_kind") not in _EXPOSURE_KINDS
            )
        ):
            reasons.add("NO_VERIFIED_COMPANY_BUSINESS_EXPOSURE")
            continue
        relations, claim_refs, claim_reasons, claim_dependencies = _qualified_relations(
            claim,
            links_by_claim.get(claim.claim_id, ()),
            evidence,
            snapshots,
            verify_object,
        )
        refs.update(claim_refs)
        reasons.update(claim_reasons)
        dependencies.update(claim_dependencies)
        value = State(raw_state)
        if EvidenceRelation.SUPPORT in relations:
            states.add(value)
        if EvidenceRelation.REFUTE in relations:
            if value is State.SATISFIED:
                states.add(State.NOT_SATISFIED)
            elif EvidenceRelation.SUPPORT in relations:
                reasons.add("MATERIAL_EVIDENCE_CONFLICT")
            else:
                # Disproving a negative claim does not establish the positive fact.
                reasons.add("NEGATIVE_REFUTATION_REQUIRES_POSITIVE_EVIDENCE")
    if len(states) > 1 or "MATERIAL_EVIDENCE_CONFLICT" in reasons:
        state = State.UNKNOWN
        reasons.add("MATERIAL_EVIDENCE_CONFLICT")
    elif states:
        state = next(iter(states))
    else:
        state = State.UNKNOWN
        reasons.add("REQUIRED_COMPANY_OR_INDUSTRY_EVIDENCE_MISSING")
    reasons.add(f"ECONOMIC_CONDITION:{family}")
    return KnowledgeConditionEvaluation(
        condition_id=role,
        state=state,
        evidence_refs=sorted(refs),
        reason_codes=sorted(reasons),
    ), dependencies


def evaluate_discovery_thesis(
    *,
    company_id: str,
    industry_id: str,
    business_profile: str,
    definition: DiscoveryThesisDefinition,
    catalog: KnowledgeDiscoveryCatalog,
    active_registry_hash: str,
    contract_catalog: DiscoveryExternalMethodCatalog | None = None,
    claims: Sequence[Claim],
    links: Sequence[ClaimEvidenceLink],
    evidence: Mapping[str, Evidence],
    snapshots: Mapping[str, SourceSnapshot],
    verify_object: Callable[[str], bool],
) -> DiscoveryThesisResult:
    """Evaluate one company/hypothesis without scoring popularity or inventing facts.

    ``verify_object`` is normally the existing ObjectStore.verify. Input selection,
    current corrections, source rights and the role mapping's semantic review stay
    with the canonical services; an arbitrary caller cannot confer publication authority.
    """
    if not all(value.strip() for value in (company_id, industry_id, business_profile)):
        raise ValueError("company, industry and business profile identities are required")
    if company_id == industry_id:
        raise ValueError("company exposure cannot reuse an industry identity")
    roles = _method_roles(
        definition,
        catalog,
        active_registry_hash,
        business_profile,
        contract_catalog,
    )
    claim_by_id: dict[str, Claim] = {}
    for claim in claims:
        previous = claim_by_id.get(claim.claim_id)
        if previous is not None and previous != claim:
            raise ValueError("conflicting canonical claim identity")
        claim_by_id[claim.claim_id] = claim
    grouped: dict[str, list[ClaimEvidenceLink]] = defaultdict(list)
    for link in links:
        grouped[link.claim_id].append(link)
    evaluations: list[KnowledgeConditionEvaluation] = []
    dependencies = {catalog.result_hash, active_registry_hash, VERSION}
    if contract_catalog is not None:
        dependencies.add(contract_catalog.result_hash)
    for role in REQUIRED_ROLES[definition.channel]:
        family, applicable = roles[role]
        result, refs = _evaluate_role(
            role,
            family,
            definition.family_id,
            applicable,
            company_id,
            industry_id,
            tuple(claim_by_id.values()),
            grouped,
            evidence,
            snapshots,
            verify_object,
        )
        evaluations.append(result)
        dependencies.update(refs)
    states = {item.state for item in evaluations}
    if State.NOT_APPLICABLE in states:
        state = State.NOT_APPLICABLE
    elif State.NOT_SATISFIED in states:
        state = State.NOT_SATISFIED
    elif State.UNKNOWN in states:
        state = State.UNKNOWN
    else:
        state = State.SATISFIED
    identity = {
        "company_id": company_id,
        "family_id": definition.family_id,
        "channel": definition.channel,
    }
    return DiscoveryThesisResult(
        company_id=company_id,
        family_id=definition.family_id,
        channel=definition.channel,
        thesis_id=f"discovery-thesis:{content_hash(identity)}",
        state=state,
        conditions=tuple(evaluations),
        method_refs=tuple(
            sorted({(item.final_skill_id, item.condition_id) for item in definition.bindings})
        ),
        satisfied_families=tuple(
            sorted(
                {
                    roles[item.condition_id][0]
                    for item in evaluations
                    if item.state is State.SATISFIED
                }
            )
        ),
        dependency_fingerprint=content_hash(
            {
                **identity,
                "business_profile": business_profile,
                "industry_id": industry_id,
                "roles": roles,
                "dependencies": sorted(dependencies),
                "states": {item.condition_id: item.state for item in evaluations},
            }
        ),
    )


def qualified_company_theses(
    results: Sequence[DiscoveryThesisResult],
) -> dict[str, tuple[str, ...]]:
    """Union distinct proven hypotheses; aliases and repeated reports add no seat.

    Divergent current results for the same hypothesis are not resolved by input order.
    They are omitted for reconciliation. A different proven hypothesis stays eligible.
    """
    grouped: dict[tuple[str, str], list[DiscoveryThesisResult]] = defaultdict(list)
    for result in results:
        grouped[(result.company_id, result.thesis_id)].append(result)
    companies: dict[str, set[str]] = defaultdict(set)
    for (company_id, thesis_id), group in grouped.items():
        if {item.state for item in group} == {State.SATISFIED}:
            companies[company_id].add(thesis_id)
    return {company: tuple(sorted(companies[company])) for company in sorted(companies)}
