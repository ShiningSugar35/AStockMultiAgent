from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents.page_repository import DocumentPageRepository
from astock.documents.repository import DocumentRepository
from astock.evidence import EvidenceRepository
from astock.research.institutional import InstitutionalResearchService
from astock.schemas.documents import (
    DocumentPage,
    DocumentType,
    PageExtractionMethod,
    SourceDocument,
)
from astock.schemas.evidence import (
    Claim,
    ClaimEvidenceBundle,
    ClaimEvidenceLink,
    ClaimStatus,
    ClaimType,
    Evidence,
    EvidenceGrade,
    EvidenceLocator,
    EvidenceRelation,
    FactStatus,
    ReviewerStatus,
    SourceSnapshot,
)
from astock.schemas.institutional_research import (
    CompanyArchetype,
    CompanyEconomicsDraft,
    CompanySegmentEconomics,
    DriverAssumptionProvenance,
    DriverHistoricalPoint,
    DriverInputValue,
    DriverNode,
    DriverOperation,
    DriverTreeDraft,
    EvidenceAuthorityTier,
    EvidenceBoundStatement,
    EvidenceSufficiencyRequest,
    EvidenceSufficiencyState,
    ForecastPack,
    ForecastScenario,
    ForecastScenarioInput,
    ForecastTemplate,
    IndustryProfileDraft,
    InstitutionalArtifactStatus,
    InstitutionalClaimType,
    InstitutionalDecisionContextBuildRequest,
    InstitutionalDecisionContextDraft,
    InstitutionalResearchFinalizeRequest,
    MarketPriceAnchor,
    SourceEpistemicMetadata,
    TaxonomyStatus,
    ValuationMethod,
    ValuationPack,
    ValuationScenarioAssumption,
)
from astock.schemas.pit import PointInTimeStatus
from astock.schemas.research import FrozenEvidencePack, ResearchCoverageStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 12, 0, tzinfo=UTC)
COMPANY = "600001"


def _runtime(tmp_path: Path) -> tuple[StateStore, ObjectStore, InstitutionalResearchService]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    return state, objects, InstitutionalResearchService(state, objects)


