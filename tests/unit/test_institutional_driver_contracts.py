"""Accounting and inverse-driver contracts, independent of network/model calls."""
from datetime import date
from decimal import Decimal

import pytest

from astock.research.fundamental_analytics import (
    dcf_fcff_value,
    equity_from_enterprise,
    solve_bounded_driver,
)
from astock.schemas.institutional_research import (
    EquityValueAdjustments,
    ForecastPeriod,
    ForecastScenario,
    ForecastTemplate,
    ValuationMethod,
    ValuationScenarioAssumption,
)

D = Decimal


def assumption(**changes):
    values = dict(
        scenario=ForecastScenario.BASE, method=ValuationMethod.DCF_FCFF,
        discount_rate=D(".10"), terminal_growth=D(".02"), net_debt=D("20"),
        shares_outstanding=D("10"), assumption_note="recorded explicit bridge",
        equity_adjustments=EquityValueAdjustments(
            non_operating_assets_excluding_cash=D("7"), minority_interests=D("3"),
            other_senior_claims=D("2"), share_count_basis="FULLY_DILUTED",
            basis_note="cash is already included in net debt; claims exclude diluted options",
        ),
    )
    return ValuationScenarioAssumption(**{**values, **changes})


def period(day):
    return ForecastPeriod(
        period_end=day, template=ForecastTemplate.OPERATING_FCFF,
        metrics={"REVENUE": D("100")}, evaluated_nodes={},
        revenue=D("100"), operating_margin=D(".2"),
        ebit=D("20"), tax_rate=D(".25"), nopat=D("15"), d_and_a=D("5"),
        capex=D("4"), change_working_capital=D("1"), fcff=D("15"),
    )


def test_consolidated_enterprise_to_parent_equity_is_explicit():
    assert equity_from_enterprise(D("100"), assumption()) == D("82")
    for item in (assumption(net_debt=None), assumption(equity_adjustments=None)):
        with pytest.raises(ValueError, match="BRIDGE_INCOMPLETE"):
            equity_from_enterprise(D("100"), item)


def test_dcf_uses_actual_discount_horizon_and_annual_cash_flows():
    item = assumption()
    days = [period(date(2027, 12, 31)), period(date(2028, 12, 31))]
    ev, equity = dcf_fcff_value(days, item, valuation_date=date(2026, 12, 31))
    t2 = D("731") / D("365")
    expected = D("15") / D("1.1") + (
        D("15") + D("15") * D("1.02") / D(".08")
    ) / D("1.1") ** t2
    assert abs(ev - expected) < D("1e-20")
    assert equity == ev - D("18")
    for invalid in (
        [days[0], days[0]],
        [period(date(2027, 3, 31)), period(date(2027, 6, 30))],
    ):
        with pytest.raises(ValueError, match="annual"):
            dcf_fcff_value(invalid, item)


@pytest.mark.parametrize("evaluate,target,kind", [
    (lambda x: x * 2, D("1"), "BRACKETED_NOT_GLOBAL_UNIQUENESS"),
    (lambda x: x * 2, D("0"), "BOUNDARY"),
    (lambda x: x * 2, D("5"), "NO_BRACKET_IN_SAMPLED_DOMAIN"),
    (lambda x: (x - D(".5")) * (x - D("1.5")), D("0"), "MULTIPLE_BRACKETS"),
    (lambda x: D("1"), D("1"), "NON_IDENTIFIABLE_FLAT"),
])
def test_driver_solver_labels_identification_limits(evaluate, target, kind):
    status, roots, points = solve_bounded_driver(
        evaluate, target=target, lower=D("0"), upper=D("2"), grid_points=17,
    )
    assert status == kind
    assert len(points) == 17
    for root in roots:
        assert abs(evaluate(root) - target) < D("1e-8")


def test_solver_rejects_non_finite_or_unbounded_requests():
    with pytest.raises(ValueError):
        solve_bounded_driver(lambda x: x, target=D("NaN"), lower=D("0"), upper=D("2"))
    with pytest.raises(ValueError):
        solve_bounded_driver(
            lambda x: x, target=D("1"), lower=D("0"), upper=D("2"), grid_points=1000
        )


