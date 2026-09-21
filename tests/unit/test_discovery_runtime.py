from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import astock.candidates.discovery_runtime as runtime_module
from astock.candidates.discovery_bridge import (
    VERIFIED_DISCOVERY_MARKER,
    DiscoverySeedCompanyContext,
)
from astock.candidates.discovery_runtime import (
    ActiveDiscoveryRegistryBinding,
    DiscoveryConditionClaimDraft,
    DiscoveryEvidenceNeed,
    DiscoveryRefreshOutcome,
    DiscoveryRuntimeCompanyContext,
    DiscoveryRuntimeService,
    VerifiedDiscoverySeedRepository,
    VerifiedDiscoverySeedService,
)
from astock.candidates.discovery_theses import (
    DiscoveryChannel,
    DiscoveryThesisResult,
    evaluate_discovery_thesis,
)
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.discovery_runtime import VerifiedDiscoverySeedRelease
from astock.schemas.documents import (
    DocumentPage,
    DocumentType,
    PageExtractionMethod,
    SourceDocument,
)
from astock.schemas.evidence import (
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceLocator,
    FactStatus,
    FetchStatus,
    SourceSnapshot,
)
from astock.schemas.knowledge_completion import (
    KnowledgeConditionState,
    KnowledgeDiscoveryCatalog,
)
from astock.schemas.market import Market
from astock.schemas.research_seeds import ResearchSeed, ResearchSeedOrigin

NOW = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def _result(
    company_id: str,
    thesis_id: str,
    *,
    state: KnowledgeConditionState = KnowledgeConditionState.SATISFIED,
    channel: DiscoveryChannel = DiscoveryChannel.EVENT,
) -> DiscoveryThesisResult:
    method = {
        DiscoveryChannel.EVENT: "EventToAlphaSkill",
        DiscoveryChannel.BOTTLENECK: "IndustryBottleneckSkill",
        DiscoveryChannel.CYCLE: "JuglarCycleStageSkill",
    }[channel]
    return DiscoveryThesisResult(
        company_id=company_id,
        family_id=f"family:{thesis_id}",
        channel=channel,
        thesis_id=thesis_id,
        state=state,
        conditions=(),
        method_refs=((method, f"condition:{thesis_id}"),),
        satisfied_families=(f"economic:{thesis_id}",)
        if state is KnowledgeConditionState.SATISFIED
        else (),
        dependency_fingerprint=("d" if state is KnowledgeConditionState.SATISFIED else "e") * 64,
    )


def _source_artifact(state: StateStore, objects: ObjectStore) -> str:
    ref = objects.put_json({"kind": "verified discovery source", "version": 1})
    artifact_id = "DiscoverySource:test"
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="DiscoverySource",
        schema_version="1.0",
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return artifact_id


def _evaluation_artifact(
    state: StateStore,
    objects: ObjectStore,
    *,
    result: DiscoveryThesisResult,
    binding: ActiveDiscoveryRegistryBinding,
    contract_catalog_hash: str,
) -> str:
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, objects)
    return service._persist_evaluation(
        context=DiscoveryRuntimeCompanyContext(
            company_id=result.company_id,
            market=Market.XSHG,
            name="Evaluation Fixture",
            industry_id="industry:X",
            business_profile="generic",
        ),
        result=result,
        binding=binding,
        catalog_object_hash="f" * 64,
        contract_catalog_hash=contract_catalog_hash,
        evidence_by_id={},
        snapshots={},
    )


def _binding(objects: ObjectStore) -> ActiveDiscoveryRegistryBinding:
    registry = objects.put_json({"kind": "active audited semantic registry"})
    return ActiveDiscoveryRegistryBinding(
        run_id="run:test",
        release_id="knowledge-semantic-admission:test",
        object_hash=registry.sha256,
    )


