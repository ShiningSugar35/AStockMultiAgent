"""Read-only feature producer over certified financial releases.

The existing financial source service owns provenance, version and byte checks.
This adapter never downloads data, changes seeds, or allocates research slots.
Industry/comparability input remains a labelled research assumption.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from astock.candidates.discovery_features import (
    build_discovery_features,
    feature_manifest,
    observed_peer_percentile,
    official_guidance_direction,
    paired_relative_change,
)
from astock.core.hashing import content_hash, sha256_bytes
from astock.documents import DocumentPageRepository
from astock.documents.official_web import load_official_web_capture
from astock.financial_sources.service import FinancialSourceService
from astock.providers.forecasts import (
    SampledForecastObservation,
    load_forecast_capture,
    select_latest_forecasts,
)
from astock.providers.tushare_reference import TushareMetricCapture, load_metric_capture
from astock.providers.tushare_structured import SHANGHAI
from astock.schemas import DocumentType, Market, PageExtractionMethod
from astock.schemas.financial import FinancialFact, FinancialIndustryProfile, FinancialPeriodType
from astock.schemas.financial_sources import FinancialStatementScope


def _required_periods(period_end: date) -> tuple[date, ...]:
    if (period_end.month, period_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise ValueError("features require a calendar quarter end")
    annual_year = period_end.year if period_end.month == 12 else period_end.year-1
    periods = {date(annual_year-offset, 12, 31) for offset in range(4)}
    for year in (period_end.year, period_end.year-1):
        stamp = period_end.replace(year=year)
        periods.add(stamp)
        periods.add(date(year, ((stamp.month-1)//3)*3+1, 1)-timedelta(days=1))
    return tuple(sorted(periods))


def build_registered_features(
    service: FinancialSourceService, *, company_id: str, period_end: date,
    as_of: datetime, industry_profile: FinancialIndustryProfile, business_group_id: str,
    supplemental: SupplementalFeatureRequest | None = None,
) -> dict[str, Any]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("registered features require an aware information boundary")
    if len(company_id) != 6 or not company_id.isascii() or not company_id.isdigit():
        raise ValueError("feature company identity must be a six-digit code")
    facts: dict[str, FinancialFact] = {}
    scopes: dict[str, FinancialStatementScope] = {}
    releases = []
    missing = []
    unbound = []
    for stamp in _required_periods(period_end):
        period = (
            FinancialPeriodType.ANNUAL if stamp.month == 12 else
            FinancialPeriodType.SEMIANNUAL if stamp.month == 6 else FinancialPeriodType.QUARTERLY
        )
        release = service.read_certified_release(company_id, stamp, period, as_of=as_of)
        if release is None:
            missing.append(stamp.isoformat())
            continue
        manifest = release.manifest
        if manifest.company_id != company_id or manifest.available_to_system_at > as_of:
            raise ValueError("financial feature source identity or visibility mismatch")
        releases.append({
            "release_id": manifest.release_id,
            "manifest_hash": content_hash(manifest.model_dump(mode="json")),
            "fact_ids": [fact.fact_id for fact in release.facts],
            "available_at": manifest.available_to_system_at.isoformat(),
        })
        for fact in release.facts:
            # The certified fact points to the official report, while source
            # observations point to the structured provider. Bind the shared
            # statement/field/period contract rather than equating their IDs.
            matched = [item for item in release.observations if (
                item.field_code == fact.field_code and item.statement_type == fact.statement_type
                and item.period_end == fact.period_end and item.period_type == fact.period_type
                and item.duration_semantics == fact.duration_semantics
            )]
            if not matched or any(item.statement_scope is not FinancialStatementScope.CONSOLIDATED
                                  for item in matched):
                unbound.append(fact.fact_id)
                continue
            if (fact.company_id != company_id or not fact.source_snapshot_id
                    or fact.source_snapshot_id != manifest.official_snapshot_id):
                raise ValueError("certified financial feature fact provenance mismatch")
            if fact.fact_id in facts and facts[fact.fact_id] != fact:
                raise ValueError("same financial fact identity has conflicting payloads")
            facts[fact.fact_id] = fact
            scopes[fact.source_snapshot_id] = FinancialStatementScope.CONSOLIDATED
    packet = build_discovery_features(
        company_id=company_id, industry_profile=industry_profile,
        business_group_id=business_group_id, period_end=period_end,
        fact_state_version=content_hash(releases), facts=tuple(facts.values()),
        scope_by_snapshot_id=scopes,
    )
    supplemental_rows = _supplemental_features(
        service, supplemental, company_id=company_id, as_of=as_of,
        industry_profile=industry_profile, business_group_id=business_group_id,
    )
    return {
        "schema_version": "registered-discovery-features-v1",
        "status": "CURRENT_RESEARCH_ONLY",
        "company_id": company_id, "as_of": as_of.isoformat(),
        "period_end": period_end.isoformat(), "formula_version": packet.formula_version,
        "semantic_fingerprint": content_hash({
            "financial": packet.semantic_fingerprint,
            "supplemental": supplemental_rows,
        }),
        "fact_state_version": packet.fact_state_version,
        "features": [*feature_manifest(packet), *supplemental_rows],
        "releases": releases, "missing_periods": missing,
        "unbound_scope_fact_ids": sorted(set(unbound)),
        "group_basis": "CALLER_SUPPLIED_RESEARCH_ASSUMPTION",
        "industry_profile": industry_profile.value, "business_group_id": business_group_id,
        "historical_point_in_time_claim": False,
        "standalone_recommendation_allowed": False,
        "blind_tranche_mutation_allowed": False,
        "research_budget_mutation_allowed": False,
        "independent_vote_weight": 0,
        "read_only": True,
    }


class PeerValuationReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    company_id: str = Field(pattern=r"^[0-9]{6}$")
    market: Market
    capture_artifact_id: str = Field(min_length=1)
    industry_profile: FinancialIndustryProfile
    business_group_id: str = Field(min_length=1)


class OfficialGuidanceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capture_artifact_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    period_start: date
    period_end: date


class SupplementalFeatureRequest(BaseModel):
    """Explicit, bounded local references; never a request to acquire more data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    market: Market
    valuation_capture_id: str | None = None
    technical_daily_release_id: str | None = Field(default=None, min_length=1, max_length=240)
    peer_valuations: tuple[PeerValuationReference, ...] = Field(default=(), max_length=19)
    forecast_capture_ids: tuple[str, ...] = Field(default=(), max_length=3)
    prior_forecast_capture_ids: tuple[str, ...] = Field(default=(), max_length=3)
    forecast_fiscal_year: int | None = Field(default=None, ge=1990, le=2099)
    prior_forecast_as_of: AwareDatetime | None = None
    official_guidance: OfficialGuidanceReference | None = None

    @model_validator(mode="after")
    def comparable_requests(self) -> SupplementalFeatureRequest:
        if self.market not in {Market.XSHG, Market.XSHE, Market.BJSE}:
            raise ValueError("supplemental features require a stock market")
        if self.peer_valuations and not self.valuation_capture_id:
            raise ValueError("peer comparison requires a target valuation capture")
        peer_ids = [(item.market, item.company_id) for item in self.peer_valuations]
        if len(peer_ids) != len(set(peer_ids)):
            raise ValueError("duplicate peer companies cannot increase the sample")
        for identifiers in (self.forecast_capture_ids, self.prior_forecast_capture_ids):
            if len(identifiers) != len(set(identifiers)) or any(not item for item in identifiers):
                raise ValueError("forecast capture references must be unique and nonempty")
        if self.forecast_capture_ids and self.forecast_fiscal_year is None:
            raise ValueError("forecast features require an explicit target fiscal year")
        if bool(self.prior_forecast_capture_ids) != (self.prior_forecast_as_of is not None):
            raise ValueError("prior forecast captures require their actual information cutoff")
        if self.prior_forecast_capture_ids and not self.forecast_capture_ids:
            raise ValueError("forecast revision requires a current sample")
        return self