@pytest.fixture
def frozen_model(tmp_path):
    from astock.schemas.institutional_research import DriverTree, ForecastPack, ValuationPack
    from tests.unit.test_institutional_research import (
        _finalize_request,
        _register_claim_with_evidence,
        _register_frozen_pack,
        _runtime,
    )

    state, objects, service = _runtime(tmp_path)
    claim = "claim:completion"
    evidence, _ = _register_claim_with_evidence(
        state, objects, claim_id=claim, evidence_id="evidence:completion",
        source_id="cninfo-disclosures:test",
    )
    pack = _register_frozen_pack(state, objects, claim_ids=[claim], evidence_ids=[evidence])
    request = _finalize_request(pack, claim, evidence)
    bundle = service.finalize(request)

    def read(identity, model):
        record = state.artifact_record(identity)
        assert record is not None
        return model.model_validate_json(objects.get_bytes(record["object_hash"]))

    forecast = read(bundle.forecast_pack_artifact_id, ForecastPack)
    tree = read(forecast.driver_tree_artifact_id, DriverTree)
    valuation = read(bundle.valuation_pack_artifact_id, ValuationPack)
    return state, objects, service, request, bundle, forecast, tree, valuation


def test_frozen_assumptions_units_and_affected_descendants(frozen_model):
    from astock.research.outcome_feedback import forecast_revision_impact

    _, _, service, request, _, forecast, tree, _ = frozen_model
    frozen = forecast.model_dump(mode="json")
    assert all(len(scenario.driver_inputs) == 14 for scenario in forecast.scenarios)
    assert {x.unit for x in forecast.scenarios[0].driver_inputs} == {"CNY", "unit", "ratio"}
    impact = forecast_revision_impact(
        forecast, tree, changed_claim_ids=["claim:completion"], invalidated_evidence_ids=[],
    )
    assert impact["downstream_recalculation_required"]
    assert all(
        "revenue" in x["affected_nodes"] and "REVENUE" in x["affected_metrics"]
        for x in impact["affected_scenarios"].values()
    )
    unrelated = forecast_revision_impact(
        forecast, tree, changed_claim_ids=["unrelated"], invalidated_evidence_ids=[],
    )
    assert not unrelated["downstream_recalculation_required"]
    assert forecast.model_dump(mode="json") == frozen
    scenarios = list(request.forecast_scenarios)
    inputs = list(scenarios[0].input_values)
    inputs[0] = inputs[0].model_copy(update={"unit": "CNY_PER_SHARE"})
    scenarios[0] = scenarios[0].model_copy(update={"input_values": inputs})
    with pytest.raises(ValueError, match="unit"):
        service.finalize(request.model_copy(update={"forecast_scenarios": scenarios}))


def test_realization_waits_for_official_period_and_scores_matching_units(frozen_model):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from astock.research.outcome_feedback import evaluate_forecast_realization
    from astock.schemas.financial import (
        FinancialFact,
        FinancialFieldCode,
        FinancialPeriodType,
        FinancialStatementType,
        FinancialUnit,
    )
    from astock.schemas.financial_sources import FinancialStatementScope

    _, _, _, _, _, forecast, tree, _ = frozen_model
    period_end = date(2027, 12, 31)
    evaluation = datetime(2028, 4, 30, tzinfo=UTC)
    pending = evaluate_forecast_realization(
        forecast, tree, None, period_end=period_end, evaluated_at=evaluation,
        context_id="recorded-fixture",
    )
    assert pending["status"].startswith("IMMATURE")
    with pytest.raises(ValueError, match="PRECEDES_FORECAST"):
        evaluate_forecast_realization(
            forecast, tree, None, period_end=period_end,
            evaluated_at=datetime(2020, 1, 1, tzinfo=UTC), context_id="recorded-fixture",
        )
    facts = tuple(
        FinancialFact(
            fact_id=field.value, company_id=forecast.company_id,
            period_end=period_end, period_type=FinancialPeriodType.ANNUAL,
            statement_type=FinancialStatementType.INCOME_STATEMENT, field_code=field,
            reported_value=value, unit=unit, evidence_ids=["official-fixture"],
        )
        for field, value, unit in (
            (FinancialFieldCode.REVENUE, D(".011"), FinancialUnit.TEN_THOUSAND_CNY),
            (FinancialFieldCode.EBIT, D("22"), FinancialUnit.CNY),
        )
    )
    release = SimpleNamespace(
        manifest=SimpleNamespace(
            company_id=forecast.company_id, period_end=period_end,
            period_type=FinancialPeriodType.ANNUAL, available_to_system_at=evaluation,
            release_id="recorded-official-release",
            model_dump=lambda **_: {"release_id": "recorded-official-release"},
        ),
        facts=facts,
        observations=(SimpleNamespace(statement_scope=FinancialStatementScope.CONSOLIDATED),),
    )
    result = evaluate_forecast_realization(
        forecast, tree, release, period_end=period_end, evaluated_at=evaluation,
        context_id="recorded-fixture",
    )
    assert result["status"] == "MATURE"
    revenue = next(x for x in result["metrics"] if x["metric"] == "REVENUE")
    assert D(revenue["actual"]) == D("110") and D(revenue["absolute_error"]) == D("10")
    assert revenue["inside_scenario_range"]
    assert not result["calibrated_probability"] and not result["production_weight_changes"]
    release.manifest.period_type = FinancialPeriodType.QUARTERLY
    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        evaluate_forecast_realization(
            forecast, tree, release, period_end=period_end, evaluated_at=evaluation,
            context_id="recorded-fixture",
        )


