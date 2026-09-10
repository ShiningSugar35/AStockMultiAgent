from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from astock.core.object_store import ObjectStore
from astock.documents import DocumentPageRepository, DocumentRepository, PdfParseService
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.research import (
    ResearchCoreService,
    ResearchSkillService,
    load_research_core_config,
    load_research_skill_registry,
)
from astock.research.serenity.compiler import SerenityInputCompiler
from astock.schemas import (
    BASE_CASE_SECTIONS,
    AdjustmentDirection,
    AdjustmentMode,
    AvailabilityBasis,
    BarRequest,
    BaseCaseBuildRequest,
    BaseCaseDraft,
    ClaimStatus,
    ClaimType,
    DocumentType,
    EvidenceAttachment,
    EvidenceFreezeRequest,
    EvidenceGrade,
    EvidenceRelation,
    FactStatus,
    FetchStatus,
    Frequency,
    Market,
    MarketBar,
    PointInTimeStatus,
    QualityStatus,
    ResearchCoverageStatus,
    ResearchFindingInput,
    ResearchFindingType,
    SourceDocument,
    SourceSnapshot,
    SpecialistAdjustmentInput,
    SpecialistCoverageStatus,
    SpecialistDeltaBuildRequest,
    SpecialistEligibility,
    SpecialistMetricInput,
    SpecialistRouteRequest,
    TimestampSemantics,
    VolumeUnit,
)
from astock.schemas.serenity.compiler import CanonicalDailyTrendCompileRequest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATEMENT = "Synthetic cited BaseCase statement that must stay out of SQLite."


class _CanonicalDailyFixtureStore:
    def __init__(self, manifest: dict[str, object], bars: list[MarketBar]) -> None:
        self.manifest = manifest
        self.bars = bars

    def load_manifest(self, request: BarRequest) -> dict[str, object] | None:
        del request
        return self.manifest

    def read_bars(self, request: BarRequest) -> list[MarketBar]:
        del request
        return self.bars