def _observation(
    metric_id: str, value: Decimal | str | None, *, unit: str, formula: str,
    source_class: str, inputs: dict[str, object], reason_codes: tuple[str, ...] = (),
    status: str | None = None,
) -> dict[str, object]:
    """Keep an observation's economic meaning and complete read dependencies visible."""
    payload: dict[str, object] = {
        "metric_id": metric_id, "value": str(value) if value is not None else None,
        "unit": unit, "formula": formula, "formula_version": "discovery-observations-v1",
        "status": status or ("CALCULATED" if value is not None else "INSUFFICIENT_DATA"),
        "reason_codes": reason_codes, "source_class": source_class,
        "source_fact_ids": (), "evidence_ids": (), "inputs": inputs,
        "missing_policy": "UNKNOWN_NOT_ZERO", "usage": "RESEARCH_PRIORITY_ONLY",
        "historical_point_in_time_claim": False, "standalone_recommendation_allowed": False,
        "independent_vote_weight": 0,
    }
    payload["information_key"] = content_hash(inputs)
    payload["dependency_fingerprint"] = content_hash(payload)
    return payload


_VALUATION_FIELDS = {
    "pe_ttm": ("VENDOR_PE_TTM", "RATIO"),
    "pb": ("VENDOR_PB", "RATIO"),
    "dv_ttm": ("VENDOR_DIVIDEND_YIELD_TTM", "PERCENT"),
}


