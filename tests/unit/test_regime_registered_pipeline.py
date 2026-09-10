"""Full local artifact pipeline: raw -> feature reports -> fitted model -> readback.

All market observations are labelled deterministic test inputs. This exercises
real storage and algorithms, but cannot prove market efficacy or live admission.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import MacroObservation, RegimeState
from astock.investor_orchestration.regime_challenger import (
    GaussianMarkovChallenger,
    GaussianRegimeModel,
    RegimeChallengerPolicy,
)
from astock.investor_orchestration.regime_features import (
    RegimeFeatureBuilder,
    RegimeFeaturePolicy,
    RegimeSeriesSpec,
)


def _series_spec(
    series_id: str, family: Literal["trend", "breadth"]
) -> RegimeSeriesSpec:
    return RegimeSeriesSpec(
        series_id=series_id,
        family=family,
        artifact_type="MacroObservation",
        value_path=("value",),
        observed_at_field="published_at",
        available_at_field="available_to_system_at",
        observation_key_field="observation_period",
        identity={"authority": "NBS", "series_key": series_id},
        unit_field="unit",
        expected_unit="synthetic-index",
        stale_after_seconds=172800,
    )


def test_registered_features_are_reconstructed_before_training_and_never_write_ledgers(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects" / "sha256")
    specs = (
        _series_spec("synthetic-trend", "trend"),
        _series_spec("synthetic-breadth", "breadth"),
    )
    feature_policy = RegimeFeaturePolicy(
        version="synthetic-registered-pipeline", history_tier="CORE_LONG_HISTORY", series=specs
    )
    builder = RegimeFeatureBuilder(state, objects, feature_policy)
    report_ids = []
    snapshots = []
    base = datetime(2025, 1, 1, tzinfo=UTC)
    ids: dict[str, list[str]] = {item.series_id: [] for item in specs}
    for day in range(24):
        at = base + timedelta(days=day)
        for item in specs:
            sign = 1 if day // 6 % 2 else -1
            value = sign * (0.6 if item.family == "trend" else 0.4) + 0.01 * (day % 3)
            raw = objects.put_bytes(f"SYNTHETIC TEST {item.series_id} {day} {value}".encode())
            observation = MacroObservation(
                observation_id=f"test-{item.series_id}-{day}",
                authority="NBS",
                release_family="synthetic-test-only",
                series_key=item.series_id,
                observation_period=at.date().isoformat(),
                value=str(value),
                unit="synthetic-index",
                published_at=at,
                ingested_at=at,
                available_to_system_at=at,
                revision="synthetic",
                source_url="https://www.stats.gov.cn/synthetic-test-only",
                source_hash=raw.sha256,
            )
            reference = objects.put_json(observation.model_dump(mode="json"))
            artifact_id = f"MacroObservation:test-{item.series_id}-{day}"
            state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="MacroObservation",
                schema_version="test-observation-v1",
                object_hash=reference.sha256,
                input_hashes=[raw.sha256],
            )
            ids[item.series_id].append(artifact_id)
        report = builder.build(as_of=at + timedelta(hours=1), artifact_ids_by_series=ids)
        report_id = builder.register(report)
        assert RegimeFeatureBuilder.load_registered(state, objects, report_id) == report
        report_ids.append(report_id)
        snapshots.append(report.snapshot)
    policy = RegimeChallengerPolicy(
        version="synthetic-registered-hmm",
        feature_fields=("trend_score", "breadth_score"),
        history_tier="CORE_LONG_HISTORY",
        minimum_train_samples=20,
        maximum_iterations=10,
        state_prototypes={
            RegimeState.HEALTHY_BULL: (0.5, 0.5),
            RegimeState.TREND_BEAR: (-0.5, -0.5),
        },
    )
    challenger = GaussianMarkovChallenger(policy)
    model = challenger.fit_registered(
        state,
        objects,
        report_ids[:20],
        training_cutoff=snapshots[19].as_of,
        feature_policy_hash=builder.policy_hash,
    )
    assert model.training_origin == "REGISTERED_PIT_FEATURES"
    assert model.training_artifact_ids == tuple(report_ids[:20])
    model_id = challenger.register(model, state, objects)
    record = state.artifact_record(model_id)
    assert record is not None
    recovered = GaussianRegimeModel.model_validate_json(objects.get_bytes(record["object_hash"]))
    assert challenger.filter(recovered, snapshots[20:]) == challenger.filter(model, snapshots[20:])
    assert recovered.admission_status == "SHADOW_ONLY"
    from astock.investor_orchestration.regime import MarketRegimeService
    from astock.investor_orchestration.store import InvestorOrchestrationStore

    comparison = MarketRegimeService(
        InvestorOrchestrationStore(state.path)
    ).compare_registered_challenger(
        model_artifact_id=model_id,
        feature_artifact_ids=tuple(report_ids[20:]),
        objects=objects,
    )
    assert comparison.challenger_predictions == challenger.filter(model, snapshots[20:])
    assert len(comparison.baseline_snapshots) == 4
    assert comparison.admission_status == "SHADOW_ONLY"
    assert comparison.broker_execution_allowed is False
    with closing(state.connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM market_regime_snapshots_v2").fetchone()[0] == 0
        )
        for table in ("journal", "ledger_entry", "order_record", "fill", "external_account_event"):
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
    with pytest.raises(ValueError, match="registered"):
        challenger.fit_registered(
            state,
            objects,
            ["fake-feature-report"],
            training_cutoff=snapshots[19].as_of,
            feature_policy_hash=builder.policy_hash,
        )