def _register_claim_with_evidence(
    state: StateStore,
    objects: ObjectStore,
    *,
    claim_id: str,
    evidence_id: str,
    source_id: str,
    grade: EvidenceGrade = EvidenceGrade.PRIMARY_OFFICIAL,
    fact_status: FactStatus = FactStatus.DIRECT,
    relation: EvidenceRelation = EvidenceRelation.SUPPORT,
    reviewer: ReviewerStatus = ReviewerStatus.HUMAN_APPROVED,
) -> tuple[str, str]:
    source_ref = objects.put_json({"source": source_id, "evidence_id": evidence_id})
    snapshot = SourceSnapshot(
        snapshot_id=f"snapshot:{evidence_id}",
        source_id=source_id,
        object_sha256=source_ref.sha256,
        fetched_at=NOW,
        available_to_system_at=NOW,
        mime="application/json",
        byte_size=source_ref.byte_size,
        created_at=NOW,
    )
    state.register_snapshot(snapshot)
    page_text = f"evidence:{evidence_id}"
    page_ref = objects.put_bytes(page_text.encode())
    document = SourceDocument(
        document_id=f"document:{evidence_id}",
        title=f"Test evidence {evidence_id}",
        publisher="TEST",
        document_type=DocumentType.ANNOUNCEMENT,
        company_ids=[COMPANY],
        published_at=NOW,
        effective_at=NOW,
        disclosure_id=f"disclosure:{evidence_id}",
        source_url="https://example.invalid/test",
        rights_status="TEST",
        created_at=NOW,
    )
    DocumentRepository(state).register(document, snapshot)
    page = DocumentPage(
        page_id=f"page:{evidence_id}",
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
    excerpt_ref = objects.put_bytes(page_text[:8].encode())
    evidence = Evidence(
        evidence_id=evidence_id,
        document_id=document.document_id,
        snapshot_id=snapshot.snapshot_id,
        page_id=f"page:{evidence_id}",
        locator=EvidenceLocator(
            page_number=1,
            char_start=0,
            char_end=8,
            parser_version="test-v1",
            created_at=NOW,
        ),
        excerpt_sha256=excerpt_ref.sha256,
        excerpt_object_sha256=excerpt_ref.sha256,
        evidence_grade=grade,
        fact_status=fact_status,
        entity_ids=[COMPANY],
        available_to_system_at=NOW,
        rights_status="TEST",
        created_at=NOW,
    )
    repo = EvidenceRepository(state)
    repo.register_evidence(evidence)
    claim = Claim(
        claim_id=claim_id,
        subject_id=COMPANY,
        predicate="test_predicate",
        object_json={"value": evidence_id},
        as_of=NOW,
        claim_type=ClaimType.FACT,
        confidence=1,
        status=ClaimStatus.VALIDATED,
        created_at=NOW,
    )
    repo.register_claim_bundle(
        ClaimEvidenceBundle(
            claim=claim,
            links=[
                ClaimEvidenceLink(
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    relation=relation,
                    reviewer_status=reviewer,
                    created_at=NOW,
                )
            ],
            created_at=NOW,
        )
    )
    return evidence_id, snapshot.snapshot_id


def _register_frozen_pack(
    state: StateStore,
    objects: ObjectStore,
    *,
    claim_ids: list[str],
    evidence_ids: list[str],
    grades: dict[str, EvidenceGrade] | None = None,
    pit_status: PointInTimeStatus = PointInTimeStatus.CERTIFIED,
) -> str:
    grades = grades or {evidence_id: EvidenceGrade.PRIMARY_OFFICIAL for evidence_id in evidence_ids}
    pack = FrozenEvidencePack(
        pack_id="frozen:test:" + content_hash({"claims": claim_ids, "evidence": evidence_ids}),
        company_id=COMPANY,
        as_of=NOW,
        formal_historical=True,
        allow_approximated=False,
        claim_ids=claim_ids,
        evidence_ids=evidence_ids,
        conflict_ids=[],
        open_conflict_ids=[],
        evidence_grade_by_id=grades,
        pit_id_by_evidence_id={evidence_id: f"pit:{evidence_id}" for evidence_id in evidence_ids},
        pit_status_by_evidence_id={evidence_id: pit_status for evidence_id in evidence_ids},
        missing_pit_evidence_ids=[],
        coverage_status=ResearchCoverageStatus.COMPLETE,
        degradation_codes=[],
        frozen_input_sha256="1" * 64,
        frozen_at=NOW,
        created_at=NOW,
    )
    ref = objects.put_json(pack.model_dump(mode="json"))
    artifact_id = f"FrozenEvidencePack:{pack.pack_id}"
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="FrozenEvidencePack",
        schema_version=pack.schema_version,
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return artifact_id


def _statement(claim_id: str, evidence_id: str, text: str = "supported") -> EvidenceBoundStatement:
    return EvidenceBoundStatement(
        statement=text,
        claim_ids=[claim_id],
        evidence_ids=[evidence_id],
        created_at=NOW,
    )


def _driver_tree(claim_id: str, evidence_id: str) -> DriverTreeDraft:
    nodes = [
        DriverNode(node_id="capex", label="Capex", operation=DriverOperation.INPUT, unit="CNY"),
        DriverNode(node_id="da", label="D&A", operation=DriverOperation.INPUT, unit="CNY"),
        DriverNode(
            node_id="margin",
            label="Operating margin",
            operation=DriverOperation.INPUT,
            unit="ratio",
        ),
        DriverNode(node_id="price", label="Price", operation=DriverOperation.INPUT, unit="CNY"),
        DriverNode(
            node_id="revenue",
            label="Revenue",
            operation=DriverOperation.MULTIPLY,
            input_node_ids=["units", "price"],
            unit="CNY",
        ),
        DriverNode(node_id="tax", label="Tax rate", operation=DriverOperation.INPUT, unit="ratio"),
        DriverNode(node_id="units", label="Units", operation=DriverOperation.INPUT, unit="unit"),
        DriverNode(
            node_id="wc",
            label="Change working capital",
            operation=DriverOperation.INPUT,
            unit="CNY",
        ),
    ]
    nodes = sorted(nodes, key=lambda item: item.node_id)
    historical = [
        DriverHistoricalPoint(
            node_id=node_id,
            period_end=date(2026, 6, 30),
            value=Decimal("1"),
            claim_ids=[claim_id],
            evidence_ids=[evidence_id],
            created_at=NOW,
        )
        for node_id in ("capex", "da", "margin", "price", "tax", "units", "wc")
    ]
    return DriverTreeDraft(
        forecast_template=ForecastTemplate.OPERATING_FCFF,
        nodes=nodes,
        output_bindings={
            "REVENUE": "revenue",
            "OPERATING_MARGIN": "margin",
            "TAX_RATE": "tax",
            "D_AND_A": "da",
            "CAPEX": "capex",
            "CHANGE_WORKING_CAPITAL": "wc",
        },
        historical_points=historical,
        created_at=NOW,
    )


def _forecast_inputs(
    claim_id: str,
    evidence_id: str,
) -> list[ForecastScenarioInput]:
    periods = (date(2027, 12, 31), date(2028, 12, 31))
    base_values = {
        "units": Decimal("10"),
        "price": Decimal("10"),
        "margin": Decimal("0.20"),
        "tax": Decimal("0.25"),
        "da": Decimal("5"),
        "capex": Decimal("4"),
        "wc": Decimal("1"),
    }
    scenario_scale = {
        ForecastScenario.BEAR: Decimal("0.90"),
        ForecastScenario.BASE: Decimal("1.00"),
        ForecastScenario.BULL: Decimal("1.10"),
    }
    result: list[ForecastScenarioInput] = []
    for scenario in ForecastScenario:
        values: list[DriverInputValue] = []
        for period in periods:
            for node_id in sorted(base_values):
                value = base_values[node_id]
                if node_id in {"units", "price"}:
                    value *= scenario_scale[scenario]
                values.append(
                    DriverInputValue(
                        node_id=node_id,
                        period_end=period,
                        value=value,
                        provenance=DriverAssumptionProvenance.EVIDENCE,
                        claim_ids=[claim_id],
                        evidence_ids=[evidence_id],
                        provenance_note="frozen evidence-backed test assumption",
                        created_at=NOW,
                    )
                )
        result.append(
            ForecastScenarioInput(
                scenario=scenario,
                input_values=sorted(values, key=lambda item: (item.node_id, item.period_end)),
                created_at=NOW,
            )
        )
    return result


def _valuation_inputs(claim_id: str, evidence_id: str) -> list[ValuationScenarioAssumption]:
    values = {
        ForecastScenario.BEAR: (Decimal("0.11"), Decimal("0.02")),
        ForecastScenario.BASE: (Decimal("0.10"), Decimal("0.03")),
        ForecastScenario.BULL: (Decimal("0.09"), Decimal("0.04")),
    }
    return [
        ValuationScenarioAssumption(
            scenario=scenario,
            method=ValuationMethod.DCF_FCFF,
            discount_rate=values[scenario][0],
            terminal_growth=values[scenario][1],
            shares_outstanding=Decimal("10"),
            assumption_claim_ids=[claim_id],
            assumption_evidence_ids=[evidence_id],
            assumption_note="evidence-bound valuation assumption",
            created_at=NOW,
        )
        for scenario in ForecastScenario
    ]


def _finalize_request(
    pack_artifact: str,
    claim_id: str,
    evidence_id: str,
    *,
    market_price_anchor: MarketPriceAnchor | None = None,
) -> InstitutionalResearchFinalizeRequest:
    statement = _statement(claim_id, evidence_id)
    return InstitutionalResearchFinalizeRequest(
        company_id=COMPANY,
        as_of=NOW,
        evidence_sufficiency=EvidenceSufficiencyRequest(
            frozen_evidence_pack_artifact_id=pack_artifact,
            material_claim_ids=[claim_id],
            created_at=NOW,
        ),
        industry_profile=IndustryProfileDraft(
            industry_id="industry:test",
            industry_name="测试行业",
            taxonomy_status=TaxonomyStatus.PROVISIONAL,
            definition=statement,
            market_size_growth=statement,
            industry_profitability=statement,
            market_share_structure=statement,
            supply_capacity_demand=statement,
            pricing_mechanism=statement,
            competitive_dynamics=statement,
            regulation_external_drivers=statement,
            created_at=NOW,
        ),
        company_economics=CompanyEconomicsDraft(
            archetype=CompanyArchetype.STABLE_OPERATING,
            archetype_basis=statement,
            segments=[
                CompanySegmentEconomics(
                    segment_id="segment:1",
                    segment_name="核心业务",
                    business_model=statement,
                    revenue_driver=statement,
                    pricing_driver=statement,
                    margin_driver=statement,
                    created_at=NOW,
                )
            ],
            pricing_power=statement,
            customer_concentration=statement,
            supplier_dependency=statement,
            competitive_position=statement,
            management_governance=statement,
            capital_allocation=statement,
            reinvestment_roic=statement,
            funding_dilution=statement,
            created_at=NOW,
        ),
        driver_tree=_driver_tree(claim_id, evidence_id),
        forecast_scenarios=_forecast_inputs(claim_id, evidence_id),
        valuation_archetype=CompanyArchetype.STABLE_OPERATING,
        market_price_anchor=market_price_anchor,
        valuation_scenarios=_valuation_inputs(claim_id, evidence_id),
        valuation_invalidation_conditions=["evidence-backed driver breaks"],
        created_at=NOW,
    )


def test_same_source_republication_does_not_satisfy_two_source_independence(tmp_path: Path) -> None:
    state, objects, service = _runtime(tmp_path)
    claim_id = "claim:industry-estimate"
    e1, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim_id,
        evidence_id="evidence:1",
        source_id="secondary-provider",
        grade=EvidenceGrade.SECONDARY,
    )
    repo = EvidenceRepository(state)
    e2, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id="claim:temporary",
        evidence_id="evidence:2",
        source_id="secondary-provider",
        grade=EvidenceGrade.SECONDARY,
    )
    # The combined claim below points to two immutable evidence items whose snapshots share
    # one source-independence group; duplicate republication must not count twice.
    combined_claim = Claim(
        claim_id="claim:industry-combined",
        subject_id=COMPANY,
        predicate="industry_estimate",
        object_json={"value": "estimate"},
        as_of=NOW,
        claim_type=ClaimType.INFERENCE,
        confidence=1,
        status=ClaimStatus.VALIDATED,
        created_at=NOW,
    )
    repo.register_claim_bundle(
        ClaimEvidenceBundle(
            claim=combined_claim,
            links=[
                ClaimEvidenceLink(
                    claim_id=combined_claim.claim_id,
                    evidence_id=e1,
                    relation=EvidenceRelation.SUPPORT,
                    reviewer_status=ReviewerStatus.HUMAN_APPROVED,
                    created_at=NOW,
                ),
                ClaimEvidenceLink(
                    claim_id=combined_claim.claim_id,
                    evidence_id=e2,
                    relation=EvidenceRelation.SUPPORT,
                    reviewer_status=ReviewerStatus.HUMAN_APPROVED,
                    created_at=NOW,
                ),
            ],
            created_at=NOW,
        )
    )
    pack = _register_frozen_pack(
        state,
        objects,
        claim_ids=[combined_claim.claim_id],
        evidence_ids=sorted([e1, e2]),
        grades={e1: EvidenceGrade.SECONDARY, e2: EvidenceGrade.SECONDARY},
    )
    report = service.run_evidence_sufficiency(
        EvidenceSufficiencyRequest(
            frozen_evidence_pack_artifact_id=pack,
            material_claim_ids=[combined_claim.claim_id],
            claim_type_overrides={
                combined_claim.claim_id: InstitutionalClaimType.INDUSTRY_ESTIMATE
            },
            created_at=NOW,
        )
    )
    assessment = report.assessments[0]
    assert assessment.state is EvidenceSufficiencyState.INSUFFICIENT
    assert assessment.support_independence_groups == ["secondary-provider"]
    assert report.status.value == "NEEDS_INFO"


