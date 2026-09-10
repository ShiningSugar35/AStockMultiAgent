"""Registered-source, mixed-frequency and truncation-invariance feature tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import MacroObservation
from astock.investor_orchestration.regime_features import (
    RegimeFeatureBuilder,
    RegimeFeaturePolicy,
    RegimeSeriesSpec,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def environment(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("regime-feature-pipeline")
    state = StateStore(root / "state.sqlite")
    state.migrate()
    return state, ObjectStore(root / "objects" / "sha256")


def spec(**updates: Any) -> RegimeSeriesSpec:
    values = {
        "series_id": "recorded-pmi",
        "family": "macro",
        "artifact_type": "MacroObservation",
        "value_path": ("value",),
        "observed_at_field": "published_at",
        "available_at_field": "available_to_system_at",
        "observation_key_field": "observation_period",
        "identity": {"series_key": "pmi", "authority": "NBS"},
        "unit_field": "unit",
        "expected_unit": "index",
        "normalization": "TRAILING_PERCENTILE",
        "minimum_history": 3,
        "normalization_window": 5,
        "stale_after_seconds": 5 * 86400,
    }
    values.update(updates)
    return RegimeSeriesSpec.model_validate(values)


def builder(
    environment: Any, *, vintage: str = "LATEST_AS_OF", **updates: Any
) -> RegimeFeatureBuilder:
    state, objects = environment
    policy = RegimeFeaturePolicy.model_validate(
        {
            "version": "recorded-feature-policy",
            "history_tier": "ENRICHED_HISTORY",
            "series": (spec(**updates),),
            "vintage": vintage,
        }
    )
    return RegimeFeatureBuilder(state, objects, policy)


def register_observation(environment: Any, day: int, value: float, **updates: Any) -> str:
    state, objects = environment
    at = BASE + timedelta(days=day)
    raw = objects.put_bytes(f"recorded-{day}-{value}-{updates}".encode())
    values = {
        "observation_id": str(uuid4()),
        "authority": "NBS",
        "release_family": "recorded-pmi",
        "series_key": "pmi",
        "observation_period": f"2026-01-{day + 1:02d}",
        "value": Decimal(str(value)),
        "unit": "index",
        "published_at": at,
        "ingested_at": at,
        "available_to_system_at": at,
        "revision": "first-observed",
        "source_url": "https://www.stats.gov.cn/recorded-test-only",
        "source_hash": raw.sha256,
    }
    values.update(updates)
    observation = MacroObservation.model_validate(values)
    reference = objects.put_json(observation.model_dump(mode="json"))
    identity = "macro-test:" + str(uuid4())
    state.register_artifact(
        artifact_id=identity,
        artifact_type="MacroObservation",
        schema_version="macro-observation-test-v1",
        object_hash=reference.sha256,
        input_hashes=[raw.sha256],
    )
    return identity


def test_future_append_cannot_change_a_prior_feature_snapshot(environment: Any) -> None:
    service = builder(environment)
    ids = [register_observation(environment, index, 40 + index) for index in range(8)]
    at = BASE + timedelta(days=4, hours=1)
    prefix = service.build(as_of=at, artifact_ids_by_series={"recorded-pmi": ids[:5]})
    complete = service.build(as_of=at, artifact_ids_by_series={"recorded-pmi": ids})
    assert prefix == complete
    assert prefix.snapshot.macro_credit_score == 1.0
    assert prefix.snapshot.family_coverage["macro"] == 1.0
    assert prefix.snapshot.family_coverage["trend"] == 0.0
    assert prefix.snapshot.trend_score is None
    assert prefix.production_admission == "NOT_ADMITTED"
    identifier = service.register(prefix)
    assert environment[0].artifact_record(identifier)["type"] == "RegimeFeatureBuildReport"


def test_later_revision_is_invisible_until_available_and_first_observed_is_preserved(
    environment: Any,
) -> None:
    ids = [register_observation(environment, index, 40 + index) for index in range(5)]
    revision = register_observation(
        environment,
        4,
        1,
        revision="revised",
        ingested_at=BASE + timedelta(days=6),
        available_to_system_at=BASE + timedelta(days=6),
    )
    latest = builder(environment, normalization="SYMMETRIC", scale=100)
    first = builder(environment, vintage="FIRST_OBSERVED", normalization="SYMMETRIC", scale=100)
    before = latest.build(
        as_of=BASE + timedelta(days=5), artifact_ids_by_series={"recorded-pmi": [*ids, revision]}
    )
    after = latest.build(
        as_of=BASE + timedelta(days=7), artifact_ids_by_series={"recorded-pmi": [*ids, revision]}
    )
    unchanged = first.build(
        as_of=BASE + timedelta(days=7), artifact_ids_by_series={"recorded-pmi": [*ids, revision]}
    )
    assert before.snapshot.macro_credit_score == 0.44
    assert after.snapshot.macro_credit_score == 0.01
    assert unchanged.snapshot.macro_credit_score == 0.44
    assert revision not in before.series_audits[0].source_artifact_ids


def test_missing_stale_and_insufficient_history_are_distinct(environment: Any) -> None:
    service = builder(environment)
    ids = [register_observation(environment, index, 40 + index) for index in range(2)]
    missing = service.build(as_of=BASE, artifact_ids_by_series={})
    short = service.build(
        as_of=BASE + timedelta(days=2), artifact_ids_by_series={"recorded-pmi": ids}
    )
    stale = service.build(
        as_of=BASE + timedelta(days=20), artifact_ids_by_series={"recorded-pmi": ids}
    )
    assert [item.series_audits[0].status for item in (missing, short, stale)] == [
        "MISSING",
        "INSUFFICIENT_HISTORY",
        "STALE",
    ]
    assert all(item.snapshot.macro_credit_score is None for item in (missing, short, stale))


def test_units_and_series_identity_are_exact_not_guessed(environment: Any) -> None:
    for changes in ({"unit": "percent"}, {"series_key": "another-series"}, {"authority": "PBOC"}):
        identity = register_observation(environment, 0, 50, **changes)
        with pytest.raises(ValueError, match="unit|identity"):
            builder(environment).build(
                as_of=BASE + timedelta(days=1), artifact_ids_by_series={"recorded-pmi": [identity]}
            )


def test_same_vintage_conflicting_values_are_rejected(environment: Any) -> None:
    ids = [register_observation(environment, 0, value) for value in (40, 50)]
    with pytest.raises(ValueError, match="conflicting"):
        builder(environment).build(
            as_of=BASE + timedelta(days=1), artifact_ids_by_series={"recorded-pmi": ids}
        )


def test_unregistered_and_corrupted_sources_cannot_produce_features(environment: Any) -> None:
    state, objects = environment
    with pytest.raises(ValueError, match="registered"):
        builder(environment).build(as_of=BASE, artifact_ids_by_series={"recorded-pmi": ["absent"]})
    identity = register_observation(environment, 0, 99)
    record = state.artifact_record(identity)
    assert record is not None
    objects.path_for(record["object_hash"]).write_bytes(b"isolated-test-corruption")
    with pytest.raises(StorageError):
        builder(environment).build(as_of=BASE, artifact_ids_by_series={"recorded-pmi": [identity]})


def test_source_availability_cannot_be_rebound_to_publication_date() -> None:
    with pytest.raises(ValueError, match="availability"):
        spec(available_at_field="published_at")


def test_future_and_naive_build_time_are_rejected(environment: Any) -> None:
    for at in (datetime.now(UTC) + timedelta(days=1), datetime(2026, 1, 1)):
        with pytest.raises(ValueError):
            builder(environment).build(as_of=at, artifact_ids_by_series={})


def test_duplicate_series_ids_and_unknown_series_are_rejected(environment: Any) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        RegimeFeaturePolicy(
            version="invalid", history_tier="ENRICHED_HISTORY", series=(spec(), spec())
        )
    with pytest.raises(ValueError, match="registered"):
        builder(environment).build(as_of=BASE, artifact_ids_by_series={"unknown": ()})


def test_old_period_revision_does_not_replace_the_latest_period(environment: Any) -> None:
    service = builder(environment, normalization="SYMMETRIC", scale=100)
    ids = [register_observation(environment, index, 40 + index) for index in range(5)]
    late_old = register_observation(
        environment,
        0,
        99,
        published_at=BASE + timedelta(days=7),
        ingested_at=BASE + timedelta(days=7),
        available_to_system_at=BASE + timedelta(days=7),
        revision="late-old-period",
    )
    report = service.build(
        as_of=BASE + timedelta(days=8), artifact_ids_by_series={"recorded-pmi": [*ids, late_old]}
    )
    assert report.snapshot.macro_credit_score == 0.44
    assert report.series_audits[0].source_artifact_ids[-1] == ids[-1]


def test_nested_policy_mutation_is_rejected(environment: Any) -> None:
    service = builder(environment)
    service.policy.series[0].identity["series_key"] = "mutated-after-freeze"
    with pytest.raises(ValueError, match="policy changed"):
        service.build(as_of=BASE, artifact_ids_by_series={})


def test_feature_report_mutation_is_not_registered(environment: Any) -> None:
    service = builder(environment)
    report = service.build(as_of=BASE, artifact_ids_by_series={})
    altered = report.model_copy(
        update={"snapshot": report.snapshot.model_copy(update={"macro_credit_score": 1.0})}
    )
    with pytest.raises(ValueError, match="hash"):
        service.register(altered)