def _daily_capture(
    service: FinancialSourceService, artifact_id: str, *, company_id: str,
    market: Market, as_of: datetime,
) -> TushareMetricCapture:
    capture = load_metric_capture(
        service.state, service.objects, artifact_id,
        subject_id=f"{market.value}:{company_id}", as_of=as_of,
    )
    if capture.endpoint != "daily_basic":
        raise ValueError("valuation features require daily_basic")
    session = datetime.strptime(capture.period, "%Y%m%d").date()
    if capture.available_to_system_at < datetime.combine(session, time(15), SHANGHAI):
        raise ValueError("valuation session is not closed at capture")
    return capture


def _valuation_values(capture: TushareMetricCapture) -> dict[str, Decimal | None]:
    values: dict[str, Decimal | None] = {}
    metrics = {item.metric: item for item in capture.metrics}
    for field, (_name, unit) in _VALUATION_FIELDS.items():
        item = metrics.get(field)
        if item is not None and (item.unit != unit or item.period_basis != "SESSION_END"):
            raise ValueError("valuation observation unit or period basis mismatch")
        value = item.value if item is not None else None
        if value is not None and not value.is_finite():
            raise ValueError("valuation observation must be finite")
        values[field] = value
    return values


def _capture_dependency(capture: TushareMetricCapture) -> dict[str, object]:
    return {
        "artifact_id": capture.artifact_id, "subject_id": capture.subject_id,
        "source_snapshot_id": capture.source_snapshot_id,
        "source_object_hash": capture.source_object_hash, "row_hash": capture.row_hash,
        "available_to_system_at": capture.available_to_system_at.isoformat(),
        "session": capture.period, "source_group": capture.source_group,
        "formal_use_allowed": capture.formal_use_allowed,
        "historical_vintage_verified": capture.historical_vintage_verified,
    }


def _valuation_features(
    service: FinancialSourceService, request: SupplementalFeatureRequest | None, *,
    company_id: str, as_of: datetime, industry_profile: FinancialIndustryProfile,
    business_group_id: str,
) -> list[dict[str, object]]:
    if request is None or request.valuation_capture_id is None:
        return [
            _observation(name, None, unit=unit, formula="reported_secondary_session_metric",
                         source_class="SECONDARY_STRUCTURED", inputs={},
                         reason_codes=("REGISTERED_DAILY_BASIC_INPUT_MISSING",))
            for name, unit in _VALUATION_FIELDS.values()
        ]
    capture = _daily_capture(
        service, request.valuation_capture_id, company_id=company_id,
        market=request.market, as_of=as_of,
    )
    values = _valuation_values(capture)
    dependency = _capture_dependency(capture)
    result = []
    for field, (name, unit) in _VALUATION_FIELDS.items():
        value = values[field]
        reasons: tuple[str, ...] = ()
        status = None
        if value is None:
            reasons = ("VENDOR_FIELD_MISSING",)
        elif field in {"pe_ttm", "pb"} and value <= 0:
            value, status, reasons = None, "NOT_APPLICABLE", ("NON_POSITIVE_MULTIPLE",)
        elif field == "dv_ttm" and value < 0:
            value, status, reasons = None, "CONFLICTED", ("NEGATIVE_DIVIDEND_YIELD",)
        result.append(_observation(
            name, value, unit=unit, formula="reported_secondary_session_metric",
            source_class="SECONDARY_STRUCTURED", inputs=dependency | {"field": field},
            reason_codes=reasons, status=status,
        ))
    result.extend(_peer_features(
        service, request, capture, company_id=company_id, as_of=as_of,
        industry_profile=industry_profile, business_group_id=business_group_id,
    ))
    return result


