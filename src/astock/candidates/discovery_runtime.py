"""Production persistence and admission for verified discovery ResearchSeeds.

This module is deliberately narrow: it does not invent company facts or call an
LLM. A publisher must provide evidence-bound DiscoveryThesisResult objects that
already passed the thesis evaluator. The release is then immutable and may be
merged into the ordinary Seed funnel under the active request policy budgets.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astock.candidates.discovery_bridge import (
    DiscoverySeedCompanyContext,
    build_verified_discovery_seed,
)
from astock.candidates.discovery_templates import (
    DiscoveryContractCatalog,
    compile_discovery_contract_catalog,
    compile_discovery_thesis_definitions,
)
from astock.candidates.discovery_theses import (
    DiscoveryChannel,
    DiscoveryThesisDefinition,
    DiscoveryThesisResult,
    evaluate_discovery_thesis,
)
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.repository import DocumentRepository
from astock.evidence.repository import EvidenceRepository
from astock.evidence.service import ClaimEvidenceService
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.semantic_admission_repository import SemanticAdmissionRepository
from astock.knowledge.skill_audit import KnowledgeSkillAuditRepository
from astock.schemas.discovery_runtime import VerifiedDiscoverySeedRelease
from astock.schemas.evidence import (
    Claim,
    ClaimEvidenceBundle,
    ClaimEvidenceLink,
    ClaimStatus,
    ClaimType,
    Evidence,
    EvidenceAttachment,
    EvidenceRelation,
    ReviewerStatus,
    SourceSnapshot,
)
from astock.schemas.knowledge_completion import (
    KnowledgeConditionState,
    KnowledgeDiscoveryCatalog,
)
from astock.schemas.market import Market
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedOrigin

_RELEASE_TYPE = "VerifiedDiscoverySeedRelease"


def _serialized_thesis_result(result: DiscoveryThesisResult) -> dict[str, object]:
    return {
        "company_id": result.company_id,
        "family_id": result.family_id,
        "channel": result.channel.value,
        "thesis_id": result.thesis_id,
        "state": result.state.value,
        "conditions": [item.model_dump(mode="json") for item in result.conditions],
        "method_refs": [list(item) for item in result.method_refs],
        "satisfied_families": list(result.satisfied_families),
        "dependency_fingerprint": result.dependency_fingerprint,
    }


@dataclass(frozen=True)
class ActiveDiscoveryRegistryBinding:
    run_id: str
    release_id: str
    object_hash: str


def active_discovery_registry_binding(
    state: StateStore,
    objects: ObjectStore,
) -> ActiveDiscoveryRegistryBinding | None:
    """Resolve the published audited/semantic registry without reading old visual overlays."""

    completion = KnowledgeCompletionRepository(state)
    run_id = completion.latest_published_run_id()
    if run_id is None:
        return None

    audit_repo = KnowledgeSkillAuditRepository(state)
    audited = audit_repo.latest_release(run_id)
    if audited is None:
        return None
    audited_id = str(audited["release_id"])
    audited_hash = str(audited["release_object_hash"])
    if not objects.verify(audited_hash):
        return None

    semantic_repo = SemanticAdmissionRepository(state)
    semantic = semantic_repo.latest_release(run_id)
    if semantic is None:
        return ActiveDiscoveryRegistryBinding(run_id, audited_id, audited_hash)

    semantic_hash = str(semantic["release_object_hash"])
    if (
        str(semantic["parent_audited_release_id"]) != audited_id
        or str(semantic["parent_audited_object_hash"]) != audited_hash
        or not objects.verify(semantic_hash)
    ):
        return None
    return ActiveDiscoveryRegistryBinding(
        run_id,
        str(semantic["release_id"]),
        semantic_hash,
    )



_AUTHORITY_ORDER = (
    "ISSUER_IR",
    "EXCHANGE_OFFICIAL",
    "CNINFO_OFFICIAL",
    "REGULATOR_OFFICIAL",
)
_ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "event_occurred": ("CORPORATE_ACTIONS",),
    "verification_window": ("CORPORATE_ACTIONS",),
    "company_exposure": ("FINANCIAL_ANNUAL", "FINANCIAL_LATEST_INTERIM"),
    "financial_transmission": ("FINANCIAL_ANNUAL", "FINANCIAL_LATEST_INTERIM"),
    "profit_capture": ("FINANCIAL_ANNUAL", "FINANCIAL_LATEST_INTERIM"),
    "company_operating_change": ("FINANCIAL_ANNUAL", "FINANCIAL_LATEST_INTERIM"),
    "price": ("DAILY_MARKET",),
}


@dataclass(frozen=True)
class DiscoveryRuntimeCompanyContext:
    company_id: str
    market: Market
    name: str
    industry_id: str
    business_profile: str
    industry_label: str | None = None

    def __post_init__(self) -> None:
        if len(self.company_id) != 6 or not self.company_id.isdigit():
            raise ValueError("discovery runtime company id must be six digits")
        if (
            not self.industry_id.strip()
            or not self.business_profile.strip()
            or not self.name.strip()
        ):
            raise ValueError("discovery runtime context is incomplete")


@dataclass(frozen=True)
class DiscoveryEvidenceNeed:
    company_id: str
    channel: str
    role: str
    thesis_family_id: str
    research_question: str
    existing_capabilities: tuple[str, ...]
    preferred_authorities: tuple[str, ...]
    recovery_order: tuple[str, ...] = (
        "LOCAL_REGISTERED_EVIDENCE",
        "CURRENT_RESEARCH_ACQUISITION",
        "PROVIDER_FALLBACK",
        "WEB_SEARCH_OFFICIAL",
    )


@dataclass(frozen=True)
class DiscoveryConditionClaimDraft:
    """One reviewed discovery condition backed by already-registered Evidence."""

    company_id: str
    industry_id: str
    channel: DiscoveryChannel
    role: str
    state: KnowledgeConditionState
    subject_id: str
    evidence_ids: tuple[str, ...]
    confidence: float
    metadata: Mapping[str, object]
    claim_type: ClaimType = ClaimType.FACT


@dataclass(frozen=True)
class DiscoveryRefreshOutcome:
    release: VerifiedDiscoverySeedRelease | None
    results: tuple[DiscoveryThesisResult, ...]
    evidence_needs: tuple[DiscoveryEvidenceNeed, ...]
    evaluation_artifact_ids: tuple[str, ...]
    finding_codes: tuple[str, ...]


class DiscoveryRuntimeService:
    """Evaluate reviewed discovery methods from canonical evidence without serial gate sprawl.

    Missing evidence is returned as a recovery plan. The caller should first run
    existing acquisition/provider capabilities, then Web Search against authoritative
    sources, and only keep the condition unresolved after those bounded paths fail.
    """

    def __init__(
        self,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
    ) -> None:
        self.project_root = project_root
        self.state = state
        self.objects = objects
        self.evidence = EvidenceRepository(state)
        self.claims = ClaimEvidenceService(
            objects,
            state,
            DocumentPageRepository(state),
            DocumentRepository(state),
            self.evidence,
        )
        self.publisher = VerifiedDiscoverySeedService(state, objects)

    def _method_contracts(
        self,
    ) -> tuple[DiscoveryContractCatalog, tuple[DiscoveryThesisDefinition, ...]]:
        # Lazy import avoids pulling the full research CLI/runtime package into the
        # Seed import path and prevents a candidates↔research initialization cycle.
        from astock.research.config import load_research_skill_registry

        method_registry = load_research_skill_registry(
            self.project_root / "configs" / "research_skills.yaml"
        )
        contract_catalog = compile_discovery_contract_catalog(method_registry)
        return contract_catalog, compile_discovery_thesis_definitions(contract_catalog)

    def admit_condition_claim(
        self,
        draft: DiscoveryConditionClaimDraft,
        *,
        as_of: datetime,
    ) -> ClaimEvidenceBundle:
        """Bind reviewed Evidence to one active discovery role in one central admission."""

        if draft.state not in {
            KnowledgeConditionState.SATISFIED,
            KnowledgeConditionState.NOT_SATISFIED,
        }:
            raise ValueError("only verified positive/negative discovery states can become claims")
        if draft.subject_id not in {draft.company_id, draft.industry_id}:
            raise ValueError("discovery claim subject must be the company or its explicit industry")
        if draft.claim_type is ClaimType.OPINION:
            raise ValueError("discovery condition claims cannot be opinion")
        if draft.role == "event_occurred" and draft.state is KnowledgeConditionState.SATISFIED:
            if draft.claim_type is not ClaimType.FACT:
                raise ValueError("satisfied event occurrence must be an observed fact")
        if draft.role == "counterevidence_review":
            if draft.claim_type is not ClaimType.INFERENCE:
                raise ValueError("counterevidence review must be an explicit reviewed inference")
            found = draft.metadata.get("material_refutation_found")
            if type(found) is not bool:
                raise ValueError(
                    "counterevidence review requires material_refutation_found boolean"
                )
            if draft.state is KnowledgeConditionState.SATISFIED and found:
                raise ValueError(
                    "satisfied counterevidence review cannot report a material refutation"
                )
            if draft.state is KnowledgeConditionState.NOT_SATISFIED and not found:
                raise ValueError("failed counterevidence review must report a material refutation")

        contract_catalog, definitions = self._method_contracts()
        definition = next(
            (item for item in definitions if item.channel is draft.channel),
            None,
        )
        if definition is None:
            raise ValueError("active discovery channel has no reviewed method definition")
        binding = next(
            (item for item in definition.bindings if item.role == draft.role),
            None,
        )
        if binding is None:
            raise ValueError("discovery role is not part of the reviewed channel contract")
        condition = contract_catalog.condition(
            binding.final_skill_id,
            binding.condition_id,
        )
        if condition is None:
            raise ValueError("reviewed discovery role condition is unavailable")

        canonical_binding: dict[str, object] = {
            "condition_state": draft.state.value,
            "thesis_family_id": definition.family_id,
            "condition_family": condition.condition_family,
        }
        metadata = dict(draft.metadata)
        if any(
            key in metadata and metadata[key] != value
            for key, value in canonical_binding.items()
        ):
            raise ValueError("metadata cannot override canonical discovery condition binding")
        # These fields are derived from the reviewed contract, never from an
        # extension payload. Matching legacy copies remain idempotent.
        object_json = {**metadata, **canonical_binding}
        return self.claims.create_claim(
            subject_id=draft.subject_id,
            predicate=f"discovery:{draft.role}",
            object_json=object_json,
            as_of=as_of,
            claim_type=draft.claim_type,
            confidence=draft.confidence,
            status=ClaimStatus.VALIDATED,
            attachments=[
                EvidenceAttachment(
                    evidence_id=evidence_id,
                    relation=EvidenceRelation.SUPPORT,
                    reviewer_status=ReviewerStatus.AUTO_VALIDATED,
                )
                for evidence_id in sorted(set(draft.evidence_ids))
            ],
        )

    def refresh(
        self,
        *,
        as_of: datetime,
        contexts: Sequence[DiscoveryRuntimeCompanyContext],
        max_verified_discovery: int,
    ) -> DiscoveryRefreshOutcome:
        binding = active_discovery_registry_binding(self.state, self.objects)
        if binding is None:
            return DiscoveryRefreshOutcome(
                release=None,
                results=(),
                evidence_needs=(),
                evaluation_artifact_ids=(),
                finding_codes=("AUDITED_DISCOVERY_REGISTRY_UNAVAILABLE",),
            )
        loaded = self._active_catalog(binding)
        if loaded is None:
            return DiscoveryRefreshOutcome(
                release=None,
                results=(),
                evidence_needs=(),
                evaluation_artifact_ids=(),
                finding_codes=("ACTIVE_DISCOVERY_CATALOG_UNAVAILABLE",),
            )
        catalog, catalog_artifact_id, catalog_object_hash = loaded

        contract_catalog, definitions = self._method_contracts()

        results: list[DiscoveryThesisResult] = []
        needs: list[DiscoveryEvidenceNeed] = []
        evaluation_artifact_ids: list[str] = [catalog_artifact_id]
        seed_contexts: list[DiscoverySeedCompanyContext] = []

        for context in contexts:
            claims, links, evidence_by_id, snapshots = self._canonical_graph(context)
            company_results: list[DiscoveryThesisResult] = []
            for definition in definitions:
                result = evaluate_discovery_thesis(
                    company_id=context.company_id,
                    industry_id=context.industry_id,
                    business_profile=context.business_profile,
                    definition=definition,
                    catalog=catalog,
                    active_registry_hash=binding.object_hash,
                    contract_catalog=contract_catalog,
                    claims=claims,
                    links=links,
                    evidence=evidence_by_id,
                    snapshots=snapshots,
                    verify_object=self.objects.verify,
                )
                results.append(result)
                company_results.append(result)
                evaluation_artifact_ids.append(
                    self._persist_evaluation(
                        context=context,
                        result=result,
                        binding=binding,
                        catalog_object_hash=catalog_object_hash,
                        contract_catalog_hash=contract_catalog.result_hash,
                        evidence_by_id=evidence_by_id,
                        snapshots=snapshots,
                    )
                )
                needs.extend(
                    self._recovery_needs(
                        context=context,
                        result=result,
                        definition=definition,
                        contract_catalog=contract_catalog,
                    )
                )

            snapshot_ids = {
                evidence_by_id[evidence_id].snapshot_id
                for result in company_results
                for condition in result.conditions
                for evidence_id in condition.evidence_refs
                if evidence_id in evidence_by_id
            }
            seed_contexts.append(
                DiscoverySeedCompanyContext(
                    company_id=context.company_id,
                    market=context.market,
                    name=context.name,
                    industry_label=context.industry_label,
                    source_snapshot_ids=tuple(sorted(snapshot_ids)),
                )
            )

        release = self.publisher.publish(
            as_of=as_of,
            contexts=tuple(seed_contexts),
            results=tuple(results),
            active_registry_hash=binding.object_hash,
            discovery_method_catalog_hash=contract_catalog.result_hash,
            source_artifact_ids=tuple(sorted(set(evaluation_artifact_ids))),
            max_verified_discovery=max_verified_discovery,
        )
        outcome = DiscoveryRefreshOutcome(
            release=release,
            results=tuple(results),
            evidence_needs=tuple(
                sorted(
                    set(needs),
                    key=lambda item: (
                        item.company_id,
                        item.channel,
                        item.role,
                        item.thesis_family_id,
                    ),
                )
            ),
            evaluation_artifact_ids=tuple(sorted(set(evaluation_artifact_ids))),
            finding_codes=(),
        )
        self._persist_refresh(
            outcome,
            binding,
            catalog_object_hash,
            contract_catalog.result_hash,
            as_of,
        )
        return outcome

    def _active_catalog(
        self,
        binding: ActiveDiscoveryRegistryBinding,
    ) -> tuple[KnowledgeDiscoveryCatalog, str, str] | None:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,object_hash FROM artifact_registry "
                "WHERE type='KnowledgeDiscoveryCatalog' ORDER BY created_at DESC,artifact_id DESC"
            ).fetchall()
        for row in rows:
            object_hash = str(row["object_hash"])
            if not self.objects.verify(object_hash):
                continue
            try:
                catalog = KnowledgeDiscoveryCatalog.model_validate_json(
                    self.objects.get_bytes(object_hash)
                )
            except ValueError:
                continue
            if catalog.registry_object_hash == binding.object_hash:
                return catalog, str(row["artifact_id"]), object_hash
        return None

    def _canonical_graph(
        self,
        context: DiscoveryRuntimeCompanyContext,
    ) -> tuple[
        list[Claim],
        list[ClaimEvidenceLink],
        dict[str, Evidence],
        dict[str, SourceSnapshot],
    ]:
        bundles = [
            *self.evidence.claim_bundles_for_subject(context.company_id),
            *self.evidence.claim_bundles_for_subject(context.industry_id),
        ]
        claim_by_id = {bundle.claim.claim_id: bundle.claim for bundle in bundles}
        link_by_identity = {
            (link.claim_id, link.evidence_id, link.relation.value): link
            for bundle in bundles
            for link in bundle.links
        }
        evidence_by_id: dict[str, Evidence] = {}
        snapshots: dict[str, SourceSnapshot] = {}
        for link in link_by_identity.values():
            evidence = self.evidence.get_evidence(link.evidence_id)
            if evidence is None:
                continue
            evidence_by_id[evidence.evidence_id] = evidence
            snapshot = self.state.get_snapshot(evidence.snapshot_id)
            if snapshot is not None:
                snapshots[snapshot.snapshot_id] = snapshot
        return (
            list(claim_by_id.values()),
            list(link_by_identity.values()),
            evidence_by_id,
            snapshots,
        )

    def _persist_evaluation(
        self,
        *,
        context: DiscoveryRuntimeCompanyContext,
        result: DiscoveryThesisResult,
        binding: ActiveDiscoveryRegistryBinding,
        catalog_object_hash: str,
        contract_catalog_hash: str,
        evidence_by_id: dict[str, Evidence],
        snapshots: dict[str, SourceSnapshot],
    ) -> str:
        evidence_ids = sorted(
            {
                evidence_id
                for condition in result.conditions
                for evidence_id in condition.evidence_refs
            }
        )
        evidence_hashes: set[str] = set()
        for evidence_id in evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            evidence_hashes.add(evidence.excerpt_object_sha256)
            snapshot = snapshots.get(evidence.snapshot_id)
            if snapshot is not None:
                evidence_hashes.add(snapshot.object_sha256)
        payload = {
            "schema_version": "discovery-thesis-evaluation-v1",
            "company_id": context.company_id,
            "market": context.market.value,
            "industry_id": context.industry_id,
            "business_profile": context.business_profile,
            "active_registry_release_id": binding.release_id,
            "active_registry_object_hash": binding.object_hash,
            "catalog_object_hash": catalog_object_hash,
            "contract_catalog_hash": contract_catalog_hash,
            "result": _serialized_thesis_result(result),
        }
        ref = self.objects.put_json(payload)
        artifact_id = f"DiscoveryThesisEvaluation:{content_hash(payload)}"
        if self.state.artifact_record(artifact_id) is None:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="DiscoveryThesisEvaluation",
                schema_version="discovery-thesis-evaluation-v1",
                object_hash=ref.sha256,
                input_hashes=sorted(
                    {
                        binding.object_hash,
                        catalog_object_hash,
                        *evidence_hashes,
                    }
                ),
            )
        return artifact_id

    @staticmethod
    def _recovery_needs(
        *,
        context: DiscoveryRuntimeCompanyContext,
        result: DiscoveryThesisResult,
        definition: DiscoveryThesisDefinition,
        contract_catalog: DiscoveryContractCatalog,
    ) -> list[DiscoveryEvidenceNeed]:
        bindings = {item.role: item for item in definition.bindings}
        needs: list[DiscoveryEvidenceNeed] = []
        for condition in result.conditions:
            if condition.state.value != "UNKNOWN":
                continue
            binding = bindings.get(condition.condition_id)
            if binding is None:
                continue
            method_condition = contract_catalog.condition(
                binding.final_skill_id,
                binding.condition_id,
            )
            if method_condition is None:
                continue
            needs.append(
                DiscoveryEvidenceNeed(
                    company_id=context.company_id,
                    channel=result.channel.value,
                    role=condition.condition_id,
                    thesis_family_id=result.family_id,
                    research_question=method_condition.semantic_question,
                    existing_capabilities=_ROLE_CAPABILITIES.get(condition.condition_id, ()),
                    preferred_authorities=_AUTHORITY_ORDER,
                )
            )
        return needs

    def _persist_refresh(
        self,
        outcome: DiscoveryRefreshOutcome,
        binding: ActiveDiscoveryRegistryBinding,
        catalog_object_hash: str,
        contract_catalog_hash: str,
        as_of: datetime,
    ) -> None:
        payload = {
            "schema_version": "discovery-refresh-report-v1",
            "as_of": as_of.isoformat(),
            "active_registry_release_id": binding.release_id,
            "active_registry_object_hash": binding.object_hash,
            "catalog_object_hash": catalog_object_hash,
            "contract_catalog_hash": contract_catalog_hash,
            "release_id": outcome.release.release_id if outcome.release is not None else None,
            "evaluation_artifact_ids": list(outcome.evaluation_artifact_ids),
            "evidence_needs": [
                {
                    "company_id": item.company_id,
                    "channel": item.channel,
                    "role": item.role,
                    "thesis_family_id": item.thesis_family_id,
                    "research_question": item.research_question,
                    "existing_capabilities": list(item.existing_capabilities),
                    "preferred_authorities": list(item.preferred_authorities),
                    "recovery_order": list(item.recovery_order),
                }
                for item in outcome.evidence_needs
            ],
            "finding_codes": list(outcome.finding_codes),
        }
        ref = self.objects.put_json(payload)
        artifact_id = f"DiscoveryRefreshReport:{content_hash(payload)}"
        if self.state.artifact_record(artifact_id) is None:
            input_hashes = [binding.object_hash, catalog_object_hash]
            if outcome.release is not None:
                release_row = self.state.artifact_record(
                    f"{_RELEASE_TYPE}:{outcome.release.release_id}"
                )
                if release_row is not None:
                    input_hashes.append(str(release_row["object_hash"]))
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="DiscoveryRefreshReport",
                schema_version="discovery-refresh-report-v1",
                object_hash=ref.sha256,
                input_hashes=sorted(set(input_hashes)),
            )
        self.state.set_checkpoint(
            scope_type="verified-discovery-refresh",
            scope_key="latest",
            cursor={
                "artifact_id": artifact_id,
                "release_id": outcome.release.release_id if outcome.release is not None else None,
                "evidence_need_count": len(outcome.evidence_needs),
            },
            status=(
                "PARTIAL"
                if outcome.finding_codes
                else "RECOVERY_REQUIRED"
                if outcome.evidence_needs
                else "READY"
            ),
            object_hash=ref.sha256,
        )


class VerifiedDiscoverySeedRepository:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def latest_for(
        self,
        *,
        as_of: datetime,
        binding: ActiveDiscoveryRegistryBinding,
    ) -> tuple[VerifiedDiscoverySeedRelease, str] | None:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,object_hash FROM artifact_registry "
                "WHERE type=? ORDER BY created_at DESC,artifact_id DESC",
                (_RELEASE_TYPE,),
            ).fetchall()

        matches: list[tuple[VerifiedDiscoverySeedRelease, str]] = []
        for row in rows:
            object_hash = str(row["object_hash"])
            if not self.objects.verify(object_hash):
                continue
            try:
                release = VerifiedDiscoverySeedRelease.model_validate_json(
                    self.objects.get_bytes(object_hash)
                )
            except ValueError:
                continue
            if (
                release.as_of <= as_of
                and release.active_registry_release_id == binding.release_id
                and release.active_registry_object_hash == binding.object_hash
            ):
                matches.append((release, object_hash))
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0].as_of, item[0].release_id))

    def release_by_object_hash(
        self,
        object_hash: str,
    ) -> VerifiedDiscoverySeedRelease | None:
        if not self.objects.verify(object_hash):
            return None
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type FROM artifact_registry WHERE object_hash=? AND type=? LIMIT 1",
                (object_hash, _RELEASE_TYPE),
            ).fetchone()
        if row is None:
            return None
        try:
            return VerifiedDiscoverySeedRelease.model_validate_json(
                self.objects.get_bytes(object_hash)
            )
        except ValueError:
            return None

    def seed_is_bound_to_report_inputs(
        self,
        seed: ResearchSeed,
        source_object_hashes: Sequence[str],
    ) -> bool:
        for object_hash in source_object_hashes:
            release = self.release_by_object_hash(object_hash)
            if release is None:
                continue
            for released in release.seeds:
                if (
                    released.company_id == seed.company_id
                    and released.market is seed.market
                    and ResearchSeedOrigin.EXPERT_SKILL in seed.origins
                    and set(released.reason_codes).issubset(seed.reason_codes)
                    and set(released.expert_domain_names).issubset(seed.expert_domain_names)
                    and set(released.expert_domain_support_skill_ids).issubset(
                        seed.expert_domain_support_skill_ids
                    )
                    and set(released.source_snapshot_ids).issubset(seed.source_snapshot_ids)
                ):
                    return True
        return False


class VerifiedDiscoverySeedService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.repository = VerifiedDiscoverySeedRepository(state, objects)

    def publish(
        self,
        *,
        as_of: datetime,
        contexts: Sequence[DiscoverySeedCompanyContext],
        results: tuple[DiscoveryThesisResult, ...],
        active_registry_hash: str,
        discovery_method_catalog_hash: str,
        source_artifact_ids: Sequence[str],
        max_verified_discovery: int,
    ) -> VerifiedDiscoverySeedRelease:
        binding = active_discovery_registry_binding(self.state, self.objects)
        if binding is None or binding.object_hash != active_registry_hash:
            raise ValueError("verified discovery release must bind the active audited registry")
        context_by_company: dict[str, DiscoverySeedCompanyContext] = {}
        for context in contexts:
            previous = context_by_company.get(context.company_id)
            if previous is not None and previous != context:
                raise ValueError("conflicting discovery company context")
            context_by_company[context.company_id] = context

        seeds: list[ResearchSeed] = []
        for company_id in sorted(context_by_company):
            seed = build_verified_discovery_seed(
                context=context_by_company[company_id],
                results=results,
                active_registry_hash=active_registry_hash,
                discovery_method_catalog_hash=discovery_method_catalog_hash,
            )
            if seed is not None:
                # ResearchSeed.created_at is normally generated at model construction time.
                # Bind it to the release snapshot so rerunning the same frozen discovery input
                # remains byte-for-byte idempotent instead of colliding on a volatile timestamp.
                seeds.append(seed.model_copy(update={"created_at": as_of}))
        seeds.sort(key=lambda item: (-item.research_priority_score, item.company_id))
        seeds = seeds[: max(0, max_verified_discovery)]

        artifact_ids = sorted(set(source_artifact_ids))
        source_hashes: set[str] = {active_registry_hash}
        matched_evaluations: set[str] = set()
        for artifact_id in artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(f"verified discovery source artifact missing: {artifact_id}")
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                raise ValueError(f"verified discovery source object unavailable: {artifact_id}")
            source_hashes.add(object_hash)
            if str(record["type"]) != "DiscoveryThesisEvaluation":
                continue
            try:
                payload = json.loads(self.objects.get_bytes(object_hash))
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != "discovery-thesis-evaluation-v1"
                or payload.get("active_registry_object_hash") != active_registry_hash
                or payload.get("contract_catalog_hash") != discovery_method_catalog_hash
                or not isinstance(payload.get("result"), dict)
            ):
                continue
            matched_evaluations.add(content_hash(payload["result"]))

        required_evaluations = {
            content_hash(_serialized_thesis_result(result)) for result in results
        }
        if not required_evaluations.issubset(matched_evaluations):
            raise ValueError(
                "verified discovery results require exact registered evaluation artifacts"
            )

        source_object_hashes = sorted(source_hashes)
        identity = {
            "as_of": as_of.isoformat(),
            "active_registry_release_id": binding.release_id,
            "active_registry_object_hash": binding.object_hash,
            "discovery_method_catalog_hash": discovery_method_catalog_hash,
            "seeds": [seed.model_dump(mode="json") for seed in seeds],
            "source_artifact_ids": artifact_ids,
            "source_object_hashes": source_object_hashes,
            "producer": "verified-discovery-runtime-v1",
        }
        release = VerifiedDiscoverySeedRelease(
            release_id=(
                "verified-discovery-seeds:"
                f"{sha256_bytes(canonical_json_bytes(identity))}"
            ),
            as_of=as_of,
            active_registry_release_id=binding.release_id,
            active_registry_object_hash=binding.object_hash,
            discovery_method_catalog_hash=discovery_method_catalog_hash,
            seeds=seeds,
            source_artifact_ids=artifact_ids,
            source_object_hashes=source_object_hashes,
            created_at=as_of,
        )
        ref = self.objects.put_json(release.model_dump(mode="json"))
        artifact_id = f"{_RELEASE_TYPE}:{release.release_id}"
        existing = self.state.artifact_record(artifact_id)
        if existing is None:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type=_RELEASE_TYPE,
                schema_version=release.schema_version,
                object_hash=ref.sha256,
                input_hashes=release.source_object_hashes,
            )
        elif (
            str(existing["object_hash"]) != ref.sha256
            or sorted(existing["input_hashes"]) != release.source_object_hashes
        ):
            raise ValueError("verified discovery release identity collision")

        self.state.set_checkpoint(
            scope_type="verified-discovery-seeds",
            scope_key="latest",
            cursor={"artifact_id": artifact_id, "release_id": release.release_id},
            status="READY",
            object_hash=ref.sha256,
        )
        return release


__all__ = [
    "ActiveDiscoveryRegistryBinding",
    "DiscoveryConditionClaimDraft",
    "DiscoveryEvidenceNeed",
    "DiscoveryRefreshOutcome",
    "DiscoveryRuntimeCompanyContext",
    "DiscoveryRuntimeService",
    "VerifiedDiscoverySeedRepository",
    "VerifiedDiscoverySeedService",
    "active_discovery_registry_binding",
]
