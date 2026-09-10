"""Causal, vintage-aware feature views over canonical registered artifacts.

No raw observations are copied to a new fact store. A versioned series binding
selects numeric fields from existing typed artifacts; transforms are trailing
only, missing inputs stay missing, and every result retains its source hashes.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import MarketRegimeFeatureSnapshot, StrictModel
from astock.investor_orchestration.utils import content_hash, utc_now

FAMILY_FIELDS = {
    "trend": "trend_score",
    "breadth": "breadth_score",
    "tail": "tail_risk_score",
    "liquidity": "liquidity_score",
    "valuation": "valuation_fragility_score",
    "earnings": "earnings_diffusion_score",
    "macro": "macro_credit_score",
}
_SOURCE_MODELS = {
    "DailyBarObservation": "astock.schemas.reference_data",
    "MacroObservation": "astock.investor_orchestration.models",
    "FinancialIntegrityEvidencePack": "astock.schemas.financial",
    "PortfolioAnalysisReport": "astock.schemas.portfolio",
    "IndustryProfile": "astock.schemas.institutional_research",
    "ValuationPack": "astock.schemas.institutional_research",
}


class RegimeSeriesSpec(StrictModel):
    series_id: str = Field(min_length=1)
    family: Literal["trend", "breadth", "tail", "liquidity", "valuation", "earnings", "macro"]
    artifact_type: str
    value_path: tuple[str | int, ...] = Field(min_length=1)
    observed_at_field: str
    available_at_field: str
    observation_key_field: str
    # Explicit instrument/series equality constraints prevent cross-security mixtures.
    identity: dict[str, str] = Field(default_factory=dict)
    unit_field: str | None = None
    expected_unit: str | None = None
    transform: Literal["LEVEL", "CHANGE", "RETURN", "VOLATILITY", "DRAWDOWN"] = "LEVEL"
    window: int = Field(default=1, ge=1, le=1000)
    normalization: Literal["SYMMETRIC", "UNIT_INTERVAL", "TRAILING_PERCENTILE"] = "SYMMETRIC"
    scale: float = Field(default=1.0, gt=0)
    direction: Literal[-1, 1] = 1
    minimum_history: int = Field(default=20, ge=2, le=1000)
    normalization_window: int = Field(default=250, ge=2, le=2000)
    stale_after_seconds: int = Field(gt=0)
    weight: float = Field(default=1.0, gt=0)
    annualization_periods: int = Field(default=252, ge=1, le=365)

    @model_validator(mode="after")
    def valid_binding(self) -> RegimeSeriesSpec:
        if self.artifact_type not in _SOURCE_MODELS:
            raise ValueError("regime feature source must use a supported canonical model")
        if self.normalization_window < self.minimum_history:
            raise ValueError("normalization window cannot be shorter than minimum history")
        if self.transform != "LEVEL" and self.window < 2:
            raise ValueError("trailing transform needs at least two observations")
        if (self.unit_field is None) != (self.expected_unit is None):
            raise ValueError("unit field and expected unit must be bound together")
        expected_availability = (
            "available_to_system_at"
            if self.artifact_type in {"DailyBarObservation", "MacroObservation"}
            else "created_at"
        )
        if self.available_at_field != expected_availability:
            raise ValueError("feature source must use its canonical availability field")
        if self.artifact_type == "DailyBarObservation" and (
            "instrument_id" not in self.identity
            or self.observed_at_field != "session_close_at"
            or self.observation_key_field != "session_date"
        ):
            raise ValueError("daily-bar features require exact instrument/session bindings")
        if (
            self.artifact_type == "MacroObservation"
            and self.observation_key_field != "observation_period"
        ):
            raise ValueError("macro revisions must be grouped by observation period")
        if (
            self.artifact_type == "MacroObservation"
            and not {"series_key", "authority"} <= self.identity.keys()
        ):
            raise ValueError("macro features require exact series and authority bindings")
        if any(
            isinstance(part, bool) or isinstance(part, int) and part < 0 for part in self.value_path
        ):
            raise ValueError("feature path indices must be nonnegative integers")
        return self


class RegimeFeaturePolicy(StrictModel):
    schema_version: Literal["regime-feature-policy-v1"] = "regime-feature-policy-v1"
    version: str = Field(min_length=1)
    history_tier: Literal["CORE_LONG_HISTORY", "ENRICHED_HISTORY"]
    series: tuple[RegimeSeriesSpec, ...] = Field(min_length=1)
    # Selection happens after availability filtering, never using final revised history.
    vintage: Literal["FIRST_OBSERVED", "LATEST_AS_OF"] = "LATEST_AS_OF"

    @model_validator(mode="after")
    def unique_series(self) -> RegimeFeaturePolicy:
        if len({item.series_id for item in self.series}) != len(self.series):
            raise ValueError("regime feature policy has duplicate series ids")
        return self


class RegimeSeriesAudit(StrictModel):
    series_id: str
    normalized_value: float | None
    raw_transformed_value: float | None = None
    observation_count: int = 0
    age_seconds: float | None = None
    status: Literal["READY", "MISSING", "STALE", "INSUFFICIENT_HISTORY"]
    source_artifact_ids: tuple[str, ...] = ()
    source_object_hashes: tuple[str, ...] = ()
    ood_score: float | None = None


class RegimeFeatureBuildReport(StrictModel):
    schema_version: Literal["regime-feature-build-report-v1"] = "regime-feature-build-report-v1"
    as_of: AwareDatetime
    policy_hash: str
    history_tier: str
    vintage: str
    snapshot: MarketRegimeFeatureSnapshot
    series_audits: tuple[RegimeSeriesAudit, ...]
    report_hash: str
    production_admission: Literal["NOT_ADMITTED"] = "NOT_ADMITTED"

    def assert_integrity(self) -> None:
        body = self.model_dump(exclude={"report_hash", "schema_version", "production_admission"})
        body["snapshot"] = self.snapshot
        body["series_audits"] = self.series_audits
        if self.as_of != self.snapshot.as_of or content_hash(body) != self.report_hash:
            raise ValueError("feature report content hash or time mismatch")
        if self.snapshot.source_revisions.get("feature_policy") != self.policy_hash:
            raise ValueError("feature snapshot policy lineage mismatch")
        for audit in self.series_audits:
            if len(audit.source_artifact_ids) != len(audit.source_object_hashes):
                raise ValueError("feature source lineage id/hash lengths differ")
            if self.snapshot.source_revisions.get(audit.series_id) != content_hash(
                {
                    "ids": audit.source_artifact_ids,
                    "hashes": audit.source_object_hashes,
                }
            ):
                raise ValueError("feature snapshot does not bind its source audits")


class _Point(StrictModel):
    artifact_id: str
    object_hash: str
    observed_at: AwareDatetime
    available_at: AwareDatetime
    key: str
    value: float
    source_edition_at: AwareDatetime | None = None


def _timestamp(value: Any) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("feature source time must be timezone-aware")
    return result.astimezone(UTC)


def _number_at(payload: Any, path: tuple[str | int, ...]) -> float:
    value = payload
    for part in path:
        if not isinstance(value, (dict, list, tuple)):
            raise ValueError("feature source path traverses a scalar")
        try:
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(part, int):
                value = value[part]
            else:
                raise ValueError("list-valued feature sources require integer path indices")
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("feature source path does not exist") from exc
    if isinstance(value, bool):
        raise ValueError("boolean cannot serve as a numeric market feature")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("feature source value is not numeric") from exc
    if not math.isfinite(result):
        raise ValueError("feature source value must be finite")
    return result


class RegimeFeatureBuilder:
    def __init__(
        self, state: StateStore, objects: ObjectStore, policy: RegimeFeaturePolicy
    ) -> None:
        self.state = state
        self.objects = objects
        self.policy = RegimeFeaturePolicy.model_validate(policy.model_dump())
        self.policy_hash = content_hash(self.policy)

    def build(
        self,
        *,
        as_of: datetime,
        artifact_ids_by_series: Mapping[str, Sequence[str]],
    ) -> RegimeFeatureBuildReport:
        if content_hash(self.policy) != self.policy_hash:
            raise ValueError("feature policy changed after it was frozen")
        as_of = _timestamp(as_of)
        if as_of > utc_now():
            raise ValueError("regime features cannot be built at a future time")
        unknown = set(artifact_ids_by_series) - {item.series_id for item in self.policy.series}
        if unknown:
            raise ValueError(f"series not registered by the feature policy: {sorted(unknown)}")
        # Only an invocation-local payload cache; all reads verify immutable bytes.
        cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        audits = []
        for spec in self.policy.series:
            points = self._visible_points(
                spec, artifact_ids_by_series.get(spec.series_id, ()), as_of, cache
            )
            audits.append(self._compute(spec, points, as_of))
        by_series = {item.series_id: item for item in audits}
        coverage: dict[str, float] = {}
        fields: dict[str, float | None] = {}
        for family, field in FAMILY_FIELDS.items():
            specs = [item for item in self.policy.series if item.family == family]
            ready = [
                (item, by_series[item.series_id].normalized_value)
                for item in specs
                if by_series[item.series_id].status == "READY"
            ]
            total_weight = sum(item.weight for item in specs)
            ready_weight = sum(item.weight for item, _ in ready)
            coverage[family] = ready_weight / total_weight if total_weight else 0.0
            fields[field] = (
                sum(item.weight * float(value) for item, value in ready if value is not None)
                / ready_weight
                if ready_weight
                else None
            )
        revisions = {
            item.series_id: content_hash(
                {
                    "ids": item.source_artifact_ids,
                    "hashes": item.source_object_hashes,
                }
            )
            for item in audits
        }
        revisions["feature_policy"] = self.policy_hash
        ood_values = [item.ood_score for item in audits if item.ood_score is not None]
        body = {
            "as_of": as_of,
            **fields,
            "family_coverage": coverage,
            "source_revisions": revisions,
            "ood_score": max(ood_values) if ood_values else None,
        }
        snapshot = MarketRegimeFeatureSnapshot(
            feature_snapshot_id="regime-features:" + content_hash(body),
            **body,
        )
        report_body = {
            "as_of": as_of,
            "policy_hash": self.policy_hash,
            "history_tier": self.policy.history_tier,
            "vintage": self.policy.vintage,
            "snapshot": snapshot,
            "series_audits": tuple(audits),
        }
        return RegimeFeatureBuildReport(report_hash=content_hash(report_body), **report_body)

    def _visible_points(
        self,
        spec: RegimeSeriesSpec,
        ids: Sequence[str],
        as_of: datetime,
        cache: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    ) -> list[_Point]:
        groups: dict[str, list[_Point]] = {}
        for artifact_id in dict.fromkeys(ids):
            if artifact_id not in cache:
                record = self.state.artifact_record(artifact_id)
                if record is None:
                    raise ValueError("feature source artifact is not registered")
                if record["type"] not in _SOURCE_MODELS:
                    raise ValueError("feature source uses an unsupported canonical type")
                model: type[BaseModel] = getattr(
                    import_module(_SOURCE_MODELS[record["type"]]), record["type"]
                )
                parsed = model.model_validate_json(
                    self.objects.get_bytes(str(record["object_hash"]))
                )
                if (
                    getattr(parsed, "schema_version", record["schema_version"])
                    != record["schema_version"]
                ):
                    raise ValueError("feature source schema version does not match the registry")
                cache[artifact_id] = (record, parsed.model_dump(mode="json"))
            record, payload = cache[artifact_id]
            if record["type"] != spec.artifact_type:
                raise ValueError("feature source type differs from its series binding")
            try:
                observed = _timestamp(payload[spec.observed_at_field])
                available = _timestamp(payload[spec.available_at_field])
                key = str(payload[spec.observation_key_field])
            except KeyError as exc:
                raise ValueError("feature source lacks its bound temporal identity") from exc
            # Later releases are invisible to this evaluation, including revised values.
            if available > as_of or observed > as_of:
                continue
            if available < observed:
                raise ValueError("source availability precedes observation")
            for name in ("available_to_system_at", "available_at", "captured_at", "ingested_at"):
                if payload.get(name) is not None and _timestamp(payload[name]) > available:
                    raise ValueError("feature binding backdates canonical source availability")
            if any(str(payload.get(name)) != value for name, value in spec.identity.items()):
                raise ValueError("feature source belongs to a different bound identity")
            if spec.unit_field is not None and payload.get(spec.unit_field) != spec.expected_unit:
                raise ValueError("feature source unit differs from its registered definition")
            if spec.artifact_type == "DailyBarObservation":
                if payload.get("adjustment_mode") != "NONE":
                    raise ValueError(
                        "raw-price features cannot silently use adjusted execution prices"
                    )
                source_snapshot = self.state.get_snapshot(str(payload["source_snapshot_id"]))
                if source_snapshot is None or source_snapshot.fetch_status.value != "SUCCEEDED":
                    raise ValueError("daily-bar source snapshot is missing or unsuccessful")
                if source_snapshot.available_to_system_at > available:
                    raise ValueError("daily-bar availability predates its raw source snapshot")
                self.objects.get_bytes(source_snapshot.object_sha256)
            source_edition_at = None
            if spec.artifact_type == "MacroObservation":
                self.objects.get_bytes(str(payload["source_hash"]))
                source_edition_at = _timestamp(payload["published_at"])
                if source_edition_at > available:
                    raise ValueError("macro publication is later than canonical availability")
            if spec.artifact_type == "FinancialIntegrityEvidencePack" and (
                payload.get("status") != "SUCCEEDED" or payload.get("coverage_status") != "COMPLETE"
            ):
                raise ValueError("financial feature lacks complete verified financial coverage")
            point = _Point(
                artifact_id=artifact_id,
                object_hash=record["object_hash"],
                observed_at=observed,
                available_at=available,
                key=key,
                value=_number_at(payload, spec.value_path),
                source_edition_at=source_edition_at,
            )
            groups.setdefault(key, []).append(point)

        def vintage_key(item: _Point) -> tuple[datetime, datetime]:
            edition = item.source_edition_at or item.available_at
            if self.policy.vintage == "FIRST_OBSERVED":
                return item.available_at, edition
            return edition, item.available_at

        selected = []
        for versions in groups.values():
            versions.sort(key=lambda item: (*vintage_key(item), item.artifact_id))
            candidate = versions[0] if self.policy.vintage == "FIRST_OBSERVED" else versions[-1]
            if any(
                vintage_key(item) == vintage_key(candidate) and item.value != candidate.value
                for item in versions
            ):
                raise ValueError("conflicting observations have the same vintage availability")
            selected.append(candidate)
        if spec.artifact_type == "MacroObservation":
            # Release dates order vintages, not economic periods. A late January
            # revision must never become the latest February/March observation.
            period_shapes = set()
            for point in selected:
                if re.fullmatch(r"[0-9]{4}-Q[1-4]", point.key):
                    period_shapes.add("quarter")
                elif re.fullmatch(r"[0-9]{4}-[0-9]{2}", point.key):
                    datetime.fromisoformat(point.key + "-01")
                    period_shapes.add("month")
                elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", point.key):
                    datetime.fromisoformat(point.key)
                    period_shapes.add("day")
                elif re.fullmatch(r"[0-9]{4}", point.key):
                    datetime.fromisoformat(point.key + "-01-01")
                    period_shapes.add("year")
                else:
                    raise ValueError(
                        "macro observation period must use a canonical sortable format"
                    )
            if len(period_shapes) > 1:
                raise ValueError("different macro frequencies must use separate series bindings")
            selected.sort(key=lambda item: item.key)
        else:
            selected.sort(key=lambda item: (item.observed_at, item.key))
            if len({item.observed_at for item in selected}) != len(selected):
                raise ValueError("one numeric series cannot mix duplicate observation times")
        return selected

    @staticmethod
    def _compute(
        spec: RegimeSeriesSpec, points: Sequence[_Point], as_of: datetime
    ) -> RegimeSeriesAudit:
        if not points:
            return RegimeSeriesAudit(
                series_id=spec.series_id, normalized_value=None, status="MISSING"
            )
        # Bounded trailing history is sufficient for both transform and normalization.
        points = points[-(spec.window + spec.normalization_window) :]
        age = (as_of - points[-1].observed_at).total_seconds()
        base = {
            "series_id": spec.series_id,
            "observation_count": len(points),
            "age_seconds": age,
            "source_artifact_ids": tuple(item.artifact_id for item in points),
            "source_object_hashes": tuple(item.object_hash for item in points),
        }
        if age > spec.stale_after_seconds:
            return RegimeSeriesAudit(normalized_value=None, status="STALE", **base)
        values = [item.value for item in points]
        if len(values) < spec.window:
            return RegimeSeriesAudit(normalized_value=None, status="INSUFFICIENT_HISTORY", **base)

        def transform(window: list[float]) -> float:
            if spec.transform == "LEVEL":
                return window[-1]
            if spec.transform == "CHANGE":
                return window[-1] - window[0]
            if any(value <= 0 for value in window):
                raise ValueError("return/volatility/drawdown features require positive prices")
            if spec.transform == "RETURN":
                return window[-1] / window[0] - 1
            if spec.transform == "DRAWDOWN":
                return window[-1] / max(window) - 1
            returns = [
                math.log(right / left) for left, right in zip(window, window[1:], strict=False)
            ]
            return statistics.pstdev(returns) * math.sqrt(spec.annualization_periods)

        transformed = [
            transform(values[end - spec.window : end])
            for end in range(spec.window, len(values) + 1)
        ]
        current = transformed[-1]
        ood = None
        if spec.normalization == "TRAILING_PERCENTILE":
            history = transformed[:-1][-spec.normalization_window :]
            if len(history) < spec.minimum_history:
                return RegimeSeriesAudit(
                    normalized_value=None, status="INSUFFICIENT_HISTORY", **base
                )
            percentile = (
                sum(value < current for value in history)
                + 0.5 * sum(value == current for value in history)
            ) / len(history)
            normalized = 2 * percentile - 1
            low, high = min(history), max(history)
            excess = max(low - current, current - high, 0)
            ood = min(1.0, excess / max(high - low, spec.scale))
        elif spec.normalization == "UNIT_INTERVAL":
            if not 0 <= current <= 1:
                raise ValueError("unit-interval feature is outside its declared scale")
            normalized = 2 * current - 1
        else:
            normalized = max(-1.0, min(1.0, current / spec.scale))
        return RegimeSeriesAudit(
            normalized_value=spec.direction * normalized,
            raw_transformed_value=current,
            status="READY",
            ood_score=ood,
            **base,
        )

    def register(self, report: RegimeFeatureBuildReport) -> str:
        report = RegimeFeatureBuildReport.model_validate(report.model_dump())
        if report.policy_hash != self.policy_hash:
            raise ValueError("feature report belongs to a different policy")
        report.assert_integrity()
        reconstructed = self.build(
            as_of=report.as_of,
            artifact_ids_by_series={
                item.series_id: item.source_artifact_ids for item in report.series_audits
            },
        )
        if reconstructed != report:
            raise ValueError("feature report differs from deterministic source reconstruction")
        policy_reference = self.objects.put_json(self.policy.model_dump(mode="json"))
        reference = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = "RegimeFeatureBuildReport:" + reference.sha256
        self.state.register_artifacts(
            [
                (
                    "RegimeFeaturePolicy:" + self.policy_hash,
                    "RegimeFeaturePolicy",
                    self.policy.schema_version,
                    policy_reference.sha256,
                    [],
                ),
                (
                    artifact_id,
                    "RegimeFeatureBuildReport",
                    report.schema_version,
                    reference.sha256,
                    sorted(
                        {
                            policy_reference.sha256,
                            *(
                                digest
                                for item in report.series_audits
                                for digest in item.source_object_hashes
                            ),
                        }
                    ),
                ),
            ]
        )
        return artifact_id

    @classmethod
    def load_registered(
        cls,
        state: StateStore,
        objects: ObjectStore,
        artifact_id: str,
    ) -> RegimeFeatureBuildReport:
        record = state.artifact_record(artifact_id)
        if record is None or record["type"] != "RegimeFeatureBuildReport":
            raise ValueError("feature-build report is not registered with its canonical type")
        report = RegimeFeatureBuildReport.model_validate_json(
            objects.get_bytes(record["object_hash"])
        )
        report.assert_integrity()
        if record["schema_version"] != report.schema_version:
            raise ValueError("feature report registry schema version mismatch")
        policy_record = state.artifact_record("RegimeFeaturePolicy:" + report.policy_hash)
        if policy_record is None or policy_record["type"] != "RegimeFeaturePolicy":
            raise ValueError("feature report is missing its registered frozen policy")
        policy = RegimeFeaturePolicy.model_validate_json(
            objects.get_bytes(policy_record["object_hash"])
        )
        if (
            content_hash(policy) != report.policy_hash
            or policy_record["object_hash"] not in record["input_hashes"]
        ):
            raise ValueError("feature report does not bind the exact registered policy")
        rebuilt = cls(state, objects, policy).build(
            as_of=report.as_of,
            artifact_ids_by_series={
                item.series_id: item.source_artifact_ids for item in report.series_audits
            },
        )
        if rebuilt != report:
            raise ValueError("registered feature report differs from source reconstruction")
        return report