def _fixture(
    tmp_path: Path,
    state,
    *,
    suffix: str,
    evidence_grade: EvidenceGrade = EvidenceGrade.PRIMARY_OFFICIAL,
    pit_status: PointInTimeStatus = PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
    conflict: bool = False,
    available_at: datetime | None = None,
    additional_evidence_grade: EvidenceGrade | None = None,
):
    text = "Revenue grew. Demand may weaken."
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = pdf.tobytes()
    pdf.close()
    objects = ObjectStore(tmp_path / f"objects-{suffix}")
    raw = objects.put_bytes(pdf_bytes)
    available = available_at or datetime(2026, 1, 10, tzinfo=UTC)
    snapshot = SourceSnapshot(
        snapshot_id=f"snapshot:research:{suffix}",
        source_id=f"source:research:{suffix}",
        object_sha256=raw.sha256,
        fetched_at=available,
        available_to_system_at=available,
        mime="application/pdf",
        byte_size=raw.byte_size,
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="TEST_FIXTURE",
    )
    state.register_snapshot(snapshot)
    document = SourceDocument(
        document_id=f"document:research:{suffix}",
        title="Synthetic research fixture",
        publisher="TEST",
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=["company:000001"],
        published_at=available,
        effective_at=available,
        disclosure_id=f"disclosure:research:{suffix}",
        source_url=f"https://example.invalid/{suffix}.pdf",
        rights_status="TEST_FIXTURE",
    )
    documents = DocumentRepository(state)
    documents.register(document, snapshot)
    pages = DocumentPageRepository(state)
    parse = PdfParseService(objects, state, pages).parse(
        document,
        snapshot,
        ocr_enabled=False,
    )
    claim_service = ClaimEvidenceService(
        objects,
        state,
        pages,
        documents,
        EvidenceRepository(state),
    )

    def evidence(phrase: str):
        start = text.index(phrase)
        return claim_service.create_page_evidence(
            page_id=parse.page_ids[0],
            char_start=start,
            char_end=start + len(phrase),
            evidence_grade=evidence_grade,
            fact_status=FactStatus.DIRECT,
            entity_ids=["company:000001"],
        )

    support = evidence("Revenue grew.")
    attachments = [
        EvidenceAttachment(
            evidence_id=support.evidence_id,
            relation=EvidenceRelation.SUPPORT,
        )
    ]
    if additional_evidence_grade is not None:
        start = text.index("Revenue grew.")
        additional_support = claim_service.create_page_evidence(
            page_id=parse.page_ids[0],
            char_start=start,
            char_end=start + len("Revenue grew."),
            evidence_grade=additional_evidence_grade,
            fact_status=FactStatus.DIRECT,
            entity_ids=["company:000001"],
        )
        attachments.append(
            EvidenceAttachment(
                evidence_id=additional_support.evidence_id,
                relation=EvidenceRelation.SUPPORT,
            )
        )
    if conflict:
        refute = evidence("Demand may weaken.")
        attachments.append(
            EvidenceAttachment(
                evidence_id=refute.evidence_id,
                relation=EvidenceRelation.REFUTE,
            )
        )
    bundle = claim_service.create_claim(
        subject_id="company:000001",
        predicate=f"research_fixture_{suffix}",
        object_json={"value": "synthetic"},
        as_of=available + timedelta(seconds=1),
        claim_type=ClaimType.FACT,
        confidence=0.8,
        status=ClaimStatus.VALIDATED,
        attachments=attachments,
    )
    basis = (
        AvailabilityBasis.PROVIDER_CURRENT_VALUE
        if pit_status is PointInTimeStatus.NOT_PIT_SAFE
        else AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP
    )
    PointInTimeService(PointInTimeRepository(state), state, objects).create(
        source_id=f"pit-source:research:{suffix}",
        source_document_id=document.document_id,
        source_snapshot_id=snapshot.snapshot_id,
        published_at=available,
        effective_at=available,
        ingested_at=available,
        available_to_system_at=available,
        point_in_time_status=pit_status,
        availability_basis=basis,
    )
    service = ResearchCoreService(
        state,
        objects,
        load_research_core_config(PROJECT_ROOT / "configs" / "research_core.yaml"),
    )
    return service, objects, bundle, support, available


def _draft(as_of: datetime, evidence_id: str, *, critical: bool = True) -> BaseCaseDraft:
    return BaseCaseDraft(
        company_id="company:000001",
        as_of=as_of,
        findings_by_section={
            section: [
                ResearchFindingInput(
                    statement=f"{_STATEMENT} {section.value}",
                    finding_type=ResearchFindingType.VERIFIED_FACT,
                    confidence=0.8,
                    critical=critical,
                    evidence_ids=[evidence_id],
                )
            ]
            for section in BASE_CASE_SECTIONS
        },
        evidence_gaps=[],
        specialist_tags=["industrial"],
        requested_base_confidence=0.85,
    )


def _specialist_fixture(
    tmp_path: Path,
    state,
    *,
    suffix: str,
    evidence_grade: EvidenceGrade = EvidenceGrade.PRIMARY_OFFICIAL,
    pit_status: PointInTimeStatus = PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
    conflict: bool = False,
    additional_evidence_grade: EvidenceGrade | None = None,
):
    core, objects, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix=suffix,
        evidence_grade=evidence_grade,
        pit_status=pit_status,
        conflict=conflict,
        additional_evidence_grade=additional_evidence_grade,
    )
    as_of = available + timedelta(seconds=2)
    frozen = core.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
            allow_approximated=pit_status is PointInTimeStatus.APPROXIMATED,
        )
    )
    base = core.build_base_case(
        BaseCaseBuildRequest(
            evidence_pack_id=frozen.pack.pack_id,
            draft=_draft(
                as_of,
                evidence.evidence_id,
                critical=evidence_grade is EvidenceGrade.PRIMARY_OFFICIAL,
            ),
        )
    )
    skills = ResearchSkillService(
        state,
        objects,
        load_research_skill_registry(PROJECT_ROOT / "configs" / "research_skills.yaml"),
    )
    return skills, base.pack, evidence