def test_community_evidence_cannot_be_upgraded_to_statutory_authority(tmp_path: Path) -> None:
    state, objects, service = _runtime(tmp_path)
    evidence_id, snapshot_id = _register_claim_with_evidence(
        state,
        objects,
        claim_id="claim:community",
        evidence_id="evidence:community",
        source_id="zhihu:author",
        grade=EvidenceGrade.COMMUNITY_LEAD,
    )
    pack = _register_frozen_pack(
        state,
        objects,
        claim_ids=["claim:community"],
        evidence_ids=[evidence_id],
        grades={evidence_id: EvidenceGrade.COMMUNITY_LEAD},
    )
    with pytest.raises(ValueError, match="authority tier"):
        service.run_evidence_sufficiency(
            EvidenceSufficiencyRequest(
                frozen_evidence_pack_artifact_id=pack,
                material_claim_ids=["claim:community"],
                source_metadata=[
                    SourceEpistemicMetadata(
                        snapshot_id=snapshot_id,
                        authority_tier=EvidenceAuthorityTier.A_STATUTORY_PRIMARY,
                        source_independence_group="forged-official",
                        system_ingested_at=NOW,
                        created_at=NOW,
                    )
                ],
                created_at=NOW,
            )
        )


def test_non_pit_safe_evidence_never_supports_material_claim(tmp_path: Path) -> None:
    state, objects, service = _runtime(tmp_path)
    evidence_id, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id="claim:not-pit",
        evidence_id="evidence:not-pit",
        source_id="cninfo-disclosures:test",
    )
    pack = _register_frozen_pack(
        state,
        objects,
        claim_ids=["claim:not-pit"],
        evidence_ids=[evidence_id],
        pit_status=PointInTimeStatus.NOT_PIT_SAFE,
    )
    report = service.run_evidence_sufficiency(
        EvidenceSufficiencyRequest(
            frozen_evidence_pack_artifact_id=pack,
            material_claim_ids=["claim:not-pit"],
            created_at=NOW,
        )
    )
    assert report.assessments[0].state is EvidenceSufficiencyState.INSUFFICIENT