def test_registered_operating_sensitivity_recalculates_frozen_driver_tree(frozen_model):
    from astock.schemas.institutional_research import DriverSensitivityRequest, ValuationPack

    state, objects, service, request, _, _, _, _ = frozen_model
    request = request.model_copy(update={
        "driver_sensitivity_requests": [
            DriverSensitivityRequest(
                node_id="units", period_end=date(2027, 12, 31),
                lower=D("8"), upper=D("12"), grid_points=5,
            )
        ]
    })
    bundle = service.finalize(request)
    repeated = service.finalize(request)
    assert repeated.model_dump(mode="json") == bundle.model_dump(mode="json")
    record = state.artifact_record(bundle.valuation_pack_artifact_id)
    assert record is not None
    result = ValuationPack.model_validate_json(objects.get_bytes(record["object_hash"]))
    assert len(result.driver_sensitivity) == 5
    prices = [x.per_share_value for x in result.driver_sensitivity]
    assert prices == sorted(prices) and prices[0] < prices[-1]
    inverse = next(x for x in result.market_implied_expectations
                   if x.expectation == "IMPLIED_OPERATING_DRIVER")
    assert inverse.implied_value is None and inverse.solution_kind == "MARKET_PRICE_ANCHOR_REQUIRED"


def test_operating_reconciliation_requires_explicit_eliminations_and_same_scope():
    from astock.schemas.institutional_research import EvidenceBoundMetric, OperatingReconciliation

    def metric(value, unit="CNY", period=date(2027, 12, 31)):
        return EvidenceBoundMetric(
            metric="REVENUE", value=D(value), unit=unit, period_end=period,
            evidence_ids=["evidence:statement"],
        )

    values = dict(
        total=metric("100"), components={"a": metric("60"), "b": metric("50")},
        eliminations=metric("-10"),
    )
    assert OperatingReconciliation.model_validate(values).total.value == D("100")
    with pytest.raises(ValueError, match="reconcile"):
        OperatingReconciliation.model_validate({**values, "eliminations": metric("0")})
    with pytest.raises(ValueError, match="same finite unit and period"):
        OperatingReconciliation.model_validate({
            **values, "eliminations": metric("-10", unit="THOUSAND_CNY"),
        })