def _canonical_daily_fixture(as_of: datetime, *, count: int = 200):
    start = as_of - timedelta(days=count - 1)
    bars = [
        MarketBar(
            observation_id=f"canonical-d1:{index}",
            provider_id="canonical-test",
            symbol="000001",
            market=Market.XSHE,
            frequency=Frequency.D1,
            timestamp=start + timedelta(days=index),
            timestamp_semantics=TimestampSemantics.DATE_ONLY,
            open=Decimal(index + 1),
            high=Decimal(index + 1),
            low=Decimal(index + 1),
            close=Decimal(index + 1),
            volume=Decimal("1000"),
            volume_unit=VolumeUnit.SHARE,
            adjustment_mode=AdjustmentMode.NONE,
        )
        for index in range(count)
    ]
    manifest: dict[str, object] = {
        "market": Market.XSHE.value,
        "symbol": "000001",
        "frequency": Frequency.D1.value,
        "adjustment_mode": AdjustmentMode.NONE.value,
        "quality_status": QualityStatus.PASS.value,
        "quality_report_id": "quality:canonical-d1-test",
        "content_hash": "a" * 64,
        "actual_end": as_of.isoformat(),
    }
    return _CanonicalDailyFixtureStore(manifest, bars), bars


def test_serenity_compiler_builds_exact_canonical_daily_moving_averages(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="serenity-compiler-daily",
    )
    evidence_pack = skills.repository.get_evidence_pack(base_case.evidence_pack_id)
    assert evidence_pack is not None
    store, bars = _canonical_daily_fixture(base_case.as_of)
    request = CanonicalDailyTrendCompileRequest(
        target_company_id=base_case.company_id,
        symbol="000001",
        market=Market.XSHE,
        as_of=base_case.as_of,
        requested_start=bars[0].timestamp,
        adjustment_mode=AdjustmentMode.NONE,
        daily_evidence_ids=[evidence.evidence_id],
    )

    contract = SerenityInputCompiler(
        state,
        skills.object_store,
        store,
    ).compile_daily_trend(request, evidence_pack=evidence_pack)

    assert [item.window for item in contract.moving_averages] == [20, 50, 100, 200]
    assert [item.value for item in contract.moving_averages] == [
        Decimal("190.5"),
        Decimal("175.5"),
        Decimal("150.5"),
        Decimal("100.5"),
    ]
    assert all(
        item.calculation_status == "CANONICAL_DETERMINISTIC"
        for item in contract.moving_averages
    )
    assert contract.daily_series.dataset_version == "a" * 64
    assert contract.daily_series.quality_report_id == "quality:canonical-d1-test"


def test_serenity_compiler_rejects_short_future_or_nonpassing_daily_inputs(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="serenity-compiler-invalid-daily",
    )
    evidence_pack = skills.repository.get_evidence_pack(base_case.evidence_pack_id)
    assert evidence_pack is not None
    store, bars = _canonical_daily_fixture(base_case.as_of, count=199)
    request = CanonicalDailyTrendCompileRequest(
        target_company_id=base_case.company_id,
        symbol="000001",
        market=Market.XSHE,
        as_of=base_case.as_of,
        requested_start=bars[0].timestamp,
        daily_evidence_ids=[evidence.evidence_id],
    )
    compiler = SerenityInputCompiler(state, skills.object_store, store)
    with pytest.raises(ValueError, match="at least 200"):
        compiler.compile_daily_trend(request, evidence_pack=evidence_pack)

    full_store, full_bars = _canonical_daily_fixture(base_case.as_of)
    request = request.model_copy(update={"requested_start": full_bars[0].timestamp})
    full_store.manifest["actual_end"] = (base_case.as_of + timedelta(days=1)).isoformat()
    compiler = SerenityInputCompiler(state, skills.object_store, full_store)
    with pytest.raises(ValueError, match="future bars"):
        compiler.compile_daily_trend(request, evidence_pack=evidence_pack)

    full_store.manifest["actual_end"] = base_case.as_of.isoformat()
    full_store.manifest["quality_status"] = QualityStatus.FAIL.value
    with pytest.raises(ValueError, match="quality gate"):
        compiler.compile_daily_trend(request, evidence_pack=evidence_pack)