def test_driver_tree_rejects_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        from astock.research.fundamental_analytics import topological_order

        topological_order(
            [
                DriverNode(
                    node_id="a",
                    label="A",
                    operation=DriverOperation.ADD,
                    input_node_ids=["b", "c"],
                    unit="x",
                ),
                DriverNode(
                    node_id="b",
                    label="B",
                    operation=DriverOperation.ADD,
                    input_node_ids=["a", "c"],
                    unit="x",
                ),
                DriverNode(node_id="c", label="C", operation=DriverOperation.INPUT, unit="x"),
            ]
        )


def test_non_fcff_template_cannot_carry_fcff_bridge() -> None:
    from astock.schemas.institutional_research import ForecastPeriod

    with pytest.raises(ValidationError, match="non-FCFF"):
        ForecastPeriod(
            period_end=date(2027, 12, 31),
            template=ForecastTemplate.FINANCIAL_RESIDUAL_INCOME,
            metrics={
                "BOOK_VALUE": Decimal("100"),
                "NET_INCOME": Decimal("10"),
                "ROE": Decimal("0.1"),
            },
            revenue=Decimal("100"),
            evaluated_nodes={"book": Decimal("100")},
        )


def test_full_finalize_recalculates_fcff_dcf_sensitivity_and_audits(tmp_path: Path) -> None:
    state, objects, service = _runtime(tmp_path)
    claim_id = "claim:core"
    evidence_id, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim_id,
        evidence_id="evidence:core",
        source_id="cninfo-disclosures:test",
    )
    pack = _register_frozen_pack(
        state,
        objects,
        claim_ids=[claim_id],
        evidence_ids=[evidence_id],
    )
    bundle = service.finalize(_finalize_request(pack, claim_id, evidence_id))
    assert bundle.status.value == "READY"
    assert "PROVISIONAL_TAXONOMY" in bundle.warning_codes
    forecast_id = bundle.forecast_pack_artifact_id
    forecast_record = state.artifact_record(forecast_id)
    assert forecast_record is not None
    forecast = ForecastPack.model_validate_json(
        objects.get_bytes(str(forecast_record["object_hash"]))
    )
    base = next(item for item in forecast.scenarios if item.scenario is ForecastScenario.BASE)
    assert base.periods[0].revenue == Decimal("100")
    assert base.periods[0].fcff == Decimal("15")
    valuation_record = state.artifact_record(bundle.valuation_pack_artifact_id)
    assert valuation_record is not None
    valuation = ValuationPack.model_validate_json(
        objects.get_bytes(str(valuation_record["object_hash"]))
    )
    assert len(valuation.sensitivity_table) == 9
    assert not valuation.scenario_prices_are_targets
    implied_scale = next(
        item
        for item in valuation.market_implied_expectations
        if item.expectation == "IMPLIED_FCFF_SCALE"
    )
    assert implied_scale.status is InstitutionalArtifactStatus.NEEDS_INFO
    assert implied_scale.reason_codes == ["MARKET_PRICE_ANCHOR_REQUIRED"]
    assert service.audit(f"FundamentalModelBundle:{bundle.bundle_id}")["status"] == "PASS"