def test_publish_accepts_only_satisfied_theses_and_persists_immutable_release(
    state: StateStore,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(object_store)
    monkeypatch.setattr(
        runtime_module,
        "active_discovery_registry_binding",
        lambda _state, _objects: binding,
    )
    positive = _result("600001", "thesis:event")
    unknown = _result(
        "600002",
        "thesis:unknown",
        state=KnowledgeConditionState.UNKNOWN,
    )
    contract_catalog_hash = "b" * 64
    source_ids = (
        _evaluation_artifact(
            state,
            object_store,
            result=positive,
            binding=binding,
            contract_catalog_hash=contract_catalog_hash,
        ),
        _evaluation_artifact(
            state,
            object_store,
            result=unknown,
            binding=binding,
            contract_catalog_hash=contract_catalog_hash,
        ),
    )
    service = VerifiedDiscoverySeedService(state, object_store)
    contexts = (
        DiscoverySeedCompanyContext(
            company_id="600001",
            market=Market.XSHG,
            name="正例",
            source_snapshot_ids=("snapshot:a",),
        ),
        DiscoverySeedCompanyContext(
            company_id="600002",
            market=Market.XSHG,
            name="未知",
            source_snapshot_ids=("snapshot:b",),
        ),
    )

    release = service.publish(
        as_of=NOW,
        contexts=contexts,
        results=(positive, unknown),
        active_registry_hash=binding.object_hash,
        discovery_method_catalog_hash=contract_catalog_hash,
        source_artifact_ids=source_ids,
        max_verified_discovery=8,
    )

    assert [seed.company_id for seed in release.seeds] == ["600001"]
    assert release.seeds[0].origins == [ResearchSeedOrigin.EXPERT_SKILL]
    assert VERIFIED_DISCOVERY_MARKER in release.seeds[0].reason_codes
    assert any(code.startswith("DISCOVERY_THESIS:") for code in release.seeds[0].reason_codes)
    artifact_id = f"VerifiedDiscoverySeedRelease:{release.release_id}"
    record = state.artifact_record(artifact_id)
    assert record is not None
    assert object_store.verify(str(record["object_hash"]))
    checkpoint = state.get_checkpoint("verified-discovery-seeds", "latest")
    assert checkpoint is not None
    assert checkpoint["cursor"]["release_id"] == release.release_id
    assert release.seeds[0].created_at == NOW

    repeated = service.publish(
        as_of=NOW,
        contexts=contexts,
        results=(positive, unknown),
        active_registry_hash=binding.object_hash,
        discovery_method_catalog_hash=contract_catalog_hash,
        source_artifact_ids=source_ids,
        max_verified_discovery=8,
    )
    assert repeated == release
    repeated_record = state.artifact_record(
        f"VerifiedDiscoverySeedRelease:{repeated.release_id}"
    )
    assert repeated_record is not None
    assert repeated_record["object_hash"] == record["object_hash"]


def test_repository_filters_by_registry_and_as_of_and_binds_seed_to_report_input(
    state: StateStore,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(object_store)
    monkeypatch.setattr(
        runtime_module,
        "active_discovery_registry_binding",
        lambda _state, _objects: binding,
    )
    result = _result("600001", "thesis:event")
    contract_catalog_hash = "c" * 64
    source_id = _evaluation_artifact(
        state,
        object_store,
        result=result,
        binding=binding,
        contract_catalog_hash=contract_catalog_hash,
    )
    service = VerifiedDiscoverySeedService(state, object_store)
    release = service.publish(
        as_of=NOW,
        contexts=(
            DiscoverySeedCompanyContext(
                company_id="600001",
                market=Market.XSHG,
                name="Example",
            ),
        ),
        results=(result,),
        active_registry_hash=binding.object_hash,
        discovery_method_catalog_hash=contract_catalog_hash,
        source_artifact_ids=(source_id,),
        max_verified_discovery=8,
    )
    row = state.artifact_record(f"VerifiedDiscoverySeedRelease:{release.release_id}")
    assert row is not None
    release_hash = str(row["object_hash"])
    repository = VerifiedDiscoverySeedRepository(state, object_store)

    assert repository.latest_for(as_of=NOW - timedelta(seconds=1), binding=binding) is None
    latest = repository.latest_for(as_of=NOW, binding=binding)
    assert latest is not None
    assert latest[0] == release
    assert latest[1] == release_hash
    wrong = ActiveDiscoveryRegistryBinding("run:test", "other-release", binding.object_hash)
    assert repository.latest_for(as_of=NOW, binding=wrong) is None
    assert repository.seed_is_bound_to_report_inputs(release.seeds[0], [release_hash])
    merged = release.seeds[0].model_copy(
        update={
            "origins": [ResearchSeedOrigin.EXPERT_SKILL, ResearchSeedOrigin.MARKET],
            "reason_codes": sorted([*release.seeds[0].reason_codes, "LIQUID_SCALE_RESEARCH_SEED"]),
        }
    )
    assert repository.seed_is_bound_to_report_inputs(merged, [release_hash])
    forged = merged.model_copy(
        update={
            "reason_codes": [
                code for code in merged.reason_codes if code != VERIFIED_DISCOVERY_MARKER
            ]
        }
    )
    assert not repository.seed_is_bound_to_report_inputs(forged, [release_hash])
    assert not repository.seed_is_bound_to_report_inputs(release.seeds[0], ["0" * 64])


def test_publish_rejects_registered_source_without_exact_evaluation_artifact(
    state: StateStore,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(object_store)
    monkeypatch.setattr(
        runtime_module,
        "active_discovery_registry_binding",
        lambda _state, _objects: binding,
    )
    result = _result("600001", "thesis:event")
    unrelated_source = _source_artifact(state, object_store)

    with pytest.raises(ValueError, match="exact registered evaluation artifacts"):
        VerifiedDiscoverySeedService(state, object_store).publish(
            as_of=NOW,
            contexts=(
                DiscoverySeedCompanyContext(
                    company_id="600001",
                    market=Market.XSHG,
                    name="Example",
                ),
            ),
            results=(result,),
            active_registry_hash=binding.object_hash,
            discovery_method_catalog_hash="c" * 64,
            source_artifact_ids=(unrelated_source,),
            max_verified_discovery=8,
        )


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _registered_official_evidence(
    state: StateStore,
    objects: ObjectStore,
    *,
    evidence_id: str = "evidence:event",
) -> tuple[Evidence, SourceSnapshot]:
    source_ref = objects.put_bytes(b"%PDF-1.7\nfixture")
    text = "official event evidence"
    text_ref = objects.put_bytes(text.encode("utf-8"))
    excerpt_ref = objects.put_bytes(text[:8].encode("utf-8"))
    snapshot = SourceSnapshot(
        snapshot_id=f"snapshot:{evidence_id}",
        source_id="szse-official-web",
        object_sha256=source_ref.sha256,
        fetched_at=NOW - timedelta(minutes=1),
        available_to_system_at=NOW - timedelta(minutes=1),
        source_url="https://example.invalid/official.pdf",
        mime="application/pdf",
        byte_size=source_ref.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="PUBLIC_OFFICIAL_WEB",
    )
    state.register_snapshot(snapshot)
    document = SourceDocument(
        document_id=f"document:{evidence_id}",
        title="Official test disclosure",
        publisher="szse-official-web",
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=["600001"],
        published_at=NOW - timedelta(minutes=2),
        disclosure_id=f"disclosure:{evidence_id}",
        source_url="https://example.invalid/official.pdf",
        rights_status="PUBLIC_OFFICIAL_WEB",
    )
    runtime_module.DocumentRepository(state).register(document, snapshot)
    page = DocumentPage(
        page_id=f"page:{evidence_id}",
        document_id=document.document_id,
        snapshot_id=snapshot.snapshot_id,
        page_number=1,
        width_points=595.0,
        height_points=842.0,
        native_text_char_count=len(text),
        text_char_count=len(text),
        text_sha256=text_ref.sha256,
        text_object_sha256=text_ref.sha256,
        extraction_method=PageExtractionMethod.NATIVE_TEXT,
        ocr_applied=False,
        parser_name="test-parser",
        parser_version="test-parser-v1",
    )
    runtime_module.DocumentPageRepository(state).register_page(page)
    evidence = Evidence(
        evidence_id=evidence_id,
        document_id=document.document_id,
        snapshot_id=snapshot.snapshot_id,
        page_id=page.page_id,
        locator=EvidenceLocator(
            page_number=1,
            char_start=0,
            char_end=8,
            parser_version="test-parser-v1",
        ),
        excerpt_sha256=excerpt_ref.sha256,
        excerpt_object_sha256=excerpt_ref.sha256,
        evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
        fact_status=FactStatus.DIRECT,
        entity_ids=["600001"],
        available_to_system_at=snapshot.available_to_system_at,
        rights_status="PUBLIC_OFFICIAL_WEB",
    )
    runtime_module.EvidenceRepository(state).register_evidence(evidence)
    return evidence, snapshot


def _empty_catalog(registry_hash: str) -> KnowledgeDiscoveryCatalog:
    catalog = KnowledgeDiscoveryCatalog(
        run_id="runtime-adapter-test",
        registry_release_id="registry:test",
        registry_object_hash=registry_hash,
        compiler_version="runtime-adapter-test-v1",
        disposition_batch_hash="b" * 64,
        entries=[],
        member_count=0,
        result_hash="0" * 64,
    )
    return catalog.model_copy(
        update={
            "result_hash": content_hash(
                catalog.model_dump(mode="json", exclude={"result_hash"})
            )
        }
    )


def test_condition_admission_derives_contract_binding_without_duplicate_semantic_gate(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, object_store)
    evidence, snapshot = _registered_official_evidence(state, object_store)
    contract_catalog, definitions = service._method_contracts()
    definition = next(item for item in definitions if item.channel is DiscoveryChannel.EVENT)

    incomplete = service.admit_condition_claim(
        DiscoveryConditionClaimDraft(
            company_id="600001",
            industry_id="industry:X",
            channel=DiscoveryChannel.EVENT,
            role="event_occurred",
            state=KnowledgeConditionState.SATISFIED,
            subject_id="600001",
            evidence_ids=(evidence.evidence_id,),
            confidence=0.95,
            metadata={},
        ),
        as_of=NOW,
    )
    event_binding = next(item for item in definition.bindings if item.role == "event_occurred")
    event_condition = contract_catalog.condition(
        event_binding.final_skill_id,
        event_binding.condition_id,
    )
    assert event_condition is not None
    assert incomplete.claim.object_json["thesis_family_id"] == definition.family_id
    assert incomplete.claim.object_json["condition_family"] == event_condition.condition_family

    catalog = _empty_catalog("a" * 64)
    first = evaluate_discovery_thesis(
        company_id="600001",
        industry_id="industry:X",
        business_profile="GENERAL_INDUSTRIAL",
        definition=definition,
        catalog=catalog,
        active_registry_hash=catalog.registry_object_hash,
        contract_catalog=contract_catalog,
        claims=[incomplete.claim],
        links=incomplete.links,
        evidence={evidence.evidence_id: evidence},
        snapshots={snapshot.snapshot_id: snapshot},
        verify_object=object_store.verify,
    )
    event_eval = next(item for item in first.conditions if item.condition_id == "event_occurred")
    assert event_eval.state is KnowledgeConditionState.UNKNOWN
    assert "EVENT_NOT_OBSERVED_AS_OCCURRED" in event_eval.reason_codes

    complete = service.admit_condition_claim(
        DiscoveryConditionClaimDraft(
            company_id="600001",
            industry_id="industry:X",
            channel=DiscoveryChannel.EVENT,
            role="event_occurred",
            state=KnowledgeConditionState.SATISFIED,
            subject_id="600001",
            evidence_ids=(evidence.evidence_id,),
            confidence=0.95,
            metadata={"actuality": "OCCURRED"},
        ),
        as_of=NOW,
    )
    second = evaluate_discovery_thesis(
        company_id="600001",
        industry_id="industry:X",
        business_profile="GENERAL_INDUSTRIAL",
        definition=definition,
        catalog=catalog,
        active_registry_hash=catalog.registry_object_hash,
        contract_catalog=contract_catalog,
        claims=[incomplete.claim, complete.claim],
        links=[*incomplete.links, *complete.links],
        evidence={evidence.evidence_id: evidence},
        snapshots={snapshot.snapshot_id: snapshot},
        verify_object=object_store.verify,
    )
    event_eval = next(item for item in second.conditions if item.condition_id == "event_occurred")
    assert event_eval.state is KnowledgeConditionState.SATISFIED


def test_counterevidence_review_requires_inference_and_consistent_result(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, object_store)
    evidence, snapshot = _registered_official_evidence(
        state,
        object_store,
        evidence_id="evidence:counterevidence",
    )

    def draft(
        *,
        found: bool,
        claim_type: ClaimType = ClaimType.FACT,
    ) -> DiscoveryConditionClaimDraft:
        return DiscoveryConditionClaimDraft(
            company_id="600001",
            industry_id="industry:X",
            channel=DiscoveryChannel.EVENT,
            role="counterevidence_review",
            state=KnowledgeConditionState.SATISFIED,
            subject_id="600001",
            evidence_ids=(evidence.evidence_id,),
            confidence=0.9,
            metadata={"material_refutation_found": found},
            claim_type=claim_type,
        )

    with pytest.raises(ValueError, match="explicit reviewed inference"):
        service.admit_condition_claim(draft(found=False), as_of=NOW)
    with pytest.raises(ValueError, match="cannot report a material refutation"):
        service.admit_condition_claim(
            draft(found=True, claim_type=ClaimType.INFERENCE),
            as_of=NOW,
        )

    bundle = service.admit_condition_claim(
        draft(found=False, claim_type=ClaimType.INFERENCE),
        as_of=NOW,
    )
    assert bundle.claim.claim_type is ClaimType.INFERENCE
    assert bundle.claim.object_json["material_refutation_found"] is False

    contract_catalog, definitions = service._method_contracts()
    definition = next(item for item in definitions if item.channel is DiscoveryChannel.EVENT)
    catalog = _empty_catalog("a" * 64)
    evaluated = evaluate_discovery_thesis(
        company_id="600001",
        industry_id="industry:X",
        business_profile="GENERAL_INDUSTRIAL",
        definition=definition,
        catalog=catalog,
        active_registry_hash=catalog.registry_object_hash,
        contract_catalog=contract_catalog,
        claims=[bundle.claim],
        links=bundle.links,
        evidence={evidence.evidence_id: evidence},
        snapshots={snapshot.snapshot_id: snapshot},
        verify_object=object_store.verify,
    )
    counter = next(
        item for item in evaluated.conditions if item.condition_id == "counterevidence_review"
    )
    assert counter.state is KnowledgeConditionState.SATISFIED


def test_condition_admission_does_not_store_unknown_as_a_verified_claim(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, object_store)
    evidence, _ = _registered_official_evidence(
        state,
        object_store,
        evidence_id="evidence:unknown",
    )
    with pytest.raises(ValueError, match="verified positive/negative"):
        service.admit_condition_claim(
            DiscoveryConditionClaimDraft(
                company_id="600001",
                industry_id="industry:X",
                channel=DiscoveryChannel.EVENT,
                role="event_occurred",
                state=KnowledgeConditionState.UNKNOWN,
                subject_id="600001",
                evidence_ids=(evidence.evidence_id,),
                confidence=0.5,
                metadata={},
            ),
            as_of=NOW,
        )


def test_release_schema_rejects_generic_expert_skill_without_verified_marker() -> None:
    generic = ResearchSeed(
        seed_id="generic",
        company_id="600001",
        market=Market.XSHG,
        name="Generic",
        origins=[ResearchSeedOrigin.EXPERT_SKILL],
        research_priority_score=0.75,
        reason_codes=["EXPERT_SKILL_DOMAIN_RESEARCH_SEED"],
    )

    with pytest.raises(ValueError, match="exact verified marker"):
        VerifiedDiscoverySeedRelease(
            release_id="bad",
            as_of=NOW,
            active_registry_release_id="registry:test",
            active_registry_object_hash="a" * 64,
            discovery_method_catalog_hash="b" * 64,
            seeds=[generic],
        )


def test_refresh_checkpoint_is_recovery_required_while_public_evidence_is_missing(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    binding = _binding(object_store)
    catalog_ref = object_store.put_json({"kind": "catalog"})
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, object_store)
    outcome = DiscoveryRefreshOutcome(
        release=None,
        results=(),
        evidence_needs=(
            DiscoveryEvidenceNeed(
                company_id="600001",
                channel="EVENT",
                role="event_occurred",
                thesis_family_id="reviewed-method:event",
                research_question="Did the event occur?",
                existing_capabilities=("CORPORATE_ACTIONS",),
                preferred_authorities=("ISSUER_IR", "EXCHANGE_OFFICIAL"),
            ),
        ),
        evaluation_artifact_ids=(),
        finding_codes=(),
    )

    service._persist_refresh(
        outcome,
        binding,
        catalog_ref.sha256,
        "b" * 64,
        NOW,
    )

    checkpoint = state.get_checkpoint("verified-discovery-refresh", "latest")
    assert checkpoint is not None
    assert checkpoint["status"] == "RECOVERY_REQUIRED"
    assert checkpoint["cursor"]["evidence_need_count"] == 1


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("condition_state", "SATISFIED"),
        ("thesis_family_id", "injected:other-thesis"),
        ("condition_family", "injected:other-condition"),
    ],
)
def test_condition_metadata_cannot_override_canonical_binding(
    state: StateStore,
    object_store: ObjectStore,
    field: str,
    override: str,
) -> None:
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, object_store)
    evidence, _ = _registered_official_evidence(state, object_store)
    before = service.evidence.claim_bundles_for_subject("600001")
    with pytest.raises(ValueError, match="canonical discovery"):
        service.admit_condition_claim(
            DiscoveryConditionClaimDraft(
                company_id="600001",
                industry_id="industry:X",
                channel=DiscoveryChannel.EVENT,
                role="event_occurred",
                state=KnowledgeConditionState.NOT_SATISFIED,
                subject_id="600001",
                evidence_ids=(evidence.evidence_id,),
                confidence=0.9,
                metadata={"actuality": "OCCURRED", field: override},
            ),
            as_of=NOW,
        )
    assert service.evidence.claim_bundles_for_subject("600001") == before


