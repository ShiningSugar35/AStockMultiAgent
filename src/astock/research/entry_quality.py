"""Deterministic, non-authoritative price-location and entry-quality research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from pathlib import Path
from statistics import mean, stdev

import yaml

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import DailyBarObservation
from astock.schemas.entry_quality import EntryQualitySnapshot, EntryQualityState, EntryQualityWindow


@dataclass(frozen=True, slots=True)
class EntryQualityPolicy:
    policy_version: str
    minimum_sessions: int
    full_history_sessions: int
    windows: tuple[int, ...]
    moving_average_windows: tuple[int, ...]
    location_weight: float
    trend_weight: float
    activity_weight: float
    location_target: float
    falling_knife_max_return_20: float
    falling_knife_max_ma50_distance: float
    falling_knife_max_trend_alignment: float
    extended_min_range_position_250: float
    extended_min_ma50_distance: float
    dislocation_max_range_position_250: float
    dislocation_max_drawdown_250: float
    dislocation_min_return_20: float
    dislocation_min_trend_alignment: float
    base_max_range_position_120: float
    base_max_abs_return_20: float
    base_min_trend_alignment: float
    trend_min_alignment: float
    trend_min_return_60: float
    partial_history_score_cap: float


def load_entry_quality_policy(path: Path) -> EntryQualityPolicy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "entry-quality-policy-v1":
        raise ValueError("unsupported entry-quality policy")
    weights = raw.get("score_weights")
    falling = raw.get("falling_knife")
    extended = raw.get("extended")
    dislocation = raw.get("attractive_dislocation")
    base = raw.get("base_building")
    trend = raw.get("trend_confirmed")
    safety = raw.get("safety")
    sections = (weights, falling, extended, dislocation, base, trend, safety)
    if not all(isinstance(item, dict) for item in sections):
        raise ValueError("entry-quality policy sections are invalid")
    assert isinstance(weights, dict)
    assert isinstance(falling, dict)
    assert isinstance(extended, dict)
    assert isinstance(dislocation, dict)
    assert isinstance(base, dict)
    assert isinstance(trend, dict)
    assert isinstance(safety, dict)
    if safety.get("standalone_recommendation_allowed") is not False or safety.get(
        "standalone_weight_authority_allowed"
    ) is not False:
        raise ValueError("entry-quality must remain research-only")
    windows = tuple(sorted({int(value) for value in raw.get("windows", [])}))
    ma_windows = tuple(sorted({int(value) for value in raw.get("moving_average_windows", [])}))
    policy = EntryQualityPolicy(
        policy_version=str(raw.get("policy_version") or ""),
        minimum_sessions=int(raw.get("minimum_sessions", 0)),
        full_history_sessions=int(raw.get("full_history_sessions", 0)),
        windows=windows,
        moving_average_windows=ma_windows,
        location_weight=float(weights.get("location", 0)),
        trend_weight=float(weights.get("trend", 0)),
        activity_weight=float(weights.get("activity", 0)),
        location_target=float(raw.get("location_target", 0.4)),
        falling_knife_max_return_20=float(falling.get("max_return_20", -0.12)),
        falling_knife_max_ma50_distance=float(falling.get("max_ma50_distance", -0.05)),
        falling_knife_max_trend_alignment=float(falling.get("max_trend_alignment", 0.25)),
        extended_min_range_position_250=float(extended.get("min_range_position_250", 0.9)),
        extended_min_ma50_distance=float(extended.get("min_ma50_distance", 0.15)),
        dislocation_max_range_position_250=float(dislocation.get("max_range_position_250", 0.4)),
        dislocation_max_drawdown_250=float(dislocation.get("max_drawdown_250", -0.18)),
        dislocation_min_return_20=float(dislocation.get("min_return_20", -0.10)),
        dislocation_min_trend_alignment=float(dislocation.get("min_trend_alignment", 0.45)),
        base_max_range_position_120=float(base.get("max_range_position_120", 0.5)),
        base_max_abs_return_20=float(base.get("max_abs_return_20", 0.08)),
        base_min_trend_alignment=float(base.get("min_trend_alignment", 0.25)),
        trend_min_alignment=float(trend.get("min_trend_alignment", 0.75)),
        trend_min_return_60=float(trend.get("min_return_60", 0.05)),
        partial_history_score_cap=float(safety.get("partial_history_score_cap", 0.70)),
    )
    if not policy.policy_version or not policy.windows or not policy.moving_average_windows:
        raise ValueError("entry-quality policy is incomplete")
    if policy.minimum_sessions < 20 or policy.full_history_sessions < policy.minimum_sessions:
        raise ValueError("entry-quality history requirements are invalid")
    if abs(policy.location_weight + policy.trend_weight + policy.activity_weight - 1.0) > 1e-9:
        raise ValueError("entry-quality score weights must sum to one")
    if not 0 <= policy.partial_history_score_cap <= 1:
        raise ValueError("entry-quality partial-history score cap is invalid")
    return policy


class EntryQualityService:
    def __init__(self, policy: EntryQualityPolicy) -> None:
        self.policy = policy

    def build(
        self,
        bars: list[DailyBarObservation],
        *,
        source_artifact_id: str,
        source_object_hash: str,
        as_of: object,
        current_price_override: Decimal | None = None,
        price_source_artifact_id: str | None = None,
        price_source_object_hash: str | None = None,
        benchmark_bars: list[DailyBarObservation] | None = None,
        benchmark_source_artifact_id: str | None = None,
        benchmark_source_object_hash: str | None = None,
    ) -> EntryQualitySnapshot:
        from datetime import datetime

        if not isinstance(as_of, datetime) or as_of.tzinfo is None:
            raise ValueError("entry-quality as_of must be timezone-aware")
        visible = sorted(
            (item for item in bars if item.available_to_system_at <= as_of),
            key=lambda item: item.session_date,
        )
        if len(visible) < 2:
            raise ValueError("entry-quality requires at least two visible daily bars")
        if len({item.session_date for item in visible}) != len(visible):
            raise ValueError("entry-quality daily bars contain duplicate sessions")
        instrument_ids = {item.instrument_id for item in visible}
        if len(instrument_ids) != 1:
            raise ValueError("entry-quality bars must belong to one instrument")

        if (price_source_artifact_id is None) != (price_source_object_hash is None):
            raise ValueError("entry-quality price lineage must be complete or absent")
        price_lineage_complete = (
            price_source_artifact_id is not None and price_source_object_hash is not None
        )
        latest = visible[-1]
        current_price = (
            current_price_override
            if current_price_override is not None and price_lineage_complete
            else latest.close
        )
        windows = [
            self._window(visible, window, current_price, created_at=as_of)
            for window in self.policy.windows
            if len(visible) >= 2
        ]
        ma_distance = self._moving_average_distance(visible, current_price)
        trend_alignment = self._trend_alignment(current_price, ma_distance)
        volume_ratio = self._activity_ratio([float(item.volume) for item in visible])
        amount_values = [float(item.amount) for item in visible if item.amount is not None]
        amount_ratio = (
            self._activity_ratio(amount_values)
            if len(amount_values) == len(visible)
            else None
        )
        benchmark_lineage_complete = (
            benchmark_source_artifact_id is not None
            and benchmark_source_object_hash is not None
        )
        if (benchmark_source_artifact_id is None) != (benchmark_source_object_hash is None):
            raise ValueError("entry-quality benchmark lineage must be complete or absent")
        relative_strength = self._relative_strength_60(
            visible,
            benchmark_bars,
            as_of=as_of,
        )
        if not benchmark_lineage_complete:
            relative_strength = None
        discontinuity = self._has_corporate_action_discontinuity(visible)

        by_window = {item.window_days: item for item in windows}
        return_20 = float(
            by_window[min(by_window, key=lambda value: abs(value - 20))].return_ratio
        )
        return_60 = float(
            by_window[min(by_window, key=lambda value: abs(value - 60))].return_ratio
        )
        window_120 = by_window[min(by_window, key=lambda value: abs(value - 120))]
        window_250 = by_window[min(by_window, key=lambda value: abs(value - 250))]
        ma50 = float(ma_distance.get("MA50", Decimal("0")))
        has_full_location_history = (
            window_250.observations >= self.policy.full_history_sessions
        )
        has_base_history = window_120.observations >= window_120.window_days

        falling = (
            return_20 <= self.policy.falling_knife_max_return_20
            and ma50 <= self.policy.falling_knife_max_ma50_distance
            and trend_alignment <= self.policy.falling_knife_max_trend_alignment
        )
        extended = (
            has_full_location_history
            and window_250.range_position >= self.policy.extended_min_range_position_250
            and ma50 >= self.policy.extended_min_ma50_distance
        )
        reasons = ["RAW_UNADJUSTED_RESEARCH_PRICE_SERIES"]
        if current_price_override is not None and not price_lineage_complete:
            reasons.append("PRICE_OVERRIDE_LINEAGE_UNAVAILABLE")
        if benchmark_bars is not None and not benchmark_lineage_complete:
            reasons.append("BENCHMARK_LINEAGE_UNAVAILABLE")
        if len(visible) < self.policy.full_history_sessions:
            reasons.extend(
                ["PARTIAL_LONG_HORIZON_HISTORY", "LONG_HORIZON_LOCATION_NOT_ADMITTED"]
            )
        if discontinuity:
            reasons.append("CORPORATE_ACTION_DISCONTINUITY_RISK")

        if len(visible) < self.policy.minimum_sessions or discontinuity:
            state = EntryQualityState.INSUFFICIENT_HISTORY
        elif falling:
            state = EntryQualityState.FALLING_KNIFE_RISK
            reasons.append("DOWNTREND_NOT_LOW_RISK_ENTRY")
        elif extended:
            state = EntryQualityState.EXTENDED
            reasons.append("PRICE_EXTENDED_FROM_INTERMEDIATE_TREND")
        elif (
            has_full_location_history
            and window_250.range_position <= self.policy.dislocation_max_range_position_250
            and float(window_250.drawdown_from_high)
            <= self.policy.dislocation_max_drawdown_250
            and return_20 >= self.policy.dislocation_min_return_20
            and trend_alignment >= self.policy.dislocation_min_trend_alignment
        ):
            state = EntryQualityState.ATTRACTIVE_DISLOCATION
            reasons.append("LOWER_PRICE_LOCATION_WITH_STABILIZING_TREND")
        elif (
            has_base_history
            and window_120.range_position <= self.policy.base_max_range_position_120
            and abs(return_20) <= self.policy.base_max_abs_return_20
            and trend_alignment >= self.policy.base_min_trend_alignment
        ):
            state = EntryQualityState.BASE_BUILDING
            reasons.append("LOWER_RANGE_BASE_BUILDING")
        elif (
            trend_alignment >= self.policy.trend_min_alignment
            and return_60 >= self.policy.trend_min_return_60
        ):
            state = EntryQualityState.TREND_CONFIRMED
            reasons.append("TREND_CONFIRMED_NOT_ABSOLUTE_LOW")
        else:
            state = EntryQualityState.NEUTRAL

        location_score = max(
            0.0,
            1.0 - abs(window_250.range_position - self.policy.location_target) / max(
                self.policy.location_target, 1 - self.policy.location_target
            ),
        )
        activity_score = min(1.0, max(0.0, amount_ratio or volume_ratio or 0.5))
        score = (
            self.policy.location_weight * location_score
            + self.policy.trend_weight * trend_alignment
            + self.policy.activity_weight * activity_score
        )
        if falling:
            score -= 0.30
        if extended:
            score -= 0.15
        if len(visible) < self.policy.full_history_sessions:
            score = min(score, self.policy.partial_history_score_cap)
        if discontinuity or len(visible) < self.policy.minimum_sessions:
            score = min(score, 0.40)
        score = min(1.0, max(0.0, score))

        payload = {
            "instrument_id": latest.instrument_id,
            "as_of": as_of.isoformat(),
            "latest_session_date": latest.session_date.isoformat(),
            "price": str(current_price),
            "state": state.value,
            "score": round(score, 8),
            "windows": [item.model_dump(mode="json") for item in windows],
            "moving_average_distance": {
                key: str(value) for key, value in sorted(ma_distance.items())
            },
            "source_artifact_id": source_artifact_id,
            "source_object_hash": source_object_hash,
            "price_source_artifact_id": price_source_artifact_id,
            "price_source_object_hash": price_source_object_hash,
            "benchmark_source_artifact_id": benchmark_source_artifact_id,
            "benchmark_source_object_hash": benchmark_source_object_hash,
            "relative_strength_60d": (
                str(relative_strength) if relative_strength is not None else None
            ),
            "reason_codes": sorted(set(reasons)),
        }
        return EntryQualitySnapshot(
            entry_quality_id="entry-quality:" + content_hash(payload),
            instrument_id=latest.instrument_id,
            as_of=as_of,
            latest_session_date=latest.session_date,
            current_price=current_price,
            state=state,
            score=score,
            windows=windows,
            moving_average_distance=dict(sorted(ma_distance.items())),
            trend_alignment_score=trend_alignment,
            volume_ratio_5d_to_20d=volume_ratio,
            amount_ratio_5d_to_20d=amount_ratio,
            relative_strength_60d=relative_strength,
            benchmark_source_artifact_id=benchmark_source_artifact_id,
            benchmark_source_object_hash=benchmark_source_object_hash,
            falling_knife_risk=falling,
            extended_risk=extended,
            reason_codes=sorted(set(reasons)),
            source_artifact_id=source_artifact_id,
            source_object_hash=source_object_hash,
            price_source_artifact_id=price_source_artifact_id,
            price_source_object_hash=price_source_object_hash,
            source_snapshot_ids=sorted({item.source_snapshot_id for item in visible}),
            created_at=as_of,
        )

    @staticmethod
    def _window(
        bars: list[DailyBarObservation],
        window: int,
        current_price: Decimal,
        *,
        created_at: datetime,
    ) -> EntryQualityWindow:
        selected = bars[-min(window, len(bars)) :]
        closes = [float(item.close) for item in selected]
        current = float(current_price)
        low = min(min(float(item.low) for item in selected), current)
        high = max(max(float(item.high) for item in selected), current)
        position = 0.5 if high == low else (current - low) / (high - low)
        returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
        volatility = stdev(returns) * sqrt(252) if len(returns) >= 2 else 0.0
        start = closes[0]
        return EntryQualityWindow(
            window_days=window,
            observations=len(selected),
            range_position=min(1.0, max(0.0, position)),
            return_ratio=Decimal(str(current / start - 1)),
            drawdown_from_high=Decimal(str(current / high - 1)),
            realized_volatility_annualized=max(0.0, volatility),
            created_at=created_at,
        )

    def _moving_average_distance(
        self,
        bars: list[DailyBarObservation],
        current_price: Decimal,
    ) -> dict[str, Decimal]:
        closes = [float(item.close) for item in bars]
        current = float(current_price)
        result: dict[str, Decimal] = {}
        for window in self.policy.moving_average_windows:
            if len(closes) < window:
                continue
            average = mean(closes[-window:])
            result[f"MA{window}"] = Decimal(str(current / average - 1))
        return result

    def _trend_alignment(self, price: Decimal, distances: dict[str, Decimal]) -> float:
        levels: list[float] = []
        current = float(price)
        for window in self.policy.moving_average_windows:
            distance = distances.get(f"MA{window}")
            if distance is None:
                continue
            levels.append(current / (1 + float(distance)))
        if not levels:
            return 0.5
        conditions = [current >= levels[0]]
        conditions.extend(levels[index - 1] >= levels[index] for index in range(1, len(levels)))
        return sum(conditions) / len(conditions)

    @staticmethod
    def _activity_ratio(values: list[float]) -> float | None:
        if len(values) < 20:
            return None
        recent = mean(values[-5:])
        baseline = mean(values[-20:])
        if baseline <= 0:
            return None
        return max(0.0, recent / baseline)

    @staticmethod
    def _relative_strength_60(
        bars: list[DailyBarObservation],
        benchmark_bars: list[DailyBarObservation] | None,
        *,
        as_of: object,
    ) -> Decimal | None:
        from datetime import datetime

        if benchmark_bars is None or not isinstance(as_of, datetime) or as_of.tzinfo is None:
            return None
        benchmark_visible = sorted(
            (item for item in benchmark_bars if item.available_to_system_at <= as_of),
            key=lambda item: item.session_date,
        )
        if len({item.instrument_id for item in benchmark_visible}) != 1:
            return None
        if len({item.session_date for item in benchmark_visible}) != len(benchmark_visible):
            return None
        stock_by_session = {item.session_date: item for item in bars}
        benchmark_by_session = {item.session_date: item for item in benchmark_visible}
        common_sessions = sorted(stock_by_session.keys() & benchmark_by_session.keys())
        if len(common_sessions) < 60:
            return None
        start_session = common_sessions[-60]
        end_session = common_sessions[-1]
        stock = (
            float(stock_by_session[end_session].close)
            / float(stock_by_session[start_session].close)
            - 1
        )
        benchmark = (
            float(benchmark_by_session[end_session].close)
            / float(benchmark_by_session[start_session].close)
            - 1
        )
        return Decimal(str(stock - benchmark))

    @staticmethod
    def _has_corporate_action_discontinuity(bars: list[DailyBarObservation]) -> bool:
        for previous, current in zip(bars, bars[1:], strict=False):
            if float(current.close) / float(previous.close) - 1 <= -0.35:
                return True
            if float(current.close) / float(previous.close) - 1 >= 0.35:
                return True
        return False


def persist_entry_quality_snapshot(
    state: StateStore,
    objects: ObjectStore,
    snapshot: EntryQualitySnapshot,
) -> str:
    parent = state.artifact_record(snapshot.source_artifact_id)
    if (
        parent is None
        or str(parent["object_hash"]) != snapshot.source_object_hash
        or not objects.verify(snapshot.source_object_hash)
    ):
        raise ValueError("entry-quality source artifact is unavailable or drifted")
    input_hashes = [snapshot.source_object_hash]
    if snapshot.price_source_artifact_id is not None:
        assert snapshot.price_source_object_hash is not None
        price_parent = state.artifact_record(snapshot.price_source_artifact_id)
        if (
            price_parent is None
            or str(price_parent["object_hash"]) != snapshot.price_source_object_hash
            or not objects.verify(snapshot.price_source_object_hash)
        ):
            raise ValueError("entry-quality price artifact is unavailable or drifted")
        input_hashes.append(snapshot.price_source_object_hash)
    if snapshot.benchmark_source_artifact_id is not None:
        assert snapshot.benchmark_source_object_hash is not None
        benchmark_parent = state.artifact_record(snapshot.benchmark_source_artifact_id)
        if (
            benchmark_parent is None
            or str(benchmark_parent["object_hash"]) != snapshot.benchmark_source_object_hash
            or not objects.verify(snapshot.benchmark_source_object_hash)
        ):
            raise ValueError("entry-quality benchmark artifact is unavailable or drifted")
        input_hashes.append(snapshot.benchmark_source_object_hash)
    ref = objects.put_json(snapshot.model_dump(mode="json"))
    artifact_id = f"EntryQualitySnapshot:{snapshot.entry_quality_id}"
    existing = state.artifact_record(artifact_id)
    if existing is not None:
        if (
            str(existing["type"]) != "EntryQualitySnapshot"
            or str(existing["object_hash"]) != ref.sha256
        ):
            raise ValueError("entry-quality artifact identity collision")
        return artifact_id
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type="EntryQualitySnapshot",
        schema_version=snapshot.schema_version,
        object_hash=ref.sha256,
        input_hashes=sorted(set(input_hashes)),
    )
    return artifact_id


__all__ = [
    "EntryQualityPolicy",
    "EntryQualityService",
    "load_entry_quality_policy",
    "persist_entry_quality_snapshot",
]
