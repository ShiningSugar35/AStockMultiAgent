"""WP-62: optional, lineage-bound technical context in the existing ResearchTeam.

A technical snapshot never becomes a second role or an eligibility shortcut.
The standard typed MarketContext and QuantFactor roles retain the only gates.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from astock.schemas.full_research import FactorSnapshot, QuantFactorResearchPack
from astock.schemas.research_team import MarketContextPack, ResearchRoleOutput
from tests.unit.test_research_team import (
    NOW,
    _register_acquisition_report,
    _service,
)
from tests.unit.test_technical_analysis import _bars, _build


def _register(state, objects, kind, ident, value, input_hashes=()):
    ref = objects.put_json(value.model_dump(mode="json") if hasattr(value, "model_dump") else value)
    state.register_artifact(
        artifact_id=ident,
        artifact_type=kind,
        schema_version=getattr(value, "schema_version", "1.0"),
        object_hash=ref.sha256,
        input_hashes=list(input_hashes),
    )
    return ref.sha256


def _setup(tmp_path):
    team, state, objects = _service(tmp_path)
    acquisition, _ = _register_acquisition_report(state, objects, company_id="600001")
    plan = team.create_company_plan(
        company_id="600001",
        acquisition_report_artifact_id=acquisition,
        as_of=NOW,
        cpu_count=4,
        memory_gib=8,
    )
    release_id = "DatasetReleaseManifest:technical-bound"
    release_hash = _register(
        state,
        objects,
        "DatasetReleaseManifest",
        release_id,
        {"instrument_id": "XSHG:600001", "fixture": "canonical"},
    )
    anchor_id = "MarketPriceAnchor:technical-bound"
    anchor_hash = _register(state, objects, "MarketPriceAnchor", anchor_id, {"close": "102"})
    bars = _bars([100 + i * 0.03 for i in range(90)])
    technical = _build(
        bars,
        as_of=NOW,
        source_artifact_id=release_id,
        source_object_hash=release_hash,
    )
    technical_id = "TechnicalIndicatorSnapshot:" + technical.snapshot_id.removeprefix("technical:")
    technical_hash = _register(
        state,
        objects,
        "TechnicalIndicatorSnapshot",
        technical_id,
        technical,
        input_hashes=[release_hash, technical.policy_sha256, technical.context_sha256],
    )
    context = MarketContextPack(
        as_of=NOW,
        company_id="600001",
        current_close=Decimal("102"),
        daily_release_artifact_id=release_id,
        daily_release_object_hash=release_hash,
        market_anchor_artifact_id=anchor_id,
        market_anchor_object_hash=anchor_hash,
        technical_snapshot_artifact_id=technical_id,
        technical_snapshot_object_hash=technical_hash,
        created_at=NOW,
    )
    return team, state, objects, plan, context, technical_id, technical_hash


def _submit(team, state, objects, plan, context, *, ident):
    _register(state, objects, "MarketContextPack", ident, context)
    return team.register_role_output(
        ResearchRoleOutput(
            plan_id=plan.plan_id,
            task_id="company-market-context",
            output_contract="MarketContextPack",
            member_artifact_ids=[ident],
            readiness_check_results={"MARKET_PRICE_ANCHOR": True},
            summary="已绑定正式行情，技术特征仅补充研究解释",
            created_at=NOW,
        )
    )


def test_market_context_accepts_matching_optional_registered_snapshot(tmp_path):
    team, state, objects, plan, context, _, _ = _setup(tmp_path)
    assert (
        _submit(team, state, objects, plan, context, ident="MarketContextPack:valid")["status"]
        == "REGISTERED"
    )


def test_technical_context_pair_rejects_partial_binding(tmp_path):
    _, _, _, _, context, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="together"):
        MarketContextPack.model_validate(
            {**context.model_dump(mode="python"), "technical_snapshot_object_hash": None}
        )


@pytest.mark.parametrize(
    "change",
    [
        {"company_id": "600002"},
        {"daily_release_object_hash": "f" * 64},
        {"technical_snapshot_object_hash": "f" * 64},
        {"as_of": NOW.replace(year=2024)},
    ],
)
def test_market_context_refuses_mismatched_technical_evidence(tmp_path, change):
    team, state, objects, plan, context, _, _ = _setup(tmp_path)
    changed = context.model_copy(update=change)
    with pytest.raises(ValueError, match="technical"):
        _submit(team, state, objects, plan, changed, ident="MarketContextPack:wrong")


def test_technical_context_cannot_cross_company_research_plans(tmp_path):
    team, state, objects, _, context, _, _ = _setup(tmp_path)
    acquisition, _ = _register_acquisition_report(state, objects, company_id="600002")
    another_plan = team.create_company_plan(
        company_id="600002",
        acquisition_report_artifact_id=acquisition,
        as_of=NOW,
        cpu_count=4,
        memory_gib=8,
    )
    with pytest.raises(ValueError, match="technical market-context provenance mismatch"):
        _submit(team, state, objects, another_plan, context, ident="MarketContextPack:cross-plan")


def test_technical_source_rejects_noncanonical_parent_artifact(tmp_path):
    team, state, objects, plan, context, _, _ = _setup(tmp_path)
    fake_parent_id = "UnqualifiedMarketInput:fixture"
    fake_parent_hash = _register(
        state, objects, "TestResearchOutput", fake_parent_id, {"source": "secondary_hint"}
    )
    candidate = _build(
        _bars([100.0] * 90),
        as_of=NOW,
        source_artifact_id=fake_parent_id,
        source_object_hash=fake_parent_hash,
    )
    candidate_id = "TechnicalIndicatorSnapshot:" + candidate.snapshot_id.removeprefix("technical:")
    candidate_hash = _register(
        state, objects, "TechnicalIndicatorSnapshot", candidate_id, candidate
    )
    bad_context = context.model_copy(
        update={
            "daily_release_artifact_id": fake_parent_id,
            "daily_release_object_hash": fake_parent_hash,
            "technical_snapshot_artifact_id": candidate_id,
            "technical_snapshot_object_hash": candidate_hash,
        }
    )
    with pytest.raises(ValueError, match="technical market-context provenance mismatch"):
        _submit(
            team,
            state,
            objects,
            plan,
            bad_context,
            ident="MarketContextPack:noncanonical-parent",
        )


def test_legacy_market_context_without_technical_remains_supported(tmp_path):
    team, state, objects, plan, context, _, _ = _setup(tmp_path)
    legacy = context.model_copy(
        update={
            "technical_snapshot_artifact_id": None,
            "technical_snapshot_object_hash": None,
        }
    )
    assert (
        _submit(team, state, objects, plan, legacy, ident="MarketContextPack:legacy")["status"]
        == "REGISTERED"
    )


def test_quant_role_reads_technical_as_existing_factor_provenance(tmp_path):
    team, state, objects, plan, context, technical_id, technical_hash = _setup(tmp_path)
    factor = FactorSnapshot(
        instrument_id="XSHG:600001",
        value=Decimal("0.1"),
        quality=Decimal("0.1"),
        growth=Decimal("0.1"),
        momentum=Decimal("0.1"),
        low_volatility=Decimal("0.1"),
        liquidity=Decimal("0.1"),
        size=Decimal("0.1"),
        earnings_revision=Decimal("0.1"),
        profitability=Decimal("0.1"),
        crowding=Decimal("0.1"),
        created_at=NOW,
    )
    pack = QuantFactorResearchPack(
        as_of=NOW,
        factors=(factor,),
        source_artifact_ids=(technical_id,),
        source_object_hashes=(technical_hash,),
        created_at=NOW,
    )
    _register(state, objects, "QuantFactorResearchPack", "QuantFactorResearchPack:bound", pack)
    result = team.register_role_output(
        ResearchRoleOutput(
            plan_id=plan.plan_id,
            task_id="quant-factor",
            output_contract="QuantFactorResearchPack",
            member_artifact_ids=["QuantFactorResearchPack:bound"],
            readiness_check_results={"QUANT_FACTOR": True},
            summary="基于本标的已注册技术来源",
            created_at=NOW,
        )
    )
    assert result["status"] == "REGISTERED"
    bad_factor = factor.model_copy(update={"instrument_id": "XSHE:000001"})
    bad = pack.model_copy(update={"factors": (bad_factor,)})
    _register(state, objects, "QuantFactorResearchPack", "QuantFactorResearchPack:wrong", bad)
    with pytest.raises(ValueError, match="QUANT_FACTOR"):
        team.register_role_output(
            ResearchRoleOutput(
                plan_id=plan.plan_id,
                task_id="quant-factor",
                output_contract="QuantFactorResearchPack",
                member_artifact_ids=["QuantFactorResearchPack:wrong"],
                readiness_check_results={"QUANT_FACTOR": True},
                summary="不应接受跨证券技术参考",
                created_at=NOW,
            )
        )
