from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from astock.providers.forecasts import (
    EastMoneyForecastProvider,
    aggregate_forecasts,
    load_forecast_capture,
    select_latest_forecasts,
)
from astock.schemas import Market


def report(identifier, institution, value, publication="2026-11-01"):
    return {"stockCode": "600519", "infoCode": identifier, "orgCode": institution,
            "orgSName": institution, "publishDate": publication,
            "predictThisYearEps": value, "predictNextYearEps": None,
            "predictNextTwoYearEps": None}


def capture(object_store, state, rows, *, total_pages=1, year: int | None = 2026, max_pages=2):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "currentYear": year, "TotalPage": total_pages, "data": rows,
        })
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = EastMoneyForecastProvider(object_store, state, client).capture(
            "600519", Market.XSHG, date(2026, 11, 1), date(2026, 11, 20),
            max_pages=max_pages, page_size=50,
        )
    return result, calls


def aggregate(object_store, state, captures, **overrides):
    kwargs = {"company_id": "600519", "market": Market.XSHG, "target_fiscal_year": 2026,
              "as_of": datetime.now(UTC) + timedelta(seconds=1),
              "window_start": date(2026, 11, 1), "window_end": date(2026, 11, 20)}
    return aggregate_forecasts(captures, state=state, objects=object_store, **(kwargs | overrides))


def test_sample_is_bounded_deduplicated_and_not_full_consensus(object_store, state):
    item, calls = capture(object_store, state, [
        {**report("AP1", "A", 1), "researcher": "张某、李某", "title": "经营预测",
         "sRatingName": "增持", "emRatingName": "买入"}, report("AP1", "A", 1),
        report("AP2", "A", 3, "2026-11-02"), report("AP3", "B", 5),
    ], total_pages=8, max_pages=1)
    result = aggregate(object_store, state, [item, item])
    assert len(calls) == 1 and item.truncated
    assert result.raw_sample_count == 4
    first = item.observations[0]
    assert first.author_text == "张某、李某" and first.report_title == "经营预测"
    assert first.reported_rating == "增持" and first.provider_normalized_rating == "买入"
    assert first.report_pdf_hash is None and first.report_page is None
    assert first.report_pdf_status == "URL_ONLY_NOT_CAPTURED"
    assert result.sample_count == result.institution_count == 2
    assert result.mean == result.median == Decimal(4)
    assert result.standard_deviation == Decimal(2).sqrt()
    assert result.source_groups == ("EASTMONEY",)
    assert result.coverage == "BOUNDED_SAMPLE_NOT_FULL_CONSENSUS"
    assert not result.historical_vintage_verified


def test_same_institution_same_time_conflicts_are_excluded(object_store, state):
    item, _ = capture(object_store, state, [report("A1", "A", 1), report("A2", "A", 2)])
    result = aggregate(object_store, state, [item])
    assert result.sample_count == 0 and result.mean is None
    assert result.conflicting_institutions == ("A",)


def test_future_capture_and_foreign_company_cannot_enter_aggregate(object_store, state):
    item, _ = capture(object_store, state, [report("AP1", "A", 1)])
    with pytest.raises(ValueError, match="cutoff"):
        aggregate(
            object_store, state, [item],
            as_of=item.available_to_system_at - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="identity"):
        aggregate(object_store, state, [item], company_id="000001")


def test_year_requires_source_field_and_bad_row_cannot_leave_partial_metrics(object_store, state):
    with pytest.raises(ValueError, match="currentYear"):
        capture(object_store, state, [], year=None)
    row = report("AP1", "A", 1)
    row["predictNextYearEps"] = "NaN"
    item, _ = capture(object_store, state, [row])
    assert item.rejected_rows == 1 and not item.observations


def test_capture_tampering_cannot_change_registered_statistics(object_store, state):
    item, _ = capture(object_store, state, [report("AP1", "A", 1)])
    altered = item.model_copy(update={"observations": tuple(
        observation.model_copy(update={"value": Decimal(100)})
        for observation in item.observations
    )})
    with pytest.raises(ValueError, match="object mismatch"):
        aggregate(object_store, state, [altered])


def test_registered_backdated_wrapper_cannot_admit_future_raw_observations(object_store, state):
    item, _ = capture(object_store, state, [report("AP1", "A", 1)])
    altered = item.model_copy(update={
        "capture_id": item.capture_id + ":backdated-wrapper",
        "available_to_system_at": item.available_to_system_at - timedelta(days=1),
    })
    frozen = object_store.put_json(altered.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=altered.capture_id, artifact_type="SampledForecastCapture",
        schema_version=altered.schema_version, object_hash=frozen.sha256,
        input_hashes=[observation.source_object_hash for observation in altered.observations],
    )
    with pytest.raises(ValueError, match="raw source was unavailable at cutoff"):
        aggregate(
            object_store, state, [altered],
            as_of=item.available_to_system_at - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="visibility differs"):
        aggregate(object_store, state, [altered])


def test_page_budget_validation_prevents_dispatch(object_store, state):
    calls = []
    def handler(request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("invalid page budget must not dispatch a request")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="page budget"):
            EastMoneyForecastProvider(object_store, state, client).capture(
                "600519", Market.XSHG, date(2026, 11, 1), date(2026, 11, 20), max_pages=100
            )
    assert calls == []


def test_verified_reader_and_pure_aggregate_never_write(object_store, state, monkeypatch):
    item, _ = capture(object_store, state, [report("AP1", "A", 1), report("AP2", "B", 3)])
    cutoff = item.available_to_system_at + timedelta(seconds=1)

    def forbid_write(*args, **kwargs):
        raise AssertionError("read-only forecast consumer must not persist objects or metadata")

    monkeypatch.setattr(object_store, "put_json", forbid_write)
    monkeypatch.setattr(state, "register_artifact", forbid_write)
    loaded = load_forecast_capture(
        state, object_store, item.capture_id, company_id="600519",
        market=Market.XSHG, as_of=cutoff,
    )
    assert loaded == item
    result = aggregate(object_store, state, [loaded], as_of=cutoff, persist=False)
    assert result.mean == Decimal(2) and result.sample_count == 2
    assert state.artifact_record(result.artifact_id) is None
    with pytest.raises(ValueError, match="cutoff"):
        load_forecast_capture(
            state, object_store, item.capture_id, company_id="600519", market=Market.XSHG,
            as_of=item.available_to_system_at - timedelta(seconds=1),
        )


def test_pure_selector_preserves_version_conflicts_and_rejects_mixed_years(object_store, state):
    item, _ = capture(object_store, state, [
        report("AP1", "A", 1), report("AP2", "A", 2), report("BP1", "B", 3),
    ])
    selected, conflicts = select_latest_forecasts(list(item.observations))
    assert conflicts == ("A",)
    assert [observation.institution_id for observation in selected] == ["B"]
    mixed = [*item.observations, item.observations[0].model_copy(
        update={"target_fiscal_year": 2027}
    )]
    with pytest.raises(ValueError, match="fiscal year"):
        select_latest_forecasts(mixed)