@pytest.mark.parametrize("with_eps", [False, True])
def test_sampled_estimates_compare_only_explicit_same_metric_period_unit(frozen_model, with_eps):
    from astock.providers.forecasts import SampledForecastAggregate
    from astock.schemas.institutional_research import (
        DriverAssumptionProvenance,
        DriverHistoricalPoint,
        DriverInputValue,
        DriverNode,
        DriverOperation,
        InstitutionalDecisionContextBuildRequest,
        InstitutionalDecisionContextDraft,
    )
    from astock.schemas.market import Market
    from tests.unit.test_institutional_research import NOW, _statement

    state, objects, service, request, bundle, _, _, _ = frozen_model
    if with_eps:
        draft = request.driver_tree
        draft = draft.model_copy(update={
            "nodes": sorted([*draft.nodes, DriverNode(
                node_id="eps", label="EPS", operation=DriverOperation.INPUT, unit="CNY_PER_SHARE",
            )], key=lambda x: x.node_id),
            "output_bindings": {**draft.output_bindings, "EPS": "eps"},
            "historical_points": sorted([
                *draft.historical_points,
                DriverHistoricalPoint(
                    node_id="eps", period_end=date(2026, 6, 30), value=D("4"),
                    claim_ids=["claim:completion"], evidence_ids=["evidence:completion"],
                    created_at=NOW,
                ),
            ], key=lambda item: (item.node_id, item.period_end)),
        })
        scenarios = []
        for scenario in request.forecast_scenarios:
            extra = [
                DriverInputValue(
                    node_id="eps", period_end=day, value=D("5"), unit="CNY_PER_SHARE",
                    provenance=DriverAssumptionProvenance.EVIDENCE, claim_ids=["claim:completion"],
                    evidence_ids=["evidence:completion"], provenance_note="recorded EPS assumption",
                )
                for day in (date(2027, 12, 31), date(2028, 12, 31))
            ]
            scenarios.append(scenario.model_copy(update={
                "input_values": sorted([*scenario.input_values, *extra],
                                       key=lambda x: (x.node_id, x.period_end)),
            }))
        bundle = service.finalize(request.model_copy(update={
            "driver_tree": draft, "forecast_scenarios": scenarios,
        }))
    aggregate = SampledForecastAggregate(
        company_id=request.company_id, market=Market.XSHG, target_fiscal_year=2027,
        as_of=NOW, window_start=date(2026, 7, 1), window_end=NOW.date(),
        source_groups=("EASTMONEY",), capture_object_hashes=("b" * 64,),
        observation_ids=("recorded-report-1", "recorded-report-2"),
        raw_sample_count=2, sample_count=2, institution_count=2, conflicting_institutions=(),
        mean=D("4"), median=D("4"), standard_deviation=D("0"), minimum=D("4"), maximum=D("4"),
    )
    ref = objects.put_json(aggregate.model_dump(mode="json"))
    artifact_id = "sampled-forecast-aggregate:" + ref.sha256
    state.register_artifact(
        artifact_id=artifact_id, artifact_type="SampledForecastAggregate",
        schema_version=aggregate.schema_version, object_hash=ref.sha256, input_hashes=[],
    )
    statement = _statement("claim:completion", "evidence:completion")
    context = service.build_decision_context(InstitutionalDecisionContextBuildRequest(
        company_id=request.company_id, as_of=NOW,
        fundamental_model_bundle_artifact_id=f"FundamentalModelBundle:{bundle.bundle_id}",
        draft=InstitutionalDecisionContextDraft(
            decision_question="What differs from these bounded report samples?",
            decision_horizon_end=date(2028, 12, 31), investment_thesis=statement,
            variant_perception=statement, key_driver_ids=["margin", "price", "units"],
            competing_hypotheses=[statement], sampled_forecast_artifact_ids=[artifact_id],
        ),
    ))
    comparison = context.sampled_expectations[0]
    assert comparison.full_market_consensus is False
    assert comparison.source_object_hash == ref.sha256 and comparison.sample_count == 2
    assert comparison.status == ("COMPARABLE" if with_eps else "NOT_COMPARABLE")
    assert comparison.difference == (D("1") if with_eps else None)


def test_premise_revision_invalidates_only_current_descendants_without_rewriting_history(
    frozen_model,
):
    from datetime import timedelta

    from astock.schemas.institutional_research import ForecastPack

    state, objects, service, request, bundle, forecast, _, _ = frozen_model
    forecast_id = bundle.forecast_pack_artifact_id
    hashes_before = {
        identity: state.artifact_record(identity)["object_hash"]
        for identity in (
            forecast_id, bundle.valuation_pack_artifact_id,
            bundle.driver_tree_artifact_id, f"FundamentalModelBundle:{bundle.bundle_id}",
        )
    }
    kwargs = dict(
        changed_claim_ids=["claim:completion"], invalidated_evidence_ids=[],
        invalidated_at=request.as_of + timedelta(days=1),
        reason="recorded restatement of the premise",
    )
    unrelated = service.invalidate_forecast(
        forecast_id, **{**kwargs, "changed_claim_ids": ["not-used"]}
    )
    assert unrelated["persistent_writes"] is False
    result = service.invalidate_forecast(forecast_id, **kwargs)
    assert set(result["invalidated_artifact_ids"]) == {
        forecast_id, bundle.valuation_pack_artifact_id,
        f"FundamentalModelBundle:{bundle.bundle_id}",
    }
    assert service.status(request.company_id)["status"] == "SUPERSEDED"
    driver_checkpoint = state.get_checkpoint("institutional-driver-tree", request.company_id)
    assert driver_checkpoint["status"] == "READY"
    for identity, object_hash in hashes_before.items():
        assert state.artifact_record(identity)["object_hash"] == object_hash
        assert objects.verify(object_hash)
    assert service.audit(f"FundamentalModelBundle:{bundle.bundle_id}")["status"] == "PASS"
    with pytest.raises(ValueError, match="PREMISE_SUPERSEDED"):
        service._load(forecast_id, "ForecastPack", ForecastPack)
    repeated = service.invalidate_forecast(forecast_id, **kwargs)
    assert repeated == result
    assert forecast.forecast_id == ForecastPack.model_validate_json(
        objects.get_bytes(hashes_before[forecast_id])
    ).forecast_id