def _peer_features(
    service: FinancialSourceService, request: SupplementalFeatureRequest,
    target: TushareMetricCapture, *, company_id: str, as_of: datetime,
    industry_profile: FinancialIndustryProfile, business_group_id: str,
) -> list[dict[str, object]]:
    target_id = target.subject_id
    panel = {target_id: _valuation_values(target)}
    dependencies = [_capture_dependency(target)]
    excluded: list[str] = []
    for peer in request.peer_valuations:
        subject = f"{peer.market.value}:{peer.company_id}"
        if subject == target_id:
            raise ValueError("target cannot be repeated as a peer")
        capture = _daily_capture(
            service, peer.capture_artifact_id, company_id=peer.company_id,
            market=peer.market, as_of=as_of,
        )
        dependencies.append(_capture_dependency(capture))
        if (
            peer.industry_profile is not industry_profile
            or peer.business_group_id != business_group_id or capture.period != target.period
        ):
            excluded.append(subject)
            continue
        panel[subject] = _valuation_values(capture)
    result = []
    for field, (name, _unit) in _VALUATION_FIELDS.items():
        values = {
            subject: (value if value is not None and
                      (value >= 0 if field == "dv_ttm" else value > 0) else None)
            for subject, row in panel.items() for value in (row[field],)
        }
        percentile, valid_ids = observed_peer_percentile(target_id, values)
        inputs: dict[str, object] = {
            "captures": dependencies, "field": field, "session": target.period,
            "industry_profile": industry_profile.value, "business_group_id": business_group_id,
            "group_basis": "CALLER_SUPPLIED_RESEARCH_ASSUMPTION",
            "eligible_company_ids": sorted(panel), "observed_company_ids": valid_ids,
            "excluded_noncomparable_ids": excluded,
            "missing_company_ids": sorted(set(panel) - set(valid_ids)),
            "sample_count": len(valid_ids), "eligible_count": len(panel),
        }
        result.append(_observation(
            name + "_PEER_PERCENTILE", percentile, unit="RATIO",
            formula="observed_same_session_cohort_midrank;minimum_distinct_companies=10",
            source_class="SECONDARY_STRUCTURED", inputs=inputs,
            reason_codes=() if percentile is not None else ("INSUFFICIENT_COMPARABLE_VALUES",),
        ))
    return result


def _forecast_panel(
    service: FinancialSourceService, identifiers: tuple[str, ...], *, company_id: str,
    market: Market, as_of: datetime,
) -> tuple[list[SampledForecastObservation], list[dict[str, object]]]:
    observations: dict[str, SampledForecastObservation] = {}
    dependencies: list[dict[str, object]] = []
    for identifier in identifiers:
        capture = load_forecast_capture(
            service.state, service.objects, identifier,
            company_id=company_id, market=market, as_of=as_of,
        )
        dependencies.append({
            "artifact_id": identifier,
            "capture_hash": content_hash(capture.model_dump(mode="json")),
            "available_to_system_at": capture.available_to_system_at.isoformat(),
            "source_snapshot_ids": capture.source_snapshot_ids,
            "window_start": capture.window_start.isoformat(),
            "window_end": capture.window_end.isoformat(), "truncated": capture.truncated,
            "coverage": capture.coverage, "historical_vintage_verified": False,
        })
        observations.update({item.observation_id: item for item in capture.observations})
    return list(observations.values()), dependencies


