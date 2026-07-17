from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from astock.schemas import MarketRegime, MarketRegimeFeatures, PointInTimeStatus
from astock.shadow import ShadowEvaluationService, load_shadow_evaluation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 7, 17, tzinfo=UTC)


def _features(**updates: object) -> MarketRegimeFeatures:
    payload: dict[str, object] = {
        "feature_snapshot_id": "features:boundary",
        "feature_snapshot_sha256": "a" * 64,
        "as_of": AS_OF,
        "daily_trend_score": Decimal("0"),
        "hourly_trend_score": Decimal("0"),
        "market_breadth": Decimal("0.50"),
        "new_high_low_balance": Decimal("0"),
        "turnover_ratio": Decimal("1"),
        "industry_diffusion": Decimal("0.50"),
        "volatility_percentile": Decimal("0.50"),
        "index_drawdown": Decimal("-0.05"),
        "style_relative_performance": Decimal("0"),
        "strategy_performance": Decimal("0"),
        "evidence_ids": ["evidence:market"],
        "pit_statuses": [PointInTimeStatus.CERTIFIED],
        "created_at": AS_OF,
    }
    payload.update(updates)
    return MarketRegimeFeatures.model_validate(payload)


def test_all_market_regime_precedence_and_boundaries_are_fixed() -> None:
    policy = load_shadow_evaluation_policy(
        PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
    )
    cases = [
        (
            _features(daily_trend_score=None),
            MarketRegime.UNCLASSIFIED,
        ),
        (
            _features(pit_statuses=[PointInTimeStatus.NOT_PIT_SAFE]),
            MarketRegime.UNCLASSIFIED,
        ),
        (
            _features(
                daily_trend_score=Decimal("0.20"),
                hourly_trend_score=Decimal("0"),
                market_breadth=Decimal("0.55"),
                volatility_percentile=Decimal("0.85"),
                index_drawdown=Decimal("-0.12"),
            ),
            MarketRegime.PANIC,
        ),
        (
            _features(
                daily_trend_score=Decimal("0.20"),
                hourly_trend_score=Decimal("0"),
                market_breadth=Decimal("0.55"),
                volatility_percentile=Decimal("0.70"),
            ),
            MarketRegime.HIGH_VOL_BULL,
        ),
        (
            _features(
                daily_trend_score=Decimal("0.20"),
                hourly_trend_score=Decimal("0"),
                market_breadth=Decimal("0.55"),
                volatility_percentile=Decimal("0.69"),
            ),
            MarketRegime.TREND_BULL,
        ),
        (
            _features(
                daily_trend_score=Decimal("-0.20"),
                hourly_trend_score=Decimal("0"),
                market_breadth=Decimal("0.45"),
            ),
            MarketRegime.TREND_BEAR,
        ),
        (_features(), MarketRegime.RANGE),
    ]
    for features, expected in cases:
        regime, reasons = ShadowEvaluationService._market_regime(  # noqa: SLF001
            features,
            policy,
        )
        assert regime is expected
        assert reasons