def test_market_price_anchor_is_required_for_market_implied_expectations(
    tmp_path: Path,
) -> None:
    state, objects, service = _runtime(tmp_path)
    claim_id = "claim:price-anchor"
    evidence_id, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim_id,
        evidence_id="evidence:price-anchor",
        source_id="cninfo-disclosures:test",
    )
    pack = _register_frozen_pack(
        state,
        objects,
        claim_ids=[claim_id],
        evidence_ids=[evidence_id],
    )
    price_ref = objects.put_json(
        {"instrument_id": "XSHG:600001", "price": "10", "as_of": NOW.isoformat()}
    )
    price_artifact = "market-reference:institutional-price-anchor"
    state.register_artifact(
        artifact_id=price_artifact,
        artifact_type="MarketReferenceRelease",
        schema_version="1.0",
        object_hash=price_ref.sha256,
        input_hashes=[],
    )
    anchor = MarketPriceAnchor(
        price=Decimal("10"),
        observed_at=NOW,
        available_to_system_at=NOW,
        source_artifact_id=price_artifact,
        source_object_hash=price_ref.sha256,
        created_at=NOW,
    )
    bundle = service.finalize(
        _finalize_request(
            pack,
            claim_id,
            evidence_id,
            market_price_anchor=anchor,
        )
    )
    valuation_record = state.artifact_record(bundle.valuation_pack_artifact_id)
    assert valuation_record is not None
    valuation = ValuationPack.model_validate_json(
        objects.get_bytes(str(valuation_record["object_hash"]))
    )
    assert valuation.market_price_anchor == anchor
    assert price_artifact in valuation.source_artifact_ids
    assert price_ref.sha256 in valuation.source_object_hashes
    assert all(item.expected_return is not None for item in valuation.results)
    implied_scale = next(
        item
        for item in valuation.market_implied_expectations
        if item.expectation == "IMPLIED_FCFF_SCALE"
    )
    assert implied_scale.status is InstitutionalArtifactStatus.READY