def _forecast_features(
    service: FinancialSourceService, request: SupplementalFeatureRequest | None, *,
    company_id: str, as_of: datetime,
) -> list[dict[str, object]]:
    names = ("SAMPLED_EPS_NEXT_YEAR_GROWTH", "SAMPLED_EPS_OBSERVED_REVISION")
    if request is None or not request.forecast_capture_ids:
        return [
            _observation(name, None, unit="RATIO", formula="median_within_institution_change",
                         source_class="SAMPLED_BROKER_FORECAST", inputs={},
                         reason_codes=("REGISTERED_FORECAST_INPUT_MISSING",))
            for name in names
        ]
    year = request.forecast_fiscal_year
    assert year is not None
    current, dependencies = _forecast_panel(
        service, request.forecast_capture_ids, company_id=company_id,
        market=request.market, as_of=as_of,
    )
    latest, conflicts = select_latest_forecasts([
        item for item in current if item.target_fiscal_year == year
    ])
    inputs: dict[str, object] = {
        "captures": dependencies, "target_fiscal_year": year, "as_of": as_of.isoformat(),
        "source_group": "EASTMONEY", "conflicting_institutions": conflicts,
        "current_institution_count": len(latest), "coverage": "BOUNDED_SAMPLE_NOT_FULL_CONSENSUS",
    }
    next_year, next_conflicts = select_latest_forecasts([
        item for item in current if item.target_fiscal_year == year + 1
    ])
    growth_pairs = _growth_pairs(latest, next_year)
    growth = _paired_forecast_feature(names[0], growth_pairs, inputs | {
        "next_fiscal_year": year + 1, "next_year_conflicting_institutions": next_conflicts,
        "growth_conflicting_institutions": sorted(set(conflicts) | set(next_conflicts)),
    })
    if not request.prior_forecast_capture_ids:
        revision = _observation(
            names[1], None, unit="RATIO", formula="median_within_institution_same_FY_revision",
            source_class="SAMPLED_BROKER_FORECAST", inputs=inputs,
            reason_codes=("OBSERVED_PRIOR_FORECAST_CAPTURE_REQUIRED",),
        )
    else:
        prior_as_of = request.prior_forecast_as_of
        assert prior_as_of is not None
        if prior_as_of >= as_of:
            raise ValueError("forecast revision requires an earlier information cutoff")
        prior, prior_dependencies = _forecast_panel(
            service, request.prior_forecast_capture_ids, company_id=company_id,
            market=request.market, as_of=prior_as_of,
        )
        previous, prior_conflicts = select_latest_forecasts([
            item for item in prior if item.target_fiscal_year == year
        ])
        pairs = _revision_pairs(latest, previous)
        revision = _paired_forecast_feature(names[1], pairs, inputs | {
            "prior_captures": prior_dependencies, "prior_as_of": prior_as_of.isoformat(),
            "prior_conflicting_institutions": prior_conflicts,
            "prior_institution_count": len(previous),
            "unpaired_current_institutions": sorted(
                {item.institution_id for item in latest}
                - {item[0].institution_id for item in pairs}
            ),
        })
    return [growth, revision]


def _growth_pairs(
    latest: list[SampledForecastObservation], following_year: list[SampledForecastObservation],
) -> list[tuple[SampledForecastObservation, SampledForecastObservation]]:
    following = {
        (item.institution_id, item.report_id, item.source_snapshot_id, item.row_hash): item
        for item in following_year
    }
    pairs = []
    for item in latest:
        key = (item.institution_id, item.report_id, item.source_snapshot_id, item.row_hash)
        next_year = following.get(key)
        if next_year is not None:
            pairs.append((next_year, item))
    return pairs


def _revision_pairs(
    current: list[SampledForecastObservation], previous: list[SampledForecastObservation],
) -> list[tuple[SampledForecastObservation, SampledForecastObservation]]:
    prior = {item.institution_id: item for item in previous}
    pairs = []
    for item in current:
        old = prior.get(item.institution_id)
        if old is None or item.available_to_system_at < old.available_to_system_at:
            continue
        if item.published_at < old.published_at:
            continue
        if item.published_at == old.published_at and item.report_id != old.report_id:
            continue
        pairs.append((item, old))
    return pairs


