"""Frozen, source-bound factor samples and read-only canonical outcome reports.

The existing feature producer and price-outcome reader own the facts. This
module freezes a prospective selection in ArtifactStore and joins later outcomes;
it never changes a candidate, blind cohort, allocation, or account.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from astock.candidates.factor_diagnostics import FactorObservation, evaluate_factor_panel
from astock.candidates.registered_features import (
    SupplementalFeatureRequest,
    build_registered_features,
)
from astock.core.hashing import content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_sources.service import FinancialSourceService
from astock.market_data.official_calendar import OfficialTradingCalendarConfig
from astock.research.debate_evaluation import QualityPolicy
from astock.research.outcome_feedback import evaluate_reference_price_path
from astock.research.reference_outcomes import ReferencePriceInputReader
from astock.schemas.financial import FinancialIndustryProfile
from astock.schemas.market import Market
from astock.schemas.paper import ReplayFeeSchedule


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactorMember(_Frozen):
    company_id: str = Field(pattern=r"^[0-9]{6}$")
    market: Market
    period_end: date
    industry_profile: FinancialIndustryProfile
    business_group_id: str = Field(min_length=1, max_length=120)
    supplemental: SupplementalFeatureRequest | None = None

    @model_validator(mode="after")
    def stock_identity(self) -> FactorMember:
        if self.market not in {Market.XSHG, Market.XSHE, Market.BJSE}:
            raise ValueError("factor samples require an A-share stock identity")
        if self.supplemental is not None and self.supplemental.market is not self.market:
            raise ValueError("factor supplemental market differs from its member")
        return self


class FactorFreezeRequest(_Frozen):
    panel_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
    population_id: str = Field(min_length=1, max_length=120)
    factor_id: str = Field(min_length=1, max_length=120)
    formula_version: str = Field(min_length=1, max_length=120)
    feature_as_of: AwareDatetime
    benchmark_id: str = Field(pattern=r"^INDEX:[0-9]{6}$")
    holding_sessions: int = Field(ge=1, le=250)
    quantiles: int = Field(default=5, ge=2, le=10)
    members: tuple[FactorMember, ...] = Field(min_length=1, max_length=200)
    fee_artifact_ids: tuple[str, ...] = Field(default=(), max_length=16)
    reference_shares: int = Field(default=100, ge=100, le=1000000)

    @model_validator(mode="after")
    def bounded_selection(self) -> FactorFreezeRequest:
        ids = [(item.market, item.company_id) for item in self.members]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate factor members cannot inflate the sample")
        if len(set(self.fee_artifact_ids)) != len(self.fee_artifact_ids):
            raise ValueError("fee artifacts must be distinct")
        if self.reference_shares % 100:
            raise ValueError("reference cost model requires explicit whole A-share lots")
        return self


class FrozenFactorMember(_Frozen):
    instrument_id: str
    business_group_id: str
    value: Decimal | None
    feature_status: str
    reason_codes: tuple[str, ...]
    unit: str
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature: dict[str, Any]
    financial_releases: tuple[dict[str, Any], ...]


class DiscoveryFactorPanel(_Frozen):
    schema_version: Literal["discovery-factor-panel-v1"] = "discovery-factor-panel-v1"
    request: FactorFreezeRequest
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signal_at: AwareDatetime
    members: tuple[FrozenFactorMember, ...]
    source_hashes: tuple[str, ...]
    fee_hashes: dict[str, str]
    historical_backtest_claim: Literal[False] = False

    @model_validator(mode="after")
    def frozen_binding(self) -> DiscoveryFactorPanel:
        if self.request_hash != content_hash(self.request.model_dump(mode="json")):
            raise ValueError("frozen factor request identity mismatch")
        if self.request.feature_as_of > self.signal_at:
            raise ValueError("factor sources were not visible at the frozen signal")
        expected = sorted(f"{item.market.value}:{item.company_id}" for item in self.request.members)
        if sorted(item.instrument_id for item in self.members) != expected:
            raise ValueError("frozen factor members differ from the selected population")
        for item in self.members:
            if item.value is not None and not item.value.is_finite():
                raise ValueError("factor values must be finite or missing")
            if item.feature.get("metric_id") != self.request.factor_id:
                raise ValueError("frozen factor metric differs from the selected metric")
            if item.feature.get("formula_version") != self.request.formula_version:
                raise ValueError("frozen factor formula differs from the selected formula")
        return self


class FactorReportRequest(_Frozen):
    panel_artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    evaluated_at: AwareDatetime
    max_rows: int = Field(default=10000, ge=1, le=10000)

    @model_validator(mode="after")
    def distinct_panels(self) -> FactorReportRequest:
        if len(set(self.panel_artifact_ids)) != len(self.panel_artifact_ids):
            raise ValueError("duplicate factor panels cannot inflate the sample")
        return self


def _read_object(
    state: StateStore, objects: ObjectStore, identity: str, kind: str,
) -> tuple[bytes, dict]:
    record = state.artifact_record(identity)
    if record is None or record["type"] != kind:
        raise ValueError("registered factor input type mismatch")
    raw = objects.get_bytes(record["object_hash"])
    if len(raw) > 2000000 or sha256_bytes(raw) != record["object_hash"]:
        raise ValueError("registered factor input integrity or byte budget failed")
    return raw, record


def _snapshot_hashes(
    state: StateStore, snapshot_ids: Iterable[str], *, as_of: datetime,
) -> set[str]:
    hashes: set[str] = set()
    for snapshot_id in snapshot_ids:
        snapshot = state.get_snapshot(snapshot_id)
        if snapshot is None or snapshot.available_to_system_at > as_of:
            raise ValueError("factor raw source is absent or unavailable at cutoff")
        hashes.add(snapshot.object_sha256)
    return hashes


def _source_hashes(state: StateStore, value: object, *, as_of: datetime) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"source_object_hash", "object_hash", "page_text_hash"}:
                if (isinstance(item, str) and len(item) == 64
                        and not set(item) - set("0123456789abcdef")):
                    result.add(item)
            if key in {"artifact_id", "capture_artifact_id"} and isinstance(item, str):
                record = state.artifact_record(item)
                if record is None:
                    raise ValueError("feature input is not registered")
                result.add(str(record["object_hash"]))
            if key in {"source_snapshot_id", "admission_snapshot_id"} and isinstance(item, str):
                result.update(_snapshot_hashes(state, (item,), as_of=as_of))
            if key == "source_snapshot_ids" and isinstance(item, (list, tuple)):
                if any(not isinstance(identifier, str) for identifier in item):
                    raise ValueError("factor source snapshot identity must be a string")
                result.update(_snapshot_hashes(state, item, as_of=as_of))
            result.update(_source_hashes(state, item, as_of=as_of))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_source_hashes(state, item, as_of=as_of))
    return result


def _financial_source_hashes(
    state: StateStore, objects: ObjectStore, release: dict[str, Any], *,
    instrument_id: str, as_of: datetime,
) -> set[str]:
    """Retain the actual byte dependencies already admitted by the certified reader."""
    raw, record = _read_object(
        state, objects, "financial-source:" + release["release_id"],
        "FinancialSourceReleaseManifest",
    )
    manifest = json.loads(raw)
    if (content_hash(manifest) != release["manifest_hash"]
            or manifest["instrument_id"] != instrument_id):
        raise ValueError("factor financial source identity or semantic binding mismatch")
    hashes = {str(record["object_hash"]), str(manifest["instrument_manifest_object_hash"])}
    snapshot_ids = (
        set(manifest["raw_snapshot_ids"]) | set(manifest["official_lineage_snapshot_ids"])
        | {manifest["official_snapshot_id"]}
    )
    return hashes | _snapshot_hashes(state, snapshot_ids, as_of=as_of)


def load_factor_panel(
    state: StateStore, objects: ObjectStore, identity: str,
) -> tuple[DiscoveryFactorPanel, str]:
    raw, record = _read_object(state, objects, identity, "DiscoveryFactorPanel")
    panel = DiscoveryFactorPanel.model_validate_json(raw)
    if record["schema_version"] != panel.schema_version:
        raise ValueError("factor panel registry version mismatch")
    expected = set(panel.source_hashes) | set(panel.fee_hashes.values())
    if set(record.get("input_hashes") or ()) != expected:
        raise ValueError("factor panel source lineage mismatch")
    if any(not objects.verify(value) for value in expected):
        raise ValueError("frozen factor source object is unavailable")
    return panel, str(record["object_hash"])


def _factor_time_window(value: datetime, as_of: datetime, max_age_hours: int) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("factor priority source time must be aware")
    age = (as_of - value).total_seconds()
    if not 0 <= age <= max_age_hours * 3600:
        raise ValueError("factor priority input is outside its explicit freshness window")


def _technical_priority_window(
    member: FrozenFactorMember, inputs: dict[str, Any], *, as_of: datetime,
    max_age_hours: int, calendar: OfficialTradingCalendarConfig | None,
) -> None:
    if calendar is None:
        raise ValueError("technical factor priority requires the official trading calendar")
    latest = date.fromisoformat(str(inputs.get("latest_session_date", "")))
    market = Market(member.instrument_id.split(":", 1)[0])
    shanghai = ZoneInfo("Asia/Shanghai")
    local = as_of.astimezone(shanghai)
    close = datetime.combine(latest, time(15), shanghai)
    sessions = calendar.open_dates(market, latest, local.date())
    if sessions is None or latest not in sessions or close > local:
        raise ValueError("technical factor latest session is not a known completed session")
    # Count wall-clock hours only on officially open dates. A Friday close stays
    # usable over a closed weekend; a new capture cannot refresh a stale bar history.
    elapsed = 0.0
    for day in sessions:
        start = datetime.combine(day, time.min, shanghai)
        left, right = max(close, start), min(local, start + timedelta(days=1))
        elapsed += max(0.0, (right - left).total_seconds())
    if elapsed > max_age_hours * 3600:
        raise ValueError("technical factor latest session is outside its freshness window")


def validate_factor_priority_window(
    panel: DiscoveryFactorPanel, *, as_of: datetime, max_age_hours: int,
    calendar: OfficialTradingCalendarConfig | None = None,
) -> None:
    """Validate frozen current inputs; historical revision baselines do not become stale inputs."""
    if as_of.tzinfo is None or as_of.utcoffset() is None or not 1 <= max_age_hours <= 720:
        raise ValueError("factor overlay requires an aware cutoff and bounded freshness")
    _factor_time_window(panel.signal_at, as_of, max_age_hours)
    _factor_time_window(panel.request.feature_as_of, as_of, max_age_hours)
    for member in panel.members:
        if member.value is None:
            continue
        inputs = member.feature.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ValueError("factor priority current input metadata is invalid")
        captures = inputs.get("captures", [])
        if not isinstance(captures, (list, tuple)):
            raise ValueError("factor priority capture metadata is invalid")
        current = [inputs, *captures]
        for capture in current:
            if not isinstance(capture, dict):
                raise ValueError("factor priority capture metadata is invalid")
            available = capture.get("available_to_system_at")
            if available is not None:
                _factor_time_window(datetime.fromisoformat(str(available)), as_of, max_age_hours)
        if member.feature.get("source_class") == "CANONICAL_RAW_DAILY":
            _technical_priority_window(
                member, inputs, as_of=as_of, max_age_hours=max_age_hours, calendar=calendar,
            )


def load_current_factor_members(
    state: StateStore, objects: ObjectStore, identity: str, *,
    as_of: datetime, max_age_hours: int, calendar: OfficialTradingCalendarConfig | None = None,
) -> tuple[DiscoveryFactorPanel, str, tuple[FrozenFactorMember, ...], tuple[str, ...]]:
    """Use frozen observations for current queue ordering without rewriting their history."""
    from astock.financial_sources.repository import FinancialSourceReleaseRepository

    panel, digest = load_factor_panel(state, objects, identity)
    validate_factor_priority_window(
        panel, as_of=as_of, max_age_hours=max_age_hours, calendar=calendar,
    )
    repository = FinancialSourceReleaseRepository(state)
    current = []
    excluded = []
    for member in panel.members:
        superseded = False
        for release in member.financial_releases:
            raw, _ = _read_object(
                state, objects, "financial-source:" + release["release_id"],
                "FinancialSourceReleaseManifest",
            )
            manifest = json.loads(raw)
            latest = repository.get(
                manifest["company_id"], manifest["period_end"], manifest["period_type"],
                as_of=as_of,
            )
            if latest is None or latest["release_id"] != release["release_id"]:
                superseded = True
        if superseded:
            excluded.append(member.instrument_id)
        else:
            current.append(member)
    return panel, digest, tuple(current), tuple(sorted(excluded))


def freeze_factor_panel(
    service: FinancialSourceService, request: FactorFreezeRequest, policy: QualityPolicy,
    *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """Explicit mutation: freeze actual reader output; repeated panel IDs keep the first vintage."""
    identity = "DiscoveryFactorPanel:" + request.panel_id
    fingerprint = content_hash(request.model_dump(mode="json"))
    if service.state.artifact_record(identity) is not None:
        panel, object_hash = load_factor_panel(service.state, service.objects, identity)
        if panel.request_hash != fingerprint:
            raise ValueError("factor panel identity is already frozen to another selection")
        return {"status": "FROZEN", "artifact_id": identity, "object_hash": object_hash,
                "signal_at": panel.signal_at.isoformat(), "idempotent_replay": True}
    signal = clock()
    if signal.tzinfo is None or signal.utcoffset() is None or request.feature_as_of > signal:
        raise ValueError("factor freeze requires an actual aware signal after its input cutoff")
    if request.holding_sessions not in policy.holding_sessions:
        raise ValueError("factor horizon is outside the existing outcome policy")
    members = []
    source_hashes: set[str] = set()
    for member in sorted(request.members, key=lambda item: (item.market.value, item.company_id)):
        packet = build_registered_features(
            service, company_id=member.company_id, period_end=member.period_end,
            as_of=request.feature_as_of, industry_profile=member.industry_profile,
            business_group_id=member.business_group_id, supplemental=member.supplemental,
        )
        rows = [item for item in packet["features"] if item["metric_id"] == request.factor_id]
        if len(rows) != 1 or rows[0]["formula_version"] != request.formula_version:
            raise ValueError("selected factor or formula is absent from the registered producer")
        feature = rows[0]
        if feature["unit"] == "CATEGORY":
            raise ValueError("categorical guidance is not a numeric ranked factor")
        value = Decimal(str(feature["value"])) if feature["value"] is not None else None
        source_hashes.update(_source_hashes(
            service.state, feature, as_of=request.feature_as_of,
        ))
        selected_facts = set(feature["source_fact_ids"])
        feature_releases = tuple(
            release for release in packet["releases"]
            if selected_facts.intersection(release["fact_ids"])
        )
        for release in feature_releases:
            source_hashes.update(_financial_source_hashes(
                service.state, service.objects, release,
                instrument_id=f"{member.market.value}:{member.company_id}",
                as_of=request.feature_as_of,
            ))
        members.append(FrozenFactorMember(
            instrument_id=f"{member.market.value}:{member.company_id}",
            business_group_id=member.business_group_id, value=value,
            feature_status=str(feature["status"]), reason_codes=tuple(feature["reason_codes"]),
            unit=str(feature["unit"]),
            dependency_fingerprint=str(feature["dependency_fingerprint"]),
            feature=feature, financial_releases=feature_releases,
        ))
    fee_hashes = {}
    for fee_id in request.fee_artifact_ids:
        raw, record = _read_object(service.state, service.objects, fee_id, "ReplayFeeSchedule")
        fee = ReplayFeeSchedule.model_validate_json(raw)
        if fee.created_at > signal:
            raise ValueError("fee assumption was not available at the frozen signal")
        fee_hashes[fee_id] = str(record["object_hash"])
    if any(not service.objects.verify(value) for value in source_hashes):
        raise ValueError("verified feature producer returned an unavailable source hash")
    panel = DiscoveryFactorPanel(
        request=request, request_hash=fingerprint, signal_at=signal, members=tuple(members),
        source_hashes=tuple(sorted(source_hashes)), fee_hashes=fee_hashes,
    )
    ref = service.objects.put_json(panel.model_dump(mode="json"))
    service.state.register_artifact(
        artifact_id=identity, artifact_type="DiscoveryFactorPanel",
        schema_version=panel.schema_version, object_hash=ref.sha256,
        input_hashes=sorted(source_hashes | set(fee_hashes.values())),
    )
    return {"status": "FROZEN", "artifact_id": identity, "object_hash": ref.sha256,
            "signal_at": signal.isoformat(), "idempotent_replay": False}


def _reference_cost(
    state: StateStore, objects: ObjectStore, panel: DiscoveryFactorPanel,
    instrument_id: str, path: dict,
) -> Decimal | None:
    """Exact scheduled commissions/taxes on a declared lot; not modeled fills or realized profit."""
    from astock.paper_trading.replay import PaperReplayService
    from astock.schemas.paper import OrderSide

    if not panel.fee_hashes:
        return None
    fees = []
    for identity, expected in panel.fee_hashes.items():
        raw, record = _read_object(state, objects, identity, "ReplayFeeSchedule")
        if record["object_hash"] != expected:
            raise ValueError("frozen factor fee reference has changed")
        fees.append(ReplayFeeSchedule.model_validate_json(raw))
    market = Market(instrument_id.split(":")[0])
    stamps = [datetime.fromisoformat(path[key]).date()
              for key in ("entry_reference_time", "exit_reference_time")]
    schedules = []
    for stamp in stamps:
        applicable = [item for item in fees
                      if market in item.applicable_markets and item.effective_from <= stamp]
        if not applicable:
            return None
        latest = max(item.effective_from for item in applicable)
        matches = [item for item in applicable if item.effective_from == latest]
        if len(matches) != 1:
            raise ValueError("factor fee schedule has conflicting effective versions")
        schedules.append(matches[0])
    prices = [Decimal(path[key]) * 100
              for key in ("entry_reference_price", "exit_reference_price")]
    if any(value != value.to_integral_value() for value in prices):
        raise ValueError("reference costs require exact stock prices in fen")
    quantity = panel.request.reference_shares
    initial, terminal = (int(value) * quantity for value in prices)
    buy = sum(PaperReplayService._fees(OrderSide.BUY, initial, schedules[0]))
    sell = sum(PaperReplayService._fees(OrderSide.SELL, terminal, schedules[1]))
    gross = Decimal(terminal) / Decimal(initial) - 1
    net = Decimal(terminal - sell) / Decimal(initial + buy) - 1
    return gross - net


def evaluate_registered_factor_panels(
    state: StateStore, objects: ObjectStore, parquet_root, calendar: OfficialTradingCalendarConfig,
    policy: QualityPolicy, request: FactorReportRequest,
) -> dict:
    """Read frozen actual feature selections and independently verified later price paths."""
    loaded = [(*load_factor_panel(state, objects, identity), identity)
              for identity in request.panel_artifact_ids]
    if sum(len(panel.members) for panel, _, _ in loaded) > request.max_rows:
        raise ValueError("factor report row budget exceeded")
    configurations = {(
        panel.request.factor_id, panel.request.formula_version, panel.request.population_id,
        panel.request.benchmark_id, panel.request.holding_sessions, panel.request.quantiles,
        panel.request.reference_shares, tuple(sorted(panel.fee_hashes.items())),
    ) for panel, _, _ in loaded}
    if len(configurations) != 1:
        raise ValueError("factor reports require the same formula, population, horizon and costs")
    days = [panel.signal_at.astimezone(ZoneInfo("Asia/Shanghai")).date() for panel, _, _ in loaded]
    if len(set(days)) != len(days):
        raise ValueError("one daily factor panel per session prevents duplicate economic samples")
    prices = ReferencePriceInputReader(
        state, objects, parquet_root, evaluated_at=request.evaluated_at, max_bars=policy.max_bars,
    )
    observations = []
    unavailable: Counter[str] = Counter()
    members_report = []
    for panel, panel_hash, identity in sorted(loaded, key=lambda item: item[0].signal_at):
        for member in panel.members:
            stock, benchmark, actions, lineage = prices.load(
                member.instrument_id, panel.request.benchmark_id, panel.signal_at,
            )
            path = evaluate_reference_price_path(
                panel.signal_at, member.instrument_id, panel.request.benchmark_id,
                stock, benchmark, calendar, policy, evaluated_at=request.evaluated_at,
                holding_sessions=panel.request.holding_sessions, corporate_action_dates=actions,
            )
            member_result = {"panel_artifact_id": identity, "panel_object_hash": panel_hash,
                             "instrument_id": member.instrument_id, "status": path["status"],
                             "feature_status": member.feature_status, "lineage": lineage}
            members_report.append(member_result)
            if path["status"] != "MATURE":
                unavailable[str(path["status"])] += 1
                continue
            deduction = _reference_cost(state, objects, panel, member.instrument_id, path)
            sources = (identity, *panel.fee_hashes,
                       *(str(item["artifact_id"]) for item in lineage["daily_inputs"]))
            action = lineage.get("corporate_actions")
            if action is not None:
                sources += (str(action["artifact_id"]),)
            observations.append(FactorObservation(
                instrument_id=member.instrument_id, group_id=member.business_group_id,
                signal_at=panel.signal_at, feature_available_at=panel.request.feature_as_of,
                outcome_end_at=datetime.fromisoformat(path["exit_reference_time"]),
                outcome_available_at=request.evaluated_at,
                feature_value=float(member.value) if member.value is not None else None,
                forward_return=float(Decimal(path["gross_price_return"])),
                cost_return=float(deduction) if deduction is not None else None,
                dependency_fingerprint=member.dependency_fingerprint, source_ids=sources,
            ))
    first = loaded[0][0].request
    result = evaluate_factor_panel(
        tuple(observations), factor_id=first.factor_id, formula_version=first.formula_version,
        evaluated_at=request.evaluated_at, quantiles=first.quantiles, max_rows=request.max_rows,
    )
    result.update({
        "status": "DIAGNOSTIC" if result["evaluated_count"] else "NOT_EVALUABLE",
        "frozen_input_count": sum(len(panel.members) for panel, _, _ in loaded),
        "unavailable_outcomes": dict(sorted(unavailable.items())), "members": members_report,
        "panels": [{"artifact_id": identity, "object_hash": value}
                   for _, value, identity in loaded],
        "selection_basis": "PROSPECTIVE_FROZEN_READER_OUTPUT",
        "outcome_basis": "VERIFIED_NO_ACTION_REFERENCE_PRICES",
        "cost_basis": ("DECLARED_LOT_SCHEDULED_FEE_MODEL_NO_FILL_OR_SLIPPAGE_MODEL"
                       if first.fee_artifact_ids else "UNKNOWN"),
        "fee_model_optional": True, "persistent_writes": False,
        "independent_sample_claim": False,
        "historical_point_in_time_backtest_claim": False,
        "experiment_promotion_allowed": False,
    })
    result["report_id"] = "registered-factor-diagnostics:" + content_hash(result)
    return result