def test_condition_metadata_preserves_matching_binding_and_normal_extensions(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    service = DiscoveryRuntimeService(PROJECT_ROOT, state, object_store)
    evidence, _ = _registered_official_evidence(state, object_store)
    catalog, definitions = service._method_contracts()
    definition = next(item for item in definitions if item.channel is DiscoveryChannel.EVENT)
    binding = next(item for item in definition.bindings if item.role == "event_occurred")
    condition = catalog.condition(binding.final_skill_id, binding.condition_id)
    assert condition is not None
    draft = DiscoveryConditionClaimDraft(
        company_id="600001",
        industry_id="industry:X",
        channel=DiscoveryChannel.EVENT,
        role="event_occurred",
        state=KnowledgeConditionState.SATISFIED,
        subject_id="600001",
        evidence_ids=(evidence.evidence_id,),
        confidence=0.9,
        metadata={
            "actuality": "OCCURRED",
            "condition_state": "SATISFIED",
            "thesis_family_id": definition.family_id,
            "condition_family": condition.condition_family,
            "review_note": "正文已核验；此字段不是发布权限",
        },
    )
    first = service.admit_condition_claim(draft, as_of=NOW)
    second = service.admit_condition_claim(draft, as_of=NOW)
    assert first == second
    assert first.claim.object_json["actuality"] == "OCCURRED"
    assert first.claim.object_json["condition_state"] == "SATISFIED"
    assert first.claim.object_json["review_note"] == "正文已核验；此字段不是发布权限"