def _paired_forecast_feature(
    name: str, pairs: list[tuple[SampledForecastObservation, SampledForecastObservation]],
    inputs: dict[str, object],
) -> dict[str, object]:
    value, count, invalid_denominators = paired_relative_change([
        (current.value, previous.value) for current, previous in pairs
    ])
    details = [
        {
            "institution_id": current.institution_id,
            "current_observation_id": current.observation_id,
            "baseline_observation_id": previous.observation_id,
            "current_report_id": current.report_id, "baseline_report_id": previous.report_id,
            "current_value": str(current.value), "baseline_value": str(previous.value),
            "current_fiscal_year": current.target_fiscal_year,
            "baseline_fiscal_year": previous.target_fiscal_year,
            "unit": current.unit,
        }
        for current, previous in pairs
    ]
    reasons = ()
    if value is None:
        reasons = ("NO_COMPARABLE_POSITIVE_BASELINE_PAIRS",)
    return _observation(
        name, value, unit="RATIO",
        formula="median((current-baseline)/positive_baseline),matched_institution_and_report_version",
        source_class="SAMPLED_BROKER_FORECAST", inputs=inputs | {
            "pairs": details, "sample_count": count,
            "non_positive_baseline_count": invalid_denominators,
        }, reason_codes=reasons,
    )


def _guidance_feature(
    service: FinancialSourceService, reference: OfficialGuidanceReference | None, *,
    company_id: str, as_of: datetime,
) -> dict[str, object]:
    name = "OFFICIAL_EARNINGS_GUIDANCE_DIRECTION"
    if reference is None:
        return _observation(
            name, None, unit="CATEGORY", formula="explicit_official_native_page_category",
            source_class="PRIMARY_OFFICIAL_WEB", inputs={},
            reason_codes=("OFFICIAL_GUIDANCE_INPUT_MISSING",),
        )
    capture, document, snapshot = load_official_web_capture(
        service.state, service.objects, reference.capture_artifact_id,
        company_id=company_id, as_of=as_of, capabilities=("disclosure.document",),
    )
    if document.document_type is not DocumentType.ANNOUNCEMENT:
        raise ValueError("guidance must be a dedicated announcement, not a financial report")
    page = DocumentPageRepository(service.state).get_page_by_id(reference.page_id)
    if (
        page is None or page.document_id != document.document_id
        or page.snapshot_id != snapshot.snapshot_id
        or page.extraction_method is not PageExtractionMethod.NATIVE_TEXT or page.ocr_applied
    ):
        raise ValueError("guidance requires the matching registered native PDF page")
    raw = service.objects.get_bytes(page.text_object_sha256)
    text = raw.decode("utf-8")
    if (sha256_bytes(raw) != page.text_sha256
            or sum(not char.isspace() for char in text) != page.text_char_count):
        raise ValueError("guidance native page text integrity mismatch")
    if "业绩预告" not in text:
        direction, reasons = None, ("GUIDANCE_DOCUMENT_SEMANTICS_NOT_EXPLICIT",)
    else:
        direction, reasons = official_guidance_direction(
            text, company_id=company_id,
            period_start=reference.period_start, period_end=reference.period_end,
        )
    inputs: dict[str, object] = {
        "capture_artifact_id": reference.capture_artifact_id,
        "source_object_hash": snapshot.object_sha256,
        "source_snapshot_id": snapshot.snapshot_id,
        "admission_snapshot_id": capture.admission_snapshot_id,
        "page_id": page.page_id, "page_number": page.page_number,
        "page_text_hash": page.text_sha256,
        "period_start": reference.period_start.isoformat(),
        "period_end": reference.period_end.isoformat(),
        "published_at": document.published_at.isoformat(),
        "available_to_system_at": snapshot.available_to_system_at.isoformat(),
        "source_url": document.source_url, "forecast_not_realized_fact": True,
    }
    return _observation(
        name, direction, unit="CATEGORY", formula="explicit_official_native_page_category",
        source_class="PRIMARY_OFFICIAL_WEB", inputs=inputs, reason_codes=reasons,
        status="OBSERVED" if direction is not None else (
            "CONFLICTED" if "GUIDANCE_DIRECTION_CONFLICT" in reasons else "INSUFFICIENT_DATA"
        ),
    )