def test_serenity_compiler_reuses_the_same_pit_evidence_gate(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, evidence = _specialist_fixture(
        tmp_path,
        state,
        suffix="serenity-compiler-pit",
    )
    evidence_pack = skills.repository.get_evidence_pack(base_case.evidence_pack_id)
    assert evidence_pack is not None
    store, bars = _canonical_daily_fixture(base_case.as_of)
    request = CanonicalDailyTrendCompileRequest(
        target_company_id=base_case.company_id,
        symbol="000001",
        market=Market.XSHE,
        as_of=base_case.as_of,
        requested_start=bars[0].timestamp,
        daily_evidence_ids=[evidence.evidence_id],
    )
    invalid_pack = evidence_pack.model_copy(
        update={
            "pit_status_by_evidence_id": {
                evidence.evidence_id: PointInTimeStatus.APPROXIMATED,
            }
        }
    )

    with pytest.raises(ValueError, match="certified or reconstructed PIT"):
        SerenityInputCompiler(state, skills.object_store, store).compile_daily_trend(
            request,
            evidence_pack=invalid_pack,
        )


def test_frozen_evidence_and_base_case_are_idempotent_cited_and_private(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix="complete",
    )
    as_of = available + timedelta(seconds=2)
    freeze_request = EvidenceFreezeRequest(
        company_id="company:000001",
        as_of=as_of,
        claim_ids=[bundle.claim.claim_id],
        formal_historical=True,
    )
    frozen = service.freeze_evidence(freeze_request)
    repeated_frozen = service.freeze_evidence(freeze_request)
    assert frozen == repeated_frozen
    assert frozen.pack.coverage_status is ResearchCoverageStatus.COMPLETE

    request = BaseCaseBuildRequest(
        evidence_pack_id=frozen.pack.pack_id,
        draft=_draft(as_of, evidence.evidence_id),
    )
    base = service.build_base_case(request)
    repeated_base = service.build_base_case(request)
    audit = service.audit("company:000001")
    assert base == repeated_base
    assert base.pack.coverage_status is ResearchCoverageStatus.COMPLETE
    assert base.pack.base_confidence == 0.85
    assert base.pack.evidence_ids == [evidence.evidence_id]
    assert audit["status"] == "PASS"
    assert audit["finding_count"] == len(BASE_CASE_SECTIONS)
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM frozen_evidence_pack_index"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM base_case_pack_index"
        ).fetchone()[0] == 1
        safe_metadata = "\n".join(
            str(value)
            for table in ("frozen_evidence_pack_index", "base_case_pack_index")
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert _STATEMENT not in safe_metadata
    assert "Synthetic cited BaseCase" not in safe_metadata


def test_open_conflict_is_preserved_as_blocking_gap_and_caps_confidence(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix="conflict",
        conflict=True,
    )
    as_of = available + timedelta(seconds=2)
    frozen = service.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
        )
    )
    assert frozen.pack.open_conflict_ids
    base = service.build_base_case(
        BaseCaseBuildRequest(
            evidence_pack_id=frozen.pack.pack_id,
            draft=_draft(as_of, evidence.evidence_id),
        )
    )
    assert base.pack.coverage_status is ResearchCoverageStatus.INSUFFICIENT
    assert base.pack.confidence_cap == 0.4
    assert base.pack.base_confidence == 0.4
    assert any(
        gap.gap_code.startswith("OPEN_EVIDENCE_CONFLICT:")
        for gap in base.pack.evidence_gaps
    )


