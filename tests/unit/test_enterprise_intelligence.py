from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from astock.documents.page_repository import DocumentPageRepository
from astock.documents.repository import DocumentRepository
from astock.evidence.repository import EvidenceRepository
from astock.research.enterprise_intelligence import EnterpriseIntelligenceService
from astock.schemas.documents import (
    DocumentPage,
    DocumentType,
    PageExtractionMethod,
    SourceDocument,
)
from astock.schemas.enterprise_intelligence import (
    EnterpriseEventType,
    EnterpriseIntelligenceObservation,
    EnterpriseRelationScope,
    EnterpriseResolutionStatus,
)
from astock.schemas.evidence import (
    Evidence,
    EvidenceGrade,
    EvidenceLocator,
    FactStatus,
    SourceSnapshot,
)
from astock.schemas.full_research import EventEvidenceClass
from astock.schemas.market import SourceClass

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _observation(
    state,
    objects,
    *,
    observation_id: str,
    source_id: str,
    source_class: SourceClass,
    fact_value: str,
    source_independence_group: str | None = None,
    event_type: EnterpriseEventType = EnterpriseEventType.PUBLIC_OFFICE_APPOINTMENT,
    evidence_grade: EvidenceGrade | None = None,
    fact_status: FactStatus = FactStatus.DIRECT,
) -> EnterpriseIntelligenceObservation:
    snapshot_ref = objects.put_bytes(f"snapshot:{source_id}:{observation_id}:{fact_value}".encode())
    snapshot = SourceSnapshot(
        snapshot_id=f"snapshot:{observation_id}",
        source_id=source_id,
        object_sha256=snapshot_ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        source_url=f"https://example.invalid/{observation_id}",
        mime="text/html",
        byte_size=snapshot_ref.byte_size,
        rights_status="TEST",
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    page_text = f"evidence:{source_id}:{fact_value}"
    page_ref = objects.put_bytes(page_text.encode())
    document = SourceDocument(
        document_id=f"document:{observation_id}",
        title=f"Enterprise intelligence {observation_id}",
        publisher=source_id,
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=["600001"],
        published_at=NOW,
        effective_at=NOW,
        disclosure_id=f"disclosure:{observation_id}",
        source_url=f"https://example.invalid/{observation_id}",
        rights_status="TEST",
        created_at=NOW,
    )
    DocumentRepository(state).register(document, snapshot)
    page = DocumentPage(
        page_id=f"page:{observation_id}",
        document_id=document.document_id,
        snapshot_id=snapshot.snapshot_id,
        page_number=1,
        width_points=100,
        height_points=100,
        native_text_char_count=len(page_text),
        text_char_count=len(page_text),
        text_sha256=page_ref.sha256,
        text_object_sha256=page_ref.sha256,
        extraction_method=PageExtractionMethod.NATIVE_TEXT,
        ocr_applied=False,
        parser_name="test-parser",
        parser_version="test-v1",
        created_at=NOW,
    )
    DocumentPageRepository(state).register_page(page)
    excerpt = objects.put_bytes(page_text[:8].encode())
    evidence = Evidence(
        evidence_id=f"evidence:{observation_id}",
        document_id=document.document_id,
        snapshot_id=snapshot.snapshot_id,
        page_id=page.page_id,
        locator=EvidenceLocator(
            page_number=1,
            char_start=0,
            char_end=8,
            parser_version="test-v1",
            created_at=NOW,
        ),
        excerpt_sha256=excerpt.sha256,
        excerpt_object_sha256=excerpt.sha256,
        evidence_grade=evidence_grade or (
            EvidenceGrade.PRIMARY_OFFICIAL
            if source_class is SourceClass.PRIMARY_OFFICIAL_WEB
            else EvidenceGrade.SECONDARY
        ),
        fact_status=fact_status,
        entity_ids=["600001", "XSHG:600001"],
        available_to_system_at=NOW,
        rights_status="TEST",
        created_at=NOW,
    )
    EvidenceRepository(state).register_evidence(evidence)
    return EnterpriseIntelligenceObservation(
        observation_id=observation_id,
        company_id="600001",
        instrument_id="XSHG:600001",
        event_type=event_type,
        relation_scope=EnterpriseRelationScope.PUBLIC_OFFICE,
        event_date=date(2026, 9, 12),
        fact_key="person:zhang:public-office",
        fact_value=fact_value,
        summary="关键人员任职变化",
        person_names=("张某",),
        source_id=source_id,
        source_class=source_class,
        source_independence_group=source_independence_group,
        evidence_id=evidence.evidence_id,
        source_snapshot_id=snapshot.snapshot_id,
        evidence_object_hash=excerpt.sha256,
        available_to_system_at=NOW + timedelta(minutes=1),
        created_at=NOW,
    )


def test_cross_source_same_fact_confirms_and_projects_typed_event(state, object_store) -> None:
    objects = object_store
    official = _observation(
        state,
        objects,
        observation_id="official",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="某市副市长",
    )
    secondary = _observation(
        state,
        objects,
        observation_id="secondary",
        source_id="qcc-enterprise-intelligence-mcp",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        fact_value="某市副市长",
    )
    service = EnterpriseIntelligenceService(state, objects)

    resolution = service.resolve([secondary, official], resolved_at=NOW + timedelta(minutes=2))
    event = service.project_news_event(resolution, [secondary, official])

    assert resolution.status is EnterpriseResolutionStatus.CONFIRMED
    assert resolution.cross_source_confirmed
    assert resolution.preferred_observation_id == official.observation_id
    assert not resolution.investigation_required
    assert event.category == "key_personnel_external_appointment"
    assert event.person_names == ("张某",)
    assert event.relation_scope == "PUBLIC_OFFICE"
    assert event.evidence_class is EventEvidenceClass.OFFICIAL_FILING
    assert event.confidence == 1


def test_material_cross_source_conflict_never_silently_overwrites(state, object_store) -> None:
    objects = object_store
    official = _observation(
        state,
        objects,
        observation_id="official-conflict",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="某市副市长",
    )
    secondary = _observation(
        state,
        objects,
        observation_id="secondary-conflict",
        source_id="tianyancha-enterprise-intelligence-api",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        fact_value="某市市长",
    )
    service = EnterpriseIntelligenceService(state, objects)

    resolution = service.resolve([official, secondary], resolved_at=NOW + timedelta(minutes=2))

    assert resolution.status is EnterpriseResolutionStatus.CONFLICTED
    assert resolution.investigation_required
    assert resolution.preferred_observation_id is None
    assert "MATERIAL_CROSS_SOURCE_FACT_CONFLICT" in resolution.conflict_reason_codes
    with pytest.raises(ValueError, match="conflicted"):
        service.project_news_event(resolution, [official, secondary])


def test_commercial_source_alone_remains_secondary_and_has_no_recommendation_authority(
    state,
    object_store,
) -> None:
    objects = object_store
    observation = _observation(
        state,
        objects,
        observation_id="qcc-only",
        source_id="qcc-enterprise-intelligence-mcp",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        fact_value="新任法定代表人",
        event_type=EnterpriseEventType.LEGAL_REPRESENTATIVE_CHANGE,
    )
    service = EnterpriseIntelligenceService(state, objects)
    resolution = service.resolve([observation], resolved_at=NOW + timedelta(minutes=2))
    event = service.project_news_event(resolution, [observation])

    assert resolution.status is EnterpriseResolutionStatus.CONFIRMED
    assert not resolution.cross_source_confirmed
    assert not resolution.recommendation_allowed
    assert event.evidence_class is EventEvidenceClass.SECONDARY_STRUCTURED
    assert event.category == "business_registration"
    assert event.confidence < 1


def test_default_resolution_is_idempotent_for_same_current_observations(
    state,
    object_store,
) -> None:
    observation = _observation(
        state,
        object_store,
        observation_id="idempotent-current",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="某市副市长",
    )
    service = EnterpriseIntelligenceService(state, object_store)

    first = service.resolve([observation])
    second = service.resolve([observation])

    assert first == second
    assert first.resolved_at == observation.available_to_system_at


def test_administrative_penalty_reuses_current_investigation_event_category(
    state,
    object_store,
) -> None:
    observation = _observation(
        state,
        object_store,
        observation_id="administrative-penalty",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="行政处罚决定",
        event_type=EnterpriseEventType.ADMINISTRATIVE_PENALTY,
    )
    service = EnterpriseIntelligenceService(state, object_store)
    resolution = service.resolve([observation], resolved_at=NOW + timedelta(minutes=2))
    event = service.project_news_event(resolution, [observation])

    assert event.category == "investigation"
    assert event.evidence_class is EventEvidenceClass.OFFICIAL_FILING
    assert not resolution.recommendation_allowed


def test_resolution_rejects_observation_not_yet_available_at_resolution_time(
    state,
    object_store,
) -> None:
    objects = object_store
    observation = _observation(
        state,
        objects,
        observation_id="future-observation",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="某市副市长",
    )
    service = EnterpriseIntelligenceService(state, objects)

    with pytest.raises(ValueError, match="future observations"):
        service.resolve([observation], resolved_at=NOW)


def test_resolution_identity_distinguishes_resolution_times(state, object_store) -> None:
    observation = _observation(
        state,
        object_store,
        observation_id="resolution-time",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="某市副市长",
    )
    service = EnterpriseIntelligenceService(state, object_store)

    first = service.resolve([observation], resolved_at=NOW + timedelta(minutes=2))
    second = service.resolve([observation], resolved_at=NOW + timedelta(minutes=3))

    assert first.resolution_id != second.resolution_id
    assert state.artifact_record(
        f"EnterpriseIntelligenceResolution:{first.resolution_id}"
    ) is not None
    assert state.artifact_record(
        f"EnterpriseIntelligenceResolution:{second.resolution_id}"
    ) is not None

def test_same_vendor_mcp_and_api_do_not_count_as_independent_confirmation(
    state,
    object_store,
) -> None:
    mcp_observation = _observation(
        state,
        object_store,
        observation_id="qcc-mcp-same-vendor",
        source_id="qcc-enterprise-intelligence-mcp",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        fact_value="某市副市长",
        source_independence_group="QCC_MCP_CALLER_LABEL",
    )
    api_observation = _observation(
        state,
        object_store,
        observation_id="qcc-api-same-vendor",
        source_id="qcc-enterprise-intelligence-api",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        fact_value="某市副市长",
        source_independence_group="QCC_API_CALLER_LABEL",
    )
    service = EnterpriseIntelligenceService(state, object_store)
    resolution = service.resolve(
        [mcp_observation, api_observation],
        resolved_at=NOW + timedelta(minutes=2),
    )
    assert resolution.status is EnterpriseResolutionStatus.CONFIRMED
    assert not resolution.cross_source_confirmed
    event = service.project_news_event(
        resolution,
        [mcp_observation, api_observation],
    )
    assert event.confidence < 1


def test_event_projection_requires_the_exact_registered_resolution_observations(
    state,
    object_store,
) -> None:
    official = _observation(
        state,
        object_store,
        observation_id="projection-official",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_value="某市副市长",
    )
    secondary = _observation(
        state,
        object_store,
        observation_id="projection-secondary",
        source_id="tianyancha-enterprise-intelligence-api",
        source_class=SourceClass.SECONDARY_STRUCTURED,
        fact_value="某市副市长",
    )
    service = EnterpriseIntelligenceService(state, object_store)
    resolution = service.resolve(
        [official, secondary],
        resolved_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="observations do not match resolution"):
        service.project_news_event(resolution, [official])



def test_caller_cannot_promote_secondary_evidence_to_official_event(state, object_store) -> None:
    observation = _observation(
        state, object_store, observation_id="grade-spoof",
        source_id="qcc-enterprise-intelligence-mcp",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        evidence_grade=EvidenceGrade.SECONDARY, fact_value="某市副市长",
    )
    service = EnterpriseIntelligenceService(state, object_store)
    resolution = service.resolve([observation])
    event = service.project_news_event(resolution, [observation])
    assert event.evidence_class is EventEvidenceClass.SECONDARY_STRUCTURED
    assert not resolution.recommendation_allowed


def test_spoofed_secondary_does_not_displace_canonical_official(state, object_store) -> None:
    official = _observation(
        state, object_store, observation_id="a-official-real",
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB, fact_value="同一任命",
    )
    secondary = _observation(
        state, object_store, observation_id="z-secondary-spoof",
        source_id="qcc-enterprise-intelligence-api",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        evidence_grade=EvidenceGrade.SECONDARY, fact_value="同一任命",
    )
    service = EnterpriseIntelligenceService(state, object_store)
    result = service.resolve([secondary, official])
    assert result.preferred_observation_id == official.observation_id
    assert service.project_news_event(result, [secondary, official]).evidence_class is (
        EventEvidenceClass.OFFICIAL_FILING
    )


@pytest.mark.parametrize(
    "fact_status", [FactStatus.UNVERIFIED, FactStatus.CONFLICTED, FactStatus.INFERRED]
)
def test_non_direct_evidence_remains_for_investigation_not_confirmed_events(
    state, object_store, fact_status,
) -> None:
    observation = _observation(
        state, object_store, observation_id="non-direct-" + fact_status.value,
        source_id="government-official-web",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        fact_status=fact_status, fact_value="待核实任命",
    )
    service = EnterpriseIntelligenceService(state, object_store)
    with pytest.raises(ValueError, match="direct.*Evidence"):
        service.resolve([observation])
    assert service.evidence.get_evidence(observation.evidence_id) is not None
    assert state.artifact_record(
        f"EnterpriseIntelligenceObservation:{observation.observation_id}"
    ) is None


@pytest.mark.parametrize("grade", [EvidenceGrade.COMMUNITY_LEAD, EvidenceGrade.PRIVATE_PRIMARY])
def test_non_public_fact_grades_cannot_be_laundered_as_enterprise_facts(
    state, object_store, grade,
) -> None:
    observation = _observation(
        state, object_store, observation_id="lead-" + grade.value,
        source_id="community-test",
        source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        evidence_grade=grade, fact_value="待核实人事消息",
    )
    service = EnterpriseIntelligenceService(state, object_store)
    with pytest.raises(ValueError, match="direct.*Evidence"):
        service.resolve([observation])
    assert service.evidence.get_evidence(observation.evidence_id) is not None


def test_one_source_cannot_claim_independent_confirmation_using_caller_groups(
    state, object_store,
) -> None:
    observations = [
        _observation(
            state, object_store, observation_id=f"same-source-{index}",
            source_id="government-official-web",
            source_class=SourceClass.SECONDARY_STRUCTURED, fact_value="同一任命",
            source_independence_group=f"caller-group-{index}",
        )
        for index in range(2)
    ]
    service = EnterpriseIntelligenceService(state, object_store)
    resolution = service.resolve(observations)
    assert not resolution.cross_source_confirmed
    assert service.project_news_event(resolution, observations).confidence < 1


def test_legacy_resolution_cannot_reintroduce_single_source_confirmation(
    state, object_store,
) -> None:
    observations = [
        _observation(
            state, object_store, observation_id=f"legacy-source-{index}",
            source_id="government-official-web",
            source_class=SourceClass.SECONDARY_STRUCTURED, fact_value="同一任命",
            source_independence_group=f"legacy-group-{index}",
        )
        for index in range(2)
    ]
    service = EnterpriseIntelligenceService(state, object_store)
    current = service.resolve(observations)
    legacy = current.model_copy(update={
        "resolution_id": "enterprise-resolution:legacy-overconfirmed",
        "cross_source_confirmed": True,
    })
    ref = object_store.put_json(legacy.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=f"EnterpriseIntelligenceResolution:{legacy.resolution_id}",
        artifact_type="EnterpriseIntelligenceResolution",
        schema_version=legacy.schema_version, object_hash=ref.sha256, input_hashes=[],
    )
    assert service.project_news_event(legacy, observations).confidence < 1
    assert service.resolve(observations) == current