def _technical_features(
    service: FinancialSourceService, request: SupplementalFeatureRequest | None, *,
    company_id: str, as_of: datetime,
) -> list[dict[str, object]]:
    """Reuse WP-62 raw-price indicators; these are research observations, not total returns."""
    names = ("TECHNICAL_EMA_TREND", "TECHNICAL_ATR_PRICE_RANGE")
    if request is None or request.technical_daily_release_id is None:
        return [
            _observation(
                name, None, unit="RATIO", formula="registered_daily_technical_observation",
                source_class="CANONICAL_RAW_DAILY", inputs={},
                reason_codes=("REGISTERED_TECHNICAL_INPUT_MISSING",),
            ) for name in names
        ]
    from astock.technical.policy import load_technical_analysis_policy
    from astock.technical.registered import analyze_registered_daily

    policy = load_technical_analysis_policy(service.root / "configs/technical_analysis.yaml")
    snapshot = analyze_registered_daily(
        service.state, service.objects, request.technical_daily_release_id,
        policy=policy, calendar_path=service.root / "configs/official_trading_calendar.yaml",
        as_of=as_of, parquet_root=service.parquet.root.parent, register=False,
    )
    if snapshot.instrument_id != f"{request.market.value}:{company_id}":
        raise ValueError("technical feature security differs from its registered daily parent")
    denominator = snapshot.ema_slow
    trend = (
        Decimal(str(snapshot.ema_fast)) / Decimal(str(denominator)) - 1
        if snapshot.ema_fast is not None and denominator is not None and denominator > 0 else None
    )
    price_range = (
        Decimal(str(snapshot.atr)) / Decimal(str(denominator))
        if snapshot.atr is not None and denominator is not None and denominator > 0 else None
    )
    dependency: dict[str, object] = {
        "artifact_id": snapshot.source_artifact_id,
        "source_object_hash": snapshot.source_object_hash,
        "source_snapshot_ids": snapshot.source_snapshot_ids,
        "technical_snapshot_id": snapshot.snapshot_id, "policy_sha256": snapshot.policy_sha256,
        "policy_version": snapshot.policy_version, "as_of": snapshot.as_of.isoformat(),
        "latest_session_date": snapshot.latest_session_date.isoformat(),
        "input_bar_count": snapshot.included_bar_count, "adjustment_mode": "NONE",
        "corporate_action_coverage": "UNPROVEN",
        "price_range_not_annualized_return_volatility": True,
        "ema_fast_period": policy.ema_fast, "ema_slow_period": policy.ema_slow,
        "atr_period": policy.atr_period,
    }
    return [
        _observation(
            name, value, unit="RATIO", formula=formula, source_class="CANONICAL_RAW_DAILY",
            inputs=dependency, reason_codes=tuple(snapshot.caution_codes) + (
                ("INSUFFICIENT_TECHNICAL_HISTORY",) if value is None else ()
            ),
        )
        for name, value, formula in (
            (names[0], trend, "EMA_fast/EMA_slow-1;raw_price_trend_only"),
            (names[1], price_range, "Wilder_ATR/EMA_slow;lower_means_narrower_price_ranges"),
        )
    ]


def _supplemental_features(
    service: FinancialSourceService, request: SupplementalFeatureRequest | None, *,
    company_id: str, as_of: datetime, industry_profile: FinancialIndustryProfile,
    business_group_id: str,
) -> list[dict[str, object]]:
    return [
        *_valuation_features(
            service, request, company_id=company_id, as_of=as_of,
            industry_profile=industry_profile, business_group_id=business_group_id,
        ),
        *_forecast_features(service, request, company_id=company_id, as_of=as_of),
        *_technical_features(service, request, company_id=company_id, as_of=as_of),
        _guidance_feature(
            service, request.official_guidance if request else None,
            company_id=company_id, as_of=as_of,
        ),
    ]