def test_critical_finding_cannot_rely_only_on_community_evidence(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, evidence, available = _fixture(
        tmp_path,
        state,
        suffix="community",
        evidence_grade=EvidenceGrade.COMMUNITY_LEAD,
    )
    as_of = available + timedelta(seconds=2)
    frozen = service.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
        )
    )
    with pytest.raises(ValueError, match="PRIMARY_OFFICIAL"):
        service.build_base_case(
            BaseCaseBuildRequest(
                evidence_pack_id=frozen.pack.pack_id,
                draft=_draft(as_of, evidence.evidence_id),
            )
        )


def test_formal_freeze_rejects_not_pit_safe_and_future_claims(
    tmp_path: Path,
    state,
) -> None:
    service, _, bundle, _, available = _fixture(
        tmp_path,
        state,
        suffix="not-pit-safe",
        pit_status=PointInTimeStatus.NOT_PIT_SAFE,
    )
    with pytest.raises(ValueError, match="not allowed"):
        service.freeze_evidence(
            EvidenceFreezeRequest(
                company_id="company:000001",
                as_of=available + timedelta(seconds=2),
                claim_ids=[bundle.claim.claim_id],
                formal_historical=True,
            )
        )
    with pytest.raises(ValueError, match="future claim"):
        service.freeze_evidence(
            EvidenceFreezeRequest(
                company_id="company:000001",
                as_of=available,
                claim_ids=[bundle.claim.claim_id],
                formal_historical=False,
            )
        )


def test_base_case_rejects_evidence_outside_frozen_scope(tmp_path: Path, state) -> None:
    service, _, bundle, _, available = _fixture(
        tmp_path,
        state,
        suffix="scope",
    )
    as_of = available + timedelta(seconds=2)
    frozen = service.freeze_evidence(
        EvidenceFreezeRequest(
            company_id="company:000001",
            as_of=as_of,
            claim_ids=[bundle.claim.claim_id],
        )
    )
    with pytest.raises(ValueError, match="outside the frozen pack"):
        service.build_base_case(
            BaseCaseBuildRequest(
                evidence_pack_id=frozen.pack.pack_id,
                draft=_draft(as_of, "evidence:not-frozen"),
            )
        )


