"""Period/vintage and parser fail-closed regressions over isolated recorded sources."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from astock.core.object_store import ObjectStore
from astock.investor_orchestration.macro import (
    ImmutableRawObjectStore,
    MacroReleaseSpec,
    OfficialMacroCaptureService,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore

AT = datetime(2026, 9, 7, 12, tzinfo=UTC)
FAMILY = "recorded-vintage-regression"
PATTERN = r"period=(?P<period>[0-9Q-]+); value=(?P<value>[-+.A-Za-z0-9]+)"


@pytest.fixture
def service(tmp_path: Path) -> OfficialMacroCaptureService:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    result = OfficialMacroCaptureService(
        store, object_store=ImmutableRawObjectStore(tmp_path / "objects")
    )
    result.config["release_specs"].append(_spec().model_dump())
    return result


def _spec(**overrides: object) -> MacroReleaseSpec:
    rule: dict[str, object] = {
        "series_key": "pmi",
        "pattern": PATTERN,
        "unit": "percent",
        "min_value": 0,
        "max_value": 100,
    }
    rule.update(overrides)
    return MacroReleaseSpec(
        authority="NBS",
        release_family=FAMILY,
        url="https://www.stats.gov.cn/recorded-vintage-regression",
        parser="regex-v1",
        regex_observations=(rule,),
    )


def _capture(
    service: OfficialMacroCaptureService,
    body: str,
    *,
    offset: int = 0,
    published: str = "Tue, 01 Sep 2026 01:00:00 GMT",
    spec: MacroReleaseSpec | None = None,
):
    return service.capture(
        spec or _spec(),
        recorded_content=body.encode(),
        recorded_headers={"content-type": "text/plain; charset=utf-8", "last-modified": published},
        captured_at=AT + timedelta(minutes=offset),
    )


def _observation(
    service: OfficialMacroCaptureService,
    *,
    as_of: datetime,
    vintage: Literal["LATEST", "FIRST_RELEASE", "FIRST_OBSERVED"] = "LATEST",
):
    return service.observation_at(
        authority="NBS",
        release_family=FAMILY,
        series_key="pmi",
        observation_period="2026-08",
        as_of=as_of,
        vintage=vintage,
    )


def test_late_old_period_does_not_replace_newer_current_observation(service) -> None:
    current = _capture(service, "period=2026-08; value=51.1")
    _capture(service, "period=2026-07; value=48.2", offset=1)
    latest = service.latest_valid_snapshot(
        authority="NBS", release_family=FAMILY, as_of=AT + timedelta(minutes=2)
    )
    assert latest == current


def test_latest_edition_is_not_late_arrival_of_an_older_publication(service) -> None:
    first = _capture(service, "period=2026-08; value=50.1")
    revised = _capture(
        service, "period=2026-08; value=50.4", offset=1, published="Wed, 02 Sep 2026 01:00:00 GMT"
    )
    _capture(
        service, "period=2026-08; value=49.0", offset=2, published="Mon, 31 Aug 2026 01:00:00 GMT"
    )
    assert _observation(service, as_of=AT) == first.observations[0]
    assert _observation(service, as_of=AT + timedelta(minutes=3)) == revised.observations[0]
    assert (
        _observation(service, as_of=AT + timedelta(minutes=3), vintage="FIRST_OBSERVED")
        == first.observations[0]
    )
    assert _observation(service, as_of=AT + timedelta(minutes=3), vintage="FIRST_RELEASE") is None


@pytest.mark.parametrize("period", ["2026-00", "2026-13", "2026-02-30", "2026-Q5", "0000-08"])
def test_invalid_period_keeps_raw_but_cannot_certify_a_structured_value(service, period) -> None:
    release = _capture(service, f"period={period}; value=50.1")
    assert release.parse_status == "FAILED"
    assert not release.observations
    assert any("INVALID_OBSERVATION_PERIOD" in warning for warning in release.warnings)
    assert ObjectStore(service.object_store.root).verify(release.source_hash)


def test_publication_date_cannot_invent_a_missing_observation_period(service) -> None:
    release = _capture(service, "value=50.1", spec=_spec(pattern=r"value=(?P<value>[0-9.]+)"))
    assert release.parse_status == "FAILED"
    assert not release.observations
    assert any("OBSERVATION_PERIOD_MISSING" in warning for warning in release.warnings)


def test_numeric_values_require_explicit_nonempty_unit(service) -> None:
    release = _capture(service, "period=2026-08; value=50.1", spec=_spec(unit=None))
    assert release.parse_status == "FAILED"
    assert not release.observations
    assert any("OBSERVATION_UNIT_MISSING" in warning for warning in release.warnings)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_values_are_reported_not_admitted_or_unhandled(service, value) -> None:
    release = _capture(
        service, f"period=2026-08; value={value}", spec=_spec(min_value=None, max_value=None)
    )
    assert release.parse_status == "FAILED"
    assert not release.observations
    assert any("NONFINITE_DECIMAL_VALUE" in warning for warning in release.warnings)
    assert ObjectStore(service.object_store.root).verify(release.source_hash)


def test_conflicting_same_period_matches_are_not_silently_first_match_wins(service) -> None:
    release = _capture(service, "period=2026-08; value=50.1\nperiod=2026-08; value=48.9")
    assert release.parse_status == "FAILED"
    assert not release.observations
    assert any("CONFLICTING_OBSERVATION" in warning for warning in release.warnings)


def test_identical_repeated_prose_deduplicates_without_false_conflict(service) -> None:
    release = _capture(service, "period=2026-08; value=50.1\nperiod=2026-08; value=50.1")
    assert release.parse_status == "PASS"
    assert len(release.observations) == 1
    assert release.observations[0].value == Decimal("50.1")


def test_same_timestamp_conflicting_vintages_do_not_tie_break_by_random_id(service) -> None:
    _capture(service, "period=2026-08; value=50.1")
    _capture(service, "period=2026-08; value=48.0")
    with pytest.raises(ValueError, match="conflict"):
        _observation(service, as_of=AT)


def test_repeated_capture_retains_original_availability(service) -> None:
    first = _capture(service, "period=2026-08; value=50.1")
    repeated = _capture(service, "period=2026-08; value=50.1", offset=1)
    assert first == repeated
    assert repeated.observations[0].available_to_system_at == AT
    assert _observation(service, as_of=AT - timedelta(seconds=1)) is None


def test_current_selection_refuses_incomparable_frequencies(service) -> None:
    _capture(service, "period=2026-08; value=50.1")
    _capture(service, "period=2026-Q3; value=50.2", offset=1)
    with pytest.raises(ValueError, match="frequency"):
        service.latest_valid_snapshot(
            authority="NBS", release_family=FAMILY, as_of=AT + timedelta(minutes=2)
        )


@pytest.mark.parametrize("offset_hours", [-7, 8])
def test_cutoff_is_same_instant_not_lexical_local_time(service, offset_hours: int) -> None:
    from datetime import timezone

    earlier = _capture(service, "period=2026-08; value=50.1")
    _capture(service, "period=2026-08; value=50.9", offset=10)
    cutoff = (AT + timedelta(minutes=5)).astimezone(timezone(timedelta(hours=offset_hours)))
    assert (
        service.latest_valid_snapshot(authority="NBS", release_family=FAMILY, as_of=cutoff)
        == earlier
    )
    assert _observation(service, as_of=cutoff) == earlier.observations[0]


def test_changed_parser_cannot_reuse_old_pass_when_current_values_are_invalid(service) -> None:
    first = _capture(service, "period=2026-08; value=50.1")
    with pytest.raises(ValueError, match="parser|semantic|reparse"):
        _capture(service, "period=2026-08; value=50.1", offset=1, spec=_spec(unit=None))
    assert (
        service.store.macro_release_by_source(
            authority="NBS", release_family=FAMILY, source_hash=first.source_hash
        )
        == first
    )
    assert ObjectStore(service.object_store.root).verify(first.source_hash)


def test_feature_builder_keeps_newest_published_edition_not_old_late_download(service) -> None:
    from astock.core.state import StateStore
    from tests.unit.test_regime_feature_pipeline import BASE, builder, register_observation

    environment = (StateStore(service.store.path), ObjectStore(service.object_store.root))
    first = register_observation(environment, 4, 40)
    revised = register_observation(
        environment,
        4,
        44,
        published_at=BASE + timedelta(days=5),
        ingested_at=BASE + timedelta(days=5),
        available_to_system_at=BASE + timedelta(days=5),
    )
    late_old = register_observation(
        environment,
        4,
        40,
        ingested_at=BASE + timedelta(days=7),
        available_to_system_at=BASE + timedelta(days=7),
    )
    latest = builder(environment, normalization="SYMMETRIC", scale=100)
    original = builder(environment, vintage="FIRST_OBSERVED", normalization="SYMMETRIC", scale=100)
    for ids in ([first, revised, late_old], [late_old, revised, first]):
        report = latest.build(
            as_of=BASE + timedelta(days=8), artifact_ids_by_series={"recorded-pmi": ids}
        )
        assert report.snapshot.macro_credit_score == 0.44
        assert report.series_audits[0].source_artifact_ids == (revised,)
        unchanged = original.build(
            as_of=BASE + timedelta(days=8), artifact_ids_by_series={"recorded-pmi": ids}
        )
        assert unchanged.snapshot.macro_credit_score == 0.40


@pytest.mark.parametrize("bound", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_bounds_preserve_failure_receipt(service, bound: str) -> None:
    result = _capture(service, "period=2026-08; value=50.1", spec=_spec(max_value=bound))
    assert result.parse_status == "FAILED" and not result.observations
    assert "INVALID_VALUE_BOUNDS:pmi" in result.warnings


def test_match_budget_cannot_report_truncated_success(service) -> None:
    service.config["transport"]["maximum_observation_matches"] = 1
    result = _capture(service, "period=2026-08; value=50.1\nperiod=2026-08; value=50.1")
    assert result.parse_status == "FAILED" and not result.observations
    assert "OBSERVATION_MATCH_BUDGET_EXCEEDED" in result.warnings