def test_fcff_rejects_mixed_money_units_before_valuation(frozen_model):
    from astock.research.fundamental_analytics import forecast_period_from_nodes

    _, _, _, _, _, forecast, tree, _ = frozen_model
    nodes = [
        node.model_copy(update={"unit": "MILLION_CNY"}) if node.node_id == "capex" else node
        for node in tree.draft.nodes
    ]
    bad_tree = tree.model_copy(update={"draft": tree.draft.model_copy(update={"nodes": nodes})})
    period = forecast.scenarios[0].periods[0]
    with pytest.raises(ValueError, match="matching monetary units"):
        forecast_period_from_nodes(bad_tree, period.evaluated_nodes, period.period_end)


@pytest.mark.parametrize("unit", ["MILLION_CNY", "TEN_THOUSAND_CNY", "USD"])
def test_all_amounts_must_be_normalized_to_cny_before_valuation(frozen_model, unit):
    from astock.research.fundamental_analytics import forecast_period_from_nodes

    _, _, _, _, _, forecast, tree, _ = frozen_model
    nodes = [
        node.model_copy(update={"unit": unit}) if node.unit == "CNY" else node
        for node in tree.draft.nodes
    ]
    altered = tree.model_copy(update={"draft": tree.draft.model_copy(update={"nodes": nodes})})
    period = forecast.scenarios[0].periods[0]
    with pytest.raises(ValueError, match="monetary units in CNY"):
        forecast_period_from_nodes(altered, period.evaluated_nodes, period.period_end)


def test_non_head_context_cannot_reuse_invalidated_forecast_ancestor(frozen_model):
    from datetime import timedelta

    from astock.schemas.institutional_research import (
        InstitutionalDecisionContext,
        InstitutionalDecisionContextBuildRequest,
        InstitutionalDecisionContextDraft,
    )
    from tests.unit.test_institutional_research import _statement

    _, _, service, request, old_bundle, _, _, _ = frozen_model
    statement = _statement("claim:completion", "evidence:completion")
    context = service.build_decision_context(InstitutionalDecisionContextBuildRequest(
        company_id=request.company_id, as_of=request.as_of,
        fundamental_model_bundle_artifact_id=f"FundamentalModelBundle:{old_bundle.bundle_id}",
        draft=InstitutionalDecisionContextDraft(
            decision_question="original frozen question",
            decision_horizon_end=date(2028, 12, 31), investment_thesis=statement,
            variant_perception=statement, key_driver_ids=["margin", "price", "units"],
            competing_hypotheses=[statement],
        ),
    ))
    scenarios = list(request.forecast_scenarios)
    inputs = list(scenarios[0].input_values)
    inputs[0] = inputs[0].model_copy(update={"value": inputs[0].value + D("1")})
    scenarios[0] = scenarios[0].model_copy(update={"input_values": inputs})
    new_bundle = service.finalize(request.model_copy(update={"forecast_scenarios": scenarios}))
    assert new_bundle.bundle_id != old_bundle.bundle_id
    service.invalidate_forecast(
        old_bundle.forecast_pack_artifact_id, changed_claim_ids=["claim:completion"],
        invalidated_evidence_ids=[], invalidated_at=request.as_of + timedelta(days=1),
        reason="recorded premise revision",
    )
    assert service.status(request.company_id)["status"] == "READY"
    identity = f"InstitutionalDecisionContext:{context.context_id}"
    with pytest.raises(ValueError, match="PREMISE_SUPERSEDED"):
        service._load(identity, "InstitutionalDecisionContext", InstitutionalDecisionContext)
    assert service.audit(identity)["status"] == "PASS"


def test_invalidation_between_registration_and_head_advance_cannot_restore_ready(
    frozen_model, monkeypatch,
):
    from datetime import timedelta

    state, _, service, request, bundle, _, _, valuation = frozen_model
    original_register = state.register_artifact
    target = "ValuationPack:recorded-racing-publication"

    def register_and_invalidate(**kwargs):
        original_register(**kwargs)
        if kwargs["artifact_id"] == target:
            service.invalidate_forecast(
                bundle.forecast_pack_artifact_id, changed_claim_ids=["claim:completion"],
                invalidated_evidence_ids=[], invalidated_at=request.as_of + timedelta(days=1),
                reason="race after immutable registration before current head",
            )

    monkeypatch.setattr(state, "register_artifact", register_and_invalidate)
    with pytest.raises(ValueError, match="PREMISE_SUPERSEDED"):
        service._persist(
            valuation.model_copy(update={"valuation_id": "recorded-racing-publication"}),
            artifact_type="ValuationPack", artifact_id=target,
            input_hashes=valuation.source_object_hashes,
            checkpoint_scope="institutional-valuation", checkpoint_key=request.company_id,
            checkpoint_status="READY",
        )
    checkpoint = state.get_checkpoint("institutional-valuation", request.company_id)
    assert checkpoint["status"] == "SUPERSEDED"
    assert checkpoint["cursor"]["artifact_id"] != target



