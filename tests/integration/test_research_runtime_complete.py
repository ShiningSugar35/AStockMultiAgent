from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from astock.acceptance.phase6 import Phase6RecordedService
from astock.committee.repository import CommitteeRepository
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.paper_trading.execution import PaperExecutionService
from astock.research.runtime import ResearchRunService
from astock.research.trade_view import TradePlanViewService
from astock.research.trading_classification import TradingClassificationService
from astock.schemas import (
    DecisionPack,
    FetchStatus,
    Market,
    OrderSide,
    PaperTradingClassification,
    SourceSnapshot,
)
from astock.schemas.institutional_research import (
    EvidenceBoundStatement,
    FundamentalModelBundle,
    InstitutionalArtifactStatus,
    InstitutionalDecisionContext,
    InstitutionalDecisionContextDraft,
)
from astock.schemas.knowledge_completion import (
    KnowledgeProviderMode,
    KnowledgeProviderReadiness,
    KnowledgeProviderStatus,
    KnowledgeSkillQuery,
    KnowledgeSkillSelection,
)
from astock.schemas.research_runtime import (
    ClassifiedTradeProtocol,
    ResearchRunFrozenInputs,
    ResearchRunMode,
    ResearchRunRequest,
    ResearchRunStatus,
    TradingClassificationCorporateActionBaseline,
    TradingClassificationDraft,
    TradingPriceLimitRegime,
    TradingSpecialRegime,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _RecordedKnowledgeProvider:
    def __init__(self, status: KnowledgeProviderStatus) -> None:
        self._status = status
        self.call_count = 0

    def default_run_id(self) -> str:
        return self._status.run_id

    def status(self, run_id: str) -> KnowledgeProviderStatus:
        assert run_id == self._status.run_id
        return self._status

    def select(self, run_id: str, query: KnowledgeSkillQuery) -> KnowledgeSkillSelection:
        assert run_id == self._status.run_id
        self.call_count += 1
        return KnowledgeSkillSelection(
            query=query,
            provider_status=self._status,
            skills=[],
            candidate_count=0,
            selected_count=0,
            latency_ms=1,
            context_bytes=0,
            estimated_tokens=0,
            cache_key="7" * 64,
            cache_hit=self.call_count > 1,
            result_hash="8" * 64,
            reason_code="RECORDED_EMPTY_SELECTION",
        )


def _register_artifact(
    state: StateStore,
    objects: ObjectStore,
    *,
    artifact_id: str,
    artifact_type: str,
    payload: object,
) -> str:
    ref = objects.put_json(payload)
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return ref.sha256


def _classification(
    state: StateStore,
    objects: ObjectStore,
    *,
    as_of,
) -> str:
    query_ref = objects.put_json({"announcements": [], "recorded": True})
    query_snapshot = SourceSnapshot(
        snapshot_id=f"cninfo-disclosures:index:{query_ref.sha256}",
        source_id="cninfo-disclosures:index",
        object_sha256=query_ref.sha256,
        fetched_at=as_of - timedelta(minutes=2),
        available_to_system_at=as_of - timedelta(minutes=2),
        source_url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        mime="application/json",
        byte_size=query_ref.byte_size,
        headers_hash=content_hash({"recorded": True}),
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="PUBLIC_DISCLOSURE",
    )
    state.register_snapshot(query_snapshot)
    baseline = TradingClassificationCorporateActionBaseline(
        baseline_id="recorded-official-baseline",
        company_id="300750",
        market=Market.XSHE,
        symbol="300750",
        as_of=as_of - timedelta(minutes=1),
        window_start="2026-06-12",
        window_end="2026-07-27",
        reference_status="OFFICIAL_ENUMERATION_COMPLETE",
        raw_snapshot_ids=[query_snapshot.snapshot_id],
        official_query_snapshot_ids=[query_snapshot.snapshot_id],
        candidate_announcement_ids=[],
        observed_record_count=0,
        reason_codes=[],
        absence_is_officially_certified=True,
        created_at=as_of - timedelta(minutes=1),
    )
    classification_service = TradingClassificationService(state, objects)
    baseline_artifact = classification_service._register_official_baseline(baseline)
    rulebook_artifact = "TradingClassificationRuleBook:recorded-v1"
    _register_artifact(
        state,
        objects,
        artifact_id=rulebook_artifact,
        artifact_type="TradingClassificationRuleBook",
        payload={"rule_version": "recorded-v1"},
    )
    reference_artifacts = []
    release_ids = {}
    for kind in ("instrument", "calendar", "daily"):
        release_id = f"recorded-{kind}-release"
        artifact_id = f"market-reference:{release_id}"
        _register_artifact(
            state,
            objects,
            artifact_id=artifact_id,
            artifact_type="MarketReferenceRelease",
            payload={"kind": kind, "release_id": release_id},
        )
        reference_artifacts.append(artifact_id)
        release_ids[kind] = release_id
    draft = TradingClassificationDraft(
        company_id="300750",
        market=Market.XSHE,
        symbol="300750",
        as_of=as_of,
        effective_from=as_of,
        valid_until=as_of + timedelta(hours=2),
        classification=PaperTradingClassification(
            instrument_id="XSHE:300750",
            board="CHINEXT",
            risk_status="NORMAL",
            fixed_price_limit_eligible=True,
            suspension_status_verified=True,
            suspended=False,
            evidence_id="recorded-classification-evidence",
            created_at=as_of,
        ),
        special_no_price_limit=False,
        special_regime=TradingSpecialRegime.ORDINARY,
        price_limit_regime=TradingPriceLimitRegime.FIXED,
        price_limit_rate_bps=2000,
        rulebook_artifact_id=rulebook_artifact,
        instrument_release_id=release_ids["instrument"],
        calendar_release_id=release_ids["calendar"],
        daily_release_id=release_ids["daily"],
        resolver_version="recorded-runtime-resolver-v1",
        corporate_action_baseline_artifact_id=baseline_artifact,
        source_artifact_ids=sorted([baseline_artifact, rulebook_artifact, *reference_artifacts]),
        created_at=as_of,
    )
    return classification_service.freeze(draft).artifact_id


def _institutional_context(
    state: StateStore,
    objects: ObjectStore,
    *,
    as_of,
) -> tuple[str, str, str, str]:
    component_specs = {
        "EvidenceSufficiencyReport:recorded-institutional": "EvidenceSufficiencyReport",
        "IndustryProfile:recorded-institutional": "IndustryProfile",
        "CompanyEconomicsProfile:recorded-institutional": "CompanyEconomicsProfile",
        "DriverTree:recorded-institutional": "DriverTree",
        "ForecastPack:recorded-institutional": "ForecastPack",
        "ValuationPack:recorded-institutional": "ValuationPack",
    }
    hashes = {
        artifact_id: _register_artifact(
            state,
            objects,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            payload={"recorded": True, "company_id": "300750", "as_of": as_of.isoformat()},
        )
        for artifact_id, artifact_type in component_specs.items()
    }
    ids = list(component_specs)
    bundle = FundamentalModelBundle(
        bundle_id="recorded-institutional-bundle",
        company_id="300750",
        as_of=as_of,
        status=InstitutionalArtifactStatus.READY,
        evidence_sufficiency_artifact_id=ids[0],
        industry_profile_artifact_id=ids[1],
        company_economics_artifact_id=ids[2],
        driver_tree_artifact_id=ids[3],
        forecast_pack_artifact_id=ids[4],
        valuation_pack_artifact_id=ids[5],
        artifact_object_hashes=hashes,
        blocking_codes=[],
        warning_codes=["RECORDED_RUNTIME_TEST_BUNDLE"],
        evidence_ids=["evidence:institutional"],
        claim_ids=["claim:institutional"],
        created_at=as_of,
    )
    bundle_ref = objects.put_json(bundle.model_dump(mode="json"))
    bundle_artifact = f"FundamentalModelBundle:{bundle.bundle_id}"
    state.register_artifact(
        artifact_id=bundle_artifact,
        artifact_type="FundamentalModelBundle",
        schema_version=bundle.schema_version,
        object_hash=bundle_ref.sha256,
        input_hashes=sorted(hashes.values()),
    )
    statement = EvidenceBoundStatement(
        statement="Recorded institutional investment thesis",
        claim_ids=["claim:institutional"],
        evidence_ids=["evidence:institutional"],
        created_at=as_of,
    )
    context = InstitutionalDecisionContext(
        context_id="recorded-institutional-context",
        company_id="300750",
        as_of=as_of,
        fundamental_model_bundle_artifact_id=bundle_artifact,
        fundamental_model_bundle_object_hash=bundle_ref.sha256,
        draft=InstitutionalDecisionContextDraft(
            decision_question="What must be true for the recorded institutional case to work?",
            decision_horizon_end=(as_of + timedelta(days=365)).date(),
            investment_thesis=statement,
            variant_perception=statement,
            key_driver_ids=["driver:margin", "driver:price", "driver:units"],
            competing_hypotheses=[statement],
            portfolio_context="position sizing remains downstream in the portfolio layer",
            created_at=as_of,
        ),
        claim_ids=["claim:institutional"],
        evidence_ids=["evidence:institutional"],
        source_artifact_ids=[bundle_artifact],
        source_object_hashes=[bundle_ref.sha256],
        created_at=as_of,
    )
    context_ref = objects.put_json(context.model_dump(mode="json"))
    context_artifact = f"InstitutionalDecisionContext:{context.context_id}"
    state.register_artifact(
        artifact_id=context_artifact,
        artifact_type="InstitutionalDecisionContext",
        schema_version=context.schema_version,
        object_hash=context_ref.sha256,
        input_hashes=[bundle_ref.sha256],
    )
    return bundle_artifact, context_artifact, bundle_ref.sha256, context_ref.sha256


def test_recorded_inputs_drive_generic_runtime_to_classified_protocol(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    parquet = tmp_path / "parquet"
    recorded = Phase6RecordedService(PROJECT_ROOT, state, objects, parquet).run("300750")
    as_of = recorded.committee_decision.as_of

    committee = CommitteeRepository(state, objects)
    bundle_summary = committee.bundle_summary(recorded.committee_decision.bundle_id)
    assert bundle_summary is not None
    assessment = committee.get_assessment(str(bundle_summary["assessment_id"]))
    assert assessment is not None

    registry_artifact = "KnowledgeSkillRegistryRelease:recorded-runtime-registry"
    registry_hash = _register_artifact(
        state,
        objects,
        artifact_id=registry_artifact,
        artifact_type="KnowledgeSkillRegistryRelease",
        payload={"registry": "recorded-runtime-registry"},
    )
    provider_status = KnowledgeProviderStatus(
        run_id="recorded-runtime-knowledge",
        status=KnowledgeProviderReadiness.READY,
        mode=KnowledgeProviderMode.REGISTRY_RELEASE,
        reason_code="REGISTRY_READY",
        total_skill_count=0,
        ready_skill_count=0,
        pending_review_count=0,
        approved_count=0,
        rejected_count=0,
        eligible_skill_count=0,
        registry_release_id="recorded-runtime-registry",
        registry_artifact_id=registry_artifact,
        registry_object_hash=registry_hash,
    )
    provider = _RecordedKnowledgeProvider(provider_status)
    classification_artifact = _classification(state, objects, as_of=as_of)
    report = recorded.report
    request = ResearchRunRequest(
        company_id="300750",
        as_of=as_of,
        mode=ResearchRunMode.RECORDED_INPUT,
        frozen_inputs=ResearchRunFrozenInputs(
            frozen_evidence_pack_artifact_id=report.frozen_evidence_pack_artifact_id,
            base_case_artifact_id=report.base_case_artifact_id,
            specialist_route_artifact_id=report.specialist_route_artifact_id,
            serenity_delta_artifact_id=report.specialist_delta_artifact_ids["SERENITY"],
            zhihu_delta_artifact_id=report.specialist_delta_artifact_ids["ZHIHU_EXPERT"],
            research_memo_artifact_id=report.research_memo_artifact_id,
            financial_integrity_artifact_id=report.financial_integrity_artifact_id,
            created_at=as_of,
        ),
        knowledge_run_id=provider_status.run_id,
        knowledge_query=KnowledgeSkillQuery(query="recorded runtime contract", top_k=1),
        committee_assessment=assessment,
        trading_classification_artifact_id=classification_artifact,
        auto_resolve_inputs=False,
        sync_reference_inputs=False,
        created_at=as_of,
    )
    service = ResearchRunService(
        project_root=PROJECT_ROOT,
        state=state,
        objects=objects,
        reference_parquet_root=parquet,
        knowledge_provider=provider,
    )

    missing_institutional = service.run(
        request.model_copy(update={"institutional_research_required": True})
    )
    assert missing_institutional.status is ResearchRunStatus.NEEDS_INFO
    assert missing_institutional.current_stage.value == "FUNDAMENTAL_MODEL"
    assert "FUNDAMENTAL_MODEL_BUNDLE_REQUIRED" in missing_institutional.needs_info_codes

    result = service.run(request)

    assert result.status is ResearchRunStatus.COMPLETE
    assert result.current_stage.value == "COMPLETE"
    assert result.paper_ledger_write_count == 0
    assert not result.broker_execution_allowed
    assert set(result.output_artifacts) >= {
        "knowledge_skill_delta",
        "committee_protocol_draft",
        "trading_classification",
        "trade_protocol",
        "paper_decision",
    }
    final_ref = result.output_artifacts["trade_protocol"]
    final_protocol = ClassifiedTradeProtocol.model_validate_json(
        objects.get_bytes(final_ref.object_hash)
    )
    assert final_protocol.trading_classification_artifact_id == classification_artifact
    assert final_protocol.final_outcome.value == "APPROVE_SIMULATION"
    assert final_protocol.paper_simulation_allowed
    assert not final_protocol.broker_execution_allowed
    assert service.audit(result.run_id).status == "PASS"

    assert request.frozen_inputs is not None
    bundle_artifact, context_artifact, bundle_hash, context_hash = _institutional_context(
        state,
        objects,
        as_of=as_of,
    )
    institutional_result = service.run(
        request.model_copy(
            update={
                "institutional_research_required": True,
                "frozen_inputs": request.frozen_inputs.model_copy(
                    update={
                        "fundamental_model_bundle_artifact_id": bundle_artifact,
                        "institutional_decision_context_artifact_id": context_artifact,
                    }
                ),
            }
        )
    )
    assert institutional_result.status is ResearchRunStatus.COMPLETE
    assert set(institutional_result.output_artifacts) >= {
        "fundamental_model",
        "institutional_decision_context",
        "decision_pack",
        "trade_protocol",
    }
    institutional_decision_ref = institutional_result.output_artifacts["decision_pack"]
    institutional_decision = DecisionPack.model_validate_json(
        objects.get_bytes(institutional_decision_ref.object_hash)
    )
    assert bundle_hash in institutional_decision.frozen_input_hashes
    assert context_hash in institutional_decision.frozen_input_hashes
    assert service.audit(institutional_result.run_id).status == "PASS"

    trade_view = TradePlanViewService(state, objects, service.reference).build(
        final_ref.artifact_id,
        reference_price_fen=20_000,
        reference_price_source="RECORDED_ACCEPTANCE_PRICE",
    )
    assert trade_view.final_outcome.value == "APPROVE_SIMULATION"
    assert trade_view.committee_expected_scenario_price_range_fen is not None
    assert trade_view.committee_expected_scenario_price_range_fen.lower_fen == 22_400
    assert trade_view.committee_expected_scenario_price_range_fen.upper_fen == 24_000
    assert trade_view.committee_downside_scenario_price_range_fen is not None
    assert trade_view.committee_downside_scenario_price_range_fen.lower_fen == 16_000
    assert trade_view.committee_downside_scenario_price_range_fen.upper_fen == 19_000
    assert not trade_view.scenario_prices_are_targets
    assert not trade_view.exact_entry_zone_available
    assert not trade_view.exact_exit_target_available
    assert not trade_view.broker_execution_allowed
    repeated_trade_view = TradePlanViewService(state, objects, service.reference).build(
        final_ref.artifact_id,
        reference_price_fen=20_000,
        reference_price_source="RECORDED_ACCEPTANCE_PRICE",
    )
    assert repeated_trade_view.view_id == trade_view.view_id
    trade_view_artifact = state.artifact_record(f"TradePlanView:{trade_view.view_id}")
    repeated_trade_view_artifact = state.artifact_record(
        f"TradePlanView:{repeated_trade_view.view_id}"
    )
    assert trade_view_artifact is not None
    assert repeated_trade_view_artifact is not None
    assert trade_view_artifact["object_hash"] == repeated_trade_view_artifact["object_hash"]

    preparation = PaperExecutionService(state, objects).prepare(
        trade_protocol_id=final_ref.artifact_id,
        reference_pack_artifact_id=report.paper_reference_pack_artifact_id,
        account_id="classified-paper",
        idempotency_key="classified-paper-prepare-v1",
        side=OrderSide.BUY,
        qty=100,
        limit_price_fen=20_000,
        requested_at=recorded.paper_reference_pack.visible_at,
    )
    assert preparation.execution_request.trade_protocol_id == final_ref.artifact_id
    assert (
        preparation.execution_request.trading_classification_artifact_id == classification_artifact
    )
    assert preparation.execution_request.schema_version == "paper-execution-request-v3"
    assert preparation.execution_request.requires_user_confirmation
    paper_audit = PaperExecutionService(state, objects).audit(preparation.execution_request_id)
    assert paper_audit["status"] == "PASS"
