from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel

from astock.investor_orchestration.models import (
    MarketRegimeFeatureSnapshot,
    MarketRegimeSnapshotV2,
    RegimeDecisionOverlay,
    RegimeState,
)
from astock.investor_orchestration.regime_features import FAMILY_FIELDS
from astock.investor_orchestration.regime_risk import (
    bind_profile_limits,
    effective_risk_caps,
    require_snapshot_time,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now

if TYPE_CHECKING:
    from astock.core.object_store import ObjectStore
    from astock.investor_orchestration.regime_comparison import RegimeShadowComparison


class MarketRegimeService:
    """Transparent regime scorecard with a Markov probability challenger.

    All features are expected to be normalized to [-1, 1], where positive values
    are risk-supportive. The service is read-only with respect to account and paper
    ledgers and persists only immutable regime metadata.
    """

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        config_path: str | Path = "configs/market_regime_v2.yaml",
    ) -> None:
        self.store = store
        self.config_path = Path(config_path)
        self.config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self._validate_config()
        self.policy_version = str(self.config["version"])
        self._config_hash = content_hash(self.config)

    def _assert_frozen_policy(self) -> None:
        if content_hash(self.config) != self._config_hash:
            raise ValueError("regime policy changed after validation")

    def _validate_config(self) -> None:
        if not isinstance(self.config, dict):
            raise ValueError("market regime config must be a mapping")
        required_keys = {
            "version",
            "minimum_family_coverage",
            "minimum_critical_family_coverage",
            "critical_feature_families",
            "ood_transition_threshold",
            "ood_unclassified_threshold",
            "emission_logit_scale",
            "minimum_confidence",
            "transition_confidence",
            "minimum_dwell_days",
            "snapshot_ttl_hours",
            "panic",
            "healthy_bull",
            "speculative_bull",
            "trend_bear",
            "risk_off",
            "state_overlay",
            "markov_transition",
        }
        missing = sorted(required_keys.difference(self.config))
        if missing:
            raise ValueError(f"market regime config missing required keys: {missing}")
        if not str(self.config["version"]).strip():
            raise ValueError("market regime config version must be non-empty")

        if "conservative_profile_defaults" not in self.config:
            raise ValueError("regime policy is missing conservative user-limit defaults")
        bind_profile_limits(None, self.config["conservative_profile_defaults"])
        threshold_fields = {
            "panic": {"tail_risk_max", "liquidity_max", "trend_max"},
            "healthy_bull": {
                "trend_min",
                "breadth_min",
                "tail_risk_min",
                "liquidity_min",
                "earnings_min",
                "valuation_min",
            },
            "speculative_bull": {"trend_min", "breadth_max", "tail_risk_max"},
            "trend_bear": {"trend_max", "breadth_max"},
            "risk_off": {"tail_risk_max", "liquidity_max"},
        }
        for name, expected in threshold_fields.items():
            section = self.config[name]
            if not isinstance(section, dict) or set(section) != expected:
                raise ValueError(f"regime threshold section {name} is incomplete")
            for key, value in section.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"regime threshold {name}.{key} must be numeric")
                if not math.isfinite(float(value)) or not -1 <= value <= 1:
                    raise ValueError(f"regime threshold {name}.{key} must be finite and normalized")
        if not set(self.config["critical_feature_families"]) <= set(FAMILY_FIELDS):
            raise ValueError("regime policy names an unknown critical feature family")

        bounded_keys = (
            "minimum_family_coverage",
            "minimum_critical_family_coverage",
            "ood_transition_threshold",
            "ood_unclassified_threshold",
            "minimum_confidence",
            "transition_confidence",
        )
        for key in bounded_keys:
            value = float(self.config[key])
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{key} must be finite and within [0, 1]")
        emission_logit_scale = float(self.config["emission_logit_scale"])
        if not math.isfinite(emission_logit_scale) or emission_logit_scale <= 0.0:
            raise ValueError("emission_logit_scale must be finite and positive")
        if float(self.config["ood_transition_threshold"]) >= float(
            self.config["ood_unclassified_threshold"]
        ):
            raise ValueError("OOD transition threshold must be below unclassified threshold")
        if int(self.config["minimum_dwell_days"]) < 0:
            raise ValueError("minimum_dwell_days must be non-negative")
        if int(self.config["snapshot_ttl_hours"]) <= 0:
            raise ValueError("snapshot_ttl_hours must be positive")
        critical_families = self.config["critical_feature_families"]
        if (
            not isinstance(critical_families, list)
            or not critical_families
            or any(not isinstance(item, str) or not item.strip() for item in critical_families)
            or len(set(critical_families)) != len(critical_families)
        ):
            raise ValueError("critical_feature_families must be a unique non-empty string list")

        overlays = self.config["state_overlay"]
        transitions = self.config["markov_transition"]
        if not isinstance(overlays, dict) or not isinstance(transitions, dict):
            raise ValueError("state_overlay and markov_transition must be mappings")
        expected_states = {state.value for state in RegimeState}
        if set(overlays) != expected_states:
            raise ValueError("state_overlay must define every regime state exactly once")
        if set(transitions) != expected_states:
            raise ValueError("markov_transition must define every regime state exactly once")

        numeric_overlay_fields = {
            "total_risk_multiplier",
            "new_position_multiplier",
            "single_name_multiplier",
            "recommendation_cap",
            "minimum_liquidity_percentile",
            "margin_of_safety_adjustment",
            "tranche_count",
            "no_trade_band_multiplier",
            "red_team_depth",
        }
        for state, row in overlays.items():
            if not isinstance(row, dict) or not numeric_overlay_fields.issubset(row):
                raise ValueError(f"state_overlay[{state}] is incomplete")
            values = [float(row[field]) for field in numeric_overlay_fields]
            if any(not math.isfinite(value) or value < 0.0 for value in values):
                raise ValueError(f"state_overlay[{state}] contains invalid numeric policy")

        conservative_order = (
            RegimeState.PANIC,
            RegimeState.TREND_BEAR,
            RegimeState.RISK_OFF_RANGE,
            RegimeState.NEUTRAL_RANGE,
            RegimeState.HEALTHY_BULL,
        )
        for metric in (
            "total_risk_multiplier",
            "new_position_multiplier",
            "single_name_multiplier",
            "recommendation_cap",
        ):
            values = [float(overlays[state.value][metric]) for state in conservative_order]
            if any(left > right for left, right in zip(values, values[1:], strict=False)):
                raise ValueError(f"state_overlay {metric} violates conservative monotonicity")
        speculative = overlays[RegimeState.SPECULATIVE_BULL.value]
        neutral = overlays[RegimeState.NEUTRAL_RANGE.value]
        for metric in (
            "total_risk_multiplier",
            "new_position_multiplier",
            "single_name_multiplier",
            "recommendation_cap",
        ):
            if float(speculative[metric]) > float(neutral[metric]):
                raise ValueError(f"SPECULATIVE_BULL cannot loosen {metric} beyond neutral")
        for metric in ("minimum_liquidity_percentile", "margin_of_safety_adjustment"):
            if float(speculative[metric]) < float(neutral[metric]):
                raise ValueError(f"SPECULATIVE_BULL cannot loosen {metric} beyond neutral")

        for source_state, row in transitions.items():
            if not isinstance(row, dict) or set(row) != expected_states:
                raise ValueError(f"markov_transition[{source_state}] must cover every state")
            probabilities = [float(row[state]) for state in expected_states]
            if any(not math.isfinite(value) or value < 0.0 for value in probabilities):
                raise ValueError(
                    f"markov_transition[{source_state}] contains an invalid probability"
                )
            if not math.isclose(sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6):
                raise ValueError(f"markov_transition[{source_state}] must sum to 1")

    def infer(
        self,
        features: MarketRegimeFeatureSnapshot,
        *,
        previous: MarketRegimeSnapshotV2 | None = None,
        persist: bool = True,
    ) -> MarketRegimeSnapshotV2:
        self._assert_frozen_policy()
        features = MarketRegimeFeatureSnapshot.model_validate(features.model_dump())
        if features.as_of > utc_now():
            raise ValueError("regime features cannot certify a future time")
        unknown = set(features.family_coverage) - set(FAMILY_FIELDS)
        if unknown:
            raise ValueError(f"unknown regime feature families: {sorted(unknown)}")
        if previous is not None:
            previous = MarketRegimeSnapshotV2.model_validate(previous.model_dump())
            require_snapshot_time(previous, utc_now())
        integrity_override, integrity_warnings = self._integrity_override(features)
        effective_previous = previous
        temporal_warnings: list[str] = []
        if previous is not None and previous.as_of > features.as_of:
            raise ValueError("previous regime snapshot cannot be newer than the feature snapshot")
        if previous is not None and previous.expires_at < features.as_of:
            effective_previous = None
            temporal_warnings.append("PREVIOUS_SNAPSHOT_EXPIRED")
        elif previous is not None and previous.policy_version != self.policy_version:
            effective_previous = None
            temporal_warnings.append("PREVIOUS_POLICY_VERSION_MISMATCH")
        coverage = self._coverage(features)
        baseline = self._baseline_state(features, coverage)
        emissions = self._emission_scores(features, coverage)
        baseline_probabilities = self._softmax(emissions)
        challenger_probabilities = self._markov_filter(
            baseline_probabilities,
            effective_previous.selected_state if effective_previous else None,
        )
        challenger_state = max(
            challenger_probabilities,
            key=lambda state: challenger_probabilities[state],
        )
        selected, emergency, warnings = self._select_with_hysteresis(
            baseline=baseline,
            challenger=challenger_state,
            probabilities=challenger_probabilities,
            features=features,
            coverage=coverage,
            previous=effective_previous,
        )
        warnings = [*temporal_warnings, *integrity_warnings, *warnings]
        if integrity_override is RegimeState.UNCLASSIFIED:
            selected, emergency = RegimeState.UNCLASSIFIED, False
        elif not emergency and integrity_override is not None:
            selected = integrity_override
        warnings.append("UNCALIBRATED_CONFIGURED_MARKOV_PRIOR")
        probabilities = self._probabilities_for_selected(challenger_probabilities, selected)
        confidence = self._confidence(
            probabilities,
            selected=selected,
            coverage=coverage,
            scorecards_agree=baseline is challenger_state,
        )
        minimum_confidence = float(self.config["minimum_confidence"])
        if confidence < minimum_confidence and selected not in {
            RegimeState.PANIC,
            RegimeState.UNCLASSIFIED,
        }:
            selected = RegimeState.TRANSITION
            warnings.append("LOW_CONFIDENCE_TRANSITION")
            probabilities = self._probabilities_for_selected(challenger_probabilities, selected)
            confidence = self._confidence(
                probabilities,
                selected=selected,
                coverage=coverage,
                scorecards_agree=baseline is challenger_state,
            )
        if selected is RegimeState.UNCLASSIFIED:
            confidence = min(confidence, 0.25)
        elif selected is RegimeState.TRANSITION:
            confidence = min(confidence, 0.45)
        as_of = features.as_of
        ttl_hours = int(self.config["snapshot_ttl_hours"])
        dwell_days = (
            effective_previous.dwell_days
            + max(0, (as_of.date() - effective_previous.as_of.date()).days)
            if effective_previous and effective_previous.selected_state is selected
            else 0
        )
        body = {
            "feature_snapshot_id": features.feature_snapshot_id,
            "as_of": as_of,
            "probabilities": probabilities,
            "selected_state": selected,
            "confidence": confidence,
            "baseline_state": baseline,
            "challenger_state": challenger_state,
            "disagreement": baseline is not challenger_state,
            "previous_state": (effective_previous.selected_state if effective_previous else None),
            "dwell_days": dwell_days,
            "emergency_override": emergency,
            "top_drivers": self._top_drivers(features),
            "valid_from": as_of,
            "expires_at": as_of + timedelta(hours=ttl_hours),
            "policy_version": self.policy_version,
            "model_version": "transparent-scorecard+markov-filter-v2",
            "coverage": coverage,
            "warnings": tuple(warnings),
        }
        snapshot_hash = content_hash(body)
        snapshot = MarketRegimeSnapshotV2(
            snapshot_id=f"regime-{uuid.uuid5(uuid.NAMESPACE_URL, snapshot_hash)}",
            **body,
        )
        if persist:
            self.store.save_regime_snapshot(snapshot)
        return snapshot

    def overlay(
        self,
        snapshot: MarketRegimeSnapshotV2,
        portfolio_intent_profile: BaseModel | Mapping[str, Any] | None,
        *,
        feature_lineage_audit: Mapping[str, str] | None = None,
        persist: bool = True,
    ) -> RegimeDecisionOverlay:
        self._assert_frozen_policy()
        snapshot = MarketRegimeSnapshotV2.model_validate(snapshot.model_dump())
        require_snapshot_time(snapshot, utc_now())
        if snapshot.policy_version != self.policy_version:
            raise ValueError("regime overlay cannot mix different policy versions")
        if persist:
            with self.store.connect() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM market_regime_snapshots_v2 WHERE snapshot_id=?",
                    (snapshot.snapshot_id,),
                ).fetchone()
            if row is None or MarketRegimeSnapshotV2.model_validate_json(str(row[0])) != snapshot:
                raise ValueError("persisted overlay must bind the exact registered regime snapshot")
        lineage_audit = dict(feature_lineage_audit or {})
        self._assert_no_cross_scope_duplicate_lineage(lineage_audit)
        policy = dict(self.config["state_overlay"][snapshot.selected_state.value])
        user_limits = self._profile_limits(portfolio_intent_profile)
        user_limits.update(effective_risk_caps(user_limits, policy))
        body = {
            "regime_snapshot_id": snapshot.snapshot_id,
            "policy_version": self.policy_version,
            **policy,
            "user_limit_binding": user_limits,
            "label_change_only_action_allowed": False,
            "feature_lineage_audit": lineage_audit,
        }
        overlay_hash = content_hash(body)
        overlay = RegimeDecisionOverlay(
            overlay_id=f"overlay-{uuid.uuid5(uuid.NAMESPACE_URL, overlay_hash)}",
            **body,
        )
        if persist:
            self.store.save_regime_overlay(overlay)
        return overlay

    @staticmethod
    def _assert_no_cross_scope_duplicate_lineage(lineage_audit: Mapping[str, str]) -> None:
        seen: dict[str, str] = {}
        for scope, lineage in lineage_audit.items():
            normalized_scope = str(scope).strip()
            normalized_lineage = str(lineage).strip()
            if not normalized_scope or not normalized_lineage:
                raise ValueError("feature lineage audit keys and values must be non-empty")
            previous_scope = seen.get(normalized_lineage)
            if previous_scope is not None and previous_scope != normalized_scope:
                raise ValueError(
                    "feature lineage reused across decision scopes: "
                    f"{previous_scope} and {normalized_scope}"
                )
            seen[normalized_lineage] = normalized_scope

    def _integrity_override(
        self, features: MarketRegimeFeatureSnapshot
    ) -> tuple[RegimeState | None, list[str]]:
        warnings: list[str] = []
        critical_families = tuple(self.config.get("critical_feature_families", ()))
        minimum_critical = float(self.config.get("minimum_critical_family_coverage", 0.0))
        for family in critical_families:
            coverage = float(features.family_coverage.get(str(family), 0.0))
            if getattr(features, FAMILY_FIELDS[str(family)]) is None:
                coverage = 0.0
            if coverage < minimum_critical:
                warnings.append(f"CRITICAL_FAMILY_COVERAGE_LOW:{family}")
                return RegimeState.UNCLASSIFIED, warnings

        ood_score = float(features.ood_score or 0.0)
        unclassified_threshold = float(self.config.get("ood_unclassified_threshold", 1.0))
        transition_threshold = float(self.config.get("ood_transition_threshold", 1.0))
        if ood_score >= unclassified_threshold:
            warnings.append("OOD_UNCLASSIFIED")
            return RegimeState.UNCLASSIFIED, warnings
        if ood_score >= transition_threshold:
            warnings.append("OOD_TRANSITION")
            return RegimeState.TRANSITION, warnings
        return None, warnings

    @staticmethod
    def _confidence(
        probabilities: Mapping[RegimeState, float],
        *,
        selected: RegimeState,
        coverage: float,
        scorecards_agree: bool,
    ) -> float:
        selected_probability = float(probabilities.get(selected, 0.0))
        ordered = sorted((float(value) for value in probabilities.values()), reverse=True)
        margin = selected_probability - (ordered[1] if len(ordered) > 1 else 0.0)
        agreement_factor = 1.0 if scorecards_agree else 0.85
        confidence = 0.72 * selected_probability + 0.28 * max(0.0, margin)
        confidence *= max(0.0, min(1.0, coverage)) * agreement_factor
        return max(0.0, min(1.0, confidence))

    def _coverage(self, features: MarketRegimeFeatureSnapshot) -> float:
        # The denominator is the frozen seven-family contract. An omitted family
        # or absent numerical feature cannot be made complete by deleting a key.
        return sum(
            float(features.family_coverage.get(family, 0.0))
            if getattr(features, field) is not None
            else 0.0
            for family, field in FAMILY_FIELDS.items()
        ) / len(FAMILY_FIELDS)

    def _baseline_state(
        self, features: MarketRegimeFeatureSnapshot, coverage: float
    ) -> RegimeState:
        minimum_coverage = float(self.config["minimum_family_coverage"])
        if coverage < minimum_coverage:
            return RegimeState.UNCLASSIFIED
        trend = self._value(features.trend_score)
        breadth = self._value(features.breadth_score)
        tail = self._value(features.tail_risk_score)
        liquidity = self._value(features.liquidity_score)
        earnings = self._value(features.earnings_diffusion_score)
        valuation = self._value(features.valuation_fragility_score)
        panic = self.config["panic"]
        if (
            tail <= float(panic["tail_risk_max"]) and liquidity <= float(panic["liquidity_max"])
        ) or (tail <= float(panic["tail_risk_max"]) and trend <= float(panic["trend_max"])):
            return RegimeState.PANIC
        healthy = self.config["healthy_bull"]
        if (
            features.earnings_diffusion_score is not None
            and features.valuation_fragility_score is not None
            and trend >= float(healthy["trend_min"])
            and breadth >= float(healthy["breadth_min"])
            and tail >= float(healthy["tail_risk_min"])
            and liquidity >= float(healthy["liquidity_min"])
            and earnings >= float(healthy["earnings_min"])
            and valuation >= float(healthy["valuation_min"])
        ):
            return RegimeState.HEALTHY_BULL
        speculative = self.config["speculative_bull"]
        if trend >= float(speculative["trend_min"]) and (
            breadth <= float(speculative["breadth_max"])
            or tail <= float(speculative["tail_risk_max"])
        ):
            return RegimeState.SPECULATIVE_BULL
        bear = self.config["trend_bear"]
        if trend <= float(bear["trend_max"]) and breadth <= float(bear["breadth_max"]):
            return RegimeState.TREND_BEAR
        risk_off = self.config["risk_off"]
        if tail <= float(risk_off["tail_risk_max"]) or liquidity <= float(
            risk_off["liquidity_max"]
        ):
            return RegimeState.RISK_OFF_RANGE
        return RegimeState.NEUTRAL_RANGE

    def _emission_scores(
        self, features: MarketRegimeFeatureSnapshot, coverage: float
    ) -> dict[RegimeState, float]:
        trend = self._value(features.trend_score)
        breadth = self._value(features.breadth_score)
        tail = self._value(features.tail_risk_score)
        liquidity = self._value(features.liquidity_score)
        valuation = self._value(features.valuation_fragility_score)
        earnings = self._value(features.earnings_diffusion_score)
        macro = self._value(features.macro_credit_score)
        support = (
            trend * 0.25
            + breadth * 0.20
            + tail * 0.15
            + liquidity * 0.15
            + valuation * 0.08
            + earnings * 0.10
            + macro * 0.07
        )
        stress = -(tail * 0.35 + liquidity * 0.30 + trend * 0.20 + breadth * 0.15)
        narrow_bull = trend * 0.55 - breadth * 0.25 - tail * 0.20
        uncertainty = max(0.0, 1.0 - coverage) + min(1.0, features.ood_score or 0.0)
        return {
            RegimeState.HEALTHY_BULL: support + max(0.0, breadth) * 0.35,
            RegimeState.SPECULATIVE_BULL: narrow_bull + max(0.0, -valuation) * 0.15,
            RegimeState.NEUTRAL_RANGE: 0.35 - abs(support) * 0.55,
            RegimeState.RISK_OFF_RANGE: stress * 0.65 + max(0.0, -liquidity) * 0.25,
            RegimeState.TREND_BEAR: -trend * 0.55 - breadth * 0.35 + stress * 0.20,
            RegimeState.PANIC: stress * 1.15 + max(0.0, -tail) * 0.50,
            RegimeState.TRANSITION: uncertainty * 0.45 + (1.0 - abs(support)) * 0.15,
            RegimeState.UNCLASSIFIED: uncertainty * 1.20 - coverage * 0.35,
        }

    def _softmax(self, scores: Mapping[RegimeState, float]) -> dict[RegimeState, float]:
        scale = float(self.config["emission_logit_scale"])
        maximum = max(scores.values())
        exponentials = {
            state: math.exp((value - maximum) * scale) for state, value in scores.items()
        }
        total = sum(exponentials.values())
        return {state: value / total for state, value in exponentials.items()}

    def _markov_filter(
        self,
        emissions: Mapping[RegimeState, float],
        previous_state: RegimeState | None,
    ) -> dict[RegimeState, float]:
        if previous_state is None:
            return dict(emissions)
        transition = self.config["markov_transition"][previous_state.value]
        weighted = {
            state: emissions[state] * max(float(transition.get(state.value, 0.0)), 1e-6)
            for state in RegimeState
        }
        total = sum(weighted.values())
        return {state: value / total for state, value in weighted.items()}

    def _select_with_hysteresis(
        self,
        *,
        baseline: RegimeState,
        challenger: RegimeState,
        probabilities: Mapping[RegimeState, float],
        features: MarketRegimeFeatureSnapshot,
        coverage: float,
        previous: MarketRegimeSnapshotV2 | None,
    ) -> tuple[RegimeState, bool, list[str]]:
        warnings: list[str] = []
        minimum_coverage = float(self.config["minimum_family_coverage"])
        if coverage < minimum_coverage:
            return RegimeState.UNCLASSIFIED, False, ["INSUFFICIENT_FEATURE_COVERAGE"]
        panic_baseline = baseline is RegimeState.PANIC
        emergency = panic_baseline
        if emergency:
            return RegimeState.PANIC, True, warnings
        if baseline is not challenger:
            warnings.append("BASELINE_CHALLENGER_DISAGREEMENT")
        top_probability = probabilities[challenger]
        transition_confidence = float(self.config["transition_confidence"])
        if top_probability < transition_confidence:
            selected = RegimeState.TRANSITION
        else:
            selected = challenger
        if previous is not None and selected is not previous.selected_state:
            elapsed_days = max(0, (features.as_of.date() - previous.as_of.date()).days)
            effective_dwell = previous.dwell_days + elapsed_days
            minimum_dwell = int(self.config["minimum_dwell_days"])
            if effective_dwell < minimum_dwell and selected not in {
                RegimeState.PANIC,
                RegimeState.UNCLASSIFIED,
            }:
                warnings.append("MINIMUM_DWELL_HELD_PREVIOUS_STATE")
                selected = previous.selected_state
        return selected, False, warnings

    @staticmethod
    def _probabilities_for_selected(
        probabilities: Mapping[RegimeState, float], selected: RegimeState
    ) -> dict[RegimeState, float]:
        result = dict(probabilities)
        if selected not in result:
            result[selected] = 0.0
        if result[selected] == max(result.values()):
            return result
        result[selected] += 1e-6
        total = sum(result.values())
        return {state: value / total for state, value in result.items()}

    @staticmethod
    def _feature_values(features: MarketRegimeFeatureSnapshot) -> dict[str, float | None]:
        return {
            "trend": features.trend_score,
            "breadth": features.breadth_score,
            "tail_risk": features.tail_risk_score,
            "liquidity": features.liquidity_score,
            "valuation": features.valuation_fragility_score,
            "earnings": features.earnings_diffusion_score,
            "macro_credit": features.macro_credit_score,
        }

    @staticmethod
    def _value(value: float | None) -> float:
        return 0.0 if value is None else max(-1.0, min(1.0, float(value)))

    def _top_drivers(self, features: MarketRegimeFeatureSnapshot) -> tuple[str, ...]:
        values = self._feature_values(features)
        ranked = sorted(
            ((name, value) for name, value in values.items() if value is not None),
            key=lambda item: abs(float(item[1])),
            reverse=True,
        )
        return tuple(f"{name}:{float(value):+.3f}" for name, value in ranked[:4])

    def compare_registered_challenger(
        self, *, model_artifact_id: str, feature_artifact_ids: tuple[str, ...],
        objects: ObjectStore | None = None,
    ) -> RegimeShadowComparison:
        """Compare true fitted, forward-filtered predictions without changing active state.

        The heavy numerical modules are loaded only on this explicit research
        path. No account service, risk activation or semantic agent is invoked.
        """
        from astock.core.object_store import ObjectStore
        from astock.core.state import StateStore
        from astock.investor_orchestration.regime_comparison import (
            RegimeShadowComparison,
            read_registered_predictions,
        )

        state = StateStore(self.store.path)
        object_store = objects or ObjectStore(self.store.path.parent / "objects" / "sha256")
        model, reports, predictions = read_registered_predictions(
            state, object_store, model_artifact_id=model_artifact_id,
            feature_artifact_ids=feature_artifact_ids,
        )
        baselines: list[MarketRegimeSnapshotV2] = []
        for report in reports:
            baseline = self.infer(
                report.snapshot, previous=baselines[-1] if baselines else None, persist=False,
            )
            baselines.append(baseline)
        return RegimeShadowComparison(
            model_artifact_id=model_artifact_id, model_parameter_hash=model.parameter_hash,
            feature_artifact_ids=feature_artifact_ids, baseline_snapshots=tuple(baselines),
            challenger_predictions=predictions,
            disagreement_count=sum(
                baseline.baseline_state != prediction.selected_state
                for baseline, prediction in zip(baselines, predictions, strict=True)
            ),
        )

    def _profile_limits(
        self,
        profile: BaseModel | Mapping[str, Any] | None,
    ) -> dict[str, float | int | str]:
        return bind_profile_limits(profile, self.config["conservative_profile_defaults"])