def _completion_context(frozen_model, bundle=None):
    from astock.schemas.institutional_research import (
        InstitutionalDecisionContextBuildRequest,
        InstitutionalDecisionContextDraft,
    )
    from tests.unit.test_institutional_research import _statement

    _, _, service, request, original, _, _, _ = frozen_model
    bundle = original if bundle is None else bundle
    statement = _statement("claim:completion", "evidence:completion")
    context = service.build_decision_context(InstitutionalDecisionContextBuildRequest(
        company_id=request.company_id, as_of=request.as_of,
        fundamental_model_bundle_artifact_id=f"FundamentalModelBundle:{bundle.bundle_id}",
        draft=InstitutionalDecisionContextDraft(
            decision_question="current frozen operating case",
            decision_horizon_end=date(2028, 12, 31), investment_thesis=statement,
            variant_perception=statement, key_driver_ids=["margin", "price", "units"],
            competing_hypotheses=[statement], created_at=request.as_of,
        ),
        created_at=request.as_of,
    ))
    return f"InstitutionalDecisionContext:{context.context_id}"


def _invalidate_completion(frozen_model):
    from datetime import timedelta

    _, _, service, request, bundle, _, _, _ = frozen_model
    return service.invalidate_forecast(
        bundle.forecast_pack_artifact_id, changed_claim_ids=["claim:completion"],
        invalidated_evidence_ids=[], invalidated_at=request.as_of + timedelta(days=1),
        reason="recorded premise changed after the initial current-input selection",
    )


def _current_runtime(frozen_model, tmp_path):
    from astock.knowledge.completion_repository import KnowledgeCompletionRepository
    from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
    from astock.research.runtime import ResearchRunService
    from tests.unit.test_institutional_research import PROJECT_ROOT

    state, objects, _, _, _, _, _, _ = frozen_model
    return ResearchRunService(
        project_root=PROJECT_ROOT, state=state, objects=objects,
        reference_parquet_root=tmp_path / "parquet",
        knowledge_provider=RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state), objects,
        ),
    )


def _current_request(
    frozen_model, context_id: str, *, auto: bool = False, financial_id: str | None = None,
):
    from astock.schemas.research_runtime import (
        ResearchRunFrozenInputs,
        ResearchRunMode,
        ResearchRunRequest,
    )

    _, _, _, request, bundle, _, _, _ = frozen_model
    return ResearchRunRequest(
        company_id=request.company_id, as_of=request.as_of,
        mode=ResearchRunMode.RECORDED_INPUT,
        institutional_research_required=True,
        auto_resolve_inputs=auto, sync_reference_inputs=False,
        frozen_inputs=ResearchRunFrozenInputs(
            frozen_evidence_pack_artifact_id=(
                request.evidence_sufficiency.frozen_evidence_pack_artifact_id
            ),
            financial_integrity_artifact_id=financial_id,
            fundamental_model_bundle_artifact_id=(
                None if auto else f"FundamentalModelBundle:{bundle.bundle_id}"
            ),
            institutional_decision_context_artifact_id=None if auto else context_id,
            created_at=request.as_of,
        ),
        created_at=request.as_of,
    )


def test_current_resolver_drops_superseded_heads_and_rejects_explicit_old_ids(
    frozen_model, tmp_path,
):
    _, _, _, _, bundle, _, _, _ = frozen_model
    context_id = _completion_context(frozen_model)
    runtime = _current_runtime(frozen_model, tmp_path)
    automatic = _current_request(frozen_model, context_id, auto=True)
    identities = {f"FundamentalModelBundle:{bundle.bundle_id}", context_id}
    before = runtime.plan(automatic)
    assert identities.issubset(before.reusable_artifact_ids)

    _invalidate_completion(frozen_model)
    after = runtime.plan(automatic)
    assert identities.isdisjoint(after.reusable_artifact_ids)
    assert {
        "FUNDAMENTAL_MODEL_BUNDLE_REQUIRED", "INSTITUTIONAL_DECISION_CONTEXT_REQUIRED",
    }.issubset(after.missing_codes)
    explicit = runtime.plan(_current_request(frozen_model, context_id))
    assert "INSTITUTIONAL_PREMISE_SUPERSEDED" in explicit.missing_codes