def test_institutional_decision_context_binds_variant_perception_and_key_drivers(
    tmp_path: Path,
) -> None:
    state, objects, service = _runtime(tmp_path)
    claim_id = "claim:context"
    evidence_id, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim_id,
        evidence_id="evidence:context",
        source_id="cninfo-disclosures:test",
    )
    pack = _register_frozen_pack(state, objects, claim_ids=[claim_id], evidence_ids=[evidence_id])
    bundle = service.finalize(_finalize_request(pack, claim_id, evidence_id))
    statement = _statement(claim_id, evidence_id)
    context = service.build_decision_context(
        InstitutionalDecisionContextBuildRequest(
            company_id=COMPANY,
            as_of=NOW,
            fundamental_model_bundle_artifact_id=f"FundamentalModelBundle:{bundle.bundle_id}",
            draft=InstitutionalDecisionContextDraft(
                decision_question="What must be true for this research case to work?",
                decision_horizon_end=date(2028, 12, 31),
                investment_thesis=statement,
                variant_perception=statement,
                key_driver_ids=["margin", "price", "units"],
                competing_hypotheses=[statement],
                portfolio_context=(
                    "standalone research context; portfolio sizing remains downstream"
                ),
                created_at=NOW,
            ),
            created_at=NOW,
        )
    )
    artifact_id = f"InstitutionalDecisionContext:{context.context_id}"
    assert context.draft.key_driver_ids == ["margin", "price", "units"]
    bundle_record = state.artifact_record(f"FundamentalModelBundle:{bundle.bundle_id}")
    assert bundle_record is not None
    assert context.fundamental_model_bundle_object_hash == bundle_record["object_hash"]
    assert service.audit(artifact_id)["status"] == "PASS"
    with pytest.raises(ValueError, match="unknown driver"):
        service.build_decision_context(
            InstitutionalDecisionContextBuildRequest(
                company_id=COMPANY,
                as_of=NOW,
                fundamental_model_bundle_artifact_id=f"FundamentalModelBundle:{bundle.bundle_id}",
                draft=InstitutionalDecisionContextDraft(
                    decision_question="invalid",
                    decision_horizon_end=date(2028, 12, 31),
                    investment_thesis=statement,
                    variant_perception=statement,
                    key_driver_ids=["margin", "price", "unknown"],
                    competing_hypotheses=[statement],
                    created_at=NOW,
                ),
                created_at=NOW,
            )
        )


def test_bundle_audit_detects_component_registry_hash_drift(tmp_path: Path) -> None:
    state, objects, service = _runtime(tmp_path)
    claim_id = "claim:audit"
    evidence_id, _ = _register_claim_with_evidence(
        state,
        objects,
        claim_id=claim_id,
        evidence_id="evidence:audit",
        source_id="cninfo-disclosures:test",
    )
    pack = _register_frozen_pack(state, objects, claim_ids=[claim_id], evidence_ids=[evidence_id])
    bundle = service.finalize(_finalize_request(pack, claim_id, evidence_id))
    with state.transaction() as connection:
        connection.execute(
            "UPDATE artifact_registry SET object_hash=? WHERE artifact_id=?",
            ("f" * 64, bundle.forecast_pack_artifact_id),
        )
    audit = service.audit(f"FundamentalModelBundle:{bundle.bundle_id}")
    assert audit["status"] == "FAIL"
    assert "FUNDAMENTAL_COMPONENT_LINEAGE_DRIFT" in audit["finding_codes"]
