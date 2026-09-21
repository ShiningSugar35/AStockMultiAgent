"""Synthetic controls for WP40; not a company sample or production acceptance."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from astock.candidates.discovery_theses import (
    REQUIRED_ROLES,
    evaluate_discovery_thesis,
    qualified_company_theses,
)
from astock.candidates.discovery_theses import (
    DiscoveryChannel as Channel,
)
from astock.candidates.discovery_theses import (
    DiscoveryMethodBinding as Binding,
)
from astock.candidates.discovery_theses import (
    DiscoveryThesisDefinition as Definition,
)
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.evidence import (
    Claim,
    ClaimEvidenceLink,
    ClaimStatus,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceLocator,
    EvidenceRelation,
    FactStatus,
    FetchStatus,
    ReviewerStatus,
    SourceSnapshot,
)
from astock.schemas.knowledge_completion import (
    KnowledgeConditionState as State,
)
from astock.schemas.knowledge_completion import (
    KnowledgeDiscoveryCatalog,
    KnowledgeDiscoveryCatalogEntry,
    KnowledgeDiscoveryConditionKind,
    KnowledgeDiscoveryConditionSpec,
    KnowledgeDiscoveryDisposition,
)

NOW = datetime(2026, 9, 18, tzinfo=UTC)
REGISTRY = "a" * 64


def catalog_for(
    channel: Channel, *, applicability: str = "GENERAL_INDUSTRIAL"
) -> KnowledgeDiscoveryCatalog:
    conditions = [
        KnowledgeDiscoveryConditionSpec(
            condition_id=role,
            condition_family=f"economic:{role}",
            condition_kind=KnowledgeDiscoveryConditionKind.SEMANTIC_QUESTION,
            applicability=[applicability],
            semantic_question=f"Verify the explicit {role} contract.",
        )
        for role in REQUIRED_ROLES[channel]
    ]
    # Synthetic method material; never registered as a real Skill or author opinion.
    entry = KnowledgeDiscoveryCatalogEntry(
        final_skill_id="synthetic-method",
        skill_name="Synthetic test method",
        primary_module=DirectSkillModule.FUNDAMENTAL_RESEARCH,
        source_hashes=["b" * 64],
        skill_object_hash="c" * 64,
        skill_origin="SYNTHETIC_TEST",
        disposition=KnowledgeDiscoveryDisposition.SEMANTIC_DISCOVERY,
        reason_codes=["SYNTHETIC_CONTROL"],
        conditions=conditions,
    )
    value = KnowledgeDiscoveryCatalog(
        run_id="synthetic-test",
        registry_release_id="synthetic-release",
        registry_object_hash=REGISTRY,
        compiler_version="synthetic-test-v1",
        disposition_batch_hash="d" * 64,
        entries=[entry],
        member_count=1,
        result_hash="e" * 64,
    )
    return rehash(value)


def rehash(catalog: KnowledgeDiscoveryCatalog) -> KnowledgeDiscoveryCatalog:
    return catalog.model_copy(
        update={
            "result_hash": content_hash(catalog.model_dump(mode="json", exclude={"result_hash"})),
        }
    )


def packet(tmp_path: Path, channel: Channel = Channel.EVENT) -> dict[str, Any]:
    store = ObjectStore(tmp_path / "objects")
    catalog = catalog_for(channel)
    family_id = f"synthetic-{channel.value.lower()}"
    claims, links, evidence, snapshots = [], [], {}, {}
    for role in REQUIRED_ROLES[channel]:
        subject = (
            "company:A"
            if role
            in {
                "company_exposure",
                "financial_transmission",
                "profit_capture",
                "company_operating_change",
            }
            else "industry:X"
        )
        raw = store.put_bytes(f"Synthetic reviewed control for {role}, {subject}".encode())
        snapshot = SourceSnapshot(
            snapshot_id=f"snapshot:{role}",
            source_id="synthetic-source",
            object_sha256=raw.sha256,
            fetched_at=NOW,
            available_to_system_at=NOW,
            mime="text/plain",
            byte_size=raw.byte_size,
        )
        item = Evidence(
            evidence_id=f"evidence:{role}",
            document_id=f"document:{role}",
            snapshot_id=snapshot.snapshot_id,
            page_id=f"page:{role}",
            locator=EvidenceLocator(
                page_number=1, char_start=0, char_end=10, parser_version="synthetic"
            ),
            excerpt_sha256=raw.sha256,
            excerpt_object_sha256=raw.sha256,
            evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
            fact_status=FactStatus.DIRECT,
            entity_ids=[subject],
            available_to_system_at=NOW,
            rights_status="LOCAL_RESEARCH",
        )
        claim = Claim(
            claim_id=f"claim:{role}",
            subject_id=subject,
            predicate=f"discovery:{role}",
            object_json={
                "condition_state": State.SATISFIED.value,
                "thesis_family_id": family_id,
                "condition_family": f"economic:{role}",
                "actuality": "OCCURRED",
                "exposure_kind": "REVENUE",
                **(
                    {"material_refutation_found": False}
                    if role == "counterevidence_review"
                    else {}
                ),
            },
            as_of=NOW,
            claim_type=(
                ClaimType.INFERENCE if role == "counterevidence_review" else ClaimType.FACT
            ),
            confidence=1,
            status=ClaimStatus.VALIDATED,
        )
        claims.append(claim)
        links.append(
            ClaimEvidenceLink(
                claim_id=claim.claim_id,
                evidence_id=item.evidence_id,
                relation=EvidenceRelation.SUPPORT,
                reviewer_status=ReviewerStatus.HUMAN_APPROVED,
            )
        )
        evidence[item.evidence_id] = item
        snapshots[snapshot.snapshot_id] = snapshot
    return dict(
        company_id="company:A",
        industry_id="industry:X",
        business_profile="GENERAL_INDUSTRIAL",
        definition=Definition(
            family_id,
            channel,
            tuple(Binding(role, "synthetic-method", role) for role in REQUIRED_ROLES[channel]),
        ),
        catalog=catalog,
        active_registry_hash=REGISTRY,
        claims=claims,
        links=links,
        evidence=evidence,
        snapshots=snapshots,
        verify_object=store.verify,
    )


def change_claim(data: dict[str, Any], role: str, **fields: Any) -> None:
    index = next(i for i, claim in enumerate(data["claims"]) if claim.claim_id == f"claim:{role}")
    claim = data["claims"][index]
    data["claims"][index] = claim.model_copy(
        update={"object_json": {**claim.object_json, **fields}}
    )


@pytest.mark.parametrize("channel", list(Channel))
@pytest.mark.parametrize("state", list(State))
def test_all_channels_preserve_four_states(tmp_path: Path, channel: Channel, state: State) -> None:
    data = packet(tmp_path, channel)
    if state is State.NOT_APPLICABLE:
        data["business_profile"] = "BANK"
    elif state is State.UNKNOWN:
        data["claims"] = []
    elif state is State.NOT_SATISFIED:
        change_claim(data, "company_exposure", condition_state=state.value)
    result = evaluate_discovery_thesis(**data)
    assert result.state is state
    assert sum(result.coverage.values()) == len(REQUIRED_ROLES[channel])
    assert bool(qualified_company_theses([result])) is (state is State.SATISFIED)


@pytest.mark.parametrize("channel,role", [(c, role) for c in Channel for role in REQUIRED_ROLES[c]])
def test_each_necessary_condition_is_actually_required(
    tmp_path: Path, channel: Channel, role: str
) -> None:
    data = packet(tmp_path, channel)
    data["claims"] = [claim for claim in data["claims"] if claim.predicate != f"discovery:{role}"]
    result = evaluate_discovery_thesis(**data)
    assert result.state is State.UNKNOWN
    assert result.coverage[State.UNKNOWN.value] == 1
    assert result.economic_support_count == len(REQUIRED_ROLES[channel]) - 1


@pytest.mark.parametrize("role", ["company_exposure", "financial_transmission"])
def test_industry_membership_cannot_prove_company_exposure(tmp_path: Path, role: str) -> None:
    data = packet(tmp_path)
    data["claims"] = [
        claim.model_copy(update={"subject_id": "industry:X"})
        if claim.claim_id == f"claim:{role}"
        else claim
        for claim in data["claims"]
    ]
    assert evaluate_discovery_thesis(**data).state is State.UNKNOWN


@pytest.mark.parametrize("kind", ["SECTOR_TAG", "KEYWORD", None])
def test_theme_names_are_not_business_exposure(tmp_path: Path, kind: str | None) -> None:
    data = packet(tmp_path)
    change_claim(data, "company_exposure", exposure_kind=kind)
    assert evaluate_discovery_thesis(**data).state is State.UNKNOWN


def test_expected_event_is_not_an_occurred_order(tmp_path: Path) -> None:
    data = packet(tmp_path)
    change_claim(data, "event_occurred", actuality="EXPECTED")
    result = evaluate_discovery_thesis(**data)
    assert result.state is State.UNKNOWN
    assert "EVENT_NOT_OBSERVED_AS_OCCURRED" in result.conditions[0].reason_codes


@pytest.mark.parametrize(
    "bad", ["lead", "conflict", "missing", "bytes", "entity", "link", "partial"]
)
def test_evidence_failures_do_not_turn_into_verified_conditions(tmp_path: Path, bad: str) -> None:
    data = packet(tmp_path)
    key = "evidence:company_exposure"
    item = data["evidence"][key]
    if bad == "lead":
        data["evidence"][key] = item.model_copy(
            update={"evidence_grade": EvidenceGrade.COMMUNITY_LEAD}
        )
    elif bad == "conflict":
        data["evidence"][key] = item.model_copy(update={"fact_status": FactStatus.CONFLICTED})
    elif bad == "missing":
        del data["evidence"][key]
    elif bad == "bytes":
        ObjectStore(tmp_path / "objects").path_for(item.excerpt_sha256).write_bytes(b"changed")
    elif bad == "entity":
        data["evidence"][key] = item.model_copy(update={"entity_ids": ["company:B"]})
    elif bad == "link":
        data["links"] = [
            link.model_copy(update={"reviewer_status": ReviewerStatus.UNREVIEWED})
            if link.evidence_id == key
            else link
            for link in data["links"]
        ]
    else:
        snapshot = data["snapshots"][item.snapshot_id]
        data["snapshots"][item.snapshot_id] = snapshot.model_copy(
            update={"fetch_status": FetchStatus.PARTIAL}
        )
    assert evaluate_discovery_thesis(**data).state is State.UNKNOWN


def test_conflict_is_not_resolved_by_duplicate_support_votes(tmp_path: Path) -> None:
    data = packet(tmp_path)
    link = data["links"][0]
    data["links"] += [link] * 20 + [link.model_copy(update={"relation": EvidenceRelation.REFUTE})]
    result = evaluate_discovery_thesis(**data)
    assert result.state is State.UNKNOWN
    assert "MATERIAL_EVIDENCE_CONFLICT" in result.conditions[0].reason_codes


def test_duplicate_sources_methods_order_and_author_name_do_not_add_support(tmp_path: Path) -> None:
    data = packet(tmp_path)
    before = evaluate_discovery_thesis(**data)
    data["links"] = list(reversed(data["links"] * 4))
    data["claims"] = list(reversed(data["claims"] * 3))
    original = data["catalog"].entries[0]
    alias = original.model_copy(
        update={"final_skill_id": "synthetic-method-z", "skill_name": "Another author same method"}
    )
    data["catalog"] = rehash(
        data["catalog"].model_copy(update={"entries": [original, alias], "member_count": 2})
    )
    definition = data["definition"]
    data["definition"] = replace(
        definition,
        bindings=definition.bindings
        + tuple(
            Binding(role, "synthetic-method-z", role) for role in REQUIRED_ROLES[definition.channel]
        ),
    )
    after = evaluate_discovery_thesis(**data)
    assert (before.state, before.thesis_id, before.economic_support_count) == (
        after.state,
        after.thesis_id,
        after.economic_support_count,
    )
    assert before.coverage == after.coverage
    assert qualified_company_theses([before, after, after]) == qualified_company_theses([before])


def test_different_hypotheses_form_union_not_global_intersection(tmp_path: Path) -> None:
    good = evaluate_discovery_thesis(**packet(tmp_path / "good", Channel.EVENT))
    data = packet(tmp_path / "weak", Channel.CYCLE)
    data["claims"] = []
    weak = evaluate_discovery_thesis(**data)
    assert qualified_company_theses([weak, good]) == {good.company_id: (good.thesis_id,)}
    contradicted = replace(good, state=State.NOT_SATISFIED)
    assert qualified_company_theses([good, contradicted]) == {}


@pytest.mark.parametrize(
    "field,value",
    [("thesis_family_id", "different-event"), ("condition_family", "unrelated-economics")],
)
def test_another_hypothesis_cannot_lend_its_generic_role_claim(
    tmp_path: Path, field: str, value: str
) -> None:
    data = packet(tmp_path)
    change_claim(data, "event_occurred", **{field: value})
    assert evaluate_discovery_thesis(**data).state is State.UNKNOWN


def test_registry_drift_and_catalog_tampering_are_rejected(tmp_path: Path) -> None:
    data = packet(tmp_path)
    data["active_registry_hash"] = "f" * 64
    with pytest.raises(ValueError, match="active audited"):
        evaluate_discovery_thesis(**data)
    data["active_registry_hash"] = REGISTRY
    data["catalog"] = data["catalog"].model_copy(update={"compiler_version": "tampered"})
    with pytest.raises(ValueError, match="content hash"):
        evaluate_discovery_thesis(**data)


def test_unmapped_method_prerequisite_is_not_silently_dropped(tmp_path: Path) -> None:
    data = packet(tmp_path)
    entry = data["catalog"].entries[0]
    condition = entry.conditions[0].model_copy(
        update={"prerequisites": ["unmapped-required-source-test"]}
    )
    entry = entry.model_copy(update={"conditions": [condition, *entry.conditions[1:]]})
    data["catalog"] = rehash(data["catalog"].model_copy(update={"entries": [entry]}))
    with pytest.raises(ValueError, match="prerequisite"):
        evaluate_discovery_thesis(**data)


def test_circular_prerequisites_do_not_justify_each_other(tmp_path: Path) -> None:
    data = packet(tmp_path)
    entry = data["catalog"].entries[0]
    first, second, *rest = entry.conditions
    entry = entry.model_copy(
        update={
            "conditions": [
                first.model_copy(update={"prerequisites": [second.condition_id]}),
                second.model_copy(update={"prerequisites": [first.condition_id]}),
                *rest,
            ]
        }
    )
    data["catalog"] = rehash(data["catalog"].model_copy(update={"entries": [entry]}))
    with pytest.raises(ValueError, match="prerequisite"):
        evaluate_discovery_thesis(**data)


@pytest.mark.parametrize("role", ["event_occurred", "company_exposure"])
def test_refuting_a_negative_does_not_manufacture_positive_business_facts(
    tmp_path: Path,
    role: str,
) -> None:
    data = packet(tmp_path)
    change_claim(
        data,
        role,
        condition_state=State.NOT_SATISFIED.value,
        actuality="EXPECTED",
        exposure_kind="THEME_TAG",
    )
    data["links"] = [
        link.model_copy(update={"relation": EvidenceRelation.REFUTE})
        if link.claim_id == f"claim:{role}"
        else link
        for link in data["links"]
    ]
    assert evaluate_discovery_thesis(**data).state is State.UNKNOWN


@pytest.mark.parametrize(
    "profile",
    [
        "GENERAL_INDUSTRIAL",
        "BANK",
        "INSURANCE",
        "SECURITIES",
        "RESOURCE_MINING",
        "REAL_ESTATE",
    ],
)
def test_explicit_semantic_applicability_does_not_apply_industrial_ratios(
    tmp_path: Path,
    profile: str,
) -> None:
    data = packet(tmp_path)
    data["business_profile"] = profile
    data["catalog"] = catalog_for(Channel.EVENT, applicability=profile)
    assert evaluate_discovery_thesis(**data).state is State.SATISFIED


@pytest.mark.parametrize("bad", [[], {}, 23])
def test_malformed_exposure_field_is_unknown_not_an_unhandled_exception(
    tmp_path: Path,
    bad: Any,
) -> None:
    data = packet(tmp_path)
    change_claim(data, "company_exposure", exposure_kind=bad)
    assert evaluate_discovery_thesis(**data).state is State.UNKNOWN