def test_current_runtime_rechecks_a_premise_invalidated_after_real_resolution(
    frozen_model, tmp_path, monkeypatch,
):
    from astock.financial_integrity import FinancialIntegrityService
    from astock.schemas.financial import FinancialAuditRequest, FinancialIndustryProfile
    from tests.helpers import make_financial_facts
    from tests.unit.test_institutional_research import PROJECT_ROOT

    state, objects, _, institutional_request, _, _, _, _ = frozen_model
    context_id = _completion_context(frozen_model)
    financial = FinancialIntegrityService(
        state, objects, rule_config_path=PROJECT_ROOT / "configs/financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs/financial_industry_profiles.yaml",
    ).run(FinancialAuditRequest(
        company_id=institutional_request.company_id, as_of=institutional_request.as_of,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        facts=make_financial_facts(
            state, objects, company_id=institutional_request.company_id,
        ),
    )).pack
    runtime = _current_runtime(frozen_model, tmp_path)
    request = _current_request(
        frozen_model, context_id,
        financial_id=f"FinancialIntegrityEvidencePack:{financial.audit_run_id}",
    )
    before = runtime.run(request)
    assert {"fundamental_model", "institutional_decision_context"}.issubset(
        before.output_artifacts
    )
    resolve = runtime.input_resolver.resolve

    def resolve_then_invalidate(*args, **kwargs):
        result = resolve(*args, **kwargs)
        assert "INSTITUTIONAL_PREMISE_SUPERSEDED" not in result.manifest.unresolved_codes
        _invalidate_completion(frozen_model)
        return result

    monkeypatch.setattr(runtime.input_resolver, "resolve", resolve_then_invalidate)
    with pytest.raises(ValueError, match="INSTITUTIONAL_PREMISE_SUPERSEDED"):
        runtime.run(request)


@pytest.mark.parametrize("capability", ["FORECAST_VALUATION", "COMPANY_RESEARCH"])
def test_current_output_cache_and_direct_load_reject_invalidated_non_head_ancestry(
    frozen_model, monkeypatch, capability,
):
    from astock.investor_orchestration.capabilities import _BASE_NODES
    from astock.investor_orchestration.output_validation import (
        RegisteredOutputVerifier,
        output_model,
    )
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from tests.unit.test_investor_domain_contracts import _request_context

    state, objects, service, request, bundle, _, _, _ = frozen_model
    context_id = _completion_context(frozen_model)
    identity = (
        bundle.valuation_pack_artifact_id if capability == "FORECAST_VALUATION" else context_id
    )
    store = InvestorOrchestrationStore(state.path)
    store.initialize()
    envelope, preflight = _request_context(store)
    verifier = RegisteredOutputVerifier(store, objects)
    verify = verifier._verify_registered_artifact
    calls = []

    def count_validation(**kwargs):
        calls.append(kwargs["artifact_id"])
        return verify(**kwargs)

    monkeypatch.setattr(verifier, "_verify_registered_artifact", count_validation)
    node = _BASE_NODES[capability]
    verifier.verify(node, (identity,), envelope, preflight)
    verifier.verify(node, (identity,), envelope, preflight)
    assert calls == [identity]  # The second invocation really used the existing proof.

    scenarios = list(request.forecast_scenarios)
    inputs = list(scenarios[0].input_values)
    inputs[0] = inputs[0].model_copy(update={"value": inputs[0].value + D("1")})
    scenarios[0] = scenarios[0].model_copy(update={"input_values": inputs})
    newer = service.finalize(request.model_copy(update={"forecast_scenarios": scenarios}))
    newer_context = _completion_context(frozen_model, newer)
    _invalidate_completion(frozen_model)
    with pytest.raises(ValueError, match="INSTITUTIONAL_PREMISE_SUPERSEDED"):
        verifier.verify(node, (identity,), envelope, preflight)
    with pytest.raises(ValueError, match="INSTITUTIONAL_PREMISE_SUPERSEDED"):
        verifier.load(identity, output_model(node.output_schema))
    assert calls == [identity]
    replacement = (
        newer.valuation_pack_artifact_id if capability == "FORECAST_VALUATION" else newer_context
    )
    verifier.verify(node, (replacement,), envelope, preflight)
    assert service.audit(identity)["status"] == "PASS"


