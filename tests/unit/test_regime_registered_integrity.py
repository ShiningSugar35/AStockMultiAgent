"""A rehashed report is not proof that canonical inputs produced its features."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import MarketRegimeFeatureSnapshot, RegimeState
from astock.investor_orchestration.regime_challenger import (
    GaussianMarkovChallenger,
    RegimeChallengerPolicy,
)
from astock.investor_orchestration.regime_features import (
    RegimeFeatureBuilder,
    RegimeFeatureBuildReport,
    RegimeFeaturePolicy,
    RegimeSeriesSpec,
)
from astock.investor_orchestration.utils import content_hash


@pytest.fixture(scope="module")
def stores(tmp_path_factory: pytest.TempPathFactory):
    root: Path = tmp_path_factory.mktemp("regime-integrity")
    state = StateStore(root / "state.sqlite")
    state.migrate()
    return state, ObjectStore(root / "objects")


def policy() -> RegimeFeaturePolicy:
    return RegimeFeaturePolicy(
        version="recorded-integrity-policy",
        history_tier="CORE_LONG_HISTORY",
        series=(
            RegimeSeriesSpec(
                series_id="market-price",
                family="trend",
                artifact_type="DailyBarObservation",
                value_path=("close",),
                observed_at_field="session_close_at",
                available_at_field="available_to_system_at",
                observation_key_field="session_date",
                identity={"instrument_id": "INDEX:000300"},
                stale_after_seconds=86400,
            ),
        ),
    )


def test_rehashing_fabricated_features_does_not_make_them_registerable(stores) -> None:
    state, objects = stores
    builder = RegimeFeatureBuilder(state, objects, policy())
    report = builder.build(as_of=datetime(2026, 1, 1, tzinfo=UTC), artifact_ids_by_series={})
    assert report.snapshot.trend_score is None
    changed_snapshot = report.snapshot.model_copy(
        update={
            "trend_score": 1.0,
            "family_coverage": {"trend": 1.0},
        }
    )
    forged = report.model_copy(update={"snapshot": changed_snapshot})
    body = forged.model_dump(exclude={"report_hash", "schema_version", "production_admission"})
    body["snapshot"] = forged.snapshot
    body["series_audits"] = forged.series_audits
    forged = forged.model_copy(update={"report_hash": content_hash(body)})
    with pytest.raises(ValueError, match="source|replay|recomput|integrity|computed"):
        builder.register(forged)


def test_registered_training_rejects_invalid_feature_report_hash(stores) -> None:
    state, objects = stores
    feature_policy_hash = content_hash(policy())
    ids = []
    for index in range(20):
        at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        snapshot = MarketRegimeFeatureSnapshot(
            feature_snapshot_id=f"tampered-{index}",
            as_of=at,
            trend_score=0.5 if index < 10 else -0.5,
            breadth_score=0.4 if index < 10 else -0.4,
            source_revisions={"feature_policy": feature_policy_hash},
        )
        report = RegimeFeatureBuildReport(
            as_of=at,
            policy_hash=feature_policy_hash,
            history_tier="CORE_LONG_HISTORY",
            vintage="LATEST_AS_OF",
            snapshot=snapshot,
            series_audits=(),
            report_hash="forged",
        )
        reference = objects.put_json(report.model_dump(mode="json"))
        identifier = f"RegimeFeatureBuildReport:forged-{index}"
        state.register_artifact(
            artifact_id=identifier,
            artifact_type="RegimeFeatureBuildReport",
            schema_version=report.schema_version,
            object_hash=reference.sha256,
            input_hashes=[],
        )
        ids.append(identifier)
    challenger = GaussianMarkovChallenger(
        RegimeChallengerPolicy(
            version="recorded-integrity",
            feature_fields=("trend_score", "breadth_score"),
            state_prototypes={
                RegimeState.HEALTHY_BULL: (0.5, 0.5),
                RegimeState.TREND_BEAR: (-0.5, -0.5),
            },
            minimum_train_samples=20,
            maximum_iterations=3,
            history_tier="CORE_LONG_HISTORY",
        )
    )
    with pytest.raises(ValueError, match="hash|integrity|source|policy"):
        challenger.fit_registered(
            state,
            objects,
            ids,
            training_cutoff=datetime(2026, 2, 1, tzinfo=UTC),
            feature_policy_hash=feature_policy_hash,
        )