def test_specialist_router_is_deterministic_family_bounded_and_explicitly_degraded(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, _ = _specialist_fixture(tmp_path, state, suffix="route")
    request = SpecialistRouteRequest(
        base_case_id=base_case.base_case_id,
        thesis_tags=["growth", "valuation", "industry", "event", "trend"],
        industry_tags=["semiconductor"],
        event_tags=["policy"],
        horizon="medium",
        available_inputs=[
            "financial_evidence",
            "valuation_inputs",
            "industry_evidence",
            "event_evidence",
            "daily_market_quality",
        ],
        available_frequencies=["1d"],
    )
    first = skills.route(request)
    repeated = skills.route(request)
    assert first == repeated
    assert len(first.plan.selected) == 2
    assert all(item.skill_id != "ResearchMemoComposer" for item in first.plan.selected)
    assert first.plan.coverage_status is SpecialistCoverageStatus.PARTIAL
    assert "CONSENSUS_UNAVAILABLE" in first.plan.degradation_codes
    assert any(
        reason == "SOURCE_FAMILY_LIMIT:SERENITY:2"
        for reasons in first.plan.excluded_skill_reasons.values()
        for reason in reasons
    )
    assert first.plan.excluded_skill_reasons["ResearchMemoComposer"] == [
        "NON_SPECIALIST_COMPOSER"
    ]


def test_specialist_router_keeps_source_family_limit_above_resource_default(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, _ = _specialist_fixture(tmp_path, state, suffix="route-budget-four")
    plan = skills.route(
        SpecialistRouteRequest(
            base_case_id=base_case.base_case_id,
            thesis_tags=["growth", "valuation", "industry", "event", "trend"],
            industry_tags=["semiconductor"],
            event_tags=["policy"],
            horizon="medium",
            available_inputs=[
                "financial_evidence",
                "valuation_inputs",
                "industry_evidence",
                "event_evidence",
                "daily_market_quality",
            ],
            available_frequencies=["1d"],
            specialist_budget=4,
        )
    ).plan

    assert plan.max_specialists == 4
    assert len(plan.selected) == 2
    assert any(
        reason == "SOURCE_FAMILY_LIMIT:SERENITY:2"
        for reasons in plan.excluded_skill_reasons.values()
        for reason in reasons
    )


def test_specialist_router_rejects_explicit_serenity_family_overflow(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, _ = _specialist_fixture(tmp_path, state, suffix="route-family-overflow")
    with pytest.raises(ValueError, match="source family limit"):
        skills.route(
            SpecialistRouteRequest(
                base_case_id=base_case.base_case_id,
                thesis_tags=[],
                industry_tags=[],
                event_tags=[],
                horizon="medium",
                available_inputs=[],
                available_frequencies=[],
                explicit_skill_ids=[
                    "IndustryBottleneckSkill",
                    "EventToAlphaSkill",
                    "GrowthProbabilitySkill",
                ],
            )
        )


def test_specialist_router_rejects_explicit_duplicate_selection_group(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, _ = _specialist_fixture(tmp_path, state, suffix="route-overlap")
    registry = skills.configured_registry.model_copy(
        update={
            "registry_version": "research-skills-selection-overlap-test-v1",
            "open_source_audit_manifest_files": [],
            "open_source_local_adaptation_release_file": None,
            "skills": [
                item.model_copy(update={"selection_group": "GROWTH_STACK"})
                if item.skill_id in {"GrowthProbabilitySkill", "GrowthValuationLens"}
                else item
                for item in skills.configured_registry.skills
            ],
        }
    )
    overlap_service = ResearchSkillService(
        skills.state,
        skills.object_store,
        registry,
    )
    with pytest.raises(ValueError, match="duplicate selection groups"):
        overlap_service.route(
            SpecialistRouteRequest(
                base_case_id=base_case.base_case_id,
                thesis_tags=[],
                industry_tags=[],
                event_tags=[],
                horizon="medium",
                available_inputs=[],
                available_frequencies=[],
                explicit_skill_ids=["GrowthProbabilitySkill", "GrowthValuationLens"],
            )
        )


def test_specialist_router_rejects_explicit_overflow_and_reports_missing_hourly_data(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, _ = _specialist_fixture(tmp_path, state, suffix="route-gaps")
    with pytest.raises(ValueError, match="active resource budget"):
        skills.route(
            SpecialistRouteRequest(
                base_case_id=base_case.base_case_id,
                thesis_tags=[],
                industry_tags=[],
                event_tags=[],
                horizon="medium",
                available_inputs=[],
                available_frequencies=[],
                explicit_skill_ids=[
                    "IndustryBottleneckSkill",
                    "EventToAlphaSkill",
                    "GrowthProbabilitySkill",
                    "GrowthValuationLens",
                ],
            )
        )

    route = skills.route(
        SpecialistRouteRequest(
            base_case_id=base_case.base_case_id,
            thesis_tags=["swing"],
            industry_tags=[],
            event_tags=[],
            horizon="short",
            available_inputs=["hourly_market_quality"],
            available_frequencies=["1d"],
        )
    ).plan
    assert route.coverage_status is SpecialistCoverageStatus.INSUFFICIENT
    assert route.selected == []
    assert len(route.unavailable) == 1
    assert route.unavailable[0].skill_id == "HourlySwingSkill"
    assert route.unavailable[0].eligibility is SpecialistEligibility.UNAVAILABLE
    assert route.unavailable[0].missing_required_frequencies == ["60m"]
    assert "FREQUENCY_UNAVAILABLE" in route.unavailable[0].reason_codes


def test_specialist_delta_is_incremental_cited_idempotent_and_private(
    tmp_path: Path,
    state,
) -> None:
    skills, base_case, evidence = _specialist_fixture(tmp_path, state, suffix="delta")
    route = skills.route(
        SpecialistRouteRequest(
            base_case_id=base_case.base_case_id,
            thesis_tags=["swing"],
            industry_tags=[],
            event_tags=[],
            horizon="short",
            available_inputs=["hourly_market_quality"],
            available_frequencies=["60m"],
            explicit_skill_ids=["HourlySwingSkill"],
        )
    ).plan
    request = SpecialistDeltaBuildRequest(
        base_case_id=base_case.base_case_id,
        route_plan_id=route.route_plan_id,
        skill_id="HourlySwingSkill",
        skill_version="hourly-swing-v1",
        incremental_findings=[
            ResearchFindingInput(
                statement="Synthetic incremental bottleneck finding kept out of SQLite.",
                finding_type=ResearchFindingType.ANALYST_INFERENCE,
                confidence=0.7,
                critical=True,
                evidence_ids=[evidence.evidence_id],
            )
        ],
        base_case_corrections=[],
        industry_specific_metrics=[
            SpecialistMetricInput(
                metric_name="synthetic scarcity",
                value=0.5,
                unit="ratio",
                evidence_ids=[evidence.evidence_id],
            )
        ],
        additional_evidence_requests=[],
        failure_modes=["synthetic_failure_mode"],
        confidence_delta=0.05,
        valuation_adjustments=[
            SpecialistAdjustmentInput(
                dimension="growth_duration",
                direction=AdjustmentDirection.DECREASE,
                magnitude=0.1,
                rationale="Synthetic valuation rationale kept out of SQLite.",
                evidence_ids=[evidence.evidence_id],
            )
        ],
        risk_adjustments=[],
        coverage_delta={},
    )
    first = skills.build_delta(request)
    repeated = skills.build_delta(request)
    assert first == repeated
    assert first.delta.evidence_ids == [evidence.evidence_id]
    assert skills.audit(base_case.base_case_id)["status"] == "PASS"
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM specialist_delta_index"
        ).fetchone()[0] == 1
        safe_metadata = "\n".join(
            str(value)
            for table in (
                "research_skill_registry_index",
                "specialist_route_plan_index",
                "specialist_delta_index",
            )
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert "Synthetic incremental bottleneck" not in safe_metadata
    assert "Synthetic valuation rationale" not in safe_metadata

    invalid = request.model_copy(
        update={
            "incremental_findings": [
                request.incremental_findings[0].model_copy(
                    update={"evidence_ids": ["evidence:not-frozen"]}
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="outside the frozen pack"):
        skills.build_delta(invalid)


def test_delta_from_unselected_skill_is_rejected(tmp_path: Path, state) -> None:
    skills, base_case, _ = _specialist_fixture(tmp_path, state, suffix="delta-unselected")
    route = skills.route(
        SpecialistRouteRequest(
            base_case_id=base_case.base_case_id,
            thesis_tags=["industry"],
            industry_tags=[],
            event_tags=[],
            horizon="long",
            available_inputs=["industry_evidence"],
            available_frequencies=[],
        )
    ).plan
    with pytest.raises(ValueError, match="selected Skill version"):
        skills.build_delta(
            SpecialistDeltaBuildRequest(
                base_case_id=base_case.base_case_id,
                route_plan_id=route.route_plan_id,
                skill_id="EventToAlphaSkill",
                skill_version="event-to-alpha-v1",
                incremental_findings=[],
                base_case_corrections=[],
                industry_specific_metrics=[],
                additional_evidence_requests=[],
                failure_modes=[],
                confidence_delta=0,
                valuation_adjustments=[],
                risk_adjustments=[],
                coverage_delta={},
            )
        )