def test_current_committee_entries_reject_a_superseded_input_but_history_stays_readable(
    tmp_path, state,
):
    from datetime import timedelta
    from typing import cast

    from astock.research.institutional import InstitutionalResearchService
    from astock.schemas.institutional_research import InstitutionalResearchFinalizeRequest
    from astock.schemas.research import FrozenEvidencePack
    from tests.integration.test_committee import _service_and_request
    from tests.unit.test_institutional_research import _finalize_request

    committee, request, artifacts = _service_and_request(tmp_path, state)
    pack = cast(FrozenEvidencePack, artifacts["FrozenEvidencePack"])
    claim, evidence = pack.claim_ids[0], pack.evidence_ids[0]
    template = _finalize_request(f"FrozenEvidencePack:{pack.pack_id}", claim, evidence)

    def fixture_time(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: pack.as_of if key == "created_at" else fixture_time(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [fixture_time(item) for item in value]
        return value

    payload = fixture_time(template.model_dump(mode="python"))
    assert isinstance(payload, dict)
    payload.update(company_id=pack.company_id, as_of=pack.as_of)
    for point in payload["driver_tree"]["historical_points"]:
        point["period_end"] = date(pack.as_of.year - 1, 12, 31)
    institutional_request = InstitutionalResearchFinalizeRequest.model_validate(payload)
    institutional = InstitutionalResearchService(state, committee.object_store)
    bundle = institutional.finalize(institutional_request)
    reference = committee.resolve_reference(f"FundamentalModelBundle:{bundle.bundle_id}")
    references = sorted(
        [*request.artifact_references, reference], key=lambda item: item.artifact_id,
    )
    current = request.model_copy(update={
        "artifact_references": references,
        "access_policy": request.access_policy.model_copy(update={
            "frozen_artifact_hashes": sorted(item.object_sha256 for item in references),
        }),
    })
    committee.plan(current)
    completed = committee.decide(current)
    assert committee.audit(completed.decision.decision_id)["status"] == "PASS"
    institutional.invalidate_forecast(
        bundle.forecast_pack_artifact_id, changed_claim_ids=[claim],
        invalidated_evidence_ids=[], invalidated_at=request.assessment.as_of + timedelta(days=1),
        reason="recorded premise changed after the original committee decision",
    )
    for operation in (committee.plan, committee.decide, committee.recover):
        with pytest.raises(ValueError, match="INSTITUTIONAL_PREMISE_SUPERSEDED"):
            operation(current)
    assert committee.audit(completed.decision.decision_id)["status"] == "PASS"
    historical = committee.resolve_reference(reference.artifact_id)
    stable_fields = {"artifact_id", "artifact_type", "object_sha256", "role"}
    assert historical.model_dump(include=stable_fields) == reference.model_dump(
        include=stable_fields,
    )


def test_historical_forecast_outcome_cli_can_read_a_superseded_prediction(
    frozen_model, tmp_path,
):
    from datetime import UTC, datetime

    import typer
    from typer.testing import CliRunner

    from astock.market_data.official_calendar import load_official_trading_calendar
    from astock.research.debate_evaluation import load_quality_policy
    from astock.research.quality_cli import (
        ForecastOutcomeRequest,
        QualityServices,
        register_commands,
    )
    from tests.unit.test_institutional_research import PROJECT_ROOT

    state, objects, service, _, bundle, forecast, _, _ = frozen_model
    _invalidate_completion(frozen_model)
    context = QualityServices(
        state=state, objects=objects, views=None,
        calendar=load_official_trading_calendar(
            PROJECT_ROOT / "configs/official_trading_calendar.yaml"
        ),
        policy=load_quality_policy(PROJECT_ROOT / "configs/research_quality.yaml"),
        parquet_root=tmp_path / "parquet",
    )
    app = typer.Typer()
    emitted = []
    register_commands(app, context, emitted.append)
    request = ForecastOutcomeRequest(
        forecast_artifact_id=bundle.forecast_pack_artifact_id,
        period_end=date(2027, 12, 31), evaluated_at=datetime(2028, 4, 30, tzinfo=UTC),
        context_id="recorded historical outcome after premise revision",
    )
    result = CliRunner().invoke(
        app, ["research-forecast-outcome", request.model_dump_json()],
    )
    assert result.exit_code == 0, result.output
    assert emitted[-1]["forecast_id"] == forecast.forecast_id
    assert emitted[-1]["status"] == "IMMATURE_OFFICIAL_RELEASE_PENDING"
    assert service.audit(f"FundamentalModelBundle:{bundle.bundle_id}")["status"] == "PASS"
