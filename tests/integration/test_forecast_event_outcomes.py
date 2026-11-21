"""Real registered forecast -> certified financial release -> probability diagnostics.

The fixed clock is an offline fixture only. The production request has no backdating
or realized-label field, and all successful outcomes use the existing v2 certifier.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

import astock.research.institutional as institutional
from astock.market_data.official_calendar import load_official_trading_calendar
from astock.research.debate_evaluation import QualityError, load_quality_policy
from astock.research.institutional import InstitutionalResearchService
from astock.research.outcome_feedback import evaluate_forecast_events, load_forecast_event
from astock.research.quality_cli import (
    ForecastEventOutcomeRequest,
    QualityServices,
    register_commands,
)
from astock.schemas.financial import FinancialPeriodType
from astock.schemas.institutional_research import (
    ForecastEventBuildRequest,
    ForecastEventDefinition,
)
from astock.schemas.market import Market
from astock.settings import ProjectPaths
from tests.integration.test_financial_sources import (
    PERIOD_END,
    _capture_existing_annual,
    _install_tushare_financial_mock,
    _service,
)
from tests.unit import test_institutional_research as fixtures

D = Decimal
FREEZE_AT = datetime(2024, 11, 21, 8, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _clock(monkeypatch, observed: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(observed.timestamp(), tz or UTC)

    monkeypatch.setattr(institutional, "datetime", Clock)


@pytest.fixture
def event_basis(tmp_path, monkeypatch):
    source = _service(tmp_path)
    service = InstitutionalResearchService(source.state, source.objects)
    monkeypatch.setattr(fixtures, "NOW", FREEZE_AT - timedelta(days=1))
    monkeypatch.setattr(fixtures, "COMPANY", "000001")
    claim, evidence = "claim:event-forecast", "evidence:event-forecast"
    fixtures._register_claim_with_evidence(
        source.state, source.objects, claim_id=claim, evidence_id=evidence,
        source_id="cninfo-disclosures:offline-event-fixture",
    )
    pack = fixtures._register_frozen_pack(
        source.state, source.objects, claim_ids=[claim], evidence_ids=[evidence],
    )
    request = fixtures._finalize_request(pack, claim, evidence)
    historical = [
        item.model_copy(update={"period_end": date(2024, 6, 30)})
        for item in request.driver_tree.historical_points
    ]
    scenarios = [
        item.model_copy(update={"input_values": [
            value.model_copy(update={"period_end": value.period_end.replace(
                year=value.period_end.year - 2,
            )}) for value in item.input_values
        ]}) for item in request.forecast_scenarios
    ]
    request = request.model_copy(update={
        "driver_tree": request.driver_tree.model_copy(update={"historical_points": historical}),
        "forecast_scenarios": scenarios,
    })
    bundle = service.finalize(request)
    event_request = ForecastEventBuildRequest(
        forecast_artifact_id=bundle.forecast_pack_artifact_id,
        definition=ForecastEventDefinition(
            market=Market.XSHE, period_end=PERIOD_END, metric="REVENUE",
            comparison="GE", threshold=D("9000000"), unit="CNY",
            description="Consolidated annual revenue reaches CNY 9 million",
        ),
        probability=D("0.8"), probability_basis="Explicit judgement on the frozen revenue driver",
        source_group="offline-analyst-a",
    )
    _clock(monkeypatch, FREEZE_AT)
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    return source, service, event_request, policy


def _certified_actual(source, monkeypatch):
    capture = _capture_existing_annual(source)
    client, calls, _ = _install_tushare_financial_mock(source, monkeypatch)
    monkeypatch.setattr(source.official, "_live_candidates", lambda *a, **kw: [])
    try:
        result = source.sync(
            "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL, live=True,
        )
    finally:
        client.close()
    assert result.release_id is not None, result.reason_codes
    assert set(calls) == {"balancesheet", "income", "cashflow"}
    release = source.read_certified_release(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert release is not None
    assert release.manifest.official_document_id == capture.document_id
    return release


def _identity(event) -> str:
    return f"ForecastEventProbability:{event.event_id}"


def _evaluate(basis, event_ids, evaluated_at):
    source, _, _, policy = basis
    return evaluate_forecast_events(
        source.state, source.objects, source, tuple(event_ids), policy,
        evaluated_at=evaluated_at, context_id="offline-certified-event-diagnostic",
    )


def _context(source, policy):
    return QualityServices(
        source.state, source.objects, None,
        load_official_trading_calendar(ROOT / "configs/official_trading_calendar.yaml"),
        policy, source.parquet.root.parent,
    )


def test_real_cli_freeze_and_readonly_certified_outcome(event_basis, monkeypatch):
    source, _, request, policy = event_basis
    emitted = []
    app = typer.Typer()
    register_commands(app, _context(source, policy), emitted.append)
    result = CliRunner().invoke(app, ["research-event-freeze", request.model_dump_json()])
    assert result.exit_code == 0, (result.output, emitted)
    frozen = emitted[-1]
    event, binding = load_forecast_event(source.state, source.objects, frozen["artifact_id"])
    assert binding["object_hash"] == frozen["object_hash"]
    assert event.frozen_at == FREEZE_AT
    assert event.calibrated_probability is False
    release = _certified_actual(source, monkeypatch)
    evaluated = release.manifest.available_to_system_at + timedelta(seconds=1)
    paths = ProjectPaths(
        root=ROOT, runtime=source.state.path.parent, objects=source.objects.root,
        parquet=source.parquet.root.parent, manifests=source.state.path.parent / "manifests",
        state_db=source.state.path,
    )
    monkeypatch.setattr(ProjectPaths, "discover", classmethod(lambda cls, start=None: paths))

    def forbidden(*args, **kwargs):
        raise AssertionError("outcomes must not initialize, write, or acquire")

    monkeypatch.setattr(source.objects, "put_json", forbidden)
    monkeypatch.setattr(source.state, "register_artifact", forbidden)
    monkeypatch.setattr(InstitutionalResearchService, "freeze_forecast_event", forbidden)
    from astock.core.state import StateStore

    monkeypatch.setattr(StateStore, "migrate", forbidden)
    readonly_app = typer.Typer()
    register_commands(readonly_app, forbidden, emitted.append)
    payload = ForecastEventOutcomeRequest(
        event_artifact_ids=(frozen["artifact_id"],), evaluated_at=evaluated,
        context_id="cli-readonly",
    )
    result = CliRunner().invoke(
        readonly_app, ["research-event-outcomes", payload.model_dump_json()],
    )
    assert result.exit_code == 0, (result.output, emitted)
    report = emitted[-1]
    observed = report["events"][0]
    assert observed["status"] == "MATURE"
    assert D(observed["actual"]) == D("10000000")  # Official PDF: 1000 万元.
    assert observed["occurred"] is True
    assert D(observed["brier_score"]) == D(".04")
    record = source.state.artifact_record(f"financial-source:{release.manifest.release_id}")
    assert record is not None and observed["release_object_hash"] == record["object_hash"]
    assert report["persistent_writes"] is False
    assert report["diagnostic"]["status"] == "INSUFFICIENT_ECONOMIC_CLUSTERS"
    assert report["diagnostic"]["calibrated_probability"] is False


def test_versions_aliases_and_thresholds_do_not_inflate_economic_clusters(event_basis, monkeypatch):
    _, service, request, _ = event_basis
    first = service.freeze_forecast_event(request)
    false_request = request.model_copy(update={
        "definition": request.definition.model_copy(update={
            "threshold": D("20000000"), "comparison": "GT", "description": "A higher threshold",
        }),
    })
    second = service.freeze_forecast_event(false_request)
    _clock(monkeypatch, FREEZE_AT + timedelta(seconds=2))
    revision = service.freeze_forecast_event(request.model_copy(update={
        "probability": D(".2"), "source_group": "republished-alias",
        "definition": request.definition.model_copy(update={"description": "Same economic event"}),
    }))
    release = _certified_actual(event_basis[0], monkeypatch)
    report = _evaluate(
        event_basis, [_identity(first), _identity(second), _identity(revision), _identity(first)],
        release.manifest.available_to_system_at + timedelta(seconds=1),
    )
    assert report["input_count"] == 4 and report["distinct_prediction_count"] == 3
    assert report["selected_economic_event_count"] == 2
    assert report["diagnostic"]["mature_event_count"] == 2
    assert report["diagnostic"]["economic_cluster_count"] == 1
    assert D(report["diagnostic"]["mean_brier_score"]) == D(".34")
    assert D(report["diagnostic"]["binned_absolute_calibration_error"]) == D(".3")
    assert report["deduplication"][0]["code"] == "LATER_OR_DUPLICATE_JUDGEMENT"
    assert report["production_weight_changes"] is False


def test_simultaneous_conflicting_probabilities_are_excluded(event_basis, monkeypatch):
    _, service, request, _ = event_basis
    first = service.freeze_forecast_event(request)
    second = service.freeze_forecast_event(request.model_copy(update={"probability": D(".2")}))
    release = _certified_actual(event_basis[0], monkeypatch)
    report = _evaluate(
        event_basis, [_identity(first), _identity(second)],
        release.manifest.available_to_system_at + timedelta(seconds=1),
    )
    assert report["selected_economic_event_count"] == 0
    assert report["diagnostic"]["mean_brier_score"] is None
    assert report["deduplication"][0]["code"] == "SIMULTANEOUS_PROBABILITY_CONFLICT"


def test_future_or_uncaptured_actual_remains_immature_without_false_label(event_basis, monkeypatch):
    source, service, request, _ = event_basis
    event = service.freeze_forecast_event(request)
    before = _evaluate(event_basis, [_identity(event)], FREEZE_AT + timedelta(days=1))
    assert before["events"][0]["status"].startswith("IMMATURE")
    release = _certified_actual(source, monkeypatch)
    before_capture = release.manifest.available_to_system_at - timedelta(seconds=1)
    missing = _evaluate(event_basis, [_identity(event)], before_capture)
    assert missing["events"][0]["status"].startswith("IMMATURE")
    assert "occurred" not in missing["events"][0]
    assert missing["diagnostic"]["mean_brier_score"] is None


def test_idempotent_freeze_keeps_first_time_and_rejects_new_retrospective_event(
    event_basis, monkeypatch,
):
    _, service, request, _ = event_basis
    first = service.freeze_forecast_event(request)
    _clock(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    again = service.freeze_forecast_event(request)
    assert again.model_dump() == first.model_dump()
    with pytest.raises(QualityError, match="NOT_FROZEN_BEFORE_PERIOD_END"):
        service.freeze_forecast_event(request.model_copy(update={"probability": D(".9")}))
    impact = service.invalidate_forecast(
        request.forecast_artifact_id, changed_claim_ids=["claim:event-forecast"],
        invalidated_evidence_ids=[], invalidated_at=datetime(2026, 1, 1, tzinfo=UTC),
        reason="Offline fixture premise correction after the event was frozen",
    )
    assert impact["downstream_recalculation_required"]
    assert service.freeze_forecast_event(request).model_dump() == first.model_dump()
    with pytest.raises(ValueError, match="INSTITUTIONAL_PREMISE_SUPERSEDED"):
        service.freeze_forecast_event(request.model_copy(update={"probability": D(".9")}))
    for extra in ({"frozen_at": FREEZE_AT}, {"created_at": FREEZE_AT}, {"occurred": True}):
        with pytest.raises(ValidationError):
            ForecastEventBuildRequest.model_validate({**request.model_dump(), **extra})


@pytest.mark.parametrize("kind,metric,unit,status", [
    ("MANAGEMENT_EVENT", None, None, "NOT_EVALUABLE_EVENT_KIND"),
    ("FINANCIAL_THRESHOLD", "EPS", "CNY_PER_SHARE", "NOT_EVALUABLE_METRIC"),
])
def test_unsupported_events_have_explicit_not_evaluable_status(
    event_basis, kind, metric, unit, status,
):
    _, service, request, _ = event_basis
    definition = ForecastEventDefinition.model_validate({
        **request.definition.model_dump(), "kind": kind, "metric": metric, "unit": unit,
    })
    event = service.freeze_forecast_event(request.model_copy(update={"definition": definition}))
    report = _evaluate(event_basis, [_identity(event)], datetime(2026, 1, 1, tzinfo=UTC))
    assert report["events"][0]["status"] == status
    assert "occurred" not in report["events"][0]


@pytest.mark.parametrize("change,code", [
    ({"unit": "RATIO"}, "EVENT_METRIC_UNIT_MISMATCH"),
    ({"metric": "NET_INCOME"}, "EVENT_FORECAST_METRIC_UNBOUND"),
    ({"period_end": date(2029, 12, 31)}, "EVENT_FORECAST_BASIS_MISMATCH"),
])
def test_freeze_requires_actual_forecast_period_and_units(event_basis, change, code):
    _, service, request, _ = event_basis
    definition = ForecastEventDefinition.model_validate({
        **request.definition.model_dump(), **change,
    })
    with pytest.raises(QualityError, match=code):
        service.freeze_forecast_event(request.model_copy(update={"definition": definition}))


@pytest.mark.parametrize("target", ["forecast", "tree", "official_pdf"])
def test_corrupt_frozen_parent_or_actual_original_is_never_scored(
    event_basis, monkeypatch, target,
):
    source, service, request, _ = event_basis
    event = service.freeze_forecast_event(request)
    release = _certified_actual(source, monkeypatch)
    if target == "official_pdf":
        snapshot = source.state.get_snapshot(release.manifest.official_snapshot_id)
        assert snapshot is not None
        digest = snapshot.object_sha256
    else:
        digest = (
            event.forecast_object_hash if target == "forecast" else event.driver_tree_object_hash
        )
    source.objects.path_for(digest).write_bytes(b"corrupted offline object")
    expected_error = ValueError if target == "official_pdf" else QualityError
    expected_code = (
        "Financial release snapshot chain is invalid" if target == "official_pdf"
        else "EVENT_SOURCE_BYTES_UNAVAILABLE"
    )
    with pytest.raises(expected_error, match=expected_code):
        _evaluate(
            event_basis, [_identity(event)], release.manifest.available_to_system_at + timedelta(1),
        )


def test_actual_instrument_must_match_the_frozen_event(event_basis, monkeypatch):
    source, service, request, _ = event_basis
    event = service.freeze_forecast_event(request.model_copy(update={
        "definition": request.definition.model_copy(update={"market": Market.XSHG}),
    }))
    release = _certified_actual(source, monkeypatch)
    with pytest.raises(QualityError, match="REALIZATION_IDENTITY_MISMATCH"):
        _evaluate(
            event_basis, [_identity(event)],
            release.manifest.available_to_system_at + timedelta(seconds=1),
        )
